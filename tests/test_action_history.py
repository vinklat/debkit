"""Tests for `ActionHistory` (action/action_history.py)."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from helpers.factories import make_action_namespace
from helpers.storage import commit_transaction, footmarks_of, seed_history, transaction_record
from txstore import (
    HISTORY_KEEP_DEFAULT,
    HISTORY_MAX_AGE_DAYS_DEFAULT,
    StorageException,
    StorageFS,
    Timestamp,
    TransactionHistory,
    TransactionModel,
)

from debkit.action import Action, ActionException, OutputType
from debkit.action.action_history import ActionHistory

# History file at the storage root, fixed by txstore's storage contract.
HISTORY_FILENAME = ".transactions.json"
# Reference time far past every test transaction, so each is older than any horizon.
LATER = datetime.now(UTC) + timedelta(days=400)


def _leave_orphan(repo_dir: Path, path: str) -> None:
    """Stage a write on a second instance and never close it, as a crashed writer would."""
    crashed = StorageFS(str(repo_dir))
    crashed.transaction_begin(99)
    crashed.put(path, b"lost")


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


@pytest.fixture
def storage(repo_dir: Path) -> StorageFS:
    return StorageFS(str(repo_dir))


@pytest.fixture
def pars(repo_dir: Path) -> Namespace:
    return make_action_namespace(repo_dir, output=OutputType.brief, dry_run=False)


def test_action_history_builds_no_repository() -> None:
    """History commands address the storage root only, so they skip the repo setup of `Action`."""
    assert not issubclass(ActionHistory, Action)


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #


def test_log_empty(pars: Namespace, capsys: pytest.CaptureFixture[str]) -> None:
    pars.output = OutputType.brief
    ActionHistory(pars).log()
    assert "No transactions recorded" in capsys.readouterr().out


def test_log_accepts_bare_target_url(
    pars: Namespace, storage: StorageFS, repo_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`log` and `revert` are root-scoped: no dist/components fields required."""
    seed_history(storage, [{"id": 1, "timestamp": "2026-06-08T10:00:00Z"}])
    pars.target_url = f"fs://{repo_dir}"
    pars.output = OutputType.brief

    ActionHistory(pars).log()

    out = capsys.readouterr().out
    assert "1" in out
    assert "2026-06-08" in out


def test_log_brief(pars: Namespace, storage: StorageFS, capsys: pytest.CaptureFixture[str]) -> None:
    seed_history(storage, [{"id": 1, "timestamp": "2026-06-08T10:00:00Z"}])
    pars.output = OutputType.brief
    ActionHistory(pars).log()
    out = capsys.readouterr().out
    assert "1" in out
    assert "2026-06-08" in out


def test_log_rejects_ansi_injection(pars: Namespace, repo_dir: Path) -> None:
    poisoned = "2026-06-08T10:00:00Z\n     99999  \x1b[2K\rSAFE"
    # Written beside txstore: its write paths refuse the reserved history name.
    (repo_dir / HISTORY_FILENAME).write_text(json.dumps([{"id": 1, "timestamp": poisoned}]))
    with pytest.raises(ActionException, match="malformed transaction timestamp"):
        ActionHistory(pars).log()


def test_log_json(pars: Namespace, storage: StorageFS, capsys: pytest.CaptureFixture[str]) -> None:
    records = [{"id": 1, "timestamp": "2026-06-08T10:00:00Z"}]
    seed_history(storage, records)
    pars.output = OutputType.json
    ActionHistory(pars).log()
    out = capsys.readouterr().out
    assert json.loads(out) == records


# --------------------------------------------------------------------------- #
# revert
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dry_run", [False, True])
def test_revert_empty_raises(pars: Namespace, dry_run: bool) -> None:
    pars.dry_run = dry_run
    with pytest.raises(ActionException, match="No transactions to revert"):
        ActionHistory(pars).revert()


def test_revert_dry_run(
    pars: Namespace, storage: StorageFS, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_history(storage, [{"id": 3, "timestamp": "2026-06-08T10:00:00Z"}])
    pars.dry_run = True
    ActionHistory(pars).revert()
    out = capsys.readouterr().out
    assert "Would revert" in out
    assert "3" in out
    # History unchanged
    records = json.loads(storage.get(HISTORY_FILENAME))
    assert len(records) == 1


def test_revert_calls_transaction_revert(pars: Namespace, storage: StorageFS) -> None:
    seed_history(storage, [{"id": 2, "timestamp": "2026-06-08T10:00:00Z"}])
    pars.dry_run = False

    expected = TransactionModel(2, Timestamp.from_json("2026-06-08T10:00:00Z")).footmarks

    with patch.object(StorageFS, "transaction_revert") as mock_revert:
        ActionHistory(pars).revert()
        mock_revert.assert_called_once()
        model = mock_revert.call_args[0][0]
        assert model.id == 2
        assert model.footmarks == expected

    # Entry removed from history
    assert json.loads(storage.get(HISTORY_FILENAME)) == []


# --------------------------------------------------------------------------- #
# Non-transactional backend guard
# --------------------------------------------------------------------------- #


def test_log_non_transactional_raises(pars: Namespace) -> None:
    pars.target_url = "http://example.com/repo bookworm main"
    with pytest.raises(ActionException, match="does not support transactions"):
        ActionHistory(pars).log()


def test_revert_non_transactional_raises(pars: Namespace) -> None:
    pars.target_url = "http://example.com/repo bookworm main"
    with pytest.raises(ActionException, match="does not support transactions"):
        ActionHistory(pars).revert()


# --------------------------------------------------------------------------- #
# prune
# --------------------------------------------------------------------------- #


def test_prune_reports_nothing_to_prune(
    pars: Namespace, capsys: pytest.CaptureFixture[str]
) -> None:
    pars.history_max_age = 90
    ActionHistory(pars).prune()
    assert "Nothing to prune" in capsys.readouterr().out


def test_prune_removes_expired_records(
    pars: Namespace, storage: StorageFS, capsys: pytest.CaptureFixture[str]
) -> None:
    old_model = commit_transaction(storage, "old.deb")
    new_model = commit_transaction(storage, "new.deb")
    seed_history(storage, [transaction_record(old_model), transaction_record(new_model)])
    # Age every stored transaction by pruning with a one-day horizon and a
    # keep floor of one, so only the newest record survives.
    pars.history_keep = 1
    pars.history_max_age = 1

    ActionHistory(pars).prune(now=LATER)

    out = capsys.readouterr().out
    assert f"Removed transaction {old_model.id}" in out
    assert f"transaction {new_model.id}" not in out
    assert [rec.id for rec in TransactionHistory(storage).load()] == [new_model.id]


def test_prune_dry_run_changes_nothing(
    pars: Namespace, storage: StorageFS, capsys: pytest.CaptureFixture[str]
) -> None:
    old_model = commit_transaction(storage, "old.deb")
    new_model = commit_transaction(storage, "new.deb")
    seed_history(storage, [transaction_record(old_model), transaction_record(new_model)])
    pars.history_keep = 1
    pars.history_max_age = 1
    pars.dry_run = True

    ActionHistory(pars).prune(now=LATER)

    assert "Would remove transaction" in capsys.readouterr().out
    assert len(TransactionHistory(storage).load()) == 2


def test_prune_sweeps_orphans_only_when_asked(
    pars: Namespace, storage: StorageFS, repo_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded = commit_transaction(storage, "kept.deb")
    seed_history(storage, [transaction_record(recorded)])
    _leave_orphan(repo_dir, "ghost.deb")
    pars.history_max_age = 90

    ActionHistory(pars).prune()
    assert "Nothing to prune" in capsys.readouterr().out

    pars.orphans = True
    ActionHistory(pars).prune()
    assert "Removed orphaned footmark" in capsys.readouterr().out


def test_prune_json_output(
    pars: Namespace, storage: StorageFS, repo_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded = commit_transaction(storage, "kept.deb")
    seed_history(storage, [transaction_record(recorded)])
    _leave_orphan(repo_dir, "ghost.deb")
    pars.output = OutputType.json
    pars.orphans = True
    pars.history_max_age = 90

    ActionHistory(pars).prune()

    payload = json.loads(capsys.readouterr().out)
    assert payload["transactions"] == []
    assert payload["footmarks"] == []
    assert payload["orphans"]


def test_prune_brief_output_lists_reclaimed_footmarks(
    pars: Namespace, storage: StorageFS, capsys: pytest.CaptureFixture[str]
) -> None:
    old_model = commit_transaction(storage, "old.deb")
    new_model = commit_transaction(storage, "new.deb")
    seed_history(storage, [transaction_record(old_model), transaction_record(new_model)])
    pars.history_keep = 1
    pars.history_max_age = 1

    ActionHistory(pars).prune(now=LATER)

    lines = [line for line in capsys.readouterr().out.splitlines() if "Removed footmark " in line]
    assert lines
    assert footmarks_of(lines, old_model) == sorted(lines)


def test_prune_defaults_when_the_namespace_lacks_the_flags(
    pars: Namespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retention falls back to the built-in defaults, not to a crash."""
    action = ActionHistory(pars)
    assert (action.history_retention.keep, action.history_retention.max_age_days) == (
        HISTORY_KEEP_DEFAULT,
        HISTORY_MAX_AGE_DAYS_DEFAULT,
    )
    action.prune()
    assert "Nothing to prune" in capsys.readouterr().out


def test_prune_rejects_a_non_transactional_backend(pars: Namespace) -> None:
    with patch("debkit.action.action_history.is_transactional", return_value=False):
        with pytest.raises(ActionException, match="does not support transactions"):
            ActionHistory(pars).prune()


def test_prune_wraps_storage_failures(pars: Namespace) -> None:
    with patch.object(
        TransactionHistory, "prune", side_effect=StorageException("listing unavailable")
    ):
        with pytest.raises(ActionException, match="listing unavailable"):
            ActionHistory(pars).prune()
