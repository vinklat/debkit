"""Mirror a Debian repository: the `RepoSync` operation.

`RepoSync` plans the copy and delete lists and transfers them; the index set it mirrors is
`Repo._get_indices`, whose rules are in `index_selection`.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING, NamedTuple

from txstore import (
    StorageException,
    StorageNotFoundException,
)

from debkit.repo import IndexTarget, Repo, SharedRoot, shared_root_claims
from debkit.repo.hashes import ChecksumException

if TYPE_CHECKING:
    from debkit.repo.package import Package
    from debkit.repo.repo_diff import RepoDiff
    from debkit.repo.repo_rw import RepoRW


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

TRANSFER_MAX_ERR_RETRIES = 3


class SyncPlan(NamedTuple):
    """One pass of a sync, as `RepoSync.build_sync_lists` plans it.

    Attributes:
        copy: Paths to copy from the source, payloads before indices.
        delete: Paths to delete from the target.
        unproven: Paths in `copy` no load or parse proved the source publishes, which `sync` skips
            when the source lacks them.
    """

    copy: list[str]
    delete: list[str]
    unproven: set[str]

    def is_empty(self) -> bool:
        """Report whether the pass has nothing to copy or delete.

        Returns:
            True when both `copy` and `delete` are empty.
        """
        return not self.copy and not self.delete


class RepoSyncException(Exception):
    """Exception raised for errors in sync to the repository."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Repo sync: {message}")


def _refuse_shared_overwrites(shared: SharedRoot, replaced: dict[str, dict[str, str]]) -> None:
    """Refuse a plan that overwrites a payload another dist of the shared root pins.

    Skipping the copy instead would leave the own Release naming bytes the target does not
    hold -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        shared: The claims of the other dists, with the digests their indices record.
        replaced: Each held payload the plan overwrites -> the digests the source records.

    Raises:
        RepoSyncException: If another dist records other bytes for a payload the plan
            overwrites, records it without a SHA digest, or did not load.
    """
    refused = sorted(
        path for path, digests in replaced.items() if not shared.replaceable(path, digests)
    )
    if refused:
        raise RepoSyncException(
            f"{len(refused)} held payload(s) would be overwritten with bytes another dist of "
            f"the shared root does not record, or whose dists did not load: {', '.join(refused)}"
        )


class RepoSync(Repo):
    """Class that extends Repo with sync functionality."""

    def build_sync_lists(self, target_repo: RepoRW, indices_only: bool = False) -> SyncPlan:
        """Build filelists for sync.

        Process some optimizations for reduce copy and remove lists.

        Args:
            target_repo: The target repo to sync with.
            indices_only: Force the index pass and plan no payload copy or deletion. Defaults to
                False.

        Returns:
            The paths to copy, the paths to delete, and the copied index paths no load or parse
            proved the source publishes.
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
            diff = self.diff(target_repo.snapshot())
            # an index-only source change (i18n, dep11) leaves the package diff empty, and a
            # target index skipped as unreadable leaves the Release hashes equal
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

        return SyncPlan(copy_list + index_copies, delete_list, unproven)

    def _plan_payloads(
        self,
        diff: RepoDiff,
        indices: Collection[str],
        target_repo: RepoRW,
        existing_files: set[str],
        *,
        can_list: bool,
    ) -> tuple[list[str], list[str]]:
        """Plan the payload copies and deletions a package diff and the target listing call for.

        Args:
            diff: This repo against the target.
            indices: Every index path the index set holds.
            target_repo: The target repo to sync with.
            existing_files: Paths the target listing returned.
            can_list: Whether `existing_files` is a real listing of the target.

        Returns:
            Payloads to copy, repairs included, and payloads to delete, orphans included; both
            sorted.
        """
        all_pkg_files = self._payload_files()
        copy_pkg_files = diff.get_filenames_to_copy() & all_pkg_files
        # a vanished stanza naming an index must not delete what the index set keeps, and a
        # payload dropped from one arch index stays while another source index lists it
        # -> [VALIDATION.md](VALIDATION.md#index-selection)
        remove_pkg_files = self._drop_release_owned(diff.get_filenames_to_remove()) - all_pkg_files

        # missing packages a previous failed sync left, and orphans; an unlisted target
        # proves neither
        repair_files: set[str] = set()
        purge_files: set[str] = set()
        if can_list:
            repair_files, purge_files = self._missing_and_unclaimed(
                all_pkg_files, indices, existing_files, planned=copy_pkg_files | remove_pkg_files
            )
        copy_pkg_files, replaced = self._reduce_copies(
            diff.get_pkgs_to_copy(), copy_pkg_files, target_repo, existing_files
        )
        # another dist sharing the root keeps its tree and the payloads it lists, and the bytes
        # its indices pin; reading those claims loads every other dist, so only a pass that
        # deletes or overwrites a held payload reads them
        if can_list and (purge_files or remove_pkg_files or replaced):
            shared = shared_root_claims(
                target_repo.storage,
                target_repo.params,
                existing_files,
                with_digests=bool(replaced),
            )
            purge_files = {file for file in purge_files if shared.deletable(file)}
            remove_pkg_files = {file for file in remove_pkg_files if shared.deletable(file)}
            _refuse_shared_overwrites(shared, replaced)
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
        packages: list[Package],
        copy_files: set[str],
        target_repo: RepoRW,
        existing_files: set[str],
    ) -> tuple[set[str], dict[str, dict[str, str]]]:
        """Drop the payloads the target already holds unchanged, as a terminated sync leaves them.

        Args:
            packages: Packages the diff copies, one per index naming them.
            copy_files: Payload paths to copy.
            target_repo: The target repo to sync with.
            existing_files: Paths the target listing returned.

        Returns:
            `copy_files` without the payloads the target already holds, and each held payload
            the copy overwrites -> the digests the source records for it.
        """
        ret = set(copy_files)
        replaced: dict[str, dict[str, str]] = {}
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
            if not self.params.verify_digests or target_repo.holds(pkg_name, package.get_digests()):
                ret.discard(pkg_name)
            else:
                if pkg_name in ret:
                    replaced[pkg_name] = package.get_digests()
                logger.warning(
                    "%s have not valid checksums in target repo. It will be copied again.",
                    package,
                )
        return ret, replaced

    def _indices_to_copy(
        self,
        indices: dict[str, IndexTarget],
        target_repo: RepoRW,
        existing_files: set[str],
        *,
        forced: bool,
        can_list: bool,
    ) -> list[str]:
        """Decide which indices this sync copies -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            indices: The sync index set, from `_get_indices()`.
            target_repo: The target repo to sync with.
            existing_files: Paths the target listing returned.
            forced: Whether a package or Release change already forces the index pass.
            can_list: Whether `existing_files` is a real listing of the target.

        Returns:
            Index paths to copy, empty when nothing forces the pass.
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

    def _reduce_indices(
        self,
        indices: dict[str, IndexTarget],
        target_repo: RepoRW,
        existing_files: set[str],
    ) -> list[str]:
        """Drop indices the target already holds unchanged.

        Agreeing root Release digest blocks settle a held entry first (free), then its
        stored metadata (one round trip); nothing settles an absent entry, and the
        source is never probed -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            indices: Index path → expected digests and whether the source is known to publish it.
            target_repo: The target repo to sync with.
            existing_files: Objects already present in the target.

        Returns:
            Index paths still to copy.
        """
        target_loaded = target_repo.release.is_loaded
        ret: list[str] = []
        for file, target in indices.items():
            held = file in existing_files

            if held and target.digests and self.params.verify_digests and target_loaded:
                if target_repo.release_agrees(file, target.digests):
                    continue
                if target_repo.holds(file, target.digests):
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
        target_repo: RepoRW,
        progress: tuple[int, int] | None = None,
        *,
        missing_ok: bool = False,
    ) -> bool:
        """Transfer a file from this repo to another repo.

        Save digests of the file in metadata.

        Args:
            file: The file to transfer.
            target_repo: The target repo to transfer to.
            progress: Progress of transfer - current of total objects. Defaults to None.
            missing_ok: Skip the file when the source lacks it, as for an index nothing proved
                published -> [VALIDATION.md](VALIDATION.md#index-selection).

        Raises:
            ChecksumException: If the checksum verification fails.

        Returns:
            True when the file needs no retry: it was transferred, or `missing_ok` is set and the
            source lacks it. False otherwise.
        """
        if progress is None:
            logger.info("Copy: %s", file)
        else:
            logger.info("Copy (%d/%d): %s", progress[0], progress[1], file)

        try:
            digests = target_repo.transfer_from(self.storage, file, path_dst=file)
        except StorageNotFoundException as exc:
            if missing_ok:
                logger.debug("%s listed in Release but missing in source. Skipped.", file)
                return True
            logger.error("Error transfer file %s: %s", file, exc)
            return False
        except StorageException as exc:
            logger.error("Error transfer file %s: %s", file, exc)
            return False

        if self.params.verify_digests and not self.verify_checksum(file, digests):
            target_repo.delete(file)
            raise ChecksumException(f"Content verify mismatch in {file}")

        target_repo.record_digests(file, digests)

        return True

    def sync(self, target_repo: RepoRW, plan: SyncPlan) -> None:
        """Sync this repo with another repo.

        Either every object transfers or the sync raises: a target that holds a new
        root `Release` describing objects it never received would advertise content
        it cannot serve. The caller wraps the run in a transaction, so raising leaves
        the target at its previous consistent state
        -> [STORAGE.md](STORAGE.md#transactions). An `unproven` path the source lacks
        is the one exception: the source serves it no more than the target will
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            target_repo: The target repo to sync with.
            plan: What to copy and delete, as `build_sync_lists` planned it.

        Raises:
            RepoSyncException: If any object still fails after the retries.
            ChecksumException: If a transferred object does not match its expected
                digests; it is never retried → [CRYPTO.md](CRYPTO.md#verification-failures).
        """
        # each pass retries only what the previous one failed; a transient error should not
        # fail the whole run
        pending = plan.copy
        for _ in range(TRANSFER_MAX_ERR_RETRIES):
            total = len(pending)
            pending = [
                name
                for cnt, name in enumerate(pending, start=1)
                if not self._transfer_file(
                    name, target_repo, progress=(cnt, total), missing_ok=name in plan.unproven
                )
            ]
            if not pending:
                break
        else:
            logger.debug("failed packages: %s", pending)
            raise RepoSyncException("Max retries reached.")

        # delete objects
        for name in plan.delete:
            target_repo.delete(name)
