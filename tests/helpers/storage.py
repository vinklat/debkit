"""Shared storage helpers and stubs for action-layer tests.

Built on the `txstore` façade only → [TESTING.md](../../TESTING.md#harness-and-gates).
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol
from unittest.mock import MagicMock

from txstore import FootmarkMixin, StorageNotFoundException, TransactionHistory, TransactionModel

if TYPE_CHECKING:
    from collections.abc import Hashable

    from txstore import Storage


class TransactionalStorage(Protocol):
    """The slice of a transactional backend `commit_transaction` drives."""

    def put(self, path: str, data: bytes) -> None: ...

    def transaction_begin(self, transaction_id: int = 0) -> TransactionModel: ...

    def transaction_commit(self) -> bool: ...


def seed_history(storage: Storage | FootmarkMixin, records: list[dict[str, object]]) -> None:
    """Write `records` (``{id, timestamp}`` objects) as the storage's transaction history.

    Delegates to `TransactionHistory.save`, the path production writes history through.
    """
    TransactionHistory(storage).save(  # type: ignore[arg-type]
        [TransactionHistory.record_to_model(record) for record in records]
    )


def commit_transaction(
    storage: TransactionalStorage, path: str, data: bytes = b"payload"
) -> TransactionModel:
    """Run one committed transaction writing `path` and return its model."""
    model = storage.transaction_begin()
    storage.put(path, data)
    storage.transaction_commit()
    return model


def footmarks_of(paths: Iterable[str], model: TransactionModel) -> list[str]:
    """The `paths` whose basename ends in one of `model`'s ``.<token>`` suffixes, sorted."""
    suffixes = tuple(f".{token}" for token in model.footmarks)
    return sorted(p for p in paths if p.rsplit("/", 1)[-1].endswith(suffixes))


def iso_timestamp(days_ago: float, *, now: datetime | None = None) -> str:
    """History-record timestamp `days_ago` days before `now` (default: current UTC time)."""
    moment = (now or datetime.now(UTC)) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def transaction_record(model: TransactionModel) -> dict[str, object]:
    """History record exactly as `maybe_transaction` writes it.

    Footmark tokens regenerate from ``{id, timestamp}``, so a record carries the transaction's
    real begin timestamp; age is controlled by moving `prune`'s reference time instead.
    """
    return {"id": model.id, "timestamp": model.timestamp.to_json()}


class TxnStorageStub(FootmarkMixin):
    """Transaction-capable stub recognised by `is_transactional`.

    The lifecycle methods only record calls, so tests assert the call sequence without a real
    backend; reserved objects (history) live in memory and the lock is a no-op.
    """

    def __init__(self) -> None:
        super().__init__()
        self.began = False
        self.committed = False
        self.discarded = False
        self.metrics = MagicMock()
        self._objects: dict[str, bytes] = {}
        self._reserved: dict[str, bytes] = {}

    def exists(self, path: str) -> bool:
        """Report whether the stub holds `path`."""
        return path in self._objects

    def get(self, path: str) -> bytes:
        """Return the bytes stored at `path`."""
        try:
            return self._objects[path]
        except KeyError as exc:
            raise StorageNotFoundException(f"Object not found: {path}") from exc

    def put(self, path: str, data: bytes, **kwargs: object) -> None:
        """Store `data` at `path`."""
        self._objects[path] = data

    def in_transaction(self) -> bool:
        """Return True once `transaction_begin` has been called."""
        return self.began

    def transaction_begin(self, transaction_id: int = 0) -> TransactionModel:
        """Record that a transaction was started."""
        model = super().transaction_begin(transaction_id)
        self.began = True
        return model

    def transaction_commit(self) -> bool:
        """Record the commit; always reports changes so history is appended."""
        self.committed = True
        return True

    def _commit_listed(self) -> Any:
        """Commit hook `maybe_transaction` calls: record it and report changes, nothing listed."""
        self.transaction_commit()
        return SimpleNamespace(changed=True, hidden=[])

    def transaction_revert(self, transaction: TransactionModel) -> None:
        """No-op revert (not exercised by action-layer tests)."""

    def transaction_discard(self) -> None:
        """Record that the transaction was discarded."""
        self.discarded = True

    # Backend primitives: only the lock and the reserved objects are reached.

    def _obj_full(self, rel: str) -> str:
        return rel

    def _obj_iter_rel_all(self) -> Iterator[str]:
        return iter(sorted(self._objects))

    def _obj_exists(self, full: str) -> bool:
        return full in self._objects

    def _obj_copy(self, src_full: str, dst_full: str) -> None:
        self._objects[dst_full] = self._objects[src_full]

    def _obj_delete(self, full: str) -> None:
        self._objects.pop(full, None)

    def _obj_touch(self, full: str) -> None:
        self._objects.setdefault(full, b"")

    def _obj_lock(self) -> AbstractContextManager[None]:
        return nullcontext()

    def _obj_lock_identity(self) -> Hashable:
        return id(self)

    def _obj_put_reserved(self, rel: str, data: bytes) -> None:
        self._reserved[rel] = data

    def _obj_get_reserved(self, rel: str) -> bytes | None:
        return self._reserved.get(rel)


class NonTxnStorageStub:
    """Non-transactional storage stub: `is_transactional` reports False for it."""

    def __init__(self) -> None:
        self.metrics = MagicMock()


def storage_namespace(**overrides: object) -> Namespace:
    """CLI-shaped namespace with every option `open_storage()` and `attach_storage_*` read."""
    base: dict[str, object] = {
        "skip_verify": True,
        "skip_verify_ssl": True,
        "skip_verify_gpg": True,
        "target_url": "fs:///tmp/repo bookworm main",
        "s3_access": "k",
        "s3_secret": "s",
        "s3_default_acl": "public-read",
        "s3_bucket_acl": None,
    }
    base.update(overrides)
    return Namespace(**base)
