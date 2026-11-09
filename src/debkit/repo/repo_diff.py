"""Repository diff class."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from debkit.repo.package import Package
    from debkit.repo.packages import PackagesDiff


class RepoDiff:
    """How a repository differs from an earlier state, one `PackagesDiff` per index slot.

    Attributes:
        slots: Each compared component and arch → how its packages changed.
    """

    def __init__(self) -> None:
        """Initialize RepoDiff."""
        self.slots: dict[tuple[str, str], PackagesDiff] = {}

    def dump(self) -> dict[str, dict[str, dict[str, list[Package]]]]:
        """Dump RepoDiff to dict.

        Returns:
            Each bucket (`added`, `removed`, `updated`, `old_versions`) → component → arch →
            its packages.
        """
        ret: dict[str, dict[str, dict[str, list[Package]]]] = {
            "added": {},
            "removed": {},
            "updated": {},
            "old_versions": {},
        }
        for (component, arch), pkgs_diff in self.slots.items():
            for bucket, pkgs in pkgs_diff._asdict().items():
                ret[bucket].setdefault(component, {})[arch] = list(pkgs.values())
        return ret

    def update(self, component: str, arch: str, pkgs_diff: PackagesDiff) -> None:
        """Record how one slot changed.

        Args:
            component: component name
            arch: architecture
            pkgs_diff: how its packages changed
        """
        self.slots[component, arch] = pkgs_diff

    def get_pkgs_to_copy(self) -> list[Package]:
        """Get list of packages to copy.

        Returns:
            List of packages to copy
        """
        pkgs: list[Package] = []
        for pkgs_diff in self.slots.values():
            pkgs.extend(pkgs_diff.added.values())
        for pkgs_diff in self.slots.values():
            pkgs.extend(pkgs_diff.updated.values())
        return pkgs

    def count_pkgs_to_copy(self) -> int:
        """Get count of distinct payloads to copy; an `all` package in several arches counts once.

        Returns:
            Count of packages to copy
        """
        return len(self.get_filenames_to_copy())

    def get_filenames_to_copy(self) -> set[str]:
        """Get list of filenames to copy.

        Returns:
            List of filenames to copy
        """
        return {str(pkg) for pkg in self.get_pkgs_to_copy()}

    def get_pkgs_to_remove(self) -> list[Package]:
        """Get list of packages to remove.

        Returns:
            List of packages to remove
        """
        # old versions are not removed: a package of the same name may be among the added ones
        return [pkg for pkgs_diff in self.slots.values() for pkg in pkgs_diff.removed.values()]

    def count_pkgs_to_remove(self) -> int:
        """Get count of distinct payloads to remove; an `all` package in several arches counts once.

        Returns:
            Count of packages to remove
        """
        return len(self.get_filenames_to_remove())

    def get_filenames_to_remove(self) -> set[str]:
        """Get list of filenames to remove.

        Returns:
            List of filenames to remove
        """
        return {str(pkg) for pkg in self.get_pkgs_to_remove()}

    def is_empty(self) -> bool:
        """Check if RepoDiff is empty.

        `update` records every component and arch it compared, so an unchanged slot holds
        empty buckets and counts as empty.

        Returns:
            True if no slot lists a package, False otherwise
        """
        return all(pkgs_diff.is_empty() for pkgs_diff in self.slots.values())
