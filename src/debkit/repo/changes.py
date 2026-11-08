"""Class containing Debian Changes file data."""

import hashlib
import logging
import re
from email.parser import Parser
from typing import TYPE_CHECKING, Any

from debkit.repo.hashes import ChecksumException, HexDigests, parse_hash_block
from debkit.repo.params import SOURCE_ARCH

if TYPE_CHECKING:
    from txstore import Storage

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

SHA256KEY = "Checksums-Sha256"
DEFAULT_ARCH = "amd64"


class ChangesException(Exception):
    """Exception raised for errors in the Changes class."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Changes: {message}")


class Changes(HexDigests):
    """Object holding parsed Debian ``.changes`` file data.

    Attributes:
        storage (Storage): Storage backend used to read the file.
        fields (dict[str, str]): Parsed header fields from the changes file.
        hashfuns (list[str]): Hash algorithms found in the changes file.
    """

    def __init__(self, storage: "Storage") -> None:
        """Initialize Changes object.

        Args:
            storage (Storage): Storage object
        """
        self.storage = storage
        self.fields: dict[str, str] = {}
        self.hashfuns: list[str] = []
        super().__init__()

    def parse(self, enc_data: bytes) -> None:
        """Parse Changes file fields from headers format.

        Args:
            enc_data (bytes): The encoded data to parse.

        Raises:
            ChangesException: If no SHA256 checksums found in changes file.
        """
        data = enc_data.decode("utf-8")

        data = re.sub("\n\n", "\n", data)  # fix - remove blank lines
        parser = Parser()
        parsed = parser.parsestr(data)
        self.fields = dict(parsed)

        if SHA256KEY not in self.fields:
            raise ChangesException("No SHA256 checksums found")

        self.hashfuns.append("SHA256")
        for hashstr, size, filename in parse_hash_block(self.fields[SHA256KEY]):
            digests = {"Size": str(size), "SHA256": hashstr}
            self.update_digests(filename, digests)

    def load(self, filename: str, digest_expected: str | None = None) -> None:
        """Load Changes file fields from data.

        Args:
            filename (str): The filename of the data.
            digest_expected (str): The expected digest of the data.

        Raises:
            ChecksumException: If the checksum of the file does not match the expected checksum.
        """
        data: bytes = self.storage.get(filename)

        if not data:
            raise ChangesException(f"Empty or missing changes file: {filename}")

        digest = hashlib.sha256(data).hexdigest()
        if (digest_expected is not None) and (digest_expected != digest):
            raise ChecksumException(
                f"{filename} checksum mismatch. Expected: {digest_expected}, got: {digest}"
            )
        logger.debug("Parse changes file %s (%d bytes)", filename, len(data))
        self.parse(data)

    def get_digests(self, filename: str) -> dict[str, Any]:
        """Get the digests of a file.

        Args:
            filename (str): The filename of the file.

        Returns:
            Dict[str, Any]: The digests of the file.
        """
        return self.digests[filename]

    def get_archs(self) -> list[str]:
        """Get every architecture token the Changes file names.

        `dpkg-genchanges` writes one Architecture field per upload, so a source plus
        binary build names `source` beside the binary arch. Reading a single token
        makes the field order decide the upload, so every token is returned.

        Returns:
            List[str]: The architecture tokens of the Changes file.
        """
        return self.fields.get("Architecture", DEFAULT_ARCH).split()

    def names_source_arch(self) -> bool:
        """Report whether the Changes file names the source architecture.

        The token may sit anywhere in the field, so the answer does not depend on the
        order `dpkg-genchanges` wrote -> [VALIDATION.md](VALIDATION.md#sources-stanzas).

        Returns:
            bool: True when any architecture token is the source one.
        """
        return SOURCE_ARCH in self.get_archs()

    def get_files(self) -> list[str]:
        """Get the files in the Changes file.

        Returns:
            List[str]: The files in the Changes file.
        """
        return list(self.digests.keys())

    def get_deb_files(self) -> list[str]:
        """Get the .deb files in the Changes file.

        Returns:
            List[str]: The deb files in the Changes file.
        """
        return [f for f in self.get_files() if f.endswith(".deb")]
