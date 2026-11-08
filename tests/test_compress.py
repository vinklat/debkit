"""Coverage for `debkit.repo.compress` codecs."""

from __future__ import annotations

import lzma

import pytest

from debkit.repo.compress import CODECS, Codec, codec_for

_DATA = b"Package: foo\nVersion: 1.0\n" * 32


@pytest.mark.parametrize("codec", CODECS, ids=lambda codec: codec.suffix or "plain")
def test_round_trip(codec: Codec) -> None:
    encoded = codec.compress(_DATA)
    assert (encoded != _DATA) is bool(codec.suffix)  # compressed unless uncompressed
    assert codec.decompress(encoded) == _DATA


def test_lzma_writes_the_lzma_alone_format() -> None:
    """`.lzma` is LZMA-alone, not xz: apt rejects an xz stream under that name."""
    encoded = codec_for("Packages.lzma").compress(_DATA)
    assert lzma.decompress(encoded, format=lzma.FORMAT_ALONE) == _DATA


def test_lzma_still_reads_an_xz_stream() -> None:
    """An `.lzma` an older debkit wrote in xz format stays readable."""
    assert codec_for("Packages.lzma").decompress(lzma.compress(_DATA)) == _DATA


@pytest.mark.parametrize(
    ("path", "suffix"),
    [
        ("dists/d/main/binary-amd64/Packages.xz", ".xz"),
        ("Packages.lzma", ".lzma"),
        ("Sources.bz2", ".bz2"),
        ("Packages.gz", ".gz"),
        ("dists/d/main/binary-amd64/Packages", ""),
    ],
)
def test_codec_for_matches_the_suffix(path: str, suffix: str) -> None:
    assert codec_for(path).suffix == suffix


def test_uncompressed_codec_is_discovered_last() -> None:
    assert CODECS[-1].suffix == ""
    assert [codec.suffix for codec in CODECS[:-1]] == [".xz", ".lzma", ".bz2", ".gz"]
