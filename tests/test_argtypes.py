"""Coverage for `debkit.argtypes` CLI type converters → [VALIDATION.md](../VALIDATION.md)."""

from __future__ import annotations

import json
import re
from argparse import ArgumentTypeError

import pytest

from debkit.argtypes import (
    HISTORY_MAX_AGE_LIMIT,
    PKG_PATTERN_MAX_LENGTH,
    bucket_acl_type,
    component_type,
    history_max_age_type,
    object_acl_type,
    pkg_pattern_type,
    positive_int_type,
)

# ---------- history retention


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("20", 20), (" 7 ", 7), ("999", 999)])
def test_positive_int_type_accepts_one_or_greater(value: str, expected: int) -> None:
    assert positive_int_type(value) == expected


@pytest.mark.parametrize(
    "value", ["0", "-1", "", "  ", "abc", "1.5", "1_0", "١", "+-5", "--5", "-+5"]
)
def test_positive_int_type_rejects_non_positive_and_non_decimal(value: str) -> None:
    with pytest.raises(ArgumentTypeError):
        positive_int_type(value)


@pytest.mark.parametrize(
    ("value", "expected"), [("0", 0), ("1", 1), ("90", 90), (str(HISTORY_MAX_AGE_LIMIT), 36500)]
)
def test_history_max_age_type_accepts_its_range(value: str, expected: int) -> None:
    assert history_max_age_type(value) == expected


@pytest.mark.parametrize("value", ["-1", "", "abc", str(HISTORY_MAX_AGE_LIMIT + 1), "+-5", "--5"])
def test_history_max_age_type_rejects_out_of_range_and_non_numeric(value: str) -> None:
    with pytest.raises(ArgumentTypeError):
        history_max_age_type(value)


# ---------- S3 canned ACLs


@pytest.mark.parametrize("value", ["none", "private", "public-read", "bucket-owner-full-control"])
def test_object_acl_type_accepts_none_and_object_acls(value: str) -> None:
    assert object_acl_type(value) == value


@pytest.mark.parametrize("value", ["", "None", "public"])
def test_object_acl_type_rejects_unknown_value(value: str) -> None:
    with pytest.raises(ArgumentTypeError):
        object_acl_type(value)


@pytest.mark.parametrize("value", ["private", "public-read"])
def test_bucket_acl_type_accepts_bucket_acls(value: str) -> None:
    assert bucket_acl_type(value) == value


@pytest.mark.parametrize("value", ["none", "bucket-owner-full-control"])
def test_bucket_acl_type_rejects_non_bucket_acl(value: str) -> None:
    with pytest.raises(ArgumentTypeError):
        bucket_acl_type(value)


# ---------- package pattern


def test_pkg_pattern_type_compiles_each_field() -> None:
    assert pkg_pattern_type('{"Package": "^foo$", "Version": "1"}') == {
        "Package": re.compile("^foo$"),
        "Version": re.compile("1"),
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("{bad", "invalid JSON"),
        ('["a"]', "JSON object"),
        ('"a"', "JSON object"),
        ("{}", "non-empty"),
        ('{"Package": 5}', "must be a string"),
        ('{"Package": ["a"]}', "must be a string"),
        ('{"Package": "("}', "invalid regex pattern"),
    ],
)
def test_pkg_pattern_type_rejects_bad_shape(value: str, message: str) -> None:
    with pytest.raises(ArgumentTypeError, match=message):
        pkg_pattern_type(value)


def test_pkg_pattern_type_rejects_too_long_pattern() -> None:
    value = '{"Package": "%s"}' % ("a" * (PKG_PATTERN_MAX_LENGTH + 1))
    with pytest.raises(ArgumentTypeError, match="too long"):
        pkg_pattern_type(value)


@pytest.mark.parametrize(
    "pattern",
    [
        "foo|bar",
        r"filename_\d+\.deb",
        r"^pool/.*\.deb$",
        "simple",
        "(foo|bar)+",
        r"lib[a-z]+_\d+",
        "(one|two|three)",
        "(?:foo)+",
        "(?:a|b)*",
        "(?=foo)+",
        "(?!bar)*",
        r"(\d{3})+",
        "(ab{2,4})+",
        "(a|a)?",
        "(?P<name>foo|bar)+",
        "(?i:foo|bar)+",
        "(a+)+$",
    ],
)
def test_pkg_pattern_type_accepts_compilable_patterns(pattern: str) -> None:
    assert pkg_pattern_type(json.dumps({"Package": pattern})) == {"Package": re.compile(pattern)}


@pytest.mark.parametrize("value", ["main", "updates/main", "non-free-firmware"])
def test_component_type_accepts_a_safe_name(value: str) -> None:
    assert component_type(value) == value


@pytest.mark.parametrize("value", ["", ".", "..", "../x", "/abs", "main?x"])
def test_component_type_rejects_an_unsafe_name(value: str) -> None:
    with pytest.raises(ArgumentTypeError, match="unsafe component name"):
        component_type(value)
