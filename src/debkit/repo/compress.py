"""Compression codecs for Debian index files."""

import bz2
import gzip
import lzma
import zlib
from collections.abc import Callable
from functools import partial
from typing import NamedTuple, Protocol


class SizeLimitError(ValueError):
    """Decoded data exceeds the size limit the caller set."""


class Codec(NamedTuple):
    """How an index file named with `suffix` is encoded and decoded.

    Attributes:
        suffix (str): File name suffix, empty for the uncompressed index.
        compress (Callable[[bytes], bytes]): Encoder.
        decompress (Callable[[bytes, int], bytes]): Decoder; refuses output larger than
            its second argument with `SizeLimitError`.
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

    def decompress(self, data: bytes, max_length: int, /) -> bytes:
        """Decode `data`, returning at most `max_length` bytes."""


def _bounded(new_decoder: Callable[[], _Decoder], data: bytes, max_size: int) -> bytes:
    """Decode every concatenated stream in `data` without producing more than `max_size` bytes.

    Each decoder call is capped one byte past what is left, so a bomb stops as soon as it
    overflows instead of after it is fully inflated. NUL padding between streams is
    skipped, as xz stream padding and the one-shot `gzip.decompress` allow.

    Args:
        new_decoder (Callable[[], _Decoder]): Factory of a fresh decoder per stream.
        data (bytes): The encoded data.
        max_size (int): The largest decoded size accepted.

    Raises:
        SizeLimitError: The decoded data exceeds `max_size`.
        EOFError: A stream ends before its end-of-stream marker.

    Returns:
        bytes: The decoded data of every stream, concatenated.
    """
    out = bytearray()
    while data:
        decoder = new_decoder()
        out += decoder.decompress(data, max_size + 1 - len(out))
        if len(out) > max_size:
            raise SizeLimitError(f"decoded data exceeds {max_size} bytes")
        # below the cap the decoder consumed all its input, so no eof means truncation
        if not decoder.eof:
            raise EOFError("Compressed data ended before the end-of-stream marker was reached")
        data = decoder.unused_data.lstrip(b"\0")
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
        path (str): Index file path.

    Returns:
        Codec: The matching codec; the uncompressed one when no suffix matches.
    """
    return next(codec for codec in CODECS if path.endswith(codec.suffix))
