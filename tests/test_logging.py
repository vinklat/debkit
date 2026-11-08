"""Coverage for `debkit._logging.configure_logging` → [CONFIG.md](../CONFIG.md#global-flags)."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator

import pytest

from debkit._logging import QUIET_LOGGERS, LogFormat, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quiet = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, level in quiet.items():
        logging.getLogger(name).setLevel(level)
    logging.captureWarnings(False)


def _format(record_message: str, level: int = logging.INFO) -> str:
    handler = logging.getLogger().handlers[0]
    record = logging.LogRecord("debkit.x", level, __file__, 1, record_message, None, None, "fn")
    return handler.format(record)


def test_plain_format_above_debug() -> None:
    configure_logging("INFO")
    assert logging.getLogger().level == logging.INFO
    assert _format("hello") == "INFO hello"


def test_debug_adds_module_and_function_and_quiets_third_party() -> None:
    configure_logging("DEBUG")
    assert _format("hello", logging.DEBUG) == "DEBUG test_logging fn hello"
    assert all(logging.getLogger(name).level == logging.INFO for name in QUIET_LOGGERS)


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_plain_lines_escape_control_characters(level: str) -> None:
    configure_logging(level)
    line = _format("pkg\r\nINFO forged\x1b[2K\x85\tend", logging.getLevelNamesMapping()[level])
    assert line.endswith("pkg\\x0d\\x0aINFO forged\\x1b[2K\\x85\tend")
    assert line.replace("\t", "").isprintable()


def test_json_lines_escape_forged_entries() -> None:
    configure_logging("WARNING", LogFormat.json)
    line = _format("pkg\nINFO forged", logging.WARNING)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["level"] == "WARNING"
    assert payload["message"] == "pkg\nINFO forged"
    assert payload["name"] == "debkit.x"


def test_json_lines_carry_the_exception() -> None:
    configure_logging("ERROR", LogFormat.json)
    try:
        raise ValueError("boom")
    except ValueError:
        info = sys.exc_info()
    handler = logging.getLogger().handlers[0]
    record = logging.LogRecord("debkit.x", logging.ERROR, __file__, 1, "failed", None, info)
    assert "ValueError: boom" in json.loads(handler.format(record))["exception"]


def test_configure_replaces_existing_handlers() -> None:
    configure_logging("INFO")
    configure_logging("INFO")
    assert len(logging.getLogger().handlers) == 1


def test_unknown_level_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        configure_logging("TRACING")
