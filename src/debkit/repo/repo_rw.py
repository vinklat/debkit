"""Class containing repository with write ability.

Inherits from Repo.
"""

from __future__ import annotations

import logging
import re
from email.utils import formatdate
from typing import TYPE_CHECKING

from debkit.repo import Repo
from debkit.repo.component import Component
from debkit.repo.params import FLAT_COMPONENT

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package
    from debkit.repo.params import RepoParams

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class RepoRW(Repo):
    """Class that extens Repo with modificaton functionality.

    Attributes:
        modified (bool): Whether the repo has been modified.
    """

    def __init__(self, storage: Storage, params: RepoParams, gpg: GnuPG | None = None) -> None:
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

        A flat layout has one component slot and its Release lists no components, so a
        `Components` field is dropped -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            fields (Optional[Dict[str, str]]): The fields to set. Defaults to None.
        """
        logger.info("Setting up a new repository.")
        if fields is not None and self.params.is_flat:
            fields = {key: value for key, value in fields.items() if key != "Components"}
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
            self.release.fields["Date"] = formatdate(localtime=False, usegmt=True)
            # a flat Release lists no components; its single slot keeps whatever name it loaded
            if not self.params.is_flat:
                self.release.fields["Components"] = " ".join(self.components)
            self.release.save()

        return saved

    def reset(self) -> None:
        """Reset the repository."""
        self.components = {}
        self.release.reset()

    def remove_pkg(self, fields_patterns: dict[str, re.Pattern[str]]) -> bool:
        """Drop packages matching ``fields_patterns`` from the indices and delete their payloads.

        Several arch indices list one `all` payload, so each payload is deleted once.

        Args:
            fields_patterns (Dict[str, re.Pattern[str]]): Compiled pattern per package field.

        Raises:
            StorageException: If the backend fails to delete a payload.

        Returns:
            bool: True iff at least one package was removed.
        """
        logger.info(
            "Removing packages with fields patterns: %s",
            {field: rx.pattern for field, rx in fields_patterns.items()},
        )

        removed: set[str] = set()
        for component in self.components.values():
            removed |= component.remove_pkg(fields_patterns)
        for filename in sorted(removed):
            logger.info("Remove package: %s", filename)
            self.storage.delete(filename)
        if removed:
            self.modified = True
        return bool(removed)

    def insert_pkg(self, component_name: str, arch: str, package: Package) -> None:
        """Add deb package object to the repo.

        A flat layout has one component slot, so ``component_name`` is ignored there and the
        package joins the component the repo loaded
        -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            component_name (str): The name of the component.
            arch (str): The architecture.
            package (Package): The Package object to insert.
        """
        if self.params.is_flat:
            component_name = next(iter(self.components), FLAT_COMPONENT)

        if component_name in self.components:
            component = self.components[component_name]
        else:
            component = Component(self.storage, self.params, self.release, component_name)
            component.setup([arch])
            self.components[component_name] = component

        component.insert_pkg(arch, package)
        self.modified = True

    def transfer_from(
        self, source_storage: Storage, path_src: str, path_dst: str | None = None
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
