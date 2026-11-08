"""Command-line parser: flags, subcommands, and their ``DEBKIT_*`` fallbacks.

Every option is listed in [CONFIG.md](CONFIG.md); resolution order →
[CONFIG.md](CONFIG.md#resolution).
"""

from __future__ import annotations

from argparse import BooleanOptionalAction, Namespace, RawDescriptionHelpFormatter

from txstore import HISTORY_KEEP_DEFAULT, HISTORY_MAX_AGE_DAYS_DEFAULT, get_supported_schemes

from debkit import __version__
from debkit._logging import LOG_LEVELS, LogFormat
from debkit._options import EnvArgumentParser
from debkit.action import add_output_cli_args
from debkit.action.action_history import ActionHistory
from debkit.action.action_pkg import ActionPkg
from debkit.action.action_sync import ActionSync
from debkit.argtypes import (
    bucket_acl_type,
    history_max_age_type,
    object_acl_type,
    positive_int_type,
)

LOG_LEVEL_DEFAULT = "INFO"
GNUPG_HOME_DEFAULT = ".gnupg"
GNUPG_KEYRING_DEFAULT = "trustedkeys.gpg"
GNUPG_PASSPHRASE_DEFAULT = ""
# Repositories serve their indices to apt clients anonymously
# → [STORAGE.md](STORAGE.md#construction).
S3_DEFAULT_ACL_DEFAULT = "public-read"


def add_common_args(parser: EnvArgumentParser) -> None:
    """Register the global options on the root parser.

    Args:
        parser: Root parser.
    """
    parser.add_argument("-V", "--version", action="version", version=__version__)

    parser.add_env_argument(
        "--target-url",
        env="DEBKIT_TARGET_URL",
        dest="target_url",
        help="target repository 'URL DIST [COMPONENT ...]'; URL is a filesystem path or uses "
        f"one of the schemes {', '.join(get_supported_schemes())} "
        "(example: 's3://endpoint/bucket/debian bookworm main')",
        required=True,
    )
    parser.add_env_argument(
        "-l",
        "--log-level",
        env="DEBKIT_LOG_LEVEL",
        dest="log_level",
        help="logging level",
        type=str.upper,
        choices=LOG_LEVELS,
        default=LOG_LEVEL_DEFAULT,
    )
    parser.add_env_argument(
        "--log-format",
        env="DEBKIT_LOG_FORMAT",
        dest="log_format",
        help="log line format",
        type=LogFormat,
        choices=list(LogFormat),
        default=LogFormat.plain,
    )
    parser.add_env_argument(
        "-n",
        "--skip-verify",
        env="DEBKIT_SKIP_VERIFY",
        dest="skip_verify",
        help="skip TLS, GPG, and digest verification - INSECURE",
        action=BooleanOptionalAction,
        default=False,
    )
    parser.add_env_argument(
        "--skip-verify-ssl",
        env="DEBKIT_SKIP_VERIFY_SSL",
        dest="skip_verify_ssl",
        help="skip TLS certificate verification - INSECURE",
        action=BooleanOptionalAction,
        default=False,
    )
    parser.add_env_argument(
        "--skip-verify-gpg",
        env="DEBKIT_SKIP_VERIFY_GPG",
        dest="skip_verify_gpg",
        help="skip GPG signature verification and signing - INSECURE",
        action=BooleanOptionalAction,
        default=False,
    )
    parser.add_env_argument(
        "--gnupg-home",
        env="DEBKIT_GNUPG_HOME",
        dest="gnupg_home",
        help="GnuPG home directory",
        default=GNUPG_HOME_DEFAULT,
    )
    parser.add_env_argument(
        "--gnupg-keyring",
        env="DEBKIT_GNUPG_KEYRING",
        dest="gnupg_keyring",
        help="keyring whose keys are trusted for Release signatures",
        default=GNUPG_KEYRING_DEFAULT,
    )
    parser.add_env_variable(
        "DEBKIT_GNUPG_PASSPHRASE",
        dest="gnupg_passphrase",
        help="passphrase of the signing key",
        default=GNUPG_PASSPHRASE_DEFAULT,
    )
    parser.add_env_argument(
        "--history-keep",
        env="DEBKIT_HISTORY_KEEP",
        dest="history_keep",
        help="newest transactions never pruned from the history",
        type=positive_int_type,
        default=HISTORY_KEEP_DEFAULT,
    )
    parser.add_env_argument(
        "--history-max-age",
        env="DEBKIT_HISTORY_MAX_AGE",
        dest="history_max_age",
        help="days after which a transaction outside --history-keep is pruned; 0 disables",
        type=history_max_age_type,
        default=HISTORY_MAX_AGE_DAYS_DEFAULT,
    )


def add_s3_args(parser: EnvArgumentParser) -> None:
    """Register the S3 options on the root parser.

    Args:
        parser: Root parser.
    """
    parser.add_env_argument(
        "-u",
        "--s3-access",
        env="DEBKIT_S3_ACCESS",
        dest="s3_access",
        help="S3 access key ID; falls back to TXSTORE_S3_ACCESS",
    )
    parser.add_env_variable(
        "DEBKIT_S3_SECRET",
        dest="s3_secret",
        help="S3 secret key; falls back to TXSTORE_S3_SECRET",
    )
    parser.add_env_argument(
        "--s3-default-acl",
        env="DEBKIT_S3_DEFAULT_ACL",
        dest="s3_default_acl",
        help="canned ACL for written objects, or 'none' to send no ACL",
        type=object_acl_type,
        default=S3_DEFAULT_ACL_DEFAULT,
    )
    parser.add_env_argument(
        "--s3-bucket-acl",
        env="DEBKIT_S3_BUCKET_ACL",
        dest="s3_bucket_acl",
        help="canned ACL for a bucket created by the run; unset sends none",
        type=bucket_acl_type,
    )


def build_parser() -> EnvArgumentParser:
    """Build the root parser with every subcommand.

    Returns:
        EnvArgumentParser: Root parser.
    """
    root_parser = EnvArgumentParser(
        prog="debkit",
        description=f"debkit {__version__}: build, mirror, and verify Debian repositories",
        formatter_class=RawDescriptionHelpFormatter,
    )
    add_common_args(root_parser)
    add_s3_args(root_parser)

    subparsers = root_parser.add_subparsers(
        help="command help", dest="command", required=True, parser_class=EnvArgumentParser
    )

    dump_parser = subparsers.add_parser("dump", help="dump repository indices structure and data")
    add_output_cli_args(dump_parser)

    list_parser = subparsers.add_parser("list", help="list all packages from repository")
    add_output_cli_args(list_parser)

    stats_parser = subparsers.add_parser("stats", help="show repository data statistics")
    add_output_cli_args(stats_parser)

    insert_parser = subparsers.add_parser("insert", help="insert packages to repository")
    ActionPkg.add_insert_cli_args(insert_parser)
    add_output_cli_args(insert_parser)

    remove_parser = subparsers.add_parser("remove", help="remove packages from repository")
    ActionPkg.add_remove_cli_args(remove_parser)
    add_output_cli_args(remove_parser)

    sync_parser = subparsers.add_parser("sync", help="sync data from other repository")
    ActionSync.add_sync_cli_args(sync_parser)
    ActionSync.add_selection_cli_args(sync_parser)
    add_output_cli_args(sync_parser)

    check_parser = subparsers.add_parser("check", help="check data consistency in repository")
    ActionSync.add_check_cli_args(check_parser)
    ActionSync.add_selection_cli_args(check_parser)
    add_output_cli_args(check_parser)

    log_parser = subparsers.add_parser("log", help="show transaction history")
    add_output_cli_args(log_parser)

    revert_parser = subparsers.add_parser("revert", help="revert latest committed transaction")
    ActionHistory.add_dry_run_cli_arg(revert_parser)

    prune_parser = subparsers.add_parser("prune", help="prune old transactions from the history")
    ActionHistory.add_dry_run_cli_arg(prune_parser)
    ActionHistory.add_prune_cli_args(prune_parser)
    add_output_cli_args(prune_parser)

    return root_parser


def parse_cli(argv: list[str] | None = None) -> Namespace:
    """Parse `argv` (or ``sys.argv``) and resolve the ``DEBKIT_*`` fallbacks.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        Namespace: Parsed and resolved options.
    """
    return build_parser().parse_args(argv)
