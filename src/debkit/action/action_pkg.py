"""Debian repo operations commands."""

import builtins
import json
import logging
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
from debkit.repo.changes import Changes
from debkit.repo.debfile import DebFile
from debkit.repo.params import ARCH_ANY, SOURCE_ARCH, is_valid_arch
from debkit.repo.release import ReleaseReadException

if TYPE_CHECKING:
    from argparse import Namespace

    from txstore import Storage

    from debkit._options import EnvArgumentParser
    from debkit.repo.repo_diff import RepoDiff


# create logger
logging.getLogger(__name__).addHandler(logging.NullHandler())

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
        self.pkg_pattern: str | None = pars.pkg_pattern if hasattr(pars, "pkg_pattern") else None
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
        if self.target_repo is None:
            return

        self.target_repo.update()
        logging.debug("%s", str(self.target_repo.storage.metrics))
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
        if self.target_repo is None:
            return

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
        if self.target_repo is None:
            return

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
        if self.target_repo is None:
            return

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
        if self.target_repo is None:
            return

        self.target_repo.update()

        if self.output == OutputType.brief:
            print("\n".join(str(package) for package in self.target_repo.get_packages()))
        elif self.output == OutputType.json:
            json_formatted_str = dump_json(list(self.target_repo.get_packages()))
            print(json_formatted_str)

    def _binary_archs(self) -> builtins.list[str]:
        """Get the repository architectures a binary package can be inserted into.

        A mirrored `Sources` index is read-only, so `source` never receives a `.deb`
        -> [VALIDATION.md](VALIDATION.md#sources-stanzas).

        Returns:
            List[str]: The repository architectures without the source one.
        """
        if self.target_repo is None:
            return []
        return [arch for arch in self.target_repo.get_archs() if arch != SOURCE_ARCH]

    def insert(self) -> None:
        """Insert packages into repository.

        Raises:
            ActionException: If both --changes-filename and --deb-filename are provided.
            ActionException: If neither --changes-filename nor --deb-filename are provided.
            ActionException: If the .deb filename does not end with '.deb'.
            ActionException: If the .changes architecture names the source one.
            ActionException: If the architecture is not found in the package.
            ActionException: If the package control field names the source architecture.
        """
        if (self.changes_filename is None) == (self.deb_filename is None):
            raise ActionException(
                "Please provide either --changes-filename or --deb-filename, not both"
            )

        if self.incoming_storage is None or self.target_repo is None:
            return

        filenames: list[str] = []
        archs: list[str] | None = None

        # load or setup target repository
        try:
            self.target_repo.update()
        except ReleaseReadException as exc:
            # no repository - create new one
            logging.debug("Target repository not found... (%s)", exc)

            self.target_repo.setup(fields=DEFAULT_REPO_FIELDS)
        prev_repo = copy(self.target_repo)

        # process deb files
        if self.changes_filename is not None:
            # TODO: split this into a separate method

            changes = Changes(self.incoming_storage)
            changes.load(self.changes_filename, digest_expected=self.changes_sha256)
            ch_arch = changes.get_arch()
            for arch in changes.get_archs():
                if not is_valid_arch(arch):
                    raise ActionException(
                        f"Invalid architecture in {self.changes_filename}: {arch!r}"
                    )

            # `Architecture` lists every arch the upload built, so a source plus binary
            # build names `source` beside the binary one; reading the first token alone
            # let the field order decide the upload
            if changes.names_source_arch():
                raise ActionException("Source packages are not supported yet")

            # logging.debug("Changes arch: %s", ch_arch)
            # logging.debug("fields: %s", self.target_repo.release.fields)

            archs = self._binary_archs() if ch_arch == ARCH_ANY else [ch_arch]

            # logging.debug("Archs: %s", archs)
            filenames = changes.get_deb_files()
        else:
            if self.deb_filename is None or not self.deb_filename.endswith(".deb"):
                raise ActionException(f"Invalid .deb filename: {self.deb_filename}")
            archs = self._binary_archs()
            filenames = [self.deb_filename]

        if (not isinstance(archs, list)) or (isinstance(archs, list) and len(archs) == 0):
            raise ActionException("Internal error - no architectures set. May be bug in the code.")

        with maybe_transaction(self.target_repo.storage, self.history_retention):
            for filename in filenames:
                debfile_obj = DebFile(self.incoming_storage, filename, comp=self.component)
                package = debfile_obj.get_package()

                pkg_arch = package.get_arch()
                if pkg_arch is not None and not is_valid_arch(pkg_arch):
                    raise ActionException(f"Invalid architecture in {filename}: {pkg_arch!r}")
                # the control field is read straight out of the uploaded .deb, so it can
                # name the source arch and overwrite the binary set `_binary_archs` picked;
                # the mirrored source index is read-only, so refuse before the transfer
                # as the changes path already does
                if pkg_arch == SOURCE_ARCH:
                    raise ActionException("Source packages are not supported yet")

                digests = self.target_repo.transfer_from(
                    self.incoming_storage, filename, path_dst=debfile_obj.filename_field
                )
                package.set_digests(digests)

                if pkg_arch is not None and pkg_arch != ARCH_ANY:
                    archs = [pkg_arch]

                if archs is None:
                    raise ActionException("Architecture not found in package")

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
        """Remove packages from repository.

        Raises:
            ValueError: If the pattern str is not a valid JSON.
        """
        if self.target_repo is None:
            return

        if self.pkg_pattern is None:
            raise ValueError("No package pattern provided.")

        self.target_repo.update()
        prev_repo = copy(self.target_repo)

        try:
            fields: dict[str, Any] = json.loads(self.pkg_pattern)
        except json.JSONDecodeError as json_error:
            raise ValueError(f"Invalid JSON format: {json_error}") from json_error

        with maybe_transaction(self.target_repo.storage, self.history_retention):
            if not self.target_repo.remove_pkg(fields):
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
        if self.target_repo is None:
            return

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
        if self.target_repo is None:
            return

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
            help="JSON format of regexp patterns of package fields. "
            'Example: {"Filename":"filename_1.deb|filename_2.deb"}',
            required=True,
        )
