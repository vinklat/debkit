"""Sync (mirror) a Debian repo from a source to a target storage, and check a repo."""

from __future__ import annotations

import logging
from argparse import BooleanOptionalAction
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from humanize import naturalsize
from txstore import get_supported_schemes, maybe_transaction, redact_url

from debkit.action import (
    Action,
    ActionException,
    OutputType,
    attach_storage_repo,
    dump_json,
    open_target_repo,
)
from debkit.repo.component import IndexLoad
from debkit.repo.hashes import ChecksumException
from debkit.repo.params import is_valid_arch
from debkit.repo.release import ReleaseNotFoundException
from debkit.repo.repo_sync import RepoSync

if TYPE_CHECKING:
    from argparse import Namespace

    from debkit._options import EnvArgumentParser
    from debkit.repo.repo_diff import RepoDiff
    from debkit.repo.repo_rw import RepoRW

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
        value: The raw flag value.

    Returns:
        The non-empty tokens, in order.
    """
    return [token for token in value.split(",") if token]


def _selection(pars: Namespace) -> tuple[list[str] | None, list[str] | None]:
    """Read the `--architectures` and `--indices-exclude` selection `sync` and `check` share.

    A check builds its index and package sets the way a sync does, so both read one
    selection -> [CONFIG.md](CONFIG.md#check).

    Args:
        pars: Command line arguments from argparser.

    Raises:
        ActionException: An `--architectures` token is not an architecture name.

    Returns:
        The selected architectures and the index exclude globs, each None when its flag is
        empty.
    """
    archs: list[str] | None = None
    if pars.architectures != "":
        archs = _split_csv(pars.architectures)
        for arch in archs:
            if not is_valid_arch(arch):
                raise ActionException(f"Invalid architecture: {arch!r}")

    # index excludes apply to both repos so --verify-existing agrees with the sync
    excludes = _split_csv(pars.indices_exclude) if pars.indices_exclude != "" else None
    return archs, excludes


class ActionSync(Action):
    """Sync debian repo from source repository.

    Attributes:
        source_repo: The repository the sync reads.
        source_url: The source deb source string; logged only through `redact_url`.
        target_url: The target deb source string; logged only through `redact_url`.
    """

    source_repo: RepoSync

    def __init__(self, pars: Namespace, create_path: bool = False):
        """Initialize ActionSync.

        Args:
            pars: Command line arguments from argparser.
            create_path: Create path or bucket if True.

        Raises:
            ActionException: An `--architectures` token is not an architecture name; refused
                before any storage is opened, so no target path or bucket is created.
        """
        archs, indices_exclude = _selection(pars)
        super().__init__(pars)
        self.verify_existing: bool = pars.verify_existing
        self.indices_only: bool = pars.indices_only
        self.allow_release_rollback: bool = pars.allow_release_rollback
        self.source_url: str = pars.source_url
        self.target_url: str = pars.target_url

        # the source, then the target, each with the selection applied
        logger.debug("Prepare source URL: %s", redact_url(pars.source_url))
        storage, params = attach_storage_repo(
            pars, pars.source_url, archs=archs, indices_exclude=indices_exclude
        )
        self.source_repo = RepoSync(storage, params, gpg=self.gpg)
        # a sync overwrites the target, so it gets no `GnuPG`: its Release is read
        # unverified and never signed -> [CRYPTO.md](CRYPTO.md#gpg)
        self.target_repo = open_target_repo(
            pars, gpg=None, create_path=create_path, archs=archs, indices_exclude=indices_exclude
        )

    def _print_sync_brief(self, diff: RepoDiff) -> None:
        """Print sync brief output.

        Args:
            diff: Repository diff object.
        """
        print(f"Added {diff.count_pkgs_to_copy()} packages.")
        print(f"Removed {diff.count_pkgs_to_remove()} packages.")
        print(
            f"{self._target_metrics.write_objects_total} objects "
            f"({naturalsize(self._target_metrics.write_bytes)}) transferred."
        )

    def _print_sync_json(self, diff: RepoDiff, source: RepoSync) -> None:
        """Print sync json output.

        Args:
            diff: Repository diff object.
            source: The source repository the sync read.
        """
        json_dict: dict[str, Any] = {
            "packages": diff.dump(),
            "transfer": {"metrics": {}},
        }
        json_dict["transfer"]["metrics"]["source_storage"] = source.storage.metrics.dump()
        json_dict["transfer"]["metrics"]["target_storage"] = self._target_metrics.dump()

        print(dump_json(json_dict))

    def sync(self) -> None:
        """Sync debian repo from source storage to target storage."""
        if self.allow_release_rollback:
            logger.warning("Release freshness checks skipped: --allow-release-rollback is set")

        with maybe_transaction(self.target_repo.storage, self.history_retention):
            # a peer may commit before `history_lock` is granted, so the target every pass
            # plans and floors against is read under it -> txstore locking rules
            logger.info("---------- Load target repo (%s)...", redact_url(self.target_url))
            run_floor = self._load_target()
            # the report compares against the target as this run found it
            target_before = self.target_repo.snapshot()
            if self.verify_existing:
                self._verify_existing(self.target_repo)

            logger.info("---------- Load source repo (%s)...", redact_url(self.source_url))
            self.source_repo.load(IndexLoad.STRICT)
            self._check_fresh(run_floor)
            self._sync_until_settled(run_floor)

        ret_diff = self.source_repo.diff(target_before)
        if self.output == OutputType.json:
            self._print_sync_json(ret_diff, self.source_repo)
        else:
            self._print_sync_brief(ret_diff)

    def _sync_until_settled(self, run_floor: str | None) -> None:
        """Run sync passes until the source Release holds across one.

        A source that changed during a pass is read again with the target the pass staged,
        and checked against both that target and the Release the run found. After
        `MAX_UPDATED_REPO_RETRIES` passes the source is not read again
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            run_floor: The target Release `Date` as the run found it.
        """
        for _ in range(MAX_UPDATED_REPO_RETRIES - 1):
            if not self._sync_pass():
                return
            synced_sha256 = self.source_repo.release.content_sha256
            logger.info("---------- Retrieve load to verify source repo changes...")
            self.source_repo.load(IndexLoad.STRICT)
            if self.source_repo.release.content_sha256 == synced_sha256:
                return
            logger.info("Source repo modified during synchronisation! Repeat sync...")
            # reads go through the transaction, so the target now describes the bytes the
            # previous pass staged -> txstore footmark rules; the run never goes below the
            # Release it found -> [CRYPTO.md](CRYPTO.md#release-freshness)
            self._check_fresh(self._load_target(), run_floor)
        if self._sync_pass():
            logger.warning(
                "Source repo still changing after %d passes; committing the last pass",
                MAX_UPDATED_REPO_RETRIES,
            )

    def _sync_pass(self) -> bool:
        """Plan one pass from the loaded source and target, and sync it.

        Returns:
            False when the plan was empty, so nothing was synced.
        """
        logger.info("Building filelists...")
        plan = self.source_repo.build_sync_lists(self.target_repo, indices_only=self.indices_only)
        if plan.is_empty():
            logger.info("Nothing to sync.")
            return False
        # a ChecksumException propagates to discard the transaction; cli.py logs it
        self.source_repo.sync(self.target_repo, plan)
        return True

    def _check_fresh(self, *floors: str | None) -> None:
        """Check the loaded source Release against each floor, in order.

        Skipped without GPG verification or with `--allow-release-rollback`
        -> [CRYPTO.md](CRYPTO.md#release-freshness).

        Args:
            floors: Target Release `Date` values the source must not undercut; None floors
                nothing.
        """
        if not self.source_repo.params.verify_gpg or self.allow_release_rollback:
            return
        now = datetime.now(UTC)
        for floor in floors:
            self.source_repo.release.check_fresh_against(floor, now)

    def _load_target(self) -> str | None:
        """Load the target repo, treating a missing or corrupt one as empty.

        A target that fails a checksum is reset, so nothing it recorded settles an
        index -> [VALIDATION.md](VALIDATION.md#index-selection). Its Release still
        floors the source, so the `Date` it carried is returned
        -> [CRYPTO.md](CRYPTO.md#release-freshness).

        Returns:
            The target Release `Date` as read, taken before any reset; None without one.
        """
        try:
            # the sync plans from the source and only copies indices, never writing one from
            # this load, so an unreadable target index is skipped
            # -> [VALIDATION.md](VALIDATION.md#index-loading)
            self.target_repo.load(IndexLoad.READ_ONLY)
        except ReleaseNotFoundException as exc:
            logger.warning("Target repo is empty: %s", exc)
        except ChecksumException as exc:
            logger.warning("Target repo corrupted: %s indices IGNORED", exc)
            floor = self.target_repo.release.fields.get("Date")
            self.target_repo.reset()
            return floor
        return self.target_repo.release.fields.get("Date")

    @staticmethod
    def _verify_existing(target: RepoRW) -> None:
        """Verify the loaded target's payloads, deleting each that fails.

        Runs inside the sync transaction, so the deletions are staged and recorded
        with the sync, whose repair pass copies each deleted payload again
        -> [STORAGE.md](STORAGE.md#transactions).

        Args:
            target: The loaded target repository.
        """
        logger.info("---------- Verify target repo packages...")
        # orphans are left to the sync's own purge
        report = target.verify_payloads(fix=True)
        if report.failed > report.fixed:
            logger.warning(
                "%d target files failed verification and were not deleted",
                report.failed - report.fixed,
            )

    @staticmethod
    def add_selection_cli_args(parser: EnvArgumentParser) -> None:
        """Register the selection flags shared by the sync and check subcommands.

        A check builds its index and package sets the way a sync does, so both
        subcommands read the same dests, env fallbacks and defaults from one
        registration: a check run with other values reports the difference as
        missing or orphan objects and `--fix` acts on it
        -> [CONFIG.md](CONFIG.md#check).

        Args:
            parser: The parser to add arguments to.
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
    def add_sync_cli_args(parser: EnvArgumentParser) -> None:
        """Add argparser arguments for sync.

        Args:
            parser: The parser to add arguments to.
        """
        parser.add_env_argument(
            "--source-url",
            env="DEBKIT_SOURCE_URL",
            dest="source_url",
            help="source repository 'URL DIST [COMPONENT ...]'; URL is a filesystem path or uses "
            f"one of the schemes {', '.join(get_supported_schemes())} "
            "(example: 'https://deb.debian.org/debian bookworm main')",
            required=True,
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


class CheckStatus(NamedTuple):
    """The totals a check reports; the JSON output prints them under these names.

    Attributes:
        errors_total: Missing, orphan, and failed files.
        errors_fixed_total: Files `--fix` deleted.
    """

    errors_total: int
    errors_fixed_total: int


class ActionCheck(Action):
    """Check a repository against its own indices.

    Attributes:
        fix: Delete corrupt payloads and orphans if True.
    """

    def __init__(self, pars: Namespace, create_path: bool = False):
        """Initialize ActionCheck.

        Args:
            pars: Command line arguments from argparser.
            create_path: Create path or bucket if True.

        Raises:
            ActionException: An `--architectures` token is not an architecture name; refused
                before any storage is opened.
        """
        archs, indices_exclude = _selection(pars)
        super().__init__(pars)
        self.fix: bool = pars.fix
        self.target_repo = open_target_repo(
            pars,
            gpg=self.gpg,
            create_path=create_path,
            archs=archs,
            indices_exclude=indices_exclude,
        )

    def check(self) -> bool:
        """Check debian repo for consistency.

        Returns:
            True if repo is consistent.
        """
        # `--fix` writes, so it is recorded as one transaction -> STORAGE.md#transactions
        with (
            maybe_transaction(self.target_repo.storage, self.history_retention)
            if self.fix
            else nullcontext()
        ):
            try:
                # `--fix` deletes what no loaded index claims, so it never skips one
                # -> [VALIDATION.md](VALIDATION.md#index-loading)
                self.target_repo.load(IndexLoad.WRITABLE if self.fix else IndexLoad.READ_ONLY)
            except ReleaseNotFoundException as exc:
                logger.error("Target repo is empty: %s", exc)
                return False
            status = self._check_target(self.target_repo)

        if self.output == OutputType.json:
            self._print_check_json(status)
        else:
            self._print_check_brief(status)

        return status.errors_total == status.errors_fixed_total

    def _check_target(self, target: RepoRW) -> CheckStatus:
        """Check the loaded target against its own indices, fixing it with `--fix`.

        Args:
            target: The loaded target repository.

        Returns:
            The error and fixed-error totals.
        """
        report = target.check(fix=self.fix)

        if report.missing:
            logger.warning(
                "Files is not in sync with indices... %s files missing.", len(report.missing)
            )
            logger.debug("Missing files: %s", report.missing)

        if report.orphans:
            logger.warning("Found %s orphan files in storage.", len(report.orphans))
            logger.info("Orphan files: %s", report.orphans)

        return CheckStatus(
            errors_total=len(report.missing) + len(report.orphans) + report.failed,
            errors_fixed_total=report.fixed,
        )

    def _print_check_brief(self, status: CheckStatus) -> None:
        """Print check brief output.

        Args:
            status: The error and fixed-error totals.
        """
        print(f"Errors total: {status.errors_total} files")
        print(f"Errors fixed: {status.errors_fixed_total} files")

    def _print_check_json(self, status: CheckStatus) -> None:
        """Print check json output.

        Args:
            status: The error and fixed-error totals.
        """
        json_dict: dict[str, dict[str, Any]] = {"status": status._asdict(), "metrics": {}}
        json_dict["metrics"]["target_storage"] = self._target_metrics.dump()

        print(dump_json(json_dict))

    @staticmethod
    def add_check_cli_args(parser: EnvArgumentParser) -> None:
        """Add argparser arguments for check.

        The selection flags come from `add_selection_cli_args`, which sync registers
        too.

        Args:
            parser: The parser to add arguments to.
        """
        parser.add_argument(
            "--fix",
            action="store_true",
            dest="fix",
            help="delete corrupted files from storage",
            default=False,
        )
