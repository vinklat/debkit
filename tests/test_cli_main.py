"""Coverage for `debkit.cli.main()` dispatch and error mapping.

See `TESTING.md` § 0 (CLI Main Loop) for the matrix this file implements.

Tests inject parse results by monkeypatching `debkit.cli.parse_cli`
and action classes in the same module namespace rather than fighting
sys.argv at runtime.
"""

from __future__ import annotations

import logging
from argparse import Namespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from txstore import StorageException

import debkit.cli as cli
from debkit.action import ActionException
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
from debkit.repo.repo_rw import RepoWriteException
from debkit.repo.repo_sync import RepoSyncException


def _install_prelude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise side-effectful prelude (logging config, urllib3 warnings)."""
    monkeypatch.setattr(cli, "configure_logging", MagicMock())
    monkeypatch.setattr(cli, "urllib3", MagicMock())


def _install_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Replace action classes in ``cli`` with MagicMock classes."""
    mock_pkg = MagicMock(name="ActionPkg")
    mock_sync = MagicMock(name="ActionSync")
    mock_history = MagicMock(name="ActionHistory")
    monkeypatch.setattr(cli, "ActionPkg", mock_pkg)
    monkeypatch.setattr(cli, "ActionSync", mock_sync)
    monkeypatch.setattr(cli, "ActionHistory", mock_history)
    return mock_pkg, mock_sync, mock_history


def _install_pars(monkeypatch: pytest.MonkeyPatch, pars: Namespace) -> None:
    """Override ``parse_cli`` so ``main()`` uses the given Namespace."""
    monkeypatch.setattr(cli, "parse_cli", lambda argv=None: pars)


# --- Dispatch (happy paths) ---


@pytest.mark.parametrize(
    ("command", "cls", "method", "expects_create_path"),
    [
        ("list", "pkg", "list", False),
        ("dump", "pkg", "dump", False),
        ("stats", "pkg", "stats", False),
        ("insert", "pkg", "insert", True),
        ("remove", "pkg", "remove", True),
        ("sync", "sync", "sync", True),
        ("log", "history", "log", False),
        ("revert", "history", "revert", False),
        ("prune", "history", "prune", False),
    ],
)
def test_main_dispatches_command_to_action(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
    command: str,
    cls: str,
    method: str,
    expects_create_path: bool,
) -> None:
    _install_prelude(monkeypatch)
    mock_pkg, mock_sync, mock_history = _install_actions(monkeypatch)
    fake_pars.command = command
    _install_pars(monkeypatch, fake_pars)

    cli.main()

    selected = {"pkg": mock_pkg, "sync": mock_sync, "history": mock_history}[cls]
    others = [
        m
        for name, m in (("pkg", mock_pkg), ("sync", mock_sync), ("history", mock_history))
        if name != cls
    ]

    if expects_create_path:
        selected.assert_called_once_with(fake_pars, create_path=True)
    else:
        selected.assert_called_once_with(fake_pars)

    getattr(selected.return_value, method).assert_called_once_with()
    for other in others:
        other.assert_not_called()


def test_main_check_consistent_does_not_exit(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
) -> None:
    _install_prelude(monkeypatch)
    _, mock_sync, _ = _install_actions(monkeypatch)
    mock_sync.return_value.check.return_value = True
    fake_pars.command = "check"
    _install_pars(monkeypatch, fake_pars)

    cli.main()

    mock_sync.assert_called_once_with(fake_pars)
    mock_sync.return_value.check.assert_called_once_with()


def test_main_check_inconsistent_exits(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_prelude(monkeypatch)
    _, mock_sync, _ = _install_actions(monkeypatch)
    mock_sync.return_value.check.return_value = False
    fake_pars.command = "check"
    _install_pars(monkeypatch, fake_pars)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "Repository is not consistent." in caplog.text


def test_main_unknown_command_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
) -> None:
    _install_prelude(monkeypatch)
    mock_pkg, mock_sync, mock_history = _install_actions(monkeypatch)
    fake_pars.command = "definitely-not-a-command"
    _install_pars(monkeypatch, fake_pars)

    cli.main()

    mock_pkg.assert_not_called()
    mock_sync.assert_not_called()
    mock_history.assert_not_called()


def test_main_maps_storage_url_error_to_read_exit(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_prelude(monkeypatch)
    fake_pars.command = "list"
    fake_pars.target_url = "http:///no-host bookworm main"
    _install_pars(monkeypatch, fake_pars)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "Invalid HTTP URL" in caplog.text
    assert "Can't read repository." in caplog.text


# --- Exception groups ---


@pytest.mark.parametrize(
    ("exc", "exit_message", "expected_warning"),
    [
        (RepoReadException("x"), "Can't read repository.", None),
        (GPGVerifyException("x"), "Can't read repository.", None),
        (ComponentException("x"), "Can't read repository.", None),
        (DebFileException("x"), "Can't read repository.", None),
        (PackageException("x"), "Can't read repository.", None),
        (PackagesException("x"), "Can't read repository.", None),
        (ReleaseReadException("x"), "Can't read repository.", None),
        (ChecksumException("x"), "Distrustful object.", None),
        (ReleaseFreshnessException("x"), "Distrustful object.", None),
        (
            RepoWriteException("x"),
            "Can't write repository.",
            "Some data may have been left unfinished.",
        ),
        (
            ReleaseWriteException("x"),
            "Can't write repository.",
            "Some data may have been left unfinished.",
        ),
        (
            ChangesException("x"),
            "Can't write repository.",
            "Some data may have been left unfinished.",
        ),
        (
            RepoSyncException("x"),
            "Sync failed.",
            "Some data may have remained incomplete.",
        ),
    ],
)
def test_main_exception_group_maps_to_exit(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
    exit_message: str,
    expected_warning: str | None,
) -> None:
    _install_prelude(monkeypatch)
    mock_pkg, _, _ = _install_actions(monkeypatch)
    mock_pkg.return_value.list.side_effect = exc
    fake_pars.command = "list"
    _install_pars(monkeypatch, fake_pars)

    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert exit_message in caplog.text

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    if expected_warning is None:
        assert warning_records == []
    else:
        assert any(expected_warning in r.getMessage() for r in warning_records)


@pytest.mark.parametrize(
    "exc",
    [
        ActionException("boom-action"),
        RepoParamsException("boom-params"),
        ValueError("boom-value"),
        StorageException("boom-storage"),
    ],
)
def test_main_generic_group_exits_with_original_message(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    _install_prelude(monkeypatch)
    mock_pkg, _, _ = _install_actions(monkeypatch)
    mock_pkg.return_value.list.side_effect = exc
    fake_pars.command = "list"
    _install_pars(monkeypatch, fake_pars)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    # The Generic group passes `exc` itself to err_exit (no canned rewrite); the
    # original payload string therefore appears in the critical log.
    canned_messages = {
        "Can't read repository.",
        "Can't write repository.",
        "Distrustful object.",
        "Sync failed.",
        "Repository is not consistent.",
    }
    for canned in canned_messages:
        assert canned not in caplog.text, f"unexpected canned rewrite: {canned}"


def _payload_for(exc: Any) -> str:
    """Best-effort recovery of the original payload string passed at construction."""
    text = str(exc)
    # Strip known layer prefixes added by exception __init__.
    for prefix in ("Repo parameters: ", "Action: ", "Storage: "):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("boom-value"),
        StorageException("boom-storage"),
        RepoParamsException("boom-params"),
    ],
)
def test_main_generic_group_logs_original_payload(
    monkeypatch: pytest.MonkeyPatch,
    fake_pars: Namespace,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    _install_prelude(monkeypatch)
    mock_pkg, _, _ = _install_actions(monkeypatch)
    mock_pkg.return_value.list.side_effect = exc
    fake_pars.command = "list"
    _install_pars(monkeypatch, fake_pars)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert _payload_for(exc) in caplog.text
