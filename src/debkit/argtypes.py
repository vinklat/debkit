"""CLI type converters for argparse flags and their ``DEBKIT_*`` fallbacks.

Each converter raises `ArgumentTypeError` (or `ValueError`), which argparse reports as a usage
error and the environment fallback reports naming the variable →
[VALIDATION.md](VALIDATION.md#principles). Imports only the standard library and the `txstore`
façade → [STRUCTURE.md](STRUCTURE.md#dependencies).
"""

from __future__ import annotations

from argparse import ArgumentTypeError
from typing import Literal

from txstore import (
    NO_ACL,
    BucketCannedACL,
    ObjectCannedACL,
    parse_bucket_acl,
    parse_object_acl,
)

# Upper bound of `--history-max-age`; `txstore.HistoryRetention` rejects larger values
# → [VALIDATION.md](VALIDATION.md#history-retention).
HISTORY_MAX_AGE_LIMIT = 36500


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
