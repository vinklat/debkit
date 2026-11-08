"""CLI type converters for argparse flags and their ``DEBKIT_*`` fallbacks.

Each converter raises `ArgumentTypeError` (or `ValueError`), which argparse reports as a usage
error and the environment fallback reports naming the variable →
[VALIDATION.md](VALIDATION.md#principles). Imports only the standard library and the `txstore`
façade → [STRUCTURE.md](STRUCTURE.md#dependencies).
"""

from __future__ import annotations

import json
import re
from argparse import ArgumentTypeError
from typing import Literal

from txstore import (
    NO_ACL,
    BucketCannedACL,
    ObjectCannedACL,
    is_unsafe_path,
    parse_bucket_acl,
    parse_object_acl,
)

# Upper bound of `--history-max-age`; `txstore.HistoryRetention` rejects larger values
# → [VALIDATION.md](VALIDATION.md#history-retention).
HISTORY_MAX_AGE_LIMIT = 36500

# Longest accepted `--pkg-pattern` regex → [VALIDATION.md](VALIDATION.md#package-pattern-matching).
PKG_PATTERN_MAX_LENGTH = 500


def _int_type(value: str, minimum: int, maximum: int | None = None) -> int:
    """Parse `value` as a decimal integer within ``minimum..maximum``.

    Args:
        value: Raw flag or environment value.
        minimum: Smallest accepted integer.
        maximum: Largest accepted integer; None for no bound.

    Raises:
        ArgumentTypeError: If `value` is not an integer in range.

    Returns:
        int: The parsed value.
    """
    stripped = str(value).strip()
    if not stripped.isascii() or not stripped.lstrip("+-").isdigit():
        raise ArgumentTypeError(f"invalid value: not an integer: {value!r}")
    parsed = int(stripped, 10)
    if parsed < minimum:
        raise ArgumentTypeError(f"invalid value: must be {minimum} or greater, got {parsed}")
    if maximum is not None and parsed > maximum:
        raise ArgumentTypeError(f"invalid value: must be {maximum} or less, got {parsed}")
    return parsed


def positive_int_type(value: str) -> int:
    """Parse a flag value that must be 1 or greater."""
    return _int_type(value, 1)


def history_max_age_type(value: str) -> int:
    """Parse a history age in days: ``0..HISTORY_MAX_AGE_LIMIT``, ``0`` disables pruning."""
    return _int_type(value, 0, HISTORY_MAX_AGE_LIMIT)


def component_type(value: str) -> str:
    """Parse a component name: non-empty, not ``.``, and no path-steering segment.

    The same rule as `is_safe_name()` in `repo/params.py`, which `argtypes` may not import
    → [VALIDATION.md](VALIDATION.md#debian-source-strings).
    """
    if value in ("", ".") or is_unsafe_path(value):
        raise ArgumentTypeError(f"unsafe component name: {value!r}")
    return value


def object_acl_type(value: str) -> ObjectCannedACL | Literal["none"]:
    """Parse an S3 object canned ACL, or ``none`` to send no ACL.

    Raises:
        ArgumentTypeError: If `value` is neither ``none`` nor an object canned ACL.
    """
    if value == NO_ACL:
        return NO_ACL
    try:
        return parse_object_acl(value)
    except ValueError as exc:
        raise ArgumentTypeError(str(exc)) from None


def bucket_acl_type(value: str) -> BucketCannedACL:
    """Parse an S3 bucket canned ACL.

    Raises:
        ArgumentTypeError: If `value` is not a bucket canned ACL.
    """
    try:
        return parse_bucket_acl(value)
    except ValueError as exc:
        raise ArgumentTypeError(str(exc)) from None


def pkg_pattern_type(value: str) -> dict[str, re.Pattern[str]]:
    """Parse a JSON object mapping package fields to regex patterns, and compile each pattern.

    Rules → [VALIDATION.md](VALIDATION.md#package-pattern-matching).

    Raises:
        ArgumentTypeError: If `value` is not a non-empty JSON object of strings, or a pattern is
            too long or does not compile.

    Returns:
        dict[str, re.Pattern[str]]: Compiled pattern per field.
    """
    try:
        fields = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ArgumentTypeError(f"invalid JSON: {exc}") from None
    if not isinstance(fields, dict) or not fields:
        raise ArgumentTypeError("must be a non-empty JSON object of field names to regex patterns")
    compiled: dict[str, re.Pattern[str]] = {}
    for name, pattern in fields.items():
        if not isinstance(pattern, str):
            raise ArgumentTypeError(
                f"{name}: pattern must be a string, not {type(pattern).__name__}"
            )
        if len(pattern) > PKG_PATTERN_MAX_LENGTH:
            raise ArgumentTypeError(
                f"{name}: pattern too long ({len(pattern)} chars, max {PKG_PATTERN_MAX_LENGTH})"
            )
        try:
            compiled[name] = re.compile(pattern)
        except re.error as exc:
            raise ArgumentTypeError(f"{name}: invalid regex pattern: {exc}") from None
    return compiled
