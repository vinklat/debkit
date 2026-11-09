"""Coverage for `debkit.repo.release`."""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from debian.deb822 import Deb822
from helpers.factories import make_clearsigned
from txstore import StorageFS, calc_digests

from debkit.repo import release as release_module
from debkit.repo.gnupg import cleartext_body
from debkit.repo.hashes import ChecksumException
from debkit.repo.params import RepoParams
from debkit.repo.release import (
    IndexEntry,
    Release,
    ReleaseFreshnessException,
    ReleaseNotFoundException,
    ReleaseReadException,
    ReleaseWriteException,
    SignatureMode,
    parse_release_date,
)


def _params(verify_gpg: bool = False, verify_digests: bool = False) -> RepoParams:
    p = RepoParams(
        dist="bookworm", components=["main"], verify_gpg=verify_gpg, verify_digests=verify_digests
    )
    return p


def _release_blob() -> bytes:
    return (
        b"Origin: example\n"
        b"Label: example\n"
        b"Codename: bookworm\n"
        b"Components: main\n"
        b"SHA256:\n"
        b" deadbeef        16 main/binary-amd64/Packages\n"
    )


def test_parse_round_trip_preserves_digests_and_fields() -> None:
    storage = MagicMock()
    r = Release(storage, _params())
    r.parse(_release_blob())

    assert r.fields["Codename"] == "bookworm"
    assert "SHA256" in r.hashfuns
    assert r.digests["main/binary-amd64/Packages"]["SHA256"] == "deadbeef"


def test_parse_serialize_round_trip() -> None:
    storage = MagicMock()
    r = Release(storage, _params())
    r.parse(_release_blob())
    out = r.serialize()
    assert "Codename: bookworm" in out
    assert "SHA256:" in out
    assert "main/binary-amd64/Packages" in out


def test_serialize_writes_folded_and_non_ascii_values_verbatim() -> None:
    """Regression: the email header writer refused a line break and MIME-encoded `Café`."""
    blob = (
        "Origin: Café\nDate: Sat, 07 Sep 2024 10:00:00 UTC\nDescription: line one\n line two\n"
        "Architectures: amd64\n arm64\nSHA256:\n aa 1 main/binary-amd64/Packages\n"
    ).encode()
    r = Release(MagicMock(), _params())
    r.parse(blob)

    out = r.serialize()

    assert "Origin: Café\n" in out
    assert "Date: Sat, 07 Sep 2024 10:00:00 UTC\n" in out
    assert "Description: line one\n line two\n" in out
    again = Release(MagicMock(), _params())
    again.parse(out.encode())
    assert again.fields == r.fields
    assert again.digests == r.digests


@pytest.mark.parametrize("blank", [" ", "\t", "  \t"], ids=["space", "tab", "mixed"])
def test_serialize_writes_a_blank_continuation_line_as_a_dot(blank: str) -> None:
    """A deb822 reader ended the stanza at the blank line and lost the hash blocks."""
    blob = (
        f"Origin: x\nDescription: line one\n{blank}\n line three\nSuite: s\n"
        "SHA256:\n aa 1 main/binary-amd64/Packages\n"
    ).encode()
    r = Release(MagicMock(), _params())
    r.parse(blob)

    out = r.serialize()

    assert "Description: line one\n .\n line three\n" in out
    (stanza,) = Deb822.iter_paragraphs(out)
    assert stanza["Suite"] == "s"
    assert "SHA256" in stanza


def test_serialize_twice_keeps_a_long_value_on_one_line() -> None:
    """A value past the old 160-column fold came back multi-line and broke the next save."""
    components = " ".join(f"component{n}" for n in range(30))
    r = Release(MagicMock(), _params())
    r.parse(f"Components: {components}\n".encode())

    again = Release(MagicMock(), _params())
    again.parse(r.serialize().encode())

    assert again.serialize() == f"Components: {components}"


def test_parse_again_replaces_the_digest_block() -> None:
    """A reload is a new manifest: a key or algorithm the new file drops must not survive."""
    r = Release(MagicMock(), _params())
    r.parse(
        b"Components: main\n"
        b"SHA1:\n 1111 16 main/binary-amd64/Packages\n"
        b"SHA256:\n aaaa 16 main/binary-amd64/Packages\n bbbb 8 main/i18n/Translation-en\n"
    )

    r.parse(b"Components: main\nSHA256:\n cccc 16 main/binary-amd64/Packages\n")

    assert r.digests == {"main/binary-amd64/Packages": {"Size": "16", "SHA256": "cccc"}}
    assert r.hashfuns == ["SHA256"]


def test_parse_keeps_the_last_of_duplicate_fields() -> None:
    r = Release(MagicMock(), _params())
    r.parse(b"Origin: first\nCodename: bookworm\nOrigin: last\n")
    assert r.fields == {"Origin": "last", "Codename": "bookworm"}


def test_reset_clears_the_digest_block() -> None:
    """A reset target records nothing, so no stale digest can settle a sync decision."""
    r = Release(MagicMock(), _params())
    r.parse(_release_blob())

    r.reset()

    assert r.digests == {}


def test_load_missing_files_raises_not_found() -> None:
    """The one read failure `insert` may answer by setting up a new repository."""
    storage = MagicMock()
    storage.exists.return_value = False
    r = Release(storage, _params())
    with pytest.raises(ReleaseNotFoundException):
        r.load()


def test_load_checksum_mismatch_raises() -> None:
    storage = MagicMock()
    # Root release found, sub-release path exists and returns blob with bad digest.
    storage.exists.side_effect = lambda path: True
    storage.get.return_value = b"Origin: x\nSHA256:\n deadbeef 1 nested\n"

    root_release = Release(storage, _params(verify_digests=True))
    root_release.digests["x/Release"] = {"SHA256": "ffffff", "Size": "1"}

    sub = Release(storage, _params(verify_digests=True))
    sub.params.indices_prefix = ""  # the seeded key cannot match, so load must raise
    with pytest.raises(ChecksumException):
        sub.load_component("main", "amd64", root_release=root_release)


def test_setup_initializes_root_release() -> None:
    r = Release(MagicMock(), _params())
    r.setup(fields={"Origin": "test", "Codename": "bookworm"})
    assert r.modified is True
    assert r.filename == "dists/bookworm/Release"
    assert r.hashfuns  # populated by setup


def test_is_empty_true_after_construction() -> None:
    r = Release(MagicMock(), _params())
    assert r.is_empty() is True


@pytest.mark.parametrize(
    ("exc", "prefix"),
    [
        (ReleaseReadException("x"), "Release read: "),
        (ReleaseNotFoundException("x"), "Release read: "),
        (ReleaseWriteException("x"), "Release write: "),
        (ReleaseFreshnessException("x"), "Release freshness: "),
    ],
)
def test_release_exceptions_name_their_failure_in_the_message(exc: Exception, prefix: str) -> None:
    """The CLI logs the message only, so the prefix is what tells a read from a write failure."""
    assert str(exc) == f"{prefix}x"


def test_save_no_op_when_not_modified() -> None:
    storage = MagicMock()
    r = Release(storage, _params())
    r.filename = "dists/bookworm/Release"
    assert r.save() is None
    storage.put.assert_not_called()


def test_load_of_a_root_with_a_digest_block_is_not_modified(tmp_path: Path) -> None:
    """Regression: parsing the digest block marked a fresh load modified, so save rewrote it."""
    storage = StorageFS(str(tmp_path))
    storage.put("dists/bookworm/Release", _release_blob())
    r = Release(storage, _params())

    r.load()

    assert r.digests
    assert r.modified is False
    assert r.save() is None


@pytest.mark.parametrize("root", [True, False], ids=["root", "component"])
def test_reset_restores_a_fresh_release_of_the_same_kind(root: bool) -> None:
    """The kind is fixed at construction: a reset clears the content, never the kind."""
    fresh = Release(MagicMock(), _params(), root=root)
    r = Release(fresh.storage, fresh.params, root=root)
    if root:
        r.setup(fields={"Origin": "test"})
    else:
        r.setup_component("main", "amd64")

    r.reset()

    shared = {"storage", "params", "gpg"}
    assert {k: v for k, v in vars(r).items() if k not in shared} == {
        k: v for k, v in vars(fresh).items() if k not in shared
    }
    assert r.save() is None


_ROOT = "dists/bookworm"


def test_save_unsigned_removes_the_stale_root_signatures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both sign the old content, so apt would read a hash mismatch or a bad signature."""
    storage = StorageFS(str(tmp_path))
    for name in ("Release", "Release.gpg", "InRelease"):
        storage.put(f"{_ROOT}/{name}", b"Origin: old\n")
    r = Release(storage, _params(), gpg=None, root=True)
    r.load()
    r.fields["Origin"] = "new"
    r.modified = True

    with caplog.at_level(logging.WARNING):
        digests = r.save()

    assert not storage.exists(f"{_ROOT}/InRelease")
    assert not storage.exists(f"{_ROOT}/Release.gpg")
    assert storage.get(f"{_ROOT}/Release") == b"Origin: new"
    assert digests == calc_digests(b"Origin: new")
    assert r.found_indices_files == [f"{_ROOT}/Release"]
    assert sum("removed stale signature" in rec.message for rec in caplog.records) == 2


def test_save_refuses_a_root_read_with_verification_on_and_no_gnupg(tmp_path: Path) -> None:
    """The sync target's mode: an unsigned save would drop the signatures it read."""
    storage = StorageFS(str(tmp_path))
    for name in ("Release", "Release.gpg"):
        storage.put(f"{_ROOT}/{name}", b"Origin: old\n")
    r = Release(storage, _params(verify_gpg=True), gpg=None, root=True)
    r.load()
    r.fields["Origin"] = "new"
    r.modified = True

    with pytest.raises(ReleaseWriteException, match="refusing to save it unsigned"):
        r.save()

    assert storage.get(f"{_ROOT}/Release") == b"Origin: old\n"
    assert storage.exists(f"{_ROOT}/Release.gpg")


@pytest.mark.parametrize(
    ("verify_gpg", "gpg", "mode"),
    [
        (True, MagicMock(), SignatureMode.VERIFY_AND_SIGN),
        (True, None, SignatureMode.READ_SIGNED_UNVERIFIED),
        (False, MagicMock(), SignatureMode.OFF),
        (False, None, SignatureMode.OFF),
    ],
    ids=["verify", "sync-target", "skip-verify-gpg", "skip-verify"],
)
def test_signature_mode_combines_the_gpg_switch_and_the_gnupg(
    verify_gpg: bool, gpg: MagicMock | None, mode: SignatureMode
) -> None:
    r = Release(MagicMock(), _params(verify_gpg=verify_gpg), gpg=gpg, root=True)

    assert r.signature_mode is mode


def test_save_unsigned_without_signatures_only_writes_the_release(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = StorageFS(str(tmp_path))
    r = Release(storage, _params())
    r.setup(fields={"Origin": "test"})

    with caplog.at_level(logging.WARNING):
        digests = r.save()

    assert digests == calc_digests(storage.get(f"{_ROOT}/Release"))
    assert "removed stale signature" not in caplog.text


def _signing_gpg(inline: str = "inline", detached: str = "detached") -> MagicMock:
    gpg = MagicMock()
    gpg.sign.side_effect = lambda data, detach=False: detached if detach else inline
    return gpg


def test_save_signed_keeps_and_rewrites_the_root_signatures() -> None:
    storage = MagicMock()
    gpg = _signing_gpg()
    r = Release(storage, _params(verify_gpg=True), gpg=gpg, root=True)
    r.setup(fields={"Origin": "test"})

    digests = r.save()

    storage.delete.assert_not_called()
    written = {call.args[0]: call.args[1] for call in storage.put.call_args_list}
    assert list(written) == [f"{_ROOT}/Release", f"{_ROOT}/InRelease", f"{_ROOT}/Release.gpg"]
    body = written[f"{_ROOT}/Release"]
    assert written[f"{_ROOT}/InRelease"] == b"inline"
    assert written[f"{_ROOT}/Release.gpg"] == b"detached"
    assert digests == calc_digests(body)
    assert [call.args[0] for call in gpg.sign.call_args_list] == [body, body]


@pytest.mark.parametrize(
    ("source", "root"),
    [("fs:///r ReleaseCandidate main", "dists/ReleaseCandidate"), ("fs:///r Release1", "Release1")],
    ids=["componented", "flat"],
)
def test_save_signed_names_the_signatures_by_layout_not_by_dist(source: str, root: str) -> None:
    """Regression: a substitution of every `Release` in the path renamed the dist too."""
    params = RepoParams(verify_gpg=True)
    params.from_deb_source(source)
    storage = MagicMock()
    r = Release(storage, params, gpg=_signing_gpg(), root=True)
    r.setup(fields={"Origin": "test"})

    r.save()

    written = [call.args[0] for call in storage.put.call_args_list]
    assert written == [f"{root}/Release", f"{root}/InRelease", f"{root}/Release.gpg"]


def _inrelease_only_root(tmp_path: Path, params: RepoParams, gpg: MagicMock | None) -> Release:
    storage = StorageFS(str(tmp_path))
    storage.put(f"{_ROOT}/InRelease", make_clearsigned(b"Origin: inline\nSuite: bookworm\n"))
    r = Release(storage, params, gpg=gpg, root=True)
    r.load()
    r.fields["Origin"] = "new"
    r.modified = True
    return r


def test_save_unsigned_of_an_inrelease_loaded_root_writes_the_release(tmp_path: Path) -> None:
    """Regression: the save wrote the unsigned body into `InRelease` and no `Release`."""
    r = _inrelease_only_root(tmp_path, _params(), gpg=None)

    r.save()

    assert (tmp_path / _ROOT / "Release").read_bytes().startswith(b"Origin: new")
    assert not (tmp_path / _ROOT / "InRelease").exists()


def test_save_signed_of_an_inrelease_loaded_root_writes_every_root_file(tmp_path: Path) -> None:
    """Regression: the save signed into `InInRelease` and `InRelease.gpg`."""
    r = _inrelease_only_root(tmp_path, _params(verify_gpg=True), gpg=_accepting_gpg())
    r.gpg = _signing_gpg()

    r.save()

    names = sorted(path.name for path in (tmp_path / _ROOT).iterdir())
    assert names == ["InRelease", "Release", "Release.gpg"]
    assert (tmp_path / _ROOT / "InRelease").read_bytes() == b"inline"


def test_save_refuses_an_empty_inline_signature_before_writing_inrelease() -> None:
    """`GnuPG.sign` returns an empty signature when signing fails, e.g. on a wrong passphrase."""
    storage = MagicMock()
    r = Release(storage, _params(verify_gpg=True), gpg=_signing_gpg(inline=""), root=True)
    r.setup(fields={"Origin": "test"})

    with pytest.raises(ReleaseWriteException, match="GnuPG sign error"):
        r.save()

    assert [call.args[0] for call in storage.put.call_args_list] == [f"{_ROOT}/Release"]


def test_save_refuses_an_empty_detached_signature() -> None:
    storage = MagicMock()
    r = Release(storage, _params(verify_gpg=True), gpg=_signing_gpg(detached=""), root=True)
    r.setup(fields={"Origin": "test"})

    with pytest.raises(ReleaseWriteException, match="GnuPG sign error"):
        r.save()

    written = [call.args[0] for call in storage.put.call_args_list]
    assert written == [f"{_ROOT}/Release", f"{_ROOT}/InRelease"]


def test_save_of_a_component_release_leaves_the_root_signatures() -> None:
    storage = MagicMock()
    r = Release(storage, _params())
    r.setup_component("main", "amd64")

    r.save()

    storage.delete.assert_not_called()


def test_parse_falls_back_to_latin_1_for_a_non_utf_8_release() -> None:
    r = Release(MagicMock(), _params())

    r.parse(b"Origin: Caf\xe9\nSuite: bookworm\n")

    assert r.fields["Origin"] == "Café"
    assert r.fields["Suite"] == "bookworm"


def test_parse_nonstandard_continuation_lines() -> None:
    """Bare unindented continuation tokens (e.g. ProxySQL) must be folded correctly."""
    blob = b"Origin: example\nArchitectures: amd64\narm64\nComponents: main\n"
    r = Release(MagicMock(), _params())
    r.parse(blob)

    assert "arm64" in r.fields["Architectures"]
    assert "amd64" in r.fields["Architectures"]
    assert r.fields["Components"] == "main"


def test_parse_stray_bare_token_in_hash_block_not_folded() -> None:
    """A stray bare token before a hash entry must not be folded, preventing triple mis-alignment.

    With context-unaware folding the token would be merged into the field-name line
    (e.g. ``SHA256: stray``), shifting the ``(digest, size, filename)`` triples so
    that ``stray`` becomes the recorded digest value — silently wrong, no exception.
    """
    blob = (
        b"Origin: example\n"
        b"Components: main\n"
        b"SHA256:\n"
        b"stray\n"  # bare token inside hash block — must NOT be folded onto SHA256:
        b" aaaa             8 main/binary-amd64/Packages\n"
    )
    r = Release(MagicMock(), _params())
    r.parse(blob)

    assert "main/binary-amd64/Packages" in r.digests
    pkg_digests = r.digests["main/binary-amd64/Packages"]
    assert pkg_digests.get("SHA256") == "aaaa"
    assert pkg_digests.get("SHA256") != "stray"


def test_listed_indices_prefixes_componented_paths() -> None:
    """Each digest key becomes a full `dists/<dist>/` storage path, sorted by key."""
    r = Release(MagicMock(), _params())
    r.digests = {
        "main/i18n/Translation-en.bz2": {"SHA256": "bbbb", "Size": "2"},
        "main/dep11/Components-amd64.yml.gz": {"SHA256": "aaaa", "Size": "1"},
    }
    assert r.listed_indices() == [
        IndexEntry(
            "main/dep11/Components-amd64.yml.gz",
            "dists/bookworm/main/dep11/Components-amd64.yml.gz",
            {"SHA256": "aaaa", "Size": "1"},
        ),
        IndexEntry(
            "main/i18n/Translation-en.bz2",
            "dists/bookworm/main/i18n/Translation-en.bz2",
            {"SHA256": "bbbb", "Size": "2"},
        ),
    ]


def test_listed_indices_flat_real_dist_prefixes_dist() -> None:
    p = RepoParams(dist="stable", verify_gpg=False, verify_digests=False)
    p.is_flat = True
    r = Release(MagicMock(), p)
    r.digests = {"Packages.xz": {"SHA256": "aaaa"}}
    assert r.listed_indices() == [
        IndexEntry("Packages.xz", "stable/Packages.xz", {"SHA256": "aaaa"})
    ]


def test_listed_indices_rootless_flat_returns_key() -> None:
    p = RepoParams(dist="/", verify_gpg=False, verify_digests=False)
    p.is_flat = True
    r = Release(MagicMock(), p)
    r.digests = {"Packages.xz": {"SHA256": "aaaa"}}
    assert r.listed_indices() == [IndexEntry("Packages.xz", "Packages.xz", {"SHA256": "aaaa"})]


def test_listed_indices_empty_release_returns_empty() -> None:
    """A Release with no digest block lists nothing."""
    assert Release(MagicMock(), _params()).listed_indices() == []


# --- layout paths come from RepoParams.indices_path -----------------------------------------


def _flat_params(dist: str = "bookworm") -> RepoParams:
    """Params for a flat repo, as a two-field deb source produces."""
    p = RepoParams(dist=dist, verify_gpg=False, verify_digests=False)
    p.is_flat = True
    p.indices_prefix = ""
    return p


def test_setup_flat_real_dist_writes_the_root_release_under_the_dist() -> None:
    """A flat repo publishes its manifest under `<dist>/`, where its loader looks."""
    r = Release(MagicMock(), _flat_params())
    r.setup(fields={"Origin": "test"})

    assert r.filename == "bookworm/Release"
    assert r.found_indices_files == ["bookworm/Release"]


def test_setup_rootless_flat_writes_the_bare_root_release() -> None:
    """A rootless flat repo has no prefix to add."""
    r = Release(MagicMock(), _flat_params(dist="/"))
    r.setup(fields={"Origin": "test"})

    assert r.filename == "Release"


def test_find_file_flat_real_dist_reads_the_root_release_setup_wrote() -> None:
    """The write and read paths agree, so a written Release is found again."""
    storage = MagicMock()
    storage.exists.side_effect = lambda path: path in {"bookworm/Release", "bookworm/Release.gpg"}
    storage.get.return_value = b"Origin: x\n"

    r = Release(storage, _flat_params())
    r.load()
    assert r.filename == "bookworm/Release"


def test_find_file_source_arch_reads_the_source_release() -> None:
    """A source component Release lives under `source/`, like its `Sources` index."""
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get.return_value = b"Origin: x\n"

    r = Release(storage, _params())
    r.load_component("main", "source", Release(r.storage, r.params, root=True))

    assert r.filename == "dists/bookworm/main/source/Release"


def test_find_file_flat_component_release_path_carries_no_leading_slash() -> None:
    """An empty prefix must not produce an absolute, root-escaping key."""
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get.return_value = b"Origin: x\n"

    r = Release(storage, _flat_params())
    r.load_component("main", "amd64", Release(r.storage, r.params, root=True))

    assert r.filename == "bookworm/binary-amd64/Release"
    assert not r.filename.startswith("/")


@pytest.mark.parametrize("arch", ["amd64", "source"])
@pytest.mark.parametrize("params_fn", [_params, _flat_params, lambda: _flat_params("/")])
def test_find_file_reads_the_component_release_setup_wrote(
    params_fn: Callable[[], RepoParams], arch: str
) -> None:
    """One composition for both sides, so a written component Release is found again."""
    storage = MagicMock()
    storage.get.return_value = b"Origin: x\n"

    writer = Release(storage, params_fn())
    writer.setup_component("main", arch)

    storage.exists.side_effect = lambda path: path == writer.filename
    reader = Release(storage, params_fn())
    reader.load_component("main", arch, Release(reader.storage, reader.params, root=True))

    assert reader.filename == writer.filename


@pytest.mark.parametrize(("comp", "arch"), [("main", ""), ("", "amd64")])
def test_component_entry_points_refuse_an_empty_coordinate(comp: str, arch: str) -> None:
    """An empty arch composed `binary-/Release`, an empty component the root Release path."""
    storage = MagicMock()
    r = Release(storage, _params())

    with pytest.raises(ValueError, match="needs a component and an arch"):
        r.load_component(comp, arch, Release(r.storage, r.params, root=True))
    with pytest.raises(ValueError, match="needs a component and an arch"):
        r.setup_component(comp, arch)

    storage.exists.assert_not_called()
    storage.get.assert_not_called()
    assert r.filename is None


def test_setup_copies_the_fields_it_is_given() -> None:
    """The caller's mapping, e.g. a module-level default, must not gain generated fields."""
    fields = {"Components": "main"}
    Release(MagicMock(), _params()).setup(fields=fields)
    assert fields == {"Components": "main"}


def test_load_component_release_verifies_against_its_flat_release_key() -> None:
    """The verify key matches the key `Component.save` writes for a flat repo."""
    data = b"Origin: x\n"
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get.return_value = data
    storage.get_stream.side_effect = lambda path: io.BytesIO(data)

    params = _flat_params()
    params.verify_digests = True
    root_release = Release(storage, _flat_params())
    root_release.digests["binary-amd64/Release"] = calc_digests(data)

    sub = Release(storage, params)
    assert sub.load_component("main", "amd64", root_release=root_release) is True


# ---------- Root Release files

_RELEASE_BODY = b"Origin: detached\nDate: Sat, 07 Sep 2024 10:00:00 UTC\n"


_ROOT_FILES = {
    "dists/bookworm/Release": _RELEASE_BODY,
    "dists/bookworm/Release.gpg": b"detached signature",
    "dists/bookworm/InRelease": make_clearsigned(_RELEASE_BODY),
}


def _root_storage(files: dict[str, bytes]) -> MagicMock:
    storage = MagicMock()
    storage.exists.side_effect = lambda path: path in files
    storage.get.side_effect = lambda path: files[path]
    storage.get_stream.side_effect = lambda path: io.BytesIO(files[path])
    return storage


def test_load_root_records_the_digests_of_every_root_file() -> None:
    """Sync binds each root copy to these bytes -> CRYPTO.md#verification-order."""
    r = Release(_root_storage(_ROOT_FILES), _params())
    r.load()

    assert r.root_digests == {
        "Release": calc_digests(_ROOT_FILES["dists/bookworm/Release"]),
        "Release.gpg": calc_digests(_ROOT_FILES["dists/bookworm/Release.gpg"]),
        "InRelease": calc_digests(_ROOT_FILES["dists/bookworm/InRelease"]),
    }
    assert (
        r.verify_root_digests("Release", calc_digests(_ROOT_FILES["dists/bookworm/Release"]))
        is True
    )
    assert r.verify_root_digests("Release", calc_digests(b"replayed")) is False


def test_load_root_refuses_a_root_file_past_the_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A root Release file is read before its signature is checked, so its size is capped."""
    monkeypatch.setattr(release_module, "MAX_RELEASE_BYTES", len(_RELEASE_BODY))
    storage = _root_storage(_ROOT_FILES)

    with pytest.raises(ReleaseReadException, match="InRelease is larger than"):
        Release(storage, _params()).load()


def test_load_component_refuses_a_release_larger_than_the_root_release_records() -> None:
    """The root Release records the per-arch Release size, so a longer body is refused."""
    data = b"Origin: x\n"
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get_stream.side_effect = lambda path: io.BytesIO(data + b"padding")
    params = _flat_params()
    params.verify_digests = True
    root_release = Release(storage, _flat_params())
    root_release.digests["binary-amd64/Release"] = calc_digests(data)

    with pytest.raises(ChecksumException, match="larger than the root Release records"):
        Release(storage, params).load_component("main", "amd64", root_release=root_release)


def _accepting_gpg() -> MagicMock:
    """A GnuPG double accepting every signature and returning what `verify` returns."""
    gpg = MagicMock()
    gpg.verify.side_effect = lambda data, detach_sign=None: (
        data if detach_sign else cleartext_body(data)
    )
    return gpg


def test_load_root_skips_a_detached_signature_without_its_release(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing verifies an unpaired `Release.gpg`, so a sync must not mirror it."""
    junk = b"JUNK not a signature"
    files = {
        "dists/bookworm/Release.gpg": junk,
        "dists/bookworm/InRelease": make_clearsigned(_RELEASE_BODY),
    }
    r = Release(_root_storage(files), _params(verify_gpg=True), gpg=_accepting_gpg())

    with caplog.at_level(logging.WARNING):
        r.load()

    assert r.get_indices_paths() == ["dists/bookworm/InRelease"]
    assert "Release.gpg" not in r.root_digests
    assert r.verify_root_digests("Release.gpg", calc_digests(junk)) is False
    assert "signs no dists/bookworm/Release" in caplog.text


def test_load_root_verifies_every_signed_form_it_mirrors() -> None:
    """An `InRelease` next to a detached `Release` is copied too, so it is verified too."""
    gpg = _accepting_gpg()
    r = Release(_root_storage(_ROOT_FILES), _params(verify_gpg=True), gpg=gpg)
    r.load()

    assert r.filename == "dists/bookworm/Release"
    assert r.fields["Origin"] == "detached"
    gpg.verify.assert_any_call(_ROOT_FILES["dists/bookworm/InRelease"])
    gpg.verify.assert_any_call(
        _ROOT_FILES["dists/bookworm/Release"], detach_sign=_ROOT_FILES["dists/bookworm/Release.gpg"]
    )


@pytest.mark.parametrize(
    "files",
    [
        {
            **_ROOT_FILES,
            "dists/bookworm/InRelease": make_clearsigned(
                _RELEASE_BODY.replace(b"07 Sep", b"10 Jun")
            ),
        },
        {
            "dists/bookworm/Release": _RELEASE_BODY.replace(b"07 Sep", b"10 Jun"),
            "dists/bookworm/InRelease": make_clearsigned(_RELEASE_BODY),
        },
    ],
    ids=["stale-inrelease-beside-detached", "unsigned-release-beside-inrelease"],
)
def test_load_root_refuses_signed_forms_that_differ(files: dict[str, bytes]) -> None:
    """Freshness reads one form while sync mirrors both, and apt prefers `InRelease`."""
    r = Release(_root_storage(files), _params(verify_gpg=True), gpg=_accepting_gpg())

    with pytest.raises(ChecksumException, match="different content"):
        r.load()


@pytest.mark.parametrize(
    "release",
    [
        _RELEASE_BODY.replace(b"\n", b"\r\n"),
        _RELEASE_BODY.replace(b"detached\n", b"detached \t\n"),
        _RELEASE_BODY.rstrip(b"\n"),
    ],
    ids=["crlf", "trailing-whitespace", "no-final-newline"],
)
def test_load_root_accepts_signed_forms_that_differ_only_where_gpg_does_not_sign(
    release: bytes,
) -> None:
    files = {**_ROOT_FILES, "dists/bookworm/Release": release}
    r = Release(_root_storage(files), _params(verify_gpg=True), gpg=_accepting_gpg())

    r.load()


def test_load_root_without_a_keyring_does_not_compare_the_signed_forms() -> None:
    """The sync target is not verified; refusing it would drop the Date it floors on."""
    files = {**_ROOT_FILES, "dists/bookworm/InRelease": make_clearsigned(b"Origin: other\n")}
    r = Release(_root_storage(files), _params(verify_gpg=True))

    r.load()
    assert r.fields["Origin"] == "detached"


def test_load_root_refuses_an_inrelease_next_to_a_detached_release_that_fails_verification() -> (
    None
):
    from debkit.repo.gnupg import GPGVerifyException

    def verify(data: bytes, detach_sign: bytes | None = None) -> bytes:
        if detach_sign is None:
            raise GPGVerifyException("GPG not valid signature!")
        return data

    gpg = MagicMock()
    gpg.verify.side_effect = verify
    r = Release(_root_storage(_ROOT_FILES), _params(verify_gpg=True), gpg=gpg)

    with pytest.raises(GPGVerifyException):
        r.load()


_INRELEASE_BODY = b"Origin: inline\nDate: Sat, 07 Sep 2024 10:00:00 UTC\n"
_INRELEASE = make_clearsigned(_INRELEASE_BODY)


@pytest.mark.parametrize(
    "files",
    [
        {"dists/bookworm/InRelease": _INRELEASE},
        {"dists/bookworm/Release": b"Origin: unsigned\n", "dists/bookworm/InRelease": _INRELEASE},
    ],
    ids=["inrelease-only", "release-without-gpg"],
)
def test_load_root_without_a_keyring_parses_the_inrelease_body(files: dict[str, bytes]) -> None:
    """A sync target loads without gpg, yet its Date still floors the source."""
    r = Release(_root_storage(files), _params(verify_gpg=True), gpg=None)
    r.load()

    assert r.filename == "dists/bookworm/InRelease"
    assert r.fields == {"Origin": "inline", "Date": "Sat, 07 Sep 2024 10:00:00 UTC"}
    assert r.root_digests["InRelease"] == calc_digests(_INRELEASE)

    older = _dated({"Date": OLD_DATE})
    with pytest.raises(ReleaseFreshnessException, match="older than the target"):
        older.check_fresh_against(r, NOW)


def test_load_root_without_a_keyring_hashes_the_body_the_source_hashes() -> None:
    """Both sides hash the signed body, so an unchanged source does not force indices."""
    import hashlib

    r = Release(_root_storage({"dists/bookworm/InRelease": _INRELEASE}), _params(verify_gpg=True))
    r.load()

    assert r.hash == hashlib.sha256(_INRELEASE_BODY).hexdigest()


def test_load_root_without_a_keyring_refuses_a_malformed_inrelease() -> None:
    from debkit.repo.gnupg import GPGVerifyException

    storage = _root_storage({"dists/bookworm/InRelease": _INRELEASE + b"unsigned tail\n"})
    with pytest.raises(GPGVerifyException, match="after the signature"):
        Release(storage, _params(verify_gpg=True)).load()


def test_load_root_refuses_an_unsigned_release_when_signatures_are_checked() -> None:
    """The repository exists, so the failure must not read as not-found."""
    storage = _root_storage({"dists/bookworm/Release": _RELEASE_BODY})
    r = Release(storage, _params(verify_gpg=True), gpg=_accepting_gpg())

    with pytest.raises(ReleaseReadException, match="signature file missing") as excinfo:
        r.load()
    assert not isinstance(excinfo.value, ReleaseNotFoundException)


@pytest.mark.parametrize(
    ("verify_gpg", "gpg"),
    [(False, MagicMock()), (False, None), (True, None)],
    ids=["skip-verify-gpg", "skip-verify", "sync-target"],
)
def test_load_root_reads_an_unsigned_release_when_signatures_are_not_checked(
    verify_gpg: bool, gpg: MagicMock | None
) -> None:
    storage = _root_storage({"dists/bookworm/Release": _RELEASE_BODY})
    r = Release(storage, _params(verify_gpg=verify_gpg), gpg=gpg)

    r.load()
    assert r.fields["Origin"] == "detached"
    if gpg is not None:
        gpg.verify.assert_not_called()


def test_load_root_reads_an_inrelease_body_unverified_with_verification_disabled() -> None:
    """`--skip-verify-gpg` reads an `InRelease`-only repository as apt's `trusted=yes` does."""
    gpg = MagicMock()
    r = Release(_root_storage({"dists/bookworm/InRelease": _INRELEASE}), _params(), gpg=gpg)

    r.load()
    assert r.filename == "dists/bookworm/InRelease"
    assert r.fields["Origin"] == "inline"
    gpg.verify.assert_not_called()


def test_load_root_falls_back_to_latin_1_for_an_inrelease_body() -> None:
    """The signed body is decoded as a detached `Release` is, latin-1 fallback included."""
    inrelease = _INRELEASE.replace(b"Origin: inline", b"Origin: Caf\xe9")
    r = Release(_root_storage({"dists/bookworm/InRelease": inrelease}), _params())

    r.load()
    assert r.fields["Origin"] == "Café"


def test_reset_and_copy_carry_the_root_digests() -> None:
    from copy import copy

    r = Release(_root_storage(_ROOT_FILES), _params())
    r.load()

    assert copy(r).root_digests == r.root_digests
    r.reset()
    assert r.root_digests == {}


# ---------- Release dates


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Sat, 10 Jun 2023 08:47:46 UTC", datetime(2023, 6, 10, 8, 47, 46, tzinfo=UTC)),
        ("Sat, 10 Jun 2023 08:47:46 GMT", datetime(2023, 6, 10, 8, 47, 46, tzinfo=UTC)),
        ("Sat, 10 Jun 2023 08:47:46 -0000", datetime(2023, 6, 10, 8, 47, 46, tzinfo=UTC)),
        (
            "Sat, 10 Jun 2023 10:47:46 +0200",
            datetime(2023, 6, 10, 10, 47, 46, tzinfo=timezone(timedelta(hours=2))),
        ),
    ],
)
def test_parse_release_date_returns_an_aware_date(value: str, expected: datetime) -> None:
    parsed = parse_release_date(value)
    assert parsed == expected
    assert parsed.tzinfo is not None


@pytest.mark.parametrize(
    "value",
    [
        "Sat, 10 Jun 2023 08:47:46 CEST",
        "Sat, 10 Jun 2023 08:47:46",
        "Sat, 10 Jun 2023 08:47:46 UTC garbage",
        "yesterday",
        "",
    ],
    ids=["named-zone", "no-zone", "trailing-text", "not-a-date", "empty"],
)
def test_parse_release_date_rejects_a_value_without_an_accepted_zone(value: str) -> None:
    """An unrecognised zone name would otherwise read silently as UTC."""
    with pytest.raises(ValueError, match="with a UTC or numeric zone"):
        parse_release_date(value)


@pytest.mark.parametrize("value", ["garbage UTC", "Sat, 99 Foo 2023 08:47:46 UTC"])
def test_parse_release_date_rejects_an_unparsable_date_in_an_accepted_zone(value: str) -> None:
    """The zone check passes, so the date body itself is what is refused."""
    with pytest.raises(ValueError, match=r"^not an RFC 2822 date: "):
        parse_release_date(value)


# ---------- Release freshness

OLD_DATE = "Sat, 10 Jun 2023 08:47:46 UTC"
NEW_DATE = "Sat, 07 Sep 2024 10:00:00 UTC"
LATER_DATE = "Wed, 01 Jan 2031 00:00:00 UTC"
NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _dated(fields: dict[str, str]) -> Release:
    """A real Release with only header fields, as a loaded or unloaded repo holds them."""
    release = Release(MagicMock(), _params())
    release.fields = dict(fields)
    return release


@pytest.mark.parametrize("src_date", [NEW_DATE, OLD_DATE], ids=["newer", "equal"])
def test_fresh_accepts_a_source_not_older_than_the_target(src_date: str) -> None:
    _dated({"Date": src_date}).check_fresh_against(_dated({"Date": OLD_DATE}), NOW)


def test_fresh_refuses_a_source_older_than_the_target() -> None:
    with pytest.raises(ReleaseFreshnessException, match="older than the target"):
        _dated({"Date": OLD_DATE}).check_fresh_against(_dated({"Date": NEW_DATE}), NOW)


def test_fresh_refuses_an_expired_source() -> None:
    source = _dated({"Date": NEW_DATE, "Valid-Until": NEW_DATE})
    with pytest.raises(ReleaseFreshnessException, match="expired"):
        source.check_fresh_against(_dated({"Date": OLD_DATE}), NOW)


def test_fresh_accepts_a_source_valid_until_later() -> None:
    _dated({"Date": NEW_DATE, "Valid-Until": LATER_DATE}).check_fresh_against(
        _dated({"Date": OLD_DATE}), NOW
    )


@pytest.mark.parametrize("field", ["Date", "Valid-Until"])
def test_fresh_refuses_a_malformed_source_date(field: str) -> None:
    source = _dated({"Date": NEW_DATE, field: "yesterday"})
    with pytest.raises(ReleaseFreshnessException, match=f"source {field} is"):
        source.check_fresh_against(_dated({"Date": OLD_DATE}), NOW)


def test_fresh_refuses_a_source_without_date_against_a_dated_target() -> None:
    with pytest.raises(ReleaseFreshnessException, match="has no Date"):
        _dated({}).check_fresh_against(_dated({"Date": OLD_DATE}), NOW)


def test_fresh_does_not_compare_with_an_unloaded_target() -> None:
    _dated({"Date": OLD_DATE}).check_fresh_against(_dated({}), NOW)


def test_fresh_logs_and_skips_a_malformed_target_date(caplog: pytest.LogCaptureFixture) -> None:
    """The source is older than any date the target could mean, so a comparison would raise."""
    with caplog.at_level(logging.WARNING):
        _dated({"Date": "Thu, 01 Jan 1970 00:00:00 UTC"}).check_fresh_against(
            _dated({"Date": "yesterday"}), NOW
        )
    assert "not compared with the source" in caplog.text
