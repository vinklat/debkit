"""Root logger configuration for the CLI.

Only `cli.main` calls `configure_logging`; every other module logs through its own logger and
never configures handlers → [STYLE.md](STYLE.md#logging). It imports only the `_escape` leaf.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum

from debkit._escape import escape_control

LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

# Chatty third-party loggers held at INFO when the root runs at DEBUG.
QUIET_LOGGERS = ("urllib3", "botocore", "boto3", "s3transfer")

_PLAIN_FORMAT = "%(levelname)s %(message)s"
_DEBUG_FORMAT = "%(levelname)s %(module)s %(funcName)s %(message)s"


class LogFormat(StrEnum):
    """Log line format."""

    plain = "plain"
    json = "json"


class _PlainFormatter(logging.Formatter):
    """Emit one plain line per record with control characters escaped.

    Repository content in a message (package names, Release fields, URLs) could otherwise carry a
    newline or carriage return that forges an extra entry, or an ANSI escape that rewrites the
    terminal (CWE-117). A traceback still spans several lines; `LogFormat.json` keeps it on one.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        r"""Format `record` through the template, then escape its control characters.

        Returns:
            The formatted line with each C0/C1 control except tab replaced by ``\xNN``.
        """
        return escape_control(super().formatMessage(record))


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record with escaped fields.

    A ``%(message)s`` template does not escape quotes, backslashes, or control characters, so
    repository content in a message (package names, Release fields, URLs) could forge extra
    entries in a line-based log shipper (CWE-117); `json.dumps` keeps each record one line.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format `record` as a single-line JSON object.

        Returns:
            JSON with ``time``, ``name``, ``level``, and ``message``, plus ``exception`` and
            ``stack`` when the record carries them.
        """
        payload: dict[str, object] = {
            "time": self.formatTime(record),
            "name": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str, log_format: LogFormat = LogFormat.plain) -> None:
    """Configure the root logger on stderr, replacing any handler already installed.

    Python warnings are captured into the ``py.warnings`` logger, so they reach the same handler.

    Args:
        level: One of `LOG_LEVELS`.
        log_format: Plain lines (module and function added at DEBUG) or one JSON object per line.
    """
    logging.captureWarnings(True)
    numeric = logging.getLevelNamesMapping()[level]
    handler = logging.StreamHandler()
    if log_format is LogFormat.json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _PlainFormatter(_DEBUG_FORMAT if numeric <= logging.DEBUG else _PLAIN_FORMAT)
        )
    logging.basicConfig(level=numeric, handlers=[handler], force=True)
    if numeric <= logging.DEBUG:
        for name in QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)
