"""Coverage for `debkit.repo.repo_rw`."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from helpers.factories import make_package, make_params, make_storage_mock

from debkit.repo.component import Component
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


def test_insert_pkg_creates_missing_component() -> None:
    repo, _release = _repo()
    pkg = make_package("pool/main/f/foo/foo.deb")
    repo.insert_pkg("main", "amd64", pkg)
    assert "main" in repo.components
    assert repo.modified is True


def test_remove_pkg_returns_false_when_no_match() -> None:
    repo, _release = _repo()
    comp = MagicMock()
    comp.remove_pkg.return_value = False
    repo.components = {"main": comp}
    assert repo.remove_pkg({"Package": "missing"}) is False
    assert repo.modified is False


def test_remove_pkg_returns_true_and_marks_modified() -> None:
    repo, _release = _repo()
    comp = MagicMock()
    comp.remove_pkg.return_value = True
    repo.components = {"main": comp}
    assert repo.remove_pkg({"Package": "foo"}) is True
    assert repo.modified is True


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
