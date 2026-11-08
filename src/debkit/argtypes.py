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
from dataclasses import dataclass, field
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

# First character of a regex branch that may start with any character.
_ANY_CHAR = ""
# Quantifiers that repeat their atom without an upper bound; ``{`` stands for ``{n,}``.
_REPEATS = frozenset("+*{")


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
            too long, risks catastrophic backtracking, or does not compile.

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
        try:
            _check_regex_safe(pattern)
            compiled[name] = re.compile(pattern)
        except re.error as exc:
            raise ArgumentTypeError(f"{name}: invalid regex pattern: {exc}") from None
        except ValueError as exc:
            raise ArgumentTypeError(f"{name}: {exc}") from None
    return compiled


@dataclass
class _Group:
    """Scan state of one open regex group.

    Attributes:
        has_quantifier: A quantifier repeats something inside the group.
        has_overlap: The group or a group inside it has alternatives that can start alike.
        firsts: First character of each closed alternative; `_ANY_CHAR` when unknown.
        branch_open: The current alternative has no first character yet.
    """

    has_quantifier: bool = False
    has_overlap: bool = False
    firsts: list[str] = field(default_factory=list)
    branch_open: bool = True

    def note_first(self, char: str) -> None:
        """Record `char` as the first character of the current alternative, once."""
        if self.branch_open:
            self.firsts.append(char)
            self.branch_open = False

    def end_branch(self) -> None:
        """Close the current alternative; an empty one matches anywhere."""
        self.note_first(_ANY_CHAR)
        self.branch_open = True

    def alternatives_overlap(self) -> bool:
        """Return True if two alternatives can start with the same character."""
        return len(self.firsts) > 1 and (
            _ANY_CHAR in self.firsts or len(set(self.firsts)) < len(self.firsts)
        )


def _check_regex_safe(pattern: str) -> None:
    """Reject a pattern that is too long or risks catastrophic backtracking.

    A heuristic for operator-supplied patterns, not a proof of linear matching →
    [VALIDATION.md](VALIDATION.md#package-pattern-matching).

    Raises:
        ValueError: If the pattern is too long, nests quantifiers, or repeats a group whose
            alternatives can start with the same character.
    """
    if len(pattern) > PKG_PATTERN_MAX_LENGTH:
        raise ValueError(
            f"Regex pattern too long ({len(pattern)} chars, max {PKG_PATTERN_MAX_LENGTH})."
        )
    groups: list[_Group] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            escaped = pattern[i + 1 : i + 2]
            _note_first(groups, _ANY_CHAR if escaped.isalnum() or not escaped else escaped)
            i += 2
        elif char == "[":
            _note_first(groups, _ANY_CHAR)
            i = _skip_class(pattern, i)
        elif char == "(":
            _note_first(groups, _ANY_CHAR)
            groups.append(_Group())
            i = _skip_group_prefix(pattern, i + 1)
        elif char == ")":
            _close_group(groups, _quantifier_at(pattern, i + 1))
            i += 1
        elif char == "|":
            if groups:
                groups[-1].end_branch()
            i += 1
        elif _quantifier_at(pattern, i) is not None:
            if groups:
                groups[-1].has_quantifier = True
            i += 1
        else:
            _note_first(groups, _ANY_CHAR if char in ".^$" else char)
            i += 1


def _note_first(groups: list[_Group], char: str) -> None:
    """Record `char` as the first character of the innermost group's current alternative."""
    if groups:
        groups[-1].note_first(char)


def _close_group(groups: list[_Group], quantifier: str | None) -> None:
    """Close the innermost group, followed by `quantifier`, and fold it into its parent.

    Raises:
        ValueError: If a quantifier repeats a group holding one, or an unbounded quantifier
            repeats a group whose alternatives can start alike.
    """
    group = groups.pop() if groups else _Group()
    group.end_branch()
    overlap = group.has_overlap or group.alternatives_overlap()
    if group.has_quantifier and quantifier is not None:
        raise ValueError(
            "Regex pattern rejected: nested quantifiers are not allowed (potential ReDoS)."
        )
    if overlap and quantifier in _REPEATS:
        raise ValueError(
            "Regex pattern rejected: repeated alternatives that can match alike (potential ReDoS)."
        )
    if groups:
        groups[-1].has_quantifier |= group.has_quantifier
        groups[-1].has_overlap |= overlap


def _quantifier_at(pattern: str, i: int) -> str | None:
    """Return the quantifier starting at `i`: ``+``, ``*``, ``?``, ``{`` for ``{n,}``, or None.

    A bounded ``{n}`` or ``{n,m}`` is not a quantifier here: it cannot backtrack without bound.
    """
    char = pattern[i : i + 1]
    if char in ("+", "*", "?"):
        return char
    if char == "{" and re.match(r"\{\d*,\}", pattern[i:]):
        return char
    return None


def _skip_class(pattern: str, i: int) -> int:
    """Return the index after the character class opening at `i`."""
    i += 1
    if pattern[i : i + 1] == "^":
        i += 1
    if pattern[i : i + 1] == "]":
        i += 1
    while i < len(pattern) and pattern[i] != "]":
        i += 2 if pattern[i] == "\\" else 1
    return i + 1


def _skip_group_prefix(pattern: str, i: int) -> int:
    """Return the index after the extension prefix of a group whose body starts at `i`.

    Skips ``?:``, ``?=``, ``?!``, ``?>``, ``?<=``, ``?<!``, ``?P<name>``, and inline flags, so
    the prefix is read neither as a quantifier nor as the first character of an alternative.
    """
    if not pattern.startswith("?", i):
        return i
    if pattern.startswith("?P<", i):
        end = pattern.find(">", i)
        return len(pattern) if end < 0 else end + 1
    if pattern.startswith(("?<=", "?<!"), i):
        return i + 3
    i += 1
    while i < len(pattern) and pattern[i] in "aiLmsux-":
        i += 1
    if pattern[i : i + 1] in (":", "=", "!", ">"):
        i += 1
    return i
