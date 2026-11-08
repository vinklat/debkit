"""Base class for Debian repo actions."""

import json
import logging
import os
from enum import Enum
from typing import TYPE_CHECKING

from txstore import HistoryRetention, StorageException, make_storage

from debkit.repo import RepoReadException
from debkit.repo.gnupg import GnuPG
from debkit.repo.params import RepoParams
from debkit.repo.repo_rw import RepoRW
from debkit.repo.repo_sync import RepoSync

if TYPE_CHECKING:
    from argparse import Namespace

    from txstore import Storage

    from debkit._options import EnvArgumentParser

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class OutputType(Enum):
    """Output type enum for action output."""

    brief = "brief"
    json = "json"

    def __str__(self) -> str:
        """Return the string value of the enum member."""
        return self.value


OUTPUT_DEFAULT = OutputType.brief


def storage_url(url: str) -> str:
    """Return `url`, or ``fs://`` plus the absolute path when `url` has no scheme.

    → [VALIDATION.md](VALIDATION.md#storage-urls).

    Args:
        url: Storage URL or filesystem path.

    Returns:
        str: A URL `make_storage` accepts.
    """
    if "://" in url:
        return url
    return f"fs://{os.path.abspath(url)}"


def open_storage(pars: "Namespace", url: str, create_path: bool = False) -> "Storage":
    """Build the `Storage` for `url` from the resolved CLI options.

    The only `make_storage` call → [STORAGE.md](STORAGE.md#construction).

    Args:
        pars: Parsed CLI namespace.
        url: Storage URL or filesystem path.
        create_path: Create the destination bucket or directory if missing.

    Returns:
        Storage: A backend instance routed by the URL scheme.
    """
    return make_storage(
        storage_url(url),
        create_path=create_path,
        verify_ssl=not (
            getattr(pars, "skip_verify", False) or getattr(pars, "skip_verify_ssl", False)
        ),
        s3_access=getattr(pars, "s3_access", None),
        s3_secret=getattr(pars, "s3_secret", None),
        s3_default_acl=getattr(pars, "s3_default_acl", None),
        s3_bucket_acl=getattr(pars, "s3_bucket_acl", None),
    )


def history_retention(pars: "Namespace") -> HistoryRetention:
    """Build the retention policy from the resolved ``--history-*`` options.

    Args:
        pars: Parsed CLI namespace.

    Returns:
        HistoryRetention: Policy for `maybe_transaction` and `prune`.
    """
    return HistoryRetention.from_env(
        keep=getattr(pars, "history_keep", None),
        max_age_days=getattr(pars, "history_max_age", None),
    )


def attach_storage_repo(
    pars: "Namespace", url: str | None = None, create_path: bool = False
) -> tuple["Storage", RepoParams]:
    """Build a `Storage` and `RepoParams` pair from a deb-source URL.

    Args:
        pars: Parsed CLI namespace.
        url: Deb-source string. Falls back to ``pars.target_url`` when ``None``.
        create_path: Create the destination bucket/directory if missing.

    Raises:
        RepoReadException: If the URL is invalid or empty.

    Returns:
        Tuple of (storage, repo_params).
    """
    if url is None:
        url = pars.target_url

    verify_gpg = not (pars.skip_verify or pars.skip_verify_gpg)
    verify_digests = not pars.skip_verify

    repo_params = RepoParams(verify_gpg=verify_gpg, verify_digests=verify_digests)
    repo_params.from_deb_source(url)

    if repo_params.url is None:
        raise RepoReadException("Invalid URL.")

    try:
        storage = open_storage(pars, repo_params.url, create_path=create_path)
    except StorageException as exc:
        raise RepoReadException(f"{exc}") from exc
    return storage, repo_params


def attach_storage_plain(
    pars: "Namespace", url: str | None = None, create_path: bool = False
) -> "Storage":
    """Build a `Storage` from a plain (non-deb-source) URL.

    Args:
        pars: Parsed CLI namespace.
        url: Storage URL. Falls back to ``pars.target_url`` when ``None``.
        create_path: Create the destination bucket/directory if missing.

    Raises:
        StorageException: If the URL is missing (``None``) after the fallback.

    Returns:
        Storage: A backend instance routed by the URL scheme.
    """
    if url is None:
        url = pars.target_url
    if url is None:
        raise StorageException("Empty storage URL.")
    return open_storage(pars, url, create_path=create_path)


def attach_storage_root(
    pars: "Namespace", url: str | None = None, create_path: bool = False
) -> "Storage":
    """Build a `Storage` from the URL field of a deb source, ignoring dist/components.

    Root-scoped commands (`log`, `revert`) address the storage root only, so
    they accept both ``<url>`` and ``<url> <dist> [<component> ...]``.

    Args:
        pars: Parsed CLI namespace.
        url: Deb-source or plain storage URL. Falls back to ``pars.target_url``
            when ``None``.
        create_path: Create the destination bucket/directory if missing.

    Raises:
        StorageException: If the URL is missing or blank after the fallback.

    Returns:
        Storage: A backend instance routed by the URL scheme.
    """
    if url is None:
        url = pars.target_url
    if not isinstance(url, str) or not url.strip():
        raise StorageException("Empty storage URL.")
    return attach_storage_plain(pars, url.split()[0], create_path=create_path)


class ActionException(Exception):
    """Exception raised for errors in the Action class.

    Attributes:
        message (str): Explanation of the error.
    """

    def __init__(self, message: str) -> None:
        """Initialize ActionException.

        Args:
            message (str): Human-readable description of the failure.
        """
        super().__init__(f"Action: {message}")


class RepoJSONEncoder(json.JSONEncoder):
    """JSON encoder that serialises objects exposing a ``dump()`` method."""

    def default(self, obj: object) -> object:
        """Return a serialisable representation of *obj*."""
        if hasattr(obj, "dump"):
            return obj.dump()
        return super().default(obj)


def dump_json(obj: object, indent: int = 2) -> str:
    """Serialize *obj* using RepoJSONEncoder so dump()-bearing objects work."""
    return json.dumps(obj, cls=RepoJSONEncoder, indent=indent)


def add_output_cli_args(parser: "EnvArgumentParser") -> None:
    """Register ``--output`` on a subcommand parser.

    Args:
        parser (EnvArgumentParser): The subcommand parser.
    """
    parser.add_env_argument(
        "-o",
        "--output",
        env="DEBKIT_OUTPUT",
        dest="output",
        help="output format",
        choices=list(OutputType),
        default=OUTPUT_DEFAULT,
        type=OutputType,
    )


class Action:
    """Base class for repo actions.

    Attributes:
        source_repo (RepoSync): Source repository.
        target_repo (RepoRW): Target repository.
        history_retention (HistoryRetention): Policy the write paths hand to
            `maybe_transaction`; a `max_age_days` of `0` records without pruning.
    """

    def __init__(self, pars: "Namespace", create_path: bool = False):
        """Initialize repo objects and storages.

        Args:
            pars (Namespace): Parsed command line arguments.
            create_path (bool): Create path or bucket if True.
        """
        self.source_repo: RepoSync | None = None
        self.gpg: GnuPG | None = None
        self.history_retention: HistoryRetention = history_retention(pars)

        skip_verify: bool = hasattr(pars, "skip_verify") and pars.skip_verify
        skip_verify_gpg: bool = hasattr(pars, "skip_verify_gpg") and pars.skip_verify_gpg

        # one warning per run, as for `--allow-release-rollback`
        # -> [CRYPTO.md](CRYPTO.md#verification-switches)
        if skip_verify:
            logger.warning(
                "GPG and digest verification skipped: --skip-verify is set; "
                "Release files are neither verified nor signed"
            )
        elif skip_verify_gpg:
            logger.warning(
                "GPG verification skipped: --skip-verify-gpg is set; "
                "Release files are neither verified nor signed"
            )
        else:
            self.gpg = GnuPG(pars.gnupg_home, pars.gnupg_keyring, pars.gnupg_passphrase)

        if hasattr(pars, "source_url"):
            logger.debug("Prepare source URL: %s", pars.source_url)
            (source_storage, source_repo_params) = attach_storage_repo(pars, url=pars.source_url)
            self.source_repo = RepoSync(source_storage, source_repo_params, gpg=self.gpg)

        # `--target-url` is required, so every action has a target
        logger.debug("Prepare target URL: %s", pars.target_url)
        (target_storage, target_repo_params) = attach_storage_repo(
            pars, url=pars.target_url, create_path=create_path
        )
        # With a source repository the target is overwritten, so its GnuPG check is skipped.
        self.target_repo: RepoRW = RepoRW(
            target_storage,
            target_repo_params,
            gpg=self.gpg if self.source_repo is None else None,
        )
