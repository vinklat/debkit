"""Class containing ``.deb`` file data."""

import logging
from io import BytesIO, UnsupportedOperation
from typing import TYPE_CHECKING

from debian import debfile
from txstore import StorageException

from debkit.repo.package import Package

if TYPE_CHECKING:
    from txstore import Storage

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class DebFileException(Exception):
    """Exception raised for errors inspecting a ``.deb`` file.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"DebFile: {message}")


class DebFile:
    """Read-side wrapper around a ``.deb`` package on a `Storage` backend.

    Attributes:
        storage (Storage): Storage backend the file lives on.
        path (str): Path of the ``.deb`` file inside ``storage``.
        comp (Optional[str]): Component name the package belongs to.
        filename_field (str): Pool-relative filename (populated by ``get_package``).
        fileobj (IO[bytes]): File-like object backing the parsed deb.
        deb (debfile.DebFile): Underlying parsed deb archive.
    """

    def __init__(self, storage: "Storage", path: str, comp: str | None = None) -> None:
        """Initialize DebFile object.

        Args:
            storage (Storage): Storage backend.
            path (str): Path to the Debian package file.
            comp (Optional[str]): Component name. Defaults to None.

        Raises:
            DebFileException: If the underlying ``.deb`` cannot be opened or parsed.
        """
        self.storage = storage
        self.path = path
        self.filename_field = path
        self.comp = comp
        try:
            try:
                self.fileobj = storage.get_stream(path)
                self.deb = debfile.DebFile(fileobj=self.fileobj)  # type: ignore[arg-type]
            except UnsupportedOperation as exc:
                logger.debug(
                    "UnsupportedOperation: %s in stream; falling back to get memory object", exc
                )
                self.fileobj = BytesIO(storage.get(path))
                self.deb = debfile.DebFile(fileobj=self.fileobj)
        except StorageException as exc:
            raise DebFileException(f"Cannot read .deb file {path}: {exc}") from exc
        except debfile.DebError as exc:
            raise DebFileException(f"Invalid .deb file {path}: {exc}") from exc

    def get_package(self) -> "Package":
        """Get control data from the Debian package.

        Raises:
            DebFileException: If the control file is not a bytes object.

        Returns:
            Package: Package object with control data.
        """
        control_lines = self.deb.control["control"]
        if not isinstance(control_lines, bytes):
            raise DebFileException(f"control file of {self.path} is not a bytes object")
        control_lines = control_lines.decode()
        package: Package = Package(fields={"Filename": "", "Size": "0"})
        package.parse(control_lines, controls_only=True)
        pkg_name = package.fields.get("Package")

        self.filename_field = (
            f"pool/{self.comp}/{self.path[0].lower()}/{pkg_name}/{self.path}"
            if self.comp is not None
            else f"pool/{self.path[0].lower()}/{pkg_name}/{self.path}"
        )

        package.set_filename(self.filename_field)

        return package
