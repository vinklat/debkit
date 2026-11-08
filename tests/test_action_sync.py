"""Unit tests for ActionSync transaction lifecycle."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
from argparse import Namespace
from collections.abc import Iterator
from copy import copy
from pathlib import Path
from typing import IO, Any, cast
from unittest.mock import MagicMock, patch

import pytest
from helpers.storage import TxnStorageStub, iso_timestamp, seed_history, storage_namespace
from txstore import (
    HistoryRetention,
    Storage,
    StorageException,
    StorageFS,
    StorageNull,
    TransactionHistory,
    maybe_transaction,
)

from debkit.action import ActionException, OutputType
from debkit.action.action_sync import MAX_UPDATED_REPO_RETRIES, ActionSync
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
            ``TxnStorageStub`` for transactional tests, txstore's ``StorageNull``
            for a backend that refuses transactions, or ``None`` to get a plain
            ``MagicMock``.
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
    source.build_sync_lists.return_value = (["pkg.deb"], [], set())
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


def test_sync_logs_the_repository_urls_without_their_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """URL userinfo is a secret -> CONFIG.md#secrets."""
    action = _make_action(storage=TxnStorageStub())
    _source(action).params.url = "https://u:s3cr3tpw@host/debian"
    _target(action).params.url = "s3://k:s3cr3tpw@host/bucket"

    with caplog.at_level(logging.DEBUG):
        action.sync()

    assert "Load source repo (https://***@host/debian)" in caplog.text
    assert "Load target repo (s3://***@host/bucket)" in caplog.text
    assert all("s3cr3tpw" not in rec.getMessage() for rec in caplog.records)


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


def test_sync_commits_the_last_pass_and_warns_when_the_source_never_settles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    hashes = iter(f"hash{n}" for n in range(1, 100))

    def update(strict_arch: bool = False) -> None:
        _source(action).release.hash = next(hashes)

    _source(action).update.side_effect = update

    with caplog.at_level(logging.WARNING):
        action.sync()

    assert _source(action).sync.call_count == MAX_UPDATED_REPO_RETRIES
    assert txn.committed
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        f"Source repo still changing after {MAX_UPDATED_REPO_RETRIES} passes; "
        "committing the last pass"
    ]


def _recording_copy(copies: list[tuple[object, MagicMock]]) -> Any:
    """A `copy` stand-in returning a fresh mock per call and recording what it copied."""

    def fake_copy(obj: object) -> MagicMock:
        snapshot = MagicMock(name=f"copy{len(copies)}")
        copies.append((obj, snapshot))
        return snapshot

    return fake_copy


def test_sync_later_pass_never_goes_below_the_target_the_run_found() -> None:
    """Each pass floors on a snapshot of its own target load and on the first one."""
    action = _make_action(storage=TxnStorageStub())
    hashes = iter(["hash1", "hash2", "hash2"])

    def update(strict_arch: bool = False) -> None:
        _source(action).release.hash = next(hashes)

    _source(action).update.side_effect = update
    copies: list[tuple[object, MagicMock]] = []

    with patch("debkit.action.action_sync.copy", _recording_copy(copies)):
        action.sync()

    # first load, the report's target_before, then the later pass's load
    assert [obj for obj, _ in copies] == [
        _target(action).release,
        _target(action),
        _target(action).release,
    ]
    first, staged = copies[0][1], copies[2][1]
    floors = [c.args[0] for c in _source(action).release.check_fresh_against.call_args_list]
    assert floors == [first, staged, first]


def test_sync_checks_release_freshness_before_planning() -> None:
    """A stale source is refused before any transfer list exists."""
    from debkit.repo.release import ReleaseFreshnessException

    action = _make_action(storage=TxnStorageStub())
    check = _source(action).release.check_fresh_against
    check.side_effect = ReleaseFreshnessException("stale")
    copies: list[tuple[object, MagicMock]] = []

    with patch("debkit.action.action_sync.copy", _recording_copy(copies)):
        with pytest.raises(ReleaseFreshnessException):
            action.sync()

    check.assert_called_once()
    target, now = check.call_args.args
    assert copies[0] == (_target(action).release, target)
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


def _fs_action(
    source_storage: StorageFS,
    target_dir: Path | None = None,
    *,
    target_storage: Storage | None = None,
) -> ActionSync:
    """A real ActionSync between two storages, every source signature accepted.

    The target is a `StorageFS` on `target_dir` unless `target_storage` is given.
    """
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
        target_storage or StorageFS(str(target_dir)), RepoParams("bookworm", ["main"], ["amd64"])
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


def test_sync_then_check_finds_the_mirror_consistent(tmp_path: Path) -> None:
    """A real fs→fs sync leaves nothing missing or orphaned for `check` to report."""
    _write_tree(tmp_path / "src", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
    action = _fs_action(StorageFS(str(tmp_path / "src")), tmp_path / "tgt")
    action.fix = False

    action.sync()

    payload = tmp_path / "tgt" / "pool/main/f/foo/foo_3_amd64.deb"
    assert payload.read_bytes() == b"deb 3"
    assert action.check() is True
    payload.unlink()
    assert action.check() is False


def test_sync_over_an_older_mirror_replaces_its_stale_payload(tmp_path: Path) -> None:
    """A real fs→fs v2→v3 sync deletes the payload v3 no longer lists, in one transaction."""
    _write_tree(tmp_path / "src", "Wed, 01 Oct 2025 00:00:00 UTC", "2")
    _fs_action(StorageFS(str(tmp_path / "src")), tmp_path / "tgt").sync()
    _write_tree(tmp_path / "src", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
    (tmp_path / "src" / "pool/main/f/foo/foo_2_amd64.deb").unlink()
    action = _fs_action(StorageFS(str(tmp_path / "src")), tmp_path / "tgt")
    action.fix = False

    action.sync()

    pool = tmp_path / "tgt" / "pool/main/f/foo"
    # dot-files are txstore footmarks of the history, not payloads
    payloads = sorted(path.name for path in pool.iterdir() if not path.name.startswith("."))
    assert payloads == ["foo_3_amd64.deb"]
    assert action.check() is True
    assert len(TransactionHistory(StorageFS(str(tmp_path / "tgt"))).load()) == 2


def test_sync_built_by_its_constructor_hands_the_retention_to_the_transaction(
    tmp_path: Path,
) -> None:
    """The real `parse_cli` → `ActionSync.__init__` → `maybe_transaction` → prune chain."""
    from debkit.action import action_sync
    from debkit.argparser import parse_cli

    target = tmp_path / "tgt"
    target.mkdir()
    seed_history(
        StorageFS(str(target)),
        [{"id": 1, "timestamp": iso_timestamp(400)}, {"id": 2, "timestamp": iso_timestamp(390)}],
    )
    _write_tree(tmp_path / "src", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
    pars = parse_cli(
        [
            *("--target-url", f"fs://{target} bookworm main", "--skip-verify-gpg"),
            *("--history-keep", "1", "--history-max-age", "90"),
            *("sync", "--source-url", f"{tmp_path / 'src'} bookworm main"),
            *("--architectures", "amd64"),
        ]
    )

    with patch.object(action_sync, "maybe_transaction", wraps=maybe_transaction) as wiring:
        ActionSync(pars).sync()

    assert wiring.call_args.args[1] == HistoryRetention(keep=1, max_age_days=90)
    assert [record.id for record in TransactionHistory(StorageFS(str(target))).load()] == [3]


def test_sync_to_s3_writes_objects_with_the_default_public_read_acl(
    tmp_path: Path, moto_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3 smoke test: an fs → `s3://` sync on moto, every option at its CLI default.

    `s3://` always means HTTPS, so the S3 storage `open_storage` asks for is built against the
    plain-HTTP moto endpoint from the settings debkit resolved → STORAGE.md#construction.
    """
    boto3 = pytest.importorskip("boto3")
    from txstore import StorageS3
    from txstore import make_storage as real_make_storage

    from debkit.argparser import parse_cli

    def moto_make_storage(url: str, **settings: object) -> Storage:
        if not url.startswith("s3://"):
            return real_make_storage(url, **settings)  # type: ignore[arg-type]
        bucket, _, prefix = url.removeprefix("s3://").split("/", 1)[1].partition("/")
        return StorageS3(
            moto_server,
            bucket,
            cast(str, settings["s3_access"]),
            cast(str, settings["s3_secret"]),
            prefix=prefix or None,
            create_bucket=cast(bool, settings["create_path"]),
            default_acl=cast(Any, settings["s3_default_acl"]),
        )

    _write_tree(tmp_path / "src", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
    monkeypatch.setenv("DEBKIT_S3_SECRET", "testing")
    monkeypatch.setattr("debkit.action.make_storage", moto_make_storage)
    pars = parse_cli(
        [
            "--target-url",
            "s3://s3.example.com/debkit-smoke/mirror bookworm main",
            "-u",
            "testing",
            "--skip-verify-gpg",
            "sync",
            "--source-url",
            f"{tmp_path / 'src'} bookworm main",
            "--architectures",
            "amd64",
        ]
    )

    ActionSync(pars, create_path=True).sync()

    client = boto3.client(
        "s3",
        endpoint_url=moto_server,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    deb = "mirror/pool/main/f/foo/foo_3_amd64.deb"
    assert client.get_object(Bucket="debkit-smoke", Key=deb)["Body"].read() == b"deb 3"
    grants = client.get_object_acl(Bucket="debkit-smoke", Key=deb)["Grants"]
    assert {
        "Type": "Group",
        "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
    } in [grant["Grantee"] for grant in grants if grant["Permission"] == "READ"]


def test_sync_floors_against_the_date_of_a_target_reset_as_corrupt(tmp_path: Path) -> None:
    """A reset clears the target Release; the Date it carried still floors the source."""
    from debkit.repo.release import ReleaseFreshnessException

    _write_tree(tmp_path / "src", "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    _write_tree(tmp_path / "tgt", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
    (tmp_path / "tgt" / _PREFIX / "main/binary-amd64/Packages.gz").write_bytes(b"corrupt")
    action = _fs_action(StorageFS(str(tmp_path / "src")), tmp_path / "tgt")

    with pytest.raises(ReleaseFreshnessException, match="older than the target"):
        action.sync()

    assert (tmp_path / "tgt" / "pool/main/f/foo/foo_3_amd64.deb").exists()


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


def test_sync_refuses_to_run_without_a_source() -> None:
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    action.source_repo = None

    with pytest.raises(ActionException, match="source repository"):
        action.sync()

    assert not txn.began


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


def test_sync_verify_existing_checks_the_target_it_loaded_inside_the_transaction() -> None:
    """The verify deletions are staged with the sync, on the one target load under the lock."""
    txn = TxnStorageStub()
    action = _make_action(storage=txn)
    action.verify_existing = True
    began_at_load: list[bool] = []
    began_at_check: list[bool] = []
    _target(action).update.side_effect = lambda: began_at_load.append(txn.began)

    def check(missing: list[str], orphans: list[str], fix: bool = False) -> tuple[int, int]:
        began_at_check.append(txn.began and not txn.committed)
        return 0, 0

    with patch("debkit.action.action_sync.RepoSync") as checker:
        checker.return_value.consistency_lists.return_value = (["gone.deb"], ["junk"])
        checker.return_value.check.side_effect = check
        action.sync()

    assert began_at_load == [True]
    assert began_at_check == [True]
    checker.return_value.check.assert_called_once_with(["gone.deb"], [], fix=True)


def test_sync_verify_existing_warns_about_a_failure_it_could_not_delete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    action = _make_action(storage=TxnStorageStub())
    action.verify_existing = True

    with patch("debkit.action.action_sync.RepoSync") as checker:
        checker.return_value.consistency_lists.return_value = ([], [])
        checker.return_value.check.return_value = (3, 1)
        with caplog.at_level(logging.WARNING):
            action.sync()

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "2 target files failed verification and were not deleted"
    ]


class _UnreadableIndicesFS(StorageFS):
    """A backend whose every index read and every listing fails."""

    def get(self, path: str) -> bytes:
        if path.startswith("dists/"):
            raise StorageException(f"backend down reading {path}")
        return super().get(path)

    def list(self, *args: Any, **kwargs: Any) -> list[str]:
        raise StorageException("backend down listing")


def test_sync_verify_existing_propagates_the_target_read_failure(tmp_path: Path) -> None:
    """No check runs on a target that did not load, so its read failure is what surfaces."""
    from debkit.repo import RepoReadException

    _write_tree(tmp_path / "src", "Thu, 01 Oct 2026 00:00:00 UTC", "3")
    _write_tree(tmp_path / "tgt", "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    action = _fs_action(
        StorageFS(str(tmp_path / "src")),
        target_storage=_UnreadableIndicesFS(str(tmp_path / "tgt")),
    )
    action.verify_existing = True

    with pytest.raises(RepoReadException, match="backend down reading"):
        action.sync()


def test_sync_verify_existing_replaces_a_corrupt_payload_in_one_transaction(
    tmp_path: Path,
) -> None:
    _write_tree(tmp_path / "src", "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    _write_tree(tmp_path / "tgt", "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    deb = tmp_path / "tgt" / "pool/main/f/foo/foo_2_amd64.deb"
    deb.write_bytes(b"corrupt")
    action = _fs_action(StorageFS(str(tmp_path / "src")), tmp_path / "tgt")
    action.verify_existing = True

    action.sync()

    assert deb.read_bytes() == b"deb 2"
    assert len(TransactionHistory(StorageFS(str(tmp_path / "tgt"))).load()) == 1


# ---------------------------------------------------------------------------
# check() — consistency of the target with its own indices
# ---------------------------------------------------------------------------


def _check_action(
    storage: Storage, *, fix: bool = False, output: OutputType = OutputType.brief
) -> ActionSync:
    """A real `check` over a target storage; check runs without a source."""
    from debkit.repo.repo_rw import RepoRW

    action = ActionSync.__new__(ActionSync)
    action.output = output
    action.fix = fix
    action.history_retention = HistoryRetention(max_age_days=0)
    action.source_repo = None
    action.target_repo = RepoRW(storage, RepoParams("bookworm", ["main"], ["amd64"]))
    return action


def test_check_finds_a_consistent_repository_consistent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_tree(tmp_path, "Tue, 01 Sep 2026 00:00:00 UTC", "2")

    assert _check_action(StorageFS(str(tmp_path))).check() is True
    assert capsys.readouterr().out == "Errors total: 0 files\nErrors fixed: 0 files\n"


class _ListingHidesFS(StorageFS):
    """A listing that disagrees with reads: it omits every path ending in `hidden`."""

    hidden = ""

    def list(self, *args: Any, **kwargs: Any) -> list[str]:
        return [path for path in super().list(*args, **kwargs) if not path.endswith(self.hidden)]


@pytest.mark.parametrize(
    ("hidden", "consistent"),
    [("Release.gpg", True), ("Packages.gz", False), (".deb", False)],
    ids=["root-release-file", "parsed-index", "payload"],
)
def test_check_counts_missing_only_what_a_parse_or_package_proves(
    tmp_path: Path, hidden: str, consistent: bool
) -> None:
    """A root Release file was just read, so a listing that omits it is not a missing file."""
    _write_tree(tmp_path, "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    storage = _ListingHidesFS(str(tmp_path))
    storage.hidden = hidden

    assert _check_action(storage).check() is consistent


@pytest.mark.parametrize("fix", [False, True])
def test_check_reports_an_orphan_and_fix_deletes_it(tmp_path: Path, fix: bool) -> None:
    _write_tree(tmp_path, "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    orphan = tmp_path / "pool/main/o/old/old_1_amd64.deb"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"old")

    assert _check_action(StorageFS(str(tmp_path)), fix=fix).check() is fix
    assert orphan.exists() is not fix


def test_check_fix_cannot_repair_a_missing_payload(tmp_path: Path) -> None:
    _write_tree(tmp_path, "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    (tmp_path / "pool/main/f/foo/foo_2_amd64.deb").unlink()

    assert _check_action(StorageFS(str(tmp_path)), fix=True).check() is False


@pytest.mark.parametrize("fix", [False, True])
def test_check_records_a_transaction_only_with_fix(tmp_path: Path, fix: bool) -> None:
    """`check --fix` deletes, so its writes are recorded like any other write."""
    from debkit.action import action_sync

    _write_tree(tmp_path, "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    action = _check_action(StorageFS(str(tmp_path)), fix=fix)

    with patch.object(action_sync, "maybe_transaction", wraps=maybe_transaction) as wiring:
        action.check()

    assert wiring.call_count == int(fix)


def test_check_prints_json_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_tree(tmp_path, "Tue, 01 Sep 2026 00:00:00 UTC", "2")
    (tmp_path / "junk").write_bytes(b"x")

    assert _check_action(StorageFS(str(tmp_path)), output=OutputType.json).check() is False
    assert json.loads(capsys.readouterr().out)["status"] == {
        "errors_total": 1,
        "errors_fixed_total": 0,
    }


def test_check_reports_an_empty_target_inconsistent(tmp_path: Path) -> None:
    assert _check_action(StorageFS(str(tmp_path))).check() is False


# ---------------------------------------------------------------------------
# sync() — non-transactional storage (regression for FootmarkMixin fix)
# ---------------------------------------------------------------------------


def test_sync_no_txn_on_non_txn_storage() -> None:
    """A backend that refuses transactions runs without one.

    `StorageNull` is a txstore `TransactionNotSupportedMixin` backend, whose
    `transaction_begin()` raises `NotImplementedError`.
    """
    action = _make_action(storage=StorageNull())

    action.sync()

    # only the transaction is skipped; the work still runs
    _source(action).sync.assert_called_once()


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

_SOURCE_URL = "fs:///tmp/src bookworm main"


def _sync_pars(**flags: object) -> Namespace:
    """A `sync` namespace over `fs://` repos with verification off; *flags* override fields."""
    fields: dict[str, object] = {
        "command": "sync",
        "source_url": _SOURCE_URL,
        "architectures": "",
        "indices_exclude": "",
    }
    fields.update(flags)
    return storage_namespace(**fields)


def _init_action(
    pars: Namespace | None = None, **flags: object
) -> tuple[ActionSync, dict[str, tuple[list[str] | None, list[str] | None]]]:
    """Run the real ``ActionSync.__init__``, recording each repo's selection as built.

    Returns the action and, per repo class, the `archs` and `indices_exclude` its
    `RepoParams` held when the repo was constructed.
    """
    from debkit.repo.repo_rw import RepoRW
    from debkit.repo.repo_sync import RepoSync

    built: dict[str, tuple[list[str] | None, list[str] | None]] = {}

    def recorder(name: str, real_init: Any) -> Any:
        def init(self: Any, storage: Any, params: RepoParams, **kwargs: Any) -> None:
            built[name] = (copy(params.archs), copy(params.indices_exclude))
            real_init(self, storage, params, **kwargs)

        return init

    with (
        patch.object(RepoSync, "__init__", recorder("RepoSync", RepoSync.__init__)),
        patch.object(RepoRW, "__init__", recorder("RepoRW", RepoRW.__init__)),
    ):
        action = ActionSync(pars if pars is not None else _sync_pars(**flags))
    return action, built


def test_init_sets_indices_exclude_on_both_repo_params_before_building_them() -> None:
    """Both repos need the same set so `check` and `--verify-existing` agree."""
    _, built = _init_action(indices_exclude="*/i18n/*,*/dep11/icons-*")

    assert built["RepoSync"] == (None, ["*/i18n/*", "*/dep11/icons-*"])
    assert built["RepoRW"] == (None, ["*/i18n/*", "*/dep11/icons-*"])


def test_init_drops_empty_indices_exclude_tokens() -> None:
    """VALIDATION.md#index-exclude-globs drops an empty token, as `--architectures` does."""
    _, built = _init_action(indices_exclude="*/i18n/*,,*/dep11/*,")

    assert built["RepoSync"][1] == ["*/i18n/*", "*/dep11/*"]
    assert built["RepoRW"][1] == ["*/i18n/*", "*/dep11/*"]


def test_init_leaves_the_selection_unset_when_the_flags_are_empty() -> None:
    _, built = _init_action()

    assert built == {"RepoSync": (None, None), "RepoRW": (None, None)}


def test_init_sets_architectures_on_both_repo_params_before_building_them() -> None:
    _, built = _init_action(architectures="amd64,arm64")

    assert built["RepoSync"] == (["amd64", "arm64"], None)
    assert built["RepoRW"] == (["amd64", "arm64"], None)


def test_init_drops_empty_architecture_tokens() -> None:
    """A stray comma must not put an empty architecture into the selection."""
    _, built = _init_action(architectures="amd64,,arm64")

    assert built["RepoSync"][0] == ["amd64", "arm64"]
    assert built["RepoRW"][0] == ["amd64", "arm64"]


def test_init_selects_the_target_architecture_for_a_check_namespace() -> None:
    """`check` reaches the same selection: without it a source mirror reads as orphaned."""
    pars = storage_namespace(
        command="check", fix=True, architectures="amd64,source", indices_exclude=""
    )

    action, built = _init_action(pars)

    assert action.source_repo is None
    assert "RepoSync" not in built
    assert built["RepoRW"] == (["amd64", "source"], None)


def test_init_reads_the_sync_flags() -> None:
    """Non-default `--verify-existing` and `--allow-release-rollback` survive `__init__`."""
    action, _ = _init_action(
        verify_existing=True, allow_release_rollback=True, output=OutputType.json
    )

    assert action.verify_existing is True
    assert action.allow_release_rollback is True
    assert action.output is OutputType.json


def test_init_gives_the_sync_target_no_gnupg() -> None:
    """A sync overwrites its target, so the target is neither verified nor signed."""
    with patch("debkit.action.GnuPG") as gnupg:
        action, _ = _init_action(skip_verify=False, skip_verify_gpg=False)

    assert action.source_repo is not None
    assert action.source_repo.gpg is gnupg.return_value
    assert action.target_repo.gpg is None


def test_init_gives_the_check_target_the_gnupg() -> None:
    pars = storage_namespace(
        command="check",
        skip_verify=False,
        skip_verify_gpg=False,
        architectures="",
        indices_exclude="",
    )

    with patch("debkit.action.GnuPG") as gnupg:
        action, _ = _init_action(pars)

    assert action.target_repo.gpg is gnupg.return_value


def test_init_logs_the_repository_urls_without_their_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """URL userinfo is a secret -> CONFIG.md#secrets."""
    secret = "s3cr3tpw"

    with caplog.at_level(logging.DEBUG):
        _init_action(source_url=f"https://u:{secret}@host/debian bookworm main")

    assert "Prepare source URL: https://***@host/debian bookworm main" in caplog.text
    assert all(secret not in rec.getMessage() for rec in caplog.records)


def test_init_refuses_an_architecture_token_that_is_not_an_architecture() -> None:
    """The token names the `binary-<arch>/` directory of every index path."""
    with pytest.raises(ActionException, match="Invalid architecture"):
        _init_action(architectures="amd64,../x")


def test_init_refuses_an_architecture_before_opening_any_storage() -> None:
    """Regression: the refusal came after `create_path` had made the target path or bucket."""
    with (
        patch("debkit.action.action_sync.attach_storage_repo") as attach,
        patch("debkit.action.action_sync.Action.__init__") as base_init,
        pytest.raises(ActionException, match="Invalid architecture"),
    ):
        ActionSync(_sync_pars(architectures="../x"), create_path=True)

    base_init.assert_not_called()
    attach.assert_not_called()
