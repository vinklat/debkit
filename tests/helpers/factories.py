"""Factory helpers for debkit tests.

These do NOT build a real Debian repository on disk. They construct
in-memory domain objects (Package, Packages, Component, RepoParams,
RepoSync, RepoRW) with the minimum state needed by the unit under test.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import MagicMock

from txstore import Storage, StorageType

from debkit.repo.component import Component
from debkit.repo.package import Package
from debkit.repo.packages import Packages
from debkit.repo.params import RepoParams
from debkit.repo.release import IndexEntry, Release
from debkit.repo.repo_rw import RepoRW
from debkit.repo.repo_sync import RepoSync
from helpers.storage import storage_namespace


def make_action_namespace(repo_dir: Path, **flags: object) -> Namespace:
    """Namespace `Action.__init__` accepts for an `fs://` target without GPG.

    *flags* extend or override the common fields (``output``, ``dry_run``,
    ``history_keep``, ...).
    """
    return storage_namespace(target_url=f"fs://{repo_dir} bookworm main", **flags)


def make_params(
    *,
    flat: bool = False,
    dist: str = "bookworm",
    verify_gpg: bool = False,
    verify_digests: bool = False,
) -> RepoParams:
    """Build a minimal RepoParams for unit tests.

    Args:
        flat: If True, sets ``is_flat=True`` and clears ``indices_prefix``.
        dist: Distribution name.
        verify_gpg: Value for ``verify_gpg`` flag.
        verify_digests: Value for ``verify_digests`` flag.

    Returns:
        Configured ``RepoParams`` instance.
    """
    p = RepoParams(
        dist=dist, components=["main"], verify_gpg=verify_gpg, verify_digests=verify_digests
    )
    p.is_flat = flat
    if flat:
        p.indices_prefix = ""
    return p


def make_packages_block(name: str = "foo", filename: str | None = None, size: int = 100) -> str:
    """Build a single Debian Packages-file stanza string.

    Args:
        name: ``Package`` field value; also used to derive the default filename.
        filename: Override the ``Filename`` field.  Defaults to a pool path derived
            from ``name``.
        size: ``Size`` field value.

    Returns:
        A headers-format stanza string (without a trailing blank line separator).
    """
    fn = filename or f"pool/main/{name[0]}/{name}/{name}_1.0_amd64.deb"
    return f"Package: {name}\nFilename: {fn}\nSize: {size}\nSHA256: deadbeef\n"


def make_package(filename: str, size: int | str = 1, **fields: str) -> Package:
    """Build a Package with the mandatory fields plus optional digests / extras.

    The Filename field drives `Package.__repr__` and is what
    `RepoDiff.get_filenames_to_copy()` consumes.

    Every Debian header field is a string, so `size` accepts one too: callers
    spell it as an int for readability, and a `**fields` mapping of header
    values can carry `Size` without conflicting with this parameter.
    """
    base_name = filename.rsplit("/", 1)[-1].split("_", 1)[0]
    pkg = Package()
    pkg.fields = {
        "Package": fields.pop("Package", base_name),
        "Filename": filename,
        "Size": str(size),
        **fields,
    }
    return pkg


def make_packages(storage: Storage, params: RepoParams, pkgs: Iterable[Package]) -> Packages:
    """Build a Packages container pre-populated by Filename."""
    container = Packages(storage, params)
    container.packages = {pkg.fields["Filename"]: pkg for pkg in pkgs}
    container.size = len(container.packages)
    return container


def make_storage_mock(
    *,
    list_result: list[str] | None = None,
    list_raises: type[BaseException] | None = None,
    storage_type: StorageType = StorageType.fs,
    transfer_digests: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock spec'd to Storage with sensible defaults.

    Args:
        list_result: Value returned by `.list()` when not raising.
        list_raises: Exception class raised from `.list()` (e.g.
            `NotImplementedError` to emulate `StorageHttp`). Mutually
            exclusive with `list_result`.
        storage_type: Returned by `.get_type()`. Must be a real
            `StorageType` member because production code uses `is` comparison.
        transfer_digests: Returned by `.transfer()`.
    """
    mock = MagicMock(spec=Storage)
    if list_raises is not None:
        mock.list.side_effect = list_raises
    else:
        mock.list.return_value = list_result if list_result is not None else []
    mock.exists.return_value = True
    mock.get_metadata.return_value = {}
    mock.get_type.return_value = storage_type
    mock.transfer.return_value = transfer_digests or {
        "SHA256": "deadbeef",
        "Size": "1",
    }
    return mock


def _attach_state(
    repo: RepoSync | RepoRW,
    *,
    components: dict[str, dict[str, Packages]] | None,
    release_hash: str,
    indices_paths: list[str] | None,
    listed_indices: list[IndexEntry] | None = None,
    release_fields: dict[str, str] | None = None,
    component_indices: dict[str, list[str]] | None = None,
) -> None:
    """Inject release / components onto a fresh Repo-derived object.

    Args:
        component_indices: Per-component index paths reported by
            `Component.get_indices_paths()`; a component absent from the mapping
            reports none, as a repo whose parse discovered no index would.
    """
    # `spec=Release` so the ownership gate's isinstance check sees a Release
    repo.release = MagicMock(spec=Release, name="Release")
    repo.release.hash = release_hash
    paths = indices_paths if indices_paths is not None else ["dists/bookworm/Release"]
    repo.release.get_indices_paths.side_effect = lambda: list(paths)
    repo.release.verify_digests.return_value = False
    listed = list(listed_indices or [])
    repo.release.listed_indices.side_effect = lambda: list(listed)
    repo.release.digests = {entry.key: entry.digests for entry in listed}
    repo.release.fields = dict(release_fields or {})
    if components:
        comp_indices = component_indices or {}
        for cname, archs in components.items():
            comp = MagicMock(spec=Component)
            comp.packages = archs
            comp.get_indices_paths.return_value = list(comp_indices.get(cname, []))
            repo.components[cname] = comp


def make_repo_sync(
    *,
    storage: Storage,
    params: RepoParams | None = None,
    components: dict[str, dict[str, Packages]] | None = None,
    release_hash: str = "src-hash",
    indices_paths: list[str] | None = None,
    listed_indices: list[IndexEntry] | None = None,
    release_fields: dict[str, str] | None = None,
    component_indices: dict[str, list[str]] | None = None,
) -> RepoSync:
    """Build a RepoSync with pre-populated state, bypassing `update()`."""
    p = params or RepoParams(
        dist="bookworm",
        components=["main"],
        verify_gpg=False,
        verify_digests=True,
    )
    repo = RepoSync(storage, p)
    _attach_state(
        repo,
        components=components,
        release_hash=release_hash,
        indices_paths=indices_paths,
        listed_indices=listed_indices,
        release_fields=release_fields,
        component_indices=component_indices,
    )
    return repo


def make_repo_rw(
    *,
    storage: Storage,
    params: RepoParams | None = None,
    components: dict[str, dict[str, Packages]] | None = None,
    release_hash: str = "tgt-hash",
    indices_paths: list[str] | None = None,
    listed_indices: list[IndexEntry] | None = None,
    release_fields: dict[str, str] | None = None,
    component_indices: dict[str, list[str]] | None = None,
    modified: bool = False,
) -> RepoRW:
    """Build a RepoRW (target side of sync) with pre-populated state."""
    p = params or RepoParams(
        dist="bookworm",
        components=["main"],
        verify_gpg=False,
        verify_digests=True,
    )
    repo = RepoRW(storage, p)
    _attach_state(
        repo,
        components=components,
        release_hash=release_hash,
        indices_paths=indices_paths,
        listed_indices=listed_indices,
        release_fields=release_fields,
        component_indices=component_indices,
    )
    repo.modified = modified
    return repo
