"""Coverage for `debkit.repo.compress` codecs."""

from __future__ import annotations

import gzip
import lzma
import tracemalloc
import zlib
from collections.abc import Buffer

import pytest

from debkit.repo.compress import CODECS, Codec, SizeLimitError, _bounded, codec_for

_DATA = b"Package: foo\nVersion: 1.0\n" * 32
_COMPRESSED = [codec for codec in CODECS if codec.suffix]
_BOMB_SIZE = 32 << 20  # past every decoder's working set, the 8 MiB xz dictionary included


def _ids(codec: Codec) -> str:
    return codec.suffix or "plain"


@pytest.mark.parametrize("codec", CODECS, ids=_ids)
def test_round_trip(codec: Codec) -> None:
    encoded = codec.compress(_DATA)
    assert (encoded != _DATA) is bool(codec.suffix)  # compressed unless uncompressed
    assert codec.decompress(encoded, len(_DATA)) == _DATA


@pytest.mark.parametrize("codec", CODECS, ids=_ids)
def test_decompress_refuses_output_past_the_limit(codec: Codec) -> None:
    with pytest.raises(SizeLimitError):
        codec.decompress(codec.compress(_DATA), len(_DATA) - 1)


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_stops_a_bomb_before_inflating_it(codec: Codec) -> None:
    """The decoder stops at the limit: memory stays far below the inflated size."""
    bomb = codec.compress(bytes(_BOMB_SIZE))
    tracemalloc.start()
    try:
        with pytest.raises(SizeLimitError):
            codec.decompress(bomb, 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < _BOMB_SIZE // 2


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_reads_concatenated_streams(codec: Codec) -> None:
    """Multi-stream xz/bz2 and multi-member gzip decode in full, as the one-shot decoders do."""
    encoded = codec.compress(_DATA) + codec.compress(b"tail")
    assert codec.decompress(encoded, len(_DATA) + 4) == _DATA + b"tail"


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_skips_nul_padding_between_many_streams(codec: Codec) -> None:
    encoded = (codec.compress(b"x") + bytes(3)) * 50
    assert codec.decompress(encoded, 50) == b"x" * 50


def test_many_streams_are_fed_to_the_decoders_in_linear_total() -> None:
    """Many tiny gzip members are not each handed the whole remaining input."""
    fed: list[int] = []

    class _Counting:
        def __init__(self) -> None:
            self._inner = zlib.decompressobj(wbits=31)

        @property
        def eof(self) -> bool:
            return self._inner.eof

        @property
        def unused_data(self) -> bytes:
            return self._inner.unused_data

        def decompress(self, data: Buffer, max_length: int, /) -> bytes:
            fed.append(len(memoryview(data)))
            return self._inner.decompress(data, max_length)

    encoded = gzip.compress(b"x", mtime=0) * 2000
    assert _bounded(_Counting, encoded, 2000) == b"x" * 2000
    # handing each member the rest of the input totals about 1000x the input
    assert sum(fed) <= 16 * len(encoded)


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_refuses_a_truncated_stream(codec: Codec) -> None:
    with pytest.raises(EOFError):
        codec.decompress(codec.compress(_DATA)[:-8], len(_DATA))


def test_xz_skips_stream_padding() -> None:
    encoded = codec_for(".xz").compress(_DATA) + bytes(4)
    assert codec_for(".xz").decompress(encoded, len(_DATA)) == _DATA


def test_lzma_writes_the_lzma_alone_format() -> None:
    """`.lzma` is LZMA-alone, not xz: apt rejects an xz stream under that name."""
    encoded = codec_for("Packages.lzma").compress(_DATA)
    assert lzma.decompress(encoded, format=lzma.FORMAT_ALONE) == _DATA


def test_lzma_still_reads_an_xz_stream() -> None:
    """An `.lzma` an older debkit wrote in xz format stays readable."""
    assert codec_for("Packages.lzma").decompress(lzma.compress(_DATA), len(_DATA)) == _DATA


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
