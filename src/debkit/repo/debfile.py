"""Class containing ``.deb`` file data."""

import logging
import re
import tarfile
from io import BytesIO, UnsupportedOperation
from typing import IO, TYPE_CHECKING

from debian import debfile
from debian.arfile import ArError
from txstore import StorageException, is_unsafe_path

from debkit.repo.package import Package
from debkit.repo.params import pool_path

if TYPE_CHECKING:
    from txstore import Storage

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Debian Policy 5.6.1 and 5.6.7: the `Source` field, or `Package` without one, becomes a pool
# directory, so both are held to the policy name -> [VALIDATION.md](VALIDATION.md#filename-safety)
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")

# A binary package built from a source of another version names it as `Source: <name> (<version>)`
_SOURCE_VERSION = re.compile(r"\s+\([^()]*\)$")


class DebFileException(Exception):
    """Exception raised for errors inspecting a ``.deb`` file."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"DebFile: {message}")


def _control_of(fp: IO[bytes]) -> bytes | str | None:
    """Return the ``control`` file of the ``.deb`` read from `fp`."""
    return debfile.DebFile(fileobj=fp).control["control"]  # type: ignore[arg-type]


class DebFile:
    """Read-side wrapper around a ``.deb`` package on a `Storage` backend.

    The control file is read once at construction; the storage handle is closed before the
    constructor returns, whether it succeeds or fails.

    Attributes:
        storage (Storage): Storage backend the file lives on.
        path (str): Path of the ``.deb`` file inside ``storage``.
        comp (Optional[str]): Component name the package belongs to.
        control (bytes | str | None): Content of the ``control`` file in ``control.tar``.
    """

    def __init__(self, storage: "Storage", path: str, comp: str | None = None) -> None:
        """Initialize DebFile object.

        Args:
            storage (Storage): Storage backend.
            path (str): Path to the Debian package file.
            comp (Optional[str]): Component name. Defaults to None.

        Raises:
            DebFileException: If the underlying ``.deb`` cannot be read or parsed.
        """
        self.storage = storage
        self.path = path
        self.comp = comp
        try:
            self.control = self._read_control()
        except StorageException as exc:
            raise DebFileException(f"Cannot read .deb file {path}: {exc}") from exc
        # `ArError` is the base of `DebError`; a truncated ar header raises a bare `OSError`,
        # and a truncated `control.tar` member an `EOFError` or a `TarError`
        except (ArError, tarfile.TarError, EOFError, OSError) as exc:
            raise DebFileException(f"Invalid .deb file {path}: {exc}") from exc

    def _read_control(self) -> bytes | str | None:
        """Read the ``control`` file, streaming when the backend can seek.

        Returns:
            bytes | str | None: The ``control`` file content.
        """
        try:
            with self.storage.get_stream(self.path) as fp:
                return _control_of(fp)
        except UnsupportedOperation as exc:
            logger.debug("%s cannot stream (%s); reading it into memory.", self.path, exc)
        with BytesIO(self.storage.get(self.path)) as fp:
            return _control_of(fp)

    def get_package(self) -> "Package":
        """Get control data from the Debian package.

        The control file comes from the upload, so its `Package` and `Source` fields and the
        upload path are checked before they name a pool key; nothing has been transferred yet.

        Raises:
            DebFileException: If the control file is not a bytes object.
            DebFileException: If the `Package` field is missing or not a policy name.
            DebFileException: If the `Source` field, without its version, is not a policy name.
            DebFileException: If the pool key would be an unsafe storage path.

        Returns:
            Package: Package object with control data.
        """
        control_lines = self.control
        if not isinstance(control_lines, bytes):
            raise DebFileException(f"control file of {self.path} is not a bytes object")
        control_lines = control_lines.decode()
        package: Package = Package(fields={"Filename": "", "Size": "0"})
        package.parse(control_lines, controls_only=True)
        pkg_name = package.fields.get("Package")
        if not isinstance(pkg_name, str) or not _PACKAGE_NAME.fullmatch(pkg_name):
            raise DebFileException(f"{self.path} names no valid Package: {pkg_name!r}")
        source = _SOURCE_VERSION.sub("", str(package.fields.get("Source", pkg_name)))
        if not _PACKAGE_NAME.fullmatch(source):
            raise DebFileException(f"{self.path} names no valid Source: {source!r}")

        filename = pool_path(self.comp, source, self.path)
        if is_unsafe_path(filename):
            raise DebFileException(f"unsafe pool path for {self.path}: {filename!r}")
        package.set_filename(filename)

        return package
