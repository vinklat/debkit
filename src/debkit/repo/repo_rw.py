"""Class containing repository with write ability.

Inherits from Repo.
"""

from __future__ import annotations

import logging
import re
from email.utils import formatdate
from typing import TYPE_CHECKING, NamedTuple

from txstore import StorageException, StorageNull

from debkit.repo import Repo
from debkit.repo.component import Component
from debkit.repo.params import FLAT_COMPONENT

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package
    from debkit.repo.params import RepoParams

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
    """Class that extens Repo with modificaton functionality.

    Attributes:
        modified: Whether the repo has been modified.
    """

    def __init__(self, storage: Storage, params: RepoParams, gpg: GnuPG | None = None) -> None:
        """Initialize RepoRW.

        Args:
            storage: Storage backend of the repository.
            params: Repository parameters.
            gpg: Verifies and signs Release files. Defaults to None.
        """
        super().__init__(storage, params, gpg=gpg)
        self.modified = False

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
        self.modified = True

    def save(self) -> bool:
        """Save the repository.

        Returns:
            True if the repository was saved, False otherwise.
        """
        if not self.modified:
            return False

        saved: bool = False
        for component in self.components.values():
            saved = component.save() or saved

        if saved:
            self.release.fields["Date"] = formatdate(localtime=False, usegmt=True)
            # a flat Release lists no components; its single slot keeps whatever name it loaded
            if not self.params.is_flat:
                self.release.fields["Components"] = " ".join(self.components)
            self.release.save()

        return saved

    def reset(self) -> None:
        """Reset the repository."""
        self.components = {}
        self.release.reset()

    def remove_pkg(self, fields_patterns: dict[str, re.Pattern[str]]) -> bool:
        """Drop packages matching ``fields_patterns`` from the indices and delete their payloads.

        Several arch indices list one `all` payload, so each payload is deleted once.

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
        for filename in sorted(removed):
            logger.info("Remove package: %s", filename)
            self.storage.delete(filename)
        if removed:
            self.modified = True
        return bool(removed)

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
        self.modified = True

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

    def check(self, *, fix: bool = False, purge_orphans: bool = True) -> CheckReport:
        """Check the repository against its own indices.

        Every payload the indices list is read back and verified; with `fix`, a corrupt one
        is deleted, so a later sync copies it again, and so is every orphan
        -> [CONFIG.md](CONFIG.md#check).

        Args:
            fix: Delete corrupt payloads and orphans. Defaults to False.
            purge_orphans: With `fix`, delete the orphans too. Defaults to True.

        Returns:
            The missing and orphan paths, and the failed and fixed counts.
        """
        missing, orphans = self.consistency_lists()
        payloads = sorted(self._payload_files())
        absent = set(missing)
        failed = 0
        fixed = 0

        for cnt, file in enumerate(payloads, start=1):
            if file in absent:
                logger.warning("Skip (%d/%d): %s (missing)", cnt, len(payloads), file)
                continue

            try:
                if not self._verify_file(file, progress=(cnt, len(payloads)), fix=fix):
                    logger.warning("Checksum verify failed: %s", file)
                    failed += 1
                    if fix:
                        self._delete(file)
                        fixed += 1
            except StorageException as exc:
                logger.error("Error checking file: %s: %s", file, exc)
                failed += 1

        if fix and purge_orphans:
            for file in orphans:
                try:
                    self._delete(file)
                    fixed += 1
                except StorageException as exc:
                    logger.error("Error deleting file: %s: %s", file, exc)

        return CheckReport(missing, orphans, failed, fixed)

    def _delete(self, file: str) -> None:
        """Delete `file` from this repository's storage.

        Args:
            file: Object path to delete.
        """
        logger.info("Delete: %s", file)
        self.storage.delete(file)

    def _verify_file(self, file: str, progress: tuple[int, int], fix: bool) -> bool:
        """Read a payload back and verify it against the indices.

        Args:
            file: The payload path.
            progress: Current and total payload counts, for the log.
            fix: Record the digests as object metadata when the payload verifies.

        Returns:
            True if the file is valid, False otherwise.
        """
        logger.info("Verify (%d/%d): %s", progress[0], progress[1], file)

        # read through to a digest sink -> [ROADMAP.md](ROADMAP.md#known-deviations)
        digests = self.storage.transfer(file, StorageNull())

        if self.params.verify_digests and not self.verify_checksum(file, digests):
            return False

        if fix:
            self.storage.add_metadata(file, digests)

        return True
