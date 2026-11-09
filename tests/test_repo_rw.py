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
from helpers import trees
from helpers.factories import make_package, make_params, make_storage_mock
from helpers.trees import payload_path, write_dist
from txstore import Storage, StorageException, StorageFS

from debkit.repo import _listed_slots, shared_root_claims
from debkit.repo.component import Component
from debkit.repo.params import RepoParams
from debkit.repo.release import RootRelease
from debkit.repo.repo_rw import CheckReport, RepoRW


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


def _removed_foo(tmp_path: Path) -> RepoRW:
    """Load bookworm from the tree under `tmp_path` and remove its `foo`."""
    repo = RepoRW(
        StorageFS(str(tmp_path)), RepoParams("bookworm", ["main"], ["amd64"], verify_gpg=False)
    )
    repo.update()
    assert repo.remove_pkg({"Package": re.compile("^foo$")}) is True
    assert repo.components["main"].packages["amd64"].packages == {}
    assert repo.modified is True
    return repo


@pytest.mark.parametrize(
    ("bullseye", "kept"),
    [({"foo": "2", "bar": "1"}, True), ({"bar": "1"}, False)],
    ids=["shared", "own"],
)
def test_remove_pkg_keeps_a_payload_another_dist_lists(
    tmp_path: Path, bullseye: dict[str, str], kept: bool
) -> None:
    write_dist(tmp_path, "bookworm", {"foo": "2"})
    write_dist(tmp_path, "bullseye", bullseye)

    _removed_foo(tmp_path)

    assert (tmp_path / payload_path("foo", "2")).exists() is kept


def test_remove_pkg_keeps_every_payload_while_another_dist_does_not_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_dist(tmp_path, "bookworm", {"foo": "2"})
    write_dist(tmp_path, "bullseye", {"bar": "1"})
    (tmp_path / "dists/bullseye/main/binary-amd64/Packages.gz").unlink()

    _removed_foo(tmp_path)

    assert (tmp_path / payload_path("foo", "2")).exists()
    assert "Dist bullseye shares the target root but did not load" in caplog.text


def test_shared_root_of_a_flat_layout_lists_nothing() -> None:
    storage = make_storage_mock()
    shared = RepoRW(storage, make_params(flat=True)).shared_root()

    assert shared.complete
    assert shared.deletable("pool/main/f/foo/foo_1_amd64.deb")
    storage.list.assert_not_called()


def test_shared_root_of_a_storage_that_cannot_list_keeps_all_but_the_own_tree() -> None:
    repo = RepoRW(make_storage_mock(list_raises=NotImplementedError), make_params())

    shared = repo.shared_root()

    assert not shared.complete
    assert not shared.deletable("pool/main/f/foo/foo_1_amd64.deb")
    assert shared.deletable("dists/bookworm/main/binary-amd64/Packages.gz")


def test_held_digests_reads_what_the_storage_holds(tmp_path: Path) -> None:
    repo = RepoRW(StorageFS(str(tmp_path)), make_params())
    (tmp_path / "held.deb").write_bytes(b"deb")

    assert repo.held_digests("absent.deb") is None
    held = repo.held_digests("held.deb")
    assert held is not None
    assert held["SHA256"] == hashlib.sha256(b"deb").hexdigest()


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


# ---------------------------------------------------------------------------
# check() — the repository against its own indices, on a real tree
# ---------------------------------------------------------------------------


class _FaultyFS(StorageFS):
    """`StorageFS` whose reads or deletes of chosen paths fail, recording metadata writes."""

    def __init__(self, base_dir: str, *, unreadable: str = "", undeletable: str = "") -> None:
        super().__init__(base_dir)
        self.unreadable = unreadable
        self.undeletable = undeletable
        self.metadata_writes: list[tuple[str, dict[str, str], str | None]] = []

    def transfer(
        self,
        path_src: str,
        storage_dst: Storage,
        path_dst: str | None = None,
        acl: str | None = None,
    ) -> dict[str, str]:
        if path_src == self.unreadable:
            raise StorageException(f"backend down reading {path_src}")
        return super().transfer(path_src, storage_dst, path_dst, acl)

    def delete(self, path: str) -> None:
        if path == self.undeletable:
            raise StorageException(f"denied deleting {path}")
        super().delete(path)

    def add_metadata(self, path: str, metadata: dict[str, str], acl: str | None = None) -> None:
        self.metadata_writes.append((path, metadata, acl))


_FOO = payload_path("foo", "1")
_BAR = payload_path("bar", "1")


def _checked(
    tmp_path: Path, storage: StorageFS | None = None, *, verify_digests: bool = True
) -> RepoRW:
    """A loaded repo over a real two-package tree."""
    write_dist(tmp_path, packages={"foo": "1", "bar": "1"})
    params = RepoParams("bookworm", ["main"], ["amd64"], verify_gpg=False)
    params.verify_digests = verify_digests
    repo = RepoRW(storage or StorageFS(str(tmp_path)), params)
    repo.update()
    return repo


def test_check_finds_a_clean_tree_clean(tmp_path: Path) -> None:
    assert _checked(tmp_path).check() == CheckReport([], [], 0, 0)


@pytest.mark.parametrize("fix", [False, True])
def test_check_counts_a_corrupt_payload_and_deletes_it_only_with_fix(
    tmp_path: Path, fix: bool
) -> None:
    repo = _checked(tmp_path)
    (tmp_path / _FOO).write_bytes(b"corrupt")

    assert repo.check(fix=fix) == CheckReport([], [], 1, 1 if fix else 0)
    assert (tmp_path / _FOO).exists() is not fix
    assert (tmp_path / _BAR).exists()


def test_check_fix_keeps_a_corrupt_payload_whose_bytes_another_dist_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bytes this dist calls corrupt are the ones the other dist's index records."""
    repo = _checked(tmp_path)
    monkeypatch.setattr(trees, "payload_bytes", lambda _version: b"trixie build")
    write_dist(tmp_path, "trixie", {"foo": "1"})

    assert repo.check(fix=True) == CheckReport([], [], 1, 0)
    assert (tmp_path / _FOO).read_bytes() == b"trixie build"
    assert f"Keep: {_FOO}" in caplog.text


def test_check_fix_deletes_a_corrupt_payload_no_other_dist_records(tmp_path: Path) -> None:
    """Bytes every index calls corrupt are deleted, so a later sync or insert places them."""
    repo = _checked(tmp_path)
    write_dist(tmp_path, "trixie", {"foo": "1"})
    (tmp_path / _FOO).write_bytes(b"corrupt")

    assert repo.check(fix=True) == CheckReport([], [], 1, 1)
    assert not (tmp_path / _FOO).exists()


def test_check_fix_keeps_a_corrupt_payload_while_another_dist_does_not_load(
    tmp_path: Path,
) -> None:
    repo = _checked(tmp_path)
    write_dist(tmp_path, "trixie", {"foo": "1"})
    (tmp_path / "dists/trixie/main/binary-amd64/Packages.gz").unlink()
    (tmp_path / _FOO).write_bytes(b"corrupt")

    assert repo.check(fix=True) == CheckReport([], [], 1, 0)
    assert (tmp_path / _FOO).exists()


def test_check_fix_keeps_a_corrupt_payload_a_second_other_dist_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sid` loads before `trixie`, so a record the later dist replaced would delete it."""
    repo = _checked(tmp_path)
    write_dist(tmp_path, "trixie", {"foo": "1"})
    monkeypatch.setattr(trees, "payload_bytes", lambda _version: b"sid build")
    write_dist(tmp_path, "sid", {"foo": "1"})

    assert repo.check(fix=True) == CheckReport([], [], 1, 0)
    assert (tmp_path / _FOO).read_bytes() == b"sid build"


def _record(data: bytes, alg: str) -> str:
    """A `Packages` stanza field recording `data` under `alg`, or no digest when `alg` is empty."""
    if alg == "SHA256":
        return f"SHA256: {hashlib.sha256(data).hexdigest()}\n"
    if alg == "MD5sum":
        return f"MD5sum: {hashlib.md5(data).hexdigest()}\n"
    return ""


@pytest.mark.parametrize(
    ("main_bytes", "contrib_bytes", "contrib_alg"),
    [
        (b"deb 1", b"corrupt", "SHA256"),
        (b"corrupt", b"deb 1", "SHA256"),
        (b"deb 1", b"corrupt", ""),
        (b"deb 1", b"corrupt", "MD5sum"),
    ],
    ids=["second-index-matches", "first-index-matches", "hashless", "md5-only"],
)
def test_check_fix_keeps_a_corrupt_payload_any_index_of_another_dist_vouches_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    main_bytes: bytes,
    contrib_bytes: bytes,
    contrib_alg: str,
) -> None:
    """Every index's record counts, and one without a SHA digest cannot clear the bytes."""
    repo = _checked(tmp_path)
    contrib = (
        f"Package: foo\nVersion: 1\nArchitecture: amd64\nFilename: {_FOO}\n"
        f"Size: {len(contrib_bytes)}\n{_record(contrib_bytes, contrib_alg)}"
    ).encode()
    monkeypatch.setattr(trees, "payload_bytes", lambda _version: main_bytes)
    write_dist(tmp_path, "trixie", {"foo": "1"}, indices={"contrib/binary-amd64/Packages": contrib})
    (tmp_path / _FOO).write_bytes(b"corrupt")

    assert repo.check(fix=True) == CheckReport([], [], 1, 0)
    assert (tmp_path / _FOO).exists()


def test_shared_root_claims_keep_digests_only_when_asked(tmp_path: Path) -> None:
    """Sync and `remove` only ask whether a path is claimed, so they hold no digests."""
    write_dist(tmp_path, "bookworm", {"bar": "1"})
    write_dist(tmp_path, "trixie", {"foo": "1"})
    storage = StorageFS(str(tmp_path))
    listing = set(storage.list())

    assert shared_root_claims(storage, make_params(), listing).recorded is None
    recorded = shared_root_claims(storage, make_params(), listing, with_digests=True).recorded
    assert recorded is not None
    assert list(recorded) == [_FOO]
    assert recorded[_FOO][0]["SHA256"] == hashlib.sha256(b"deb 1").hexdigest()


@pytest.mark.parametrize("component", ["main", "updates/main"])
def test_check_fix_keeps_a_payload_another_dist_lists_under_any_component(
    tmp_path: Path, component: str
) -> None:
    """Regression: an `updates/main` neighbour's indices went unread, so its payload was purged."""
    write_dist(tmp_path, "bookworm", {"bar": "1"}, component=component)
    write_dist(tmp_path, "trixie", {"foo": "1"})
    neighbour = tmp_path / payload_path("bar", "1", component)
    repo = RepoRW(
        StorageFS(str(tmp_path)), RepoParams("trixie", ["main"], ["amd64"], verify_gpg=False)
    )
    repo.update()

    repo.check(fix=True)

    assert neighbour.exists()


@pytest.mark.parametrize(
    ("components", "key", "slot"),
    [
        ("main", "main/binary-amd64/Packages.gz", ("main", "amd64")),
        ("updates/main", "updates/main/binary-arm64/Packages.xz", ("updates/main", "arm64")),
        ("main updates/main", "updates/main/source/Sources.gz", ("updates/main", "source")),
        (None, "updates/main/binary-amd64/Packages", ("updates/main", "amd64")),
        (
            "main",
            "main/debian-installer/binary-amd64/Packages.gz",
            ("main/debian-installer", "amd64"),
        ),
        ("main", "contrib/binary-amd64/Packages", ("contrib", "amd64")),
        ("main", "main/i18n/Translation-en.xz", None),
    ],
    ids=[
        "plain",
        "slashed",
        "longest-first",
        "no-components-field",
        "udeb",
        "undeclared",
        "not-an-index",
    ],
)
def test_listed_slots_reads_index_keys_below_each_component(
    components: str | None, key: str, slot: tuple[str, str] | None
) -> None:
    """The index-key parser agrees with the path builder for every component name."""
    release = RootRelease(make_storage_mock(), make_params())
    release.fields = {} if components is None else {"Components": components}
    release.digests = {key: {"SHA256": "x"}}

    slots = _listed_slots(release)

    assert slots == ({} if slot is None else {slot[0]: {slot[1]}})


def test_corrupt_deletable_keeps_a_claimed_payload_without_collected_digests(
    tmp_path: Path,
) -> None:
    """Without digests nothing can clear the bytes, so a claimed path is never deletable."""
    write_dist(tmp_path, "bookworm", {"bar": "1"})
    write_dist(tmp_path, "trixie", {"foo": "1"})
    storage = StorageFS(str(tmp_path))
    shared = shared_root_claims(storage, make_params(), set(storage.list()))

    assert not shared.corrupt_deletable(_FOO, {"SHA256": hashlib.sha256(b"deb 1").hexdigest()})


def test_check_reports_a_missing_payload_without_reading_it(tmp_path: Path) -> None:
    storage = _FaultyFS(str(tmp_path), unreadable=_FOO)
    repo = _checked(tmp_path, storage)
    (tmp_path / _FOO).unlink()

    assert repo.check() == CheckReport([_FOO], [], 0, 0)


@pytest.mark.parametrize(
    ("fix", "purge_orphans", "deleted"),
    [(False, True, False), (True, True, True), (True, False, False)],
    ids=["check", "check-fix", "verify-existing"],
)
def test_check_deletes_orphans_from_its_own_storage_only_with_fix(
    tmp_path: Path, fix: bool, purge_orphans: bool, deleted: bool
) -> None:
    repo = _checked(tmp_path)
    orphan = tmp_path / "pool/main/z/zz/zz_1_amd64.deb"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    key = orphan.relative_to(tmp_path).as_posix()

    report = repo.check(fix=fix, purge_orphans=purge_orphans)

    assert report == CheckReport([], [key], 0, 1 if deleted else 0)
    assert orphan.exists() is not deleted


def test_check_logs_an_orphan_it_cannot_delete_and_counts_it_unfixed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = "pool/main/z/zz/zz_1_amd64.deb"
    repo = _checked(tmp_path, _FaultyFS(str(tmp_path), undeletable=key))
    (tmp_path / key).parent.mkdir(parents=True)
    (tmp_path / key).write_bytes(b"orphan")

    assert repo.check(fix=True).fixed == 0
    assert f"Error deleting file: {key}" in caplog.text


def test_check_counts_a_payload_it_cannot_read_as_failed(tmp_path: Path) -> None:
    repo = _checked(tmp_path, _FaultyFS(str(tmp_path), unreadable=_FOO))

    assert repo.check(fix=True) == CheckReport([], [], 1, 0)
    assert (tmp_path / _FOO).exists()


def test_check_without_digest_verification_accepts_any_payload(tmp_path: Path) -> None:
    repo = _checked(tmp_path, verify_digests=False)
    (tmp_path / _FOO).write_bytes(b"corrupt")

    assert repo.check(fix=True) == CheckReport([], [], 0, 0)


def test_check_fix_records_each_verified_digest_as_metadata(tmp_path: Path) -> None:
    """The ACL is the storage's own default, so none is passed with the metadata."""
    storage = _FaultyFS(str(tmp_path))
    repo = _checked(tmp_path, storage)

    repo.check(fix=True)

    assert sorted(path for path, _, _ in storage.metadata_writes) == sorted([_FOO, _BAR])
    assert all(digests["SHA256"] and acl is None for _, digests, acl in storage.metadata_writes)
