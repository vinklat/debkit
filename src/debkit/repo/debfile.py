"""Class containing ``.deb`` file data."""

from __future__ import annotations

import logging
import lzma
import re
import tarfile
import zlib
from collections.abc import Callable
from io import BytesIO, UnsupportedOperation
from typing import IO, TYPE_CHECKING

from debian import debfile
from debian.arfile import ArError
from txstore import StorageException, is_unsafe_path

from debkit.repo.compress import CODECS, CorruptStreamError, SizeLimitError, zstd_decompress
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


# Largest compressed `control.tar` member and `control` file read from an upload, and the
# largest `control.tar` they decode to; all are held in memory before the package is
# transferred -> [VALIDATION.md](VALIDATION.md#object-sizes)
MAX_CONTROL_BYTES = 4 * 1024 * 1024
MAX_CONTROL_TAR_BYTES = 64 * 1024 * 1024

# `control.tar` compressions dpkg writes, each decoded under a size limit
_CONTROL_DECODERS: dict[str, Callable[[bytes, int], bytes]] = {
    **{codec.suffix: codec.decompress for codec in CODECS},
    ".zst": zstd_decompress,
}


def _control_of(fp: IO[bytes], path: str) -> bytes | None:
    """Return the ``control`` file of the ``.deb`` read from `fp`, refusing an oversized one.

    The ``control.tar`` member is decoded under `MAX_CONTROL_TAR_BYTES` and walked as a stream,
    so neither a compression bomb nor a tar of many members is inflated in full.

    Args:
        fp: The open ``.deb``.
        path: Its storage path, for messages.

    Raises:
        DebFileException: The ``control.tar`` member, its decoded tar, or the ``control`` file
            passes its size limit, or the member is in an unknown compression or does not
            decode.
        DebError: The ``control`` file is missing.

    Returns:
        The ``control`` file content, or None when it is not a regular file.
    """
    deb = debfile.DebFile(fileobj=fp)  # type: ignore[arg-type]
    part = next(
        (member for member in deb.getmembers() if member.name.startswith("control.tar")), None
    )
    if part is None:
        raise debfile.DebError("control.tar not found inside package")
    if part.size > MAX_CONTROL_BYTES:
        raise DebFileException(f"{part.name} of {path} is larger than {MAX_CONTROL_BYTES} bytes")
    decode = _CONTROL_DECODERS.get(part.name.removeprefix("control.tar"))
    if decode is None:
        raise DebFileException(f"{part.name} of {path} has an unknown compression")
    try:
        tar_data = decode(part.read(), MAX_CONTROL_TAR_BYTES)
    except SizeLimitError as exc:
        raise DebFileException(
            f"{part.name} of {path} decodes past {MAX_CONTROL_TAR_BYTES} bytes"
        ) from exc
    except CorruptStreamError as exc:
        raise DebFileException(f"Invalid .deb file {path}: {part.name}: {exc}") from exc

    with tarfile.open(fileobj=BytesIO(tar_data), mode="r|") as tar:
        for member in tar:
            if member.name not in ("./control", "control"):
                continue
            if member.size > MAX_CONTROL_BYTES:
                raise DebFileException(
                    f"control file of {path} is larger than {MAX_CONTROL_BYTES} bytes"
                )
            handle = tar.extractfile(member)
            return handle.read() if handle is not None else None
    raise debfile.DebError("File not found inside package")


class DebFile:
    """Read-side wrapper around a ``.deb`` package on a `Storage` backend.

    The control file is read once at construction; the storage handle is closed before the
    constructor returns, whether it succeeds or fails.

    Attributes:
        storage: Storage backend the file lives on.
        path: Path of the ``.deb`` file inside ``storage``.
        comp: Component name the package belongs to, or None for a pool without one.
        control: Content of the ``control`` file in ``control.tar``.
    """

    def __init__(self, storage: Storage, path: str, comp: str | None = None) -> None:
        """Initialize DebFile object.

        Args:
            storage: Storage backend.
            path: Path to the Debian package file.
            comp: Component name. Defaults to None.

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
        # `ArError` is the base of `DebError`; a truncated ar header raises a bare `OSError`, a
        # truncated `control.tar` member an `EOFError` or a `TarError`, and one whose compressed
        # data does not decode past its first tar header an `LZMAError` or a `zlib.error`
        except (ArError, tarfile.TarError, EOFError, OSError, lzma.LZMAError, zlib.error) as exc:
            raise DebFileException(f"Invalid .deb file {path}: {exc}") from exc

    def _read_control(self) -> bytes | None:
        """Read the ``control`` file, streaming when the backend can seek.

        Returns:
            The ``control`` file content.
        """
        try:
            with self.storage.get_stream(self.path) as fp:
                return _control_of(fp, self.path)
        except UnsupportedOperation as exc:
            logger.debug("%s cannot stream (%s); reading it into memory.", self.path, exc)
        with BytesIO(self.storage.get(self.path)) as fp:
            return _control_of(fp, self.path)

    def get_package(self) -> Package:
        """Get control data from the Debian package.

        The control file comes from the upload, so its `Package` and `Source` fields and the
        upload path are checked before they name a pool key; nothing has been transferred yet.

        Raises:
            DebFileException: If the control file is not a bytes object.
            DebFileException: If the control file is not UTF-8.
            DebFileException: If the `Package` field is missing or not a policy name.
            DebFileException: If the `Source` field, without its version, is not a policy name.
            DebFileException: If the pool key would be an unsafe storage path.

        Returns:
            Package object with control data.
        """
        control = self.control
        if not isinstance(control, bytes):
            raise DebFileException(f"control file of {self.path} is not a bytes object")
        # Debian Policy 5.1: control data is UTF-8
        # -> [VALIDATION.md](VALIDATION.md#control-file-encoding)
        try:
            control_lines = control.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DebFileException(f"control file of {self.path} is not UTF-8: {exc}") from exc
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
