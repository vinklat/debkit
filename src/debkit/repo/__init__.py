"""Base class containing Debian repository indices and data.

Method update() loads the repository.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Generator, Iterable, Mapping
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from txstore import StorageException, compare_digests, is_unsafe_path

from debkit.repo.component import Component, ComponentException
from debkit.repo.gnupg import GPGVerifyException
from debkit.repo.hashes import ChecksumException
from debkit.repo.index_selection import select_index_entry
from debkit.repo.package import PackageException
from debkit.repo.packages import Packages, PackagesException
from debkit.repo.params import (
    ARCH_ANY,
    FLAT_COMPONENT,
    SOURCE_ARCH,
    RepoParams,
    is_safe_name,
    is_valid_arch,
)
from debkit.repo.release import Release, ReleaseReadException, RootRelease
from debkit.repo.repo_diff import RepoDiff

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package
    from debkit.repo.release import IndexEntry

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# the architecture directory of a binary index key, `<comp>/binary-<arch>/Packages[.<ext>]`
_BINARY_INDEX_KEY = re.compile(r"binary-(?P<arch>[^/]+)/Packages(?:\.[^/]+)?")


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


class Repo:
    """Object containing Debian repository indices and data.

    Attributes:
        storage: The storage for the repo.
        params: The input parameters.
        gpg: The GnuPG object.
        release: The root Release file of repo.
        components: The components of the repo by its names
        modified: True if the repo has been modified, False otherwise.
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
        self.modified = False
        # payloads reported as Release-owned. Building the package set again in one run, as a
        # sync and its check do, reports each once
        self._owned_payloads_warned: set[str] = set()

        logger.debug(
            "Repo: Storage=%s, Dist=%s, Components=%s",
            storage,
            params.dist,
            params.components,
        )

    def __copy__(self) -> Repo:
        """Snapshot this repo, as a diff against its later state needs it.

        The root Release and every component and package map are copied, so a later load,
        insert, or save of the original leaves the snapshot as it was. The snapshot is a
        plain `Repo`: it is only read.

        Returns:
            A snapshot of this repo.
        """
        ret = Repo(self.storage, self.params, gpg=self.gpg)
        ret.release = copy(self.release)
        ret.components = {
            comp_name: component.copy_onto(ret.release)
            for comp_name, component in self.components.items()
        }
        ret.modified = self.modified
        return ret

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
            for name in self.release.fields.get("Components", "").split():
                if is_safe_name(name):
                    yield name
                else:
                    logger.warning("Release component %r is not a safe name. Skipped.", name)

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
            yield from self._listed_binary_archs()

    def _listed_binary_archs(self) -> list[str]:
        """Get the binary architectures the root Release lists a `Packages` index for.

        Only the selected components count; a directory name that is not an architecture
        token, or `all`, is skipped like a Release token.

        Returns:
            Sorted architecture names, without duplicates.
        """
        archs: set[str] = set()
        for comp in self.get_components():
            prefix = f"{comp}/"
            for key in self.release.digests:
                match = _BINARY_INDEX_KEY.fullmatch(key[len(prefix) :])
                if match is None or not key.startswith(prefix):
                    continue
                arch = match["arch"]
                if arch == ARCH_ANY:
                    continue
                if not is_valid_arch(arch):
                    logger.warning("%r in a Release index path is not an architecture.", arch)
                    continue
                archs.add(arch)
        if archs:
            logger.info("Release declares no Architectures; using its indices: %s", sorted(archs))
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
                logger.warning("%r in Release %s is not an architecture. Skipped.", arch, field)
                continue
            yield arch

    def update(self, strict_arch: bool = False, skip_unreadable: bool = False) -> None:
        """Load Debian repository.

        This method does a similar thing to `apt-get update` command.

        Args:
            strict_arch: Whether to use strict architecture. Defaults to False. If True, it will
                raise an exception if the architecture is not available. If False, it will skip an
                architecture without an index and continue loading other architectures.
            skip_unreadable: Whether an architecture whose index exists but cannot be read is
                skipped as well. Defaults to False, which raises: a caller that saves would
                replace that index -> [VALIDATION.md](VALIDATION.md#index-loading).

        Raises:
            RepoReadException: The root Release storage fails to read.
            ReleaseReadException: The root Release is missing, too large, or unsigned while
                signatures are checked.
            GPGVerifyException: A signature fails, or an `InRelease` is malformed.
            ChecksumException: A per-arch Release or index fails its digest, or the two signed
                root forms differ.
            ComponentException: An architecture cannot be loaded and may not be skipped.
        """
        try:
            self.release.load()
        except StorageException as exc:
            raise RepoReadException(f"{exc}") from exc

        if not self.params.is_flat and "Components" not in self.release.fields:
            logger.warning("Components field not found in Release of non-flat repo.")

        # a reload replaces the components: one the new Release drops must not survive
        self.components = {}
        pkgs_total = 0
        for component_name in self.get_components():
            component = Component(self.storage, self.params, self.release, component_name)

            pkgs_total += component.load(
                list(self.get_archs()), strict_arch=strict_arch, skip_unreadable=skip_unreadable
            )

            self.components[component_name] = component

        logger.debug("Total %s packages in %s.", pkgs_total, self.storage)
        if pkgs_total == 0:
            logger.warning("Empty repo.")

    def dump(self, brief: bool = False) -> dict[str, dict[str, Any]]:
        """Dump the repository data to a dict.

        Args:
            brief: Whether to dump a brief version of output. Defaults to False.

        Returns:
            A dict representation of the repository. The dictionary includes the release, components
            of the repository.
        """
        # add Release (mandatory)
        ret: dict[str, dict[str, Any]] = {
            "release": self.release.dump(brief=brief),
            "components": {},
        }

        # add components (mandatory)
        ret["components"] = {
            component_name: component.dump(brief=brief)
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
        ret: dict[str, dict[str, dict[str, int]]] = {}

        for comp_name, component in self.components.items():
            if comp_name not in ret:
                ret[comp_name] = {}
            for arch, packages in component.packages.items():
                if arch not in ret[comp_name]:
                    ret[comp_name][arch] = {
                        "packages_total": len(packages.packages),
                        "packages_bytes": 0,
                    }
                    for filename, package in packages.packages.items():
                        size = str(package.fields["Size"])
                        if not (size.isascii() and size.isdigit()):
                            raise PackageException(f"{filename} has a malformed Size: {size!r}")
                        ret[comp_name][arch]["packages_bytes"] += int(size)
        return ret

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

    def diff(self, other_repo: Repo) -> RepoDiff:
        """Get how this repo, the newer state, differs from `other_repo`, the older one.

        Every component and arch slot either side holds is compared, so a slot only the older
        repo holds reports its packages as removed.

        Args:
            other_repo: The older state to diff against.

        Returns:
            Packages added, removed and updated in this repo, by component and arch.
        """
        diff = RepoDiff()
        empty = Packages(self.storage, self.params)
        slots = {
            (comp_name, arch)
            for repo in (self, other_repo)
            for comp_name, component in repo.components.items()
            for arch in component.packages
        }

        # always compared: equal Release bytes do not prove equal loaded packages, since a
        # load may skip an unreadable index -> [VALIDATION.md](VALIDATION.md#index-loading)
        for comp_name, arch in sorted(slots):
            newer = self._slot(comp_name, arch) or empty
            older = other_repo._slot(comp_name, arch) or empty
            diff.update(comp_name, arch, **older.diff(newer)._asdict())

        return diff

    def _slot(self, comp_name: str, arch: str) -> Packages | None:
        """Return the package container of one component and arch, or None when absent."""
        component = self.components.get(comp_name)
        return component.packages.get(arch) if component is not None else None

    def _release_blocks(self) -> Generator[Release]:
        """Yield every Release whose digest block is authoritative for this repo.

        The ownership gate and the digest check read the same blocks, so both walk
        this one iterator -> [CRYPTO.md](CRYPTO.md#verification-order).

        Yields:
            The root Release, then every component Release.
        """
        yield self.release

        for component in self.components.values():
            for packages in component.packages.values():
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

    def verify_checksum(self, filename: str, digests_incoming: dict[str, str]) -> bool:
        """Verify file checksum in all available indices files.

        Args:
            filename: The name of the file to verify.
            digests_incoming: The digests of the file.

        Returns:
            Whether the file checksum was verified.
        """
        release_key = self.params.release_key(filename)

        # no digest block lists the root Release files, so each copy is bound to the bytes
        # the load fetched and verified, and no stanza may vouch for one
        # -> [CRYPTO.md](CRYPTO.md#verification-order)
        if release_key in ["Release", "InRelease", "Release.gpg"]:
            return self.release.verify_root_digests(release_key, digests_incoming)

        # a package index is remote metadata, so a stanza naming a path the signed Release
        # also covers would define that path's content instead of the GPG-rooted Release.
        # The Release owns every key inside its digest block, keyed by the path it lists, and
        # is checked against exactly the blocks that granted it that ownership
        # -> [CRYPTO.md](CRYPTO.md#verification-order)
        if self._release_owns(release_key):
            return any(
                release.verify_digests(release_key, digests_incoming)
                for release in self._release_blocks()
            )

        # only paths outside every block fall to the containers, each keyed by the `Filename`
        # field it parsed, so this is one dict lookup and a path no index names simply misses.
        # A suffix gate here would have to track every compression `dpkg-source` emits
        return any(
            packages.verify_checksum(filename, digests_incoming)
            for component in self.components.values()
            for packages in component.packages.values()
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
                if path in ret:
                    continue
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

    @staticmethod
    def _repair_and_purge(
        payloads: set[str],
        indices: Collection[str],
        existing_files: set[str],
        planned: set[str] | None = None,
    ) -> tuple[set[str], set[str]]:
        """Compare a target listing with the package and index sets.

        Args:
            payloads: Every payload path the package set holds.
            indices: Every index path the index set holds.
            existing_files: Paths the target listing returned.
            planned: Paths a package diff already copies or removes, left out of both results.
                Defaults to None (nothing planned).

        Returns:
            The payloads the listing lacks, and the listed paths neither set holds.
        """
        planned = planned or set()
        repair = payloads - planned - existing_files
        purge = {file for file in existing_files - payloads - planned if file not in indices}
        return repair, purge

    def consistency_lists(self, *, with_digests: bool = False) -> ConsistencyLists:
        """List what this repository's own storage lacks and what it holds unclaimed.

        Missing are the payloads and the parsed component indices the listing lacks:
        the root Release files were just read and nothing is probed, so a listing that
        disagrees with a read cannot make them missing. Orphans are listed paths that
        neither the package set nor the index set holds, and no other dist sharing the root
        claims -> [CONFIG.md](CONFIG.md#check).

        Args:
            with_digests: Also collect the digests the other dists record, for a caller that
                judges a corrupt payload. Defaults to False.

        Returns:
            Missing and orphan paths, sorted, and the shared root claims from the same
            listing; both lists empty and the claims unknown when the storage cannot list.
        """
        try:
            existing_files = set(self.storage.list())
        except NotImplementedError:
            return ConsistencyLists([], [], _unlisted_shared_root(self.params))

        indices = self._get_indices()
        missing, orphans = self._repair_and_purge(self._payload_files(), indices, existing_files)
        shared = shared_root_claims(
            self.storage, self.params, existing_files, with_digests=with_digests
        )
        orphans = {file for file in orphans if shared.deletable(file)}
        parsed = {
            path for component in self.components.values() for path in component.get_indices_paths()
        }
        root_files = set(self.release.get_indices_paths())
        missing |= (parsed & indices.keys()) - root_files - existing_files
        return ConsistencyLists(sorted(missing), sorted(orphans), shared)

    def shared_root(self) -> SharedRoot:
        """Collect what the other dists sharing this repository's root claim.

        A flat layout is never listed, since it has no `dists/` tree to share
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Returns:
            The claims; unknown when the storage cannot list, so only the own index tree is
            deletable.
        """
        if not _has_dists_tree(self.params):
            return _FLAT_ROOT
        try:
            existing_files = set(self.storage.list())
        except NotImplementedError:
            return _unlisted_shared_root(self.params)
        return shared_root_claims(self.storage, self.params, existing_files)


class SharedRoot(NamedTuple):
    """What the other dists of a shared target root claim.

    The rules are in [VALIDATION.md](VALIDATION.md#index-selection).

    Attributes:
        prefix: The synced dist's own index tree, `dists/<dist>/`, or empty for a flat layout.
        claimed: Paths another dist holds: its `dists/<dist>/` tree and the payloads its indices
            list.
        complete: Whether every other dist loaded, so `claimed` holds all of its payloads.
        recorded: Payload path -> the digests every index of every other dist records for it.
    """

    prefix: str
    claimed: frozenset[str]
    complete: bool
    recorded: Mapping[str, tuple[dict[str, str], ...]] = {}

    def deletable(self, path: str) -> bool:
        """Report whether deleting `path` from the root leaves every other dist's claims intact.

        Args:
            path: Storage path in the target root.

        Returns:
            False for a path another dist claims, and for any path outside the own index tree
            while another dist's payloads are unknown.
        """
        if path in self.claimed:
            return False
        return self.complete or path.startswith(self.prefix)

    def corrupt_deletable(self, path: str, digests: dict[str, str]) -> bool:
        """Report whether a payload whose bytes fail the own indices may be deleted.

        Another dist's index may record exactly these bytes, so they are kept for it; one that
        records other bytes loses nothing, since the bytes are wrong for it too. A record with
        no SHA digest cannot tell, so it keeps them
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            path: Storage path of the payload in the target root.
            digests: The digests of the bytes the root holds at `path`.

        Returns:
            False while another dist's payloads are unknown, when a record of `path` carries no
            SHA digest, and when one records `digests`.
        """
        if not self.complete:
            return False
        records = self.recorded.get(path, ())
        # `compare_digests` matches only on a SHA digest, so a record without one never
        # matches even itself
        if any(not compare_digests(record, record) for record in records):
            return False
        return not any(compare_digests(record, digests) for record in records)


class ConsistencyLists(NamedTuple):
    """What `Repo.consistency_lists` found in the repository's own storage listing.

    Attributes:
        missing: Payloads and parsed component indices the listing lacks, sorted.
        orphans: Listed paths that no index and no other dist claims, sorted.
        shared: What the other dists sharing the root claim, from the same listing.
    """

    missing: list[str]
    orphans: list[str]
    shared: SharedRoot


# a flat layout has no `dists/` tree, so no other dist shares its root
_FLAT_ROOT = SharedRoot("", frozenset(), complete=True)


def _has_dists_tree(params: RepoParams) -> bool:
    """Report whether the repository's indices sit under a `dists/<dist>/` tree.

    Args:
        params: The repository's parameters.

    Returns:
        False for a flat layout, and for one with no dist to name the tree.
    """
    return not params.is_flat and bool(params.indices_prefix)


def _unlisted_shared_root(params: RepoParams) -> SharedRoot:
    """The claims of a root whose storage cannot list.

    Args:
        params: The repository's parameters.

    Returns:
        No claims; for a componented layout incomplete, so nothing outside the own index tree
        is deletable.
    """
    if not _has_dists_tree(params):
        return _FLAT_ROOT
    return SharedRoot(f"{params.indices_prefix}/", frozenset(), complete=False)


# what a neighbour dist's load may raise; every one leaves that dist's payloads unknown, so
# `Repo.update` and its Raises are the list to keep this in step with
_NOT_LOADED = (
    RepoReadException,
    ReleaseReadException,
    GPGVerifyException,
    ComponentException,
    PackagesException,
    PackageException,
    ChecksumException,
    StorageException,
)


# a binary or source index key a root Release lists: `<comp>/binary-<arch>/Packages[.<ext>]` or
# `<comp>/source/Sources[.<ext>]`
_LISTED_INDEX = re.compile(
    r"(?P<comp>[^/]+)/(?:binary-(?P<arch>[^/]+)/Packages|source/Sources)(?:\.[^/]+)?"
)


def _claims_of(storage: Storage, dist: str) -> dict[str, list[dict[str, str]]]:
    """Load another dist of the root and return the payloads its indices list.

    Exactly the indices its root Release lists are loaded, and each must load: one skipped
    would leave its payloads unclaimed, so they would read as orphans
    -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        storage: The target storage.
        dist: The other dist.

    Raises:
        RepoReadException: A listed component is not a safe name.
        ReleaseReadException: The dist has no root Release, or it does not parse.
        ComponentException: A listed index is missing or does not load.

    Returns:
        Each payload path the dist's binary and source indices list -> the digests each index
        records, so a path two indices record differently keeps both.
    """
    release = RootRelease(storage, RepoParams(dist=dist, verify_gpg=False, verify_digests=False))
    release.load()
    slots: dict[str, set[str]] = {}
    for key in release.digests:
        if listed := _LISTED_INDEX.fullmatch(key):
            slots.setdefault(listed["comp"], set()).add(listed["arch"] or SOURCE_ARCH)

    claims: dict[str, list[dict[str, str]]] = {}
    for comp, archs in sorted(slots.items()):
        if not is_safe_name(comp):
            raise RepoReadException(f"dist {dist} lists an unsafe component {comp!r}")
        params = RepoParams(
            dist=dist,
            components=[comp],
            archs=sorted(archs),
            verify_gpg=False,
            verify_digests=False,
        )
        repo = Repo(storage, params)
        repo.update(strict_arch=True)
        # every slot, not `get_packages`, which yields a path shared by two arch indices once
        for component in repo.components.values():
            for packages in component.packages.values():
                for package in packages.packages.values():
                    claims.setdefault(str(package), []).append(package.get_digests())
    return claims


def shared_root_claims(
    storage: Storage, params: RepoParams, existing: set[str], *, with_digests: bool = False
) -> SharedRoot:
    """Collect what the other dists of a componented target root claim.

    A Debian root holds many dists under `dists/<dist>/` sharing one `pool/`. Each other dist
    is loaded unverified, since its claims only ever keep a path, never copy one
    -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        storage: The target storage.
        params: The synced dist's parameters.
        existing: Paths the target listing returned.
        with_digests: Also keep the digests each index of each other dist records, which only
            `SharedRoot.corrupt_deletable` reads. Defaults to False.

    Returns:
        The claims; none for a flat layout, which has no `dists/` tree.
    """
    if not _has_dists_tree(params):
        return _FLAT_ROOT

    prefix = f"{params.indices_prefix}/"
    foreign = {
        path for path in existing if path.startswith("dists/") and not path.startswith(prefix)
    }
    dists = sorted({path.split("/")[1] for path in foreign if path.count("/") >= 2})
    claimed = set(foreign)
    recorded: dict[str, list[dict[str, str]]] = {}
    complete = True

    for dist in dists:
        if not is_safe_name(dist):
            logger.warning("Shared root dist %r is not a safe name; its payloads are kept.", dist)
            complete = False
            continue
        try:
            claims = _claims_of(storage, dist)
        except _NOT_LOADED as exc:
            logger.warning(
                "Dist %s shares the target root but did not load (%s); "
                "nothing outside %s is deleted.",
                dist,
                exc,
                prefix,
            )
            complete = False
            continue
        claimed.update(claims)
        if with_digests:
            for path, records in claims.items():
                recorded.setdefault(path, []).extend(records)

    if dists:
        logger.info("Shared root: keeping what dists %s claim", ", ".join(dists))
    return SharedRoot(
        prefix,
        frozenset(claimed),
        complete,
        {path: tuple(records) for path, records in recorded.items()},
    )
