"""Unit tests for ActionPkg transaction lifecycle (insert / remove)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from helpers.factories import make_package, make_params
from helpers.storage import TxnStorageStub, iso_timestamp, seed_history
from txstore import (
    HistoryRetention,
    StorageFS,
    StorageNull,
    TransactionHistory,
    maybe_transaction,
)

from debkit.action import OutputType
from debkit.action.action_pkg import ActionPkg
from debkit.repo.debfile import DebFileException
from debkit.repo.params import pool_path
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
    pkg = MagicMock()
    pkg.get_arch.return_value = arch
    pkg.get_filename.return_value = "pool/main/h/hello/hello_1.0_amd64.deb"
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

    getattr(action, op)()

    # only the transaction is skipped; the work still runs
    target = _target(action)
    if op == "insert":
        target.transfer_from.assert_called_once()
    else:
        target.remove_pkg.assert_called_once_with(action.pkg_pattern)
    target.save.assert_called_once_with()


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
    tmp_path: Path,
    mock_debfile_cls: MagicMock,
    name: str,
    *,
    gpg: MagicMock | None = None,
    output: OutputType = OutputType.brief,
) -> ActionPkg:
    """Insert one amd64 deb into an `fs` repository under *tmp_path*, signed only with *gpg*."""
    deb = f"{name}_1.0_amd64.deb"
    incoming = StorageFS(str(tmp_path / "in"))
    incoming.put(deb, name.encode())

    action = _make_pkg_action()
    action.output = output
    action.incoming_storage = incoming
    action.deb_filename = deb
    action.target_repo = RepoRW(
        StorageFS(str(tmp_path / "repo")), make_params(verify_gpg=gpg is not None), gpg=gpg
    )
    instance = MagicMock()
    instance.get_package.return_value = make_package(
        pool_path("main", name, deb), Package=name, Version="1.0", Architecture="amd64"
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
def test_insert_of_an_identical_package_leaves_the_indices_unchanged(
    mock_debfile_cls: MagicMock, tmp_path: Path
) -> None:
    """Regression: an empty-list diff read as a change, so the Release was rewritten."""
    release = tmp_path / "repo" / "dists" / "bookworm" / "Release"
    dates = iter(["Sun, 01 Sep 2024 00:00:00 GMT", "Mon, 02 Sep 2024 00:00:00 GMT"])
    with patch("debkit.repo.repo_rw.formatdate", side_effect=lambda **_: next(dates)):
        _insert_into_real_repo(tmp_path, mock_debfile_cls, "foo")
        before = release.read_bytes()

        _insert_into_real_repo(tmp_path, mock_debfile_cls, "foo")

    assert release.read_bytes() == before


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


# ---------------------------------------------------------------------------
# dump(), stats(), list() — read-only views of a real repository
# ---------------------------------------------------------------------------

_HELLO = "pool/main/h/hello/hello_1.0_amd64.deb"
_WORLD = "pool/main/w/world/world_1.0_arm64.deb"


def _view_action(tmp_path: Path, output: OutputType, *, empty: bool = False) -> ActionPkg:
    """Action over an `fs` repository holding hello (amd64) and world (arm64) unless *empty*."""
    storage = StorageFS(str(tmp_path))
    built = RepoRW(storage, make_params())
    built.setup({"Architectures": "amd64 arm64", "Components": "main"})
    for arch, payload, size in (("amd64", _HELLO, 1000), ("arm64", _WORLD, 2000)):
        storage.put(payload, b"deb")
        built.insert_pkg("main", arch, make_package(payload, size=size, Version="1.0"))
    if empty:
        built.remove_pkg({"Package": re.compile(".")})
    built.save()

    action = _make_pkg_action()
    action.output = output
    action.target_repo = RepoRW(storage, make_params())
    return action


def test_dump_brief_prints_the_release_and_the_package_count_per_arch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _view_action(tmp_path, OutputType.brief).dump()

    out = capsys.readouterr().out
    assert out.startswith("Release:\n")
    assert "  Origin: bookworm\n" in out
    assert "Components:\n  main:\n" in out
    assert "    amd64: Component: main, Architecture: amd64\n      1 packages\n" in out
    assert "    arm64: Component: main, Architecture: arm64\n      1 packages\n" in out


def test_dump_json_carries_the_release_fields_and_every_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _view_action(tmp_path, OutputType.json).dump()

    dump = json.loads(capsys.readouterr().out)
    assert dump["release"]["data"]["Origin"] == "bookworm"
    assert dump["release"]["digests"]
    assert set(dump["components"]["main"]) == {"amd64", "arm64"}


def test_stats_brief_prints_sizes_and_counts_per_arch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _view_action(tmp_path, OutputType.brief).stats()

    out = capsys.readouterr().out
    assert "    amd64: 1.0 kB, 1 packages\n" in out
    assert "    arm64: 2.0 kB, 1 packages\n" in out
    assert out.splitlines()[-2] == "Indices read:"


def test_stats_json_reports_package_and_index_totals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _view_action(tmp_path, OutputType.json).stats()

    stats = json.loads(capsys.readouterr().out)
    assert stats["packages"]["main"]["amd64"] == {"packages_total": 1, "packages_bytes": 1000}
    assert stats["indices"]["files_total"] > 0
    assert stats["indices"]["files_bytes"] > 0


def test_list_brief_prints_one_filename_per_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _view_action(tmp_path, OutputType.brief).list()

    assert sorted(capsys.readouterr().out.split()) == [_HELLO, _WORLD]


def test_list_json_prints_the_fields_of_every_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _view_action(tmp_path, OutputType.json).list()

    packages = json.loads(capsys.readouterr().out)
    assert sorted(pkg["Filename"] for pkg in packages) == [_HELLO, _WORLD]


@pytest.mark.parametrize(
    ("output", "expected"), [(OutputType.brief, "\n"), (OutputType.json, "[]\n")]
)
def test_list_of_a_repository_without_packages_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: OutputType, expected: str
) -> None:
    _view_action(tmp_path, output, empty=True).list()

    assert capsys.readouterr().out == expected


def test_insert_without_incoming_storage_raises() -> None:
    """Regression: a namespace without `incoming_url` returned silently with exit 0."""
    from debkit.action import ActionException

    action = _make_pkg_action()
    action.deb_filename = "foo_1.0_amd64.deb"
    action.incoming_storage = None

    with pytest.raises(ActionException, match="incoming-url"):
        action.insert()


@patch("debkit.action.action_pkg.DebFile")
def test_insert_built_by_its_constructor_hands_the_retention_to_the_transaction(
    mock_debfile_cls: MagicMock, tmp_path: Path
) -> None:
    """The real `parse_cli` → `ActionPkg.__init__` → `maybe_transaction` chain."""
    from debkit.action import action_pkg
    from debkit.argparser import parse_cli

    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "foo_1.0_amd64.deb").write_bytes(b"foo")
    pars = parse_cli(
        [
            *("--target-url", f"fs://{tmp_path / 'repo'} bookworm main", "--skip-verify-gpg"),
            *("--history-keep", "2", "--history-max-age", "30"),
            *("insert", "--deb-filename", "foo_1.0_amd64.deb"),
            *("--incoming-url", f"fs://{tmp_path / 'in'}"),
        ]
    )
    pool = "pool/main/f/foo/foo_1.0_amd64.deb"
    instance = MagicMock()
    instance.get_package.return_value = make_package(
        pool, Package="foo", Version="1.0", Architecture="amd64"
    )
    mock_debfile_cls.return_value = instance

    with patch.object(action_pkg, "maybe_transaction", wraps=maybe_transaction) as wiring:
        ActionPkg(pars, create_path=True).insert()

    assert wiring.call_args.args[1] == HistoryRetention(keep=2, max_age_days=30)
    assert (tmp_path / "repo" / pool).read_bytes() == b"foo"


def test_insert_from_a_missing_incoming_directory_fails_without_creating_it(
    tmp_path: Path,
) -> None:
    """The incoming storage is only read, so a mistyped path is never materialised."""
    from debkit.argparser import parse_cli

    pars = parse_cli(
        [
            *("--target-url", f"fs://{tmp_path / 'repo'} bookworm main", "--skip-verify-gpg"),
            *("insert", "--deb-filename", "foo_1.0_amd64.deb"),
            *("--incoming-url", f"fs://{tmp_path / 'in'}"),
        ]
    )

    with pytest.raises(DebFileException, match="foo_1.0_amd64.deb"):
        ActionPkg(pars, create_path=True).insert()

    assert not (tmp_path / "in").exists()


@pytest.mark.parametrize("swapped", [False, True], ids=["matching", "swapped"])
@patch("debkit.action.action_pkg.DebFile")
def test_insert_checks_each_deb_against_its_changes_digest(
    mock_debfile_cls: MagicMock, tmp_path: Path, swapped: bool
) -> None:
    """Regression: a payload swapped after its `.changes` was pinned was published."""
    import hashlib

    from debkit.repo.hashes import ChecksumException

    deb = "foo_1.0_amd64.deb"
    listed = b"the built package"
    incoming = StorageFS(str(tmp_path / "in"))
    incoming.put(deb, b"a swapped package" if swapped else listed)
    incoming.put(
        "foo_1.0_amd64.changes",
        f"Format: 1.8\nArchitecture: amd64\nChecksums-Sha256:\n"
        f" {hashlib.sha256(listed).hexdigest()} {len(listed)} {deb}\n".encode(),
    )
    action = _make_pkg_action()
    action.incoming_storage = incoming
    action.deb_filename = None
    action.changes_filename = "foo_1.0_amd64.changes"
    action.target_repo = RepoRW(StorageFS(str(tmp_path / "repo")), make_params(verify_digests=True))
    instance = MagicMock()
    instance.get_package.return_value = make_package(
        f"pool/main/f/foo/{deb}", Package="foo", Version="1.0", Architecture="amd64"
    )
    mock_debfile_cls.return_value = instance
    pool = tmp_path / "repo" / "pool/main/f/foo" / deb

    if not swapped:
        action.insert()
        assert pool.read_bytes() == listed
        return
    with pytest.raises(ChecksumException, match="does not match its digest"):
        action.insert()
    assert not pool.exists()
    assert not (tmp_path / "repo" / "dists" / "bookworm" / "Release").exists()


@pytest.mark.parametrize("output", [OutputType.brief, OutputType.json])
@patch("debkit.action.action_pkg.DebFile")
def test_insert_and_remove_print_their_diff(
    mock_debfile_cls: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    output: OutputType,
) -> None:
    """Pins the stdout of both commands in both formats."""
    _insert_into_real_repo(tmp_path, mock_debfile_cls, "foo", output=output)
    inserted_out = capsys.readouterr().out
    removing = _make_pkg_action()
    removing.output = output
    removing.target_repo = RepoRW(StorageFS(str(tmp_path / "repo")), make_params())
    removing.pkg_pattern = {"Package": re.compile("^foo$")}

    removing.remove()
    removed_out = capsys.readouterr().out

    if output == OutputType.brief:
        assert inserted_out.startswith("Inserted 1 packages.\n")
        assert inserted_out.endswith("transferred.\n")
        assert removed_out == "Removed 1 packages.\n"
        return
    assert inserted_out.startswith('{\n  "packages"'), "dump_json's shared indent"
    inserted, removed = json.loads(inserted_out), json.loads(removed_out)
    assert list(inserted) == list(removed) == ["packages", "transfer"]
    assert inserted["packages"]["added"]["main"]["amd64"][0]["Package"] == "foo"
    assert removed["packages"]["removed"]["main"]["amd64"][0]["Package"] == "foo"


@pytest.mark.parametrize(
    ("arch", "expected"),
    [("amd64", ["amd64"]), ("all", ["amd64", "arm64"]), (None, ["amd64", "arm64"])],
    ids=["own-arch", "all", "arch-less"],
)
def test_target_archs_follows_the_control_architecture(
    arch: str | None, expected: list[str]
) -> None:
    fields = {"Architecture": arch} if arch is not None else {}
    package = make_package("pool/main/f/foo/foo.deb", Package="foo", **fields)

    assert ActionPkg._target_archs("foo.deb", package, ["amd64", "arm64"]) == expected


@pytest.mark.parametrize(
    ("arch", "binary_archs", "message"),
    [
        ("../x", ["amd64"], "Invalid architecture"),
        ("source", ["amd64"], "Source packages are not supported"),
        ("all", [], "lists no binary architecture"),
    ],
    ids=["invalid", "source", "no-binary-arch"],
)
def test_target_archs_refuses_an_unusable_architecture(
    arch: str, binary_archs: list[str], message: str
) -> None:
    from debkit.action import ActionException

    package = make_package("pool/main/f/foo/foo.deb", Package="foo", Architecture=arch)

    with pytest.raises(ActionException, match=message):
        ActionPkg._target_archs("foo.deb", package, binary_archs)
