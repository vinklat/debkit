"""Class containing fields data from Debian package."""

import os
import re
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from typing import Any

from debkit.repo.hashes import SUPPORT_HASH_LIST

MANDATORY_FIELDS = ["Package", "Filename", "Size"]

# DEB_CONTROL_FIELDS = [
#     'Package', 'Package-Type', 'Version', 'Maintainer', 'Description', 'Section',
#     'Priority', 'Installed-Size', 'Protected', 'Essential', 'Build-Essential',
#     'Architecture', 'Origin', 'Bugs', 'Homepage', 'Tag', 'Multi-Arch', 'Source',
#     'Subarchitecture', 'Kernel-Version', 'Installer-Menu-Item', 'Depends', 'Pre-Depends',
#     'Recommends', 'Suggests', 'Breaks', 'Conflicts', 'Replaces', 'Enhances', 'Provides',
#     'Built-Using', 'Built-For-Profiles', 'Auto-Built-Package', 'Build-Ids'
# ]

# DEB_OTHER_FIELDS = [
#     'Filename', 'Size', 'MD5sum', 'SHA1', 'SHA256', 'SHA512', 'Description-md5',
#     'Ghc-Package', 'Python-Version', 'Ruby-Versions', 'Python-Egg-Name', 'Important',
#     'Go-Import-Path', 'Efi-Vendor', 'Gstreamer-Decoders', 'Description-Md5',
#     'Gstreamer-Elements', 'X-Cargo-Built-Using', 'Gstreamer-Encoders',
#     'Cnf-Extra-Commands', 'Gstreamer-Version', 'Gstreamer-Uri-Sinks',
#     'Gstreamer-Uri-Sources', 'Lua-Versions', 'Postgresql-Catversion',
#     'Cnf-Visible-Pkgname', 'Cnf-Ignore-Commands', 'Cnf-Priority-Bonus',
#     'Original-Maintainer', 'Task', 'Xul-Appid', 'Modaliases', 'Ubuntu-Oem-Kernel-Flavour'
# ]

FILENAME_FIELD_KEY = "Filename"


class PackageException(Exception):
    """Exception raised for errors in the Package class.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Package: {message}")


class Package:
    """Object containing fields data from Debian package.

    Attributes:
        fields (Dict[str, Any]): The package fields.
        modified (bool): True if the package fields have been modified, False otherwise.
    """

    def __init__(self, fields: dict[str, Any] | None = None) -> None:
        """Initialize Package with optional pre-populated fields.

        Args:
            fields (dict[str, Any] | None): Initial field values. Defaults to None.
        """
        self.fields: dict[str, Any] = fields if fields else {}
        self.modified: bool = False

    def __copy__(self) -> "Package":
        """Create a shallow copy of this Package.

        Returns:
            Package: A shallow copy of this Package.
        """
        return Package(fields=self.fields.copy())

    def __repr__(self) -> str:
        """Return a string representation of the Package.

        Returns:
            str: A string representation of the Package.
        """
        return f"{self.fields.get(FILENAME_FIELD_KEY, '<empty package>')}"

    def parse(self, headers: str, controls_only: bool = False) -> str | None:
        """Parse fields of one package from headers format string.

        Args:
            headers (str): Lines in string to parse.
            controls_only (bool): If True, only parse controls fields. Defaults to False.

        Raises:
            PackageException: If mandatory field is missing or newline err in field.

        Returns:
            str | None: The package name. None if data not parsed.
        """
        parser = Parser()
        parsed = parser.parsestr(headers)

        if not parsed:
            return None

        # may not work - email.parser + some Debianrepo: newlines leaks
        # self.fields = dict(parsed)

        # workaround
        # update note2: add strip workaround due to wrap line parse
        self.fields.update(
            {field: data.replace("\n", "").strip() for field, data in parsed.items()}
        )

        if controls_only:
            return None

        # test mandatory fields
        for test_field in MANDATORY_FIELDS:
            if test_field not in self.fields:
                raise PackageException(f"mandatory field '{test_field}' is missing: {self.fields}")

        # when relative path is used in Filename, it not work in some storages like S3
        orig_filename = self.fields[FILENAME_FIELD_KEY]
        norm_filename = os.path.normpath(orig_filename)
        if norm_filename != orig_filename:
            self.fields[FILENAME_FIELD_KEY] = norm_filename
            self.modified = True

        # newline in header check
        for key, value in self.fields.items():
            if value.find("\n") != -1:
                raise PackageException(f"newline in field: {key}:{value.encode('utf-8')}")

        # return package name
        filename: str | None = self.fields[FILENAME_FIELD_KEY]
        return filename

    def set_filename(self, filename: str) -> None:
        """Set the Filename field.

        Args:
            filename (str): The new filename.

        Raises:
            PackageException: If the filename is empty.
        """
        if not filename:
            raise PackageException("filename is empty")
        self.fields[FILENAME_FIELD_KEY] = filename
        self.modified = True

    def get_arch(self) -> str | None:
        """Get the architecture of the package.

        Returns:
            str | None: The architecture. None if the field is not present.
        """
        return self.fields.get("Architecture", None)

    def serialize(self) -> str:
        """Serialize fields of one package to headers format.

        Returns:
            str: The serialized package fields (lines).
        """
        msg = EmailMessage(policy=policy.default.clone(max_line_length=160))
        for field, data in self.fields.items():
            msg.add_header(field, data)

        return msg.as_string()

    def dump(self) -> dict[str, Any]:
        """Return dump of package fields in dict.

        Returns:
            Dict[str, Any]: The package fields.
        """
        return self.fields

    def field_re_sub(self, field: str, pattern: str, repl: str) -> None:
        """Replace field value using regexp.

        Args:
            field (str): The field to replace.
            pattern (str): The pattern to replace.
            repl (str): The replacement string.

        Raises:
            PackageException: If field not exist.
        """
        try:
            result = re.sub(pattern, repl, self.fields[field])
        except KeyError as exc:
            raise PackageException(f"field '{field}' not exist - not modified!") from exc
        self.fields[field] = result
        self.modified = True

    def get_digests(self) -> dict[str, str]:
        """Return all hexdigests from fields.

        Returns:
            Dict[str, str]: The hexdigests.
        """
        return {alg: self.fields[alg] for alg in SUPPORT_HASH_LIST if alg in self.fields}

    def set_digests(self, digests: dict[str, str]) -> None:
        """Set all hexdigests to fields.

        Args:
            digests (Dict[str, str]): The hexdigests.
        """
        for alg, value in digests.items():
            self.fields[alg] = value
        self.modified = True

    def compare(self, other: object) -> bool:
        """Compare this Package with another Package.

        Args:
            other (object): The other Package to compare with.

        Raises:
            ValueError: If the other object is not a Package.

        Returns:
            bool: True if the fields of the two Packages are equal, False otherwise.
        """
        if not isinstance(other, Package):
            raise ValueError("Can only compare a Package with another Package.")
        return self.fields == other.fields

    def __eq__(self, other: object) -> bool:
        """Check if this Package is equal to another Package.

        Args:
            other (object): The other Package to compare with.

        Returns:
            bool: True if the fields of the two Packages are equal, False otherwise.
        """
        return self.compare(other)
