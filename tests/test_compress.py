"""Coverage for `debkit.repo.compress` codecs."""

from __future__ import annotations

import gzip
import io
import lzma
import random
import struct
import subprocess
import tracemalloc
import zlib
from collections.abc import Buffer, Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from helpers.binaries import requires_binary
from txstore import StorageException, StorageMetrics

from debkit.repo.compress import (
    CODECS,
    Codec,
    CorruptStreamError,
    SizeLimitError,
    _bounded,
    codec_for,
    read_capped,
    zstd_decompress,
)

_DATA = b"Package: foo\nVersion: 1.0\n" * 32
_COMPRESSED = [codec for codec in CODECS if codec.suffix]
_BOMB_SIZE = 32 << 20  # past every decoder's working set, the 8 MiB xz dictionary included


def _ids(codec: Codec) -> str:
    return codec.suffix or "plain"


@pytest.mark.parametrize("codec", CODECS, ids=_ids)
def test_codec_round_trips_data(codec: Codec) -> None:
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
    """Any NUL run between streams is skipped for every codec, a deliberate leniency."""
    encoded = (codec.compress(b"x") + bytes(3)) * 50
    assert codec.decompress(encoded, 50) == b"x" * 50


def _fed(members: int) -> int:
    """Decode `members` tiny gzip members; return how many input bytes the decoders were fed."""
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

    encoded = gzip.compress(b"x", mtime=0) * members
    assert _bounded(_Counting, encoded, members) == b"x" * members
    return sum(fed)


def test_many_streams_are_fed_to_the_decoders_in_linear_total() -> None:
    """Many tiny gzip members are not each handed the whole remaining input."""
    # doubling the members doubles a linear total and quadruples a quadratic one;
    # below about 1000 members the per-member constant hides the difference
    assert _fed(2000) < 2.5 * _fed(1000)


# incompressible, so its stream spans several doubling chunks
_NOISE = random.Random(0).randbytes(4096)


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_reads_a_stream_spanning_many_chunks(codec: Codec) -> None:
    encoded = codec.compress(_NOISE) + codec.compress(b"tail")
    assert codec.decompress(encoded, len(_NOISE) + 4) == _NOISE + b"tail"


def test_decompress_reads_a_stream_ending_on_a_chunk_boundary() -> None:
    first = gzip.compress(bytes(233), compresslevel=0, mtime=0)
    assert len(first) == 256  # exactly the first chunk
    encoded = first + gzip.compress(b"tail", mtime=0)
    assert codec_for(".gz").decompress(encoded, 237) == bytes(233) + b"tail"


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_counts_the_limit_across_streams(codec: Codec) -> None:
    encoded = codec.compress(_NOISE) + codec.compress(b"tail")
    with pytest.raises(SizeLimitError):
        codec.decompress(encoded, len(_NOISE) + 3)


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_refuses_junk_after_a_valid_stream(codec: Codec) -> None:
    with pytest.raises(CorruptStreamError):
        codec.decompress(codec.compress(_DATA) + b"junk", len(_DATA) + 4)


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_refuses_a_truncated_stream(codec: Codec) -> None:
    with pytest.raises(CorruptStreamError):
        codec.decompress(codec.compress(_DATA)[:-8], len(_DATA))


@pytest.mark.parametrize("codec", _COMPRESSED, ids=_ids)
def test_decompress_refuses_data_that_does_not_decode(codec: Codec) -> None:
    with pytest.raises(CorruptStreamError):
        codec.decompress(b"not a compressed index", len(_DATA))


def _xz_with_dictionary(prop: int) -> bytes:
    """An xz stream of `_DATA` whose block header asks for the LZMA2 dictionary `prop` codes."""
    encoded = bytearray(codec_for(".xz").compress(_DATA))
    # stream header (12 bytes), then a 12-byte block header: size, flags, LZMA2 filter id and
    # props size, the dictionary prop, padding, and the CRC32 of the 8 bytes before it
    assert encoded[12:16] == b"\x02\x00\x21\x01"
    encoded[16] = prop
    encoded[20:24] = zlib.crc32(encoded[12:20]).to_bytes(4, "little")
    return bytes(encoded)


def test_xz_decodes_the_largest_preset_dictionary() -> None:
    """Preset -9's 64 MiB dictionary (prop 28) stays under the decoder's memory limit."""
    assert codec_for(".xz").decompress(_xz_with_dictionary(28), len(_DATA)) == _DATA


@pytest.mark.parametrize(
    ("path", "encoded"),
    [
        # a 1.5 GiB LZMA2 dictionary (prop 37)
        ("Packages.xz", _xz_with_dictionary(37)),
        # an LZMA-alone header asking for a 4 GiB dictionary
        (
            "Packages.lzma",
            b"\x5d" + struct.pack("<I", 0xFFFFFFFF) + codec_for(".lzma").compress(_DATA)[5:],
        ),
    ],
    ids=["xz", "lzma-alone"],
)
def test_lzma_refuses_a_dictionary_past_the_memory_limit(path: str, encoded: bytes) -> None:
    with pytest.raises(CorruptStreamError, match="Memory usage limit"):
        codec_for(path).decompress(encoded, len(_DATA))


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


# ---------------------------------------------------------------------------
# read_capped
# ---------------------------------------------------------------------------


class _CountingStream(io.BytesIO):
    """A stream that records how many bytes were read from it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_total = 0

    def read(self, size: int | None = -1, /) -> bytes:
        chunk = super().read(size)
        self.read_total += len(chunk)
        return chunk


def _capped_storage(data: bytes) -> tuple[MagicMock, _CountingStream]:
    stream = _CountingStream(data)
    storage = MagicMock()
    storage.get_stream.return_value = stream
    storage.metrics = StorageMetrics()
    return storage, stream


def test_read_capped_returns_an_object_at_the_limit() -> None:
    storage, _ = _capped_storage(b"x" * 10)

    assert read_capped(storage, "p", 10) == b"x" * 10
    assert storage.metrics.read_bytes == 10


def test_read_capped_refuses_an_object_past_the_limit_without_reading_it_all() -> None:
    storage, stream = _capped_storage(b"x" * (3 << 20))

    with pytest.raises(SizeLimitError, match="larger than 10 bytes"):
        read_capped(storage, "p", 10)
    assert stream.read_total == 11
    assert stream.closed


class _FailingStream(io.BytesIO):
    def read(self, size: int | None = -1, /) -> bytes:
        raise OSError("disk gone")


def test_read_capped_maps_a_read_fault_as_txstore_get_does() -> None:
    storage = MagicMock()
    storage.get_stream.return_value = _FailingStream()
    storage.metrics = StorageMetrics()

    with pytest.raises(StorageException, match="Error reading p: disk gone"):
        read_capped(storage, "p", 10)
    assert storage.metrics.read_errors_total == 1


# ---------------------------------------------------------------------------
# zstd: the subprocess wrapper, driven by a stand-in `zstd` on PATH
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_zstd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Install a `zstd` shell script with the given body as the only binary on PATH."""

    def install(body: str) -> None:
        script = tmp_path / "bin" / "zstd"
        script.parent.mkdir(exist_ok=True)
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", str(script.parent))

    return install


def test_zstd_decompress_returns_what_the_decoder_writes(
    fake_zstd: Callable[[str], None],
) -> None:
    fake_zstd("exec /bin/cat")

    assert zstd_decompress(_DATA, len(_DATA)) == _DATA


def test_zstd_decompress_runs_the_decoder_without_debkit_variables(
    fake_zstd: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decoder reads an unverified upload, so it never holds a secret."""
    monkeypatch.setenv("DEBKIT_GNUPG_PASSPHRASE", "x")
    monkeypatch.setenv("TXSTORE_S3_SECRET", "x")
    fake_zstd('[ -n "$DEBKIT_GNUPG_PASSPHRASE$TXSTORE_S3_SECRET" ] && exit 1; exec /bin/cat')

    assert zstd_decompress(_DATA, len(_DATA)) == _DATA


def test_zstd_decompress_refuses_a_stream_the_decoder_rejects(
    fake_zstd: Callable[[str], None],
) -> None:
    fake_zstd("/bin/cat >/dev/null; exit 1")

    with pytest.raises(CorruptStreamError, match="exited with status 1"):
        zstd_decompress(_DATA, len(_DATA))


def test_zstd_decompress_kills_a_decoder_writing_past_the_limit(
    fake_zstd: Callable[[str], None],
) -> None:
    """The decoder never ends on its own, so only the kill at the cap returns."""
    fake_zstd("exec /usr/bin/yes")

    with pytest.raises(SizeLimitError, match="past 1024 bytes"):
        zstd_decompress(_DATA, 1024)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_zstd_decompress_feeds_a_killed_decoder_without_a_thread_traceback(
    fake_zstd: Callable[[str], None],
) -> None:
    """Input larger than the pipe buffer is still being written when the cap kills the
    decoder, so the writer thread sees a broken pipe and ends quietly."""
    fake_zstd("exec /usr/bin/yes")

    with pytest.raises(SizeLimitError, match="past 1024 bytes"):
        zstd_decompress(b"x" * (4 << 20), 1024)


def test_zstd_decompress_refuses_when_no_decoder_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(CorruptStreamError, match="cannot run zstd"):
        zstd_decompress(_DATA, len(_DATA))


# ---------------------------------------------------------------------------
# zstd: the real decoder, for what a stand-in cannot show
# ---------------------------------------------------------------------------


def _zstd(data: bytes) -> bytes:
    return subprocess.run(["zstd", "-c", "-q"], input=data, capture_output=True, check=True).stdout


@requires_binary("zstd")
def test_zstd_decompress_real_decoder_round_trips() -> None:
    assert zstd_decompress(_zstd(_DATA), len(_DATA)) == _DATA


@requires_binary("zstd")
def test_zstd_decompress_real_decoder_refuses_a_truncated_frame() -> None:
    with pytest.raises(CorruptStreamError, match="exited with status"):
        zstd_decompress(_zstd(_DATA)[:-8], len(_DATA))


@requires_binary("zstd")
def test_zstd_decompress_real_decoder_refuses_garbage() -> None:
    with pytest.raises(CorruptStreamError, match="exited with status"):
        zstd_decompress(b"not a zstd frame" * 4, len(_DATA))


@requires_binary("zstd")
def test_zstd_decompress_real_decoder_stops_a_bomb_at_the_limit() -> None:
    bomb = _zstd(bytes(_BOMB_SIZE))
    assert len(bomb) < 1 << 16

    with pytest.raises(SizeLimitError, match="past 1024 bytes"):
        zstd_decompress(bomb, 1024)
