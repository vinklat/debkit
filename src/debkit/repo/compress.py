"""Compression algorithm function maps for Debian index files."""

import bz2
import gzip
import logging
import lzma
from collections.abc import Callable

logging.getLogger(__name__).addHandler(logging.NullHandler())

COMPRESS_MAP: dict[str, Callable[[bytes], bytes]] = {
    ".xz": lzma.compress,
    ".lzma": lzma.compress,
    ".bz2": bz2.compress,
    ".gz": gzip.compress,
}

DECOMPRESS_MAP: dict[str, Callable[[bytes], bytes]] = {
    ".xz": lzma.decompress,
    ".lzma": lzma.decompress,
    ".bz2": bz2.decompress,
    ".gz": gzip.decompress,
}
