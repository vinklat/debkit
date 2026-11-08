"""Base class containing Debian repository indices and data.

Method update() loads the repository.
"""

import logging
import re
from collections.abc import Generator
from copy import copy
from typing import TYPE_CHECKING, Any

from txstore import StorageException

from debkit.repo.component import Component
from debkit.repo.packages import Packages
from debkit.repo.params import (
    ARCH_ANY,
    FLAT_COMPONENT,
    RepoParams,
    is_safe_name,
    is_valid_arch,
)
from debkit.repo.release import Release
from debkit.repo.repo_diff import RepoDiff

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# the architecture directory of a binary index key, `<comp>/binary-<arch>/Packages[.<ext>]`
_BINARY_INDEX_KEY = re.compile(r"binary-(?P<arch>[^/]+)/Packages(?:\.[^/]+)?")


class RepoReadException(Exception):
    """Exception raised for errors in reading the repository."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Repo read: {message}")


class Repo:
    """Object containing Debian repository indices and data.

    Attributes:
        storage (Storage): The storage for the repo.
        params (RepoParams): The input parameters.
        gpg (GnuPG): The GnuPG object.
        release (Release): The root Release file of repo.
        components (Dict[str, Component]): The components of the repo by its names
        modified (bool): True if the repo has been modified, False otherwise.
    """

    def __init__(
        self, storage: "Storage", params: "RepoParams", gpg: "GnuPG | None" = None
    ) -> None:
        """Init repo object.

        Args:
            storage (Storage): The storage for the repo.
            params (RepoParams): The input parameters.
            gpg (Optional[GnuPG]): The GnuPG object.
        """
        self.storage = storage
        self.params = params
        self.gpg = gpg
        self.release = Release(storage, params, gpg=gpg)
        self.components: dict[str, Component] = {}
        self.modified = False

        logger.debug(
            "Repo: Storage=%s, Dist=%s, Components=%s",
            storage,
            params.dist,
            params.components,
        )

    def __copy__(self) -> "Repo":
        """Create a shallow copy of this Repo.

        Returns:
            Repo: A shallow copy of this Repo.
        """
        ret = Repo(self.storage, self.params, gpg=self.gpg)
        ret.release = copy(self.release)
        ret.components = {
            comp_name: component.copy_onto(ret.release)
            for comp_name, component in self.components.items()
        }
        ret.modified = self.modified
        return ret

    def copy_from(self, source_repo: "Repo") -> None:
        """Init repo from another repo.

        Args:
            source_repo (Repo): The source repo to copy from.
        """
        copy_repo = copy(source_repo)
        self.release = copy_repo.release
        self.components = copy_repo.components
        self.modified = copy_repo.modified

    def get_components(self) -> Generator[str]:
        """Get the components of the repo.

        Yields:
            Generator[str, None, None]: The name of a component.
        """
        # try get from params
        if isinstance(self.params.components, list):
            yield from self.params.components

        # try get from Release file; its tokens name storage directories, so an unsafe one
        # is dropped like a bad architecture token
        # -> [VALIDATION.md](VALIDATION.md#debian-source-strings)
        elif "Components" in self.release.fields:
            for name in self.release.fields.get("Components", "").split():
                if is_safe_name(name):
                    yield name
                else:
                    logger.warning("Release component %r is not a safe name. Skipped.", name)

        else:
            yield FLAT_COMPONENT

    def get_archs(self) -> Generator[str]:
        """Get the architectures of the repo.

        The `--architectures` selection wins; without one the Release tokens decide. A
        Release without `Architectures` yields the empty arch of a flat repo, or the
        binary architectures a componented one lists indices for
        -> [VALIDATION.md](VALIDATION.md#architectures).

        Yields:
            List[str]: The name of an arch.
        """
        if isinstance(self.params.archs, list) and len(self.params.archs) > 0:
            yield from self.params.archs
            return

        archs = list(self.release_archs())
        if archs or "Architectures" in self.release.fields:
            yield from archs
        elif self.params.is_flat:
            yield ""
        else:
            yield from self._listed_binary_archs()

    def _listed_binary_archs(self) -> list[str]:
        """Get the binary architectures the root Release lists a `Packages` index for.

        Only the selected components count; a directory name that is not an architecture
        token, or `all`, is skipped like a Release token.

        Returns:
            List[str]: Sorted architecture names, without duplicates.
        """
        archs: set[str] = set()
        for comp in self.get_components():
            prefix = f"{comp}/"
            for key in self.release.digests:
                match = _BINARY_INDEX_KEY.fullmatch(key[len(prefix) :])
                if match is None or not key.startswith(prefix):
                    continue
                arch = match["arch"]
                if arch == ARCH_ANY:
                    continue
                if not is_valid_arch(arch):
                    logger.warning("%r in a Release index path is not an architecture.", arch)
                    continue
                archs.add(arch)
        if archs:
            logger.info("Release declares no Architectures; using its indices: %s", sorted(archs))
        return sorted(archs)

    def release_archs(self) -> Generator[str]:
        """Get the architectures the root Release declares, ignoring the selection.

        Reads `Architectures`, else the singular `Architecture`, and skips `all`. A
        Release token that is not a well-formed architecture name is remote metadata
        steering a storage path, so it is dropped and logged
        -> [VALIDATION.md](VALIDATION.md#architectures).

        Yields:
            str: The name of an arch.
        """
        if "Architectures" in self.release.fields:
            field = "Architectures"
        elif "Architecture" in self.release.fields:
            field = "Architecture"
        else:
            return

        for arch in self.release.fields.get(field, "").split():
            if arch == ARCH_ANY:
                continue
            if not is_valid_arch(arch):
                logger.warning("%r in Release %s is not an architecture. Skipped.", arch, field)
                continue
            yield arch

    def update(self, strict_arch: bool = False) -> None:
        """Load Debian repository.

        This method does a similar thing to `apt-get update` command.

        Args:
            strict_arch (bool): Whether to use strict architecture. Defaults to False.
                If True, it will raise an exception if the architecture is not available.
                If False, it will skip the architecture and continue loading other architectures.

        Raises:
            RepoReadException: Error in reading the repository.
        """
        try:
            self.release.load()
        except StorageException as exc:
            raise RepoReadException(f"{exc}") from exc

        if not self.params.is_flat and "Components" not in self.release.fields:
            logger.warning("Components field not found in Release of non-flat repo.")

        # a reload replaces the components: one the new Release drops must not survive
        self.components = {}
        pkgs_total = 0
        for component_name in self.get_components():
            component = Component(self.storage, self.params, self.release, component_name)

            pkgs_total += component.load(list(self.get_archs()), strict_arch=strict_arch)

            self.components[component_name] = component

        logger.debug("Total %s packages in %s.", pkgs_total, self.storage)
        if pkgs_total == 0:
            logger.warning("Empty repo.")

    def dump(self, brief: bool = False) -> dict[str, dict[str, Any]]:
        """Dump the repository data to a dict.

        Parameters:
            brief (bool): Whether to dump a brief version of output. Defaults to False.

        Returns:
            Dict[str, Dict[str,Any]]: A dict representation of the repository.
                The dictionary includes the release,
                components of the repository.
        """
        # add Release (mandatory)
        ret: dict[str, dict[str, Any]] = {
            "release": self.release.dump(brief=brief),
            "components": {},
        }

        # add components (mandatory)
        ret["components"] = {
            component_name: component.dump(brief=brief)
            for component_name, component in self.components.items()
        }

        return ret

    def stats(self) -> dict[str, dict[str, dict[str, int]]]:
        """Get statistics of the repository.

        Returns:
            Dict[str, Dict[str, Dict[str, int]]]: A dict representation of the
                repository statistics.
        """
        ret: dict[str, dict[str, dict[str, int]]] = {}

        for comp_name, component in self.components.items():
            if comp_name not in ret:
                ret[comp_name] = {}
            for arch, packages in component.packages.items():
                if arch not in ret[comp_name]:
                    ret[comp_name][arch] = {
                        "packages_total": len(packages.packages),
                        "packages_bytes": 0,
                    }
                    for _, package in packages.packages.items():
                        ret[comp_name][arch]["packages_bytes"] += int(package.fields["Size"])
        return ret

    def get_packages(self) -> Generator["Package"]:
        """Get all filenames of packeges in repository.

        Yields:
            Generator["Package", None, None]: The package.
        """
        for _, component in self.components.items():  # not used comp name
            for _, packages in component.packages.items():  # not used arch
                for _, package in packages.packages.items():
                    yield package

    def diff(self, other_repo: "Repo") -> RepoDiff:
        """Get the difference between this repo and another repo.

        Args:
            other_repo (Repo): The other repo to diff with.

        Returns:
            RepoDiff: The object containing differences between the repos.
        """
        diff = RepoDiff()

        if self.modified or other_repo.modified or (self.release.hash != other_repo.release.hash):
            for component_name, component in self.components.items():
                for arch, packages in component.packages.items():
                    if (
                        component_name in other_repo.components
                        and arch in other_repo.components[component_name].packages
                    ):
                        other_packages = other_repo.components[component_name].packages[arch]
                    else:
                        # other repo does not have this component or arch
                        other_packages = Packages(other_repo.storage, other_repo.params)

                    added, removed, updated, old_versions = other_packages.diff(packages)
                    diff.update(component_name, arch, added, removed, updated, old_versions)

        return diff

    def _release_blocks(self) -> Generator[Release]:
        """Yield every Release whose digest block is authoritative for this repo.

        The ownership gate and the digest check read the same blocks, so both walk
        this one iterator -> [CRYPTO.md](CRYPTO.md#verification-order).

        Yields:
            Generator[Release]: The root Release, then every component Release.
        """
        yield self.release

        for component in self.components.values():
            for packages in component.packages.values():
                yield packages.release

    def _release_owns(self, release_key: str) -> bool:
        """Report whether a signed Release digest block covers this key.

        The root Release is GPG-verified and every component Release is chained to it
        by digest, so a key either Release lists is authoritative for that path. A
        package stanza naming the same key must not outrank it
        -> [CRYPTO.md](CRYPTO.md#verification-order).

        Args:
            release_key (str): Path as the Release lists it, from `release_key()`.

        Returns:
            bool: True when a Release digest block names the key.
        """
        return any(release_key in release.digests for release in self._release_blocks())

    def _release_keys(self) -> set[str]:
        """Collect every key a signed Release digest block covers.

        The set form of `_release_owns` for a caller testing many keys at once: it walks
        the same blocks, so both answer alike -> [CRYPTO.md](CRYPTO.md#verification-order).

        Returns:
            Set[str]: Every key a Release digest block lists.
        """
        return {key for release in self._release_blocks() for key in release.digests}

    def verify_checksum(self, filename: str, digests_incoming: dict[str, str]) -> bool:
        """Verify file checksum in all available indices files.

        Args:
            filename (str): The name of the file to verify.
            digests_incoming (Dict[str, str]): The digests of the file.

        Returns:
            bool: Whether the file checksum was verified.
        """
        release_key = self.params.release_key(filename)

        # no digest block lists the root Release files, so each copy is bound to the bytes
        # the load fetched and verified, and no stanza may vouch for one
        # -> [CRYPTO.md](CRYPTO.md#verification-order)
        if release_key in ["Release", "InRelease", "Release.gpg"]:
            return self.release.verify_root_digests(release_key, digests_incoming)

        # a package index is remote metadata, so a stanza naming a path the signed Release
        # also covers would define that path's content instead of the GPG-rooted Release.
        # The Release owns every key inside its digest block, keyed by the path it lists, and
        # is checked against exactly the blocks that granted it that ownership
        # -> [CRYPTO.md](CRYPTO.md#verification-order)
        if self._release_owns(release_key):
            return any(
                release.verify_digests(release_key, digests_incoming)
                for release in self._release_blocks()
            )

        # only paths outside every block fall to the containers, each keyed by the `Filename`
        # field it parsed, so this is one dict lookup and a path no index names simply misses.
        # A suffix gate here would have to track every compression `dpkg-source` emits
        return any(
            packages.verify_checksum(filename, digests_incoming)
            for component in self.components.values()
            for packages in component.packages.values()
        )
