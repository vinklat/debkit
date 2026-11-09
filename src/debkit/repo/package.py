"""Class containing fields data from Debian package."""

from __future__ import annotations

import posixpath
from typing import Any

from debian.deb822 import Deb822

from debkit.repo.hashes import SUPPORT_HASH_LIST

MANDATORY_FIELDS = ["Package", "Filename", "Size"]

# fields used as storage keys or digests; a line break in one is never a folded value
SINGLE_LINE_FIELDS = [*MANDATORY_FIELDS, *SUPPORT_HASH_LIST]

FILENAME_FIELD_KEY = "Filename"


class PackageException(Exception):
    """Exception raised for errors in the Package class."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Package: {message}")


class Package:
    """Object containing fields data from Debian package.

    Attributes:
        fields: The package fields.
    """

    def __init__(self, fields: dict[str, Any] | None = None) -> None:
        """Initialize Package with optional pre-populated fields.

        Args:
            fields: Initial field values. Defaults to None.
        """
        self.fields: dict[str, Any] = fields if fields else {}

    def __repr__(self) -> str:
        """Return a string representation of the Package.

        Returns:
            A string representation of the Package.
        """
        return f"{self.fields.get(FILENAME_FIELD_KEY, '<empty package>')}"

    def parse(self, headers: str, controls_only: bool = False) -> str | None:
        """Parse fields of one package from headers format string.

        Args:
            headers: Lines in string to parse.
            controls_only: If True, only parse controls fields. Defaults to False.

        Multi-line values (`Description`, `Conffiles`) keep their continuation lines, so a
        rewritten index carries them unchanged -> [VALIDATION.md](VALIDATION.md#package-stanzas).

        Raises:
            PackageException: If mandatory field is missing or a single-line field holds a
                line break.

        Returns:
            The package name. None if data not parsed.
        """
        parsed = Deb822(headers)

        if not parsed:
            return None

        self.fields.update(parsed)

        if controls_only:
            return None

        # test mandatory fields
        for test_field in MANDATORY_FIELDS:
            if test_field not in self.fields:
                raise PackageException(f"mandatory field '{test_field}' is missing: {self.fields}")

        # a storage key is a POSIX path whatever the host OS; a relative segment does not
        # work in some storages like S3
        self.fields[FILENAME_FIELD_KEY] = posixpath.normpath(self.fields[FILENAME_FIELD_KEY])

        for key in SINGLE_LINE_FIELDS:
            value = str(self.fields.get(key, ""))
            if "\n" in value:
                raise PackageException(f"newline in field: {key}:{value!r}")

        # return package name
        filename: str | None = self.fields[FILENAME_FIELD_KEY]
        return filename

    def get_filename(self) -> str:
        """Get the Filename field.

        Raises:
            PackageException: If the filename is missing or empty.

        Returns:
            The pool-relative filename.
        """
        filename = self.fields.get(FILENAME_FIELD_KEY)
        if not filename:
            raise PackageException("filename is empty")
        return str(filename)

    def set_filename(self, filename: str) -> None:
        """Set the Filename field.

        Args:
            filename: The new filename.

        Raises:
            PackageException: If the filename is empty.
        """
        if not filename:
            raise PackageException("filename is empty")
        self.fields[FILENAME_FIELD_KEY] = filename

    def get_arch(self) -> str | None:
        """Get the architecture of the package.

        Returns:
            The architecture. None if the field is not present.
        """
        return self.fields.get("Architecture", None)

    def serialize(self) -> str:
        """Serialize fields of one package to headers format.

        Values are written verbatim: multi-line values keep their continuation lines and
        non-ASCII text stays UTF-8.

        Returns:
            The serialized stanza, ending with the blank line that separates stanzas.
        """
        return Deb822({field: str(data) for field, data in self.fields.items()}).dump() + "\n"

    def dump(self) -> dict[str, Any]:
        """Return dump of package fields in dict.

        Returns:
            The package fields.
        """
        return self.fields

    def get_digests(self) -> dict[str, str]:
        """Return all hexdigests from fields.

        Returns:
            The hexdigests.
        """
        return {alg: self.fields[alg] for alg in SUPPORT_HASH_LIST if alg in self.fields}

    def set_digests(self, digests: dict[str, str]) -> None:
        """Set all hexdigests to fields.

        Args:
            digests: The hexdigests.
        """
        self.fields.update(digests)

    def __eq__(self, other: object) -> bool:
        """Check if this Package is equal to another Package.

        Args:
            other: The object to compare with.

        Returns:
            True if the fields of the two Packages are equal, False otherwise; NotImplemented for an
            object that is not a Package.
        """
        if not isinstance(other, Package):
            return NotImplemented
        return self.fields == other.fields
