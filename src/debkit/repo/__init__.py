"""Base class containing Debian repository indices and data.

Method load() loads the repository.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from txstore import StorageException, is_unsafe_path

from debkit.repo.component import Component, IndexLoad
from debkit.repo.hashes import parse_size
from debkit.repo.index_selection import match_index_tail, select_index_entry
from debkit.repo.package import PackageException
from debkit.repo.packages import diff_packages
from debkit.repo.params import (
    ARCH_ANY,
    FLAT_COMPONENT,
    RepoParams,
    is_safe_name,
    is_valid_arch,
)
from debkit.repo.release import ROOT_RELEASE_KEYS, Release, RootRelease
from debkit.repo.repo_diff import RepoDiff

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package
    from debkit.repo.release import IndexEntry

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# each component and arch slot -> its package map, by `Filename`, as `Repo.snapshot` takes it
type RepoSnapshot = dict[tuple[str, str], dict[str, Package]]


@dataclass(frozen=True)
class IndexTarget:
    """One index in the mirror set -> [VALIDATION.md](VALIDATION.md#index-selection).

    `digests` are what the source root Release records for the path, or `None` for
    the root Release files, which are not listed in their own digest block.
    `known_published` says the Release load or a parse already fetched the object.
    """

    digests: dict[str, str] | None
    known_published: bool


class RepoReadException(Exception):
    """Exception raised for errors in reading the repository."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Repo read: {message}")


def _package_size(filename: str, package: Package) -> int:
    """Read a package's `Size` as a byte count.

    Raises:
        PackageException: The `Size` is not a decimal byte count.
    """
    size = parse_size(package.fields["Size"])
    if size is None:
        raise PackageException(f"{filename} has a malformed Size: {package.fields['Size']!r}")
    return size


class Repo:
    """Object containing Debian repository indices and data.

    Attributes:
        storage: The storage for the repo.
        params: The input parameters.
        gpg: The GnuPG object.
        release: The root Release file of repo.
        components: The components of the repo by its names
    """

    def __init__(self, storage: Storage, params: RepoParams, gpg: GnuPG | None = None) -> None:
        """Init repo object.

        Args:
            storage: The storage for the repo.
            params: The input parameters.
            gpg: The GnuPG object. Defaults to None.
        """
        self.storage = storage
        self.params = params
        self.gpg = gpg
        self.release = RootRelease(storage, params, gpg=gpg)
        self.components: dict[str, Component] = {}
        # Release-token lines already logged. A sync reloads its source after each pass and
        # builds the index set again, so each is logged once per repository; the selection is
        # not cached at load, since a reload, `setup()`, and `save()` change the Release
        self._release_lines_logged: set[str] = set()
        # payloads reported as Release-owned. Building the package set again in one run, as a
        # sync and its check do, reports each once
        self._owned_payloads_warned: set[str] = set()

        logger.debug(
            "Repo: Storage=%s, Dist=%s, Components=%s",
            storage,
            params.dist,
            params.components,
        )

    def snapshot(self) -> RepoSnapshot:
        """Snapshot the package maps, as a diff against this repo's later state needs them.

        A load or a mutation replaces packages rather than changing one in place, so copied
        maps keep describing the repo as it was. A flat slot snapshots its whole index, not
        its arch view: a flat sync copies the index whole, so it plans every payload in it
        -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Returns:
            Each component and arch slot → a copy of its package map.
        """
        return {
            (comp_name, arch): dict(packages.packages)
            for comp_name, component in self.components.items()
            for arch, packages in component.packages.items()
        }

    def _log_once(self, level: int, msg: str, *args: object) -> None:
        """Log a line about the root Release unless this repository already logged it.

        Args:
            level: The logging level.
            msg: The message format.
            *args: The format arguments.
        """
        line = msg % args
        if line not in self._release_lines_logged:
            self._release_lines_logged.add(line)
            logger.log(level, line)

    def get_components(self) -> Generator[str]:
        """Get the components of the repo.

        Yields:
            The name of a component.
        """
        # try get from params
        if isinstance(self.params.components, list):
            yield from self.params.components

        # try get from Release file; its tokens name storage directories, so an unsafe one
        # is dropped like a bad architecture token
        # -> [VALIDATION.md](VALIDATION.md#debian-source-strings)
        elif "Components" in self.release.fields:
            names = []
            for name in self.release.fields.get("Components", "").split():
                if is_safe_name(name):
                    names.append(name)
                else:
                    self._log_once(
                        logging.WARNING, "Release component %r is not a safe name. Skipped.", name
                    )
            # a flat layout names one index file whatever the component, so a second slot
            # over it would save last and drop what the first one changed
            # -> [VALIDATION.md](VALIDATION.md#repository-index-paths)
            if self.params.is_flat:
                if len(names) > 1:
                    self._log_once(
                        logging.WARNING,
                        "Flat Release lists components %s; using %r.",
                        names,
                        names[0],
                    )
                names = names[:1] or [FLAT_COMPONENT]
            yield from names

        else:
            yield FLAT_COMPONENT

    def get_archs(self) -> Generator[str]:
        """Get the architectures of the repo.

        The `--architectures` selection wins; without one the Release tokens decide. A
        Release without `Architectures` yields the empty arch of a flat repo, or the
        binary architectures a componented one lists indices for
        -> [VALIDATION.md](VALIDATION.md#architectures).

        Yields:
            The name of an arch.
        """
        if isinstance(self.params.archs, list) and len(self.params.archs) > 0:
            yield from self.params.archs
            return

        archs = list(self.release_archs())
        if archs or "Architectures" in self.release.fields:
            yield from archs
        elif self.params.is_flat:
            yield ""
        else:
            yield from self._listed_binary_archs(self.get_components())

    def _listed_binary_archs(self, components: Iterable[str]) -> list[str]:
        """Get the binary architectures the root Release lists a `Packages` index for.

        Only the selected components count; a directory name that is not an architecture
        token, or `all`, is skipped like a Release token.

        Args:
            components: The selected components.

        Returns:
            Sorted architecture names, without duplicates.
        """
        archs: set[str] = set()
        for comp in components:
            for key in self.release.digests:
                match = match_index_tail(key, comp)
                if match is None or match["arch"] is None:
                    continue
                arch = match["arch"]
                if arch == ARCH_ANY:
                    continue
                if not is_valid_arch(arch):
                    self._log_once(
                        logging.WARNING, "%r in a Release index path is not an architecture.", arch
                    )
                    continue
                archs.add(arch)
        if archs:
            self._log_once(
                logging.INFO,
                "Release declares no Architectures; using its indices: %s",
                sorted(archs),
            )
        return sorted(archs)

    def release_archs(self) -> Generator[str]:
        """Get the architectures the root Release declares, ignoring the selection.

        Reads `Architectures`, else the singular `Architecture`, and skips `all`. A
        Release token that is not a well-formed architecture name is remote metadata
        steering a storage path, so it is dropped and logged
        -> [VALIDATION.md](VALIDATION.md#architectures).

        Yields:
            The name of an arch.
        """
        if "Architectures" in self.release.fields:
            field = "Architectures"
        elif "Architecture" in self.release.fields:
            field = "Architecture"
        else:
            return

        for arch in self.release.fields.get(field, "").split():
            if arch == ARCH_ANY:
                continue
            if not is_valid_arch(arch):
                self._log_once(
                    logging.WARNING,
                    "%r in Release %s is not an architecture. Skipped.",
                    arch,
                    field,
                )
                continue
            yield arch

    def load(self, mode: IndexLoad = IndexLoad.WRITABLE) -> None:
        """Load Debian repository.

        This method does a similar thing to `apt-get update` command.

        Args:
            mode: What an architecture that does not load may skip. Defaults to `WRITABLE`,
                which skips a missing index but raises on an unreadable one, since a caller
                that saves would replace it -> [VALIDATION.md](VALIDATION.md#index-loading).

        Raises:
            RepoReadException: The root Release storage fails to read, or a strict load
                selects no architecture.
            ReleaseReadException: The root Release is missing, too large, unsigned while
                signatures are checked, or a Release holds an unreadable line.
            GPGVerifyException: A signature fails, or an `InRelease` is malformed.
            ChecksumException: A per-arch Release or index fails its digest, or the two signed
                root forms differ.
            ComponentException: An architecture cannot be loaded and may not be skipped.
            PackageException: An index stanza has no `Filename`.
            StorageException: An index exists but its storage fails to read.
        """
        try:
            self.release.load()
        except StorageException as exc:
            raise RepoReadException(str(exc)) from exc

        if not self.params.is_flat and "Components" not in self.release.fields:
            self._log_once(
                logging.WARNING, "Components field not found in Release of non-flat repo."
            )

        # the arch set reads only the selection and the root Release, so it is resolved once
        archs = list(self.get_archs())
        # a strict load that selects no architecture loads nothing, and a sync would plan
        # every target index and payload away -> [VALIDATION.md](VALIDATION.md#architectures)
        if mode is IndexLoad.STRICT and not archs:
            raise RepoReadException(
                "Release selects no architecture: it declares none and lists no index."
            )

        # a reload replaces the components: one the new Release drops must not survive
        self.components = {}
        pkgs_total = 0
        for component_name in self.get_components():
            component = Component(self.storage, self.params, self.release, component_name)

            pkgs_total += component.load(archs, mode)

            self.components[component_name] = component

        logger.debug("Total %s packages in %s.", pkgs_total, self.storage)
        if pkgs_total == 0:
            logger.warning("Empty repo.")

    def dump(self) -> dict[str, dict[str, Any]]:
        """Dump the repository data to a dict.

        Returns:
            A dict representation of the repository. The dictionary includes the release, components
            of the repository.
        """
        # add Release (mandatory)
        ret: dict[str, dict[str, Any]] = {
            "release": self.release.dump(),
            "components": {},
        }

        # add components (mandatory)
        ret["components"] = {
            component_name: component.dump()
            for component_name, component in self.components.items()
        }

        return ret

    def stats(self) -> dict[str, dict[str, dict[str, int]]]:
        """Get statistics of the repository.

        Raises:
            PackageException: A package `Size` is not a decimal byte count.

        Returns:
            A dict representation of the repository statistics.
        """
        stats: dict[str, dict[str, dict[str, int]]] = {}
        for comp_name, component in self.components.items():
            stats[comp_name] = {}
            for arch in component.packages:
                slot = component.slot_packages(arch)
                stats[comp_name][arch] = {
                    "packages_total": len(slot),
                    "packages_bytes": sum(
                        _package_size(filename, package) for filename, package in slot.items()
                    ),
                }
        return stats

    def get_packages(self) -> Generator[Package]:
        """Get every package of the repository, each payload once.

        A flat layout shares one index across its arch slots, and an `all` package sits in
        every binary index of its component, so a payload is yielded for its first slot only.

        Yields:
            The package.
        """
        seen: set[str] = set()
        for component in self.components.values():
            for packages in component.packages.values():
                for package in packages.packages.values():
                    if (filename := str(package)) not in seen:
                        seen.add(filename)
                        yield package

    def diff(self, older: RepoSnapshot) -> RepoDiff:
        """Get how this repo, the newer state, differs from `older`.

        Every component and arch slot either side holds is compared, so a slot only the older
        state holds reports its packages as removed.

        Args:
            older: The older state, as `snapshot` took it.

        Returns:
            Packages added, removed and updated in this repo, by component and arch.
        """
        diff = RepoDiff()
        newer = self.snapshot()

        # always compared: equal Release bytes do not prove equal loaded packages, since a
        # load may skip an unreadable index -> [VALIDATION.md](VALIDATION.md#index-loading)
        for slot in sorted(newer.keys() | older.keys()):
            diff.record(*slot, diff_packages(newer.get(slot, {}), older.get(slot, {})))

        return diff

    def _release_blocks(self) -> Generator[Release]:
        """Yield every Release whose digest block is authoritative for this repo.

        The ownership gate and the digest check read the same blocks, so both walk
        this one iterator -> [CRYPTO.md](CRYPTO.md#verification-order).

        Yields:
            The root Release, then every component Release.
        """
        yield self.release

        for component in self.components.values():
            for packages in component.containers():
                yield packages.release

    def _release_owns(self, release_key: str) -> bool:
        """Report whether a signed Release digest block covers this key.

        The root Release is GPG-verified and every component Release is chained to it
        by digest, so a key either Release lists is authoritative for that path. A
        package stanza naming the same key must not outrank it
        -> [CRYPTO.md](CRYPTO.md#verification-order).

        Args:
            release_key: Path as the Release lists it, from `release_key()`.

        Returns:
            True when a Release digest block names the key.
        """
        return any(release_key in release.digests for release in self._release_blocks())

    def _release_keys(self) -> set[str]:
        """Collect every key a signed Release digest block covers.

        The set form of `_release_owns` for a caller testing many keys at once: it walks
        the same blocks, so both answer alike -> [CRYPTO.md](CRYPTO.md#verification-order).

        Returns:
            Every key a Release digest block lists.
        """
        return {key for release in self._release_blocks() for key in release.digests}

    def verify_checksum(self, filename: str, incoming_digests: dict[str, str]) -> bool:
        """Verify file checksum in all available indices files.

        Args:
            filename: The name of the file to verify.
            incoming_digests: The digests of the file.

        Returns:
            Whether the file checksum was verified.
        """
        release_key = self.params.release_key(filename)

        # no digest block lists the root Release files, so each copy is bound to the bytes
        # the load fetched and verified, and no stanza may vouch for one
        # -> [CRYPTO.md](CRYPTO.md#verification-order)
        if release_key in ROOT_RELEASE_KEYS:
            return self.release.verify_root_digests(release_key, incoming_digests)

        # a package index is remote metadata, so a stanza naming a path the signed Release
        # also covers would define that path's content instead of the GPG-rooted Release.
        # The Release owns every key inside its digest block, keyed by the path it lists, and
        # is checked against exactly the blocks that granted it that ownership
        # -> [CRYPTO.md](CRYPTO.md#verification-order)
        if self._release_owns(release_key):
            return any(
                release.verify_digests(release_key, incoming_digests)
                for release in self._release_blocks()
            )

        # only paths outside every block fall to the containers, each keyed by the `Filename`
        # field it parsed, so this is one dict lookup and a path no index names simply misses.
        # A suffix gate here would have to track every compression `dpkg-source` emits
        return any(
            packages.verify_checksum(filename, incoming_digests)
            for component in self.components.values()
            for packages in component.containers()
        )

    def _get_indices(self) -> dict[str, IndexTarget]:
        """Get every repository index file to mirror, with its expected digests.

        The signed root Release is the manifest: it lists each index apt may fetch,
        so every listed path is a candidate. Entries are filtered by the selected
        components and architectures and by `RepoParams.indices_exclude`
        → [VALIDATION.md](VALIDATION.md#index-exclude-globs).

        The root `Release`, `Release.gpg`, and `InRelease` files are not listed in
        the digest block, carry no digests, and are never filtered. A listed key that
        would escape the storage root is dropped
        -> [VALIDATION.md](VALIDATION.md#filename-safety). Nothing is probed
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Returns:
            Index path → the digests recorded for it and whether the source is already known to
            publish it.
        """
        ret: dict[str, IndexTarget] = {}

        components = None if self.params.is_flat else set(self.get_components())
        archs = set(self.get_archs())
        arch_universe = archs | set(self.release_archs())
        excludes = self.params.indices_exclude or []

        def selected(key: str) -> bool:
            return select_index_entry(
                key,
                components=components,
                archs=archs,
                arch_universe=arch_universe,
                excludes=excludes,
            )

        # root Release files: the manifest itself, never filtered and never listed
        # in their own digest block
        for path in self.release.get_indices_paths():
            ret[path] = IndexTarget(None, known_published=True)

        # indices already discovered while parsing, e.g. Packages files. Parsing
        # proved they exist, so they are not probed again, but the selection rules
        # still apply to them.
        for component in self.components.values():
            for path in component.get_indices_paths():
                key = self.params.release_key(path)
                if not selected(key):
                    continue
                ret[path] = IndexTarget(self.release.digests.get(key), known_published=True)

        for entry in self.release.listed_indices():
            if entry.path in ret:
                continue

            if not selected(entry.key):
                continue

            if not self._index_path_is_safe(entry):
                continue

            # listed is not published; the transfer settles it
            ret[entry.path] = IndexTarget(entry.digests, known_published=False)

        return ret

    def _index_path_is_safe(self, entry: IndexEntry) -> bool:
        """Report whether a Release-listed index path stays inside the repository.

        The digest block is remote metadata, so a hostile or malformed Release can
        name a key outside the storage root. A rejected entry is dropped from the
        index set rather than failing the transfer, so one bad record cannot stop a
        mirror -> [VALIDATION.md](VALIDATION.md#filename-safety).

        Args:
            entry: candidate index from the root Release digest block.

        Returns:
            True when the path is safe to mirror.
        """
        if is_unsafe_path(entry.key) or is_unsafe_path(entry.path):
            logger.warning("%s listed in Release is an unsafe index path. Skipped.", entry.key)
            return False
        return True

    def _payload_files(self) -> set[str]:
        """Collect the parsed payload paths, dropping any the Release owns.

        Returns:
            Paths of every parsed package the Release does not own.
        """
        return self._drop_release_owned(str(pkg) for pkg in self.get_packages())

    def _drop_release_owned(self, paths: Iterable[str]) -> set[str]:
        """Drop the stanza paths a Release digest block lists.

        A stanza `Filename` a Release digest block also lists is an index, not a
        payload: the index set alone decides whether it is copied or purged, so index
        selection and the payload copy and removal cannot disagree
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            paths: Stanza paths, as `str(Package)` gives them.

        Returns:
            The paths no Release digest block lists.
        """
        ret: set[str] = set()
        # one walk of the Release blocks, not one per package
        owned_keys = self._release_keys()

        for path in paths:
            if self.params.release_key(path) in owned_keys:
                if path not in self._owned_payloads_warned:
                    self._owned_payloads_warned.add(path)
                    logger.warning(
                        "%s is named by a Release digest block, not mirrored as a package", path
                    )
                continue
            ret.add(path)

        return ret
