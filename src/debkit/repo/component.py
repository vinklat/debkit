"""Class containg Debian packages (Packages objects) for each architecture."""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import TYPE_CHECKING, Any

from txstore import StorageException

from debkit.repo.packages import Packages, PackagesException, PackagesNotFoundException
from debkit.repo.params import ARCH_ANY, SOURCE_ARCH, is_valid_arch
from debkit.repo.release import Release
from debkit.repo.sources import Sources

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.package import Package
    from debkit.repo.params import RepoParams

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def container_for(arch: str) -> type[Packages]:
    """Return the index container class that owns an architecture.

    The source architecture is mirrored through a read-only `Sources` index, every
    other one through `Packages`. Both the read and the write path resolve the class
    here: `Packages.setup` names its file through `index_name`/`arch_dir`, so a
    `Packages` built for the source arch would write a binary index into the canonical
    `Sources` path, which the next load cannot parse
    -> [VALIDATION.md](VALIDATION.md#sources-stanzas).

    Args:
        arch: The architecture name.

    Returns:
        `Sources` for the source architecture, `Packages` otherwise.
    """
    return Sources if arch == SOURCE_ARCH else Packages


class IndexLoad(Enum):
    """What a component load skips when an arch's index does not load.

    The modes and the commands using each -> [VALIDATION.md](VALIDATION.md#index-loading).

    Attributes:
        STRICT: Every arch must load; a missing or unreadable index raises.
        WRITABLE: A missing index is skipped, so an insert starts it; an unreadable one
            raises, since a save would replace it, except the never-written `Sources`.
        READ_ONLY: A missing or unreadable index is skipped with a warning.
    """

    STRICT = "strict"
    WRITABLE = "writable"
    READ_ONLY = "read-only"


class ComponentException(Exception):
    """Exception raised for errors in the Component class."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Component: {message}")


class Component:
    """Container to store sets of Debian packages for each architecture.

    Attributes:
        name: The name of the component.
        storage: The storage object.
        params: The parameters for the Component object.
        packages: 'Packages' objects by architecture.
        root_release: The release object.
        is_flat: Read-only; delegates to ``params.is_flat``.
    """

    def __init__(self, storage: Storage, params: RepoParams, release: Release, name: str) -> None:
        """Initialize a Component object.

        Args:
            storage: The storage object.
            params: The parameters for the Component object.
            release: The release object.
            name: The name of the component.
        """
        self.storage = storage
        self.params = params
        self.packages: dict[str, Packages] = {}
        self.root_release = release
        self.name = name

    @property
    def is_flat(self) -> bool:
        """Whether the underlying repo layout is flat (delegates to ``params``)."""
        return self.params.is_flat

    def _flat_container(self, arch: str) -> Packages | None:
        """Get the container a flat layout already holds for the index file of `arch`.

        A flat layout names one `Packages` (and one `Sources`) file whatever the arch, so
        every arch slot shares one container; separate ones would each save the whole file
        and the last save would win -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            arch: The architecture of the slot.

        Returns:
            The shared container, or None when the layout is componented or no slot holds that index
            file yet.
        """
        if not self.is_flat:
            return None
        kind = container_for(arch)
        return next((pkgs for pkgs in self.packages.values() if type(pkgs) is kind), None)

    def containers(self) -> list[Packages]:
        """Get every index container of the component, each once.

        Flat arch slots share one container
        -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Returns:
            The distinct containers, in slot order.
        """
        return list({id(pkgs): pkgs for pkgs in self.packages.values()}.values())

    def slot_packages(self, arch: str) -> dict[str, Package]:
        """Get the packages one arch slot reports, keyed by `Filename`.

        A componented slot reports its whole index. The one index of a flat layout holds
        every arch, whichever are selected, so a flat arch slot reports the packages of its
        arch and of `all`, as a componented `binary-<arch>/` index lists them; a package with
        no `Architecture`, or the empty arch of a flat repo naming none, is reported whole.
        Reports read it; payload planning reads the whole index, since a flat sync copies the
        index whole -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            arch: The architecture of the slot.

        Returns:
            The slot's packages.
        """
        packages = self.packages[arch]
        if not (self.is_flat and arch):
            return packages.packages
        return {
            filename: package
            for filename, package in packages.packages.items()
            if package.fields.get("Architecture", arch) in (arch, ARCH_ANY)
        }

    def load(self, archs: list[str], mode: IndexLoad = IndexLoad.WRITABLE) -> int:
        """Load package sets (Packages objects) for all architectures.

        An arch with no index file is skipped unless `STRICT`, so an insert can start its
        index. One whose index exists but does not decode, or whose per-arch Release storage
        fails, is skipped only in `READ_ONLY`, or for the never-written `Sources` index in
        `WRITABLE`: a caller that saves would replace it with a fresh index holding its own
        changes alone. A storage failure reading an existing index is a backend failure and
        propagates in every mode -> [VALIDATION.md](VALIDATION.md#index-loading).

        Args:
            archs: The list of architectures.
            mode: What an arch that does not load may skip. Defaults to `WRITABLE`.

        Raises:
            ComponentException: If an arch cannot be loaded and may not be skipped, or the
                architecture check fails.
            StorageException: An index exists but its storage fails to read.

        Returns:
            The total number of packages loaded.
        """
        npkgs = 0
        for arch in archs:
            shared = self._flat_container(arch)
            if shared is not None:
                self.packages[arch] = shared
                continue
            kind = container_for(arch)
            packages = kind(self.storage, self.params)
            may_skip_unreadable = mode is IndexLoad.READ_ONLY or (
                mode is IndexLoad.WRITABLE and kind is Sources
            )

            if not self.is_flat:
                try:
                    packages.release.load_component(self.name, arch, root_release=self.root_release)
                except StorageException as exc:
                    if not may_skip_unreadable:
                        raise ComponentException(f"Cannot load {self.name}: {arch}: {exc}") from exc

                    logger.warning("Architecture %s not available.", arch)
                    continue

            try:
                packages.load(self.root_release, self.name, arch)
            except PackagesException as exc:
                missing = isinstance(exc, PackagesNotFoundException)
                if mode is IndexLoad.STRICT or not (missing or may_skip_unreadable):
                    raise ComponentException(f"Cannot load {self.name}: {arch}: {exc}") from exc

                logger.warning("Packages for %s failed: %s", arch, exc)
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
            True if any package was saved, False otherwise.
        """
        ret: bool = False
        # a container shared by several flat slots is saved once
        for packages in self.containers():
            digests = packages.save()

            if digests is None:
                continue

            ret = True
            for filename, digest in digests.items():
                self.root_release.update_digests(self.params.release_key(filename), digest)

        return ret

    def dump(self) -> dict[str, dict[str, Any]]:
        """Dump architectures containing Packages objects to a dict.

        Returns:
            A dictionary where the keys are architectures and the values are the dumped packages.
        """
        return {
            arch: packages.dump(self.slot_packages(arch).values())
            for arch, packages in self.packages.items()
        }

    def get_indices_paths(self) -> list[str]:
        """Get list of indices files paths.

        Returns:
            A list of indices paths for each package in the component.
        """
        return [path for packages in self.containers() for path in packages.get_indices_paths()]

    def setup(self, archs: list[str]) -> None:
        """Setup the component.

        Args:
            archs: The list of architectures.
        """
        for arch in archs:
            self.packages[arch] = self._new_slot(arch)

    def _new_slot(self, arch: str) -> Packages:
        """Get a fresh container for `arch`, or the one a flat layout already holds.

        Args:
            arch: The architecture of the slot.

        Returns:
            The container the slot holds.
        """
        shared = self._flat_container(arch)
        if shared is not None:
            return shared
        packages = container_for(arch)(self.storage, self.params)
        packages.setup(self.name, arch)
        return packages

    def insert_pkg(self, arch: str, package: Package) -> None:
        """Add deb package object to the component.

        Args:
            arch: The architecture.
            package: The Package object to insert.

        Raises:
            ComponentException: If ``arch`` is neither empty nor an architecture name.
        """
        # the arch names the `binary-<arch>/` index path, so a caller that skipped the
        # action-layer check cannot steer it -> [VALIDATION.md](VALIDATION.md#architectures)
        if arch and not is_valid_arch(arch):
            raise ComponentException(f"invalid architecture: {arch!r}")
        if arch not in self.packages:
            self.packages[arch] = self._new_slot(arch)

        logger.info("Insert package: %s %s", package, arch)

        self.packages[arch].insert(package)

    def remove_pkg(self, fields_patterns: dict[str, re.Pattern[str]]) -> set[str]:
        """Drop matching packages from every arch index of the component; storage is untouched.

        A mirrored `Sources` index removes nothing →
        [VALIDATION.md](VALIDATION.md#sources-stanzas).

        Args:
            fields_patterns: Compiled pattern per package field.

        Returns:
            Filenames dropped from any arch, each once.
        """
        removed: set[str] = set()
        for packages in self.containers():
            removed |= packages.remove(fields_patterns)
        return removed
