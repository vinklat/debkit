"""Repository diff class."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from debkit.repo.package import Package


class RepoDiff:
    """Class containing repository diff data.

    Attributes:
        added_pkgs (Dict[str, Dict[str, List[Package]]]): added packages
        removed_pkgs (Dict[str, Dict[str, List[Package]]]): removed packages
        updated_pkgs (Dict[str, Dict[str, List[Package]]]): updated packages
        old_versions_pkgs (Dict[str, Dict[str, List[Package]]]): old versions packages

    Example:
        >>> from debkit.repo.repo_diff import RepoDiff
        >>> repo_diff = RepoDiff()
        >>> repo_diff.added_pkgs["main"]["amd64"] = [pkg1, pkg2]
        >>> repo_diff.removed_pkgs["main"]["amd64"] = [pkg3, pkg4]
        >>> repo_diff.updated_pkgs["main"]["amd64"] = [pkg5, pkg6]
        >>> repo_diff.old_versions_pkgs["main"]["amd64"] = [pkg7, pkg8]
    """

    def __init__(self) -> None:
        """Initialize RepoDiff."""
        self.added_pkgs: dict[str, dict[str, list[Package]]] = {}
        self.removed_pkgs: dict[str, dict[str, list[Package]]] = {}
        self.updated_pkgs: dict[str, dict[str, list[Package]]] = {}
        self.old_versions_pkgs: dict[str, dict[str, list[Package]]] = {}

    def dump(self) -> dict[str, dict[str, dict[str, list[Package]]]]:
        """Dump RepoDiff to dict.

        Returns:
            Dict[str, Dict[str, Dict[str, List[Package]]]]: RepoDiff output dict
        """
        return {
            "added": self.added_pkgs,
            "removed": self.removed_pkgs,
            "updated": self.updated_pkgs,
            "old_versions": self.old_versions_pkgs,
        }

    def update(
        self,
        component: str,
        arch: str,
        added: dict[str, Package],
        removed: dict[str, Package],
        updated: dict[str, Package],
        old_versions: dict[str, Package],
    ) -> None:
        """Update RepoDiff with new data.

        Args:
            component (str): component name
            arch (str): architecture
            added (Dict[str, Package]): added packages
            removed (Dict[str, Package]): removed packages
            updated (Dict[str, Package]): updated packages
            old_versions (Dict[str, Package]): old versions packages
        """
        self.added_pkgs.setdefault(component, {})[arch] = list(added.values())
        self.removed_pkgs.setdefault(component, {})[arch] = list(removed.values())
        self.updated_pkgs.setdefault(component, {})[arch] = list(updated.values())
        self.old_versions_pkgs.setdefault(component, {})[arch] = list(old_versions.values())

    def get_pkgs_to_copy(self) -> list[Package]:
        """Get list of packages to copy.

        Returns:
            List[Package]: list of packages to copy
        """
        pkgs: list[Package] = []
        for component in self.added_pkgs.values():
            for arch in component.values():
                pkgs.extend(arch)
        for component in self.updated_pkgs.values():
            for arch in component.values():
                pkgs.extend(arch)
        return pkgs

    def count_pkgs_to_copy(self) -> int:
        """Get count of distinct payloads to copy; an `all` package in several arches counts once.

        Returns:
            int: count of packages to copy
        """
        return len(self.get_filenames_to_copy())

    def get_filenames_to_copy(self) -> set[str]:
        """Get list of filenames to copy.

        Returns:
            Set[str]: list of filenames to copy
        """
        return {str(pkg) for pkg in self.get_pkgs_to_copy()}

    def get_pkgs_to_remove(self) -> list[Package]:
        """Get list of packages to remove.

        Returns:
            List[Package]: list of packages to remove
        """
        pkgs: list[Package] = []
        for component in self.removed_pkgs.values():
            for arch in component.values():
                pkgs.extend(arch)
        # old versions are not removed: a package of the same name may be among the added ones
        return pkgs

    def count_pkgs_to_remove(self) -> int:
        """Get count of distinct payloads to remove; an `all` package in several arches counts once.

        Returns:
            int: count of packages to remove
        """
        return len(self.get_filenames_to_remove())

    def get_filenames_to_remove(self) -> set[str]:
        """Get list of filenames to remove.

        Returns:
            Set[str]: list of filenames to remove
        """
        return {str(pkg) for pkg in self.get_pkgs_to_remove()}

    def is_empty(self) -> bool:
        """Check if RepoDiff is empty.

        `update` records every component and arch it compared, so an unchanged slot holds
        empty lists and counts as empty.

        Returns:
            bool: True if no slot lists a package, False otherwise
        """
        return not any(
            pkgs
            for by_comp in self.dump().values()
            for by_arch in by_comp.values()
            for pkgs in by_arch.values()
        )
