"""Tests for repo/__init__.py: the Repo base, its component/arch resolution, and
`verify_checksum`'s source order (CRYPTO.md#verification-order).
"""

from __future__ import annotations

from copy import copy

import pytest
from helpers.factories import make_package, make_packages, make_params, make_storage_mock

from debkit.repo import Repo
from debkit.repo.component import Component
from debkit.repo.package import PackageException
from debkit.repo.params import RepoParams

_DIGEST = {"SHA256": "a" * 64}
_OTHER_DIGEST = {"SHA256": "b" * 64}
_DEB = "pool/main/h/hello/hello_1.0_amd64.deb"


def _repo(params: RepoParams | None = None) -> Repo:
    """Repo over a mocked storage, with no indices loaded."""
    return Repo(make_storage_mock(), params or make_params())


def _with_component_package(
    repo: Repo, filename: str, digests: dict[str, str], arch: str = "amd64"
) -> None:
    """Attach one component holding a package index with a single package."""
    pkg = make_package(filename, **digests)
    packages = make_packages(repo.storage, repo.params, [pkg])
    component = Component(repo.storage, repo.params, repo.release, "main")
    component.packages = {arch: packages}
    repo.components["main"] = component


# --- verification order -------------------------------------------------------------------------


def test_verify_checksum_accepts_a_package_matching_its_index_entry() -> None:
    repo = _repo()
    _with_component_package(repo, _DEB, _DIGEST)

    assert repo.verify_checksum(_DEB, _DIGEST) is True


def test_verify_checksum_rejects_a_package_whose_index_digest_differs() -> None:
    repo = _repo()
    _with_component_package(repo, _DEB, _DIGEST)

    assert repo.verify_checksum(_DEB, _OTHER_DIGEST) is False


@pytest.mark.parametrize("filename", ["Release", "InRelease", "Release.gpg"])
def test_verify_checksum_binds_release_files_to_the_loaded_bytes(filename: str) -> None:
    """A root Release copy must be the bytes the load verified, not a second fetch."""
    repo = _repo()
    repo.release.root_digests = {filename: _DIGEST}

    assert repo.verify_checksum(f"dists/bookworm/{filename}", _DIGEST) is True
    assert repo.verify_checksum(f"dists/bookworm/{filename}", _OTHER_DIGEST) is False


@pytest.mark.parametrize("filename", ["Release", "InRelease", "Release.gpg"])
def test_verify_checksum_refuses_a_release_file_the_load_never_fetched(filename: str) -> None:
    assert _repo().verify_checksum(f"dists/bookworm/{filename}", _DIGEST) is False


def test_verify_checksum_ignores_a_stanza_naming_a_root_release_file() -> None:
    """Only the loaded bytes vouch for a root Release file, never a package index."""
    repo = _repo()
    _with_component_package(repo, "dists/bookworm/Release", _DIGEST)

    assert repo.verify_checksum("dists/bookworm/Release", _DIGEST) is False


def test_verify_checksum_accepts_an_index_listed_in_the_component_release() -> None:
    repo = _repo()
    _with_component_package(repo, _DEB, _DIGEST)
    packages = repo.components["main"].packages["amd64"]
    packages.release.digests = {"main/binary-amd64/Packages": _DIGEST}

    assert repo.verify_checksum("dists/bookworm/main/binary-amd64/Packages", _DIGEST) is True


def test_verify_checksum_accepts_an_index_listed_in_the_root_release() -> None:
    repo = _repo()
    repo.release.digests = {"main/binary-amd64/Packages": _DIGEST}

    assert repo.verify_checksum("dists/bookworm/main/binary-amd64/Packages", _DIGEST) is True


def test_verify_checksum_returns_false_when_no_index_verifies() -> None:
    assert _repo().verify_checksum("dists/bookworm/main/binary-amd64/Packages", _DIGEST) is False


def test_verify_checksum_keeps_the_indices_prefix_on_a_package_path() -> None:
    """A flat repo has no prefix to strip, so the raw path must still verify."""
    repo = _repo(make_params(flat=True))
    _with_component_package(repo, _DEB, _DIGEST)

    assert repo.verify_checksum(_DEB, _DIGEST) is True


@pytest.mark.parametrize(
    "filename",
    [
        "pool/main/h/hello/hello_2.10.orig.tar.zst",
        "pool/main/h/hello/hello_2.10.orig.tar.lzma",
        "pool/main/h/hello/hello_2.10-3.diff.xz",
    ],
)
def test_verify_checksum_accepts_any_payload_an_index_names(filename: str) -> None:
    """`Sources` indexes every compression dpkg-source emits, so no suffix list gates them."""
    repo = _repo()
    _with_component_package(repo, filename, _DIGEST)

    assert repo.verify_checksum(filename, _DIGEST) is True
    assert repo.verify_checksum(filename, _OTHER_DIGEST) is False


def test_verify_checksum_rejects_a_pool_path_no_index_names() -> None:
    """A pool path is outside the Release digest block, so an unindexed one has no source."""
    repo = _repo()
    _with_component_package(repo, _DEB, _DIGEST)

    assert repo.verify_checksum("pool/main/h/hello/hello_1.0.txt", _DIGEST) is False


def test_verify_checksum_accepts_a_flat_udeb_whose_filename_carries_the_dist_prefix() -> None:
    """A pool path is matched raw against its `Packages` entry, before any mapping."""
    repo = _repo(make_params(flat=True))
    udeb = "bookworm/pool/main/h/hello/hello_1.0_amd64.udeb"
    _with_component_package(repo, udeb, _DIGEST)

    assert repo.verify_checksum(udeb, _DIGEST) is True
    assert repo.verify_checksum(udeb, _OTHER_DIGEST) is False


@pytest.mark.parametrize(
    ("params", "path"),
    [
        (make_params(), "dists/bookworm/main/dep11/icons-64x64.tar.gz"),
        (make_params(flat=True), "bookworm/main/dep11/icons-64x64.tar.gz"),
    ],
    ids=["componented", "flat"],
)
def test_verify_checksum_maps_a_package_suffixed_index_through_the_release_key(
    params: RepoParams, path: str
) -> None:
    """A dep11 icon tarball ends in `.tar.gz` but is a Release-listed index, so the
    Release lookups must still see it under its Release key."""
    repo = _repo(params)
    repo.release.digests["main/dep11/icons-64x64.tar.gz"] = _DIGEST

    assert repo.verify_checksum(path, _DIGEST) is True
    assert repo.verify_checksum(path, _OTHER_DIGEST) is False


def test_verify_checksum_maps_a_non_package_index_through_the_release_key() -> None:
    """A non-package path is still mapped, so the flat layout prefix is stripped."""
    repo = _repo(make_params(flat=True))
    repo.release.digests["Packages.xz"] = _DIGEST

    assert repo.verify_checksum("bookworm/Packages.xz", _DIGEST) is True


@pytest.mark.parametrize(
    "filename",
    [
        "pool/main/h/hello/hello_2.10-3.dsc",
        "pool/main/h/hello/hello_2.10-3.diff.gz",
        "pool/main/h/hello/hello_2.10.orig.tar.gz.asc",
    ],
)
def test_verify_checksum_accepts_a_source_payload_through_its_index(filename: str) -> None:
    """Source payload suffixes must reach the index lookup like any other package."""
    repo = _repo()
    _with_component_package(repo, filename, _DIGEST, arch="source")

    assert repo.verify_checksum(filename, _DIGEST) is True
    assert repo.verify_checksum(filename, _OTHER_DIGEST) is False


# --- component and architecture resolution ------------------------------------------------------


def test_get_components_prefers_the_params_list() -> None:
    assert list(_repo().get_components()) == ["main"]


def test_get_components_falls_back_to_the_release_field() -> None:
    repo = _repo(RepoParams(dist="bookworm"))
    repo.release.fields = {"Components": "main contrib"}

    assert list(repo.get_components()) == ["main", "contrib"]


def test_get_components_drops_an_unsafe_release_token(caplog: pytest.LogCaptureFixture) -> None:
    repo = _repo(RepoParams(dist="bookworm"))
    repo.release.fields = {"Components": "main .. ../../x contrib"}

    assert list(repo.get_components()) == ["main", "contrib"]
    assert "'../../x' is not a safe name" in caplog.text


def test_get_components_defaults_to_the_flat_marker() -> None:
    repo = _repo(RepoParams(dist="bookworm"))
    repo.release.fields = {}

    assert list(repo.get_components()) == ["/"]


def test_get_archs_prefers_the_params_list() -> None:
    assert list(_repo(RepoParams(dist="bookworm", archs=["arm64"])).get_archs()) == ["arm64"]


def test_get_archs_from_the_release_field_drops_all() -> None:
    repo = _repo(RepoParams(dist="bookworm"))
    repo.release.fields = {"Architectures": "amd64 all arm64"}

    assert list(repo.get_archs()) == ["amd64", "arm64"]


def test_get_archs_from_the_release_field_drops_a_token_that_is_not_an_architecture() -> None:
    """A signed but hostile Release must not steer the `binary-<arch>/` directory."""
    repo = _repo(RepoParams(dist="bookworm"))
    repo.release.fields = {"Architectures": "amd64 x/../.. arm64"}

    assert list(repo.get_archs()) == ["amd64", "arm64"]


def test_get_archs_from_the_singular_release_field() -> None:
    repo = _repo(RepoParams(dist="bookworm"))
    repo.release.fields = {"Architecture": "i386"}

    assert list(repo.get_archs()) == ["i386"]


def test_get_archs_from_an_invalid_singular_release_field_defaults_to_an_empty_arch() -> None:
    repo = _repo(make_params(flat=True))
    repo.release.fields = {"Architecture": "x/.."}

    assert list(repo.get_archs()) == [""]


def test_release_archs_ignores_the_params_list() -> None:
    """The declared set feeds index selection even when `--architectures` narrows it."""
    repo = _repo(RepoParams(dist="bookworm", archs=["amd64"]))
    repo.release.fields = {"Architectures": "amd64 all i386"}

    assert list(repo.release_archs()) == ["amd64", "i386"]


def test_get_archs_of_a_flat_repo_defaults_to_a_single_empty_arch() -> None:
    repo = _repo(make_params(flat=True))
    repo.release.fields = {}

    assert list(repo.get_archs()) == [""]


def test_get_archs_of_a_componented_repo_without_architectures_reads_its_index_paths() -> None:
    """An empty arch would write `main/binary-/Packages` and a Release over the root one."""
    repo = _repo()
    repo.release.fields = {"Components": "main"}
    for key in (
        "main/binary-arm64/Packages.gz",
        "main/binary-amd64/Packages",
        "main/binary-amd64/Packages.xz",
        "main/binary-all/Packages",
        "main/binary-X..Y/Packages",
        "main/source/Sources.gz",
        "contrib/binary-i386/Packages",
        "main/binary-mips/Release",
        "main/i18n/Translation-en",
    ):
        repo.release.update_digests(key, _DIGEST)

    assert list(repo.get_archs()) == ["amd64", "arm64"]


def test_get_archs_of_a_componented_repo_without_any_index_is_empty() -> None:
    repo = _repo()
    repo.release.fields = {}

    assert list(repo.get_archs()) == []


def test_update_drops_a_component_the_reloaded_release_no_longer_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload replaces the components, so a sync pass never plans against a dropped one."""
    repo = _repo()
    repo.components["contrib"] = Component(repo.storage, repo.params, repo.release, "contrib")
    monkeypatch.setattr(repo.release, "load", lambda: True)
    monkeypatch.setattr(Component, "load", lambda self, archs, **_: 0)

    repo.update()

    assert list(repo.components) == ["main"]


# --- release precedence over a colliding package stanza -----------------------------------------


_INDEX_PATH = "dists/bookworm/main/i18n/Translation-en.xz"
_INDEX_KEY = "main/i18n/Translation-en.xz"


def test_verify_checksum_rejects_a_package_stanza_shadowing_a_listed_index() -> None:
    """A stanza naming a Release-listed path must not define that path's content.

    The root Release is GPG-verified, so it owns every key in its digest block. A
    package index is remote metadata; letting it answer first would let a colliding
    stanza launder attacker bytes past the signed digest.
    """
    repo = _repo()
    _with_component_package(repo, _INDEX_PATH, _OTHER_DIGEST)
    repo.release.digests = {_INDEX_KEY: _DIGEST}

    assert repo.verify_checksum(_INDEX_PATH, _OTHER_DIGEST) is False


def test_verify_checksum_accepts_a_listed_index_matching_the_root_release() -> None:
    """The Release digest still verifies its own key when a stanza collides with it."""
    repo = _repo()
    _with_component_package(repo, _INDEX_PATH, _OTHER_DIGEST)
    repo.release.digests = {_INDEX_KEY: _DIGEST}

    assert repo.verify_checksum(_INDEX_PATH, _DIGEST) is True


def test_verify_checksum_rejects_a_stanza_shadowing_a_component_listed_index() -> None:
    """A component Release is chained to the root by digest, so it owns its keys too."""
    repo = _repo()
    _with_component_package(repo, _INDEX_PATH, _OTHER_DIGEST)
    repo.components["main"].packages["amd64"].release.digests = {_INDEX_KEY: _DIGEST}

    assert repo.verify_checksum(_INDEX_PATH, _OTHER_DIGEST) is False


def test_verify_checksum_still_uses_the_index_for_a_path_no_release_lists() -> None:
    """A pool object is outside every digest block, so its index stays authoritative."""
    repo = _repo()
    _with_component_package(repo, _DEB, _DIGEST)
    repo.release.digests = {_INDEX_KEY: _DIGEST}

    assert repo.verify_checksum(_DEB, _DIGEST) is True


# --- copies -------------------------------------------------------------------------------------


def _loaded_repo() -> Repo:
    """Repo with one indexed component and a digest record in its root Release."""
    repo = _repo()
    repo.release.update_digests("main/binary-amd64/Packages", {"SHA256": "old", "Size": "1"})
    repo.release.root_digests = {"Release": {"SHA256": "root"}}
    _with_component_package(repo, _DEB, _DIGEST)
    return repo


def test_copy_rebinds_each_component_to_the_copied_release() -> None:
    """A copy's save records index digests in its own Release, never the original's."""
    repo = _loaded_repo()

    copied = copy(repo)

    assert copied.release is not repo.release
    assert copied.components["main"].root_release is copied.release
    assert repo.components["main"].root_release is repo.release


def test_copied_release_digest_records_are_independent() -> None:
    """`update_digests` mutates a record in place, so a shared one would change both."""
    repo = _loaded_repo()
    copied = copy(repo)

    copied.release.update_digests("main/binary-amd64/Packages", {"SHA256": "new"})
    copied.release.root_digests["Release"]["SHA256"] = "changed"

    assert repo.release.digests["main/binary-amd64/Packages"]["SHA256"] == "old"
    assert repo.release.root_digests["Release"]["SHA256"] == "root"


# --- diff ---------------------------------------------------------------------------------------


def test_diff_compares_packages_even_when_the_release_hashes_match() -> None:
    """A target load that skipped an unreadable index keeps the source's Release bytes."""
    source, target = _repo(), _repo()
    source.release.hash = target.release.hash = "same"
    _with_component_package(source, _DEB, _DIGEST)

    diff = source.diff(target)

    assert diff.get_filenames_to_copy() == {_DEB}


# --- stats --------------------------------------------------------------------------------------


def test_stats_refuses_a_package_size_that_is_not_a_byte_count() -> None:
    """A malformed index `Size` is a read error, not a stray `ValueError` from `int()`."""
    repo = _repo()
    _with_component_package(repo, _DEB, {"Size": "12kB"})

    with pytest.raises(PackageException, match="malformed Size"):
        repo.stats()


def test_diff_reports_the_packages_of_a_slot_only_the_older_repo_holds_as_removed() -> None:
    """Regression: a slot the source dropped was skipped, so the sync report said Removed 0."""
    newer, older = _repo(), _repo()
    _with_component_package(older, _DEB, _DIGEST, arch="arm64")

    diff = newer.diff(older)

    assert diff.get_filenames_to_remove() == {_DEB}
    assert diff.get_filenames_to_copy() == set()
