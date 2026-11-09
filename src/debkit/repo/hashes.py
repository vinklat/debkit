"""Debian hash blocks and the per-file digest table of a `Release` or index.

The digest helpers and the algorithm-name lists come from `txstore`; this module owns what
is Debian-specific on top of them -> [CRYPTO.md](CRYPTO.md#checksums).
"""

from __future__ import annotations

import re

from txstore import COMPARE_HASH_LIST, SUPPORT_HASH_LIST, compare_digests

__all__ = [
    "CHECKSUMS_SHA256",
    "CHECKSUM_FIELDS",
    "COMPARE_HASH_LIST",
    "SUPPORT_HASH_LIST",
    "ChecksumException",
    "HexDigests",
    "parse_hash_block",
]

# A `Sources` or `.changes` hash-block field -> the algorithm its digests are in; a `Release`
# names its hash fields after the algorithm itself -> [CRYPTO.md](CRYPTO.md#checksums)
CHECKSUMS_SHA256 = "Checksums-Sha256"
CHECKSUM_FIELDS = {
    "Files": "MD5Sum",
    "Checksums-Sha1": "SHA1",
    CHECKSUMS_SHA256: "SHA256",
    "Checksums-Sha512": "SHA512",
}


def parse_hash_block(text: str) -> list[tuple[str, str, str]]:
    """Split a Debian hash block into `(hexdigest, size, filename)` triples.

    A hash block is the multi-line value of a `Release` hash field, a
    `Checksums-<algorithm>` field, or the `Files` field of a `Sources` stanza:
    whitespace-separated runs of three tokens. A trailing partial run is dropped.
    The five-column `Files` field of a `.changes` file has a different shape and does
    not belong here -> [CRYPTO.md](CRYPTO.md#checksums).

    Args:
        text: Field value, continuation indentation included.

    Returns:
        One triple per complete run, in file order.
    """
    parts = re.split(r"\s+", text.strip())
    return list(zip(*[iter(parts)] * 3, strict=False))


class ChecksumException(Exception):
    """Exception raised for digest mismatch or distrustful object failures."""

    def __init__(self, message: str) -> None:
        """Initialize the exception with a checksum error message.

        Args:
            message: Explanation of the error.
        """
        super().__init__(f"Checksum: {message}")


class HexDigests:
    """Class for managing checksums of files.

    Attributes:
        digests: The checksums of files.
    """

    def __init__(self) -> None:
        """Initialize the HexDigests object."""
        self.digests: dict[str, dict[str, str]] = {}

    def verify_digests(self, filename: str, incoming_digests: dict[str, str]) -> bool:
        """Verify checksum of a file.

        Args:
            filename: The filename to verify.
            incoming_digests: The checksums to verify.

        Returns:
            True if the checksum is valid, False otherwise.
        """
        if filename not in self.digests:
            return False

        if not compare_digests(self.digests[filename], incoming_digests):
            return False

        return True

    def declared_size(self, filename: str) -> int | None:
        """Get the `Size` the digest table records for a file.

        Args:
            filename: The filename to look up.

        Returns:
            The recorded size, or None when the file or a well-formed size is not recorded.
        """
        size = str(self.digests.get(filename, {}).get("Size", ""))
        return int(size) if size.isascii() and size.isdigit() else None

    def update_digests(self, filename: str, incoming_digests: dict[str, str]) -> None:
        """Update checksums of a file.

        Args:
            filename: The filename to update.
            incoming_digests: The checksums to update.
        """
        if filename not in self.digests:
            self.digests[filename] = {}

        self.digests[filename].update(incoming_digests)
