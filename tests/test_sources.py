"""Unit tests for ``Sources`` (TESTING.md § Repo classes).

Covers: stanza expansion into per-file records, the safety and digest rules, and the
read-only contract. Storage is mocked; no real backend.
"""

from __future__ import annotations

import gzip
import logging
import re
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from helpers.factories import make_params, make_storage_mock

from debkit.repo import Repo
from debkit.repo.component import Component
from debkit.repo.compress import codec_for
from debkit.repo.packages import PackagesException
from debkit.repo.params import RepoParams
from debkit.repo.sources import Sources

_DIR = "pool/main/h/hello"

_STANZA_3 = (
    "Package: hello\n"
    "Binary: hello\n"
    "Version: 2.10-3\n"
    "Format: 3.0 (quilt)\n"
    f"Directory: {_DIR}\n"
    "Files:\n"
    " 1111111111111111 1234 hello_2.10-3.dsc\n"
    " 2222222222222222 725946 hello_2.10.orig.tar.gz\n"
    " 3333333333333333 6132 hello_2.10-3.debian.tar.xz\n"
    "Checksums-Sha256:\n"
    " aaaa 1234 hello_2.10-3.dsc\n"
    " bbbb 725946 hello_2.10.orig.tar.gz\n"
    " cccc 6132 hello_2.10-3.debian.tar.xz\n"
)

_STANZA_4 = (
    "Package: hello\n"
    "Version: 2.10-4\n"
    f"Directory: {_DIR}\n"
    "Checksums-Sha256:\n"
    " dddd 1240 hello_2.10-4.dsc\n"
    " bbbb 725946 hello_2.10.orig.tar.gz\n"
    " eeee 6140 hello_2.10-4.debian.tar.xz\n"
)

_SOURCES = _STANZA_3 + "\n" + _STANZA_4


def _new_sources(storage: MagicMock | None = None, params: RepoParams | None = None) -> Sources:
    return Sources(storage or make_storage_mock(), params or make_params())


# ---------------------------------------------------------------------------
# stanza expansion
# ---------------------------------------------------------------------------


def test_sources_parse_expands_each_stanza_into_one_record_per_file() -> None:
    """Five distinct payloads across two stanzas that share an orig tarball."""
    src = _new_sources()

    assert src._parse_indices(_SOURCES) == 5
    assert set(src.packages) == {
        f"{_DIR}/hello_2.10-3.dsc",
        f"{_DIR}/hello_2.10.orig.tar.gz",
        f"{_DIR}/hello_2.10-3.debian.tar.xz",
        f"{_DIR}/hello_2.10-4.dsc",
        f"{_DIR}/hello_2.10-4.debian.tar.xz",
    }


@pytest.mark.parametrize(
    "data",
    [_STANZA_3 + " \n" + _STANZA_4, (_STANZA_3 + "\n" + _STANZA_4).replace("\n", "\r\n")],
    ids=["whitespace-line", "crlf"],
)
def test_sources_parse_splits_on_a_whitespace_only_line(data: str) -> None:
    """Sources shares the Packages stanza split: both stanzas load, not one merged block."""
    assert _new_sources()._parse_indices(data) == 5


def test_sources_parse_record_carries_the_stanza_fields_and_every_digest() -> None:
    """A payload record looks like a binary package record to every consumer."""
    src = _new_sources()
    src._parse_indices(_STANZA_3)

    dsc = src.packages[f"{_DIR}/hello_2.10-3.dsc"]
    assert dsc.fields["Package"] == "hello"
    assert dsc.fields["Version"] == "2.10-3"
    assert dsc.fields["Size"] == "1234"
    assert str(dsc) == f"{_DIR}/hello_2.10-3.dsc"
    assert dsc.get_digests() == {"MD5Sum": "1111111111111111", "SHA256": "aaaa"}


def test_sources_parse_keeps_the_last_of_a_repeated_field() -> None:
    """A sloppy mirror still syncs -> VALIDATION.md#repeated-fields."""
    src = _new_sources()
    src._parse_indices("Directory: pool/main/x/elsewhere\n" + _STANZA_4)

    assert {path.rsplit("/", 1)[0] for path in src.packages} == {_DIR}


def test_sources_parse_keys_a_shared_payload_once_and_counts_files() -> None:
    """Two versions sharing one orig tarball yield one record, and `size` counts files."""
    src = _new_sources()
    src._parse_indices(_SOURCES)

    assert src.size == 5
    assert len(src) == 5


def test_sources_parse_logs_when_a_shared_payload_disagrees(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tarball listed twice with different digests is a source-repo defect."""
    conflicting = _STANZA_3 + "\n" + _STANZA_4.replace(" bbbb 725946", " ffff 725946")
    src = _new_sources()

    with caplog.at_level(logging.DEBUG):
        src._parse_indices(conflicting)

    assert "differing digests" in caplog.text


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_sources_parse_missing_directory_raises() -> None:
    src = _new_sources()
    stanza = "Package: hello\nChecksums-Sha256:\n aaaa 1 hello.dsc\n"

    with pytest.raises(PackagesException, match="no usable Directory"):
        src._parse_indices(stanza)


def test_sources_parse_rejects_a_directory_in_the_dists_tree() -> None:
    """Every dist sharing a componented root keeps its metadata under `dists/`."""
    src = _new_sources()
    stanza = "Package: hello\nDirectory: dists/bullseye\nChecksums-Sha256:\n aaaa 1 Release\n"

    with pytest.raises(PackagesException, match="dists/ tree"):
        src._parse_indices(stanza)


@pytest.mark.parametrize("name", ["sub/hello.dsc", "../hello.dsc"])
def test_sources_parse_rejects_an_unsafe_file_name(name: str) -> None:
    """A payload name is a bare file name; a path segment could escape the pool."""
    src = _new_sources()
    stanza = f"Package: hello\nDirectory: {_DIR}\nChecksums-Sha256:\n aaaa 1 {name}\n"

    with pytest.raises(PackagesException, match="unsafe file name"):
        src._parse_indices(stanza)


@pytest.mark.parametrize(
    "directory", ["../../etc", ".", "/etc", "http://evil.example/pool", "http:/evil.example/pool"]
)
def test_sources_parse_unusable_directory_raises(directory: str) -> None:
    """`.` joins to a pool-relative key that names the storage root, so it is refused
    alongside the paths that escape it."""
    src = _new_sources()
    stanza = f"Package: hello\nDirectory: {directory}\nChecksums-Sha256:\n aaaa 1 hello.dsc\n"

    with pytest.raises(PackagesException, match="no usable Directory"):
        src._parse_indices(stanza)


def test_sources_parse_md5_only_payload_raises() -> None:
    """`normalize_digests` drops MD5, so an MD5-only record would recopy forever."""
    src = _new_sources()
    stanza = f"Package: hello\nDirectory: {_DIR}\nFiles:\n 1111 1 hello.dsc\n"

    with pytest.raises(PackagesException, match="no SHA digest"):
        src._parse_indices(stanza)


def test_sources_parse_size_disagreement_between_blocks_raises() -> None:
    src = _new_sources()
    stanza = (
        f"Package: hello\nDirectory: {_DIR}\n"
        "Files:\n 1111 1 hello.dsc\n"
        "Checksums-Sha256:\n aaaa 2 hello.dsc\n"
    )

    with pytest.raises(PackagesException, match="different size"):
        src._parse_indices(stanza)


# ---------------------------------------------------------------------------
# read-only contract
# ---------------------------------------------------------------------------


def test_sources_save_writes_nothing() -> None:
    """`Component.save` calls every container, so this returns rather than raises."""
    src = _new_sources()
    src._parse_indices(_STANZA_3)
    src.modified = True

    assert src.save() is None
    cast(MagicMock, src.storage).put.assert_not_called()


def test_sources_insert_raises() -> None:
    with pytest.raises(PackagesException, match="read-only"):
        _new_sources().insert(MagicMock())


def test_sources_remove_drops_nothing_and_logs_the_kept_matches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The base contract's "nothing removed", so no caller has to know the type; the match
    is logged, as the caller's "nothing matched" would otherwise hide it."""
    src = _new_sources()
    src._parse_indices(_STANZA_3)

    with caplog.at_level(logging.WARNING):
        assert src.remove({"Package": re.compile(".*")}) == set()

    assert "3 source payloads match" in caplog.text
    assert len(src.packages) == 3


def test_sources_remove_logs_nothing_when_nothing_matches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    src = _new_sources()
    src._parse_indices(_STANZA_3)

    with caplog.at_level(logging.WARNING):
        assert src.remove({"Package": re.compile("^absent$")}) == set()

    assert "source payloads match" not in caplog.text


def test_sources_payload_verifies_through_the_repo_whatever_its_compression() -> None:
    """A `.tar.zst` payload must verify: the transfer deletes and raises otherwise."""
    params = make_params()
    stanza = (
        "Package: hello\n"
        "Version: 2.10-3\n"
        f"Directory: {_DIR}\n"
        "Checksums-Sha256:\n"
        " aaaa 725946 hello_2.10.orig.tar.zst\n"
    )
    src = _new_sources(params=params)
    src._parse_indices(stanza)

    repo = Repo(make_storage_mock(), params)
    component = Component(repo.storage, params, repo.release, "main")
    component.packages = {"source": src}
    repo.components["main"] = component

    payload = f"{_DIR}/hello_2.10.orig.tar.zst"
    assert repo.verify_checksum(payload, {"SHA256": "aaaa"}) is True
    assert repo.verify_checksum(payload, {"SHA256": "bbbb"}) is False


def test_sources_load_expands_the_index_it_finds() -> None:
    """The inherited load path feeds the Sources parser."""
    storage = make_storage_mock()
    storage.get.return_value = gzip.compress(_STANZA_3.encode("utf-8"))
    src = _new_sources(storage=storage, params=make_params(verify_digests=False))
    root = MagicMock()
    path = "dists/bookworm/main/source/Sources.gz"
    found = ([path], path, codec_for(".gz").decompress)

    with patch.object(Sources, "_find_packages_file", return_value=found):
        assert src.load(root, "main", "source") is True

    assert src.size == 3
