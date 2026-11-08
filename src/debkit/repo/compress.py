"""Compression codecs for Debian index files."""

import bz2
import gzip
import logging
import lzma
from collections.abc import Callable
from functools import partial
from typing import NamedTuple

logging.getLogger(__name__).addHandler(logging.NullHandler())


class Codec(NamedTuple):
    """How an index file named with `suffix` is encoded and decoded.

    Attributes:
        suffix (str): File name suffix, empty for the uncompressed index.
        compress (Callable[[bytes], bytes]): Encoder.
        decompress (Callable[[bytes], bytes]): Decoder.
    """

    suffix: str
    compress: Callable[[bytes], bytes]
    decompress: Callable[[bytes], bytes]


def _identity(data: bytes) -> bytes:
    """Return an uncompressed index as is."""
    return data


# discovery order: the preferred compression first, the uncompressed index last
# -> [VALIDATION.md](VALIDATION.md#repository-index-paths)
CODECS: tuple[Codec, ...] = (
    Codec(".xz", lzma.compress, lzma.decompress),
    # `.lzma` is the legacy LZMA-alone format; decoding auto-detects, so an `.lzma`
    # written in xz format by an older debkit still reads
    Codec(".lzma", partial(lzma.compress, format=lzma.FORMAT_ALONE), lzma.decompress),
    Codec(".bz2", bz2.compress, bz2.decompress),
    Codec(".gz", gzip.compress, gzip.decompress),
    Codec("", _identity, _identity),
)


def codec_for(path: str) -> Codec:
    """Return the codec an index path's suffix names.

    Args:
        path (str): Index file path.

    Returns:
        Codec: The matching codec; the uncompressed one when no suffix matches.
    """
    return next(codec for codec in CODECS if path.endswith(codec.suffix))
