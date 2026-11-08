"""Unit tests for ``Packages`` (TESTING.md § 7b).

Covers: parse / modify-rules, file discovery, checksum verification, load/save
round-trips, insert/remove (including security guards), diff, and helpers.
Storage is mocked; no real backend or .deb payloads.
"""

from __future__ import annotations

import gzip
import logging
import re
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from helpers.factories import make_package, make_packages_block, make_params, make_storage_mock
from txstore import StorageFS, calc_digests

from debkit.repo.hashes import ChecksumException
from debkit.repo.packages import MAX_INDICES_BYTES, Packages, PackagesException
from debkit.repo.params import RepoParams
from debkit.repo.release import Release


def _new_packages(storage: MagicMock | None = None, params: RepoParams | None = None) -> Packages:
    return Packages(storage or make_storage_mock(), params or make_params())


def _storage(pkgs: Packages) -> MagicMock:
    """Return the storage mock that `_new_packages` attached."""
    return cast(MagicMock, pkgs.storage)


# ---------------------------------------------------------------------------
# _parse_indices
# ---------------------------------------------------------------------------


def test_packages_parse_indices_round_trip_no_modify() -> None:
    pkgs = _new_packages()
    data = make_packages_block("foo") + "\n" + make_packages_block("bar")
    n = pkgs._parse_indices(data)
    assert n == 2
    assert len(pkgs) == 2
    assert pkgs.modified is False


def test_packages_parse_indices_rule_no_match_leaves_modified_false() -> None:
    pkgs = _new_packages()
    pkgs._parse_indices(make_packages_block("foo"), modify_rules=[("Package", "nope", "x")])
    assert pkgs.modified is False


def test_packages_parse_indices_rule_match_sets_modified() -> None:
    pkgs = _new_packages()
    pkgs._parse_indices(make_packages_block("foo"), modify_rules=[("Package", "foo", "FOO")])
    assert pkgs.modified is True
    [pkg] = pkgs.packages.values()
    assert pkg.fields["Package"] == "FOO"


def test_packages_parse_indices_trailing_blank_block_does_not_stop() -> None:
    pkgs = _new_packages()
    data = make_packages_block("foo") + "\n" + make_packages_block("bar") + "\n\n"
    pkgs._parse_indices(data)
    assert len(pkgs) == 2


@pytest.mark.parametrize(
    "filename",
    [
        "%2e%2e/%2e%2e/outside/evil.deb",  # urljoin leaves the dots encoded
        "..%2f..%2foutside/evil.deb",  # encoded separators
        "../outside/evil.deb",  # plain traversal
        "/etc/passwd",  # rooted key
        "http://evil/x.deb",  # scheme-form key
    ],
)
def test_packages_parse_indices_drops_an_unsafe_filename(
    filename: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A Filename is remote metadata, so a hostile index can name a key outside the root.

    The stanza is dropped rather than failing the load, so one bad record in a 60k-package
    index cannot stop a mirror.
    """
    pkgs = _new_packages()
    with caplog.at_level(logging.WARNING):
        n = pkgs._parse_indices(make_packages_block("evil", filename=filename))

    assert n == 0
    assert pkgs.packages == {}
    assert "unsafe package path" in caplog.text


def test_packages_parse_indices_keeps_a_legitimate_filename() -> None:
    """A pool key with an encoded epoch plus is legitimate and must survive."""
    pkgs = _new_packages()
    filename = "pool/main/f/foo/foo_1.0%2Bdeb10u1_amd64.deb"
    n = pkgs._parse_indices(make_packages_block("foo", filename=filename))

    assert n == 1
    assert filename in pkgs.packages


def test_packages_parse_indices_drops_only_the_unsafe_stanza() -> None:
    """One poisoned record must not remove the honest ones around it."""
    pkgs = _new_packages()
    data = "\n\n".join(
        [
            make_packages_block("good1"),
            make_packages_block("evil", filename="../outside/evil.deb"),
            make_packages_block("good2"),
        ]
    )
    n = pkgs._parse_indices(data)

    assert n == 2
    assert not any("outside" in key for key in pkgs.packages)


def test_packages_parse_indices_rule_missing_field_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pkgs = _new_packages()
    with caplog.at_level(logging.WARNING):
        pkgs._parse_indices(make_packages_block("foo"), modify_rules=[("Missing", ".*", "x")])
    assert pkgs.modified is False
    assert any("Modify rule failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _find_packages_file
# ---------------------------------------------------------------------------


def test_packages_find_packages_file_prefers_xz() -> None:
    pkgs = _new_packages()
    root = MagicMock(spec=Release)
    root.digests = {
        "main/binary-amd64/Packages.xz": {},
        "main/binary-amd64/Packages.gz": {},
    }
    _storage(pkgs).exists.return_value = True
    path, _fun = pkgs._find_packages_file(root, "main", "amd64")
    assert path == "dists/bookworm/main/binary-amd64/Packages.xz"
    assert "dists/bookworm/main/binary-amd64/Packages.gz" in pkgs.found_indices_paths


def test_packages_find_packages_file_reads_the_uncompressed_index_last() -> None:
    """A Release listing only the plain index is readable; a compressed form wins over it."""
    pkgs = _new_packages()
    root = MagicMock(spec=Release)
    root.digests = {"main/binary-amd64/Packages": {}}
    _storage(pkgs).exists.return_value = True
    path, fun = pkgs._find_packages_file(root, "main", "amd64")
    assert (path, fun(b"raw")) == ("dists/bookworm/main/binary-amd64/Packages", b"raw")

    pkgs = _new_packages()
    root.digests["main/binary-amd64/Packages.gz"] = {}
    _storage(pkgs).exists.return_value = True
    path, _fun = pkgs._find_packages_file(root, "main", "amd64")
    assert path == "dists/bookworm/main/binary-amd64/Packages.gz"
    assert pkgs.found_indices_paths == [path, "dists/bookworm/main/binary-amd64/Packages"]


def test_packages_find_packages_file_raises_when_release_empty() -> None:
    pkgs = _new_packages()
    root = MagicMock(spec=Release)
    root.digests = {}
    with pytest.raises(PackagesException, match="missing indices file"):
        pkgs._find_packages_file(root, "main", "amd64")


def test_packages_find_packages_file_flat_real_dist_uses_dist_prefix() -> None:
    """Regression for the ``'dist'`` typo: flat real-dist path must be under ``<dist>/``."""
    pkgs = _new_packages(params=make_params(flat=True, dist="stable"))
    root = MagicMock(spec=Release)
    root.digests = {"Packages.xz": {}}
    _storage(pkgs).exists.return_value = True
    path, _ = pkgs._find_packages_file(root, comp="", arch="amd64")
    assert path == "stable/Packages.xz"


def test_packages_find_packages_file_source_arch_looks_under_source_dir() -> None:
    """The source architecture indexes `Sources` under `source/`, not `binary-source/`."""
    pkgs = _new_packages()
    root = MagicMock(spec=Release)
    root.digests = {"main/source/Sources.xz": {}}
    _storage(pkgs).exists.return_value = True
    path, _ = pkgs._find_packages_file(root, comp="main", arch="source")
    assert path == "dists/bookworm/main/source/Sources.xz"


def test_packages_find_packages_file_flat_source_arch_sits_at_the_dist_root() -> None:
    """A flat repo carries no architecture directory, for source as for binary."""
    pkgs = _new_packages(params=make_params(flat=True, dist="stable"))
    root = MagicMock(spec=Release)
    root.digests = {"Sources.xz": {}}
    _storage(pkgs).exists.return_value = True
    path, _ = pkgs._find_packages_file(root, comp="", arch="source")
    assert path == "stable/Sources.xz"


# ---------------------------------------------------------------------------
# _verify_indices_checksum
# ---------------------------------------------------------------------------


def test_packages_verify_indices_checksum_root_hit() -> None:
    pkgs = _new_packages()
    root = MagicMock(spec=Release)
    root.verify_digests.return_value = True
    with patch.object(pkgs.release, "verify_digests", return_value=False):
        assert (
            pkgs._verify_indices_checksum(root, "dists/bookworm/main/binary-amd64/Packages.xz", {})
            is True
        )


def test_packages_verify_indices_checksum_per_arch_hit() -> None:
    pkgs = _new_packages()
    root = MagicMock(spec=Release)
    root.verify_digests.return_value = False
    with patch.object(pkgs.release, "verify_digests", return_value=True):
        assert (
            pkgs._verify_indices_checksum(root, "dists/bookworm/main/binary-amd64/Packages.xz", {})
            is True
        )


def test_packages_verify_indices_checksum_both_miss() -> None:
    pkgs = _new_packages()
    root = MagicMock(spec=Release)
    root.verify_digests.return_value = False
    with patch.object(pkgs.release, "verify_digests", return_value=False):
        assert (
            pkgs._verify_indices_checksum(root, "dists/bookworm/main/binary-amd64/Packages.xz", {})
            is False
        )


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_packages_setup_non_flat_paths_under_binary_arch() -> None:
    pkgs = _new_packages()
    with patch.object(Release, "setup_component") as setup_component:
        pkgs.setup("main", "amd64")
    setup_component.assert_called_once_with("main", "amd64")
    assert pkgs.used_indices_path == "dists/bookworm/main/binary-amd64/Packages.gz"
    assert pkgs.found_indices_paths == [
        "dists/bookworm/main/binary-amd64/Packages",
        "dists/bookworm/main/binary-amd64/Packages.gz",
    ]


def test_packages_setup_flat_paths_at_dist_root() -> None:
    """Flat repo must write `Packages*` at the dist root, mirroring the read path.

    Regression for the read/write asymmetry: `_find_packages_file()` looks for
    `<dist>/Packages*` (no `binary-<arch>/`) in flat layout; `setup()` used to
    always prepend `binary-<arch>/`, so the just-written index was never
    re-discovered by the loader.
    """
    pkgs = _new_packages(params=make_params(flat=True))
    pkgs.setup("main", "amd64")
    assert pkgs.used_indices_path == "bookworm/Packages.gz"
    assert pkgs.found_indices_paths == ["bookworm/Packages", "bookworm/Packages.gz"]


def test_packages_setup_flat_writes_no_component_release() -> None:
    """A flat repo reads no per-component Release, so its empty arch must not set one up.

    Composed from the empty arch, that Release used the root Release path, and the root
    Release then listed its own digests.
    """
    pkgs = _new_packages(params=make_params(flat=True))
    pkgs.setup("main", "")
    pkgs.insert(make_package("pool/main/f/foo/foo_1.0_amd64.deb"))

    ret = pkgs.save()

    assert pkgs.release.filename is None
    assert ret is not None
    assert set(ret) == {"bookworm/Packages", "bookworm/Packages.gz"}


def test_packages_setup_source_arch_writes_under_source_dir() -> None:
    """The write path names the source index exactly as `_find_packages_file()` reads it."""
    pkgs = _new_packages()
    with patch.object(Release, "setup_component"):
        pkgs.setup("main", "source")
    assert pkgs.used_indices_path == "dists/bookworm/main/source/Sources.gz"
    assert pkgs.found_indices_paths == [
        "dists/bookworm/main/source/Sources",
        "dists/bookworm/main/source/Sources.gz",
    ]


def test_packages_setup_flat_source_arch_writes_at_the_dist_root() -> None:
    """A flat repo carries no architecture directory on the write path either."""
    pkgs = _new_packages(params=make_params(flat=True))
    pkgs.setup("main", "source")
    assert pkgs.used_indices_path == "bookworm/Sources.gz"
    assert pkgs.found_indices_paths == ["bookworm/Sources", "bookworm/Sources.gz"]


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def test_packages_load_empty_payload_with_matching_digest_returns_false() -> None:
    """Legitimately empty index (Release digest matches empty bytes) → False.

    Digest verification MUST run before the empty-payload short-circuit so
    that truncated / 0-byte objects against a non-empty Release digest fail
    loudly instead of being silently treated as "no packages for this arch".
    """
    p = make_params(verify_digests=True)
    pkgs = _new_packages(params=p)
    _storage(pkgs).get.return_value = b""
    with (
        patch.object(
            Packages,
            "_find_packages_file",
            return_value=("dists/bookworm/main/binary-amd64/Packages.xz", lambda b: b),
        ),
        patch("debkit.repo.packages.calc_digests") as cd,
        patch.object(Packages, "_verify_indices_checksum", return_value=True),
    ):
        root = MagicMock(spec=Release)
        assert pkgs.load(root, "main", "amd64") is False
        cd.assert_called_once_with(b"")


def test_packages_load_empty_payload_with_mismatched_digest_raises() -> None:
    """0-byte object against non-empty Release digest must raise ChecksumException."""
    p = make_params(verify_digests=True)
    pkgs = _new_packages(params=p)
    _storage(pkgs).get.return_value = b""
    with (
        patch.object(
            Packages,
            "_find_packages_file",
            return_value=("dists/bookworm/main/binary-amd64/Packages.xz", lambda b: b),
        ),
        patch.object(Packages, "_verify_indices_checksum", return_value=False),
    ):
        root = MagicMock(spec=Release)
        with pytest.raises(ChecksumException, match="Mismatch"):
            pkgs.load(root, "main", "amd64")


def test_packages_load_digest_mismatch_raises() -> None:
    p = make_params(verify_digests=True)
    pkgs = _new_packages(params=p)
    _storage(pkgs).get.return_value = b"hello"
    with (
        patch.object(
            Packages,
            "_find_packages_file",
            return_value=("dists/bookworm/main/binary-amd64/Packages", lambda b: b),
        ),
        patch.object(pkgs, "_verify_indices_checksum", return_value=False),
    ):
        root = MagicMock(spec=Release)
        with pytest.raises(ChecksumException):
            pkgs.load(root, "main", "amd64")


def test_packages_load_exceeds_size_cap_raises() -> None:
    pkgs = _new_packages()
    huge = b"x" * (MAX_INDICES_BYTES + 1)
    _storage(pkgs).get.return_value = b"compressed"
    with patch.object(
        Packages,
        "_find_packages_file",
        return_value=("p", lambda _b: huge),
    ):
        root = MagicMock(spec=Release)
        with pytest.raises(PackagesException, match="soft cap"):
            pkgs.load(root, "main", "amd64")


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_packages_save_not_modified_returns_none() -> None:
    pkgs = _new_packages()
    assert pkgs.save() is None
    _storage(pkgs).put.assert_not_called()


def test_packages_save_writes_each_indices_and_release_once() -> None:
    pkgs = _new_packages()
    pkgs.packages = {
        "pool/main/f/foo/foo_1.0_amd64.deb": make_package(
            "pool/main/f/foo/foo_1.0_amd64.deb", size=1, SHA256="aa"
        ),
    }
    pkgs.modified = True
    pkgs.found_indices_paths = [
        "dists/bookworm/main/binary-amd64/Packages",
        "dists/bookworm/main/binary-amd64/Packages.gz",
    ]
    pkgs.release = MagicMock(spec=Release)
    pkgs.release.filename = "dists/bookworm/main/binary-amd64/Release"
    pkgs.release.save.return_value = {"SHA256": "rr"}

    ret = pkgs.save()

    assert _storage(pkgs).put.call_count == 2
    pkgs.release.save.assert_called_once()  # regression: was called once per file
    assert ret is not None
    assert "dists/bookworm/main/binary-amd64/Release" in ret


def test_packages_save_rewrites_the_uncompressed_index_the_release_lists(tmp_path: Path) -> None:
    """A plain `Packages` the Release lists is rewritten with its compressed sibling.

    Skipping it left a stale index whose Release digest no longer matched once the
    component Release was rewritten.
    """
    storage = StorageFS(str(tmp_path))
    params = make_params(verify_digests=True)
    plain = make_packages_block("foo").encode()
    root = Release(storage, params)
    for suffix, data in (("", plain), (".gz", gzip.compress(plain))):
        storage.put(f"dists/bookworm/main/binary-amd64/Packages{suffix}", data)
        root.update_digests(f"main/binary-amd64/Packages{suffix}", calc_digests(data))
    pkgs = Packages(storage, params)
    assert pkgs.load(root, "main", "amd64") is True

    pkgs.insert(make_package("pool/main/b/bar/bar_1.0_amd64.deb", SHA256="aa"))
    ret = pkgs.save()

    plain_path = "dists/bookworm/main/binary-amd64/Packages"
    assert ret is not None
    assert set(ret) == {plain_path, f"{plain_path}.gz"}
    written = storage.get(plain_path)
    assert gzip.decompress(storage.get(f"{plain_path}.gz")) == written
    assert b"bar_1.0_amd64.deb" in written
    assert ret[plain_path] == calc_digests(written)


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------


def test_packages_insert_sets_modified_and_indexes_by_filename() -> None:
    pkgs = _new_packages()
    pkg = make_package("pool/main/x/xyz_1.0.deb")
    pkgs.insert(pkg)
    assert pkgs.modified is True
    assert "pool/main/x/xyz_1.0.deb" in pkgs.packages


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_packages_remove_all_fields_match() -> None:
    pkgs = _new_packages()
    pkgs.packages = {
        "pool/foo.deb": make_package("pool/foo.deb", Package="foo"),
        "pool/bar.deb": make_package("pool/bar.deb", Package="bar"),
    }
    assert pkgs.remove({"Package": re.compile("^foo$")}) == {"pool/foo.deb"}
    _storage(pkgs).delete.assert_not_called()
    assert "pool/foo.deb" not in pkgs.packages
    assert "pool/bar.deb" in pkgs.packages
    assert pkgs.modified is True


def test_packages_remove_rejects_traversal_filename() -> None:
    pkgs = _new_packages()
    pkgs.packages = {"../../etc/passwd": make_package("../../etc/passwd", Package="evil")}
    with pytest.raises(PackagesException, match="unsafe filename"):
        pkgs.remove({"Package": re.compile("evil")})
    assert "../../etc/passwd" in pkgs.packages


def test_packages_remove_rejects_leading_slash() -> None:
    pkgs = _new_packages()
    pkgs.packages = {"/etc/passwd": make_package("/etc/passwd", Package="evil")}
    with pytest.raises(PackagesException, match="unsafe filename"):
        pkgs.remove({"Package": re.compile("evil")})


def test_packages_remove_no_match_returns_false() -> None:
    pkgs = _new_packages()
    pkgs.packages = {"pool/foo.deb": make_package("pool/foo.deb", Package="foo")}
    assert pkgs.remove({"Package": re.compile("nope")}) == set()
    assert pkgs.modified is False


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_packages_diff_added_removed_updated_old_versions() -> None:
    s = make_storage_mock()
    p = make_params()
    src = Packages(s, p)
    src.packages = {
        "a.deb": make_package("a.deb", size=1),
        "b.deb": make_package("b.deb", size=1),
    }
    tgt = Packages(s, p)
    tgt.packages = {
        "a.deb": make_package("a.deb", size=2),  # updated (different Size)
        "c.deb": make_package("c.deb", size=1),  # added in target
    }
    added, removed, updated, old = src.diff(tgt)
    assert set(added) == {"c.deb"}
    assert set(removed) == {"b.deb"}
    assert set(updated) == {"a.deb"}
    assert set(old) == {"a.deb"}


# ---------------------------------------------------------------------------
# verify_checksum / get_indices_paths / is_empty / __len__
# ---------------------------------------------------------------------------


def test_packages_verify_checksum_match_and_miss() -> None:
    pkgs = _new_packages()
    pkgs.packages = {
        "pool/foo.deb": make_package("pool/foo.deb", SHA256="aa", MD5Sum="bb"),
    }
    assert pkgs.verify_checksum("pool/foo.deb", {"SHA256": "aa"}) is True
    assert pkgs.verify_checksum("pool/foo.deb", {"SHA256": "zz"}) is False
    assert pkgs.verify_checksum("pool/missing.deb", {"SHA256": "aa"}) is False


def test_packages_verify_checksum_finds_a_package_whose_key_carries_a_pool_path() -> None:
    """The lookup key is the `Filename` field verbatim, pool prefix included."""
    pkgs = _new_packages()
    pool_key = "pool/main/h/hello/hello_2.10-3_amd64.deb"
    pkgs.packages = {pool_key: make_package(pool_key, SHA256="aa")}
    assert pkgs.verify_checksum(pool_key, {"SHA256": "aa"}) is True
    assert pkgs.verify_checksum("hello_2.10-3_amd64.deb", {"SHA256": "aa"}) is False


def test_packages_get_indices_paths_concatenates() -> None:
    pkgs = _new_packages()
    pkgs.found_indices_paths = ["A"]
    pkgs.release = MagicMock(spec=Release)
    pkgs.release.get_indices_paths.return_value = ["B"]
    assert pkgs.get_indices_paths() == ["A", "B"]


def test_packages_is_empty_and_len() -> None:
    pkgs = _new_packages()
    assert pkgs.is_empty() is True
    assert len(pkgs) == 0
    pkgs.packages = {"x.deb": make_package("x.deb")}
    assert pkgs.is_empty() is False
    assert len(pkgs) == 1
