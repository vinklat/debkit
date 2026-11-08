"""Sync (mirror) debian repo from source to target storage."""

# pylint: disable=duplicate-code

import logging
from argparse import BooleanOptionalAction
from copy import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from humanize import naturalsize
from txstore import get_supported_schemes, maybe_transaction

from debkit.action import Action, ActionException, OutputType, dump_json
from debkit.repo.hashes import ChecksumException
from debkit.repo.params import is_valid_arch
from debkit.repo.release import ReleaseReadException
from debkit.repo.repo_sync import RepoSync

if TYPE_CHECKING:
    from argparse import Namespace

    from debkit._options import EnvArgumentParser
    from debkit.repo.repo_diff import RepoDiff

ARCHITECTURES_DEFAULT = "amd64,arm64"
INDICES_EXCLUDE_DEFAULT = ""

# create logger
logging.getLogger(__name__).addHandler(logging.NullHandler())

RESET_TARGET_DEFAULT = False
IGNORE_TARGET_INDEX_DEFAULT = False
SKIP_DEBS_DEFAULT = False

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


class ActionSync(Action):
    """Sync debian repo from source repository.

    Attributes:
        output (OutputType): Output type.
        fix (bool): Fix inconsistencies if True.
    """

    def __init__(self, pars: "Namespace", create_path: bool = False):
        """Initialize ActionSync.

        Args:
            pars (Namespace): Command line arguments from argparser.
            create_path (bool): Create path or bucket if True.
        """
        super().__init__(pars, create_path=create_path)
        self.output: OutputType = pars.output if hasattr(pars, "output") else OutputType.brief
        self.fix: bool = pars.fix if hasattr(pars, "fix") else False
        self.verify_existing: bool = (
            pars.verify_existing if hasattr(pars, "verify_existing") else False
        )
        self.indices_only: bool = pars.indices_only if hasattr(pars, "indices_only") else False
        self.allow_release_rollback: bool = getattr(pars, "allow_release_rollback", False)

        # force selected architectures from argparser
        if getattr(pars, "architectures", "") != "":
            archs = _split_csv(pars.architectures)
            for arch in archs:
                if not is_valid_arch(arch):
                    raise ActionException(f"Invalid architecture: {arch!r}")
            if self.source_repo is not None:
                self.source_repo.params.archs = archs
            if self.target_repo is not None:
                self.target_repo.params.archs = archs

        # index excludes apply to both repos so --verify-existing agrees with the sync
        if getattr(pars, "indices_exclude", "") != "":
            excludes = _split_csv(pars.indices_exclude)
            if self.source_repo is not None:
                self.source_repo.params.indices_exclude = excludes
            if self.target_repo is not None:
                self.target_repo.params.indices_exclude = excludes

    def _print_sync_brief(self, diff: "RepoDiff") -> None:
        """Print sync brief output.

        Args:
            diff (RepoDiff): Repository diff object.
        """
        if self.source_repo is None or self.target_repo is None:
            return

        print(f"Added {diff.count_pkgs_to_copy()} packages.")
        print(f"Removed {diff.count_pkgs_to_remove()} packages.")
        print(
            f"{self.target_repo.storage.metrics.write_objects_total} objects "
            f"({naturalsize(self.target_repo.storage.metrics.write_bytes)}) transferred."
        )

    def _print_sync_json(self, diff: "RepoDiff") -> None:
        """Print sync json output.

        Args:
            diff (RepoDiff): Repository diff object.
        """
        if self.source_repo is None or self.target_repo is None:
            return

        json_dict: dict[str, Any] = {
            "packages": diff.dump(),
            "transfer": {"metrics": {}},
        }
        json_dict["transfer"]["metrics"]["source_storage"] = self.source_repo.storage.metrics.dump()
        json_dict["transfer"]["metrics"]["target_storage"] = self.target_repo.storage.metrics.dump()

        print(dump_json(json_dict, indent=4))

    def sync(self) -> None:
        """Sync debian repo from source storage to target storage."""
        if self.source_repo is None or self.target_repo is None:
            # TODO raise exception
            logging.error("Source or target repo is not initialized")
            return

        if self.verify_existing:
            try:
                logging.info(
                    "---------- Load target repo to verify (%s)...", self.target_repo.params.url
                )
                self._load_target()
            finally:
                dup_repo = RepoSync(self.target_repo.storage, self.target_repo.params)
                dup_repo.copy_from(self.target_repo)
                missing_list, _ = dup_repo.build_sync_lists(self.target_repo)
                dup_repo.check(missing_list, [], fix=True)

        if self.allow_release_rollback:
            logging.warning("Release freshness checks skipped: --allow-release-rollback is set")

        pass_cnt: int = MAX_UPDATED_REPO_RETRIES
        prev_hash: str = "nohash"
        with maybe_transaction(self.target_repo.storage, self.history_retention):
            # a peer may commit before `history_lock` is granted, so the target every pass
            # plans and floors against is read under it -> txstore locking rules
            logging.info("---------- Load target repo (%s)...", self.target_repo.params.url)
            self._load_target()
            # the report compares against the target as this run found it
            target_before = copy(self.target_repo)

            while pass_cnt > 0:
                if pass_cnt == MAX_UPDATED_REPO_RETRIES:
                    logging.info("---------- Load source repo (%s)...", self.source_repo.params.url)
                else:
                    logging.info("---------- Retrieve load to verify source repo changes...")

                self.source_repo.update(strict_arch=True)

                if prev_hash == self.source_repo.release.hash:
                    break

                if pass_cnt < MAX_UPDATED_REPO_RETRIES:
                    logging.info("Source repo modified during synchronisation! Repeat sync...")
                    # reads go through the transaction, so the target now describes the
                    # bytes the previous pass staged -> txstore footmark rules
                    self._load_target()

                if self.source_repo.params.verify_gpg and not self.allow_release_rollback:
                    now = datetime.now(UTC)
                    self.source_repo.release.check_fresh_against(self.target_repo.release, now)
                    if pass_cnt < MAX_UPDATED_REPO_RETRIES:
                        # a staged target reset as corrupt has no Date; the run never goes
                        # below the Release it found -> [CRYPTO.md](CRYPTO.md#release-freshness)
                        self.source_repo.release.check_fresh_against(target_before.release, now)

                logging.info("Building filelists...")
                copy_list, delete_list = self.source_repo.build_sync_lists(
                    self.target_repo, indices_only=self.indices_only
                )

                if len(copy_list) == 0 and len(delete_list) == 0:
                    logging.info("Nothing to sync.")
                    break

                try:
                    self.source_repo.sync(self.target_repo, copy_list, delete_list)
                except ChecksumException as exc:
                    logging.error("Checksum error: %s", exc)
                    raise

                pass_cnt -= 1
                prev_hash = self.source_repo.release.hash

        ret_diff = self.source_repo.diff(target_before)
        if self.output == OutputType.json:
            self._print_sync_json(ret_diff)
        else:
            self._print_sync_brief(ret_diff)

    def _load_target(self) -> None:
        """Load the target repo, treating a missing or corrupt one as empty.

        A target that fails a checksum is reset, so nothing it recorded settles an
        index -> [VALIDATION.md](VALIDATION.md#index-selection).
        """
        if self.target_repo is None:
            return
        try:
            self.target_repo.update()
        except ReleaseReadException as exc:
            logging.warning("Target repo is empty: %s", exc)
        except ChecksumException as exc:
            logging.warning("Target repo corrupted: %s indices IGNORED", exc)
            self.target_repo.reset()

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
        if self.target_repo is not None:
            json_dict["metrics"]["target_storage"] = self.target_repo.storage.metrics.dump()

        print(dump_json(json_dict, indent=4))

    def check(self) -> bool:
        """Check debian repo for consistency.

        Returns:
            bool: True if repo is consistent.
        """
        if self.target_repo is None:
            logging.error("Target repo is not initialized")
            return False

        try:
            self.target_repo.update()
        except ReleaseReadException as exc:
            logging.error("Target repo is empty: %s", exc)
            return False

        dup_repo = RepoSync(self.target_repo.storage, self.target_repo.params)
        dup_repo.copy_from(self.target_repo)
        missing_list, orphan_list = dup_repo.build_sync_lists(self.target_repo)

        list_err_total: int = 0
        if len(missing_list) > 0:
            logging.warning(
                "Files is not in sync with indices... %s files missing.", len(missing_list)
            )
            logging.debug("Missing files: %s", missing_list)
            list_err_total = len(missing_list)

        if len(orphan_list) > 0:
            logging.warning("Found %s orphan files in storage.", len(orphan_list))
            logging.info("Orphan files: %s", orphan_list)
            list_err_total += len(orphan_list)

        file_err_total, file_err_fixed_total = dup_repo.check(
            missing_list, orphan_list, fix=self.fix
        )

        status = {
            "errors_total": list_err_total + file_err_total,
            "errors_fixed_total": file_err_fixed_total,
        }

        if self.output == OutputType.json:
            self._print_check_json(status)
        else:
            self._print_check_brief(status)

        return (file_err_total + list_err_total) == file_err_fixed_total

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
