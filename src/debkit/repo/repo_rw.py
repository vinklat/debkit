"""Class containing repository with write ability.

Inherits from Repo.
"""

from __future__ import annotations

import logging
import re
from email.utils import formatdate
from typing import TYPE_CHECKING, NamedTuple

from txstore import StorageException, StorageNull, compare_digests, normalize_digests

from debkit.repo import Repo
from debkit.repo.component import Component
from debkit.repo.params import FLAT_COMPONENT, SOURCE_ARCH

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.package import Package

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class CheckReport(NamedTuple):
    """What `RepoRW.check` found in the repository's own storage, and what it fixed.

    Attributes:
        missing: Payloads and parsed component indices the listing lacks, sorted.
        orphans: Listed paths that no index claims, sorted.
        failed: Payloads that failed verification or could not be read.
        fixed: Payloads and orphans deleted.
    """

    missing: list[str]
    orphans: list[str]
    failed: int
    fixed: int


class RepoRW(Repo):
    """A `Repo` that inserts, removes, checks, and saves.

    Whether anything changed is the caller's diff against a snapshot; `save` writes only the
    containers that changed, then the root Release that lists them.
    """

    def setup(self, fields: dict[str, str] | None = None) -> None:
        """Setup the repository.

        A flat layout has one component slot and its Release lists no components, so a
        `Components` field is dropped -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            fields: The fields to set. Defaults to None.
        """
        logger.info("Setting up a new repository.")
        if fields is not None and self.params.is_flat:
            fields = {key: value for key, value in fields.items() if key != "Components"}
        self.release.setup(fields=fields)
        self._selection = None

    def save(self) -> bool:
        """Save the repository.

        Returns:
            True if a component saved, False otherwise.
        """
        saved: bool = False
        for component in self.components.values():
            saved = component.save() or saved

        if saved:
            self.release.fields["Date"] = formatdate(localtime=False, usegmt=True)
            # a flat Release lists no components; its single slot keeps whatever name it loaded
            if not self.params.is_flat:
                self.release.fields["Components"] = " ".join(self.components)
            # the fields just changed, and so did the index digests the components recorded
            self.release.modified = True
            self.release.save()

        return saved

    def reset(self) -> None:
        """Reset the repository."""
        self.components = {}
        self.release.reset()

    def remove_pkg(self, fields_patterns: dict[str, re.Pattern[str]]) -> bool:
        """Drop packages matching ``fields_patterns`` from the indices and delete their payloads.

        Several arch indices list one `all` payload, so each payload is deleted once. A payload
        another dist sharing the root lists is kept, and so is every payload while another
        dist's claims are unknown -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            fields_patterns: Compiled pattern per package field.

        Raises:
            StorageException: If the backend fails to delete a payload.

        Returns:
            True iff at least one package was removed.
        """
        logger.info(
            "Removing packages with fields patterns: %s",
            {field: rx.pattern for field, rx in fields_patterns.items()},
        )

        removed: set[str] = set()
        for component in self.components.values():
            removed |= component.remove_pkg(fields_patterns)
        if not removed:
            return False

        shared = self.shared_root()
        for filename in sorted(removed):
            if not shared.deletable(filename):
                logger.info(
                    "Keep package: %s (another dist sharing the root may list it)", filename
                )
                continue
            logger.info("Remove package: %s", filename)
            self.storage.delete(filename)
        return True

    def binary_archs(self) -> list[str]:
        """Get the repository architectures a binary package can be inserted into.

        A mirrored `Sources` index is read-only, so `source` never receives a `.deb`
        -> [VALIDATION.md](VALIDATION.md#sources-stanzas). The empty arch names no
        `binary-<arch>/` directory, so only a flat repo inserts under it
        -> [VALIDATION.md](VALIDATION.md#architectures).

        Returns:
            The repository architectures without the source one.
        """
        return [
            arch
            for arch in self.selection.archs
            if arch != SOURCE_ARCH and (arch or self.params.is_flat)
        ]

    def insert_pkg(self, component_name: str, arch: str, package: Package) -> None:
        """Add deb package object to the repo.

        A flat layout has one component slot, so ``component_name`` is ignored there and the
        package joins the component the repo loaded
        -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            component_name: The name of the component.
            arch: The architecture.
            package: The Package object to insert.
        """
        if self.params.is_flat:
            component_name = next(iter(self.components), FLAT_COMPONENT)

        if component_name in self.components:
            component = self.components[component_name]
        else:
            component = Component(self.storage, self.params, self.release, component_name)
            component.setup([arch])
            self.components[component_name] = component

        component.insert_pkg(arch, package)

    def transfer_from(
        self, source_storage: Storage, path_src: str, path_dst: str | None = None
    ) -> dict[str, str]:
        """Transfer a file from an external storage into this repo's storage.

        Backend-specific ACL handling is delegated to the destination
        storage (e.g. ``StorageS3.default_acl``); this method does not
        hard-code any ACL string.

        Args:
            source_storage: The storage to read from.
            path_src: Source path in `source_storage`.
            path_dst: Destination path in this repo. Defaults to `path_src`.

        Returns:
            Digests of the transferred object.
        """
        return source_storage.transfer(path_src, self.storage, path_dst=path_dst)

    def holds(self, path: str, digests: dict[str, str]) -> bool:
        """Report whether the stored metadata of `path` records `digests`.

        The digest-skip decision shared by payloads and indices: one metadata round trip,
        never a read of the object; normalization and comparison stay in txstore's digest
        helpers -> [CRYPTO.md](CRYPTO.md#implementation-rules). The caller has already
        established that `path` exists and that digest verification is on.

        Args:
            path: The object path.
            digests: The digests another repository records for the object.

        Returns:
            True when the held copy matches and need not be transferred.
        """
        return compare_digests(digests, normalize_digests(self.storage.get_metadata(path)))

    def release_agrees(self, path: str, digests: dict[str, str]) -> bool:
        """Report whether this repository's loaded root Release records `digests` for an index.

        The Release is already loaded, so its digest block costs nothing, while
        `holds` is a round trip per key and a full Debian Release lists hundreds of
        indices -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            path: The index's storage path, identical in both repositories.
            digests: The digests another repository's Release records for it.

        Returns:
            True when this Release records matching digests under its own layout's key.
        """
        held = self.release.digests.get(self.params.release_key(path))
        if not held:
            return False
        return compare_digests(digests, normalize_digests(held))

    def held_digests(self, path: str) -> dict[str, str] | None:
        """Return the digests of the bytes this repo's storage holds at `path`, if any.

        Args:
            path: The object path.

        Returns:
            Its digests, or None when the storage does not hold it.
        """
        if not self.storage.exists(path):
            return None
        return self._read_digests(path)

    def check(self, *, fix: bool = False, purge_orphans: bool = True) -> CheckReport:
        """Check the repository against its own indices.

        Every payload the indices list is read back and verified; with `fix`, a corrupt one
        is deleted, so a later sync or insert places it again, and so is every orphan, while a
        valid one has the digests read back recorded as object metadata, which the sync's
        held-payload skip reads -> [CONFIG.md](CONFIG.md#check). A corrupt payload whose bytes
        another dist sharing the root records is kept for that dist, and so is every one while
        another dist's payloads are unknown -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            fix: Delete corrupt payloads and orphans, and record each valid payload's
                digests. Defaults to False.
            purge_orphans: With `fix`, delete the orphans too. Defaults to True.

        Returns:
            The missing and orphan paths, and the failed and fixed counts.
        """
        missing, orphans, shared = self.consistency_lists(with_digests=fix)
        payloads = sorted(self._payload_files())
        absent = set(missing)
        failed = 0
        fixed = 0

        for cnt, file in enumerate(payloads, start=1):
            if file in absent:
                logger.warning("Skip (%d/%d): %s (missing)", cnt, len(payloads), file)
                continue

            try:
                valid, digests = self._verify_file(file, progress=(cnt, len(payloads)))
                if valid and fix:
                    self.record_digests(file, digests)
                elif not valid:
                    logger.warning("Checksum verify failed: %s", file)
                    failed += 1
                    if fix and shared.corrupt_deletable(file, digests):
                        self.delete(file)
                        fixed += 1
                    elif fix:
                        logger.warning(
                            "Keep: %s (another dist sharing the root may record its bytes)", file
                        )
            except StorageException as exc:
                logger.error("Error checking file: %s: %s", file, exc)
                failed += 1

        if fix and purge_orphans:
            for file in orphans:
                try:
                    self.delete(file)
                    fixed += 1
                except StorageException as exc:
                    logger.error("Error deleting file: %s: %s", file, exc)

        return CheckReport(missing, orphans, failed, fixed)

    def delete(self, file: str) -> None:
        """Delete `file` from this repository's storage, logging it.

        Args:
            file: Object path to delete.
        """
        logger.info("Delete: %s", file)
        self.storage.delete(file)

    def record_digests(self, file: str, digests: dict[str, str]) -> None:
        """Record `digests` as the object metadata of `file`, which the sync's skip reads.

        Args:
            file: Object path in this repository's storage.
            digests: The digests of the bytes it holds.
        """
        self.storage.add_metadata(file, digests)

    def _verify_file(self, file: str, progress: tuple[int, int]) -> tuple[bool, dict[str, str]]:
        """Read a payload back and verify it against the indices; nothing is written.

        Args:
            file: The payload path.
            progress: Current and total payload counts, for the log.

        Returns:
            Whether the file is valid, and the digests of the bytes read back.
        """
        logger.info("Verify (%d/%d): %s", progress[0], progress[1], file)

        digests = self._read_digests(file)

        if self.params.verify_digests and not self.verify_checksum(file, digests):
            return False, digests

        return True, digests

    def _read_digests(self, path: str) -> dict[str, str]:
        """Read an object of this repo's storage through and return its digests.

        Args:
            path: The object path.

        Returns:
            Its digests.
        """
        # read through to a digest sink -> [ROADMAP.md](ROADMAP.md#known-deviations)
        return self.storage.transfer(path, StorageNull())
