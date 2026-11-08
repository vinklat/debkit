"""Compression codecs for Debian index files."""

from __future__ import annotations

import bz2
import gzip
import lzma
import re
import zlib
from collections.abc import Buffer, Callable
from functools import partial
from typing import NamedTuple, Protocol


class SizeLimitError(ValueError):
    """Decoded data exceeds the size limit the caller set."""


class Codec(NamedTuple):
    """How an index file named with `suffix` is encoded and decoded.

    Attributes:
        suffix: File name suffix, empty for the uncompressed index.
        compress: Encoder.
        decompress: Decoder; refuses output larger than its second argument with `SizeLimitError`.
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


# a fresh decoder first reads a small chunk, doubling up to the largest, so a stream's end
# copies at most one chunk of what follows into `unused_data`
_FIRST_CHUNK = 256
_LARGEST_CHUNK = 1 << 20
_NON_NUL = re.compile(rb"[^\0]")


def _bounded(new_decoder: Callable[[], _Decoder], data: bytes, max_size: int) -> bytes:
    """Decode every concatenated stream in `data` without producing more than `max_size` bytes.

    Each decoder call is capped one byte past what is left, so a bomb stops as soon as it
    overflows instead of after it is fully inflated. NUL padding between streams is
    skipped, as xz stream padding and the one-shot `gzip.decompress` allow. The input is
    walked by offset in growing chunks, so many concatenated streams decode in linear time
    instead of copying the rest of the input at every stream end.

    Args:
        new_decoder: Factory of a fresh decoder per stream.
        data: The encoded data.
        max_size: The largest decoded size accepted.

    Raises:
        SizeLimitError: The decoded data exceeds `max_size`.
        EOFError: A stream ends before its end-of-stream marker.

    Returns:
        The decoded data of every stream, concatenated.
    """
    view = memoryview(data)
    out = bytearray()
    offset = 0
    while offset < len(view):
        decoder = new_decoder()
        step = _FIRST_CHUNK
        while True:
            chunk = view[offset : offset + step]
            out += decoder.decompress(chunk, max_size + 1 - len(out))
            if len(out) > max_size:
                raise SizeLimitError(f"decoded data exceeds {max_size} bytes")
            # below the cap the decoder consumed the whole chunk unless the stream ended in it
            if decoder.eof:
                offset += len(chunk) - len(decoder.unused_data)
                break
            offset += len(chunk)
            if offset >= len(view):
                raise EOFError("Compressed data ended before the end-of-stream marker was reached")
            step = min(step * 2, _LARGEST_CHUNK)
        match = _NON_NUL.search(data, offset)
        offset = match.start() if match else len(view)
    return bytes(out)


def _identity(data: bytes) -> bytes:
    """Return an uncompressed index as is."""
    return data


def _identity_bounded(data: bytes, max_size: int) -> bytes:
    """Return an uncompressed index as is, refusing one larger than `max_size`."""
    if len(data) > max_size:
        raise SizeLimitError(f"data exceeds {max_size} bytes")
    return data


# `.lzma` is the legacy LZMA-alone format; decoding auto-detects, so an `.lzma`
# written in xz format by an older debkit still reads
_lzma_decompress = partial(_bounded, partial(lzma.LZMADecompressor, lzma.FORMAT_AUTO))

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


def codec_for(path: str) -> Codec:
    """Return the codec an index path's suffix names.

    Args:
        path: Index file path.

    Returns:
        The matching codec; the uncompressed one when no suffix matches.
    """
    return next(codec for codec in CODECS if path.endswith(codec.suffix))
