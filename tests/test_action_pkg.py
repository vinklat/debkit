"""Unit tests for ActionPkg transaction lifecycle (insert / remove)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from helpers.factories import make_package, make_params
from helpers.storage import TxnStorageStub, iso_timestamp, seed_history
from txstore import HistoryRetention, StorageFS, StorageNull, TransactionHistory

from debkit.action import OutputType
from debkit.action.action_pkg import ActionPkg
from debkit.repo.release import ReleaseNotFoundException, ReleaseReadException
from debkit.repo.repo_rw import RepoRW

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pkg_action(*, storage: object = None) -> ActionPkg:
    """Build a minimally-patched ActionPkg instance (bypasses __init__).

    Args:
        storage: The storage object attached to the target repo.  Pass a
            ``TxnStorageStub`` for transactional tests, txstore's ``StorageNull``
            for a backend that refuses transactions, or ``None`` to get a plain
            ``MagicMock``.
    """
    action = ActionPkg.__new__(ActionPkg)
    action.output = OutputType.brief
    action.pkg_pattern = None
    action.changes_filename = None
    action.changes_sha256 = None
    action.deb_filename = "hello_1.0_amd64.deb"
    action.component = "main"
    action.incoming_storage = MagicMock()
    action.history_retention = HistoryRetention(max_age_days=0)

    target = MagicMock()
    target.storage = storage if storage is not None else MagicMock()
    target.get_archs.return_value = ["amd64"]
    target.params.is_flat = False
    target.transfer_from.return_value = {"SHA256": "abc", "Size": "100"}
    target.diff.return_value = MagicMock(
        is_empty=MagicMock(return_value=False),
        count_pkgs_to_copy=MagicMock(return_value=1),
        count_pkgs_to_remove=MagicMock(return_value=0),
    )
    action.target_repo = target

    return action


def _target(action: ActionPkg) -> MagicMock:
    """Return the mocked target repo that `_make_pkg_action` attached."""
    return cast(MagicMock, action.target_repo)


def _debfile_mock(mock_debfile_cls: MagicMock, *, arch: str = "amd64") -> MagicMock:
    """Wire up a DebFile mock and return it."""
    instance = MagicMock()
    instance.filename_field = "pool/main/h/hello/hello_1.0_amd64.deb"
    pkg = MagicMock()
    pkg.get_arch.return_value = arch
    instance.get_package.return_value = pkg
    mock_debfile_cls.return_value = instance
    return instance


# ---------------------------------------------------------------------------
# Transaction wiring, shared by insert() and remove()
# ---------------------------------------------------------------------------

_OPS = ["insert", "remove"]


def _txn_action(op: str, storage: object, mock_debfile_cls: MagicMock) -> ActionPkg:
    """Build an action set up to run `op`: a .deb to insert, or a pattern to remove."""
    action = _make_pkg_action(storage=storage)
    if op == "insert":
        _debfile_mock(mock_debfile_cls)
    else:
        action.deb_filename = None
        action.pkg_pattern = {"Package": re.compile("hello")}
    return action


@pytest.mark.parametrize("op", _OPS)
@patch("debkit.action.action_pkg.DebFile")
def test_txn_begins_and_commits_on_success(mock_debfile_cls: MagicMock, op: str) -> None:
    txn = TxnStorageStub()
    action = _txn_action(op, txn, mock_debfile_cls)

    getattr(action, op)()

    assert txn.began
    assert txn.committed
    assert not txn.discarded


@pytest.mark.parametrize("op", _OPS)
@patch("debkit.action.action_pkg.DebFile")
def test_txn_discards_on_exception(mock_debfile_cls: MagicMock, op: str) -> None:
    txn = TxnStorageStub()
    action = _txn_action(op, txn, mock_debfile_cls)
    if op == "insert":
        mock_debfile_cls.return_value.get_package.side_effect = RuntimeError("fail")
    else:
        _target(action).remove_pkg.side_effect = RuntimeError("fail")

    with pytest.raises(RuntimeError, match="fail"):
        getattr(action, op)()

    assert txn.began
    assert not txn.committed
    assert txn.discarded


@pytest.mark.parametrize("op", _OPS)
@patch("debkit.action.action_pkg.DebFile")
def test_txn_skipped_on_non_txn_storage(mock_debfile_cls: MagicMock, op: str) -> None:
    """A backend that refuses transactions runs without one.

    `StorageNull` is a txstore `TransactionNotSupportedMixin` backend, whose
    `transaction_begin()` raises `NotImplementedError`.
    """
    action = _txn_action(op, StorageNull(), mock_debfile_cls)

    getattr(action, op)()  # must not raise


@pytest.mark.parametrize("op", _OPS)
@patch("debkit.action.action_pkg.DebFile")
def test_txn_gets_the_retention_policy(mock_debfile_cls: MagicMock, op: str) -> None:
    txn = TxnStorageStub()
    action = _txn_action(op, txn, mock_debfile_cls)
    action.history_retention = HistoryRetention(keep=1, max_age_days=5)

    with patch("debkit.action.action_pkg.maybe_transaction") as wiring:
        getattr(action, op)()

    wiring.assert_called_once_with(txn, action.history_retention)


@patch("debkit.action.action_pkg.DebFile")
def test_insert_prunes_expired_history_on_a_real_storage(
    mock_debfile_cls: MagicMock, tmp_path: Path
) -> None:
    """End to end through the action: a recorded insert expires aged history."""
    storage = StorageFS(str(tmp_path / "repo"))
    seed_history(
        storage,
        [{"id": 1, "timestamp": iso_timestamp(400)}, {"id": 2, "timestamp": iso_timestamp(390)}],
    )
    action = _make_pkg_action(storage=storage)
    _debfile_mock(mock_debfile_cls)
    action.history_retention = HistoryRetention(keep=1, max_age_days=90)

    def transfer(*_args: object, **_kwargs: object) -> dict[str, str]:
        storage.put("pool/main/h/hello/hello_1.0_amd64.deb", b"deb")
        return {"SHA256": "abc", "Size": "100"}

    _target(action).transfer_from.side_effect = transfer

    action.insert()

    assert [rec.id for rec in TransactionHistory(storage).load()] == [3]


# ---------------------------------------------------------------------------
# insert() — an existing repository is never set up again
# ---------------------------------------------------------------------------

_PACKAGES = "dists/bookworm/main/binary-amd64/Packages.gz"


def _amd64_filenames(storage: StorageFS) -> set[str]:
    """Package filenames a fresh read of the `fs` repository finds in `main` amd64."""
    repo = RepoRW(storage, make_params())
    repo.update()
    return set(repo.components["main"].packages["amd64"].packages)


def _insert_into_real_repo(
    tmp_path: Path, mock_debfile_cls: MagicMock, name: str, *, gpg: MagicMock | None = None
) -> ActionPkg:
    """Insert one amd64 deb into an `fs` repository under *tmp_path*, signed only with *gpg*."""
    deb = f"{name}_1.0_amd64.deb"
    incoming = StorageFS(str(tmp_path / "in"))
    incoming.put(deb, name.encode())

    action = _make_pkg_action()
    action.incoming_storage = incoming
    action.deb_filename = deb
    action.target_repo = RepoRW(
        StorageFS(str(tmp_path / "repo")), make_params(verify_gpg=gpg is not None), gpg=gpg
    )
    instance = MagicMock()
    instance.filename_field = f"pool/main/{name[0]}/{name}/{deb}"
    instance.get_package.return_value = make_package(
        instance.filename_field, Package=name, Version="1.0", Architecture="amd64"
    )
    mock_debfile_cls.return_value = instance

    action.insert()
    return action


@patch("debkit.action.action_pkg.DebFile")
def test_insert_into_an_unsigned_repo_keeps_its_packages(
    mock_debfile_cls: MagicMock, tmp_path: Path
) -> None:
    """`--skip-verify-gpg` writes no signature, and a later insert must still read the repo."""
    _insert_into_real_repo(tmp_path, mock_debfile_cls, "foo")
    action = _insert_into_real_repo(tmp_path, mock_debfile_cls, "bar")

    assert _amd64_filenames(_target(action).storage) == {
        "pool/main/f/foo/foo_1.0_amd64.deb",
        "pool/main/b/bar/bar_1.0_amd64.deb",
    }


@patch("debkit.action.action_pkg.DebFile")
def test_insert_refuses_an_unsigned_repo_without_setting_it_up_again(
    mock_debfile_cls: MagicMock, tmp_path: Path
) -> None:
    """Only a missing Release means a new repository; a missing signature is a read failure."""
    first = _insert_into_real_repo(tmp_path, mock_debfile_cls, "foo")
    before = _target(first).storage.get(_PACKAGES)

    with (
        patch.object(RepoRW, "setup") as setup,
        pytest.raises(ReleaseReadException, match="signature file missing") as excinfo,
    ):
        _insert_into_real_repo(tmp_path, mock_debfile_cls, "bar", gpg=MagicMock())

    assert not isinstance(excinfo.value, ReleaseNotFoundException)
    setup.assert_not_called()
    assert _target(first).storage.get(_PACKAGES) == before


# ---------------------------------------------------------------------------
# insert() — read-only Sources index
# ---------------------------------------------------------------------------


@patch("debkit.action.action_pkg.DebFile")
def test_insert_of_an_arch_all_deb_skips_the_source_arch(mock_debfile_cls: MagicMock) -> None:
    """An `Architecture: all` deb fans out over the binary arches only.

    A mirrored `Sources` index is read-only, so inserting into it would raise
    `PackagesException` and surface through the Read exit group on a write path.
    """
    action = _make_pkg_action(storage=TxnStorageStub())
    _target(action).get_archs.return_value = ["amd64", "source"]
    _debfile_mock(mock_debfile_cls, arch="all")

    action.insert()

    inserted = [call.args[1] for call in _target(action).insert_pkg.call_args_list]
    assert inserted == ["amd64"]


# ---------------------------------------------------------------------------
# insert() — each .changes file goes to the arches its own control field names
# ---------------------------------------------------------------------------


def _changes_upload(
    mock_changes_cls: MagicMock, mock_debfile_cls: MagicMock, field: str, files: list[str]
) -> ActionPkg:
    """An `ActionPkg` inserting a mocked `.changes` whose debs carry the arch in their name."""
    action = _make_pkg_action(storage=TxnStorageStub())
    action.deb_filename = None
    action.changes_filename = "hello_1.0_amd64.changes"
    changes = mock_changes_cls.return_value
    changes.get_archs.return_value = field.split()
    changes.names_source_arch.return_value = False
    changes.get_deb_files.return_value = files

    def debfile(_storage: object, filename: str, comp: str) -> MagicMock:
        instance = MagicMock()
        instance.filename_field = f"pool/{comp}/{filename}"
        instance.get_package.return_value = make_package(
            filename, Architecture=filename.removesuffix(".deb").rsplit("_", 1)[1]
        )
        return instance

    mock_debfile_cls.side_effect = debfile
    return action


def _inserted(action: ActionPkg) -> dict[str, list[str]]:
    """Arches each inserted package went to, keyed by its file name."""
    ret: dict[str, list[str]] = {}
    for call in _target(action).insert_pkg.call_args_list:
        ret.setdefault(call.args[2].fields["Filename"], []).append(call.args[1])
    return ret


@pytest.mark.parametrize("field", ["all amd64", "amd64 all"])
@pytest.mark.parametrize(
    "files",
    [["a_1_amd64.deb", "b_1_all.deb"], ["b_1_all.deb", "a_1_amd64.deb"]],
    ids=["arch-first", "all-first"],
)
@patch("debkit.action.action_pkg.DebFile")
@patch("debkit.action.action_pkg.Changes")
def test_insert_of_a_changes_targets_each_file_by_its_own_arch(
    mock_changes_cls: MagicMock, mock_debfile_cls: MagicMock, field: str, files: list[str]
) -> None:
    """Neither the `Architecture` field order nor the file order picks a package's arches."""
    action = _changes_upload(mock_changes_cls, mock_debfile_cls, field, files)
    _target(action).get_archs.return_value = ["amd64", "arm64", "source"]

    action.insert()

    assert _inserted(action) == {"a_1_amd64.deb": ["amd64"], "b_1_all.deb": ["amd64", "arm64"]}


@pytest.mark.parametrize(
    "repo_archs", [["source"], [], [""]], ids=["source-only", "none", "empty-arch"]
)
@patch("debkit.action.action_pkg.DebFile")
@patch("debkit.action.action_pkg.Changes")
def test_insert_refuses_an_all_package_when_the_target_has_no_binary_arch(
    mock_changes_cls: MagicMock, mock_debfile_cls: MagicMock, repo_archs: list[str]
) -> None:
    from debkit.action import ActionException

    action = _changes_upload(mock_changes_cls, mock_debfile_cls, "all", ["b_1_all.deb"])
    _target(action).get_archs.return_value = repo_archs

    with pytest.raises(ActionException, match="lists no binary architecture"):
        action.insert()

    _target(action).transfer_from.assert_not_called()
    _target(action).insert_pkg.assert_not_called()


# ---------------------------------------------------------------------------
# insert() — a control field naming the source architecture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("architecture", ["source amd64", "amd64 source"])
@patch("debkit.action.action_pkg.Changes")
def test_insert_refuses_a_changes_naming_the_source_arch_in_any_position(
    mock_changes_cls: MagicMock, architecture: str
) -> None:
    """`dpkg-genchanges` writes every built arch into one field, so reading the first
    token alone let the field order decide whether the upload was refused.
    """
    from debkit.action import ActionException

    action = _make_pkg_action(storage=TxnStorageStub())
    action.deb_filename = None
    action.changes_filename = "hello_1.0_amd64.changes"

    changes = mock_changes_cls.return_value
    tokens = architecture.split()
    changes.get_archs.return_value = tokens
    changes.names_source_arch.return_value = "source" in tokens
    changes.get_deb_files.return_value = ["hello_1.0_amd64.deb"]

    with pytest.raises(ActionException, match="Source packages are not supported"):
        action.insert()

    assert _target(action).insert_pkg.call_args_list == []


@patch("debkit.action.action_pkg.DebFile")
def test_insert_refuses_a_deb_whose_control_names_the_source_arch(
    mock_debfile_cls: MagicMock,
) -> None:
    """The control field is read straight out of the uploaded .deb, so it can overwrite
    the binary set `_binary_archs` picked and reach the read-only source index.
    """
    from debkit.action import ActionException

    txn = TxnStorageStub()
    action = _make_pkg_action(storage=txn)
    _debfile_mock(mock_debfile_cls, arch="source")

    with pytest.raises(ActionException, match="Source packages are not supported"):
        action.insert()

    assert txn.discarded
    # VALIDATION.md#sources-stanzas refuses it before the transfer, not after
    _target(action).transfer_from.assert_not_called()
    _target(action).insert_pkg.assert_not_called()


# ---------------------------------------------------------------------------
# insert() — an architecture token that is not an architecture name
# ---------------------------------------------------------------------------


@patch("debkit.action.action_pkg.DebFile")
def test_insert_refuses_a_deb_whose_control_arch_is_not_an_architecture(
    mock_debfile_cls: MagicMock,
) -> None:
    """The control arch names the `binary-<arch>/` directory, so `../x` would steer the write."""
    from debkit.action import ActionException

    action = _make_pkg_action(storage=TxnStorageStub())
    _debfile_mock(mock_debfile_cls, arch="../x")

    with pytest.raises(ActionException, match="Invalid architecture"):
        action.insert()

    _target(action).transfer_from.assert_not_called()


@patch("debkit.action.action_pkg.Changes")
def test_insert_refuses_a_changes_whose_arch_is_not_an_architecture(
    mock_changes_cls: MagicMock,
) -> None:
    """Every `.changes` token is checked, not only the first."""
    from debkit.action import ActionException

    action = _make_pkg_action(storage=TxnStorageStub())
    action.deb_filename = None
    action.changes_filename = "hello_1.0_amd64.changes"
    changes = mock_changes_cls.return_value
    changes.get_archs.return_value = ["amd64", "x/../.."]
    changes.names_source_arch.return_value = False

    with pytest.raises(ActionException, match="Invalid architecture"):
        action.insert()

    _target(action).insert_pkg.assert_not_called()
