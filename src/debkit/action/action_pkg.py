"""Debian repo operations commands."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from humanize import naturalsize
from txstore import compare_digests, maybe_transaction

from debkit._escape import escape_control
from debkit.action import (
    Action,
    ActionException,
    OutputType,
    attach_storage_plain,
    dump_json,
    open_target_repo,
)
from debkit.argtypes import component_type, pkg_pattern_type
from debkit.repo.changes import Changes
from debkit.repo.component import IndexLoad
from debkit.repo.debfile import DebFile
from debkit.repo.hashes import ChecksumException
from debkit.repo.params import ARCH_ANY, SOURCE_ARCH, is_valid_arch
from debkit.repo.release import ReleaseNotFoundException

if TYPE_CHECKING:
    from argparse import Namespace

    from txstore import Storage

    from debkit._options import EnvArgumentParser
    from debkit.repo import RepoSnapshot
    from debkit.repo.package import Package
    from debkit.repo.repo_diff import RepoDiff


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# the component an insert targets without `--component` → [CONFIG.md](CONFIG.md#insert)
DEFAULT_COMPONENT = "main"
DEFAULT_REPO_FIELDS = {"Architectures": "amd64 arm64 all", "Components": DEFAULT_COMPONENT}


class ActionPkg(Action):
    """Actions with debian packages in repository."""

    def __init__(self, pars: Namespace, create_path: bool = False):
        """Initialize ActionPkg.

        Args:
            pars: Command line arguments from argparser.
            create_path: Create path or bucket if True.

        Raises:
            ActionException: An `insert` names neither or both of `--changes-filename` and
                `--deb-filename`, or a `.deb` file name without the suffix; refused before
                any storage is opened, so no target path or bucket is created.
        """
        # one action serves five subcommands, each registering only its own flags
        self.pkg_pattern: dict[str, re.Pattern[str]] | None = getattr(pars, "pkg_pattern", None)
        self.changes_filename: str | None = getattr(pars, "changes_filename", None)
        self.changes_sha256: str | None = getattr(pars, "changes_sha256", None)
        self.deb_filename: str | None = getattr(pars, "deb_filename", None)
        self.component: str = getattr(pars, "component", DEFAULT_COMPONENT)
        is_insert = pars.command == "insert"
        if is_insert:
            self._check_insert_source()

        super().__init__(pars)
        self.target_repo = open_target_repo(pars, gpg=self.gpg, create_path=create_path)
        self.incoming_storage: Storage | None = (
            attach_storage_plain(pars, pars.incoming_url) if is_insert else None
        )

    def _check_insert_source(self) -> None:
        """Check the `insert` upload flags -> [CONFIG.md](CONFIG.md#insert).

        Raises:
            ActionException: Neither or both of `--changes-filename` and `--deb-filename`.
            ActionException: A `--deb-filename` that does not end with `.deb`.
        """
        if (self.changes_filename is None) == (self.deb_filename is None):
            raise ActionException(
                "insert needs exactly one of --changes-filename or --deb-filename"
            )
        if self.deb_filename is not None and not self.deb_filename.endswith(".deb"):
            raise ActionException(f"Invalid .deb filename: {self.deb_filename}")

    def dump(self) -> None:
        """Dump repository to stdout."""
        self.target_repo.update(IndexLoad.READ_ONLY)
        logger.debug("%s", str(self._target_metrics))
        if self.output == OutputType.brief:
            self._print_brief_dump()
        else:
            print(dump_json(self.target_repo.dump()))

    def _print_brief_dump(self) -> None:
        """Print the target's Release fields and each arch's Release and package count."""
        # Release fields are remote text, so a control character is escaped as a log line
        # escapes it -> [CONFIG.md](CONFIG.md#output-flag)
        print("Release:")
        for key, value in self.target_repo.release.fields.items():
            print(escape_control(f"  {key}: {value}"))

        print("Components:")
        for component_name, component in self.target_repo.components.items():
            print(f"  {component_name}:")
            for arch_name, packages in component.packages.items():
                # a per-arch Release is optional, so an empty one is reported as absent
                if packages.release.is_empty():
                    release = "<No release data>"
                else:
                    release = escape_control(
                        ", ".join(f"{k}: {v}" for k, v in packages.release.fields.items())
                    )
                print(f"    {arch_name}: {release}")
                print(f"      {len(component.slot_packages(arch_name))} packages")

    def stats(self) -> None:
        """Print repository statistics to stdout."""
        self.target_repo.update(IndexLoad.READ_ONLY)
        stats = self.target_repo.stats()

        if self.output == OutputType.brief:
            self._print_brief_stats(stats)
        else:
            self._print_json_stats(stats)

    def _print_brief_stats(self, stats: dict[str, dict[str, dict[str, int]]]) -> None:
        """Print brief naturalized statistics to stdout.

        Args:
            stats: A dictionary containing the stats data.
        """
        print("Packages:")
        for component, arch in stats.items():
            print(f"  {component}:")
            for arch_name, arch_data in arch.items():
                print(
                    f"    {arch_name}: {naturalsize(arch_data['packages_bytes'])}, "
                    f"{arch_data['packages_total']} packages"
                )
        print("Indices read:")
        print(
            f"  {naturalsize(self._target_metrics.read_bytes)}, "
            f"{self._target_metrics.read_objects_total} files"
        )

    def _print_json_stats(self, stats: dict[str, dict[str, dict[str, int]]]) -> None:
        """Print JSON statistics to stdout.

        Args:
            stats: A dictionary containing the stats data.
        """
        ret = {
            "packages": stats,
            "indices": {
                "files_total": self._target_metrics.read_objects_total,
                "files_bytes": self._target_metrics.read_bytes,
            },
        }

        json_formatted_str = dump_json(ret)
        print(json_formatted_str)

    def list_packages(self) -> None:
        """List packages in repository to stdout."""
        self.target_repo.update(IndexLoad.READ_ONLY)

        if self.output == OutputType.brief:
            print("\n".join(str(package) for package in self.target_repo.get_packages()))
        else:
            json_formatted_str = dump_json(list(self.target_repo.get_packages()))
            print(json_formatted_str)

    def _load_target_or_setup(self) -> RepoSnapshot:
        """Load the target, or set up a new one where it has no Release.

        Called inside the command's transaction: a peer may commit before `history_lock`
        is granted, so a target read before it would save over the peer's indices
        -> [STORAGE.md](STORAGE.md#transactions).

        Returns:
            A snapshot of the target as loaded, for the command's diff.
        """
        try:
            self.target_repo.update()
        except ReleaseNotFoundException as exc:
            # no repository - create new one; any other read failure must not reset an
            # existing one
            logger.debug("Target repository not found... (%s)", exc)

            self.target_repo.setup(fields=DEFAULT_REPO_FIELDS)
        return self.target_repo.snapshot()

    def _load_changes(self, incoming_storage: Storage, changes_filename: str) -> Changes:
        """Load and check a `.changes` upload, whose digests each listed `.deb` must match.

        Args:
            incoming_storage: The storage holding the upload.
            changes_filename: The `.changes` file in it.

        Raises:
            ActionException: An architecture token is not an architecture name.
            ActionException: An architecture token names the source one.

        Returns:
            The upload; `get_deb_files()` lists its `.deb` files.
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

        return changes

    def insert(self) -> None:
        """Insert packages into repository.

        The upload flags were checked when the action was built
        -> `_check_insert_source`.

        Raises:
            ActionException: If the .changes architecture names the source one.
            ActionException: If an architecture token is not an architecture name.
            ActionException: If the package control field names the source architecture.
            ActionException: If an `all` or arch-less package finds no binary architecture
                in the target.
            ChecksumException: If a transferred `.deb` does not match its `.changes` digest.
        """
        # `--incoming-url` is required for `insert`, so argparse already refused a missing one
        assert self.incoming_storage is not None, "insert without --incoming-url"

        # process deb files
        changes: Changes | None = None
        if self.changes_filename is not None:
            changes = self._load_changes(self.incoming_storage, self.changes_filename)
            filenames = changes.get_deb_files()
        else:
            # exactly one of the two filenames is set -> `_check_insert_source`
            assert self.deb_filename is not None
            filenames = [self.deb_filename]

        with maybe_transaction(self.target_repo.storage, self.history_retention):
            target_before = self._load_target_or_setup()
            binary_archs = self.target_repo.binary_archs()
            for filename in filenames:
                debfile_obj = DebFile(self.incoming_storage, filename, comp=self.component)
                package = debfile_obj.get_package()
                archs = self._target_archs(filename, package, binary_archs)

                pool_key = package.get_filename()
                held = self.target_repo.held_digests(pool_key)
                digests = self.target_repo.transfer_from(
                    self.incoming_storage, filename, path_dst=pool_key
                )
                # the pinned `.changes` lists each payload's digest, so a payload swapped in
                # the incoming storage is refused before any other check can misname it;
                # raising discards the staged transfer
                # -> [CRYPTO.md](CRYPTO.md#verification-failures)
                if (
                    changes is not None
                    and self.target_repo.params.verify_digests
                    and not changes.verify_digests(filename, digests)
                ):
                    raise ChecksumException(
                        f"{filename} does not match its digest in {self.changes_filename}"
                    )
                # other dists and arch indices may list this pool key with the digests of the
                # bytes it holds, so replacing those bytes would break their records
                # -> [VALIDATION.md](VALIDATION.md#filename-safety)
                if held is not None and not compare_digests(held, digests):
                    raise ActionException(
                        f"{pool_key} already holds other bytes than {filename}; "
                        "upload them under another file name or version"
                    )
                package.set_digests(digests)

                for arch in archs:
                    self.target_repo.insert_pkg(self.component, arch, package)

            ret_diff = self._save_if_changed(target_before)

        self._print_pkg(ret_diff)

    @staticmethod
    def _target_archs(filename: str, package: Package, binary_archs: list[str]) -> list[str]:
        """Get the repository architectures one uploaded package is inserted into.

        Each package goes by its own control field, so neither the upload's field order
        nor its file order picks the arches -> [VALIDATION.md](VALIDATION.md#architectures).

        Args:
            filename: The `.deb` file, for messages.
            package: Its control data.
            binary_archs: The target's binary architectures, for `all` or an arch-less package.

        Raises:
            ActionException: If the control `Architecture` is not an architecture name.
            ActionException: If it names the source architecture.
            ActionException: If an `all` or arch-less package finds no binary architecture.

        Returns:
            The architectures to insert into.
        """
        pkg_arch = package.get_arch()
        if pkg_arch is not None and not is_valid_arch(pkg_arch):
            raise ActionException(f"Invalid architecture in {filename}: {pkg_arch!r}")
        # the control field is read straight out of the uploaded .deb, so it can name the
        # source arch; the mirrored source index is read-only, so refuse before the
        # transfer as the changes path already does
        if pkg_arch == SOURCE_ARCH:
            raise ActionException("Source packages are not supported yet")

        archs = [pkg_arch] if pkg_arch is not None and pkg_arch != ARCH_ANY else binary_archs
        if not archs:
            raise ActionException(
                f"Cannot insert {filename} (Architecture: {pkg_arch or 'none'}): "
                "the target Release lists no binary architecture"
            )
        return archs

    def _save_if_changed(self, target_before: RepoSnapshot) -> RepoDiff:
        """Diff the target against its state before the command and save it if it changed.

        Called inside the command's transaction, so the save is staged with the payloads.

        Args:
            target_before: The target as loaded before the command, as `snapshot` took it.

        Returns:
            The diff, for the command's output.
        """
        ret_diff = self.target_repo.diff(target_before)
        if not ret_diff.is_empty():
            self.target_repo.save()
        return ret_diff

    def _print_pkg(self, diff: RepoDiff) -> None:
        """Print an insert or remove diff in the selected output format.

        Args:
            diff: Repository diff object.
        """
        if self.output == OutputType.brief:
            self._print_pkg_brief(diff)
        else:
            self._print_pkg_json(diff)

    def remove(self) -> None:
        """Remove the packages matching the parsed ``--pkg-pattern`` from the repository.

        Raises:
            ActionException: If no package matched.
        """
        # `--pkg-pattern` is required for `remove`, so argparse already refused a missing one
        assert self.pkg_pattern is not None, "remove without --pkg-pattern"

        with maybe_transaction(self.target_repo.storage, self.history_retention):
            # a peer may commit before `history_lock` is granted, so the target is read
            # under it -> [STORAGE.md](STORAGE.md#transactions)
            self.target_repo.update()
            target_before = self.target_repo.snapshot()
            if not self.target_repo.remove_pkg(self.pkg_pattern):
                raise ActionException("No removable packages matched the pattern.")
            ret_diff = self._save_if_changed(target_before)

        self._print_pkg(ret_diff)

    def _print_pkg_brief(self, diff: RepoDiff) -> None:
        """Print package operation brief output.

        Args:
            diff: Repository diff object.
        """
        if diff.count_pkgs_to_copy() > 0:
            print(f"Inserted {diff.count_pkgs_to_copy()} packages.")
        if diff.count_pkgs_to_remove() > 0:
            print(f"Removed {diff.count_pkgs_to_remove()} packages.")
        if diff.count_pkgs_to_copy() > 0:
            print(
                f"{self._target_metrics.write_objects_total} objects "
                f"({naturalsize(self._target_metrics.write_bytes)}) transferred."
            )

    def _print_pkg_json(self, diff: RepoDiff) -> None:
        """Print package operation json output.

        Args:
            diff: Repository diff object.
        """
        json_dict: dict[str, Any] = {
            "packages": diff.dump(),
            "transfer": {"metrics": {}, "errors": {}},
        }
        json_dict["transfer"]["metrics"]["target_storage"] = self._target_metrics.dump()

        print(dump_json(json_dict))

    @staticmethod
    def add_insert_cli_args(parser: EnvArgumentParser) -> None:
        """Add arguments for insert.

        Args:
            parser: The argument parser to which the arguments should be added.
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
            default=DEFAULT_COMPONENT,
            type=component_type,
        )

    @staticmethod
    def add_remove_cli_args(parser: EnvArgumentParser) -> None:
        """Add arguments for delete.

        Args:
            parser: The argument parser to which the arguments should be added.
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
