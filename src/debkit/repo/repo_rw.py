"""Class containing repository with write ability.

Inherits from Repo.
"""

import logging
from email.utils import formatdate
from typing import TYPE_CHECKING, Any

from debkit.repo import Repo
from debkit.repo.component import Component

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package
    from debkit.repo.params import RepoParams

# create logger
logging.getLogger(__name__).addHandler(logging.NullHandler())


class RepoWriteException(Exception):
    """Exception raised for errors in writing to the repository.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"{message}")


class RepoRW(Repo):
    """Class that extens Repo with modificaton functionality.

    Attributes:
        modified (bool): Whether the repo has been modified.
    """

    def __init__(
        self, storage: "Storage", params: "RepoParams", gpg: "GnuPG | None" = None
    ) -> None:
        """Initialize RepoRW.

        Args:
            storage (Storage): storage
            params (RepoParams): repository parameters
            gpg (Optional[GnuPG]): GnuPG object. Defaults to None.
        """
        super().__init__(storage, params, gpg=gpg)
        self.modified = False

    def setup(self, fields: dict[str, str] | None = None) -> None:
        """Setup the repository.

        Args:
            fields (Optional[Dict[str, str]]): The fields to set. Defaults to None.
        """
        logging.info("Setting up a new repository.")
        self.release.setup(fields=fields)
        self.modified = True

    def save(self) -> bool:
        """Save the repository.

        Returns:
            bool: True if the repository was saved, False otherwise.
        """
        if not self.modified:
            return False

        saved: bool = False
        for component in self.components.values():
            saved = component.save() or saved

        if saved:
            self.release.fields.update(
                {
                    "Date": formatdate(localtime=False, usegmt=True),
                    "Components": " ".join(self.components),
                }
            )
            self.release.save()

        return saved

    def reset(self) -> None:
        """Reset the repository."""
        self.components = {}
        self.release.reset()

    def add_component(self, name: str) -> None:
        """Add component to the repo.

        Args:
            name (str): component name
        """
        new_component = Component(self.storage, self.params, self.release, name)

        self.components[name] = new_component
        self.modified = True

    def remove_pkg(self, fields_patterns: dict[str, Any]) -> bool:
        """Remove deb package objects matching ``fields_patterns`` from the repo.

        Args:
            fields_patterns (Dict[str, Any]): The fields of the package to match.

        Returns:
            bool: True iff at least one package was removed.
        """
        logging.info("Removing packages with fields patterns: %s", fields_patterns)

        # Materialise the per-component results: a generator inside ``any()`` would
        # short-circuit and only the first matching component would actually drop
        # the packages.
        results = [component.remove_pkg(fields_patterns) for component in self.components.values()]
        removed = any(results)
        if removed:
            self.modified = True
        return removed

    def insert_pkg(self, component_name: str, arch: str, package: "Package") -> None:
        """Add deb package object to the repo.

        Args:
            component_name (str): The name of the component.
            arch (str): The architecture.
            package (Package): The Package object to insert.
        """
        if component_name in self.components:
            component = self.components[component_name]
        else:
            component = Component(self.storage, self.params, self.release, component_name)
            component.setup([arch])
            self.components[component_name] = component

        component.insert_pkg(arch, package)
        self.modified = True

    def transfer_from(
        self, source_storage: "Storage", path_src: str, path_dst: str | None = None
    ) -> dict[str, str]:
        """Transfer a file from an external storage into this repo's storage.

        Backend-specific ACL handling is delegated to the destination
        storage (e.g. ``StorageS3.default_acl``); this method does not
        hard-code any ACL string.

        Args:
            source_storage: The storage to read from.
            path_src: Source path in `source_storage`.
            path_dst: Destination path in this repo. Defaults to `path_src`.

        Returns:
            Digests of the transferred object.
        """
        return source_storage.transfer(path_src, self.storage, path_dst=path_dst)
