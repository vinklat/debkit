"""Tests for RepoDiff (§ 11 of TESTING.md).

Pure-logic class with no I/O; fed directly with name -> Package dicts.
"""

from __future__ import annotations

from helpers.factories import make_package

from debkit.repo.package import Package
from debkit.repo.repo_diff import RepoDiff


def _by_name(*pkgs: Package) -> dict[str, Package]:
    return {p.fields["Filename"]: p for p in pkgs}


def test_repo_diff_is_empty_on_fresh_instance() -> None:
    diff = RepoDiff()

    assert diff.is_empty() is True
    assert diff.count_pkgs_to_copy() == 0
    assert diff.count_pkgs_to_remove() == 0


def test_repo_diff_is_not_empty_after_single_update() -> None:
    diff = RepoDiff()
    added = _by_name(make_package("pool/main/a/a_1_amd64.deb"))

    diff.update("main", "amd64", added, {}, {}, {})

    assert diff.is_empty() is False
    dumped = diff.dump()
    assert "main" in dumped["added"]
    assert "amd64" in dumped["added"]["main"]


def test_repo_diff_update_separates_four_buckets_per_component_arch() -> None:
    diff = RepoDiff()
    added = _by_name(make_package("pool/main/a/a_1_amd64.deb"))
    removed = _by_name(make_package("pool/main/r/r_1_amd64.deb"))
    updated = _by_name(make_package("pool/main/u/u_2_amd64.deb"))
    old = _by_name(make_package("pool/main/u/u_1_amd64.deb"))

    diff.update("main", "amd64", added, removed, updated, old)
    dumped = diff.dump()

    assert [p.fields["Filename"] for p in dumped["added"]["main"]["amd64"]] == [
        "pool/main/a/a_1_amd64.deb"
    ]
    assert [p.fields["Filename"] for p in dumped["removed"]["main"]["amd64"]] == [
        "pool/main/r/r_1_amd64.deb"
    ]
    assert [p.fields["Filename"] for p in dumped["updated"]["main"]["amd64"]] == [
        "pool/main/u/u_2_amd64.deb"
    ]
    assert [p.fields["Filename"] for p in dumped["old_versions"]["main"]["amd64"]] == [
        "pool/main/u/u_1_amd64.deb"
    ]


def test_repo_diff_get_pkgs_to_copy_is_added_plus_updated_without_old_versions() -> None:
    diff = RepoDiff()
    added = make_package("pool/main/a/a_1_amd64.deb")
    upd = make_package("pool/main/u/u_2_amd64.deb")
    old = make_package("pool/main/u/u_1_amd64.deb")

    diff.update("main", "amd64", _by_name(added), {}, _by_name(upd), _by_name(old))

    copy_pkgs = diff.get_pkgs_to_copy()

    assert {p.fields["Filename"] for p in copy_pkgs} == {
        "pool/main/a/a_1_amd64.deb",
        "pool/main/u/u_2_amd64.deb",
    }
    assert diff.count_pkgs_to_copy() == len(copy_pkgs)


def test_repo_diff_get_filenames_to_copy_uses_package_repr() -> None:
    diff = RepoDiff()
    added = make_package("pool/main/a/a_1_amd64.deb")
    upd = make_package("pool/main/u/u_2_amd64.deb")

    diff.update("main", "amd64", _by_name(added), {}, _by_name(upd), {})

    assert diff.get_filenames_to_copy() == {
        "pool/main/a/a_1_amd64.deb",
        "pool/main/u/u_2_amd64.deb",
    }


def test_repo_diff_get_pkgs_to_remove_excludes_old_versions() -> None:
    diff = RepoDiff()
    removed = make_package("pool/main/r/r_1_amd64.deb")
    old = make_package("pool/main/u/u_1_amd64.deb")

    diff.update("main", "amd64", {}, _by_name(removed), {}, _by_name(old))

    remove_pkgs = diff.get_pkgs_to_remove()
    assert {p.fields["Filename"] for p in remove_pkgs} == {"pool/main/r/r_1_amd64.deb"}
    assert diff.get_filenames_to_remove() == {"pool/main/r/r_1_amd64.deb"}
    assert diff.count_pkgs_to_remove() == 1


def test_repo_diff_counts_an_arch_all_payload_once() -> None:
    """Two arch indices list one `all` payload; the brief output counts one package."""
    diff = RepoDiff()
    pkg = make_package("pool/main/h/hello_1_all.deb")
    for arch in ("amd64", "arm64"):
        diff.update("main", arch, _by_name(pkg), _by_name(pkg), {}, {})

    assert len(diff.get_pkgs_to_remove()) == 2
    assert diff.count_pkgs_to_copy() == 1
    assert diff.count_pkgs_to_remove() == 1


def test_repo_diff_two_components_coexist_in_dump() -> None:
    diff = RepoDiff()
    a_main = make_package("pool/main/a/a_1_amd64.deb")
    a_contrib = make_package("pool/contrib/b/b_1_amd64.deb")

    diff.update("main", "amd64", _by_name(a_main), {}, {}, {})
    diff.update("contrib", "amd64", _by_name(a_contrib), {}, {}, {})

    dumped = diff.dump()
    assert set(dumped["added"].keys()) == {"main", "contrib"}
    assert [p.fields["Filename"] for p in dumped["added"]["main"]["amd64"]] == [
        "pool/main/a/a_1_amd64.deb"
    ]
    assert [p.fields["Filename"] for p in dumped["added"]["contrib"]["amd64"]] == [
        "pool/contrib/b/b_1_amd64.deb"
    ]


def test_repo_diff_update_preserves_other_arch_in_same_component() -> None:
    """LOOP-H1 regression: second update() on same component for a different
    arch must NOT clobber the first arch."""
    diff = RepoDiff()
    amd_added = make_package("pool/main/a/a_1_amd64.deb")
    arm_added = make_package("pool/main/a/a_1_arm64.deb")
    amd_removed = make_package("pool/main/r/r_1_amd64.deb")
    arm_removed = make_package("pool/main/r/r_1_arm64.deb")
    amd_updated = make_package("pool/main/u/u_2_amd64.deb")
    arm_updated = make_package("pool/main/u/u_2_arm64.deb")
    amd_old = make_package("pool/main/u/u_1_amd64.deb")
    arm_old = make_package("pool/main/u/u_1_arm64.deb")

    diff.update(
        "main",
        "amd64",
        _by_name(amd_added),
        _by_name(amd_removed),
        _by_name(amd_updated),
        _by_name(amd_old),
    )
    diff.update(
        "main",
        "arm64",
        _by_name(arm_added),
        _by_name(arm_removed),
        _by_name(arm_updated),
        _by_name(arm_old),
    )

    dumped = diff.dump()
    for bucket in ("added", "removed", "updated", "old_versions"):
        assert set(dumped[bucket]["main"].keys()) == {"amd64", "arm64"}, bucket

    assert diff.get_filenames_to_copy() == {
        "pool/main/a/a_1_amd64.deb",
        "pool/main/a/a_1_arm64.deb",
        "pool/main/u/u_2_amd64.deb",
        "pool/main/u/u_2_arm64.deb",
    }
    assert diff.get_filenames_to_remove() == {
        "pool/main/r/r_1_amd64.deb",
        "pool/main/r/r_1_arm64.deb",
    }
