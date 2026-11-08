"""Mirror a Debian repository: the `RepoSync` operation.

`RepoSync` plans the copy and delete lists, transfers them, and checks a mirror;
which Release-listed indices it carries is decided in `index_selection`.
"""

import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from txstore import (
    StorageException,
    StorageNotFoundException,
    StorageNull,
    compare_digests,
    is_unsafe_path,
    normalize_digests,
)

from debkit.repo import Repo
from debkit.repo.hashes import ChecksumException
from debkit.repo.index_selection import select_index_entry

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package
    from debkit.repo.params import RepoParams
    from debkit.repo.release import IndexEntry
    from debkit.repo.repo_diff import RepoDiff
    from debkit.repo.repo_rw import RepoRW


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

TRANSFER_MAX_ERR_RETRIES = 3


@dataclass(frozen=True)
class IndexTarget:
    """One index in the mirror set -> [VALIDATION.md](VALIDATION.md#index-selection).

    `digests` are what the source root Release records for the path, or `None` for
    the root Release files, which are not listed in their own digest block.
    `known_published` says the Release load or a parse already fetched the object.
    """

    digests: dict[str, str] | None
    known_published: bool


class RepoSyncException(Exception):
    """Exception raised for errors in sync to the repository."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Repo sync: {message}")


class RepoSync(Repo):
    """Class that extends Repo with sync functionality."""

    def __init__(
        self, storage: "Storage", params: "RepoParams", gpg: "GnuPG | None" = None
    ) -> None:
        """Initialize RepoSync.

        Args:
            storage (Storage): storage
            params (RepoParams): repository parameters
            gpg (Optional[GnuPG], optional): GnuPG object. Defaults to None.
        """
        super().__init__(storage, params, gpg=gpg)
        # payloads reported as Release-owned. `build_sync_lists` and `check` each build
        # the package set, so the report is per run, not per pass
        self._owned_payloads_warned: set[str] = set()

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
            Dict[str, IndexTarget]: Index path → the digests recorded for it and
                whether the source is already known to publish it.
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

    def _payload_files(self) -> set[str]:
        """Collect the parsed payload paths, dropping any the Release owns.

        Returns:
            Set[str]: Paths of every parsed package the Release does not own.
        """
        return self._drop_release_owned(str(pkg) for pkg in self.get_packages())

    def _drop_release_owned(self, paths: Iterable[str]) -> set[str]:
        """Drop the stanza paths a Release digest block lists.

        A stanza `Filename` a Release digest block also lists is an index, not a
        payload: the index set alone decides whether it is copied or purged, so index
        selection and the payload copy and removal cannot disagree
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            paths (Iterable[str]): Stanza paths, as `str(Package)` gives them.

        Returns:
            Set[str]: The paths no Release digest block lists.
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

    def build_sync_lists(
        self, target_repo: "RepoRW", indices_only: bool = False
    ) -> tuple[list[str], list[str], set[str]]:
        """Build filelists for sync.

        Process some optimizations for reduce copy and remove lists.

        Args:
            target_repo (Repo): The target repo to sync with.
            indices_only (bool): Force the index pass and plan no payload copy or
                deletion. Defaults to False.

        Returns:
            tuple: The paths to copy, the paths to delete, and the copied index paths
                no load or parse proved the source publishes, which `sync` skips when
                the source lacks them.
        """
        try:
            existing_files = set(target_repo.storage.list())
            can_list = True
        except NotImplementedError:
            existing_files, can_list = set(), False

        indices = self._get_indices()

        copy_list: list[str] = []
        delete_list: list[str] = []
        forced = True
        if not indices_only:
            diff = self.diff(target_repo)
            # an index-only source change (i18n, dep11) leaves the package diff empty
            forced = self.release.hash != target_repo.release.hash or not diff.is_empty()
            copy_list, delete_list = self._plan_payloads(
                diff, indices, target_repo, existing_files, can_list=can_list
            )

        logger.info("     indices: %d objects", len(indices))
        logger.info(" target repo: %d objects already existing", len(existing_files))

        index_copies = self._indices_to_copy(
            indices, target_repo, existing_files, forced=forced, can_list=can_list
        )
        unproven = {
            file
            for file in index_copies
            if not indices[file].known_published and file not in existing_files
        }

        return copy_list + index_copies, delete_list, unproven

    def _plan_payloads(
        self,
        diff: "RepoDiff",
        indices: Collection[str],
        target_repo: "RepoRW",
        existing_files: set[str],
        *,
        can_list: bool,
    ) -> tuple[list[str], list[str]]:
        """Plan the payload copies and deletions a package diff and the target listing call for.

        Args:
            diff (RepoDiff): This repo against the target.
            indices (Collection[str]): Every index path the index set holds.
            target_repo (RepoRW): The target repo to sync with.
            existing_files (Set[str]): Paths the target listing returned.
            can_list (bool): Whether `existing_files` is a real listing of the target.

        Returns:
            Tuple[List[str], List[str]]: Payloads to copy, repairs included, and
                payloads to delete, orphans included; both sorted.
        """
        all_pkg_files = self._payload_files()
        copy_pkg_files = diff.get_filenames_to_copy() & all_pkg_files
        # a vanished stanza naming an index must not delete what the index set keeps
        remove_pkg_files = self._drop_release_owned(diff.get_filenames_to_remove())

        # missing packages a previous failed sync left, and orphans; an unlisted target
        # proves neither
        repair_files: set[str] = set()
        purge_files: set[str] = set()
        if can_list:
            repair_files, purge_files = self._repair_and_purge(
                all_pkg_files, indices, existing_files, planned=copy_pkg_files | remove_pkg_files
            )

        copy_pkg_files = self._reduce_copies(
            diff.get_pkgs_to_copy(), copy_pkg_files, target_repo, existing_files
        )
        # files a previous failed sync never got to delete
        remove_pkg_files &= existing_files

        logger.info(" source repo: %d packages (total)", len(all_pkg_files))
        logger.info("     to copy: %d packages", len(copy_pkg_files))
        logger.info("   to remove: %d packages", len(remove_pkg_files))
        logger.info("   to repair: %d objects (corrupted or missing)", len(repair_files))
        logger.info("    to purge: %d objects (orphans)", len(purge_files))

        return sorted(copy_pkg_files | repair_files), sorted(remove_pkg_files | purge_files)

    def _reduce_copies(
        self,
        packages: list["Package"],
        copy_files: set[str],
        target_repo: "RepoRW",
        existing_files: set[str],
    ) -> set[str]:
        """Drop the payloads the target already holds unchanged, as a terminated sync leaves them.

        Args:
            packages (List[Package]): Packages the diff copies, one per index naming them.
            copy_files (Set[str]): Payload paths to copy.
            target_repo (RepoRW): The target repo to sync with.
            existing_files (Set[str]): Paths the target listing returned.

        Returns:
            Set[str]: `copy_files` without the payloads the target already holds.
        """
        ret = set(copy_files)
        seen: dict[str, dict[str, str]] = {}
        for package in packages:
            pkg_name = str(package)
            if pkg_name in seen:
                if package.get_digests() != seen[pkg_name]:
                    logger.warning(
                        "%s has inconsistent digests across indices in source repo", package
                    )
                continue
            seen[pkg_name] = package.get_digests()
            if pkg_name not in existing_files:
                continue
            if not self.params.verify_digests or self._digest_unchanged(
                pkg_name, package.get_digests(), target_repo
            ):
                ret.discard(pkg_name)
            else:
                logger.warning(
                    "%s have not valid checksums in target repo. It will be copied again.",
                    package,
                )
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
            payloads (Set[str]): Every payload path the package set holds.
            indices (Collection[str]): Every index path the index set holds.
            existing_files (Set[str]): Paths the target listing returned.
            planned (Optional[Set[str]]): Paths a package diff already copies or
                removes, left out of both results.

        Returns:
            Tuple[Set[str], Set[str]]: The payloads the listing lacks, and the listed
                paths neither set holds.
        """
        planned = planned or set()
        repair = payloads - planned - existing_files
        purge = {file for file in existing_files - payloads - planned if file not in indices}
        return repair, purge

    def consistency_lists(self) -> tuple[list[str], list[str]]:
        """List what this repository's own storage lacks and what it holds unclaimed.

        Missing are the payloads and the parsed component indices the listing lacks:
        the root Release files were just read and nothing is probed, so a listing that
        disagrees with a read cannot make them missing. Orphans are listed paths that
        neither the package set nor the index set holds
        -> [CONFIG.md](CONFIG.md#check).

        Returns:
            Tuple[List[str], List[str]]: Missing and orphan paths, sorted; both empty
                when the storage cannot list.
        """
        try:
            existing_files = set(self.storage.list())
        except NotImplementedError:
            return [], []

        indices = self._get_indices()
        missing, orphans = self._repair_and_purge(self._payload_files(), indices, existing_files)
        parsed = {
            path for component in self.components.values() for path in component.get_indices_paths()
        }
        root_files = set(self.release.get_indices_paths())
        missing |= (parsed & indices.keys()) - root_files - existing_files
        return sorted(missing), sorted(orphans)

    def _indices_to_copy(
        self,
        indices: dict[str, IndexTarget],
        target_repo: "RepoRW",
        existing_files: set[str],
        *,
        forced: bool,
        can_list: bool,
    ) -> list[str]:
        """Decide which indices this sync copies -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            indices (Dict[str, IndexTarget]): The sync index set, from `_get_indices()`.
            target_repo (RepoRW): The target repo to sync with.
            existing_files (Set[str]): Paths the target listing returned.
            forced (bool): Whether a package or Release change already forces the index pass.
            can_list (bool): Whether `existing_files` is a real listing of the target.

        Returns:
            List[str]: Index paths to copy, empty when nothing forces the pass.
        """
        # an absent entry no load or parse proved is expected absent, so it never forces
        if can_list and not forced:
            forced = any(
                target.known_published and file not in existing_files
                for file, target in indices.items()
            )

        if not forced:
            return []
        return self._reduce_indices(indices, target_repo, existing_files)

    def _digest_unchanged(
        self,
        file: str,
        digests: dict[str, str],
        target_repo: "RepoRW",
    ) -> bool:
        """Report whether the target already holds `file` with the expected digests.

        The digest-skip decision shared by packages and indices; normalization and
        comparison stay in `hexdigest.py` -> [CRYPTO.md](CRYPTO.md#implementation-rules).
        The caller has already established that `file` exists in the target and that
        digest verification is on.

        Args:
            file (str): object path, identical in both repositories.
            digests (Dict[str, str]): digests the source records for the object.
            target_repo (RepoRW): The target repo to sync with.

        Returns:
            bool: True when the target copy matches and need not be transferred.
        """
        return compare_digests(digests, normalize_digests(target_repo.storage.get_metadata(file)))

    def _index_path_is_safe(self, entry: "IndexEntry") -> bool:
        """Report whether a Release-listed index path stays inside the repository.

        The digest block is remote metadata, so a hostile or malformed Release can
        name a key outside the storage root. A rejected entry is dropped from the
        index set rather than failing the transfer, so one bad record cannot stop a
        mirror -> [VALIDATION.md](VALIDATION.md#filename-safety).

        Args:
            entry (IndexEntry): candidate index from the root Release digest block.

        Returns:
            bool: True when the path is safe to mirror.
        """
        if is_unsafe_path(entry.key) or is_unsafe_path(entry.path):
            logger.warning("%s listed in Release is an unsafe index path. Skipped.", entry.key)
            return False
        return True

    def _target_release_agrees(
        self,
        file: str,
        digests: dict[str, str],
        target_repo: "RepoRW",
    ) -> bool:
        """Report whether both root Releases record the same digests for an index.

        The target `Release` is already loaded and its hash compared in
        `build_sync_lists`, so its digest block costs nothing, while
        `Storage.get_metadata` is a round trip per key and a full Debian Release
        lists hundreds of indices. Which entries agreement may settle is decided by
        `_reduce_indices`.

        Args:
            file (str): object path, identical in both repositories.
            digests (Dict[str, str]): digests the source Release records for the index.
            target_repo (RepoRW): The target repo to sync with.

        Returns:
            bool: True when the target Release records matching digests for the key.
        """
        target_digests = target_repo.release.digests.get(target_repo.params.release_key(file))
        if not target_digests:
            return False
        return compare_digests(digests, normalize_digests(target_digests))

    def _reduce_indices(
        self,
        indices: dict[str, IndexTarget],
        target_repo: "RepoRW",
        existing_files: set[str],
    ) -> list[str]:
        """Drop indices the target already holds unchanged.

        Agreeing root Release digest blocks settle a held entry first (free), then its
        stored metadata (one round trip); nothing settles an absent entry, and the
        source is never probed -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            indices (Dict[str, IndexTarget]): Index path → expected digests and
                whether the source is known to publish it.
            target_repo (RepoRW): The target repo to sync with.
            existing_files (Set[str]): Objects already present in the target.

        Returns:
            List[str]: Index paths still to copy.
        """
        target_loaded = bool(target_repo.release.hash)
        ret: list[str] = []
        for file, target in indices.items():
            held = file in existing_files

            if held and target.digests and self.params.verify_digests and target_loaded:
                if self._target_release_agrees(file, target.digests, target_repo):
                    continue
                if self._digest_unchanged(file, target.digests, target_repo):
                    continue
                logger.debug(
                    "%s has different checksums in target repo. It will be copied again.", file
                )

            ret.append(file)

        logger.info("     indices: %d objects to copy", len(ret))
        return ret

    def _transfer_file(
        self,
        file: str,
        target_repo: "Repo",
        progress: tuple[int, int] | None = None,
        *,
        missing_ok: bool = False,
    ) -> bool:
        """Transfer a file from this repo to another repo.

        Save digests of the file in metadata.

        Args:
            file (str): The file to transfer.
            target_repo (Repo): The target repo to transfer to.
            progress (Optional[Tuple[int, int]], optional): Progress of transfer - current of
                total objects.
                Defaults to None.
            missing_ok (bool): Skip the file when the source lacks it, as for an index
                nothing proved published -> [VALIDATION.md](VALIDATION.md#index-selection).

        Raises:
            ChecksumException: If the checksum verification fails.

        Returns:
            bool: True when the file needs no retry: it was transferred, or `missing_ok`
                is set and the source lacks it. False otherwise.
        """
        if progress is None:
            logger.info("Copy: %s", file)
        else:
            logger.info("Copy (%d/%d): %s", progress[0], progress[1], file)

        try:
            digests = self.storage.transfer(
                file,
                target_repo.storage,
                path_dst=file,
            )
        except StorageNotFoundException as exc:
            if missing_ok:
                logger.debug("%s listed in Release but missing in source. Skipped.", file)
                return True
            logger.error("Error transfer file %s: %s", file, exc)
            return False
        except StorageException as exc:
            logger.error("Error transfer file %s: %s", file, exc)
            return False

        if self.params.verify_digests:
            if not self.verify_checksum(file, digests):
                target_repo.storage.delete(file)
                raise ChecksumException(f"Content verify mismatch in {file}")

        target_repo.storage.add_metadata(file, digests)

        return True

    def _delete_file(self, file: str, target_repo: "Repo") -> None:
        """Delete a file from this repo.

        Args:
            file (str): The file to delete.
            target_repo (Repo): The target repo to delete from.
        """
        logger.info("Delete: %s", file)
        target_repo.storage.delete(file)

    def _verify_file(
        self, file: str, progress: tuple[int, int] | None = None, fix: bool | None = False
    ) -> bool:
        """Transfer a file and verify checksums.

        Args:
            file (str): The file to transfer.
            progress (Optional[Tuple[int, int]], optional): Progress of transfer - current of
                total objects.
                Defaults to None.
            fix (Optional[bool], optional): Fix inconsistencies if True. Defaults to False.

        Returns:
            bool: True if the file is valid, False otherwise.
        """
        dev_null = StorageNull()

        if progress is None:
            logger.info("Verify: %s", file)
        else:
            logger.info("Verify (%d/%d): %s", progress[0], progress[1], file)

        digests = self.storage.transfer(file, dev_null)

        if self.params.verify_digests:
            if not self.verify_checksum(file, digests):
                return False

        if fix:
            self.storage.add_metadata(file, digests)

        return True

    def sync(
        self,
        target_repo: "Repo",
        copy_list: list[str],
        delete_list: list[str],
        *,
        unproven: Collection[str] = frozenset(),
        depth: int = 0,
    ) -> None:
        """Sync this repo with another repo.

        Either every object transfers or the sync raises: a target that holds a new
        root `Release` describing objects it never received would advertise content
        it cannot serve. The caller wraps the run in a transaction, so raising leaves
        the target at its previous consistent state
        -> [STORAGE.md](STORAGE.md#transactions). An `unproven` path the source lacks
        is the one exception: the source serves it no more than the target will
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            target_repo (Repo): The target repo to sync with.
            copy_list (List[str]): The list of files to copy.
            delete_list (List[str]): The list of files to delete.
            unproven (Collection[str]): Paths in `copy_list` skipped when the source
                lacks them, from `build_sync_lists`.
            depth (int): The depth of the sync. Defaults to 0.

        Raises:
            RepoSyncException: If any object still fails after the retries.
            ChecksumException: If a transferred object does not match its expected
                digests; it is never retried → [CRYPTO.md](CRYPTO.md#verification-failures).
        """
        # break recursion if max retries reached
        if depth >= TRANSFER_MAX_ERR_RETRIES:
            logger.debug("failed packages: %s", copy_list)
            raise RepoSyncException("Max retries reached.")

        failed_list: list[str] = []
        total: int = len(copy_list)
        cnt: int = 0
        for name in copy_list:
            cnt += 1
            if not self._transfer_file(
                name, target_repo, progress=(cnt, total), missing_ok=name in unproven
            ):
                failed_list.append(name)

        # retry whatever failed; a transient error should not fail the whole run
        if len(failed_list) > 0:
            return self.sync(
                target_repo, failed_list, delete_list, unproven=unproven, depth=depth + 1
            )

        # delete objects
        for name in delete_list:
            self._delete_file(name, target_repo)

    def check(
        self, missing_list: list[str], orphan_list: list[str], fix: bool = False
    ) -> tuple[int, int]:
        """Check repository for consistency.

        Args:
            missing_list (List[str]): List of missing packages.
            orphan_list (List[str]): List of orphan files to delete.
            fix (bool): Delete inconsistencies if True

        Returns:
            Tuple[int, int]: A tuple containing the number of errors and the number of errors fixed.
        """
        all_pkg_files = sorted(self._payload_files())

        total: int = len(all_pkg_files)
        cnt: int = 0
        ret_err_total: int = 0
        ret_err_fixed_total: int = 0

        for file in all_pkg_files:
            cnt += 1

            if file in missing_list:
                logger.warning("Skip (%d/%d): %s (missing)", cnt, total, file)
                continue

            try:
                if not self._verify_file(file, progress=(cnt, total), fix=fix):
                    logger.warning("Checksum verify failed: %s", file)
                    ret_err_total += 1
                    if fix:
                        self._delete_file(file, self)
                        ret_err_fixed_total += 1
            except StorageException as exc:
                logger.error("Error checking file: %s: %s", file, exc)
                ret_err_total += 1

        if fix:
            for file in orphan_list:
                try:
                    self._delete_file(file, self)
                    ret_err_fixed_total += 1
                except StorageException as exc:
                    logger.error("Error deleting file: %s: %s", file, exc)

        return ret_err_total, ret_err_fixed_total
