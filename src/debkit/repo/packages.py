"""Class containing packages (Package objects) from 'Packages' indices file."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from copy import copy
from typing import (
    TYPE_CHECKING,
    Any,
)

from txstore import calc_digests, compare_digests, is_unsafe_path

from debkit.repo.compress import CODECS, CorruptStreamError, SizeLimitError, codec_for, read_capped
from debkit.repo.hashes import ChecksumException
from debkit.repo.package import Package
from debkit.repo.release import Release

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.params import RepoParams

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


# Soft cap on decompressed Packages-indices size (bytes); decoding stops as soon as it is
# passed, so a zip-bomb-style payload is never inflated in full
# -> [VALIDATION.md](VALIDATION.md#index-decoding)
MAX_INDICES_BYTES = 256 * 1024 * 1024

# Debian Policy 5.1: a stanza ends at a blank line, which may hold spaces or tabs
# -> [VALIDATION.md](VALIDATION.md#index-decoding)
_STANZA_SEPARATOR = re.compile(r"\n[ \t]*\n\s*")


class PackagesException(Exception):
    """Exception raised for errors in the Packages class."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Packages: {message}")


class PackagesNotFoundException(PackagesException):
    """No index file the Release lists for the arch is in storage; nothing was read."""


class Packages:
    """Container to store packages (Package objects).

    Attributes:
        storage: The storage object.
        params: The parameters for the Packages object.
        release: The release object.
        packages: The packages objects from Packages indices file.
        used_indices_path: The Packages file used for self.packages.
        found_indices_paths: Every index form the Release lists that this container reads or
            writes: found by `load`, planned by `setup`.
        size: The number of packages.
        modified: Whether the Packages object has been modified.
    """

    def __init__(self, storage: Storage, params: RepoParams) -> None:
        """Initialize a Packages object.

        Args:
            storage: The storage object of repo.
            params: The parameters of related repo.
        """
        self.storage = storage
        self.params = params
        self.release = Release(storage, params)
        self.packages: dict[str, Package] = {}
        self.used_indices_path = ""
        self.found_indices_paths: list[str] = []
        self.modified = False

    def __copy__(self) -> Packages:
        """Snapshot this index: its Release and package map are copies, the packages shared.

        A load or a mutation replaces packages rather than changing one in place, so the
        snapshot keeps describing the index as it was. A `Sources` copy stays a `Sources`.

        Returns:
            A copy of the same class.
        """
        ret = type(self)(self.storage, self.params)
        ret.release = copy(self.release)
        ret.packages = copy(self.packages)
        ret.used_indices_path = self.used_indices_path
        ret.found_indices_paths = self.found_indices_paths.copy()
        ret.modified = self.modified
        return ret

    @property
    def size(self) -> int:
        """Number of packages, derived on each read so no insert or removal leaves it stale."""
        return len(self.packages)

    def _index_inner(self, arch: str, suffix: str = "") -> str:
        """Compose the Release-relative inner path of a package index.

        One composition for the write and the read alike, so a written index lands
        where the loader looks -> [VALIDATION.md](VALIDATION.md#repository-index-paths).
        A flat repo hosts the index at the component or dist root; a componented one
        nests it under the architecture directory.

        Args:
            arch: The architecture name, or the source architecture.
            suffix: The compression suffix; empty for the uncompressed index.

        Returns:
            The path under `<comp>/`, or the bare filename for a flat repo.
        """
        name = f"{self.params.index_name(arch)}{suffix}"
        return name if self.params.is_flat else f"{self.params.arch_dir(arch)}/{name}"

    def setup(self, comp: str, arch: str) -> None:
        """Initialize the new empty Packages object.

        A flat repo carries no per-component Release, as its loader reads none.

        Args:
            comp: The component name.
            arch: The architecture name.
        """
        self.used_indices_path = self.params.indices_path(self._index_inner(arch, ".gz"), comp)
        plain_path = self.params.indices_path(self._index_inner(arch), comp)
        self.found_indices_paths = [plain_path, self.used_indices_path]
        if not self.params.is_flat:
            self.release.setup_component(comp, arch)

    @staticmethod
    def _stanzas(data: str) -> list[str]:
        """Split a decoded index into its stanzas, CRLF line ends read as LF.

        Args:
            data: The decoded index.

        Returns:
            Every stanza holding more than whitespace, in index order.
        """
        blocks = _STANZA_SEPARATOR.split(data.replace("\r\n", "\n"))
        return [block for block in blocks if block.strip()]

    def _parse_indices(self, data: str) -> int:
        """Read list of packages from data from Packages file.

        Use decompress function for unpack, parse data from headers format.

        Args:
            data: The data from the Packages file.

        Returns:
            The number of packages parsed.
        """
        for block in self._stanzas(data):
            package = Package()
            pkg_name = package.parse(block)

            if pkg_name is None:
                continue

            # a Filename is remote metadata, so a hostile index can name a key outside the
            # storage root, or the root itself once an empty value is normalized to `.`; a
            # rejected stanza is dropped rather than failing the load, so one bad record
            # cannot stop a mirror -> [VALIDATION.md](VALIDATION.md#filename-safety)
            if pkg_name == "." or is_unsafe_path(pkg_name):
                logger.warning("%s named in an index is an unsafe package path. Skipped.", pkg_name)
                continue

            self.packages[pkg_name] = package
        return self.size

    def _serialize_indices(self) -> str:
        """Serialize the content of the Packages file to headers format.

        Returns:
            The serialized packages indices.
        """
        return "".join(package.serialize() for package in self.packages.values())

    def _find_packages_file(
        self, root_release: Release, comp: str, arch: str
    ) -> tuple[list[str], str, Callable[[bytes, int], bytes]]:
        """Find the index files the Release lists and storage holds, and pick one to read.

        Every listed form present is found, the uncompressed one included, so a save
        rewrites each; the first in `CODECS` order is read. Nothing is assigned, so a
        repeated `load` finds the same files.

        Args:
            root_release: The root release object.
            comp: The component name.
            arch: The architecture name.

        Raises:
            PackagesNotFoundException: There is no Packages* file.

        Returns:
            Every found index path, the path to read, and its decompression function.
        """
        found: list[str] = []
        chosen: tuple[str, Callable[[bytes, int], bytes]] | None = None

        for codec in CODECS:
            indices_path = self.params.indices_path(self._index_inner(arch, codec.suffix), comp)

            if self.params.release_key(indices_path) not in root_release.digests:
                logger.debug("%s not in Release...", indices_path)
                continue
            if not self.storage.exists(indices_path):
                logger.debug("%s not exist...", indices_path)
                continue

            found.append(indices_path)
            if chosen is None:
                logger.debug("%s found... (use this)", indices_path)
                chosen = indices_path, codec.decompress
            else:
                logger.debug("%s found... (skip)", indices_path)

        if chosen is not None:
            return found, *chosen

        #  there is no any Packages indices file found / 404 everywhere
        if self.params.is_flat:
            raise PackagesNotFoundException("there is no indices file.")

        raise PackagesNotFoundException(
            f"missing indices file for arch {arch} in component {comp}."
        )

    def _read_indices(self, root_release: Release, path: str) -> bytes:
        """Read an index no larger than the Release records, before its digest is checked.

        Args:
            root_release: The root release object.
            path: The path of the index file.

        Raises:
            ChecksumException: The file is larger than the size a checked Release records.
            PackagesException: No checked Release records its size and it is stored past
                `MAX_INDICES_BYTES`.

        Returns:
            The stored index bytes -> [VALIDATION.md](VALIDATION.md#object-sizes).
        """
        declared: int | None = None
        if self.params.verify_digests:
            key = self.params.release_key(path)
            sizes = [
                size
                for release in (root_release, self.release)
                if (size := release.declared_size(key)) is not None
            ]
            declared = max(sizes) if sizes else None
        try:
            return read_capped(
                self.storage, path, MAX_INDICES_BYTES if declared is None else declared
            )
        except SizeLimitError as exc:
            if declared is None:
                raise PackagesException(f"Stored indices {exc}.") from exc
            raise ChecksumException(f"Mismatch for {path}: {exc}.") from exc

    def _verify_indices_checksum(
        self, root_release: Release, path: str, digests: dict[str, str]
    ) -> bool:
        """Verify checksum of Packages file.

        Args:
            root_release: The root release object.
            path: The path of the Packages file.
            digests: The checksums to verify.

        Returns:
            True if the checksum is valid, False otherwise.
        """
        # map the full path back to the key the Release lists it under
        path = self.params.release_key(path)

        if root_release.verify_digests(path, digests):
            return True

        if self.release.verify_digests(path, digests):
            return True

        return False

    def load(
        self,
        root_release: Release,
        comp: str,
        arch: str,
    ) -> bool:
        """Load 'Packages' indices file from repo.

        Args:
            root_release: The root release object.
            comp: The component name.
            arch: The architecture name.

        Raises:
            ChecksumException: Checksum mismatch for Packages file, or a file larger than the
                size a checked Release records.
            PackagesNotFoundException: No Packages* file the Release lists is in storage.
            PackagesException: The index is truncated, does not decode, is stored or decodes
                past `MAX_INDICES_BYTES`, or is not UTF-8; a missing one raises the
                `PackagesNotFoundException` subclass.

        Returns:
            True if the packages indices file is successfully loaded, False otherwise.
        """
        found, indices_path, decompress_fun = self._find_packages_file(root_release, comp, arch)
        # replaced, not appended: a repeated load, or one after `setup`, reads storage afresh
        self.found_indices_paths = found
        self.used_indices_path = indices_path
        enc_data = self._read_indices(root_release, indices_path)

        # Verify the digest BEFORE deciding the payload is "legitimately empty".
        # `_find_packages_file` only guarantees the key exists in Release and in
        # storage; a truncated / 0-byte object against a non-empty digest in
        # Release must fail loudly instead of being silently treated as
        # "no packages for this arch" (which would mask repo-vs-pool drift
        # and bypass `strict_arch` in the caller).
        if self.params.verify_digests:
            digests = calc_digests(enc_data)
            if not self._verify_indices_checksum(root_release, indices_path, digests):
                raise ChecksumException(f"Mismatch for {indices_path}.")

        if not enc_data:
            logger.info("No packages indices.")
            return False

        try:
            raw = decompress_fun(enc_data, MAX_INDICES_BYTES)
        except SizeLimitError as exc:
            raise PackagesException(
                f"Decompressed indices {indices_path} exceeds soft cap ({MAX_INDICES_BYTES} bytes)."
            ) from exc
        except CorruptStreamError as exc:
            raise PackagesException(f"Cannot decode indices {indices_path}: {exc}") from exc

        # Debian Policy 5.1: control data is UTF-8 -> [VALIDATION.md](VALIDATION.md#index-decoding)
        try:
            data = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackagesException(f"Cannot decode indices {indices_path}: {exc}") from exc

        pkgs_total = self._parse_indices(data)

        if comp and arch:
            logger.info("Found %s packages in %s arch %s.", pkgs_total, comp, arch)
        else:
            logger.info("Found %s packages.", pkgs_total)
        return True

    def save(self) -> dict[str, dict[str, str]] | None:
        """Write serialized content to every Packages indices file, each in its own codec.

        Returns:
            The filename and digests of the encoded data. If the Packages object has not been
            modified, return None.
        """
        if not self.modified:
            return None

        data = self._serialize_indices()
        raw = data.encode("utf-8")
        ret: dict[str, dict[str, str]] = {}

        for filename in self.found_indices_paths:
            enc_data = codec_for(filename).compress(raw)

            logger.info("Save %s...", filename)
            self.storage.put(filename, enc_data)
            ret[filename] = calc_digests(enc_data)

        # Per-arch Release is saved exactly once, not once per indices filename.
        rdigests = self.release.save()
        if rdigests is not None and self.release.filename is not None:
            ret[self.release.filename] = rdigests

        return ret

    def dump(self, brief: bool = False) -> dict[str, dict[str, Any]]:
        """Dump packages object to a dict.

        Args:
            brief: If False, also includes packages list.

        Returns:
            The dictionary representation of the packages object.
        """
        ret: dict[str, dict[str, Any]] = {"packages": {"files": self.found_indices_paths}}

        # add packages
        if brief:
            ret["packages"]["data"] = len(self.packages)
        else:
            ret["packages"]["data"] = list(self.packages.values())

        # add release
        if not self.release.is_empty():
            ret["release"] = self.release.dump(brief=brief)

        return ret

    def insert(self, package: Package) -> None:
        """Add package to the Packages object.

        Args:
            package: The package to add.
        """
        self.packages[str(package)] = package
        self.modified = True

    def remove(self, fields: dict[str, re.Pattern[str]]) -> set[str]:
        """Drop the matching packages from this index; never touch storage.

        An `all` package is listed in several arch indices, so the caller deletes each returned
        payload once → `RepoRW.remove_pkg`.

        Args:
            fields: Compiled pattern per package field; a package matches when every pattern finds a
                match in its field → [VALIDATION.md](VALIDATION.md#package-pattern-matching).

        Raises:
            PackagesException: If a matched package has an unsafe Filename; nothing is dropped.

        Returns:
            Filenames of the dropped packages; empty when nothing matched.
        """
        removed: set[str] = set()
        for pkg_name, package in self.packages.items():
            if all(
                field in package.fields and rx.search(package.fields[field])
                for field, rx in fields.items()
            ):
                if is_unsafe_path(pkg_name):
                    raise PackagesException(f"unsafe filename: {pkg_name}")
                removed.add(pkg_name)

        for pkg_name in removed:
            self.packages.pop(pkg_name)
        if removed:
            self.modified = True
        return removed

    def get_indices_paths(self) -> list[str]:
        """Get list of all indices files.

        Returns:
            List of all indices file paths.
        """
        ret: list[str] = []
        ret.extend(self.found_indices_paths)
        ret.extend(self.release.get_indices_paths())

        return ret

    def verify_checksum(self, filename: str, incoming_digests: dict[str, str]) -> bool:
        """Verify checksum of a package.

        Args:
            filename: The filename to verify.
            incoming_digests: The checksums to verify.

        Returns:
            True if the checksum is valid, False otherwise.
        """
        # every container keys by the `Filename` field itself, so the index is the lookup
        package = self.packages.get(filename)
        if package is None:
            return False

        return compare_digests(incoming_digests, package.get_digests())

    def diff(
        self, second: Packages
    ) -> tuple[dict[str, Package], dict[str, Package], dict[str, Package], dict[str, Package]]:
        """Compare two sets of packages.

        Args:
            second: The second set of packages to compare with.

        Returns:
            Packages by name: added, removed, and updated in `second`, and the old versions the
            updated ones replace.
        """
        added_packages = {
            pkg_name: package
            for pkg_name, package in second.packages.items()
            if pkg_name not in self.packages
        }
        removed_packages = {
            pkg_name: package
            for pkg_name, package in self.packages.items()
            if pkg_name not in second.packages
        }
        updated_packages = {
            pkg_name: package
            for pkg_name, package in second.packages.items()
            if pkg_name in self.packages and package != self.packages[pkg_name]
        }
        old_versions = {
            pkg_name: package
            for pkg_name, package in self.packages.items()
            if pkg_name in updated_packages
        }

        return added_packages, removed_packages, updated_packages, old_versions

    def is_empty(self) -> bool:
        """Check if the Packages object is empty.

        Returns:
            True if the Packages object is empty, False otherwise.
        """
        return not bool(self.packages)

    def __len__(self) -> int:
        """Return the number of packages.

        Returns:
            The number of packages.
        """
        return len(self.packages)
