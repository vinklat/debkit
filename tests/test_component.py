"""Unit tests for ``Component`` (TESTING.md § 7c).

Covers: exception repr, load/save/setup/insert/remove orchestration,
copy semantics, is_flat property, and arch-check behaviour.
Storage and Packages are mocked; no real backend.
"""

from __future__ import annotations

import logging
import re
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from helpers.factories import make_package, make_params, make_storage_mock
from txstore import StorageException

from debkit.repo.component import Component, ComponentException, IndexLoad, container_for
from debkit.repo.packages import Packages, PackagesException, PackagesNotFoundException
from debkit.repo.params import SOURCE_ARCH, RepoParams
from debkit.repo.release import Release
from debkit.repo.sources import Sources


def _component(name: str = "main", params: RepoParams | None = None) -> Component:
    p = params or make_params()
    storage = make_storage_mock()
    release = MagicMock(spec=Release)
    return Component(storage, p, release, name)


def _root_release(comp: Component) -> MagicMock:
    """Return the release mock that `_component` attached."""
    return cast(MagicMock, comp.root_release)


# ---------------------------------------------------------------------------
# Exception repr
# ---------------------------------------------------------------------------


def test_component_exception_message_no_self_leak() -> None:
    """Regression: ``__init__(self, ...)`` used to leak the instance into args."""
    exc = ComponentException("boom")
    assert str(exc) == "Component: boom"


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def test_component_load_non_flat_aggregates_size() -> None:
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.is_empty.return_value = True
        pkgs_mock.size = 7
        PackagesCls.return_value = pkgs_mock
        total = comp.load(["amd64"])
    assert total == 7
    pkgs_mock.release.load_component.assert_called_once_with(
        "main", "amd64", root_release=comp.root_release
    )
    pkgs_mock.load.assert_called_once()


def test_component_load_storage_exception_skips_when_unreadable_may_be_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.load_component.side_effect = StorageException("not found")
        PackagesCls.return_value = pkgs_mock
        with caplog.at_level(logging.WARNING):
            total = comp.load(["amd64"], IndexLoad.READ_ONLY)
    assert total == 0
    assert "Architecture amd64 not available" in caplog.text


def test_component_load_storage_exception_stops_a_load_that_may_save() -> None:
    """A fresh slot would overwrite the arch's index, so an unread Release stops the load."""
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.load_component.side_effect = StorageException("timeout")
        PackagesCls.return_value = pkgs_mock
        with pytest.raises(ComponentException, match="Cannot load main: amd64: timeout"):
            comp.load(["amd64"])
    assert comp.packages == {}


def test_component_load_storage_exception_strict_raises() -> None:
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.load_component.side_effect = StorageException("not found")
        PackagesCls.return_value = pkgs_mock
        with pytest.raises(ComponentException, match="Cannot load main"):
            comp.load(["amd64"], IndexLoad.STRICT)


def test_component_load_architectures_field_multi_token_succeeds() -> None:
    """Regression: per-arch Release listing ``Architectures: amd64 arm64 all``."""
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.is_empty.return_value = False
        pkgs_mock.release.fields = {"Architectures": "amd64 arm64 all"}
        pkgs_mock.size = 1
        PackagesCls.return_value = pkgs_mock
        total = comp.load(["amd64"])
    assert total == 1


def test_component_load_architecture_mismatch_raises() -> None:
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.is_empty.return_value = False
        pkgs_mock.release.fields = {"Architectures": "i386"}
        pkgs_mock.size = 0
        PackagesCls.return_value = pkgs_mock
        with pytest.raises(ComponentException, match="architecture check mismatch"):
            comp.load(["amd64"])


def test_component_load_packages_failure_strict_raises() -> None:
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.load.side_effect = PackagesException("bad")
        PackagesCls.return_value = pkgs_mock
        with pytest.raises(ComponentException, match="Cannot load main"):
            comp.load(["amd64"], IndexLoad.STRICT)


@pytest.mark.parametrize("mode", [IndexLoad.WRITABLE, IndexLoad.READ_ONLY])
def test_component_load_skips_an_arch_without_an_index(mode: IndexLoad) -> None:
    """An insert starts the index of an arch that has none yet."""
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.load.side_effect = PackagesNotFoundException("missing indices file")
        PackagesCls.return_value = pkgs_mock
        assert comp.load(["amd64"], mode) == 0
    assert comp.packages == {}


def test_component_load_stops_on_an_index_that_does_not_load() -> None:
    """Regression: the arch was skipped, so a save replaced its index with a fresh one."""
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.load.side_effect = PackagesException("Cannot decode indices")
        PackagesCls.return_value = pkgs_mock
        with pytest.raises(ComponentException, match="amd64: Packages: Cannot decode"):
            comp.load(["amd64"])
    assert comp.packages == {}


def test_component_load_skips_an_unreadable_index_when_asked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.load.side_effect = PackagesException("Cannot decode indices")
        PackagesCls.return_value = pkgs_mock
        with caplog.at_level(logging.WARNING):
            assert comp.load(["amd64"], IndexLoad.READ_ONLY) == 0
    assert comp.packages == {}
    assert "Packages for amd64 failed: Packages: Cannot decode indices" in caplog.text


@pytest.mark.parametrize("mode", list(IndexLoad))
def test_component_load_index_storage_failure_propagates_in_every_mode(mode: IndexLoad) -> None:
    """A backend failure reading an existing index is not an unreadable index to skip."""
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.is_empty.return_value = True
        pkgs_mock.load.side_effect = StorageException("timeout")
        PackagesCls.return_value = pkgs_mock
        with pytest.raises(StorageException, match="timeout"):
            comp.load(["amd64"], mode)
    assert comp.packages == {}


def test_component_load_skips_an_unreadable_sources_index() -> None:
    """A `Sources` index is never written, so no save can replace it."""
    comp = _component()
    with patch("debkit.repo.component.Sources") as SourcesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.load.side_effect = PackagesException("carries no SHA digest")
        SourcesCls.return_value = pkgs_mock
        assert comp.load(["source"]) == 0
    assert comp.packages == {}


def test_component_load_source_arch_builds_a_sources_container() -> None:
    """The source architecture needs the Sources parser, not the Packages one."""
    comp = _component()
    with (
        patch("debkit.repo.component.Packages") as PackagesCls,
        patch("debkit.repo.component.Sources") as SourcesCls,
    ):
        for cls in (PackagesCls, SourcesCls):
            mock = MagicMock()
            mock.release.is_empty.return_value = True
            mock.size = 1
            cls.return_value = mock
        comp.load(["amd64", "source"])

    PackagesCls.assert_called_once()
    SourcesCls.assert_called_once()
    assert comp.packages["source"] is SourcesCls.return_value


def test_component_load_source_arch_accepts_an_architecture_source_release() -> None:
    """`<comp>/source/Release` declares `Architecture: source`, which must pass."""
    comp = _component()
    with patch("debkit.repo.component.Sources") as SourcesCls:
        pkgs_mock = MagicMock(spec=Packages(make_storage_mock(), make_params()))
        pkgs_mock.release.is_empty.return_value = False
        pkgs_mock.release.fields = {"Architecture": "source"}
        pkgs_mock.size = 2
        SourcesCls.return_value = pkgs_mock
        assert comp.load(["source"]) == 2


# ---------------------------------------------------------------------------
# copy / is_flat property
# ---------------------------------------------------------------------------


def test_component_is_flat_delegates_to_params() -> None:
    p = make_params(flat=True)
    comp = _component(params=p)
    assert comp.is_flat is True
    with pytest.raises(AttributeError):
        comp.is_flat = False  # type: ignore[misc]  # asserts the property stays read-only


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_component_save_no_modified_returns_false() -> None:
    comp = _component()
    p_mock = MagicMock()
    p_mock.save.return_value = None
    comp.packages = {"amd64": p_mock}
    assert comp.save() is False


def test_component_save_propagates_digests_to_root_release() -> None:
    comp = _component()
    p_mock = MagicMock()
    p_mock.save.return_value = {
        "dists/bookworm/main/binary-amd64/Packages.xz": {"SHA256": "aa"},
    }
    comp.packages = {"amd64": p_mock}
    assert comp.save() is True
    _root_release(comp).update_digests.assert_called_once()
    args, _ = _root_release(comp).update_digests.call_args
    # The dists/<dist> prefix must be stripped before calling update_digests.
    assert args[0] == "main/binary-amd64/Packages.xz"


def test_component_save_strips_the_flat_dist_prefix_from_release_keys() -> None:
    """A flat repo keys its Release entries without the `<dist>/` path prefix."""
    comp = _component(params=make_params(flat=True))
    p_mock = MagicMock()
    p_mock.save.return_value = {"bookworm/Packages.gz": {"SHA256": "aa"}}
    comp.packages = {"amd64": p_mock}

    assert comp.save() is True
    args, _ = _root_release(comp).update_digests.call_args
    assert args[0] == "Packages.gz"


# ---------------------------------------------------------------------------
# remove_pkg
# ---------------------------------------------------------------------------


def test_component_remove_pkg_iterates_all_arches() -> None:
    """Every arch's Packages.remove is invoked; a payload listed in both is returned once."""
    comp = _component()
    a, b = MagicMock(), MagicMock()
    a.remove.return_value = {"pool/foo_all.deb", "pool/foo_amd64.deb"}
    b.remove.return_value = {"pool/foo_all.deb"}
    comp.packages = {"amd64": a, "arm64": b}
    assert comp.remove_pkg({"Package": re.compile("foo")}) == {
        "pool/foo_all.deb",
        "pool/foo_amd64.deb",
    }
    a.remove.assert_called_once()
    b.remove.assert_called_once()


def test_component_remove_pkg_no_match_returns_false() -> None:
    comp = _component()
    a = MagicMock()
    a.remove.return_value = set()
    comp.packages = {"amd64": a}
    assert comp.remove_pkg({"Package": re.compile("x")}) == set()


def test_component_remove_pkg_skips_the_read_only_source_container() -> None:
    """A binary removal must not abort on the mirrored `Sources` index.

    `Component.load` builds a `Sources` for the source architecture, whose
    `remove()` matches nothing, so the binary match alone decides the result.
    """
    comp = _component()
    binary = MagicMock()
    binary.remove.return_value = {"pool/foo.deb"}
    comp.packages = {
        "amd64": binary,
        "source": Sources(make_storage_mock(), make_params()),
    }

    assert comp.remove_pkg({"Package": re.compile("foo")}) == {"pool/foo.deb"}

    binary.remove.assert_called_once()


def test_component_remove_pkg_returns_false_when_only_the_source_container_is_loaded() -> None:
    """Nothing is removable, but the removal still reports cleanly."""
    comp = _component()
    comp.packages = {"source": Sources(make_storage_mock(), make_params())}
    assert comp.remove_pkg({"Package": re.compile("foo")}) == set()


# ---------------------------------------------------------------------------
# setup / insert_pkg
# ---------------------------------------------------------------------------


def test_component_setup_creates_packages_per_arch() -> None:
    comp = _component()
    with patch("debkit.repo.component.Packages") as PackagesCls:
        PackagesCls.side_effect = lambda *a, **kw: MagicMock()
        comp.setup(["amd64", "arm64"])
    assert set(comp.packages) == {"amd64", "arm64"}


def test_component_insert_pkg_on_unknown_arch_creates_packages() -> None:
    comp = _component()
    pkg = make_package("pool/main/f/foo.deb")

    comp.insert_pkg("amd64", pkg)

    created = comp.packages["amd64"]
    assert type(created) is Packages
    assert created.packages == {"pool/main/f/foo.deb": pkg}
    assert created.used_indices_path == "dists/bookworm/main/binary-amd64/Packages.gz"


# ---------------------------------------------------------------------------
# container_for: one owner for the SOURCE_ARCH mapping
# ---------------------------------------------------------------------------


def test_container_for_maps_the_source_arch_to_sources() -> None:
    assert container_for(SOURCE_ARCH) is Sources


@pytest.mark.parametrize("arch", ["amd64", "arm64", "all"])
def test_container_for_maps_a_binary_arch_to_packages(arch: str) -> None:
    assert container_for(arch) is Packages


def test_component_setup_builds_a_sources_container_for_the_source_arch() -> None:
    """`Packages.setup` names its file through the layout, so a binary container would
    write a binary index into the canonical `Sources` path the next load cannot parse.
    """
    comp = _component()
    comp.setup([SOURCE_ARCH])

    assert isinstance(comp.packages[SOURCE_ARCH], Sources)


def test_component_insert_pkg_refuses_the_read_only_source_index() -> None:
    """A mirrored `Sources` index is read-only, so the insert is refused, not written."""
    comp = _component()
    pkg = make_package("pool/main/f/foo.dsc")

    with pytest.raises(PackagesException):
        comp.insert_pkg(SOURCE_ARCH, pkg)


@pytest.mark.parametrize("arch", ["../../evil", "AMD64", "-x"])
def test_component_insert_pkg_refuses_an_arch_that_is_not_a_token(arch: str) -> None:
    """The arch names the `binary-<arch>/` index path, so the repo layer refuses it too."""
    comp = _component()

    with pytest.raises(ComponentException, match="invalid architecture"):
        comp.insert_pkg(arch, make_package("pool/main/f/foo.deb"))

    assert arch not in comp.packages


def test_component_load_refuses_a_release_without_an_architecture_field() -> None:
    comp = Component(make_storage_mock(), make_params(), MagicMock(spec=Release), "main")
    with patch("debkit.repo.component.Packages") as packages_cls:
        packages = MagicMock()
        packages.release.is_empty.return_value = False
        packages.release.fields = {"Origin": "x"}
        packages_cls.return_value = packages

        with pytest.raises(ComponentException, match="no 'Architecture' field"):
            comp.load(["amd64"])


def _component_with(params: RepoParams, slots: dict[str, Packages]) -> Component:
    component = Component(make_storage_mock(), params, Release(make_storage_mock(), params), "/")
    component.packages = slots
    return component


def _index_of(*pkgs: tuple[str, str | None]) -> Packages:
    """A container holding one package per (filename, Architecture or None)."""
    packages = Packages(make_storage_mock(), make_params())
    packages.packages = {
        name: make_package(name, **({"Architecture": arch} if arch else {})) for name, arch in pkgs
    }
    return packages


def test_containers_lists_a_container_shared_by_flat_slots_once() -> None:
    shared, other = _index_of(("a.deb", "amd64")), _index_of(("s.dsc", None))
    component = _component_with(make_params(flat=True), {"amd64": shared, "arm64": shared})
    component.packages["source"] = other

    assert component.containers() == [shared, other]


def test_containers_keeps_distinct_componented_containers() -> None:
    amd64, arm64 = _index_of(("a.deb", "amd64")), _index_of(("b.deb", "arm64"))
    component = _component_with(make_params(), {"amd64": amd64, "arm64": arm64})

    assert component.containers() == [amd64, arm64]


@pytest.mark.parametrize("selected", [["amd64"], ["amd64", "arm64"]], ids=["one-slot", "two-slots"])
def test_slot_packages_reports_a_flat_slot_by_its_arch(selected: list[str]) -> None:
    """The flat index holds every arch whichever are selected; a slot reports its own arch,
    `all`, and a package naming none."""
    index = _index_of(("a.deb", "amd64"), ("b.deb", "arm64"), ("c.deb", "all"), ("d.deb", None))
    component = _component_with(make_params(flat=True), dict.fromkeys(selected, index))

    assert set(component.slot_packages("amd64")) == {"a.deb", "c.deb", "d.deb"}


def test_slot_packages_reports_the_empty_flat_arch_and_a_componented_slot_whole() -> None:
    index = _index_of(("a.deb", "amd64"), ("b.deb", "arm64"))

    assert set(_component_with(make_params(flat=True), {"": index}).slot_packages("")) == {
        "a.deb",
        "b.deb",
    }
    assert set(_component_with(make_params(), {"amd64": index}).slot_packages("amd64")) == {
        "a.deb",
        "b.deb",
    }
