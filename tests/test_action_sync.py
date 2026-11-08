"""Unit tests for ActionSync transaction lifecycle."""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import IO, cast
from unittest.mock import MagicMock, patch

import pytest
from helpers.storage import NonTxnStorageStub, TxnStorageStub
from txstore import HistoryRetention, Storage, StorageFS, is_transactional, maybe_transaction

from debkit.action import OutputType
from debkit.action.action_sync import ActionSync
from debkit.repo.params import RepoParams
from debkit.repo.release import ReleaseNotFoundException, ReleaseReadException

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(*, storage: object = None) -> ActionSync:
    """Build a minimally-patched ActionSync instance.

    Bypasses ``__init__`` to avoid argparse / storage attachment logic.

    Args:
        storage: The storage object attached to the target repo.  Pass a
            ``TxnStorageStub`` for transactional tests, a ``NonTxnStorageStub``
            for regression tests, or ``None`` to get a plain ``MagicMock``.
    """
    action = ActionSync.__new__(ActionSync)
    action.output = OutputType.brief
    action.fix = False
    action.verify_existing = False
    action.indices_only = False
    action.allow_release_rollback = False
    action.history_retention = HistoryRetention(max_age_days=0)

    source = MagicMock()
    source.params.url = "fs:///src"
    source.params.verify_gpg = True
    source.release.hash = "hash1"
    source.build_sync_lists.return_value = (["pkg.deb"], [])
    source.sync.return_value = []
    source.diff.return_value = MagicMock(is_empty=MagicMock(return_value=True))
    action.source_repo = source

    target = MagicMock()
    target.params.url = "s3:///tgt"
    target.storage = storage if storage is not None else MagicMock()
    action.target_repo = target

    return action


def _source(action: ActionSync) -> MagicMock:
    """Return the mocked source repo attached by `_make_action` or `_init_action`."""
    return cast(MagicMock, action.source_repo)


def _target(action: ActionSync) -> MagicMock:
    """Return the mocked target repo attached by `_make_action` or `_init_action`."""
    return cast(MagicMock, action.target_repo)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_txn_stub_is_transactional() -> None:
    assert is_transactional(TxnStorageStub())
    assert not is_transactional(NonTxnStorageStub())


# ---------------------------------------------------------------------------
# sync() — transactional storage
# ---------------------------------------------------------------------------


def test_sync_begins_and_commits_on_success() -> None:
    txn = TxnStorageStub()
    action = _make_action(storage=txn)

    action.sync()

    assert txn.began
    assert txn.committed
    assert not txn.discarded


def test_sync_discards_on_exception() -> None:
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    _source(action).sync.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        action.sync()

    assert txn.began
    assert not txn.committed
    assert txn.discarded


def test_sync_discards_on_checksum_exception() -> None:
    """ChecksumException must propagate so the transaction is discarded, not committed."""
    from debkit.repo.hashes import ChecksumException

    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    _source(action).sync.side_effect = ChecksumException("digest mismatch")

    with pytest.raises(ChecksumException, match="digest mismatch"):
        action.sync()

    assert txn.began
    assert not txn.committed
    assert txn.discarded


def test_sync_reloads_the_target_before_a_later_pass() -> None:
    """A pass after the source changed plans against what the previous pass staged."""
    action = _make_action(storage=TxnStorageStub())
    hashes = iter(["hash1", "hash2", "hash2"])

    def update(strict_arch: bool = False) -> None:
        _source(action).release.hash = next(hashes)

    _source(action).update.side_effect = update

    action.sync()

    assert _source(action).sync.call_count == 2
    assert _target(action).update.call_count == 2
    assert _source(action).release.check_fresh_against.call_count == 3


def test_sync_later_pass_never_goes_below_the_target_the_run_found() -> None:
    """A staged target reset as corrupt has no Date, so it cannot be the only floor."""
    action = _make_action(storage=TxnStorageStub())
    hashes = iter(["hash1", "hash2", "hash2"])

    def update(strict_arch: bool = False) -> None:
        _source(action).release.hash = next(hashes)

    _source(action).update.side_effect = update
    target_before = MagicMock()

    with patch("debkit.action.action_sync.copy", return_value=target_before):
        action.sync()

    floors = [c.args[0] for c in _source(action).release.check_fresh_against.call_args_list]
    assert floors == [_target(action).release, _target(action).release, target_before.release]


def test_sync_checks_release_freshness_before_planning() -> None:
    """A stale source is refused before any transfer list exists."""
    from debkit.repo.release import ReleaseFreshnessException

    action = _make_action(storage=TxnStorageStub())
    check = _source(action).release.check_fresh_against
    check.side_effect = ReleaseFreshnessException("stale")

    with pytest.raises(ReleaseFreshnessException):
        action.sync()

    check.assert_called_once()
    target, now = check.call_args.args
    assert target is _target(action).release
    assert now.tzinfo is not None
    _source(action).build_sync_lists.assert_not_called()


def test_sync_skips_release_freshness_without_gpg_verification() -> None:
    """Dates are trusted only from a signed Release."""
    action = _make_action(storage=TxnStorageStub())
    _source(action).params.verify_gpg = False

    action.sync()

    _source(action).release.check_fresh_against.assert_not_called()


def test_sync_allow_release_rollback_warns_once_across_passes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The override skips every pass and is reported once per run."""
    action = _make_action(storage=TxnStorageStub())
    action.allow_release_rollback = True
    hashes = iter(["hash1", "hash2", "hash2"])

    def update(strict_arch: bool = False) -> None:
        _source(action).release.hash = next(hashes)

    _source(action).update.side_effect = update

    with caplog.at_level(logging.WARNING):
        action.sync()

    assert _source(action).sync.call_count == 2
    _source(action).release.check_fresh_against.assert_not_called()
    assert sum("--allow-release-rollback" in r.getMessage() for r in caplog.records) == 1


def test_sync_reports_against_the_target_before_the_first_pass() -> None:
    """The printed counts diff the source with a copy taken before any pass reloaded it."""
    action = _make_action(storage=TxnStorageStub())

    action.sync()

    reported = _source(action).diff.call_args.args[0]
    assert reported is not action.target_repo


# ---------------------------------------------------------------------------
# sync() — root Release copies bound to the verified bytes, on StorageFS
# ---------------------------------------------------------------------------

_PREFIX = "dists/bookworm"


def _write_tree(base: Path, date: str, version: str) -> None:
    """Write a one-package repository whose detached Release is dated *date*."""
    deb = f"pool/main/f/foo/foo_{version}_amd64.deb"
    payload = f"deb {version}".encode()
    packages = (
        f"Package: foo\nVersion: {version}\nArchitecture: amd64\nFilename: {deb}\n"
        f"Size: {len(payload)}\nSHA256: {hashlib.sha256(payload).hexdigest()}\n"
    ).encode()
    packages = gzip.compress(packages, mtime=0)
    release = (
        f"Origin: t\nSuite: bookworm\nDate: {date}\nArchitectures: amd64\nComponents: main\n"
        f"SHA256:\n {hashlib.sha256(packages).hexdigest()} {len(packages)} "
        "main/binary-amd64/Packages.gz\n"
    ).encode()
    for path, data in {
        deb: payload,
        f"{_PREFIX}/main/binary-amd64/Packages.gz": packages,
        f"{_PREFIX}/Release": release,
        f"{_PREFIX}/Release.gpg": b"signature",
    }.items():
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        (base / path).write_bytes(data)


def _fs_action(source_storage: StorageFS, target_dir: Path) -> ActionSync:
    """A real ActionSync between two `StorageFS` trees, every source signature accepted."""
    from debkit.repo.repo_rw import RepoRW
    from debkit.repo.repo_sync import RepoSync

    gpg = MagicMock()
    gpg.verify.side_effect = lambda data, detach_sign=None: data
    action = ActionSync.__new__(ActionSync)
    action.output = OutputType.brief
    action.verify_existing = False
    action.indices_only = False
    action.allow_release_rollback = False
    action.history_retention = HistoryRetention(max_age_days=0)
    action.source_repo = RepoSync(
        source_storage, RepoParams("bookworm", ["main"], ["amd64"]), gpg=gpg
    )
    action.target_repo = RepoRW(
        StorageFS(str(target_dir)), RepoParams("bookworm", ["main"], ["amd64"])
    )
    return action


class _SwapReleaseOnceFS(StorageFS):
    """Serve other bytes for the first stream of the root Release, as a hostile source may."""

    def __init__(self, base_dir: str, swapped: bytes) -> None:
        super().__init__(base_dir)
        self.swapped: bytes | None = swapped
        self._in_get = False

    def get(self, path: str) -> bytes:
        # `get` may read through `get_stream`; the swap targets the copy stream only.
        self._in_get = True
        try:
            return super().get(path)
        finally:
            self._in_get = False

    def get_stream(self, path: str) -> IO[bytes]:
        if path == f"{_PREFIX}/Release" and self.swapped is not None and not self._in_get:
            data, self.swapped = self.swapped, None
            return io.BytesIO(data)
        return super().get_stream(path)


@pytest.mark.parametrize(
    "swapped_date",
    ["Sat, 01 Aug 2026 00:00:00 UTC", "Fri, 01 Oct 2100 00:00:00 UTC"],
    ids=["older-signed-release", "forged-future-release"],
)
def test_sync_refuses_a_root_release_copy_that_differs_from_the_verified_one(
    tmp_path: Path, swapped_date: str
) -> None:
    """Freshness was checked on the loaded bytes; the copy must be those bytes."""
    from debkit.repo.hashes import ChecksumException

    _write_tree(tmp_path / "src", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
    _write_tree(tmp_path / "tgt", "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    real = (tmp_path / "src" / _PREFIX / "Release").read_bytes()
    target_before = (tmp_path / "tgt" / _PREFIX / "Release").read_bytes()

    swapping = _SwapReleaseOnceFS(
        str(tmp_path / "src"), real.replace(b"Thu, 01 Oct 2026 00:00:00 UTC", swapped_date.encode())
    )
    action = _fs_action(swapping, tmp_path / "tgt")

    with pytest.raises(ChecksumException, match="Release"):
        action.sync()

    assert (tmp_path / "tgt" / _PREFIX / "Release").read_bytes() == target_before
    assert (tmp_path / "tgt" / "pool/main/f/foo/foo_2_amd64.deb").exists()


def test_sync_floors_against_a_peer_commit_that_lands_before_the_lock(tmp_path: Path) -> None:
    """A peer holding `history_lock` commits a newer Release; this run must floor on it."""
    from contextlib import contextmanager

    from debkit.action import action_sync
    from debkit.repo.release import ReleaseFreshnessException

    _write_tree(tmp_path / "src", "Tue, 15 Sep 2026 00:00:00 UTC", "1")
    _write_tree(tmp_path / "tgt", "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    action = _fs_action(StorageFS(str(tmp_path / "src")), tmp_path / "tgt")
    real_transaction = maybe_transaction

    @contextmanager
    def peer_commits_first(storage: Storage, retention: HistoryRetention | None) -> Iterator[None]:
        _write_tree(tmp_path / "tgt", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
        with real_transaction(storage, retention):
            yield

    with patch.object(action_sync, "maybe_transaction", peer_commits_first):
        with pytest.raises(ReleaseFreshnessException, match="older than the target"):
            action.sync()

    assert (tmp_path / "tgt" / "pool/main/f/foo/foo_3_amd64.deb").exists()


def test_sync_loads_the_target_inside_the_transaction() -> None:
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    began_at_load: list[bool] = []
    _target(action).update.side_effect = lambda: began_at_load.append(txn.began)

    action.sync()

    assert began_at_load == [True]


def test_sync_treats_a_missing_target_as_empty() -> None:
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    _target(action).update.side_effect = ReleaseNotFoundException("File not found")

    action.sync()

    assert txn.committed
    _source(action).sync.assert_called_once()


def test_sync_stops_on_a_target_it_cannot_read() -> None:
    """Only a missing Release means an empty target; any other read failure is not one."""
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    _target(action).update.side_effect = ReleaseReadException("GPG signature file missing")

    with pytest.raises(ReleaseReadException):
        action.sync()

    assert txn.discarded
    _source(action).sync.assert_not_called()


def test_sync_verify_existing_reads_the_target_again_under_the_lock() -> None:
    """Verifying existing objects runs before the lock, so its target read is not reused."""
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    action.verify_existing = True
    began_at_load: list[bool] = []
    _target(action).update.side_effect = lambda: began_at_load.append(txn.began)

    with patch("debkit.action.action_sync.RepoSync") as dup_repo:
        dup_repo.return_value.build_sync_lists.return_value = ([], [])
        action.sync()

    assert began_at_load == [False, True]


# ---------------------------------------------------------------------------
# sync() — non-transactional storage (regression for FootmarkMixin fix)
# ---------------------------------------------------------------------------


def test_sync_no_txn_on_non_txn_storage() -> None:
    """TransactionNotSupportedMixin backend must not trigger NotImplementedError.

    Before the FootmarkMixin discriminator fix, isinstance(storage,
    is_transactional) returned True for NonTxnStorageStub, and the
    subsequent transaction_begin() call raised NotImplementedError.
    """
    non_txn = NonTxnStorageStub()
    action = _make_action(storage=non_txn)

    action.sync()  # must not raise


# ---------------------------------------------------------------------------
# Retention hand-off to maybe_transaction
# ---------------------------------------------------------------------------


def test_sync_hands_retention_to_maybe_transaction() -> None:
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    action.history_retention = HistoryRetention(keep=1, max_age_days=5)

    with patch("debkit.action.action_sync.maybe_transaction") as wiring:
        action.sync()

    wiring.assert_called_once_with(txn, action.history_retention)


# ---------------------------------------------------------------------------
# Repo params forced from the CLI
# ---------------------------------------------------------------------------


def _init_action(**flags: object) -> ActionSync:
    """Run ``ActionSync.__init__`` with the base Action init and storage stubbed out."""
    from argparse import Namespace

    fields: dict[str, object] = {"architectures": "", "indices_exclude": ""}
    fields.update(flags)
    pars = Namespace(**fields)

    def fake_base_init(self: ActionSync, pars: Namespace, create_path: bool = False) -> None:
        self.source_repo = MagicMock()
        self.source_repo.params = RepoParams(dist="bookworm", components=["main"])
        self.target_repo = MagicMock()
        self.target_repo.params = RepoParams(dist="bookworm", components=["main"])

    with patch("debkit.action.action_sync.Action.__init__", fake_base_init):
        return ActionSync(pars)


def test_init_sets_indices_exclude_on_both_repo_params() -> None:
    """Both repos need the same set so `check` and `--verify-existing` agree."""
    action = _init_action(indices_exclude="*/i18n/*,*/dep11/icons-*")

    assert _source(action).params.indices_exclude == ["*/i18n/*", "*/dep11/icons-*"]
    assert _target(action).params.indices_exclude == ["*/i18n/*", "*/dep11/icons-*"]


def test_init_drops_empty_indices_exclude_tokens() -> None:
    """VALIDATION.md#index-exclude-globs drops an empty token, as `--architectures` does."""
    action = _init_action(indices_exclude="*/i18n/*,,*/dep11/*,")

    assert _source(action).params.indices_exclude == ["*/i18n/*", "*/dep11/*"]
    assert _target(action).params.indices_exclude == ["*/i18n/*", "*/dep11/*"]


def test_init_leaves_indices_exclude_unset_when_flag_is_empty() -> None:
    action = _init_action()

    assert _source(action).params.indices_exclude is None
    assert _target(action).params.indices_exclude is None


def test_init_sets_architectures_on_both_repo_params() -> None:
    action = _init_action(architectures="amd64,arm64")

    assert _source(action).params.archs == ["amd64", "arm64"]
    assert _target(action).params.archs == ["amd64", "arm64"]


def test_init_drops_empty_architecture_tokens() -> None:
    """A stray comma must not put an empty architecture into the selection."""
    action = _init_action(architectures="amd64,,arm64")

    assert _source(action).params.archs == ["amd64", "arm64"]
    assert _target(action).params.archs == ["amd64", "arm64"]


def test_init_selects_the_source_architecture_for_a_check_namespace() -> None:
    """`check` reaches the same selection: without it a source mirror reads as orphaned."""
    action = _init_action(architectures="amd64,source", fix=True)

    assert _target(action).params.archs == ["amd64", "source"]


def test_init_refuses_an_architecture_token_that_is_not_an_architecture() -> None:
    """The token names the `binary-<arch>/` directory of every index path."""
    from debkit.action import ActionException

    with pytest.raises(ActionException, match="Invalid architecture"):
        _init_action(architectures="amd64,../x")
