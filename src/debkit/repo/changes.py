"""Class containing Debian Changes file data."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from txstore import StorageException, is_unsafe_path

from debkit.repo.compress import SizeLimitError, read_capped
from debkit.repo.gnupg import CLEARSIGN_BEGIN, GPGVerifyException, cleartext_body
from debkit.repo.hashes import CHECKSUMS_SHA256, ChecksumException, HexDigests, parse_hash_block
from debkit.repo.params import SOURCE_ARCH
from debkit.repo.stanza import parse_stanza

if TYPE_CHECKING:
    from txstore import Storage

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_ARCH = "amd64"

# Largest `.changes` file read; a real one lists a few files and is a few KiB, so this bounds
# what a mistyped name or a misbehaving backend can make a load buffer
# -> [VALIDATION.md](VALIDATION.md#object-sizes)
MAX_CHANGES_BYTES = 4 * 1024 * 1024


class ChangesException(Exception):
    """Exception raised for errors in the Changes class."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Changes: {message}")


class Changes(HexDigests):
    """Object holding parsed Debian ``.changes`` file data.

    Attributes:
        storage: Storage backend used to read the file.
        fields: Parsed header fields from the changes file.
    """

    def __init__(self, storage: Storage) -> None:
        """Initialize Changes object.

        Args:
            storage: Storage object
        """
        self.storage = storage
        self.fields: dict[str, str] = {}
        super().__init__()

    def parse(self, enc_data: bytes) -> None:
        """Parse Changes file fields from headers format.

        Args:
            enc_data: The encoded data to parse.

        A clearsigned file, as `debsign` writes it, is read through its signed body; the
        signature itself is not verified -> [CRYPTO.md](CRYPTO.md#verification-failures).

        Raises:
            ChangesException: If the file is not UTF-8.
            ChangesException: If a field appears twice, its name compared without case.
            ChangesException: If a blank line stands between two fields.
            ChangesException: If a line is neither a field, a continuation, nor empty.
            ChangesException: If no SHA256 checksums found in changes file.
            ChangesException: If a listed file is not a bare, safe file name.
            ChangesException: If a clearsigned file is malformed.
        """
        if enc_data.startswith(CLEARSIGN_BEGIN.encode()):
            try:
                enc_data = cleartext_body(enc_data)
            except GPGVerifyException as exc:
                raise ChangesException(f"malformed signed file: {exc}") from exc
        # Debian Policy 5.1: control data is UTF-8
        # -> [VALIDATION.md](VALIDATION.md#control-file-encoding)
        try:
            data = enc_data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChangesException(f"not UTF-8: {exc}") from exc

        # one paragraph, as dpkg reads it: a blank line would end the fields there and hide
        # the digests and architectures after it -> [VALIDATION.md](VALIDATION.md#blank-lines)
        parsed, more_paragraphs, unreadable = parse_stanza(data)
        if more_paragraphs:
            raise ChangesException("blank line between fields: a .changes is one paragraph")
        if unreadable:
            raise ChangesException("unreadable line: a .changes field line needs a colon")
        # Debian Policy 5.1 field names ignore case, and dpkg refuses a repeated one: a reader
        # keeping another copy would see other digests and architectures than debkit checks
        # -> [VALIDATION.md](VALIDATION.md#repeated-fields)
        names = [name.lower() for name in parsed]
        if repeated := sorted({name for name in names if names.count(name) > 1}):
            raise ChangesException(f"repeated field: {', '.join(repeated)}")
        self.fields = dict(parsed.items())

        if CHECKSUMS_SHA256 not in self.fields:
            raise ChangesException("No SHA256 checksums found")

        for hashstr, size, filename in parse_hash_block(self.fields[CHECKSUMS_SHA256]):
            # a listed name is read from the incoming storage and names the pool key, so
            # it is a bare file name -> [VALIDATION.md](VALIDATION.md#filename-safety)
            if "/" in filename or is_unsafe_path(filename):
                raise ChangesException(f"unsafe file name: {filename!r}")
            digests = {"Size": str(size), "SHA256": hashstr}
            self.update_digests(filename, digests)

    def load(self, filename: str, digest_expected: str | None = None) -> None:
        """Load Changes file fields from data.

        Args:
            filename: The filename of the data.
            digest_expected: The expected digest of the data.

        Raises:
            ChangesException: If the file cannot be read, is empty, or is larger than
                `MAX_CHANGES_BYTES`.
            ChecksumException: If the checksum of the file does not match the expected checksum.
        """
        try:
            data = read_capped(self.storage, filename, MAX_CHANGES_BYTES)
        except StorageException as exc:
            raise ChangesException(f"Cannot read .changes file {filename}: {exc}") from exc
        except SizeLimitError as exc:
            raise ChangesException(str(exc)) from exc

        if not data:
            raise ChangesException(f"Empty changes file: {filename}")

        digest = hashlib.sha256(data).hexdigest()
        if (digest_expected is not None) and (digest_expected != digest):
            raise ChecksumException(
                f"{filename} checksum mismatch. Expected: {digest_expected}, got: {digest}"
            )
        logger.debug("Parse changes file %s (%d bytes)", filename, len(data))
        self.parse(data)

    def get_archs(self) -> list[str]:
        """Get every architecture token the Changes file names.

        `dpkg-genchanges` writes one Architecture field per upload, so a source plus
        binary build names `source` beside the binary arch. Reading a single token
        makes the field order decide the upload, so every token is returned.

        Returns:
            The architecture tokens of the Changes file.
        """
        return self.fields.get("Architecture", DEFAULT_ARCH).split()

    def names_source_arch(self) -> bool:
        """Report whether the Changes file names the source architecture.

        The token may sit anywhere in the field, so the answer does not depend on the
        order `dpkg-genchanges` wrote -> [VALIDATION.md](VALIDATION.md#sources-stanzas).

        Returns:
            True when any architecture token is the source one.
        """
        return SOURCE_ARCH in self.get_archs()

    def get_deb_files(self) -> list[str]:
        """Get the .deb files in the Changes file.

        Returns:
            The deb files in the Changes file.
        """
        return [f for f in self.digests if f.endswith(".deb")]
