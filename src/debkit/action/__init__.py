"""Base class for Debian repo actions."""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import TYPE_CHECKING, NamedTuple

from txstore import HistoryRetention, StorageException, make_storage, redact_url

from debkit.repo import RepoReadException
from debkit.repo.gnupg import GnuPG
from debkit.repo.params import RepoParams, RepoParamsException
from debkit.repo.repo_rw import RepoRW

if TYPE_CHECKING:
    from argparse import Namespace

    from txstore import Storage, StorageMetrics

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
        A URL `make_storage` accepts.
    """
    if "://" in url:
        return url
    return f"fs://{os.path.abspath(url)}"


def open_storage(pars: Namespace, url: str, create_path: bool = False) -> Storage:
    """Build the `Storage` for `url` from the resolved CLI options.

    The only `make_storage` call → [STORAGE.md](STORAGE.md#construction).

    Args:
        pars: Parsed CLI namespace.
        url: Storage URL or filesystem path.
        create_path: Create the destination bucket or directory if missing.

    Raises:
        RepoParamsException: txstore rejects the URL → [VALIDATION.md](VALIDATION.md#storage-urls).

    Returns:
        A backend instance routed by the URL scheme.
    """
    try:
        return make_storage(
            storage_url(url),
            create_path=create_path,
            verify_ssl=verify_policy(pars).verify_ssl,
            s3_access=pars.s3_access,
            s3_secret=pars.s3_secret,
            s3_default_acl=pars.s3_default_acl,
            s3_bucket_acl=pars.s3_bucket_acl,
        )
    except ValueError as exc:
        # the URL may carry credentials -> [CONFIG.md](CONFIG.md#secrets)
        raise RepoParamsException(f"{exc}") from exc


def history_retention(pars: Namespace) -> HistoryRetention:
    """Build the retention policy from the resolved ``--history-*`` options.

    Args:
        pars: Parsed CLI namespace.

    Returns:
        Policy for `maybe_transaction` and `prune`.
    """
    return HistoryRetention.from_env(keep=pars.history_keep, max_age_days=pars.history_max_age)


class VerifyPolicy(NamedTuple):
    """The checks the resolved ``--skip-verify*`` switches leave on.

    → [CRYPTO.md](CRYPTO.md#verification-switches).
    """

    verify_ssl: bool
    verify_gpg: bool
    verify_digests: bool


def verify_policy(pars: Namespace) -> VerifyPolicy:
    """Resolve ``--skip-verify``, ``--skip-verify-ssl``, and ``--skip-verify-gpg``.

    Args:
        pars: Parsed CLI namespace.

    Returns:
        Which of TLS, GPG, and digest verification stay on.
    """
    return VerifyPolicy(
        verify_ssl=not (pars.skip_verify or pars.skip_verify_ssl),
        verify_gpg=not (pars.skip_verify or pars.skip_verify_gpg),
        verify_digests=not pars.skip_verify,
    )


def attach_storage_repo(
    pars: Namespace,
    url: str,
    create_path: bool = False,
    *,
    archs: list[str] | None = None,
    indices_exclude: list[str] | None = None,
) -> tuple[Storage, RepoParams]:
    """Build a `Storage` and `RepoParams` pair from a deb-source URL.

    Args:
        pars: Parsed CLI namespace.
        url: Deb-source string.
        create_path: Create the destination bucket/directory if missing.
        archs: The `--architectures` selection; None keeps the Release's.
        indices_exclude: The `--indices-exclude` globs; None mirrors every listed index.

    Raises:
        RepoParamsException: If the deb source or its URL is invalid.
        RepoReadException: If the storage cannot be constructed.

    Returns:
        Tuple of (storage, repo_params).
    """
    policy = verify_policy(pars)
    repo_params = RepoParams(
        archs=archs,
        indices_exclude=indices_exclude,
        verify_gpg=policy.verify_gpg,
        verify_digests=policy.verify_digests,
    )
    repo_params.from_deb_source(url)

    # `from_deb_source` sets the URL or raises
    assert repo_params.url is not None

    try:
        storage = open_storage(pars, repo_params.url, create_path=create_path)
    except StorageException as exc:
        raise RepoReadException(f"{exc}") from exc
    return storage, repo_params


def attach_storage_plain(pars: Namespace, url: str, create_path: bool = False) -> Storage:
    """Build a `Storage` from a plain (non-deb-source) URL.

    Args:
        pars: Parsed CLI namespace.
        url: Storage URL.
        create_path: Create the destination bucket/directory if missing.

    Returns:
        A backend instance routed by the URL scheme.
    """
    return open_storage(pars, url, create_path=create_path)


def attach_storage_root(pars: Namespace, url: str, create_path: bool = False) -> Storage:
    """Build a `Storage` from the URL field of a deb source, ignoring dist/components.

    Root-scoped commands (`log`, `revert`) address the storage root only, so
    they accept both ``<url>`` and ``<url> <dist> [<component> ...]``.

    Args:
        pars: Parsed CLI namespace.
        url: Deb-source or plain storage URL.
        create_path: Create the destination bucket/directory if missing.

    Raises:
        StorageException: If the URL is blank.

    Returns:
        A backend instance routed by the URL scheme.
    """
    if not url.strip():
        raise StorageException("Empty storage URL.")
    return attach_storage_plain(pars, url.split()[0], create_path=create_path)


def open_target_repo(
    pars: Namespace,
    *,
    gpg: GnuPG | None,
    create_path: bool = False,
    archs: list[str] | None = None,
    indices_exclude: list[str] | None = None,
) -> RepoRW:
    """Open the `--target-url` repository with its selection.

    Args:
        pars: Parsed CLI namespace.
        gpg: Verifies and signs its Release; None reads it unverified and never signs it.
        create_path: Create the target path or bucket if missing.
        archs: The `--architectures` selection; None keeps the Release's.
        indices_exclude: The `--indices-exclude` globs; None excludes nothing.

    Raises:
        RepoParamsException: If the deb source or its URL is invalid.
        RepoReadException: If the storage cannot be constructed.

    Returns:
        The target repository, not yet loaded.
    """
    # `--target-url` is required, so every action has a target
    logger.debug("Prepare target URL: %s", redact_url(pars.target_url))
    storage, params = attach_storage_repo(
        pars,
        pars.target_url,
        create_path=create_path,
        archs=archs,
        indices_exclude=indices_exclude,
    )
    return RepoRW(storage, params, gpg=gpg)


class ActionException(Exception):
    """Exception raised for errors in the Action class."""

    def __init__(self, message: str) -> None:
        """Initialize ActionException.

        Args:
            message: Human-readable description of the failure.
        """
        super().__init__(f"Action: {message}")


class RepoJSONEncoder(json.JSONEncoder):
    """JSON encoder that serialises objects exposing a ``dump()`` method."""

    def default(self, obj: object) -> object:
        """Return a serialisable representation of *obj*."""
        if hasattr(obj, "dump"):
            return obj.dump()
        return super().default(obj)


def dump_json(obj: object) -> str:
    """Serialize *obj* via RepoJSONEncoder, two-space indented, so dump()-bearing objects work."""
    return json.dumps(obj, cls=RepoJSONEncoder, indent=2)


def add_output_cli_args(parser: EnvArgumentParser) -> None:
    """Register ``--output`` on a subcommand parser.

    Args:
        parser: The subcommand parser.
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
        output: Output format.
        gpg: Verifies and signs Release files; None when GPG verification is off.
        target_repo: Target repository.
        history_retention: Policy the write paths hand to `maybe_transaction`; a `max_age_days` of
            `0` records without pruning.
    """

    target_repo: RepoRW

    def __init__(self, pars: Namespace):
        """Read the output, verification, and history settings every action shares.

        No storage is opened here: each subcommand opens its repositories after this
        returns, through `open_target_repo` or `attach_storage_repo`, so it can refuse its
        own flags first and set every attribute before a repository exists.

        Args:
            pars: Parsed command line arguments.
        """
        # every action subcommand registers `--output` -> [CONFIG.md](CONFIG.md#global-flags)
        self.output: OutputType = pars.output
        self.gpg: GnuPG | None = None
        self.history_retention: HistoryRetention = history_retention(pars)

        policy = verify_policy(pars)
        # one warning per run, as for `--allow-release-rollback`
        # -> [CRYPTO.md](CRYPTO.md#verification-switches)
        if not policy.verify_digests:
            logger.warning(
                "GPG and digest verification skipped: --skip-verify is set; "
                "Release files are neither verified nor signed"
            )
        elif not policy.verify_gpg:
            logger.warning(
                "GPG verification skipped: --skip-verify-gpg is set; "
                "Release files are neither verified nor signed"
            )
        else:
            self.gpg = GnuPG(
                pars.gnupg_home, pars.gnupg_keyring, pars.gnupg_passphrase, pars.gnupg_sign_key
            )

    @property
    def _target_metrics(self) -> StorageMetrics:
        """The read and write counters of the target storage, which the reports print.

        Returns:
            The target storage's metrics.
        """
        return self.target_repo.storage.metrics
