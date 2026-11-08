"""Transaction history actions (log, revert, prune)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from txstore import (
    HistoryRetention,
    StorageException,
    TransactionHistory,
    TransactionModel,
    is_transactional,
)

from debkit.action import (
    Action,
    ActionException,
    OutputType,
    attach_storage_root,
    dump_json,
    history_retention,
)

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace


def _record(model: TransactionModel) -> dict[str, object]:
    """Return the history-file form of `model`: ``{"id", "timestamp"}``."""
    return {"id": model.id, "timestamp": model.timestamp.to_json()}


logging.getLogger(__name__).addHandler(logging.NullHandler())


class ActionHistory(Action):
    """Actions for transaction history: log, revert, and prune.

    Attributes:
        output (OutputType): Output format.
        dry_run (bool): If True, revert and prune print what would happen
            without applying.
        orphans (bool): If True, prune also sweeps footmark objects that belong
            to no recorded transaction.
        history_retention (HistoryRetention): Policy prune applies.
    """

    def __init__(self, pars: Namespace) -> None:
        """Initialize ActionHistory.

        Args:
            pars: Parsed CLI namespace.
        """
        self.output: OutputType = pars.output if hasattr(pars, "output") else OutputType.brief
        self.dry_run: bool = pars.dry_run if hasattr(pars, "dry_run") else False
        self.orphans: bool = getattr(pars, "orphans", False)
        self.history_retention: HistoryRetention = history_retention(pars)
        self._storage = attach_storage_root(pars)

    @contextmanager
    def _translate_storage_errors(self) -> Iterator[None]:
        """Map ``StorageException`` to ``ActionException`` for this command."""
        try:
            yield
        except StorageException as exc:
            raise ActionException(str(exc)) from exc

    def log(self) -> None:
        """Print transaction history.

        Raises:
            ActionException: If the storage backend does not support transactions,
                or the history file contains a malformed record.
        """
        self._assert_transactional()
        history = TransactionHistory(self._storage)
        with self._translate_storage_errors():
            records = history.load()

        if self.output == OutputType.json:
            print(dump_json([_record(rec) for rec in records]))
        else:
            if not records:
                print("No transactions recorded.")
                return
            for rec in records:
                print(f"  {rec.id:>4}  {rec.timestamp.to_json()}")

    def revert(self) -> None:
        """Revert the latest committed transaction.

        Raises:
            ActionException: If history is empty or backend is non-transactional.
        """
        self._assert_transactional()
        history = TransactionHistory(self._storage)

        if self.dry_run:
            with self._translate_storage_errors():
                records = history.load()
                if not records:
                    raise ActionException("No transactions to revert.")
                last = records[-1]
            print(f"Would revert transaction {last.id} ({last.timestamp.to_json()})")
            return

        with self._translate_storage_errors():
            removed = history.revert_last()
        if removed is None:
            raise ActionException("No transactions to revert.")
        print(f"Reverted transaction {removed.id}")

    def prune(self, now: datetime | None = None) -> None:
        """Prune expired transactions, and orphaned footmarks when asked.

        Retention comes from the global flags → [CONFIG.md](CONFIG.md#prune);
        the rule it applies → [STORAGE.md](STORAGE.md#history-commands).

        Args:
            now: Reference time for record age; None means the current time.

        Raises:
            ActionException: If the storage backend does not support
                transactions, or the history cannot be read or rewritten.
        """
        self._assert_transactional()
        history = TransactionHistory(self._storage)

        with self._translate_storage_errors():
            result = history.prune(
                self.history_retention,
                now=now,
                orphans=self.orphans,
                dry_run=self.dry_run,
            )

        if self.output == OutputType.json:
            print(
                dump_json(
                    {
                        "transactions": [_record(rec) for rec in result.records],
                        "footmarks": result.footmark_paths,
                        "orphans": result.orphans,
                    }
                )
            )
            return

        verb = "Would remove" if self.dry_run else "Removed"
        for rec in result.records:
            print(f"  {verb} transaction {rec.id}  {rec.timestamp.to_json()}")
        for path in result.footmark_paths:
            print(f"  {verb} footmark {path}")
        for path in result.orphans:
            print(f"  {verb} orphaned footmark {path}")
        if not result.records and not result.orphans:
            print("Nothing to prune.")

    def _assert_transactional(self) -> None:
        """Guard: raise if backend lacks transaction support."""
        if not is_transactional(self._storage):
            raise ActionException("Storage backend does not support transactions.")

    @staticmethod
    def add_dry_run_cli_arg(parser: ArgumentParser) -> None:
        """Register `--dry-run`, shared by the revert and prune subcommands.

        Args:
            parser: The argument parser for a history subcommand.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            default=False,
            help="show what would be changed without applying",
        )

    @staticmethod
    def add_prune_cli_args(parser: ArgumentParser) -> None:
        """Add prune-specific CLI arguments.

        Args:
            parser: The argument parser for the prune subcommand.
        """
        parser.add_argument(
            "--orphans",
            action="store_true",
            dest="orphans",
            default=False,
            help="also remove footmark objects belonging to no recorded transaction",
        )
