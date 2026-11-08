"""Debian hash blocks and the per-file digest table of a `Release` or index.

The digest helpers and the algorithm-name lists come from `txstore`; this module owns what
is Debian-specific on top of them -> [CRYPTO.md](CRYPTO.md#checksums).
"""

import re
from typing import Any

from txstore import COMPARE_HASH_LIST, SUPPORT_HASH_LIST, compare_digests

__all__ = [
    "COMPARE_HASH_LIST",
    "SUPPORT_HASH_LIST",
    "ChecksumException",
    "HexDigests",
    "parse_hash_block",
]


def parse_hash_block(text: str) -> list[tuple[str, str, str]]:
    """Split a Debian hash block into `(hexdigest, size, filename)` triples.

    A hash block is the multi-line value of a `Release` hash field, a
    `Checksums-<algorithm>` field, or the `Files` field of a `Sources` stanza:
    whitespace-separated runs of three tokens. A trailing partial run is dropped.
    The five-column `Files` field of a `.changes` file has a different shape and does
    not belong here -> [CRYPTO.md](CRYPTO.md#checksums).

    Args:
        text (str): Field value, continuation indentation included.

    Returns:
        List[Tuple[str, str, str]]: One triple per complete run, in file order.
    """
    parts = re.split(r"\s+", text.strip())
    return list(zip(*[iter(parts)] * 3, strict=False))


class ChecksumException(Exception):
    """Exception raised for digest mismatch or distrustful object failures."""

    def __init__(self, message: str) -> None:
        """Initialize the exception with a checksum error message.

        Args:
            message (str): Explanation of the error.
        """
        super().__init__(f"Checksum: {message}")


class HexDigests:
    """Class for managing checksums of files.

    Attributes:
        digests (Dict[str, Dict[str, Any]]): The checksums of files.
        modified (bool): Whether the release has been modified.
    """

    def __init__(self) -> None:
        """Initialize the HexDigests object."""
        self.digests: dict[str, dict[str, Any]] = {}
        self.modified = False

    def verify_digests(self, filename: str, incoming_digests: dict[str, str]) -> bool:
        """Verify checksum of a file.

        Args:
            filename (str): The filename to verify.
            incoming_digests (Dict[str,str]): The checksums to verify.

        Returns:
            bool: True if the checksum is valid, False otherwise.
        """
        if filename not in self.digests:
            return False

        if not compare_digests(self.digests[filename], incoming_digests):
            return False

        return True

    def update_digests(self, filename: str, incoming_digests: dict[str, str]) -> None:
        """Update checksums of a file.

        Args:
            filename (str): The filename to update.
            incoming_digests (Dict[str,str]): The checksums to update.
        """
        if filename not in self.digests:
            self.digests[filename] = {}

        self.digests[filename].update(incoming_digests)
        self.modified = True
