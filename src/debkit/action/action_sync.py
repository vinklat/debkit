"""Sync (mirror) debian repo from source to target storage."""

# pylint: disable=duplicate-code

import logging
from argparse import BooleanOptionalAction
from contextlib import nullcontext
from copy import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from humanize import naturalsize
from txstore import get_supported_schemes, maybe_transaction, redact_url

from debkit.action import Action, ActionException, OutputType, attach_storage_repo, dump_json
from debkit.repo.hashes import ChecksumException
from debkit.repo.params import is_valid_arch
from debkit.repo.release import ReleaseNotFoundException
from debkit.repo.repo_rw import RepoRW
from debkit.repo.repo_sync import RepoSync

if TYPE_CHECKING:
    from argparse import Namespace

    from debkit._options import EnvArgumentParser
    from debkit.repo.release import Release
    from debkit.repo.repo_diff import RepoDiff

ARCHITECTURES_DEFAULT = "amd64,arm64"
INDICES_EXCLUDE_DEFAULT = ""

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# a maximum number of repetitions if the source has changed during synchronization
MAX_UPDATED_REPO_RETRIES = 4


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated flag value, dropping empty tokens.

    A value of commas alone yields an empty list, which callers still apply
    -> [VALIDATION.md](VALIDATION.md#architectures).

    Args:
        value (str): The raw flag value.

    Returns:
        List[str]: The non-empty tokens, in order.
    """
    return [token for token in value.split(",") if token]


def _checker_for(target: "RepoRW") -> RepoSync:
    """Wrap a loaded target in a `RepoSync` that checks the target's own storage.

    Args:
        target (RepoRW): The loaded target repository.

    Returns:
        RepoSync: A repository over the target storage holding the target's indices.
    """
    checker = RepoSync(target.storage, target.params)
    checker.copy_from(target)
    return checker


class ActionSync(Action):
    """Sync debian repo from source repository.

    Attributes:
        source_repo (RepoSync | None): The `sync` source; None for `check`.
        fix (bool): Fix inconsistencies if True.
    """

    def __init__(self, pars: "Namespace", create_path: bool = False):
        """Initialize ActionSync.

        Args:
            pars (Namespace): Command line arguments from argparser.
            create_path (bool): Create path or bucket if True.

        Raises:
            ActionException: An `--architectures` token is not an architecture name; refused
                before any storage is opened, so no target path or bucket is created.
        """
        self._archs: list[str] | None = None
        if pars.architectures != "":
            self._archs = _split_csv(pars.architectures)
            for arch in self._archs:
                if not is_valid_arch(arch):
                    raise ActionException(f"Invalid architecture: {arch!r}")

        # index excludes apply to both repos so --verify-existing agrees with the sync
        self._indices_exclude: list[str] | None = (
            _split_csv(pars.indices_exclude) if pars.indices_exclude != "" else None
        )

        self.source_repo: RepoSync | None = None
        super().__init__(pars, create_path=create_path)
        self.fix: bool = getattr(pars, "fix", False)
        self.verify_existing: bool = getattr(pars, "verify_existing", False)
        self.indices_only: bool = getattr(pars, "indices_only", False)
        self.allow_release_rollback: bool = getattr(pars, "allow_release_rollback", False)

    def _attach_repos(self, pars: "Namespace", create_path: bool) -> None:
        """Open the `sync` source, then the target, each with the selection applied.

        `check` has no `--source-url`. A `sync` overwrites the target, so the target
        gets no `GnuPG`: its Release is read unverified and never signed
        -> [CRYPTO.md](CRYPTO.md#gpg).

        Args:
            pars (Namespace): Command line arguments from argparser.
            create_path (bool): Create the target path or bucket if True.
        """
        if hasattr(pars, "source_url"):
            logger.debug("Prepare source URL: %s", redact_url(pars.source_url))
            storage, params = attach_storage_repo(
                pars, pars.source_url, archs=self._archs, indices_exclude=self._indices_exclude
            )
            self.source_repo = RepoSync(storage, params, gpg=self.gpg)

        logger.debug("Prepare target URL: %s", redact_url(pars.target_url))
        storage, params = attach_storage_repo(
            pars,
            pars.target_url,
            create_path=create_path,
            archs=self._archs,
            indices_exclude=self._indices_exclude,
        )
        self.target_repo = RepoRW(
            storage, params, gpg=self.gpg if self.source_repo is None else None
        )

    def _print_sync_brief(self, diff: "RepoDiff") -> None:
        """Print sync brief output.

        Args:
            diff (RepoDiff): Repository diff object.
        """
        print(f"Added {diff.count_pkgs_to_copy()} packages.")
        print(f"Removed {diff.count_pkgs_to_remove()} packages.")
        print(
            f"{self.target_repo.storage.metrics.write_objects_total} objects "
            f"({naturalsize(self.target_repo.storage.metrics.write_bytes)}) transferred."
        )

    def _print_sync_json(self, diff: "RepoDiff", source: RepoSync) -> None:
        """Print sync json output.

        Args:
            diff (RepoDiff): Repository diff object.
            source (RepoSync): The source repository the sync read.
        """
        json_dict: dict[str, Any] = {
            "packages": diff.dump(),
            "transfer": {"metrics": {}},
        }
        json_dict["transfer"]["metrics"]["source_storage"] = source.storage.metrics.dump()
        json_dict["transfer"]["metrics"]["target_storage"] = self.target_repo.storage.metrics.dump()

        print(dump_json(json_dict, indent=4))

    def sync(self) -> None:
        """Sync debian repo from source storage to target storage."""
        if self.source_repo is None:
            raise ActionException("sync needs a source repository (--source-url)")

        if self.allow_release_rollback:
            logger.warning("Release freshness checks skipped: --allow-release-rollback is set")

        prev_hash: str = ""
        with maybe_transaction(self.target_repo.storage, self.history_retention):
            # a peer may commit before `history_lock` is granted, so the target every pass
            # plans and floors against is read under it -> txstore locking rules
            logger.info(
                "---------- Load target repo (%s)...", redact_url(self.target_repo.params.url or "")
            )
            target_floor = run_floor = self._load_target()
            # the report compares against the target as this run found it
            target_before = copy(self.target_repo)
            if self.verify_existing:
                self._verify_existing(self.target_repo)

            for attempt in range(MAX_UPDATED_REPO_RETRIES):
                if attempt == 0:
                    logger.info(
                        "---------- Load source repo (%s)...",
                        redact_url(self.source_repo.params.url or ""),
                    )
                else:
                    logger.info("---------- Retrieve load to verify source repo changes...")

                self.source_repo.update(strict_arch=True)

                if attempt > 0:
                    if prev_hash == self.source_repo.release.hash:
                        break
                    logger.info("Source repo modified during synchronisation! Repeat sync...")
                    # reads go through the transaction, so the target now describes the
                    # bytes the previous pass staged -> txstore footmark rules
                    target_floor = self._load_target()

                if self.source_repo.params.verify_gpg and not self.allow_release_rollback:
                    now = datetime.now(UTC)
                    self.source_repo.release.check_fresh_against(target_floor, now)
                    if attempt > 0:
                        # the run never goes below the Release it found
                        # -> [CRYPTO.md](CRYPTO.md#release-freshness)
                        self.source_repo.release.check_fresh_against(run_floor, now)

                logger.info("Building filelists...")
                copy_list, delete_list, unproven = self.source_repo.build_sync_lists(
                    self.target_repo, indices_only=self.indices_only
                )

                if len(copy_list) == 0 and len(delete_list) == 0:
                    logger.info("Nothing to sync.")
                    break

                # a ChecksumException propagates to discard the transaction; cli.py logs it
                self.source_repo.sync(self.target_repo, copy_list, delete_list, unproven=unproven)

                prev_hash = self.source_repo.release.hash
            else:
                # -> [VALIDATION.md](VALIDATION.md#index-selection)
                logger.warning(
                    "Source repo still changing after %d passes; committing the last pass",
                    MAX_UPDATED_REPO_RETRIES,
                )

        ret_diff = self.source_repo.diff(target_before)
        if self.output == OutputType.json:
            self._print_sync_json(ret_diff, self.source_repo)
        else:
            self._print_sync_brief(ret_diff)

    def _load_target(self) -> "Release":
        """Load the target repo, treating a missing or corrupt one as empty.

        A target that fails a checksum is reset, so nothing it recorded settles an
        index -> [VALIDATION.md](VALIDATION.md#index-selection). Its Release still
        floors the source, so the `Date` it carried is kept in the returned snapshot
        -> [CRYPTO.md](CRYPTO.md#release-freshness).

        Returns:
            Release: A snapshot of the target Release as read, taken before any reset.
        """
        try:
            self.target_repo.update()
        except ReleaseNotFoundException as exc:
            logger.warning("Target repo is empty: %s", exc)
        except ChecksumException as exc:
            logger.warning("Target repo corrupted: %s indices IGNORED", exc)
            floor = copy(self.target_repo.release)
            self.target_repo.reset()
            return floor
        return copy(self.target_repo.release)

    @staticmethod
    def _verify_existing(target: "RepoRW") -> None:
        """Verify the loaded target's payloads, deleting each that fails.

        Runs inside the sync transaction, so the deletions are staged and recorded
        with the sync, whose repair pass copies each deleted payload again
        -> [STORAGE.md](STORAGE.md#transactions).

        Args:
            target (RepoRW): The loaded target repository.
        """
        logger.info("---------- Verify target repo packages...")
        checker = _checker_for(target)
        missing_list, _ = checker.consistency_lists()
        err_total, err_fixed_total = checker.check(missing_list, [], fix=True)
        if err_total > err_fixed_total:
            logger.warning(
                "%d target files failed verification and were not deleted",
                err_total - err_fixed_total,
            )

    def _print_check_brief(self, status: dict[str, int]) -> None:
        """Print check brief output.

        Args:
            status (dict[str, int]): Dictionary with errors and fixed errors.
        """
        print(f"Errors total: {status['errors_total']} files")
        print(f"Errors fixed: {status['errors_fixed_total']} files")

    def _print_check_json(self, status: dict[str, int]) -> None:
        """Print check json output.

        Args:
            status (dict[str, int]): Dictionary with errors and fixed errors.
        """
        json_dict: dict[str, dict[str, Any]] = {"status": status, "metrics": {}}
        json_dict["metrics"]["target_storage"] = self.target_repo.storage.metrics.dump()

        print(dump_json(json_dict, indent=4))

    def check(self) -> bool:
        """Check debian repo for consistency.

        Returns:
            bool: True if repo is consistent.
        """
        # `--fix` writes, so it is recorded as one transaction -> STORAGE.md#transactions
        with (
            maybe_transaction(self.target_repo.storage, self.history_retention)
            if self.fix
            else nullcontext()
        ):
            try:
                self.target_repo.update()
            except ReleaseNotFoundException as exc:
                logger.error("Target repo is empty: %s", exc)
                return False
            status = self._check_target(self.target_repo)

        if self.output == OutputType.json:
            self._print_check_json(status)
        else:
            self._print_check_brief(status)

        return status["errors_total"] == status["errors_fixed_total"]

    def _check_target(self, target: "RepoRW") -> dict[str, int]:
        """Check the loaded target against its own indices, fixing it with `--fix`.

        Args:
            target (RepoRW): The loaded target repository.

        Returns:
            Dict[str, int]: The error and fixed-error totals.
        """
        dup_repo = _checker_for(target)
        missing_list, orphan_list = dup_repo.consistency_lists()

        list_err_total: int = 0
        if len(missing_list) > 0:
            logger.warning(
                "Files is not in sync with indices... %s files missing.", len(missing_list)
            )
            logger.debug("Missing files: %s", missing_list)
            list_err_total = len(missing_list)

        if len(orphan_list) > 0:
            logger.warning("Found %s orphan files in storage.", len(orphan_list))
            logger.info("Orphan files: %s", orphan_list)
            list_err_total += len(orphan_list)

        file_err_total, file_err_fixed_total = dup_repo.check(
            missing_list, orphan_list, fix=self.fix
        )

        return {
            "errors_total": list_err_total + file_err_total,
            "errors_fixed_total": file_err_fixed_total,
        }

    @staticmethod
    def add_selection_cli_args(parser: "EnvArgumentParser") -> None:
        """Register the selection flags shared by the sync and check subcommands.

        A check builds its index and package sets the way a sync does, so both
        subcommands read the same dests, env fallbacks and defaults from one
        registration: a check run with other values reports the difference as
        missing or orphan objects and `--fix` acts on it
        -> [CONFIG.md](../../../CONFIG.md#check).

        Args:
            parser (EnvArgumentParser): The parser to add arguments to.
        """
        parser.add_env_argument(
            "--architectures",
            env="DEBKIT_ARCHITECTURES",
            dest="architectures",
            help="comma separated list of architectures to limit the selection",
            default=ARCHITECTURES_DEFAULT,
        )

        parser.add_env_argument(
            "--indices-exclude",
            env="DEBKIT_INDICES_EXCLUDE",
            dest="indices_exclude",
            help="comma separated globs of Release-listed index paths to skip "
            "(Example: '*/i18n/*,*/dep11/icons-*')",
            default=INDICES_EXCLUDE_DEFAULT,
        )

    @staticmethod
    def add_sync_cli_args(parser: "EnvArgumentParser") -> None:
        """Add argparser arguments for sync.

        Args:
            parser (EnvArgumentParser): The parser to add arguments to.
        """
        parser.add_env_argument(
            "--source-url",
            env="DEBKIT_SOURCE_URL",
            dest="source_url",
            help="source repository URL or filesystem path; schemes: "
            f"{', '.join(get_supported_schemes())} (example: s3://endpoint/bucket/prefix)",
            default=".",
        )

        parser.add_env_argument(
            "--allow-release-rollback",
            env="DEBKIT_ALLOW_RELEASE_ROLLBACK",
            dest="allow_release_rollback",
            help="accept a source Release older than the target or past its Valid-Until",
            action=BooleanOptionalAction,
            default=False,
        )

        parser.add_argument(
            "--verify-existing",
            action="store_true",
            dest="verify_existing",
            help="verify existing packages before sync",
            default=False,
        )

        parser.add_argument(
            "--indices-only",
            action="store_true",
            dest="indices_only",
            help="force sync indices objects, skip deb packages",
            default=False,
        )

    @staticmethod
    def add_check_cli_args(parser: "EnvArgumentParser") -> None:
        """Add argparser arguments for check.

        The selection flags come from `add_selection_cli_args`, which sync registers
        too.

        Args:
            parser (EnvArgumentParser): The parser to add arguments to.
        """
        parser.add_argument(
            "--fix",
            action="store_true",
            dest="fix",
            help="delete corrupted files from storage",
            default=False,
        )
