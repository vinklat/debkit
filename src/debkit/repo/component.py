"""Class containg Debian packages (Packages objects) for each architecture."""

from __future__ import annotations

import logging
from copy import copy
from typing import TYPE_CHECKING, Any

from txstore import StorageException

from debkit.repo.packages import Packages, PackagesException
from debkit.repo.params import ARCH_ANY, SOURCE_ARCH
from debkit.repo.release import Release
from debkit.repo.sources import Sources

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.package import Package
    from debkit.repo.params import RepoParams

# create logger
logging.getLogger(__name__).addHandler(logging.NullHandler())


def container_for(arch: str) -> type[Packages]:
    """Return the index container class that owns an architecture.

    The source architecture is mirrored through a read-only `Sources` index, every
    other one through `Packages`. Both the read and the write path resolve the class
    here: `Packages.setup` names its file through `index_name`/`arch_dir`, so a
    `Packages` built for the source arch would write a binary index into the canonical
    `Sources` path, which the next load cannot parse
    -> [VALIDATION.md](VALIDATION.md#sources-stanzas).

    Args:
        arch (str): The architecture name.

    Returns:
        type[Packages]: `Sources` for the source architecture, `Packages` otherwise.
    """
    return Sources if arch == SOURCE_ARCH else Packages


class ComponentException(Exception):
    """Exception raised for errors in the Component class.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Component: {message}")


class Component:
    """Container to store sets of Debian packages for each architecture.

    Attributes:
        name (str): The name of the component.
        storage (Storage): The storage object.
        params (RepoParams): The parameters for the Component object.
        packages (Dict[str, Packages]): 'Packages' objects by architecture.
        root_release (Release): The release object.
        modified (bool): True if the component has been modified, False otherwise.
        is_flat (bool): Read-only; delegates to ``params.is_flat``.
    """

    def __init__(self, storage: Storage, params: RepoParams, release: Release, name: str) -> None:
        """Initialize a Component object.

        Args:
            storage (Storage): The storage object.
            params (RepoParams): The parameters for the Component object.
            release (Release): The release object.
            name (str): The name of the component.
        """
        self.storage = storage
        self.params = params
        self.packages: dict[str, Packages] = {}
        self.root_release = release
        self.name = name
        self.modified = False

    @property
    def is_flat(self) -> bool:
        """Whether the underlying repo layout is flat (delegates to ``params``)."""
        return self.params.is_flat

    def __copy__(self) -> Component:
        """Create a shallow copy of this Component object.

        Returns:
            Component: A shallow copy of this Component object.
        """
        ret = Component(self.storage, self.params, self.root_release, self.name)
        ret.packages = {arch: copy(packages) for arch, packages in self.packages.items()}
        ret.modified = self.modified
        return ret

    def load(
        self,
        archs: list[str],
        modify_rules: list[tuple[str, str, str]] | None = None,
        strict_arch: bool = False,
    ) -> int:
        """Load package sets (Packages objects) for all architectures.

        Args:
            archs (List[str]): The list of architectures.
            modify_rules (Optional[List[Tuple[str, str, str]]]): A list of modification rules.
                Each rule is a tuple containing a field name, a pattern, and a replacement string.
                Defaults to None.
            strict_arch (bool): If True, strict architecture check is performed.
                Defaults to False.

        Raises:
            ComponentException: If the architecture check fails.

        Returns:
            int: The total number of packages loaded.
        """
        npkgs = 0
        for arch in archs:
            packages = container_for(arch)(self.storage, self.params)

            if not self.is_flat:
                try:
                    packages.release.load(comp=self.name, arch=arch, root_release=self.root_release)
                except StorageException as exc:
                    if strict_arch:
                        raise ComponentException(f"Cannot load {self.name}: {arch}") from exc

                    # If the architecture is not available, skip it
                    logging.warning("Architecture %s not available.", arch)
                    continue

            try:
                packages.load(self.root_release, self.name, arch, modify_rules=modify_rules)
            except PackagesException as exc:
                if strict_arch:
                    raise ComponentException(f"Cannot load {self.name}: {arch}") from exc

                # If the Packages not available, skip it
                logging.warning("Packages for %s failed.", arch)
                continue

            # Release file validation check
            if not self.is_flat and not packages.release.is_empty():
                if "Architecture" in packages.release.fields:
                    comp_arch_raw = packages.release.fields["Architecture"]
                elif "Architectures" in packages.release.fields:
                    comp_arch_raw = packages.release.fields["Architectures"]
                else:
                    raise ComponentException("no 'Architecture' field in Release")

                # `Architectures` can list multiple tokens (e.g. "amd64 arm64 all");
                # `all` is universally compatible.
                comp_archs = set(comp_arch_raw.split())
                if arch not in comp_archs and ARCH_ANY not in comp_archs:
                    raise ComponentException("architecture check mismatch.")

            self.packages[arch] = packages
            npkgs += packages.size
        return npkgs

    def save(self) -> bool:
        """Save all package sets (Packages objects) for all architectures.

        Returns:
            bool: True if any package was saved, False otherwise.
        """
        ret: bool = False
        digests: dict[str, dict[str, str]] | None = {}
        for packages in self.packages.values():
            digests = packages.save()

            if digests is None:
                continue

            ret = True
            for filename, digest in digests.items():
                self.root_release.update_digests(self.params.release_key(filename), digest)

        return ret

    def dump(self, brief: bool = False) -> dict[str, dict[str, Any]]:
        """Dump architectures containing Packages objects to a dict.

        Args:
            brief (bool): If True, dump brief output. Defaults to False.

        Returns:
            Dict[str, Dict]: A dictionary where the keys are architectures and the values
                are the dumped packages.
        """
        return {arch: packages.dump(brief=brief) for arch, packages in self.packages.items()}

    def get_indices_paths(self) -> list[str]:
        """Get list of indices files paths.

        Returns:
            List[str]: A list of indices paths for each package in the component.
        """
        return [
            path for packages in self.packages.values() for path in packages.get_indices_paths()
        ]

    def setup(self, archs: list[str]) -> None:
        """Setup the component.

        Args:
            archs (List[str]): The list of architectures.
        """
        for arch in archs:
            packages = container_for(arch)(self.storage, self.params)
            packages.setup(self.name, arch)
            self.packages[arch] = packages
        self.modified = False

    def insert_pkg(self, arch: str, package: Package) -> None:
        """Add deb package object to the component.

        Args:
            arch (str): The architecture.
            package (Package): The Package object to insert.
        """
        if arch not in self.packages:
            packages = container_for(arch)(self.storage, self.params)
            packages.setup(self.name, arch)
            self.packages[arch] = packages

        logging.info("Insert package: %s %s", package, arch)
        # package.fields["X-Factory-Architecture"] = arch

        self.packages[arch].insert(package)
        self.modified = True

    def remove_pkg(self, fields_patterns: dict[str, Any]) -> set[str]:
        """Drop matching packages from every arch index of the component; storage is untouched.

        A mirrored `Sources` index removes nothing →
        [VALIDATION.md](VALIDATION.md#sources-stanzas).

        Args:
            fields_patterns (Dict[str, Any]): The fields of the package to match.

        Returns:
            Set[str]: Filenames dropped from any arch, each once.
        """
        removed: set[str] = set()
        for packages in self.packages.values():
            removed |= packages.remove(fields_patterns)
        if removed:
            self.modified = True
        return removed
