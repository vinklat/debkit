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

from debkit.repo.compress import COMPRESS_MAP, DECOMPRESS_MAP
from debkit.repo.hashes import ChecksumException
from debkit.repo.package import Package, PackageException
from debkit.repo.release import Release

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.params import RepoParams

# create logger
logging.getLogger(__name__).addHandler(logging.NullHandler())


def _is_unbounded_brace(pattern: str, i: int) -> bool:
    """Return True if the ``{`` at position *i* opens an unbounded quantifier ``{n,}``.

    Bounded forms such as ``{n}`` and ``{n,m}`` cannot cause catastrophic
    backtracking and must not be treated as dangerous quantifiers.
    """
    j = i + 1
    length = len(pattern)
    while j < length and pattern[j].isdigit():
        j += 1
    if j >= length or pattern[j] != ",":
        return False  # {n} form — bounded
    j += 1  # skip comma
    return j < length and pattern[j] == "}"  # True only for {n,}


# Soft cap on decompressed Packages-indices size (bytes). Guards against
# zip-bomb-style payloads served over http(s). Documented in VALIDATION.md.
MAX_INDICES_BYTES = 256 * 1024 * 1024


class PackagesException(Exception):
    """Exception raised for errors in the Packages class.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Packages: {message}")


class Packages:
    """Container to store packages (Package objects).

    Attributes:
        storage (Storage): The storage object.
        params (RepoParams): The parameters for the Packages object.
        release (Release): The release object.
        packages (Dict[str, Package]): The packages objects from Packages indices file.
        used_indices_path (str): The Packages file used for self.packages.
        found_indices_files (List[str]): The list of all Package* files from Release.
        compress (Callable): The function used for compress.
        decompress (Callable): The function used for decompress.
        size (int): The number of packages.
        modified (bool): Whether the Packages object has been modified.
    """

    def __init__(self, storage: Storage, params: RepoParams) -> None:
        """Initialize a Packages object.

        Args:
            storage (Storage): The storage object of repo.
            params (RepoParams): The parameters of related repo.
        """
        self.storage = storage
        self.params = params
        self.release = Release(storage, params)
        self.packages: dict[str, Package] = {}
        self.used_indices_path = ""
        self.found_indices_paths: list[str] = []
        self.compress_fun: Callable[[bytes], bytes] | None = None
        self.size = 0
        self.modified = False

    def __copy__(self) -> Packages:
        """Create a shallow copy of this Packages object.

        Returns:
            Packages: A shallow copy of this Packages object.
        """
        ret = type(self)(self.storage, self.params)
        ret.release = copy(self.release)
        ret.packages = copy(self.packages)
        ret.used_indices_path = self.used_indices_path
        ret.found_indices_paths = self.found_indices_paths.copy()
        ret.compress_fun = self.compress_fun
        ret.size = self.size
        ret.modified = self.modified
        return ret

    def _index_inner(self, arch: str, suffix: str = "") -> str:
        """Compose the Release-relative inner path of a package index.

        One composition for the write and the read alike, so a written index lands
        where the loader looks -> [VALIDATION.md](VALIDATION.md#repository-index-paths).
        A flat repo hosts the index at the component or dist root; a componented one
        nests it under the architecture directory.

        Args:
            arch (str): The architecture name, or the source architecture.
            suffix (str): The compression suffix; empty for the uncompressed index.

        Returns:
            str: The path under `<comp>/`, or the bare filename for a flat repo.
        """
        name = f"{self.params.index_name(arch)}{suffix}"
        return name if self.params.is_flat else f"{self.params.arch_dir(arch)}/{name}"

    def setup(self, comp: str, arch: str) -> None:
        """Initialize the new empty Packages object.

        Args:
            comp (str): The component name.
            arch (str): The architecture name.
        """
        suffix = ".gz"
        self.used_indices_path = self.params.indices_path(self._index_inner(arch, suffix), comp)
        plain_path = self.params.indices_path(self._index_inner(arch), comp)
        self.found_indices_paths = [plain_path, self.used_indices_path]
        self.compress_fun = COMPRESS_MAP[suffix]
        self.release.setup(comp=comp, arch=arch)

    def _parse_indices(
        self, data: str, modify_rules: list[tuple[str, str, str]] | None = None
    ) -> int:
        """Read list of packages from data from Packages file.

        Use decompress function for unpack, parse data from headers format.

        Args:
            data (str): The data from the Packages file.
            modify_rules (Optional[List[Tuple[str, str, str]]]): A list of modification rules.
                Each rule is a tuple containing a field name, a pattern, and a replacement string.
                Defaults to None (no rules).

        Returns:
            int: The number of packages parsed.
        """
        if modify_rules is None:
            modify_rules = []

        blocks = re.split(r"\n{2,}", data)
        for block in blocks:
            if not block.strip():
                continue

            package = Package()
            pkg_name = package.parse(block)

            if pkg_name is None:
                continue

            # a Filename is remote metadata, so a hostile index can name a key outside the
            # storage root; a rejected stanza is dropped rather than failing the load, so one
            # bad record cannot stop a mirror -> [VALIDATION.md](VALIDATION.md#filename-safety)
            if is_unsafe_path(pkg_name):
                logging.warning(
                    "%s named in an index is an unsafe package path. Skipped.", pkg_name
                )
                continue

            # apply modification rules; only mark `modified` if a rule actually changed a field
            for field, pattern, replace in modify_rules:
                try:
                    before = package.fields.get(field)
                    package.field_re_sub(field, pattern, replace)
                    if package.fields.get(field) != before:
                        self.modified = True
                except PackageException as exc:
                    logging.warning("Modify rule failed for %s: %s", pkg_name, exc)

            self.packages[pkg_name] = package
        self.size = len(self.packages)
        return self.size

    def _serialize_indices(self) -> str:
        """Serialize the content of the Packages file to headers format.

        Returns:
            str: The serialized packages indices.
        """
        return "".join(package.serialize() for package in self.packages.values())

    def _find_packages_file(
        self, root_release: Release, comp: str, arch: str
    ) -> tuple[str, str, Callable[[bytes], bytes]]:
        """Find usable existing Package indices files from repo.

        There may be more than one indices file in the repo with different compression.
        This function finds all and choose best one for use.

        Args:
            root_release (Release): The root release object.
            comp (str): The component name.
            arch (str): The architecture name.

        Raises:
            PackagesException: There is no Packages* file.

        Returns:
            Tuple[str, str, Callable]: A tuple containing the
                path of the found file, the file suffix, and the decompression function.
                If no file is found, raise PackagesException.
        """
        ret: None | tuple[str, str, Callable[[bytes], bytes]] = None

        for suffix, decompress_fun in DECOMPRESS_MAP.items():
            # search for all Packages indices files
            # .xz compression preferred for use; verify path from Release file
            indices_path = self.params.indices_path(self._index_inner(arch, suffix), comp)
            indices_path_short = self.params.release_key(indices_path)

            if indices_path_short in root_release.digests:
                if not self.storage.exists(indices_path):
                    logging.debug("%s not exist...", indices_path)
                    continue

                # add found Package* files for sync
                self.found_indices_paths.append(indices_path)

                # get packages from first found Packages* file (.xz)
                if not self.used_indices_path:
                    logging.debug("%s found... (use this)", indices_path)
                    ret = indices_path, suffix, decompress_fun
                    self.used_indices_path = indices_path
                    continue

                # else
                logging.debug("%s found... (skip)", indices_path)

            else:
                logging.debug("%s not in Release...", indices_path)

        if ret is not None:
            return ret

        #  there is no any Packages indices file found / 404 everywhere
        if self.params.is_flat:
            raise PackagesException("there is no indices file.")

        # else
        raise PackagesException(f"missing indices file for arch {arch} in component {comp}.")

    def _verify_indices_checksum(
        self, root_release: Release, path: str, digests: dict[str, str]
    ) -> bool:
        """Verify checksum of Packages file.

        Args:
            root_release (Release): The root release object.
            path (str): The path of the Packages file.
            digests (Dict[str, str]): The checksums to verify.

        Returns:
            bool: True if the checksum is valid, False otherwise.
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
        modify_rules: list[tuple[str, str, str]] | None = None,
    ) -> bool:
        """Load 'Packages' indices file from repo.

        Args:
            root_release (Release): The root release object.
            comp (str): The component name.
            arch (str): The architecture name.
            modify_rules (Optional[List[Tuple[str, str, str]]]): The modify rules function.
                Defaults to None.

        Raises:
            ChecksumException: Checksum mismatch for Packages file.

        Returns:
            bool: True if the packages indices file is successfully loaded, False otherwise.
        """
        (indices_path, suffix, decompress_fun) = self._find_packages_file(root_release, comp, arch)

        enc_data = self.storage.get(indices_path)

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
            logging.info("No packages indices.")
            return False

        if decompress_fun is None:
            raw = enc_data
        else:
            raw = decompress_fun(enc_data)

        if len(raw) > MAX_INDICES_BYTES:
            raise PackagesException(
                f"Decompressed indices {indices_path} exceeds soft cap "
                f"({len(raw)} > {MAX_INDICES_BYTES} bytes)."
            )

        data = raw.decode("utf-8")

        pkgs_total = self._parse_indices(data, modify_rules=modify_rules)
        self.compress_fun = COMPRESS_MAP[suffix]

        if comp and arch:
            logging.info("Found %s packages in %s arch %s.", pkgs_total, comp, arch)
        else:
            logging.info("Found %s packages.", pkgs_total)
        return True

    def save(self) -> dict[str, dict[str, str]] | None:
        """Write serialized content to the Packages indices file.

        Optional compress data.

        Returns:
            Dict[Dict[str, str]]: The filename and digests of the encoded data.
              If the Packages object has not been modified, return None.
        """
        if not self.modified:
            return None

        data = self._serialize_indices()
        raw = data.encode("utf-8")
        ret: dict[str, dict[str, str]] = {}

        for filename in self.found_indices_paths:
            compress_fun = None
            for suffix, fun in COMPRESS_MAP.items():
                if filename.endswith(suffix):
                    compress_fun = fun

            enc_data = compress_fun(raw) if compress_fun is not None else raw

            logging.info("Save %s...", filename)
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
            brief (bool): If False, also includes packages list.

        Returns:
            Dict[str, Dict[str,Any]]: The dictionary representation of the packages object.
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
            package (Package): The package to add.
        """
        self.packages[str(package)] = package
        self.modified = True

    _MAX_PATTERN_LENGTH = 500

    @staticmethod
    def _validate_regex_safe(pattern: str) -> None:
        """Reject regex patterns that contain nested quantifiers.

        This is a lightweight heuristic that catches the most common ReDoS
        pattern family (quantified groups containing inner quantifiers, e.g.
        ``(a+)+``).  It does NOT guarantee protection against all catastrophic
        backtracking constructs.  This is acceptable for a local CLI tool
        where the pattern originates from the operator, not from untrusted
        network input.

        Raises:
            ValueError: If the pattern contains nested quantifiers or exceeds
                the maximum allowed length.
        """
        if len(pattern) > Packages._MAX_PATTERN_LENGTH:
            raise ValueError(
                f"Regex pattern too long ({len(pattern)} chars, "
                f"max {Packages._MAX_PATTERN_LENGTH})."
            )

        # Single-pass state machine detecting nested quantifiers.
        # Tracks whether a quantifier (+, *, ?, {n,m}) appears inside a group,
        # then checks if the group closing ')' is itself followed by a quantifier.
        quantifiers = {"+", "*", "?"}
        i = 0
        length = len(pattern)
        # Stack of booleans: True if current group contains a quantifier.
        group_has_quantifier: list[bool] = []

        while i < length:
            ch = pattern[i]

            # Skip escaped characters.
            if ch == "\\":
                i += 2
                continue

            # Skip character classes entirely.
            if ch == "[":
                i += 1
                if i < length and pattern[i] == "^":
                    i += 1
                if i < length and pattern[i] == "]":
                    i += 1
                while i < length and pattern[i] != "]":
                    if pattern[i] == "\\":
                        i += 1
                    i += 1
                i += 1
                continue

            if ch == "(":
                # Push new group onto stack.
                group_has_quantifier.append(False)
                i += 1
                # Skip Python's group-extension prefix `?` (e.g. `(?:`, `(?P<`,
                # `(?=`, `(?!`, `(?<=`, `(?<!`) so it is not misread as a
                # quantifier and does not falsely mark the group as quantifier-bearing.
                if i < length and pattern[i] == "?":
                    i += 1
                    if i < length and pattern[i] in "P<=!:":
                        i += 1
                continue

            if ch == ")":
                inner_has_quantifier = group_has_quantifier.pop() if group_has_quantifier else False
                # Check if the group is followed by a quantifier.
                next_i = i + 1
                if (
                    inner_has_quantifier
                    and next_i < length
                    and (
                        pattern[next_i] in quantifiers
                        or (pattern[next_i] == "{" and _is_unbounded_brace(pattern, next_i))
                    )
                ):
                    raise ValueError(
                        "Regex pattern rejected: nested quantifiers are not allowed "
                        "(potential ReDoS)."
                    )
                # Propagate: a child group that contained a quantifier counts
                # as a quantifier-bearing element in the parent group.
                if inner_has_quantifier and group_has_quantifier:
                    group_has_quantifier[-1] = True
                # Closing ')' followed by an unbounded quantifier also marks the parent.
                if next_i < length and (
                    pattern[next_i] in quantifiers
                    or (pattern[next_i] == "{" and _is_unbounded_brace(pattern, next_i))
                ):
                    if group_has_quantifier:
                        group_has_quantifier[-1] = True
                i += 1
                continue

            # Detect quantifier and mark the current group.
            # Only unbounded braces ({n,}) count; bounded {n} / {n,m} are safe.
            if ch in quantifiers or (ch == "{" and _is_unbounded_brace(pattern, i)):
                if group_has_quantifier:
                    group_has_quantifier[-1] = True
                i += 1
                continue

            i += 1

    def remove(self, fields: dict[str, Any]) -> set[str]:
        """Drop the matching packages from this index; never touch storage.

        An `all` package is listed in several arch indices, so the caller deletes each returned
        payload once → `RepoRW.remove_pkg`.

        Args:
            fields (Dict[str, str]): The fields to match a package
                (regex patterns; ALL must match).

        Raises:
            ValueError: If any pattern is not a valid regular expression.
            PackagesException: If a matched package has an unsafe Filename; nothing is dropped.

        Returns:
            Set[str]: Filenames of the dropped packages; empty when nothing matched.
        """
        for pattern in fields.values():
            self._validate_regex_safe(pattern)

        try:
            compiled = {field: re.compile(pattern) for field, pattern in fields.items()}
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

        removed: set[str] = set()
        for pkg_name, package in self.packages.items():
            if all(
                field in package.fields and rx.search(package.fields[field])
                for field, rx in compiled.items()
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
            List[str]: List of all indices file paths.
        """
        ret: list[str] = []
        ret.extend(self.found_indices_paths)
        ret.extend(self.release.get_indices_paths())

        return ret

    def verify_checksum(self, filename: str, incoming_digests: dict[str, str]) -> bool:
        """Verify checksum of a package.

        Args:
            filename (str): The filename to verify.
            incoming_digests (Dict[str,str]): The checksums to verify.

        Returns:
            bool: True if the checksum is valid, False otherwise.
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
            second (Packages): The second set of packages to compare with.

        Returns:
            Tuple[Dict[str, 'Package'], Dict[str, 'Package'], Dict[str, 'Package'],
            Dict[str, 'Package']]:
            A tuple containing dictionaries of added, removed, and updated packages.
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
            bool: True if the Packages object is empty, False otherwise.
        """
        return not bool(self.packages)

    def __len__(self) -> int:
        """Return the number of packages.

        Returns:
            int: The number of packages.
        """
        return len(self.packages)
