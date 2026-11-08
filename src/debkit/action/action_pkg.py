"""Debian repo operations commands."""

import builtins
import logging
import re
from copy import copy
from typing import TYPE_CHECKING, Any

from humanize import naturalsize
from txstore import maybe_transaction

# from pympler import tracker
from debkit.action import (
    Action,
    ActionException,
    OutputType,
    attach_storage_plain,
    dump_json,
)
from debkit.argtypes import pkg_pattern_type
from debkit.repo.changes import Changes
from debkit.repo.debfile import DebFile
from debkit.repo.params import ARCH_ANY, SOURCE_ARCH, is_valid_arch
from debkit.repo.release import ReleaseNotFoundException

if TYPE_CHECKING:
    from argparse import Namespace

    from txstore import Storage

    from debkit._options import EnvArgumentParser
    from debkit.repo.repo_diff import RepoDiff


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_REPO_FIELDS = {"Architectures": "amd64 arm64 all", "Components": "main"}


class ActionPkg(Action):
    """Actions with debian packages in repository.

    Attributes:
        output (OutputType): Output type.
    """

    def __init__(self, pars: "Namespace", create_path: bool = False):
        """Initialize ActionPkg.

        Args:
            pars (Namespace): Command line arguments from argparser.
            create_path (bool): Create path or bucket if True.
        """
        super().__init__(pars, create_path=create_path)
        self.output: OutputType = pars.output if hasattr(pars, "output") else OutputType.brief
        self.pkg_pattern: dict[str, re.Pattern[str]] | None = getattr(pars, "pkg_pattern", None)
        self.changes_filename: str | None = (
            pars.changes_filename if hasattr(pars, "changes_filename") else None
        )
        self.changes_sha256: str | None = (
            pars.changes_sha256 if hasattr(pars, "changes_sha256") else None
        )
        self.deb_filename: str | None = pars.deb_filename if hasattr(pars, "deb_filename") else None
        self.component: str = pars.component if hasattr(pars, "component") else "main"

        self.incoming_storage: Storage | None = (
            attach_storage_plain(pars, pars.incoming_url, create_path=True)
            if hasattr(pars, "incoming_url")
            else None
        )

    def dump(self) -> None:
        """Dump repository to stdout."""
        self.target_repo.update()
        logger.debug("%s", str(self.target_repo.storage.metrics))
        dump_dict = self.target_repo.dump(brief=self.output == OutputType.brief)

        if self.output == OutputType.brief:
            self._print_brief_dump(dump_dict)
        elif self.output == OutputType.json:
            self._print_json_dump(dump_dict)

    def _print_brief_dump(self, dump: dict[str, Any]) -> None:
        """Print a brief summary of the dump to stdout.

        Args:
            dump (Dict[str, Any]): A dictionary containing the dump data.
        """
        print("Release:")
        try:
            for key, value in dump["release"]["data"].items():
                print(f"  {key}: {value}")
        except KeyError:
            print("  <No release data>")

        if "components" in dump:
            print("Components:")
            for component_name, component in dump["components"].items():
                print(f"  {component_name}:")
                for arch_name, arch in component.items():
                    try:
                        print(f"    {arch_name}:", end=" ")
                        print(", ".join(f"{k}: {v}" for k, v in arch["release"]["data"].items()))
                    except KeyError:
                        print("<No release data>")
                    print(f"      {arch['packages']['data']} packages")
        if "packages" in dump:
            print("  1 packages")

    def _print_json_dump(self, dump: dict[str, Any]) -> None:
        """Print the dump in JSON format.

        Args:
            dump (Dict[str, Any]): A dictionary containing the dump data.

        Raises:
            TypeError: If the dump is not JSON serializable.
        """
        try:
            json_formatted_str = dump_json(dump)
        except TypeError as type_error:
            raise TypeError(f"Repo is not JSON serializable: {type_error}") from type_error
        print(json_formatted_str)

    def stats(self) -> None:
        """Print repository statistics to stdout."""
        self.target_repo.update()
        stats = self.target_repo.stats()

        if self.output == OutputType.brief:
            self._print_brief_stats(stats)
        elif self.output == OutputType.json:
            self._print_json_stats(stats)

    def _print_brief_stats(self, stats: dict[str, dict[str, dict[str, int]]]) -> None:
        """Print brief naturalized statistics to stdout.

        Args:
            stats (Dict[str, Dict[str, Dict[str, int]]]): A dictionary containing
                the stats data.
        """
        print("Packages:")
        for component, arch in stats.items():
            print(f"  {component}:")
            for arch_name, arch_data in arch.items():
                try:
                    print(
                        f"    {arch_name}: {naturalsize(arch_data['packages_bytes'])}, "
                        f"{arch_data['packages_total']} packages"
                    )
                except KeyError:
                    print(f"    {arch_name}: <no data>")
        print("Indices read:")
        print(
            f"  {naturalsize(self.target_repo.storage.metrics.read_bytes)}, "
            f"{self.target_repo.storage.metrics.read_objects_total} files"
        )

    def _print_json_stats(self, stats: dict[str, dict[str, dict[str, int]]]) -> None:
        """Print JSON statistics to stdout.

        Args:
            stats (Dict[str, Dict[str, Dict[str, int]]]): A dictionary containing
                the stats data.
        """
        ret = {
            "packages": stats,
            "indices": {
                "files_total": self.target_repo.storage.metrics.read_objects_total,
                "files_bytes": self.target_repo.storage.metrics.read_bytes,
            },
        }

        json_formatted_str = dump_json(ret)
        print(json_formatted_str)

    def list(self) -> None:
        """List packages in repository to stdout."""
        self.target_repo.update()

        if self.output == OutputType.brief:
            print("\n".join(str(package) for package in self.target_repo.get_packages()))
        elif self.output == OutputType.json:
            json_formatted_str = dump_json(list(self.target_repo.get_packages()))
            print(json_formatted_str)

    def _binary_archs(self) -> builtins.list[str]:
        """Get the repository architectures a binary package can be inserted into.

        A mirrored `Sources` index is read-only, so `source` never receives a `.deb`
        -> [VALIDATION.md](VALIDATION.md#sources-stanzas). The empty arch names no
        `binary-<arch>/` directory, so only a flat repo inserts under it
        -> [VALIDATION.md](VALIDATION.md#architectures).

        Returns:
            List[str]: The repository architectures without the source one.
        """
        is_flat = self.target_repo.params.is_flat
        return [
            arch
            for arch in self.target_repo.get_archs()
            if arch != SOURCE_ARCH and (arch or is_flat)
        ]

    def _changes_deb_files(
        self, incoming_storage: "Storage", changes_filename: str
    ) -> builtins.list[str]:
        """Load and check a `.changes` upload and list the `.deb` files it carries.

        Args:
            incoming_storage (Storage): The storage holding the upload.
            changes_filename (str): The `.changes` file in it.

        Raises:
            ActionException: An architecture token is not an architecture name.
            ActionException: An architecture token names the source one.

        Returns:
            List[str]: The `.deb` files the upload lists.
        """
        changes = Changes(incoming_storage)
        changes.load(changes_filename, digest_expected=self.changes_sha256)
        for arch in changes.get_archs():
            if not is_valid_arch(arch):
                raise ActionException(f"Invalid architecture in {changes_filename}: {arch!r}")

        # `Architecture` lists every arch the upload built, so a source plus binary
        # build names `source` beside the binary one; reading the first token alone
        # let the field order decide the upload
        if changes.names_source_arch():
            raise ActionException("Source packages are not supported yet")

        return changes.get_deb_files()

    def insert(self) -> None:
        """Insert packages into repository.

        Raises:
            ActionException: If both --changes-filename and --deb-filename are provided.
            ActionException: If neither --changes-filename nor --deb-filename are provided.
            ActionException: If the .deb filename does not end with '.deb'.
            ActionException: If the .changes architecture names the source one.
            ActionException: If an architecture token is not an architecture name.
            ActionException: If the package control field names the source architecture.
            ActionException: If an `all` or arch-less package finds no binary architecture
                in the target.
        """
        if (self.changes_filename is None) == (self.deb_filename is None):
            raise ActionException(
                "Please provide either --changes-filename or --deb-filename, not both"
            )

        if self.incoming_storage is None:
            return

        # load or setup target repository
        try:
            self.target_repo.update()
        except ReleaseNotFoundException as exc:
            # no repository - create new one; any other read failure must not reset an
            # existing one
            logger.debug("Target repository not found... (%s)", exc)

            self.target_repo.setup(fields=DEFAULT_REPO_FIELDS)
        prev_repo = copy(self.target_repo)

        # process deb files
        if self.changes_filename is not None:
            filenames = self._changes_deb_files(self.incoming_storage, self.changes_filename)
        else:
            if self.deb_filename is None or not self.deb_filename.endswith(".deb"):
                raise ActionException(f"Invalid .deb filename: {self.deb_filename}")
            filenames = [self.deb_filename]

        binary_archs = self._binary_archs()

        with maybe_transaction(self.target_repo.storage, self.history_retention):
            for filename in filenames:
                debfile_obj = DebFile(self.incoming_storage, filename, comp=self.component)
                package = debfile_obj.get_package()

                pkg_arch = package.get_arch()
                if pkg_arch is not None and not is_valid_arch(pkg_arch):
                    raise ActionException(f"Invalid architecture in {filename}: {pkg_arch!r}")
                # the control field is read straight out of the uploaded .deb, so it can
                # name the source arch; the mirrored source index is read-only, so refuse
                # before the transfer as the changes path already does
                if pkg_arch == SOURCE_ARCH:
                    raise ActionException("Source packages are not supported yet")

                # each package by its own control field, so neither the upload's field
                # order nor its file order picks the arches
                # -> [VALIDATION.md](VALIDATION.md#architectures)
                archs = (
                    [pkg_arch] if pkg_arch is not None and pkg_arch != ARCH_ANY else binary_archs
                )
                if not archs:
                    raise ActionException(
                        f"Cannot insert {filename} (Architecture: {pkg_arch or 'none'}): "
                        "the target Release lists no binary architecture"
                    )

                digests = self.target_repo.transfer_from(
                    self.incoming_storage, filename, path_dst=debfile_obj.filename_field
                )
                package.set_digests(digests)

                for arch in archs:
                    self.target_repo.insert_pkg(self.component, arch, package)

            ret_diff = self.target_repo.diff(prev_repo)

            if not ret_diff.is_empty():
                self.target_repo.save()

        if self.output == OutputType.brief:
            self._print_pkg_brief(ret_diff)
        elif self.output == OutputType.json:
            self._print_pkg_json(ret_diff)

    def remove(self) -> None:
        """Remove the packages matching the parsed ``--pkg-pattern`` from the repository.

        Raises:
            ValueError: If no package pattern was parsed.
            ActionException: If no package matched.
        """
        if self.pkg_pattern is None:
            raise ValueError("No package pattern provided.")

        self.target_repo.update()
        prev_repo = copy(self.target_repo)

        with maybe_transaction(self.target_repo.storage, self.history_retention):
            if not self.target_repo.remove_pkg(self.pkg_pattern):
                raise ActionException("No packages matched the pattern.")
            ret_diff = self.target_repo.diff(prev_repo)

            if not ret_diff.is_empty():
                self.target_repo.save()

        if self.output == OutputType.brief:
            self._print_pkg_brief(ret_diff)
        elif self.output == OutputType.json:
            self._print_pkg_json(ret_diff)

    def _print_pkg_brief(self, diff: "RepoDiff") -> None:
        """Print package operation brief output.

        Args:
            diff (RepoDiff): Repository diff object.
        """
        if diff.count_pkgs_to_copy() > 0:
            print(f"Inserted {diff.count_pkgs_to_copy()} packages.")
        if diff.count_pkgs_to_remove() > 0:
            print(f"Removed {diff.count_pkgs_to_remove()} packages.")
        if diff.count_pkgs_to_copy() > 0:
            print(
                f"{self.target_repo.storage.metrics.write_objects_total} objects "
                f"({naturalsize(self.target_repo.storage.metrics.write_bytes)}) transferred."
            )

    def _print_pkg_json(self, diff: "RepoDiff") -> None:
        """Print package operation json output.

        Args:
            diff (RepoDiff): Repository diff object.
        """
        json_dict: dict[str, Any] = {
            "packages": diff.dump(),
            "transfer": {"metrics": {}, "errors": {}},
        }
        json_dict["transfer"]["metrics"]["target_storage"] = self.target_repo.storage.metrics.dump()

        print(dump_json(json_dict, indent=4))

    @staticmethod
    def add_insert_cli_args(parser: "EnvArgumentParser") -> None:
        """Add arguments for insert.

        Args:
            parser (EnvArgumentParser): The argument parser to which the arguments should
                be added.
        """
        parser.add_env_argument(
            "--changes-filename",
            env="DEBKIT_CHANGES_FILENAME",
            dest="changes_filename",
            help=".changes filename of incoming packages",
        )
        parser.add_env_argument(
            "--changes-sha256",
            env="DEBKIT_CHANGES_SHA256",
            dest="changes_sha256",
            help="validate .changes file SHA256 checksum",
        )
        parser.add_env_argument(
            "--deb-filename",
            env="DEBKIT_DEB_FILENAME",
            dest="deb_filename",
            help=".deb filename of incoming package",
        )
        parser.add_env_argument(
            "--incoming-url",
            env="DEBKIT_INCOMING_URL",
            dest="incoming_url",
            help="url for incoming files",
            required=True,
        )
        parser.add_env_argument(
            "--component",
            env="DEBKIT_COMPONENT",
            dest="component",
            help="component of Debian repo for incoming packages",
            default="main",
        )

    @staticmethod
    def add_remove_cli_args(parser: "EnvArgumentParser") -> None:
        """Add arguments for delete.

        Args:
            parser (EnvArgumentParser): The argument parser to which the arguments should
                be added.
        """
        parser.add_env_argument(
            "--pkg-pattern",
            env="DEBKIT_PKG_PATTERN",
            dest="pkg_pattern",
            type=pkg_pattern_type,
            help="JSON format of regexp patterns of package fields. "
            'Example: {"Filename":"filename_1.deb|filename_2.deb"}',
            required=True,
        )
