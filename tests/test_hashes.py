"""Debian hash blocks and `HexDigests` in `debkit.repo.hashes` (CRYPTO.md#checksums)."""

from __future__ import annotations

import pytest
from txstore import calc_digests

from debkit.repo.hashes import HexDigests, has_sha, parse_hash_block, parse_size


def test_verify_digests_refuses_an_md5_only_entry() -> None:
    """A Release entry listing only MD5Sum cannot vouch for a file."""
    hexdigests = HexDigests()
    hexdigests.digests["main/binary-amd64/Packages"] = {"MD5Sum": calc_digests(b"x")["MD5Sum"]}
    assert hexdigests.verify_digests("main/binary-amd64/Packages", calc_digests(b"x")) is False


# --- parse_hash_block ----------------------------------------------------------------------------


def test_parse_hash_block_yields_one_triple_per_line() -> None:
    """A hash block is a run of `(digest, size, filename)` triples, in file order."""
    block = "\n aaaa 123 main/binary-amd64/Packages\n bbbb 7 main/i18n/Translation-en\n"

    assert parse_hash_block(block) == [
        ("aaaa", "123", "main/binary-amd64/Packages"),
        ("bbbb", "7", "main/i18n/Translation-en"),
    ]


def test_parse_hash_block_tolerates_column_padding() -> None:
    """`Release.serialize` right-aligns the size column; the split ignores runs."""
    block = "\n aaaa              123 Packages\n"

    assert parse_hash_block(block) == [("aaaa", "123", "Packages")]


def test_parse_hash_block_drops_a_trailing_partial_run() -> None:
    """A truncated final line is ignored rather than raising, as callers always did."""
    assert parse_hash_block("aaaa 1 x.deb bbbb 2") == [("aaaa", "1", "x.deb")]


def test_parse_hash_block_empty_text_yields_nothing() -> None:
    assert parse_hash_block("") == []
    assert parse_hash_block("   \n  ") == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12", 12), ("0", 0), ("12kB", None), ("\u0661\u0662", None), ("", None), (" 1", None)],
    ids=["digits", "zero", "suffix", "non-ascii-digits", "empty", "padded"],
)
def test_parse_size_reads_only_ascii_digits(value: str, expected: int | None) -> None:
    assert parse_size(value) == expected


# --- has_sha -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"MD5Sum": "a"}, False),
        ({}, False),
        ({"sha256": "a"}, False),
        ({"SHA1": "a"}, True),
        ({"SHA256": "a", "Size": 3}, True),
        ({"MD5Sum": "a", "SHA512": "b"}, True),
    ],
    ids=["md5-only", "empty", "lowercase-key", "sha1", "sha256-with-size", "md5-and-sha512"],
)
def test_has_sha_needs_an_algorithm_comparison_keeps(
    record: dict[str, object], expected: bool
) -> None:
    assert has_sha(record) is expected
