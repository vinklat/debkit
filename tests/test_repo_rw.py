"""Coverage for `debkit.repo.repo_rw`."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, call

import pytest
from helpers.factories import make_package, make_params, make_storage_mock
from txstore import StorageException, StorageFS

from debkit.repo.component import Component
from debkit.repo.params import RepoParams
from debkit.repo.repo_rw import RepoRW


def _repo() -> tuple[RepoRW, MagicMock]:
    storage = make_storage_mock()
    repo = RepoRW(storage, make_params())
    release = MagicMock(name="Release")
    release.fields = {}
    repo.release = release
    repo.components = {}
    return repo, release


def test_setup_marks_modified() -> None:
    repo, release = _repo()
    repo.setup({"Origin": "x"})
    release.setup.assert_called_once()
    assert repo.modified is True


def test_save_no_op_when_unmodified() -> None:
    assert _repo()[0].save() is False


def test_save_stamps_and_writes_the_release_after_a_component_saved() -> None:
    repo, release = _repo()
    saved, untouched = MagicMock(), MagicMock()
    saved.save.return_value = True
    untouched.save.return_value = False
    repo.components = cast(dict[str, Component], {"main": saved, "contrib": untouched})
    repo.modified = True

    assert repo.save() is True

    saved.save.assert_called_once_with()
    untouched.save.assert_called_once_with()
    assert release.fields["Components"] == "main contrib"
    assert parsedate_to_datetime(release.fields["Date"]).tzinfo == UTC
    release.save.assert_called_once_with()


def test_save_leaves_the_release_when_no_component_saved() -> None:
    repo, release = _repo()
    untouched = MagicMock()
    untouched.save.return_value = False
    repo.components = cast(dict[str, Component], {"main": untouched})
    repo.modified = True

    assert repo.save() is False

    assert release.fields == {}
    release.save.assert_not_called()


def test_insert_pkg_creates_missing_component() -> None:
    repo, _release = _repo()
    pkg = make_package("pool/main/f/foo/foo.deb")
    repo.insert_pkg("main", "amd64", pkg)
    assert "main" in repo.components
    assert repo.modified is True


def test_remove_pkg_returns_false_when_no_match() -> None:
    repo, _release = _repo()
    comp = MagicMock()
    comp.remove_pkg.return_value = set()
    repo.components = {"main": comp}
    assert repo.remove_pkg({"Package": re.compile("missing")}) is False
    assert repo.modified is False
    cast(MagicMock, repo.storage).delete.assert_not_called()


def test_remove_pkg_deletes_each_payload_once_and_marks_modified() -> None:
    repo, _release = _repo()
    main, contrib = MagicMock(), MagicMock()
    main.remove_pkg.return_value = {"pool/foo_all.deb"}
    contrib.remove_pkg.return_value = {"pool/foo_all.deb", "pool/bar.deb"}
    repo.components = {"main": main, "contrib": contrib}
    assert repo.remove_pkg({"Package": re.compile("foo")}) is True
    assert repo.modified is True
    assert cast(MagicMock, repo.storage).delete.call_args_list == [
        call("pool/bar.deb"),
        call("pool/foo_all.deb"),
    ]


def test_remove_pkg_propagates_a_delete_failure() -> None:
    repo, _release = _repo()
    comp = MagicMock()
    comp.remove_pkg.return_value = {"pool/foo.deb"}
    repo.components = {"main": comp}
    cast(MagicMock, repo.storage).delete.side_effect = StorageException("boom")
    with pytest.raises(StorageException, match="boom"):
        repo.remove_pkg({"Package": re.compile("foo")})


def test_transfer_from_does_not_pass_acl_kwarg() -> None:
    """ACL handling lives in storage layer; repo must not inject it."""
    repo, _release = _repo()
    src = make_storage_mock()
    src.transfer.return_value = {"SHA256": "deadbeef"}

    digests = repo.transfer_from(src, "incoming/foo.deb")

    assert digests == {"SHA256": "deadbeef"}
    _, kwargs = src.transfer.call_args
    assert "acl" not in kwargs
    assert kwargs.get("path_dst") is None


def test_reset_clears_state() -> None:
    repo, release = _repo()
    repo.components = cast(dict[str, Component], {"main": MagicMock()})
    repo.reset()
    assert repo.components == {}
    release.reset.assert_called_once()


def test_remove_pkg_of_an_arch_all_payload_on_a_real_storage(tmp_path: Path) -> None:
    """An `all` payload listed by two arch indices is deleted once, outside a transaction.

    Deleting it once per index would raise `StorageNotFoundException` on the second arch.
    """
    storage = StorageFS(str(tmp_path))
    payload = "pool/main/h/hello/hello_1.0_all.deb"
    storage.put(payload, b"deb")
    built = RepoRW(storage, make_params())
    built.setup({"Architectures": "amd64 arm64", "Components": "main"})
    for arch in ("amd64", "arm64"):
        built.insert_pkg("main", arch, make_package(payload, Package="hello", Architecture="all"))
    built.save()

    repo = RepoRW(storage, make_params())
    repo.update()
    assert not storage.in_transaction()
    assert repo.remove_pkg({"Package": re.compile("^hello$")}) is True

    assert not storage.exists(payload)
    assert {arch: pkgs.packages for arch, pkgs in repo.components["main"].packages.items()} == {
        "amd64": {},
        "arm64": {},
    }


_FLAT_INDEX = b"Package: upstream\nFilename: pool/u/upstream/upstream_1_amd64.deb\nSize: 1\n\n"


def _flat_repo(root: Path) -> RepoRW:
    params = RepoParams(verify_gpg=False, verify_digests=False)
    params.from_deb_source(f"fs://{root} /")
    repo = RepoRW(StorageFS(str(root)), params)
    repo.update()
    return repo


def test_insert_pkg_into_flat_repo_joins_its_single_component(tmp_path: Path) -> None:
    """Regression: `--component main` added a second slot over the same `Packages` file."""
    digest = hashlib.sha256(_FLAT_INDEX).hexdigest()
    (tmp_path / "Packages").write_bytes(_FLAT_INDEX)
    (tmp_path / "Release").write_text(
        f"Origin: x\nArchitectures: amd64\nSHA256:\n {digest} {len(_FLAT_INDEX)} Packages\n"
    )
    repo = _flat_repo(tmp_path)

    repo.insert_pkg("main", "amd64", make_package("pool/n/new/new_1_amd64.deb", Package="new"))
    repo.save()

    assert list(repo.components) == ["/"]
    index = (tmp_path / "Packages").read_text()
    assert "Package: upstream" in index
    assert "Package: new" in index
    assert "Components" not in (tmp_path / "Release").read_text()
    reloaded = _flat_repo(tmp_path)
    assert list(reloaded.components) == ["/"]
    assert reloaded.stats()["/"]["amd64"]["packages_total"] == 2


def test_setup_of_a_flat_repo_drops_the_components_field(tmp_path: Path) -> None:
    """Regression: a new flat repo kept `Components: main` from the insert defaults."""
    params = RepoParams(verify_gpg=False, verify_digests=False)
    params.from_deb_source(f"fs://{tmp_path} /")
    repo = RepoRW(StorageFS(str(tmp_path)), params)
    repo.setup(fields={"Architectures": "amd64", "Components": "main"})

    repo.insert_pkg("main", "amd64", make_package("pool/n/new/new_1_amd64.deb", Package="new"))
    repo.save()

    release = (tmp_path / "Release").read_text()
    assert "Architectures: amd64" in release
    assert "Components" not in release
    assert list(_flat_repo(tmp_path).components) == ["/"]


def _flat_packages(root: Path) -> list[str]:
    return re.findall(r"^Package: (\S+)$", (root / "Packages").read_text(), re.MULTILINE)


def test_insert_into_a_new_multi_arch_flat_repo_keeps_every_package(tmp_path: Path) -> None:
    """Regression: each arch slot saved the one flat `Packages` file and the last one won."""
    params = RepoParams(verify_gpg=False, verify_digests=False)
    params.from_deb_source(f"fs://{tmp_path} /")
    repo = RepoRW(StorageFS(str(tmp_path)), params)
    repo.setup(fields={"Architectures": "amd64 arm64 all"})

    repo.insert_pkg("main", "amd64", make_package("pool/a/aa/aa_1_amd64.deb", Package="aa"))
    for arch in repo.get_archs():
        repo.insert_pkg("main", arch, make_package("pool/c/cc/cc_1_all.deb", Package="cc"))
    repo.save()

    assert sorted(_flat_packages(tmp_path)) == ["aa", "cc"]
    reloaded = _flat_repo(tmp_path)
    assert {arch: s["packages_total"] for arch, s in reloaded.stats()["/"].items()} == {
        "amd64": 2,
        "arm64": 2,
    }


def test_insert_into_a_loaded_multi_arch_flat_repo_keeps_every_package(tmp_path: Path) -> None:
    digest = hashlib.sha256(_FLAT_INDEX).hexdigest()
    (tmp_path / "Packages").write_bytes(_FLAT_INDEX)
    (tmp_path / "Release").write_text(
        f"Origin: x\nArchitectures: amd64 arm64\nSHA256:\n {digest} {len(_FLAT_INDEX)} Packages\n"
    )
    repo = _flat_repo(tmp_path)

    repo.insert_pkg("main", "amd64", make_package("pool/a/aa/aa_1_amd64.deb", Package="aa"))
    for arch in ("amd64", "arm64"):
        repo.insert_pkg("main", arch, make_package("pool/c/cc/cc_1_all.deb", Package="cc"))
    repo.save()

    assert sorted(_flat_packages(tmp_path)) == ["aa", "cc", "upstream"]
