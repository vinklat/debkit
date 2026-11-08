"""Mirror a Debian repository: index selection rules and the `RepoSync` operation.

The module-level rules decide which Release-listed indices a mirror carries;
`RepoSync` plans the copy and delete lists, transfers them, and checks a mirror.
"""

import fnmatch
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from txstore import (
    StorageException,
    StorageNull,
    compare_digests,
    is_unsafe_path,
    normalize_digests,
)

from debkit.repo import Repo
from debkit.repo.hashes import ChecksumException
from debkit.repo.params import ARCH_ANY, SOURCE_ARCH, RepoParams

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.package import Package
    from debkit.repo.release import IndexEntry
    from debkit.repo.repo_rw import RepoRW


# create logger
logging.getLogger(__name__).addHandler(logging.NullHandler())

TRANSFER_MAX_ERR_RETRIES = 3


@dataclass(frozen=True)
class IndexTarget:
    """One index in the mirror set.

    `digests` are what the source root Release records for the path, or `None` for
    the root Release files, which are not listed in their own digest block.

    `known_published` says the source is already known to publish the object: the
    Release load found it, or a parse fetched it. A Release lists paths the repository
    never publishes, so an entry that is only listed carries `known_published=False`
    and is probed just before it enters the copy list. It stays in the set either way,
    because the orphan pass must not delete a target object the source Release
    truthfully lists -> [VALIDATION.md](VALIDATION.md#index-selection).
    """

    digests: dict[str, str] | None
    known_published: bool


# Index directories no container parses. `Packages` opens only
# `<component>/binary-<arch>/Packages*` and `<component>/source/Sources*`, so an index
# under one of these is mirrored while the payloads it names never join the package
# set -> [VALIDATION.md](VALIDATION.md#index-selection).
UNPARSED_INDEX_DIRS = ("debian-installer",)

# Prefix of an installer image tree segment, `installer-<arch>/`, where `<arch>` is one
# the repository knows. The Release lists the checksum manifests it holds, not the
# images themselves, so mirroring them copies manifests for a tree the mirror does not
# carry.
UNPARSED_INDEX_DIR_PREFIX = "installer-"

# File names listing the same udeb payloads from outside `debian-installer/`:
# `Contents-udeb-<arch>` maps paths to packages the package set never gains.
UNPARSED_INDEX_NAME_PREFIXES = ("Contents-udeb",)

# Directory and file stem of the source indices, `source/Sources*`.
SOURCE_INDEX_NAMES = (RepoParams.arch_dir(SOURCE_ARCH), RepoParams.index_name(SOURCE_ARCH))


def _split_component(key: str, components: set[str] | None) -> tuple[str | None, str]:
    """Split a Release key into its component segment and the path below it.

    The component is a name the repository chose, so the structural rules read only
    the path below it: a component called `extra-armhf` or `installer-extras` says
    nothing about the architecture or the shape of the indices under it. One split
    feeds both the component membership test and those rules, so the two never
    disagree on where a component ends -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        key (str): Path as the Release lists it, relative to the layout prefix.
        components (Optional[Set[str]]): Selected components, or `None` for a flat
            repo whose paths carry no component segment.

    Returns:
        Tuple[Optional[str], str]: The component and the key below it, or `None` and
            the whole key for a flat repo and for a key with no `/`, which belongs to
            no component.
    """
    component, sep, rest = key.partition("/")
    if components is None or not sep:
        return None, key
    return component, rest


def _unselected_arch_token(key: str, archs: set[str], arch_universe: set[str]) -> str | None:
    """Find an architecture token in `key` the repository knows but has not selected.

    A token is a `-<arch>` run ending at a `.`, a `/`, or the end of the key. Several
    known architectures can end at the same position, as `amd64` does inside
    `kfreebsd-amd64`; the longest one owns the token, because that is the name the
    repository publishes the file under. Matching the shorter one would drop
    `binary-kfreebsd-amd64/Packages.xz` whenever `amd64` itself was unselected.
    Alternatives are ordered longest first, so the leftmost-first match `re` returns
    is also the longest at each token.

    Args:
        key (str): Release key below its component segment -> `_split_component`.
        archs (Set[str]): Selected architectures.
        arch_universe (Set[str]): Every architecture this repository knows about.

    Returns:
        Optional[str]: The unselected architecture owning a token, or `None` when every
            token in the key belongs to a selected architecture.
    """
    if not arch_universe:
        return None

    alternatives = "|".join(
        re.escape(arch) for arch in sorted(arch_universe, key=len, reverse=True)
    )
    wanted = archs | {ARCH_ANY}

    for match in re.finditer(rf"-({alternatives})(?=[./]|$)", key):
        if match.group(1) not in wanted:
            return match.group(1)

    return None


def _is_unparsed_index(key: str, arch_universe: set[str]) -> bool:
    """Report whether a Release key names an index nothing parses.

    A `debian-installer` index names `.udeb` payloads that never reach the package
    set, so mirroring it would copy the index, leave the payloads behind, and purge
    any the target already held as orphans: apt would read an index whose objects
    404. This is the rule `Sources` already follows, for the same reason.

    Two shapes carry the same content from outside that directory, and each needs its
    own test: `installer-<arch>/` image trees are a directory segment naming an
    architecture the repository knows, and the `Contents-udeb-<arch>` listing is a
    file name -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        key (str): Release key below its component segment -> `_split_component`.
        arch_universe (Set[str]): Every architecture this repository knows about.

    Returns:
        bool: True when the key names an index nothing parses.
    """
    parts = key.split("/")
    installer_dirs = {f"{UNPARSED_INDEX_DIR_PREFIX}{arch}" for arch in arch_universe}
    if any(part in UNPARSED_INDEX_DIRS or part in installer_dirs for part in parts[:-1]):
        return True
    return any(part.partition(".")[0].startswith(UNPARSED_INDEX_NAME_PREFIXES) for part in parts)


def _is_source_index(key: str) -> bool:
    """Report whether a Release key names a source index.

    In a componented repository they live under `<component>/source/`, in a flat one
    they are the `Sources*` files at the root, and `Contents-source` carries a
    `-source` token. This is the whole rule, so the architecture token rule never has
    to know the source architecture -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        key (str): Release key below its component segment -> `_split_component`.

    Returns:
        bool: True when the key belongs to the source architecture.
    """
    stems = [part.partition(".")[0] for part in key.split("/")]
    return any(stem in SOURCE_INDEX_NAMES or stem.endswith(f"-{SOURCE_ARCH}") for stem in stems)


def select_index_entry(
    key: str,
    *,
    components: set[str] | None,
    archs: set[str],
    arch_universe: set[str],
    excludes: list[str],
) -> bool:
    """Decide whether a Release-listed index belongs in the sync set.

    An entry is dropped when an exclude glob matches it, when it lives under an
    unselected component, when it is a source index and `source` is not selected,
    when nothing parses the index it names, or when its name carries an architecture
    token the repository knows but has not selected. The component segment is never
    read as an architecture or installer token; only the path below it is. An
    architecture token is matched only against `arch_universe`, so name parts that
    merely look like one (`icons-64x64`) are left alone; where several known
    architectures end at one token the longest one owns it. A key with no `/`
    belongs to no component, as a `Contents-<arch>` file at the dists root does, and
    passes the component rule.

    Args:
        key (str): Path as the Release lists it, relative to the layout prefix.
        components (Optional[Set[str]]): Selected components, or `None` for a flat
            repo where paths carry no component segment.
        archs (Set[str]): Selected architectures.
        arch_universe (Set[str]): Every architecture this repository knows about.
        excludes (List[str]): fnmatch globs matched against `key`.

    Returns:
        bool: True when the entry belongs in the sync set.
    """
    for pattern in excludes:
        if fnmatch.fnmatchcase(key, pattern):
            return False

    component, below = _split_component(key, components)
    # a component implies `components` is set; the second test narrows it for mypy
    if component is not None and components is not None and component not in components:
        return False

    if _is_source_index(below) and SOURCE_ARCH not in archs:
        logging.debug("%s is a source index and source is not selected. Skipped.", key)
        return False

    if _is_unparsed_index(below, arch_universe):
        logging.debug("%s is an index nothing parses. Skipped.", key)
        return False

    unselected = _unselected_arch_token(below, archs, arch_universe)
    if unselected is not None:
        logging.debug("%s carries unselected architecture %s. Skipped.", key, unselected)
        return False

    return True


class RepoSyncException(Exception):
    """Exception raised for errors in sync to the repository.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"{message}")


class RepoSync(Repo):
    """Class that extends Repo with sync functionality."""

    def __init__(
        self, storage: "Storage", params: "RepoParams", gpg: "GnuPG | None" = None
    ) -> None:
        """Initialize RepoSync.

        Args:
            storage (Storage): storage
            params (RepoParams): repository parameters
            gpg (Optional[GnuPG], optional): GnuPG object. Defaults to None.
        """
        super().__init__(storage, params, gpg=gpg)
        # payloads reported as Release-owned. `build_sync_lists` and `check` each build
        # the package set, so the report is per run, not per pass
        self._owned_payloads_warned: set[str] = set()

    def _get_indices(self) -> dict[str, IndexTarget]:
        """Get every repository index file to mirror, with its expected digests.

        The signed root Release is the manifest: it lists each index apt may fetch,
        so every listed path is a candidate. Entries are filtered by the selected
        components and architectures and by `RepoParams.indices_exclude`
        → [VALIDATION.md](VALIDATION.md#index-exclude-globs).

        The root `Release`, `Release.gpg`, and `InRelease` files are not listed in
        the digest block, carry no digests, and are never filtered. A listed key that
        would escape the storage root is dropped
        -> [VALIDATION.md](VALIDATION.md#filename-safety).

        Nothing is probed here. A Release lists paths the repository does not publish,
        and proving which is which costs one round trip per entry against a source
        that is usually HTTP; `_reduce_indices` pays that only for an entry it is
        about to copy or an entry the target lacks
        → [VALIDATION.md](VALIDATION.md#index-selection).

        Returns:
            Dict[str, IndexTarget]: Index path → the digests recorded for it and
                whether the source is already known to publish it.
        """
        ret: dict[str, IndexTarget] = {}

        components = None if self.params.is_flat else set(self.get_components())
        archs = set(self.get_archs())
        arch_universe = archs | set(self.release_archs())
        excludes = self.params.indices_exclude or []

        def selected(key: str) -> bool:
            return select_index_entry(
                key,
                components=components,
                archs=archs,
                arch_universe=arch_universe,
                excludes=excludes,
            )

        # root Release files: the manifest itself, never filtered and never listed
        # in their own digest block
        for path in self.release.get_indices_paths():
            ret[path] = IndexTarget(None, known_published=True)

        # indices already discovered while parsing, e.g. Packages files. Parsing
        # proved they exist, so they are not probed again, but the selection rules
        # still apply to them.
        for component in self.components.values():
            for path in component.get_indices_paths():
                if path in ret:
                    continue
                key = self.params.release_key(path)
                if not selected(key):
                    continue
                ret[path] = IndexTarget(self.release.digests.get(key), known_published=True)

        for entry in self.release.listed_indices():
            if entry.path in ret:
                continue

            if not selected(entry.key):
                continue

            if not self._index_path_is_safe(entry):
                continue

            # Listed is not published: the uncompressed Packages is listed by archives
            # that publish only the compressed one. The path stays in the set so the
            # orphan pass keeps its hands off a target object the source lists, and
            # `_reduce_indices` settles publication if it comes to a transfer.
            ret[entry.path] = IndexTarget(entry.digests, known_published=False)

        return ret

    def _payload_files(self) -> set[str]:
        """Collect the parsed payload paths, dropping any the Release owns.

        Returns:
            Set[str]: Paths of every parsed package the Release does not own.
        """
        return self._drop_release_owned(str(pkg) for pkg in self.get_packages())

    def _drop_release_owned(self, paths: Iterable[str]) -> set[str]:
        """Drop the stanza paths a Release digest block lists.

        A stanza `Filename` a Release digest block also lists is an index, not a
        payload: the index set alone decides whether it is copied or purged, so index
        selection and the payload copy and removal cannot disagree
        -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            paths (Iterable[str]): Stanza paths, as `str(Package)` gives them.

        Returns:
            Set[str]: The paths no Release digest block lists.
        """
        ret: set[str] = set()
        # one walk of the Release blocks, not one per package
        owned_keys = self._release_keys()

        for path in paths:
            if self.params.release_key(path) in owned_keys:
                if path not in self._owned_payloads_warned:
                    self._owned_payloads_warned.add(path)
                    logging.warning(
                        "%s is named by a Release digest block, not mirrored as a package", path
                    )
                continue
            ret.add(path)

        return ret

    def build_sync_lists(
        self, target_repo: "RepoRW", indices_only: bool = False
    ) -> tuple[list[str], list[str]]:
        """Build filelists for sync.

        Process some optimizations for reduce copy and remove lists.

        Args:
            target_repo (Repo): The target repo to sync with.
            indices_only (bool): Sync only indices. Defaults to False.

        Returns:
            tuple: A tuple containing the packages to copy and remove.
        """
        force_indices_copy = False
        copy_pkgs: list[Package] = []
        # remove_pkgs: List[Package] = []
        copy_pkg_files: set[str] = set()
        remove_pkg_files: set[str] = set()

        diff = self.diff(target_repo)

        # an index-only source change (i18n, dep11) leaves the package diff empty
        if self.release.hash != target_repo.release.hash:
            force_indices_copy = True

        if not diff.is_empty():
            force_indices_copy = True
            copy_pkgs = diff.get_pkgs_to_copy()
            # remove_pkgs = diff.get_pkgs_to_remove()
            copy_pkg_files = diff.get_filenames_to_copy()
            # a vanished stanza naming an index must not delete what the index set keeps
            remove_pkg_files = self._drop_release_owned(diff.get_filenames_to_remove())

        all_pkg_files: set[str] = self._payload_files()
        copy_pkg_files &= all_pkg_files

        # if target repo can't list files skip some optimizations
        can_list: bool = True
        try:
            existing_files = set(target_repo.storage.list())
        except NotImplementedError:
            existing_files = set()
            can_list = False

        indices = self._get_indices()

        repair_files: set[str] = set()
        purge_files: set[str] = set()

        # search for missing packages in target repo
        # caused by previous failed sync
        if can_list:
            for file in all_pkg_files:
                if (
                    (file not in copy_pkg_files)
                    and (file not in remove_pkg_files)
                    and (file not in existing_files)
                ):
                    repair_files.add(file)

        # search useless files (orphans) in target repo
        for file in existing_files:
            if (
                (file not in indices)
                and (file not in all_pkg_files)
                and (file not in remove_pkg_files)
            ):
                purge_files.add(file)

        # reduce copy_pkg_files by existing files
        # files done by previous terminated sync
        if not indices_only:
            seen: dict[str, dict[str, str]] = {}
            for package in copy_pkgs:
                pkg_name = str(package)
                if pkg_name in seen:
                    if package.get_digests() != seen[pkg_name]:
                        logging.warning(
                            "%s has inconsistent digests across indices in source repo",
                            package,
                        )
                    continue
                seen[pkg_name] = package.get_digests()
                if pkg_name in existing_files:
                    if not self.params.verify_digests or self._digest_unchanged(
                        pkg_name, package.get_digests(), target_repo
                    ):
                        copy_pkg_files.discard(pkg_name)
                    else:
                        logging.warning(
                            "%s have not valid checksums in target repo. It will be copied again.",
                            package,
                        )

        # reduce remove_pkg_files by not existing files
        # files not deleted by previous failed sync
        for file in remove_pkg_files.copy():
            if file not in existing_files:
                remove_pkg_files.remove(file)

        logging.info(" source repo: %d packages (total)", len(all_pkg_files))
        logging.info("     to copy: %d packages", len(copy_pkg_files))
        logging.info("   to remove: %d packages", len(remove_pkg_files))
        logging.info("     indices: %d objects", len(indices))
        logging.info(" target repo: %d objects already existing", len(existing_files))
        logging.info("   to repair: %d objects (corrupted or missing)", len(repair_files))
        logging.info("    to purge: %d objects (orphans)", len(purge_files))

        copy_list: list[str] = []
        delete_list: list[str] = []

        if not indices_only:
            copy_list = sorted(copy_pkg_files.union(repair_files))
            delete_list = sorted(remove_pkg_files.union(purge_files))
        else:
            force_indices_copy = True

        copy_list.extend(
            self._indices_to_copy(
                indices, target_repo, existing_files, forced=force_indices_copy, can_list=can_list
            )
        )

        return copy_list, delete_list

    def _indices_to_copy(
        self,
        indices: dict[str, IndexTarget],
        target_repo: "RepoRW",
        existing_files: set[str],
        *,
        forced: bool,
        can_list: bool,
    ) -> list[str]:
        """Decide which indices this sync copies -> [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            indices (Dict[str, IndexTarget]): The sync index set, from `_get_indices()`.
            target_repo (RepoRW): The target repo to sync with.
            existing_files (Set[str]): Paths the target listing returned.
            forced (bool): Whether a package or Release change already forces the index pass.
            can_list (bool): Whether `existing_files` is a real listing of the target.

        Returns:
            List[str]: Index paths to copy, empty when nothing forces or recovers one.
        """
        # check indices is consitent in target repo. A candidate the source may not
        # publish proves nothing about the target: it is expected to be absent there,
        # so its absence is not an inconsistency and must not force the index pass.
        if can_list and not forced:
            forced = any(
                target.known_published and file not in existing_files
                for file, target in indices.items()
            )

        if forced:
            return self._reduce_indices(indices, target_repo, existing_files)

        if not can_list:
            return []

        # nothing forced the pass, but a listed path the target lacks is either one
        # the source never publishes or one it 404ed once, and only a probe tells
        # them apart. Reducing that subset alone recovers the second without
        # re-emitting the root Release files, so a run that recovers nothing still
        # copies nothing and opens no transaction
        unproven = {
            file: target
            for file, target in indices.items()
            if not target.known_published and file not in existing_files
        }
        if not unproven:
            return []
        return self._reduce_indices(unproven, target_repo, existing_files)

    def _digest_unchanged(
        self,
        file: str,
        digests: dict[str, str],
        target_repo: "RepoRW",
    ) -> bool:
        """Report whether the target already holds `file` with the expected digests.

        The digest-skip decision shared by packages and indices; normalization and
        comparison stay in `hexdigest.py` -> [CRYPTO.md](CRYPTO.md#implementation-rules).
        The caller has already established that `file` exists in the target and that
        digest verification is on.

        Args:
            file (str): object path, identical in both repositories.
            digests (Dict[str, str]): digests the source records for the object.
            target_repo (RepoRW): The target repo to sync with.

        Returns:
            bool: True when the target copy matches and need not be transferred.
        """
        return compare_digests(digests, normalize_digests(target_repo.storage.get_metadata(file)))

    def _index_path_is_safe(self, entry: "IndexEntry") -> bool:
        """Report whether a Release-listed index path stays inside the repository.

        The digest block is remote metadata, so a hostile or malformed Release can
        name a key outside the storage root. A rejected entry is dropped from the
        index set rather than failing the transfer, so one bad record cannot stop a
        mirror -> [VALIDATION.md](VALIDATION.md#filename-safety).

        Args:
            entry (IndexEntry): candidate index from the root Release digest block.

        Returns:
            bool: True when the path is safe to mirror.
        """
        if is_unsafe_path(entry.key) or is_unsafe_path(entry.path):
            logging.warning("%s listed in Release is an unsafe index path. Skipped.", entry.key)
            return False
        return True

    def _target_release_agrees(
        self,
        file: str,
        digests: dict[str, str],
        target_repo: "RepoRW",
    ) -> bool:
        """Report whether both root Releases record the same digests for an index.

        The target `Release` is already loaded and its hash compared in
        `build_sync_lists`, so its digest block costs nothing, while
        `Storage.get_metadata` is a round trip per key and a full Debian Release
        lists hundreds of indices. Which entries agreement may settle is decided by
        `_reduce_indices`.

        Args:
            file (str): object path, identical in both repositories.
            digests (Dict[str, str]): digests the source Release records for the index.
            target_repo (RepoRW): The target repo to sync with.

        Returns:
            bool: True when the target Release records matching digests for the key.
        """
        target_digests = target_repo.release.digests.get(target_repo.params.release_key(file))
        if not target_digests:
            return False
        return compare_digests(digests, normalize_digests(target_digests))

    def _reduce_indices(
        self,
        indices: dict[str, IndexTarget],
        target_repo: "RepoRW",
        existing_files: set[str],
    ) -> list[str]:
        """Drop indices the target already holds unchanged.

        An entry the target holds is settled by the cheapest check that proves it
        current, in order: agreeing root Release digest blocks (free), then the stored
        object metadata (one round trip). The root `Release` files carry no digests of
        their own and are always copied.

        A held entry that neither settles is transferred without a source probe: it
        was mirrored from this source once, so a changed digest is a republish, not a
        delisting, and `Storage.exists` is one round trip per key while a point
        release changes hundreds of listed indices at a time.

        An entry the target lacks is never settled, whatever the blocks say: a source
        404 is indistinguishable from a key the source never publishes, and the last
        sync copies the Release whether or not it copied the object. It is probed
        unless the source is already known to publish it, because a sync either
        transfers every object or raises → [STORAGE.md](STORAGE.md#transactions),
        so a path the source lists but does not publish is dropped here.

        A target whose Release did not load, empty or reset after a checksum failure,
        settles nothing: its stored metadata was written with the bytes a failed check
        just distrusted, so every held entry is copied again
        → [VALIDATION.md](VALIDATION.md#index-selection).

        Args:
            indices (Dict[str, IndexTarget]): Index path → expected digests and
                whether the source is known to publish it.
            target_repo (RepoRW): The target repo to sync with.
            existing_files (Set[str]): Objects already present in the target.

        Returns:
            List[str]: Index paths still to copy.
        """
        target_loaded = bool(target_repo.release.hash)
        ret: list[str] = []
        for file, target in indices.items():
            held = file in existing_files

            if held and target.digests and self.params.verify_digests and target_loaded:
                if self._target_release_agrees(file, target.digests, target_repo):
                    continue
                if self._digest_unchanged(file, target.digests, target_repo):
                    continue
                logging.debug(
                    "%s has different checksums in target repo. It will be copied again.", file
                )

            if not held and not target.known_published and not self.storage.exists(file):
                logging.debug("%s listed in Release but missing in source.", file)
                continue

            ret.append(file)

        logging.info("     indices: %d objects to copy", len(ret))
        return ret

    def _transfer_file(
        self, file: str, target_repo: "Repo", progress: tuple[int, int] | None = None
    ) -> bool:
        """Transfer a file from this repo to another repo.

        Save digests of the file in metadata.

        Args:
            file (str): The file to transfer.
            target_repo (Repo): The target repo to transfer to.
            progress (Optional[Tuple[int, int]], optional): Progress of transfer - current of
                total objects.
                Defaults to None.

        Raises:
            ChecksumException: If the checksum verification fails.

        Returns:
            bool: True if the file was transferred successfully, False otherwise.
        """
        if progress is None:
            logging.info("Copy: %s", file)
        else:
            logging.info("Copy (%d/%d): %s", progress[0], progress[1], file)

        try:
            digests = self.storage.transfer(
                file,
                target_repo.storage,
                path_dst=file,
            )
        except StorageException as exc:
            logging.error("Error transfer file %s: %s", file, exc)
            return False

        if self.params.verify_digests:
            if not self.verify_checksum(file, digests):
                target_repo.storage.delete(file)
                raise ChecksumException(f"Content verify mismatch in {file}")

        target_repo.storage.add_metadata(file, digests)

        return True

    def _delete_file(self, file: str, target_repo: "Repo") -> None:
        """Delete a file from this repo.

        Args:
            file (str): The file to delete.
            target_repo (Repo): The target repo to delete from.
        """
        logging.info("Delete: %s", file)
        target_repo.storage.delete(file)

    def _verify_file(
        self, file: str, progress: tuple[int, int] | None = None, fix: bool | None = False
    ) -> bool:
        """Transfer a file and verify checksums.

        Args:
            file (str): The file to transfer.
            progress (Optional[Tuple[int, int]], optional): Progress of transfer - current of
                total objects.
                Defaults to None.
            fix (Optional[bool], optional): Fix inconsistencies if True. Defaults to False.

        Returns:
            bool: True if the file is valid, False otherwise.
        """
        dev_null = StorageNull()

        if progress is None:
            logging.info("Verify: %s", file)
        else:
            logging.info("Verify (%d/%d): %s", progress[0], progress[1], file)

        digests = self.storage.transfer(file, dev_null)

        if self.params.verify_digests:
            if not self.verify_checksum(file, digests):
                return False

        if fix:
            self.storage.add_metadata(file, digests)

        return True

    def sync(
        self, target_repo: "Repo", copy_list: list[str], delete_list: list[str], depth: int = 0
    ) -> None:
        """Sync this repo with another repo.

        Either every object transfers or the sync raises: a target that holds a new
        root `Release` describing objects it never received would advertise content
        it cannot serve. The caller wraps the run in a transaction, so raising leaves
        the target at its previous consistent state
        -> [STORAGE.md](STORAGE.md#transactions).

        Args:
            target_repo (Repo): The target repo to sync with.
            copy_list (List[str]): The list of files to copy.
            delete_list (List[str]): The list of files to delete.
            depth (int): The depth of the sync. Defaults to 0.

        Raises:
            RepoSyncException: If any object still fails after the retries.
            ChecksumException: If a transferred object does not match its expected
                digests; it is never retried → [CRYPTO.md](CRYPTO.md#verification-failures).
        """
        # break recursion if max retries reached
        if depth >= TRANSFER_MAX_ERR_RETRIES:
            logging.debug("failed packages: %s", copy_list)
            raise RepoSyncException("Max retries reached.")

        failed_list: list[str] = []
        total: int = len(copy_list)
        cnt: int = 0
        for name in copy_list:
            cnt += 1
            if not self._transfer_file(name, target_repo, progress=(cnt, total)):
                failed_list.append(name)

        # retry whatever failed; a transient error should not fail the whole run
        if len(failed_list) > 0:
            return self.sync(target_repo, failed_list, delete_list, depth=depth + 1)

        # delete objects
        for name in delete_list:
            self._delete_file(name, target_repo)

    def check(
        self, missing_list: list[str], orphan_list: list[str], fix: bool = False
    ) -> tuple[int, int]:
        """Check repository for consistency.

        Args:
            missing_list (List[str]): List of missing packages.
            orphan_list (List[str]): List of orphan files to delete.
            fix (bool): Delete inconsistencies if True

        Returns:
            Tuple[int, int]: A tuple containing the number of errors and the number of errors fixed.
        """
        all_pkg_files = sorted(self._payload_files())

        total: int = len(all_pkg_files)
        cnt: int = 0
        ret_err_total: int = 0
        ret_err_fixed_total: int = 0

        for file in all_pkg_files:
            cnt += 1

            if file in missing_list:
                logging.warning("Skip (%d/%d): %s (missing)", cnt, total, file)
                continue

            try:
                if not self._verify_file(file, progress=(cnt, total), fix=fix):
                    logging.warning("Checksum verify failed: %s", file)
                    ret_err_total += 1
                    if fix:
                        self._delete_file(file, self)
                        ret_err_fixed_total += 1
            except StorageException as exc:
                logging.error("Error checking file: %s: %s", file, exc)
                ret_err_total += 1

        if fix:
            for file in orphan_list:
                try:
                    self._delete_file(file, self)
                    ret_err_fixed_total += 1
                except StorageException as exc:
                    logging.error("Error deleting file: %s: %s", file, exc)

        return ret_err_total, ret_err_fixed_total
