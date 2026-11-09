"""Coverage for `debkit.repo.changes`.

See ``TESTING.md`` § "Repo classes" for the matrix this file implements.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from helpers.factories import make_clearsigned
from txstore import StorageFS

from debkit.repo import changes as changes_module
from debkit.repo.changes import Changes, ChangesException
from debkit.repo.hashes import ChecksumException

_CHANGES_BLOB = (
    b"Format: 1.8\n"
    b"Architecture: amd64\n"
    b"Checksums-Sha256:\n"
    b" deadbeef 123 foo_1.0_amd64.deb\n"
    b" cafebabe 7 bar_1.0_all.deb\n"
)


def _storage_with(data: bytes) -> MagicMock:
    storage = MagicMock()
    storage.get.return_value = data
    return storage


def test_parse_extracts_sha256_and_size() -> None:
    changes = Changes(_storage_with(_CHANGES_BLOB))
    changes.parse(_CHANGES_BLOB)

    assert changes.digests["foo_1.0_amd64.deb"] == {"Size": "123", "SHA256": "deadbeef"}
    assert changes.digests["bar_1.0_all.deb"] == {"Size": "7", "SHA256": "cafebabe"}


def test_parse_without_sha256_raises() -> None:
    changes = Changes(_storage_with(b""))
    with pytest.raises(ChangesException):
        changes.parse(b"Format: 1.8\nArchitecture: amd64\n")


@pytest.mark.parametrize(
    "repeat",
    [
        b"Checksums-Sha256:\n bbbb 2 foo_1.0_amd64.deb\n",
        b"checksums-sha256:\n bbbb 2 foo_1.0_amd64.deb\n",
        b"Architecture: source\n",
    ],
    ids=["checksums", "checksums-other-case", "architecture"],
)
def test_parse_refuses_a_repeated_field(repeat: bytes) -> None:
    """dpkg refuses a repeated field; another reader keeping another copy sees other digests."""
    with pytest.raises(ChangesException, match="repeated field"):
        Changes(_storage_with(b"")).parse(_CHANGES_BLOB + repeat)


def test_get_archs_returns_every_token() -> None:
    blob = b"Format: 1.8\nArchitecture: amd64 all\nChecksums-Sha256:\n deadbeef 1 x.deb\n"
    changes = Changes(_storage_with(blob))
    changes.parse(blob)
    assert changes.get_archs() == ["amd64", "all"]


def test_get_archs_defaults_when_missing() -> None:
    blob = b"Format: 1.8\nChecksums-Sha256:\n deadbeef 1 x.deb\n"
    changes = Changes(_storage_with(blob))
    changes.parse(blob)
    assert changes.get_archs() == ["amd64"]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (b"source amd64", True),
        (b"amd64 source", True),
        (b"source", True),
        (b"amd64", False),
        (b"amd64 all", False),
    ],
)
def test_names_source_arch_reads_every_token(field: bytes, expected: bool) -> None:
    """`dpkg-genchanges` may write `source` after the binary arch, so the field order
    must not decide whether the upload names the source architecture.
    """
    blob = b"Format: 1.8\nArchitecture: " + field + b"\nChecksums-Sha256:\n deadbeef 1 x.deb\n"
    changes = Changes(_storage_with(blob))
    changes.parse(blob)
    assert changes.names_source_arch() is expected


def test_get_deb_files_filters_extension() -> None:
    changes = Changes(_storage_with(_CHANGES_BLOB))
    changes.parse(_CHANGES_BLOB)
    assert set(changes.get_deb_files()) == {"foo_1.0_amd64.deb", "bar_1.0_all.deb"}


def _stored(tmp_path: Path, data: bytes) -> StorageFS:
    """An `fs` storage holding *data* as `incoming.changes`."""
    (tmp_path / "incoming.changes").write_bytes(data)
    return StorageFS(str(tmp_path))


def test_load_empty_payload_raises(tmp_path: Path) -> None:
    changes = Changes(_stored(tmp_path, b""))
    with pytest.raises(ChangesException, match="Empty changes file"):
        changes.load("incoming.changes")


def test_load_refuses_a_file_past_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mistyped name or a misbehaving backend is refused before it is buffered in full."""
    monkeypatch.setattr(changes_module, "MAX_CHANGES_BYTES", len(_CHANGES_BLOB) - 1)
    with pytest.raises(ChangesException, match="larger than"):
        Changes(_stored(tmp_path, _CHANGES_BLOB)).load("incoming.changes")


def test_load_reads_a_file_at_the_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes_module, "MAX_CHANGES_BYTES", len(_CHANGES_BLOB))
    changes = Changes(_stored(tmp_path, _CHANGES_BLOB))
    changes.load("incoming.changes")
    assert "foo_1.0_amd64.deb" in changes.digests


def test_load_wraps_a_missing_file(tmp_path: Path) -> None:
    """A missing upload is a refused `.changes` (Read), as a missing `.deb` is."""
    with pytest.raises(ChangesException, match="Cannot read .changes file"):
        Changes(StorageFS(str(tmp_path))).load("incoming.changes")


def test_load_checksum_mismatch_raises_checksum_exc(tmp_path: Path) -> None:
    changes = Changes(_stored(tmp_path, _CHANGES_BLOB))
    with pytest.raises(ChecksumException):
        changes.load("incoming.changes", digest_expected="not-a-real-digest")


def test_load_matching_digest_parses(tmp_path: Path) -> None:
    digest = hashlib.sha256(_CHANGES_BLOB).hexdigest()
    changes = Changes(_stored(tmp_path, _CHANGES_BLOB))
    changes.load("incoming.changes", digest_expected=digest)
    assert "foo_1.0_amd64.deb" in changes.digests


@pytest.mark.parametrize("name", ["../foo_1.0_amd64.deb", "sub/foo_1.0_amd64.deb", "/abs.deb"])
def test_parse_refuses_a_listed_file_that_is_not_a_bare_name(name: str) -> None:
    blob = f"Format: 1.8\nChecksums-Sha256:\n deadbeef 1 {name}\n".encode()
    with pytest.raises(ChangesException, match="unsafe file name"):
        Changes(_storage_with(blob)).parse(blob)


def test_parse_reads_a_clearsigned_file_through_its_signed_body() -> None:
    """Regression: `debsign` output failed with "No SHA256 checksums found"."""
    blob = make_clearsigned(_CHANGES_BLOB)
    changes = Changes(_storage_with(blob))
    changes.parse(blob)

    assert changes.get_deb_files() == ["foo_1.0_amd64.deb", "bar_1.0_all.deb"]


def test_parse_malformed_clearsigned_file_raises() -> None:
    blob = b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n" + _CHANGES_BLOB
    with pytest.raises(ChangesException, match="malformed signed file"):
        Changes(_storage_with(blob)).parse(blob)


@pytest.mark.parametrize(
    "blob",
    [
        _CHANGES_BLOB + b"Maintainer: Jos\xe9\n",
        make_clearsigned(_CHANGES_BLOB + b"Maintainer: Jos\xe9\n"),
    ],
    ids=["plain", "clearsigned"],
)
def test_parse_refuses_a_file_that_is_not_utf_8(blob: bytes) -> None:
    """Debian Policy 5.1: a latin-1 `.changes` is a typed refusal, not a traceback."""
    with pytest.raises(ChangesException, match="not UTF-8"):
        Changes(_storage_with(blob)).parse(blob)
