r"""Command-line entry point: parse options, configure logging, dispatch, map errors to exit 1.

     _      _     _    _ _
  __| | ___| |__ | | _(_) |_
 / _` |/ _ \ '_ \| |/ / | __|
| (_| |  __/ |_) |   <| | |_
 \__,_|\___|_.__/|_|\_\_|\__|
"""

from __future__ import annotations

import logging
import sys

import urllib3
from txstore import StorageException

from debkit import __version__
from debkit._logging import configure_logging
from debkit.action import ActionException, verify_policy
from debkit.action.action_history import ActionHistory
from debkit.action.action_pkg import ActionPkg
from debkit.action.action_sync import ActionSync
from debkit.argparser import parse_cli
from debkit.repo import RepoReadException
from debkit.repo.changes import ChangesException
from debkit.repo.component import ComponentException
from debkit.repo.debfile import DebFileException
from debkit.repo.gnupg import GPGVerifyException
from debkit.repo.hashes import ChecksumException
from debkit.repo.package import PackageException
from debkit.repo.packages import PackagesException
from debkit.repo.params import RepoParamsException
from debkit.repo.release import (
    ReleaseFreshnessException,
    ReleaseReadException,
    ReleaseWriteException,
)
from debkit.repo.repo_sync import RepoSyncException

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def err_exit(message: object) -> None:
    """Log a critical message and exit with status 1.

    Args:
        message: Error message; coerced to ``str`` for logging.
    """
    logger.critical(message)
    sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    """Run one debkit command.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Raises:
        SystemExit: Status 2 on a usage error, 1 on a mapped failure
            → [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups).
    """
    pars = parse_cli(argv)
    configure_logging(pars.log_level, pars.log_format)
    if not verify_policy(pars).verify_ssl:
        # txstore logs one warning per storage; urllib3 would add one per request.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    logger.debug("debkit version: %s", __version__)

    try:
        match pars.command:
            case "list":
                ActionPkg(pars).list_packages()
            case "dump":
                ActionPkg(pars).dump()
            case "stats":
                ActionPkg(pars).stats()
            case "insert":
                ActionPkg(pars, create_path=True).insert()
            case "remove":
                ActionPkg(pars, create_path=True).remove()
            case "sync":
                ActionSync(pars, create_path=True).sync()
            case "check":
                if not ActionSync(pars).check():
                    err_exit("Repository is not consistent.")
            case "log":
                ActionHistory(pars).log()
            case "revert":
                ActionHistory(pars).revert()
            case "prune":
                ActionHistory(pars).prune()

    except (
        RepoReadException,
        GPGVerifyException,
        ComponentException,
        ChangesException,
        DebFileException,
        PackageException,
        PackagesException,
        ReleaseReadException,
    ) as exc:
        logger.critical(exc)
        err_exit("Can't read repository.")

    except (ChecksumException, ReleaseFreshnessException) as exc:
        logger.critical(exc)
        err_exit("Distrustful object.")

    except ReleaseWriteException as exc:
        logger.critical(exc)
        logger.warning("Some data may have been left unfinished.")
        err_exit("Can't write repository.")

    except RepoSyncException as exc:
        logger.critical(exc)
        logger.warning("Some data may have remained incomplete.")
        err_exit("Sync failed.")

    except (
        ActionException,
        RepoParamsException,
        ValueError,
        StorageException,
    ) as exc:
        err_exit(exc)
