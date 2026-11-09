"""What the other dists of a shared target root claim, and the target's reconciliation lists.

A componented root holds many dists under `dists/<dist>/` sharing one `pool/`; what each other
dist's indices list keeps a path from deletion or replacement
-> [VALIDATION.md](VALIDATION.md#index-selection).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, NamedTuple

from txstore import StorageException, compare_digests

from debkit.repo import INDEX_TAIL, Repo, RepoReadException
from debkit.repo.component import ComponentException, IndexLoad
from debkit.repo.gnupg import GPGVerifyException
from debkit.repo.hashes import ChecksumException, has_sha
from debkit.repo.package import PackageException
from debkit.repo.packages import PackagesException
from debkit.repo.params import SOURCE_ARCH, RepoParams, is_safe_name
from debkit.repo.release import Release, ReleaseReadException, RootRelease

if TYPE_CHECKING:
    from txstore import Storage

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class SharedRoot(NamedTuple):
    """What the other dists of a shared target root claim.

    The rules are in [VALIDATION.md](VALIDATION.md#index-selection).

    Attributes:
        prefix: The synced dist's own index tree, `dists/<dist>/`, or empty for a flat layout.
        claimed: Paths another dist holds: its `dists/<dist>/` tree and the payloads its indices
            list.
        complete: Whether every other dist loaded, so `claimed` holds all of its payloads.
        recorded: Payload path -> the digests every index of every other dist records for it;
            None only when the caller did not ask for them.
    """

    prefix: str
    claimed: frozenset[str]
    complete: bool
    recorded: Mapping[str, tuple[dict[str, str], ...]] | None = None

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
            SHA digest, and when one records `digests`; without collected digests, what
            `deletable` says, which never deletes a claimed path.
        """
        if self.recorded is None:
            return self.deletable(path)
        if not self.complete:
            return False
        records = self.recorded.get(path, ())
        if not all(has_sha(record) for record in records):
            return False
        return not any(compare_digests(record, digests) for record in records)

    def replaceable(self, path: str, digests: dict[str, str]) -> bool:
        """Report whether a held payload may be overwritten with the bytes the own source records.

        Another dist's index pinning other bytes at the same key would serve its clients a hash
        mismatch once they change; a record with no SHA digest cannot tell, so it refuses too
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            path: Storage path of the payload in the target root.
            digests: The digests the own source records for `path`.

        Returns:
            True when no other dist records `path`, or every record of it carries a SHA digest
            matching `digests`; False while another dist's payloads are unknown.
        """
        if not self.complete:
            return False
        if self.recorded is None:
            return path not in self.claimed
        records = self.recorded.get(path, ())
        return all(has_sha(record) and compare_digests(record, digests) for record in records)


class ConsistencyLists(NamedTuple):
    """What `RepoRW.consistency_lists` found in the repository's own storage listing.

    Attributes:
        missing: Payloads and parsed component indices the listing lacks, sorted.
        orphans: Listed paths that no index and no other dist claims, sorted.
        shared: What the other dists sharing the root claim, from the same listing.
    """

    missing: list[str]
    orphans: list[str]
    shared: SharedRoot


# a flat layout has no `dists/` tree, so no other dist shares its root
_FLAT_ROOT = SharedRoot("", frozenset(), complete=True, recorded={})


def _has_dists_tree(params: RepoParams) -> bool:
    """Report whether the repository's indices sit under a `dists/<dist>/` tree.

    Args:
        params: The repository's parameters.

    Returns:
        False for a flat layout, and for one with no dist to name the tree.
    """
    return not params.is_flat and bool(params.indices_prefix)


def unlisted_shared_root(params: RepoParams) -> SharedRoot:
    """The claims of a root whose storage cannot list.

    Args:
        params: The repository's parameters.

    Returns:
        No claims; for a componented layout incomplete, so nothing outside the own index tree
        is deletable.
    """
    if not _has_dists_tree(params):
        return _FLAT_ROOT
    return SharedRoot(f"{params.indices_prefix}/", frozenset(), complete=False, recorded={})


# what a neighbour dist's load may raise: `RootRelease.load`, the unsafe-component refusal in
# `_claims_of`, and `Repo.load` and its Raises; every one leaves that dist's payloads unknown
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


# a listed index key whose Release names no components: the component is everything before
# the index tail, so `updates/main/binary-amd64/Packages` reads as component `updates/main`
_LISTED_INDEX = re.compile(rf"(?P<comp>.+)/(?:{INDEX_TAIL.pattern})")


def _listed_slots(release: Release) -> dict[str, set[str]]:
    """Map each component of a root Release to the archs whose indices it lists.

    A component may hold `/`, so a key is matched below a component the `Components` field
    declares, the longest first; a key no declared component explains still claims, with the
    key's head as its component, since a missed index would leave its payloads deletable
    -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        release: A loaded root Release.

    Returns:
        Component -> its binary archs, plus `SOURCE_ARCH` for a listed `Sources`.
    """
    declared = sorted(release.fields.get("Components", "").split(), key=len, reverse=True)
    slots: dict[str, set[str]] = {}
    for key in release.digests:
        for comp in declared:
            if key.startswith(f"{comp}/") and (tail := INDEX_TAIL.fullmatch(key[len(comp) + 1 :])):
                slots.setdefault(comp, set()).add(tail["arch"] or SOURCE_ARCH)
                break
        else:
            if listed := _LISTED_INDEX.fullmatch(key):
                slots.setdefault(listed["comp"], set()).add(listed["arch"] or SOURCE_ARCH)
    return slots


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

    claims: dict[str, list[dict[str, str]]] = {}
    for comp, archs in sorted(_listed_slots(release).items()):
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
        repo.load(IndexLoad.STRICT)
        # every slot, not `get_packages`, which yields a path shared by two arch indices once
        for component in repo.components.values():
            for packages in component.packages.values():
                for package in packages.packages.values():
                    claims.setdefault(str(package), []).append(package.get_digests())
    return claims


def shared_root_claims(
    storage: Storage,
    params: RepoParams,
    existing: set[str] | None = None,
    *,
    with_digests: bool = False,
) -> SharedRoot:
    """Collect what the other dists of a componented target root claim.

    A Debian root holds many dists under `dists/<dist>/` sharing one `pool/`. Each other dist
    is loaded unverified, since its claims only ever keep a path, never copy one
    -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        storage: The target storage.
        params: The synced dist's parameters.
        existing: Paths the target listing returned; None lists the storage, so a caller
            that already listed it judges claims against the same listing. A flat layout is
            never listed.
        with_digests: Also keep the digests each index of each other dist records, which
            `SharedRoot.corrupt_deletable` and `SharedRoot.replaceable` read. Defaults to
            False: a caller that only deletes keeps them out of memory.

    Returns:
        The claims; none for a flat layout, which has no `dists/` tree, and unknown when the
        storage cannot list.
    """
    if not _has_dists_tree(params):
        return _FLAT_ROOT
    if existing is None:
        try:
            existing = set(storage.list())
        except NotImplementedError:
            return unlisted_shared_root(params)

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
                "nothing outside %s is deleted or overwritten.",
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
        {path: tuple(records) for path, records in recorded.items()} if with_digests else None,
    )


def missing_and_unclaimed(
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
