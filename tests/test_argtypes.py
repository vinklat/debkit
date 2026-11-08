"""Coverage for `debkit.argtypes` CLI type converters → [VALIDATION.md](../VALIDATION.md)."""

from __future__ import annotations

from argparse import ArgumentTypeError

import pytest

from debkit.argtypes import (
    HISTORY_MAX_AGE_LIMIT,
    bucket_acl_type,
    history_max_age_type,
    object_acl_type,
    positive_int_type,
)

# ---------- history retention


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("20", 20), (" 7 ", 7), ("999", 999)])
def test_positive_int_type_accepts_one_or_greater(value: str, expected: int) -> None:
    assert positive_int_type(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "", "  ", "abc", "1.5", "1_0", "١"])
def test_positive_int_type_rejects_non_positive_and_non_decimal(value: str) -> None:
    with pytest.raises(ArgumentTypeError):
        positive_int_type(value)


@pytest.mark.parametrize(
    ("value", "expected"), [("0", 0), ("1", 1), ("90", 90), (str(HISTORY_MAX_AGE_LIMIT), 36500)]
)
def test_history_max_age_type_accepts_its_range(value: str, expected: int) -> None:
    assert history_max_age_type(value) == expected


@pytest.mark.parametrize("value", ["-1", "", "abc", str(HISTORY_MAX_AGE_LIMIT + 1)])
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
