"""Compression codecs for Debian index files."""

from __future__ import annotations

import bz2
import gzip
import lzma
import re
import subprocess
import threading
import zlib
from collections.abc import Buffer, Callable
from functools import partial
from typing import IO, TYPE_CHECKING, NamedTuple, Protocol

from txstore import StorageException

if TYPE_CHECKING:
    from txstore import Storage


class SizeLimitError(ValueError):
    """Decoded data exceeds the size limit the caller set."""


class CorruptStreamError(ValueError):
    """Encoded data is truncated or does not decode."""


# the read size of `read_capped`; the cap, not the chunk, bounds what is buffered
_READ_CHUNK = 1 << 20


def read_capped(storage: Storage, path: str, limit: int) -> bytes:
    """Read a stored object, refusing it as soon as it passes `limit` bytes.

    Unlike `Storage.get`, which buffers the whole body, at most `limit + 1` bytes are read,
    so an oversized object is refused before it is verified or buffered in full
    -> [VALIDATION.md](VALIDATION.md#object-sizes).

    Args:
        storage: The storage holding the object.
        path: The object path.
        limit: The largest size accepted, in bytes.

    Raises:
        SizeLimitError: The object is larger than `limit`.
        StorageException: The object cannot be opened or read.

    Returns:
        The object content.
    """
    out = bytearray()
    with storage.get_stream(path) as stream:
        while len(out) <= limit:
            try:
                chunk = stream.read(min(_READ_CHUNK, limit + 1 - len(out)))
            except OSError as exc:
                # as txstore's own `get` maps it -> [ROADMAP.md](ROADMAP.md#known-deviations)
                storage.metrics.read_errors_inc()
                raise StorageException(f"Error reading {path}: {exc}") from exc
            size = len(out)
            out += chunk
            # a stream that yields no bytes has ended
            if len(out) == size:
                break
    storage.metrics.read_bytes_inc(len(out))
    if len(out) > limit:
        raise SizeLimitError(f"{path} is larger than {limit} bytes")
    return bytes(out)


class Codec(NamedTuple):
    """How an index file named with `suffix` is encoded and decoded.

    Attributes:
        suffix: File name suffix, empty for the uncompressed index.
        compress: Encoder.
        decompress: Decoder; refuses output larger than its second argument with
            `SizeLimitError` and a truncated or undecodable stream with `CorruptStreamError`.
    """

    suffix: str
    compress: Callable[[bytes], bytes]
    decompress: Callable[[bytes, int], bytes]


class _Decoder(Protocol):
    """The incremental decoder shape `lzma`, `bz2` and `zlib` share."""

    @property
    def eof(self) -> bool:
        """Whether the end-of-stream marker was reached."""

    @property
    def unused_data(self) -> bytes:
        """Input found after the end of the stream."""

    def decompress(self, data: Buffer, max_length: int, /) -> bytes:
        """Decode `data`, returning at most `max_length` bytes."""


# each chunk is at most the bytes already read in this stream plus the first chunk, so the
# bytes read past a stream's end stay within that stream's size (linear in total), and a
# large stream still needs few decoder calls
_FIRST_CHUNK = 256
_LARGEST_CHUNK = 1 << 20
_NON_NUL = re.compile(rb"[^\0]")


def _bounded(new_decoder: Callable[[], _Decoder], data: bytes, max_size: int) -> bytes:
    """Decode every concatenated stream in `data` without producing more than `max_size` bytes.

    Each decoder call is capped one byte past what is left, so a bomb stops as soon as it
    overflows instead of after it is fully inflated. Any run of NUL bytes between or after
    streams is skipped for every codec, a deliberate superset of xz's 4-byte stream padding
    and of what the one-shot `gzip.decompress` strips. The input is walked by offset in
    growing chunks, so many concatenated streams decode in linear time instead of copying
    the rest of the input at every stream end.

    Args:
        new_decoder: Factory of a fresh decoder per stream.
        data: The encoded data.
        max_size: The largest decoded size accepted.

    Raises:
        SizeLimitError: The decoded data exceeds `max_size`.
        CorruptStreamError: A stream ends before its end-of-stream marker or does not decode.

    Returns:
        The decoded data of every stream, concatenated.
    """
    view = memoryview(data)
    out = bytearray()
    offset = 0
    while offset < len(data):
        decoder = new_decoder()
        step = _FIRST_CHUNK
        while True:
            chunk = view[offset : offset + step]
            try:
                out += decoder.decompress(chunk, max_size + 1 - len(out))
            except (lzma.LZMAError, OSError, zlib.error) as exc:
                # bz2 reports garbage as OSError; the decoder does no I/O
                raise CorruptStreamError(str(exc)) from exc
            if len(out) > max_size:
                raise SizeLimitError(f"decoded data exceeds {max_size} bytes")
            # under the cap the decoder consumed the whole chunk unless the stream ended in
            # it; at the cap zlib keeps input in `unconsumed_tail`, which the offset
            # arithmetic below would skip, hence the cap check runs first
            if decoder.eof:
                offset += len(chunk) - len(decoder.unused_data)
                break
            offset += len(chunk)
            if offset >= len(data):
                raise CorruptStreamError("compressed data ended before the end-of-stream marker")
            step = min(step * 2, _LARGEST_CHUNK)
        match = _NON_NUL.search(data, offset)
        offset = match.start() if match else len(data)
    return bytes(out)


def _identity(data: bytes) -> bytes:
    """Return an uncompressed index as is."""
    return data


def _identity_bounded(data: bytes, max_size: int) -> bytes:
    """Return an uncompressed index as is, refusing one larger than `max_size`.

    Raises:
        SizeLimitError: `data` exceeds `max_size`.
    """
    if len(data) > max_size:
        raise SizeLimitError(f"data exceeds {max_size} bytes")
    return data


# preset -9 needs a 64 MiB dictionary; a larger one cannot help under `MAX_INDICES_BYTES`,
# so a stream asking for more fails as corrupt instead of allocating it
_LZMA_MEMLIMIT = 128 << 20

# `.lzma` is the legacy LZMA-alone format; decoding auto-detects, so an `.lzma`
# written in xz format by an older debkit still reads
_lzma_decompress = partial(
    _bounded, partial(lzma.LZMADecompressor, lzma.FORMAT_AUTO, memlimit=_LZMA_MEMLIMIT)
)

# discovery order: the preferred compression first, the uncompressed index last
# -> [VALIDATION.md](VALIDATION.md#repository-index-paths)
CODECS: tuple[Codec, ...] = (
    Codec(".xz", lzma.compress, _lzma_decompress),
    Codec(".lzma", partial(lzma.compress, format=lzma.FORMAT_ALONE), _lzma_decompress),
    Codec(".bz2", bz2.compress, partial(_bounded, bz2.BZ2Decompressor)),
    # wbits=31 makes zlib parse the gzip header and check its trailer
    Codec(".gz", gzip.compress, partial(_bounded, partial(zlib.decompressobj, wbits=31))),
    Codec("", _identity, _identity_bounded),
)


def _feed(stdin: IO[bytes], data: bytes) -> None:
    """Write `data` to a decoder's stdin, which closes early once the output passes its cap."""
    try:
        stdin.write(data)
        stdin.close()
    except BrokenPipeError:
        pass


def zstd_decompress(data: bytes, max_size: int) -> bytes:
    """Decode zstd `data` through the `zstd` binary without producing more than `max_size` bytes.

    The standard library before Python 3.14 has no zstd decoder. The output is read one byte
    past `max_size` at most, and the decoder is killed there, so a bomb never inflates in full
    -> [VALIDATION.md](VALIDATION.md#object-sizes).

    Args:
        data: The encoded data.
        max_size: The largest decoded size accepted.

    Raises:
        SizeLimitError: The decoded data exceeds `max_size`.
        CorruptStreamError: The data does not decode, or `zstd` cannot run.

    Returns:
        The decoded data.
    """
    try:
        proc = subprocess.Popen(
            ["zstd", "-dc", "-q"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CorruptStreamError(f"cannot run zstd: {exc}") from exc
    assert proc.stdin is not None
    assert proc.stdout is not None
    writer = threading.Thread(target=_feed, args=(proc.stdin, data), daemon=True)
    writer.start()
    out = proc.stdout.read(max_size + 1)
    if len(out) > max_size:
        proc.kill()
    proc.stdout.close()
    returncode = proc.wait()
    writer.join()
    if len(out) > max_size:
        raise SizeLimitError(f"zstd data decodes past {max_size} bytes")
    if returncode != 0:
        raise CorruptStreamError(f"zstd exited with status {returncode}")
    return out


def codec_for(path: str) -> Codec:
    """Return the codec an index path's suffix names.

    Args:
        path: Index file path.

    Returns:
        The matching codec; the uncompressed one when no suffix matches.
    """
    return next(codec for codec in CODECS if path.endswith(codec.suffix))
