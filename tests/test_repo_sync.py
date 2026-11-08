"""Coverage for debkit.repo.repo_sync.RepoSync.

See TESTING.md section 10 (RepoSync). Tests are isolated: Storage is mocked
via `MagicMock(spec=Storage)` and Release/Component state is injected by the
factory helpers in tests/helpers/factories.py instead of being parsed from a
real Debian tree on disk.
"""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock

import pytest
from helpers.factories import (
    make_package,
    make_packages,
    make_repo_rw,
    make_repo_sync,
    make_storage_mock,
)
from txstore import StorageException, StorageType

from debkit.repo.hashes import ChecksumException
from debkit.repo.package import Package
from debkit.repo.packages import Packages
from debkit.repo.params import RepoParams
from debkit.repo.release import IndexEntry
from debkit.repo.repo_rw import RepoRW
from debkit.repo.repo_sync import (
    RepoSync,
    RepoSyncException,
    select_index_entry,
)

INDICES = ["dists/bookworm/Release", "dists/bookworm/main/binary-amd64/Packages"]
FLAT_INDICES = ["bookworm/Release"]


def _make_flat(params: RepoParams, rootless: bool = False) -> None:
    """Switch params to the flat layout a two-field deb source produces."""
    params.is_flat = True
    params.indices_prefix = ""
    if rootless:
        params.dist = "/"


# ---------- helpers ----------


def _mock_storage(repo: RepoSync | RepoRW) -> MagicMock:
    """Return the underlying ``MagicMock`` for a repo's storage attribute."""
    return cast(MagicMock, repo.storage)


def _two_repos(
    *,
    src_components: dict[str, dict[str, list[Package]]],
    tgt_components: dict[str, dict[str, list[Package]]],
    tgt_list: list[str] | None = None,
    tgt_list_raises: type[BaseException] | None = None,
    tgt_metadata: dict[str, str] | None = None,
    same_hash: bool = False,
    verify_digests: bool = True,
    listed: list[IndexEntry] | None = None,
    tgt_listed: list[IndexEntry] | None = None,
    src_exists: set[str] | None = None,
    src_archs: list[str] | None = None,
    release_fields: dict[str, str] | None = None,
    indices_exclude: list[str] | None = None,
    flat: bool = False,
    rootless: bool = False,
    src_component_indices: dict[str, list[str]] | None = None,
) -> tuple[RepoSync, RepoRW]:
    """Build a source RepoSync + target RepoRW.

    Args:
        src_components: Mapping of component -> arch -> package list for the source.
        tgt_components: Mapping of component -> arch -> package list for the target.
        tgt_list: Files returned by ``target.storage.list()``.
        tgt_list_raises: Exception raised by ``target.storage.list()`` instead.
        tgt_metadata: Value returned by ``target.storage.get_metadata()``.
        same_hash: If True both repos share the same release hash (no diff trigger).
        verify_digests: Passed to ``RepoParams``.
        listed: Index entries the source root Release lists in its digest block.
        tgt_listed: Index entries the target root Release lists in its digest block.
        src_exists: Paths that exist in the source storage (default: all).
        src_archs: Architectures selected on the source params.
        release_fields: Fields of the source root Release (e.g. ``Architectures``).
        indices_exclude: Exclude globs set on both repo params.
        flat: If True both repos use a flat layout (``<dist>/`` prefix, no components).
        rootless: With ``flat``, the dist is ``/`` so index paths carry no prefix at all.
        src_component_indices: Per-component index paths the source parse discovered.
    """
    component_names = sorted(set(src_components) | set(tgt_components))
    src_params = RepoParams(
        dist="bookworm",
        components=component_names,
        archs=src_archs,
        indices_exclude=indices_exclude,
        verify_gpg=False,
        verify_digests=verify_digests,
    )
    src_storage = make_storage_mock(exists_paths=src_exists)
    src_comp_map: dict[str, dict[str, Packages]] = {
        comp: {arch: make_packages(src_storage, src_params, pkgs) for arch, pkgs in archs.items()}
        for comp, archs in src_components.items()
    }
    if flat:
        _make_flat(src_params, rootless)
        indices_paths = ["Release"] if rootless else FLAT_INDICES
    else:
        indices_paths = INDICES
    src = make_repo_sync(
        storage=src_storage,
        params=src_params,
        components=src_comp_map,
        release_hash="src" if not same_hash else "shared",
        indices_paths=list(indices_paths),
        listed_indices=listed,
        release_fields=release_fields,
        component_indices=src_component_indices,
    )

    tgt_storage = make_storage_mock(
        list_result=tgt_list,
        list_raises=tgt_list_raises,
    )
    if tgt_metadata is not None:
        tgt_storage.get_metadata.return_value = tgt_metadata
    tgt_params = RepoParams(
        dist="bookworm",
        components=component_names,
        indices_exclude=indices_exclude,
        verify_gpg=False,
        verify_digests=verify_digests,
    )
    tgt_comp_map: dict[str, dict[str, Packages]] = {
        comp: {arch: make_packages(tgt_storage, tgt_params, pkgs) for arch, pkgs in archs.items()}
        for comp, archs in tgt_components.items()
    }
    if flat:
        _make_flat(tgt_params, rootless)
    tgt = make_repo_rw(
        storage=tgt_storage,
        params=tgt_params,
        components=tgt_comp_map,
        release_hash="tgt" if not same_hash else "shared",
        indices_paths=list(indices_paths),
        listed_indices=tgt_listed,
    )
    return src, tgt


# =========================================================================
# § 3a. build_sync_lists
# =========================================================================


def test_build_sync_lists_no_diff_no_changes() -> None:
    pkg = make_package("p1.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert copy_list == []
    assert delete_list == []


def test_build_sync_lists_added_package_triggers_force_indices_copy() -> None:
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", *INDICES],
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert "p2.deb" in copy_list
    for ix in INDICES:
        assert ix in copy_list
    assert delete_list == []


def test_build_sync_lists_orphan_in_target_goes_to_delete_list() -> None:
    p1 = make_package("p1.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", "junk.deb", *INDICES],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert delete_list == ["junk.deb"]
    assert copy_list == []


def test_build_sync_lists_missing_index_forces_indices_copy() -> None:
    p1 = make_package("p1.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", INDICES[0]],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert INDICES[1] in copy_list
    assert INDICES[0] in copy_list
    assert delete_list == []


def test_build_sync_lists_missing_package_goes_to_repair() -> None:
    p1 = make_package("p1.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=list(INDICES),
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert "p1.deb" in copy_list
    assert delete_list == []


def test_build_sync_lists_existing_target_pkg_with_matching_digest_is_skipped() -> None:
    src_pkg = make_package("p1.deb", size=2, SHA256="hash-new")
    tgt_pkg = make_package("p1.deb", size=1, SHA256="hash-old")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [src_pkg]}},
        tgt_components={"main": {"amd64": [tgt_pkg]}},
        tgt_list=["p1.deb", *INDICES],
        tgt_metadata={"SHA256": "hash-new"},
        verify_digests=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert "p1.deb" not in copy_list


def test_build_sync_lists_existing_target_pkg_with_bad_digest_is_kept_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    src_pkg = make_package("p1.deb", size=2, SHA256="hash-new")
    tgt_pkg = make_package("p1.deb", size=1, SHA256="hash-old")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [src_pkg]}},
        tgt_components={"main": {"amd64": [tgt_pkg]}},
        tgt_list=["p1.deb", *INDICES],
        tgt_metadata={"SHA256": "hash-wrong"},
        verify_digests=True,
    )

    with caplog.at_level(logging.WARNING):
        copy_list, _ = src.build_sync_lists(tgt)

    assert "p1.deb" in copy_list
    assert any("have not valid checksums" in r.message for r in caplog.records)


def test_build_sync_lists_existing_target_pkg_dropped_without_metadata_when_verify_off() -> None:
    src_pkg = make_package("p1.deb", size=2, SHA256="hash-new")
    tgt_pkg = make_package("p1.deb", size=1, SHA256="hash-old")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [src_pkg]}},
        tgt_components={"main": {"amd64": [tgt_pkg]}},
        tgt_list=["p1.deb", *INDICES],
        verify_digests=False,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert "p1.deb" not in copy_list
    _mock_storage(tgt).get_metadata.assert_not_called()


def test_build_sync_lists_non_listable_target_skips_orphan_and_repair() -> None:
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list_raises=NotImplementedError,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    # Only diff-derived copy + forced indices, no repair / no orphan deletion.
    assert "p2.deb" in copy_list
    for ix in INDICES:
        assert ix in copy_list
    assert delete_list == []


def test_build_sync_lists_indices_only_forces_indices_and_skips_packages() -> None:
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", *INDICES],
    )

    copy_list, delete_list = src.build_sync_lists(tgt, indices_only=True)

    assert sorted(copy_list) == sorted(INDICES)
    assert "p2.deb" not in copy_list
    assert delete_list == []


def test_build_sync_lists_duplicate_all_deb_across_archs() -> None:
    """An arch-all .deb appears in both amd64 and arm64 indices.

    Previously this raised KeyError because copy_pkg_files.remove() was
    called twice for the same filename (once per arch).
    """
    all_pkg = make_package("pool/main/c/ca-certs/ca-certs_1.0_all.deb", SHA256="aaa")
    arch_pkg = make_package("pool/main/f/foo/foo_1.0_amd64.deb", SHA256="bbb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [all_pkg, arch_pkg], "arm64": [all_pkg]}},
        tgt_components={"main": {"amd64": [], "arm64": []}},
        tgt_list=[
            "pool/main/c/ca-certs/ca-certs_1.0_all.deb",
            "pool/main/f/foo/foo_1.0_amd64.deb",
            *INDICES,
        ],
        tgt_metadata={"SHA256": "aaa"},
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert "pool/main/c/ca-certs/ca-certs_1.0_all.deb" not in copy_list
    assert delete_list == []


def test_build_sync_lists_duplicate_all_deb_inconsistent_digests_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same filename across archs with diverging digests must not be silently hidden."""
    all_pkg_amd64 = make_package("pool/main/c/ca-certs/ca-certs_1.0_all.deb", SHA256="aaa")
    all_pkg_arm64 = make_package("pool/main/c/ca-certs/ca-certs_1.0_all.deb", SHA256="bbb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [all_pkg_amd64], "arm64": [all_pkg_arm64]}},
        tgt_components={"main": {"amd64": [], "arm64": []}},
        tgt_list=list(INDICES),
    )

    with caplog.at_level(logging.WARNING):
        src.build_sync_lists(tgt)

    assert any("inconsistent digests" in r.message for r in caplog.records)


def test_build_sync_lists_remove_pkg_reduced_when_not_in_target_existing() -> None:
    # Locks the line in build_sync_lists that drops remove_pkg_files entries
    # which are no longer present in target.storage.list().
    p1 = make_package("p1.deb")
    p_removed = make_package("p_old.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1]}},
        tgt_components={"main": {"amd64": [p1, p_removed]}},
        tgt_list=["p1.deb", *INDICES],  # p_old.deb intentionally not listed
    )

    _, delete_list = src.build_sync_lists(tgt)

    assert "p_old.deb" not in delete_list


# =========================================================================
# Release-derived index set
# =========================================================================


def _entry(key: str, digest: str = "aaaa", prefix: str = "dists/bookworm") -> IndexEntry:
    """Build an index entry as a root Release lists it.

    `prefix` is the layout prefix `RepoParams.indices_path()` prepends: the default
    componented `dists/<dist>`, `<dist>` for a flat repo, or `""` for a rootless one.
    """
    path = f"{prefix}/{key}" if prefix else key
    return IndexEntry(key, path, {"SHA256": digest, "Size": "1"})


def test_build_sync_lists_release_listed_index_present_at_source_is_copied() -> None:
    """dep11 and i18n indices are listed in the Release, so they belong in the mirror."""
    pkg = make_package("p1.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    i18n = _entry("main/i18n/Translation-en.bz2")
    # a package diff carries the index pass; a listed candidate cannot force it alone
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg, make_package("p2.deb")]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[dep11, i18n],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path in copy_list
    assert i18n.path in copy_list


def test_build_sync_lists_release_listed_index_absent_at_source_is_skipped() -> None:
    """A Release lists the uncompressed Packages that Debian does not publish."""
    pkg = make_package("p1.deb")
    missing = _entry("main/binary-amd64/Packages")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[missing],
        src_exists=set(INDICES),
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert missing.path not in copy_list
    # the target is fully synced, so nothing is forced and nothing is copied
    assert copy_list == []
    assert delete_list == []


def test_build_sync_lists_release_listed_index_in_target_is_not_purged() -> None:
    """Regression: dep11 objects used to be deleted from the target as orphans."""
    pkg = make_package("p1.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        listed=[dep11],
        same_hash=True,
    )

    _, delete_list = src.build_sync_lists(tgt)

    assert dep11.path not in delete_list


def test_build_sync_lists_release_listed_index_filtered_by_component() -> None:
    pkg = make_package("p1.deb")
    contrib = _entry("contrib/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[contrib],
        same_hash=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert contrib.path not in copy_list


def test_build_sync_lists_release_listed_index_filtered_by_arch() -> None:
    """An i386 index is dropped when only amd64 is selected; `all` always stays."""
    pkg = make_package("p1.deb")
    i386 = _entry("main/dep11/Components-i386.yml.gz")
    every = _entry("main/Contents-all.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg, make_package("p2.deb")]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[i386, every],
        src_archs=["amd64"],
        release_fields={"Architectures": "amd64 i386"},
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert i386.path not in copy_list
    assert every.path in copy_list


def test_build_sync_lists_release_listed_index_filtered_by_the_singular_arch_field() -> None:
    """A root Release naming its one architecture in `Architecture` still makes it known."""
    pkg = make_package("p1.deb")
    i386 = _entry("main/binary-i386/Packages.xz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg, make_package("p2.deb")]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[i386],
        src_archs=["amd64"],
        release_fields={"Architecture": "i386"},
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert i386.path not in copy_list


def test_build_sync_lists_release_listed_index_filtered_by_exclude_glob() -> None:
    pkg = make_package("p1.deb")
    i18n = _entry("main/i18n/Translation-en.bz2")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg, make_package("p2.deb")]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[i18n, dep11],
        indices_exclude=["*/i18n/*"],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert i18n.path not in copy_list
    assert dep11.path in copy_list


def test_build_sync_lists_root_release_files_always_included() -> None:
    """Release, Release.gpg and InRelease are not listed in their own digest block."""
    pkg = make_package("p1.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb"],
        listed=[],
        same_hash=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert set(INDICES) <= set(copy_list)


def test_build_sync_lists_unchanged_index_with_matching_digest_is_skipped() -> None:
    """An index the target already holds unchanged is not transferred again."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        tgt_metadata=dict(dep11.digests),
        listed=[dep11],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path not in copy_list
    assert "p2.deb" in copy_list


def test_build_sync_lists_index_with_stale_digest_is_recopied() -> None:
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        tgt_metadata={"SHA256": "stale", "Size": "1"},
        listed=[dep11],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path in copy_list


def test_build_sync_lists_root_release_files_copied_without_digests() -> None:
    """Release, Release.gpg and InRelease carry no digests of their own."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", *INDICES],
        tgt_metadata={"SHA256": "whatever", "Size": "1"},
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert set(INDICES) <= set(copy_list)


def test_build_sync_lists_index_skip_disabled_when_verify_digests_off() -> None:
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        tgt_metadata=dict(dep11.digests),
        listed=[dep11],
        verify_digests=False,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path in copy_list


def test_build_sync_lists_release_hash_change_forces_indices_without_package_diff() -> None:
    """An i18n-only source update changes the Release but no package."""
    pkg = make_package("p1.deb")
    i18n = _entry("main/i18n/Translation-en.bz2")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", i18n.path, *INDICES],
        tgt_metadata={"SHA256": "stale", "Size": "1"},
        listed=[i18n],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert i18n.path in copy_list


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("main/binary-amd64/Packages.xz", True),
        ("main/binary-i386/Packages.xz", False),
        ("main/Contents-udeb-i386.gz", False),
        ("main/Contents-all.gz", True),
        ("main/dep11/icons-64x64.tar.gz", True),
        ("main/dep11/CID-Index-i386.json.gz", False),
        ("main/i18n/Translation-en.xz", True),
        ("main/debian-installer/binary-i386/Packages.gz", False),
        ("contrib/binary-amd64/Packages.xz", False),
        # a Contents file at the dists root belongs to no component
        ("Contents-amd64.gz", True),
        ("Contents-i386.gz", False),
    ],
)
def test_select_index_entry_component_and_arch_rules(key: str, expected: bool) -> None:
    assert (
        select_index_entry(
            key,
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64", "i386"},
            excludes=[],
        )
        is expected
    )


def test_select_index_entry_flat_repo_keeps_component_less_paths() -> None:
    """A flat repo has no component segment, so the component rule does not apply."""
    assert (
        select_index_entry(
            "Packages.xz",
            components=None,
            archs={"amd64"},
            arch_universe={"amd64"},
            excludes=[],
        )
        is True
    )


def test_select_index_entry_exclude_glob_wins_over_selection() -> None:
    assert (
        select_index_entry(
            "main/i18n/Translation-en.bz2",
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64"},
            excludes=["*/i18n/*"],
        )
        is False
    )


# =========================================================================
# § 3b. sync (recursion + retry)
# =========================================================================


def _patch_transfer(repo: RepoSync, results: dict[str, bool | list[bool]] | bool) -> MagicMock:
    """Replace _transfer_file with a mock driven by `results`.

    `results` may be a dict of filename -> per-call results, or a single bool.
    Multiple calls on the same filename consume the list in order.
    """
    state: dict[str, list[bool]] = {}
    if isinstance(results, dict):
        for k, v in results.items():
            state[k] = v if isinstance(v, list) else [v]

    def side_effect(file: str, *_a: object, **_k: object) -> bool:
        if isinstance(results, bool):
            return results
        bucket = state.setdefault(file, [True])
        if len(bucket) > 1:
            return bucket.pop(0)
        return bucket[0] if bucket else True

    mock = MagicMock(side_effect=side_effect)
    repo._transfer_file = mock  # type: ignore[method-assign]
    return mock


def test_sync_happy_path() -> None:
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [make_package("p1.deb")]}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=[],
    )
    transfer = _patch_transfer(src, True)

    src.sync(tgt, copy_list=["p1.deb"], delete_list=["x.deb"])

    transfer.assert_called_once()
    _mock_storage(tgt).delete.assert_called_once_with("x.deb")


def test_sync_single_surviving_failure_raises() -> None:
    """One object that never transfers fails the run; a partial target is not left behind."""
    src, tgt = _two_repos(
        src_components={"main": {"amd64": []}}, tgt_components={"main": {"amd64": []}}, tgt_list=[]
    )
    results: dict[str, bool | list[bool]] = {f"f{i}.deb": True for i in range(20)}
    results["f7.deb"] = False
    _patch_transfer(src, results)

    with pytest.raises(RepoSyncException, match="Max retries reached"):
        src.sync(tgt, copy_list=list(results.keys()), delete_list=[])


def test_sync_transient_failure_is_retried_and_succeeds() -> None:
    """A blip on one object still recovers, so a flaky link does not fail the run."""
    src, tgt = _two_repos(
        src_components={"main": {"amd64": []}}, tgt_components={"main": {"amd64": []}}, tgt_list=[]
    )
    results: dict[str, bool | list[bool]] = {f"f{i}.deb": True for i in range(20)}
    results["f7.deb"] = [False, True]
    _patch_transfer(src, results)

    src.sync(tgt, copy_list=list(results.keys()), delete_list=[])


def test_sync_empty_copy_list_with_deletes_only() -> None:
    src, tgt = _two_repos(
        src_components={"main": {"amd64": []}}, tgt_components={"main": {"amd64": []}}, tgt_list=[]
    )
    transfer = _patch_transfer(src, True)

    src.sync(tgt, copy_list=[], delete_list=["a", "b"])
    transfer.assert_not_called()
    assert _mock_storage(tgt).delete.call_count == 2


# =========================================================================
# § 3c. check (verify + fix)
# =========================================================================


def _patch_verify(repo: RepoSync, outcomes: dict[str, bool | type[BaseException]]) -> MagicMock:
    """Replace _verify_file with a controlled mock."""

    def side_effect(file: str, *_a: object, **_k: object) -> bool:
        outcome = outcomes.get(file, True)
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome("boom")
        return bool(outcome)

    mock = MagicMock(side_effect=side_effect)
    repo._verify_file = mock  # type: ignore[method-assign]
    return mock


def _src_with_pkgs(filenames: list[str]) -> RepoSync:
    src, _ = _two_repos(
        src_components={"main": {"amd64": [make_package(f) for f in filenames]}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=[],
    )
    return src


def test_check_all_clean_returns_zero_zero() -> None:
    src = _src_with_pkgs(["p1.deb", "p2.deb"])
    _patch_verify(src, {})

    assert src.check(missing_list=[], orphan_list=[]) == (0, 0)
    _mock_storage(src).delete.assert_not_called()


def test_check_one_corrupt_no_fix() -> None:
    src = _src_with_pkgs(["p1.deb", "p2.deb"])
    _patch_verify(src, {"p1.deb": False})

    assert src.check(missing_list=[], orphan_list=[], fix=False) == (1, 0)
    _mock_storage(src).delete.assert_not_called()


def test_check_one_corrupt_with_fix_deletes_corrupt() -> None:
    src = _src_with_pkgs(["p1.deb", "p2.deb"])
    _patch_verify(src, {"p1.deb": False})

    assert src.check(missing_list=[], orphan_list=[], fix=True) == (1, 1)
    _mock_storage(src).delete.assert_called_once_with("p1.deb")


def test_check_missing_file_skipped_and_not_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    src = _src_with_pkgs(["p1.deb", "p2.deb"])
    verify = _patch_verify(src, {})

    with caplog.at_level(logging.WARNING):
        assert src.check(missing_list=["p1.deb"], orphan_list=[]) == (0, 0)

    called = {call.args[0] for call in verify.call_args_list}
    assert "p1.deb" not in called
    assert "p2.deb" in called
    assert any("missing" in r.message for r in caplog.records)


def test_check_storage_exception_counts_as_error() -> None:
    src = _src_with_pkgs(["p1.deb", "p2.deb"])
    _patch_verify(src, {"p1.deb": StorageException})

    assert src.check(missing_list=[], orphan_list=[]) == (1, 0)
    _mock_storage(src).delete.assert_not_called()


def test_check_orphans_with_fix_are_deleted_and_counted() -> None:
    src = _src_with_pkgs(["p1.deb"])
    _patch_verify(src, {})

    err, fixed = src.check(missing_list=[], orphan_list=["junk.deb", "old.deb"], fix=True)

    assert (err, fixed) == (0, 2)
    _mock_storage(src).delete.assert_any_call("junk.deb")
    _mock_storage(src).delete.assert_any_call("old.deb")


def test_check_orphans_without_fix_are_not_touched() -> None:
    src = _src_with_pkgs(["p1.deb"])
    _patch_verify(src, {})

    assert src.check(missing_list=[], orphan_list=["junk.deb"], fix=False) == (0, 0)
    _mock_storage(src).delete.assert_not_called()


# =========================================================================
# § 3d. _transfer_file
# =========================================================================


def _transfer_setup(
    *,
    storage_type: StorageType = StorageType.fs,
    transfer_raises: type[BaseException] | None = None,
    verify_digests: bool = True,
) -> tuple[RepoSync, RepoRW]:
    src, _ = _two_repos(
        src_components={"main": {"amd64": []}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=[],
        verify_digests=verify_digests,
    )
    tgt_storage = make_storage_mock(storage_type=storage_type)
    if transfer_raises is not None:
        _mock_storage(src).transfer.side_effect = transfer_raises("boom")
    tgt = make_repo_rw(
        storage=tgt_storage,
        components={},
        release_hash="tgt",
        indices_paths=list(INDICES),
    )
    src.verify_checksum = MagicMock(return_value=True)  # type: ignore[method-assign]
    return src, tgt


@pytest.mark.parametrize("file", ["p1.deb", "dists/bookworm/Release"])
def test_transfer_file_uses_final_path(file: str) -> None:
    src, tgt = _transfer_setup()

    assert src._transfer_file(file, tgt) is True

    src_storage = _mock_storage(src)
    src_storage.transfer.assert_called_once()
    kwargs = src_storage.transfer.call_args.kwargs
    assert kwargs["path_dst"] == file
    _mock_storage(tgt).add_metadata.assert_called_once_with(file, src_storage.transfer.return_value)


def test_transfer_file_storage_exception_returns_false() -> None:
    src, tgt = _transfer_setup(transfer_raises=StorageException)

    assert src._transfer_file("p1.deb", tgt) is False

    tgt_storage = _mock_storage(tgt)
    tgt_storage.delete.assert_not_called()
    tgt_storage.add_metadata.assert_not_called()


def test_transfer_file_bad_digest_raises_and_cleans_up() -> None:
    src, tgt = _transfer_setup(verify_digests=True)
    src.verify_checksum = MagicMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(ChecksumException):
        src._transfer_file("p1.deb", tgt)

    tgt_storage = _mock_storage(tgt)
    tgt_storage.delete.assert_called_once_with("p1.deb")
    tgt_storage.add_metadata.assert_not_called()


def test_transfer_file_verifies_a_dep11_tarball_against_the_release_key() -> None:
    """Regression: a `.tar.gz` index skipped the Release-key mapping, so its lookup
    missed, the transferred object was deleted and the sync aborted."""
    dep11 = _entry("main/dep11/icons-64x64.tar.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": []}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=[],
        listed=[dep11],
    )
    _mock_storage(src).transfer.return_value = dict(dep11.digests)
    cast(MagicMock, src.release).verify_digests.side_effect = lambda key, digests: (
        key == dep11.key and digests == dep11.digests
    )

    assert src._transfer_file(dep11.path, tgt) is True

    tgt_storage = _mock_storage(tgt)
    tgt_storage.delete.assert_not_called()
    tgt_storage.add_metadata.assert_called_once_with(dep11.path, dep11.digests)


def test_transfer_file_skips_checksum_when_verify_digests_off() -> None:
    src, tgt = _transfer_setup(verify_digests=False)
    src.verify_checksum = MagicMock(return_value=False)  # type: ignore[method-assign]

    assert src._transfer_file("p1.deb", tgt) is True

    src.verify_checksum.assert_not_called()
    _mock_storage(tgt).add_metadata.assert_called_once()


def test_transfer_file_does_not_pass_acl_kwarg() -> None:
    """ACL handling now lives in the storage layer (StorageS3.default_acl)."""
    src, tgt = _transfer_setup(storage_type=StorageType.s3)

    assert src._transfer_file("p1.deb", tgt) is True

    kwargs = _mock_storage(src).transfer.call_args.kwargs
    assert "acl" not in kwargs


# =========================================================================
# § 3e. _verify_file
# =========================================================================


def test_verify_file_valid_digests_no_fix() -> None:
    src, _ = _two_repos(
        src_components={"main": {"amd64": []}}, tgt_components={"main": {"amd64": []}}, tgt_list=[]
    )
    src.verify_checksum = MagicMock(return_value=True)  # type: ignore[method-assign]

    assert src._verify_file("p1.deb") is True
    _mock_storage(src).add_metadata.assert_not_called()


def test_verify_file_invalid_digests_returns_false() -> None:
    src, _ = _two_repos(
        src_components={"main": {"amd64": []}}, tgt_components={"main": {"amd64": []}}, tgt_list=[]
    )
    src.verify_checksum = MagicMock(return_value=False)  # type: ignore[method-assign]

    assert src._verify_file("p1.deb") is False
    _mock_storage(src).transfer.assert_called_once()
    _mock_storage(src).add_metadata.assert_not_called()


def test_verify_file_skips_verify_when_digests_disabled() -> None:
    src, _ = _two_repos(
        src_components={"main": {"amd64": []}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=[],
        verify_digests=False,
    )
    src.verify_checksum = MagicMock(return_value=False)  # type: ignore[method-assign]

    assert src._verify_file("p1.deb") is True
    src.verify_checksum.assert_not_called()


def test_verify_file_fix_does_not_pass_acl_kwarg() -> None:
    """ACL handling now lives in the storage layer (StorageS3.default_acl)."""
    src, _ = _two_repos(
        src_components={"main": {"amd64": []}}, tgt_components={"main": {"amd64": []}}, tgt_list=[]
    )
    src_storage = _mock_storage(src)
    src_storage.get_type.return_value = StorageType.s3
    src.verify_checksum = MagicMock(return_value=True)  # type: ignore[method-assign]

    assert src._verify_file("p1.deb", fix=True) is True

    src_storage.add_metadata.assert_called_once()
    kwargs = src_storage.add_metadata.call_args.kwargs
    assert "acl" not in kwargs


# --- exclude scope and purge ---------------------------------------------------------------------


def test_build_sync_lists_exclude_glob_drops_component_discovered_index() -> None:
    """The exclude globs reach indices found by parsing, not just Release-listed ones."""
    pkg = make_package("p1.deb")
    discovered = "dists/bookworm/main/i18n/Translation-en.bz2"
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", discovered, *INDICES],
        src_component_indices={"main": [discovered]},
        indices_exclude=["*/i18n/*"],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert discovered not in copy_list
    assert discovered in delete_list


def test_build_sync_lists_root_release_files_survive_exclude_glob() -> None:
    """The manifest itself is never filtered; a catch-all glob cannot drop it."""
    pkg = make_package("p1.deb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        indices_exclude=["*"],
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert set(INDICES) <= set(copy_list)
    assert not set(INDICES) & set(delete_list)


def test_build_sync_lists_excluded_index_is_purged_from_target() -> None:
    """An excluded index the target still holds is removed as an orphan."""
    pkg = make_package("p1.deb")
    i18n = _entry("main/i18n/Translation-en.bz2")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", i18n.path, *INDICES],
        listed=[i18n],
        indices_exclude=["*/i18n/*"],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert i18n.path not in copy_list
    assert i18n.path in delete_list


# --- index path safety --------------------------------------------------------------------------


def test_build_sync_lists_unsafe_release_listed_index_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hostile Release naming a key outside the repo is dropped, not mirrored."""
    pkg = make_package("p1.deb")
    evil = _entry("main/../../../etc/passwd")
    safe = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[evil, safe],
    )

    with caplog.at_level(logging.WARNING):
        copy_list, _ = src.build_sync_lists(tgt)

    assert evil.path not in copy_list
    assert safe.path in copy_list
    assert "unsafe index path" in caplog.text


@pytest.mark.parametrize(
    "evil_key",
    ["http://mirror.example/outside-the-repo", "http:/mirror.example/outside-the-repo"],
)
def test_build_sync_lists_scheme_form_release_key_is_skipped(
    evil_key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rootless layout hands a Release key to `join_url` unprefixed.

    `urljoin` resolves a reference carrying a scheme against the origin rather than the
    repository prefix, so the key would fetch a sibling path on the source host. No
    layout prefix stands in the way here, which is what makes the rootless one the case
    the guard has to catch.
    """
    pkg = make_package("p1.deb")
    evil = _entry(evil_key, prefix="")
    safe = _entry("Packages.gz", prefix="")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[evil, safe],
        flat=True,
        rootless=True,
    )

    with caplog.at_level(logging.WARNING):
        copy_list, _ = src.build_sync_lists(tgt)

    assert evil.path not in copy_list
    assert "unsafe index path" in caplog.text


# --- source existence probing -------------------------------------------------------------------


def test_build_sync_lists_source_index_existence_never_lists_the_source() -> None:
    """One probe per entry entering the copy list; `list()` would walk the whole pool."""
    pkg = make_package("p1.deb")
    entries = [
        _entry("main/dep11/Components-amd64.yml.gz"),
        _entry("main/i18n/Translation-en.bz2"),
        _entry("main/cnf/Commands-amd64.xz"),
    ]
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=entries,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    cast(MagicMock, src.storage).list.assert_not_called()
    assert cast(MagicMock, src.storage).exists.call_count == len(entries)
    assert all(entry.path in copy_list for entry in entries)


def test_build_sync_lists_source_probe_keeps_only_published_indices() -> None:
    """Each listed entry is probed: the published one is mirrored, the other dropped."""
    pkg = make_package("p1.deb")
    present = _entry("main/dep11/Components-amd64.yml.gz")
    absent = _entry("main/i18n/Translation-en")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[present, absent],
        src_exists={present.path, *INDICES},
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert present.path in copy_list
    assert absent.path not in copy_list
    assert cast(MagicMock, src.storage).exists.call_count == 2


def test_build_sync_lists_component_discovered_index_is_not_probed() -> None:
    """Parsing proved these indices exist, so they are mirrored without a probe."""
    pkg = make_package("p1.deb")
    discovered = "dists/bookworm/main/binary-amd64/Packages.xz"
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        src_component_indices={"main": [discovered]},
        src_exists=set(),
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert discovered in copy_list
    cast(MagicMock, src.storage).exists.assert_not_called()


def test_build_sync_lists_unpublished_listed_index_in_target_is_not_purged() -> None:
    """A path the source Release lists stays out of the orphan set, published or not."""
    pkg = make_package("p1.deb")
    # a dep11 path reaches the set only through the Release, never through a parse
    absent = _entry("main/dep11/Components-amd64.yml")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", absent.path, *INDICES],
        listed=[absent],
        src_exists=set(INDICES),
        same_hash=True,
    )

    _, delete_list = src.build_sync_lists(tgt)

    assert absent.path not in delete_list


def test_build_sync_lists_unpublished_index_does_not_force_the_index_pass() -> None:
    """Absent at the target is expected for a path the source never publishes.

    The probe still runs -- only it can tell that path from one the source 404ed
    once -- but nothing is copied and the root Release files stay out of the pass.
    """
    pkg = make_package("p1.deb")
    absent = _entry("main/dep11/Components-amd64.yml")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[absent],
        src_exists=set(INDICES),
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert copy_list == []
    assert delete_list == []
    cast(MagicMock, src.storage).exists.assert_any_call(absent.path)


def test_build_sync_lists_component_index_digest_matches_release_key() -> None:
    """A component-discovered index the target holds unchanged is not recopied."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    packages = _entry("main/binary-amd64/Packages.xz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", packages.path, *INDICES],
        tgt_metadata=dict(packages.digests),
        listed=[packages],
        src_component_indices={"main": [packages.path]},
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert packages.path not in copy_list
    assert "p2.deb" in copy_list


def test_build_sync_lists_flat_repo_mirrors_dist_prefixed_indices() -> None:
    """A flat repo keys its Release entries without the `<dist>/` path prefix."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    packages = _entry("Packages.xz", prefix="bookworm")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", packages.path, *FLAT_INDICES],
        tgt_metadata=dict(packages.digests),
        listed=[packages],
        src_component_indices={"main": [packages.path]},
        flat=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert packages.path == "bookworm/Packages.xz"
    assert packages.path not in copy_list
    assert "p2.deb" in copy_list


def test_build_sync_lists_flat_repo_recopies_stale_index() -> None:
    """The flat digest-skip engages both ways: a stale index is still transferred."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    packages = _entry("Packages.xz", prefix="bookworm")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", packages.path, *FLAT_INDICES],
        tgt_metadata={"SHA256": "stale", "Size": "1"},
        listed=[packages],
        src_component_indices={"main": [packages.path]},
        flat=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert packages.path in copy_list


def test_build_sync_lists_rootless_flat_repo_mirrors_bare_indices() -> None:
    """A rootless flat repo has no prefix at all, so path and Release key coincide."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    packages = _entry("Packages.xz", prefix="")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", packages.path, *FLAT_INDICES],
        tgt_metadata=dict(packages.digests),
        listed=[packages],
        src_component_indices={"main": [packages.path]},
        flat=True,
        rootless=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert packages.path == "Packages.xz"
    assert packages.path not in copy_list


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("main/binary-kfreebsd-amd64/Packages.xz", True),
        ("main/Contents-kfreebsd-amd64.gz", True),
        # a selected arch is kept even though its suffix arch is unselected
        ("main/binary-hurd-i386/Packages.xz", False),
        ("main/binary-amd64/Packages.xz", False),
        ("main/binary-i386/Packages.xz", False),
    ],
)
def test_select_index_entry_longest_architecture_token_owns_the_match(
    key: str, expected: bool
) -> None:
    """A hyphenated arch owns its token, so an unselected suffix cannot drop it."""
    assert (
        select_index_entry(
            key,
            components={"main"},
            archs={"kfreebsd-amd64"},
            arch_universe={"kfreebsd-amd64", "amd64", "i386", "hurd-i386"},
            excludes=[],
        )
        is expected
    )


def test_select_index_entry_keeps_every_key_when_no_architecture_is_known() -> None:
    """With an empty universe no name part can be an architecture token."""
    assert (
        select_index_entry(
            "main/binary-amd64/Packages.xz",
            components={"main"},
            archs=set(),
            arch_universe=set(),
            excludes=[],
        )
        is True
    )


def test_build_sync_lists_hyphenated_arch_index_is_not_purged_as_an_orphan() -> None:
    """Dropping a selected hyphenated arch purged indices whose packages still copied."""
    pkg = make_package("p1.deb")
    packages = _entry("main/binary-kfreebsd-amd64/Packages.xz")
    src, tgt = _two_repos(
        src_components={"main": {"kfreebsd-amd64": [pkg]}},
        tgt_components={"main": {"kfreebsd-amd64": [pkg]}},
        tgt_list=["p1.deb", packages.path, *INDICES],
        listed=[packages],
        src_archs=["kfreebsd-amd64"],
        release_fields={"Architectures": "kfreebsd-amd64 amd64 i386"},
        same_hash=True,
    )

    _, delete_list = src.build_sync_lists(tgt)

    assert packages.path not in delete_list


@pytest.mark.parametrize(
    ("key", "wanted"),
    [
        ("extra-amd64/binary-arm64/Packages.xz", True),
        ("installer-amd64/binary-arm64/Packages.xz", True),
        ("debian-installer/binary-arm64/Packages.xz", True),
        ("Contents-udeb-extras/binary-arm64/Packages.xz", True),
        # the path below the component still carries the unselected token
        ("extra-amd64/binary-amd64/Packages.xz", False),
    ],
)
def test_select_index_entry_component_name_is_not_read_as_a_token(key: str, wanted: bool) -> None:
    """A component is a chosen name, so only the path below it can trip a structural rule."""
    assert (
        select_index_entry(
            key,
            components={key.partition("/")[0]},
            archs={"arm64"},
            arch_universe={"amd64", "arm64"},
            excludes=[],
        )
        is wanted
    )


def test_build_sync_lists_component_named_for_an_arch_is_not_purged() -> None:
    """A component name ending in an unselected arch purged every index under it."""
    pkg = make_package("p1.deb")
    packages = _entry("extra-armhf/binary-amd64/Packages.xz")
    src, tgt = _two_repos(
        src_components={"extra-armhf": {"amd64": [pkg]}},
        tgt_components={"extra-armhf": {"amd64": [pkg]}},
        tgt_list=["p1.deb", packages.path, *INDICES],
        listed=[packages],
        src_archs=["amd64"],
        release_fields={"Architectures": "amd64 armhf"},
        same_hash=True,
    )

    _, delete_list = src.build_sync_lists(tgt)

    assert packages.path not in delete_list


# --- index skip: manifests before storage -------------------------------------------------------


def test_build_sync_lists_agreeing_release_manifests_skip_the_metadata_probe() -> None:
    """Both Releases record the same digests, so the index costs no round trip on either side."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        listed=[dep11],
        tgt_listed=[dep11],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path not in copy_list
    cast(MagicMock, tgt.storage).get_metadata.assert_not_called()
    cast(MagicMock, src.storage).exists.assert_not_called()


def test_build_sync_lists_disagreeing_release_manifests_fall_back_to_metadata() -> None:
    """A target Release recording something else still gets the storage probe."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    stale = _entry("main/dep11/Components-amd64.yml.gz", digest="bbbb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        tgt_metadata=dict(dep11.digests),
        listed=[dep11],
        tgt_listed=[stale],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path not in copy_list
    cast(MagicMock, tgt.storage).get_metadata.assert_called()


def test_build_sync_lists_index_absent_from_target_release_is_probed() -> None:
    """A key the target Release does not record is not agreement."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        tgt_metadata=dict(dep11.digests),
        listed=[dep11],
        tgt_listed=[],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path not in copy_list
    cast(MagicMock, tgt.storage).get_metadata.assert_called()


def test_build_sync_lists_stale_index_is_recopied_when_neither_source_agrees() -> None:
    """A genuinely changed index is still transferred."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    stale = _entry("main/dep11/Components-amd64.yml.gz", digest="bbbb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        tgt_metadata={"SHA256": "cccc", "Size": "1"},
        listed=[dep11],
        tgt_listed=[stale],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path in copy_list


def test_build_sync_lists_held_index_with_changed_digests_is_not_probed() -> None:
    """An earlier transfer proved the source publishes a held index; a new digest is a republish."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    stale = _entry("main/dep11/Components-amd64.yml.gz", digest="bbbb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", dep11.path, *INDICES],
        tgt_metadata={"SHA256": "cccc", "Size": "1"},
        listed=[dep11],
        tgt_listed=[stale],
        src_exists=set(INDICES),
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert dep11.path in copy_list
    cast(MagicMock, src.storage).exists.assert_not_called()


def test_build_sync_lists_absent_index_both_releases_agree_is_still_probed() -> None:
    """Agreement does not settle an absent key: the last skip may have been a 404.

    The last sync copies the Release whether or not it copied the object, so a source
    that 404s once records the digest anyway. Skipping the probe on agreement would
    keep skipping it on every later run, leaving a published object missing until its
    digest changed.
    """
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    unpublished = _entry("main/i18n/Translation-en")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[unpublished],
        tgt_listed=[unpublished],
        src_exists=set(INDICES),
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert unpublished.path not in copy_list
    cast(MagicMock, src.storage).exists.assert_any_call(unpublished.path)


def test_build_sync_lists_absent_index_published_later_recovers() -> None:
    """A key the source starts publishing under the recorded digest is copied."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    recovered = _entry("main/i18n/Translation-en")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[recovered],
        tgt_listed=[recovered],
        src_exists={*INDICES, recovered.path},
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert recovered.path in copy_list


def test_build_sync_lists_absent_index_is_probed_with_no_other_reason_to_sync() -> None:
    """A one-time source 404 is recovered on the next run, not on the next republish.

    Both root Releases record the entry and the package diff is empty, so nothing
    forces the index pass. The target still lacks the object, and the source now
    serves it, so the probe that recovers it has to run anyway.
    """
    pkg = make_package("p1.deb")
    recovered = _entry("main/i18n/Translation-en")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[recovered],
        tgt_listed=[recovered],
        src_exists={*INDICES, recovered.path},
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert copy_list == [recovered.path]
    assert delete_list == []


@pytest.mark.parametrize("package_diff", [False, True], ids=["nothing-to-sync", "package-diff"])
def test_build_sync_lists_held_index_is_never_probed(package_diff: bool) -> None:
    """A held entry costs no source probe, whether the recovery sweep or a forced pass sees it.

    The source does not publish the entry, so a probe would drop it and show up in the list.
    """
    p1 = make_package("p1.deb")
    held = _entry("main/i18n/Translation-en")
    src_pkgs = [p1, make_package("p2.deb")] if package_diff else [p1]
    src, tgt = _two_repos(
        src_components={"main": {"amd64": src_pkgs}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", *INDICES, held.path],
        listed=[held],
        tgt_listed=[held],
        src_exists=set(INDICES),
        same_hash=not package_diff,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert held.path not in copy_list
    cast(MagicMock, src.storage).exists.assert_not_called()


def test_build_sync_lists_changed_packages_do_not_recopy_an_unchanged_translation() -> None:
    """The index pass a changed Packages forces still skips a Translation both Releases agree on."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    packages = _entry("main/binary-amd64/Packages.xz", digest="new")
    old_packages = _entry("main/binary-amd64/Packages.xz", digest="old")
    translation = _entry("main/i18n/Translation-en.bz2")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", packages.path, translation.path, *INDICES],
        tgt_metadata=dict(old_packages.digests),
        listed=[packages, translation],
        tgt_listed=[old_packages, translation],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert packages.path in copy_list
    assert translation.path not in copy_list
    probed = [call.args[0] for call in cast(MagicMock, tgt.storage).get_metadata.call_args_list]
    assert translation.path not in probed


def test_build_sync_lists_absent_index_with_a_new_digest_is_still_probed() -> None:
    """A changed digest for an absent key may be a first publication, so it is probed."""
    p1 = make_package("p1.deb")
    p2 = make_package("p2.deb")
    published = _entry("main/i18n/Translation-en")
    stale = _entry("main/i18n/Translation-en", digest="bbbb")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1, p2]}},
        tgt_components={"main": {"amd64": [p1]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[published],
        tgt_listed=[stale],
        src_exists={*INDICES, published.path},
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert published.path in copy_list
    cast(MagicMock, src.storage).exists.assert_any_call(published.path)


def test_build_sync_lists_reset_target_recopies_every_held_index() -> None:
    """A target reset after a checksum failure settles nothing, not even by stored metadata.

    The metadata still matches the source: it was written with the bytes the failed
    check distrusts, so skipping on it would leave a corrupt index in place forever.
    """
    p1 = make_package("p1.deb")
    packages = _entry("main/binary-amd64/Packages.xz")
    dep11 = _entry("main/dep11/Components-amd64.yml.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [p1]}},
        tgt_components={},
        tgt_list=["p1.deb", packages.path, dep11.path, *INDICES],
        tgt_metadata=dict(packages.digests),
        listed=[packages, dep11],
    )
    tgt.release.hash = ""

    copy_list, _ = src.build_sync_lists(tgt)

    assert packages.path in copy_list
    assert dep11.path in copy_list
    probed = {call.args[0] for call in cast(MagicMock, tgt.storage).get_metadata.call_args_list}
    assert probed.isdisjoint({packages.path, dep11.path})


def test_build_sync_lists_same_hash_unlistable_target_copies_no_index() -> None:
    """With nothing to sync and no listing to recover from, the index pass stays off."""
    pkg = make_package("p1.deb")
    listed = _entry("main/i18n/Translation-en")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list_raises=NotImplementedError,
        listed=[listed],
        tgt_listed=[listed],
        same_hash=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert copy_list == []
    cast(MagicMock, src.storage).exists.assert_not_called()


def test_build_sync_lists_same_hash_without_digest_verification_copies_no_index() -> None:
    """Digest checks off cannot settle a held entry, so only the unforced pass keeps it out."""
    pkg = make_package("p1.deb")
    held = _entry("main/i18n/Translation-en")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [pkg]}},
        tgt_components={"main": {"amd64": [pkg]}},
        tgt_list=["p1.deb", *INDICES, held.path],
        listed=[held],
        tgt_listed=[held],
        same_hash=True,
        verify_digests=False,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert copy_list == []


def test_target_release_agrees_uses_the_target_layout_key() -> None:
    """The lookup keys the target's own Release by the target's layout.

    The two layouts differ, so the source's prefix would strip `bookworm/` and find
    the key the target Release lists under its own `trixie/` prefix only.
    """
    flat = _entry("Packages.xz", prefix="bookworm")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": []}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=[],
        listed=[flat],
        tgt_listed=[flat],
        flat=True,
    )
    tgt.params.dist = "trixie"

    assert src._target_release_agrees(flat.path, dict(flat.digests), tgt) is False
    moved = _entry("Packages.xz", prefix="trixie")
    assert src._target_release_agrees(moved.path, dict(moved.digests), tgt) is True


# --- source packages ----------------------------------------------------------------------------


_HELLO = "pool/main/h/hello"


def _hello_sources() -> list[Package]:
    """The three payloads one `Sources` stanza names, as expanded records."""
    return [
        make_package(f"{_HELLO}/hello_2.10-3.dsc", 1234, Version="2.10-3", SHA256="a" * 64),
        make_package(f"{_HELLO}/hello_2.10.orig.tar.gz", 725946, Version="2.10-3", SHA256="b" * 64),
        make_package(
            f"{_HELLO}/hello_2.10-3.debian.tar.xz", 6132, Version="2.10-3", SHA256="c" * 64
        ),
    ]


def test_build_sync_lists_treats_source_payloads_as_ordinary_package_set_members() -> None:
    """A payload the Sources index names diffs and copies like any other package.

    The harness injects `components` directly, so this asserts the expansion
    reaches `copy_list` -- not the architecture selection, which
    `test_build_sync_lists_omits_source_payloads_when_source_is_unselected`
    covers through the path that actually gates it.
    """
    deb = make_package("p1.deb")
    sources = _hello_sources()
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [deb], "source": sources}},
        tgt_components={"main": {"amd64": [deb]}},
        tgt_list=["p1.deb", *INDICES],
        src_archs=["amd64", "source"],
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert {str(pkg) for pkg in sources} <= set(copy_list)
    assert not {str(pkg) for pkg in sources} & set(delete_list)


def test_build_sync_lists_omits_source_payloads_when_source_is_unselected() -> None:
    """Without `source` selected the payloads are not mirrored, and stale ones go.

    The source repository carries no `source` container, which is what an
    unselected architecture produces: `Component.load` is never asked for it.
    """
    deb = make_package("p1.deb")
    dsc = f"{_HELLO}/hello_2.10-3.dsc"
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [deb]}},
        tgt_components={"main": {"amd64": [deb]}},
        tgt_list=["p1.deb", dsc, *INDICES],
        src_archs=["amd64"],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert dsc not in copy_list
    assert dsc in delete_list


def test_build_sync_lists_keeps_source_payloads_already_on_the_target() -> None:
    """A mirrored payload is not an orphan: it belongs to the source package set."""
    deb = make_package("p1.deb")
    sources = _hello_sources()
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [deb], "source": sources}},
        tgt_components={"main": {"amd64": [deb], "source": sources}},
        tgt_list=["p1.deb", *[str(pkg) for pkg in sources], *INDICES],
        src_archs=["amd64", "source"],
        same_hash=True,
    )

    _, delete_list = src.build_sync_lists(tgt)

    assert delete_list == []


@pytest.mark.parametrize(
    "key",
    [
        "main/source/Sources.xz",
        "main/source/Release",
        "main/source/by-hash/SHA256/0123abcd",
        "main/source/Sources.diff/Index",
        "main/Contents-source.gz",
        "Contents-source.gz",
    ],
)
@pytest.mark.parametrize("selected", [False, True])
def test_select_index_entry_source_indices_follow_the_source_selection(
    key: str, selected: bool
) -> None:
    """An unselected source architecture drops its indices, as any other would.

    The universe omits `source`, as a Debian `Architectures` field does, so the source
    rule alone has to own every shape of the key.
    """
    archs = {"amd64", "source"} if selected else {"amd64"}

    assert (
        select_index_entry(
            key,
            components={"main"},
            archs=archs,
            arch_universe={"amd64", "i386"},
            excludes=[],
        )
        is selected
    )


@pytest.mark.parametrize(
    ("key", "wanted"),
    [
        ("main/debian-installer/binary-amd64/Packages.gz", False),
        ("main/debian-installer/binary-amd64/Release", False),
        ("main/debian-installer/binary-amd64/Packages.diff/Index", False),
        ("main/installer-amd64/current/images/SHA256SUMS", False),
        ("main/installer-amd64/20230607+deb12u12/images/MD5SUMS", False),
        ("main/Contents-udeb-amd64.gz", False),
        ("Contents-udeb-amd64.gz", False),
        ("main/binary-amd64/Packages.gz", True),
        ("main/Contents-amd64.gz", True),
        # an installer tree names an architecture; any other suffix is a plain directory
        ("main/installer-extras/binary-amd64/Packages.gz", True),
    ],
)
def test_select_index_entry_unparsed_index_directory_is_dropped(key: str, wanted: bool) -> None:
    """Nothing parses these, so mirroring one would advertise udebs the mirror lacks.

    The selected architecture is the point: `-amd64` is wanted everywhere else, so
    only the unparsed rule can drop an installer tree or a udeb listing named for it.
    """
    assert (
        select_index_entry(
            key,
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64", "i386"},
            excludes=[],
        )
        is wanted
    )


def test_select_index_entry_unparsed_directory_is_independent_of_user_excludes() -> None:
    """The rule is structural: a user glob replaces the exclude list, not this."""
    assert (
        select_index_entry(
            "main/debian-installer/binary-amd64/Packages.gz",
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64"},
            excludes=["*/i18n/*"],
        )
        is False
    )


@pytest.mark.parametrize("selected", [False, True])
def test_select_index_entry_flat_sources_index_follows_the_source_selection(
    selected: bool,
) -> None:
    """A flat repo keeps its `Sources` index at the root, with no component segment."""
    archs = {"amd64", "source"} if selected else {"amd64"}

    assert (
        select_index_entry(
            "Sources.xz",
            components=None,
            archs=archs,
            arch_universe={"amd64"},
            excludes=[],
        )
        is selected
    )


def test_build_sync_lists_drops_contents_source_when_the_release_omits_source() -> None:
    """A Debian `Architectures` field omits `source`, so the set must not need it there."""
    deb = make_package("p1.deb")
    contents = _entry("main/Contents-source.gz")
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [deb]}},
        tgt_components={"main": {"amd64": [deb]}},
        tgt_list=["p1.deb", *INDICES],
        listed=[contents],
        src_archs=["amd64"],
        release_fields={"Architectures": "amd64 i386"},
        same_hash=True,
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert contents.path not in copy_list


def test_build_sync_lists_purges_source_objects_when_source_is_unselected() -> None:
    """A target mirrored with sources drops them once source is no longer selected."""
    deb = make_package("p1.deb")
    sources_index = _entry("main/source/Sources.xz")
    source_release = _entry("main/source/Release")
    dsc = f"{_HELLO}/hello_2.10-3.dsc"
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [deb]}},
        tgt_components={"main": {"amd64": [deb]}},
        tgt_list=["p1.deb", sources_index.path, source_release.path, dsc, *INDICES],
        listed=[sources_index, source_release],
        src_archs=["amd64"],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert sources_index.path not in copy_list
    assert {sources_index.path, source_release.path, dsc} <= set(delete_list)


def test_build_sync_lists_purges_debian_installer_objects_from_the_target() -> None:
    """The index goes with its udebs, so the mirror never serves one whose objects 404."""
    deb = make_package("p1.deb")
    di_index = _entry("main/debian-installer/binary-amd64/Packages.gz")
    udeb = "pool/main/h/hello/hello-udeb_2.10_amd64.udeb"
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [deb]}},
        tgt_components={"main": {"amd64": [deb]}},
        tgt_list=["p1.deb", di_index.path, udeb, *INDICES],
        listed=[di_index],
        same_hash=True,
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert di_index.path not in copy_list
    assert {di_index.path, udeb} <= set(delete_list)


def test_build_sync_lists_skips_a_payload_the_release_owns() -> None:
    """A stanza naming a dropped index must not smuggle it in as a package."""
    di_index = _entry("main/debian-installer/binary-amd64/Packages.gz")
    rogue = make_package(di_index.path)
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [rogue]}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=list(INDICES),
        listed=[di_index],
    )

    copy_list, _ = src.build_sync_lists(tgt)

    assert di_index.path not in copy_list


def test_build_sync_lists_skips_a_payload_a_component_release_owns() -> None:
    """A component Release owns its keys as the root does, so its block counts too."""
    di_index = _entry("main/debian-installer/binary-amd64/Packages.gz")
    rogue = make_package(di_index.path)
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [rogue]}},
        tgt_components={"main": {"amd64": []}},
        tgt_list=list(INDICES),
    )
    src.components["main"].packages["amd64"].release.digests = {
        di_index.key: dict(di_index.digests)
    }

    copy_list, _ = src.build_sync_lists(tgt)

    assert di_index.path not in copy_list


def test_build_sync_lists_purges_a_payload_the_release_owns() -> None:
    """The index set decides the dropped index, so the orphan pass still reclaims it."""
    di_index = _entry("main/debian-installer/binary-amd64/Packages.gz")
    rogue = make_package(di_index.path)
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [rogue]}},
        tgt_components={"main": {"amd64": [rogue]}},
        tgt_list=[di_index.path, *INDICES],
        listed=[di_index],
        same_hash=True,
    )

    _, delete_list = src.build_sync_lists(tgt)

    assert di_index.path in delete_list


def test_build_sync_lists_keeps_a_selected_index_a_vanished_target_stanza_named() -> None:
    """The index set keeps a selected index, so removing the stanza must not delete it."""
    index = _entry("main/binary-amd64/Packages.gz")
    rogue = make_package(index.path)
    src, tgt = _two_repos(
        src_components={"main": {"amd64": []}},
        tgt_components={"main": {"amd64": [rogue]}},
        tgt_list=[index.path, *INDICES],
        listed=[index],
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert index.path in copy_list
    assert index.path not in delete_list


def test_build_sync_lists_purges_a_dropped_index_a_vanished_target_stanza_named() -> None:
    """Leaving the removal to the index set still reclaims an index it drops."""
    di_index = _entry("main/debian-installer/binary-amd64/Packages.gz")
    rogue = make_package(di_index.path)
    src, tgt = _two_repos(
        src_components={"main": {"amd64": []}},
        tgt_components={"main": {"amd64": [rogue]}},
        tgt_list=[di_index.path, *INDICES],
        listed=[di_index],
    )

    copy_list, delete_list = src.build_sync_lists(tgt)

    assert di_index.path not in copy_list
    assert di_index.path in delete_list


def test_payload_the_release_owns_is_reported_once_per_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`check` rebuilds the package set, so the report must not repeat it."""
    di_index = _entry("main/debian-installer/binary-amd64/Packages.gz")
    rogue = make_package(di_index.path)
    src, tgt = _two_repos(
        src_components={"main": {"amd64": [rogue]}},
        tgt_components={"main": {"amd64": [rogue]}},
        tgt_list=[di_index.path, *INDICES],
        listed=[di_index],
        same_hash=True,
    )

    with caplog.at_level(logging.WARNING):
        src.build_sync_lists(tgt)
        src.check([], [])

    reports = [r for r in caplog.records if "named by a Release digest block" in r.message]
    assert len(reports) == 1
