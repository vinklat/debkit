"""Coverage for `debkit.repo.release`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from txstore import StorageFS, calc_digests

from debkit.repo.gnupg import cleartext_body
from debkit.repo.hashes import ChecksumException
from debkit.repo.params import RepoParams
from debkit.repo.release import (
    IndexEntry,
    Release,
    ReleaseFreshnessException,
    ReleaseNotFoundException,
    ReleaseReadException,
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


def test_save_no_op_when_not_modified() -> None:
    storage = MagicMock()
    r = Release(storage, _params())
    r.filename = "dists/bookworm/Release"
    assert r.save() is None
    storage.put.assert_not_called()


_ROOT = "dists/bookworm"


def test_save_unsigned_removes_the_stale_root_signatures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both sign the old content, so apt would read a hash mismatch or a bad signature."""
    storage = StorageFS(str(tmp_path))
    for name in ("Release", "Release.gpg", "InRelease"):
        storage.put(f"{_ROOT}/{name}", b"Origin: old\n")
    r = Release(storage, _params(), gpg=None)
    r.load()
    r.fields["Origin"] = "new"
    r.modified = True

    with caplog.at_level(logging.WARNING):
        assert r.save() is not None

    assert not storage.exists(f"{_ROOT}/InRelease")
    assert not storage.exists(f"{_ROOT}/Release.gpg")
    assert storage.get(f"{_ROOT}/Release") == b"Origin: new"
    assert r.found_indices_files == [f"{_ROOT}/Release"]
    assert sum("removed stale signature" in rec.message for rec in caplog.records) == 2


def test_save_unsigned_without_signatures_only_writes_the_release(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = StorageFS(str(tmp_path))
    r = Release(storage, _params())
    r.setup(fields={"Origin": "test"})

    with caplog.at_level(logging.WARNING):
        assert r.save() is not None

    assert storage.exists(f"{_ROOT}/Release")
    assert "removed stale signature" not in caplog.text


def test_save_signed_keeps_and_rewrites_the_root_signatures() -> None:
    storage = MagicMock()
    gpg = MagicMock()
    gpg.sign.side_effect = lambda data, detach=False: "detached" if detach else "inline"
    r = Release(storage, _params(verify_gpg=True), gpg=gpg)
    r.setup(fields={"Origin": "test"})

    r.save()

    storage.delete.assert_not_called()
    written = [call.args[0] for call in storage.put.call_args_list]
    assert written == [f"{_ROOT}/Release", f"{_ROOT}/InRelease", f"{_ROOT}/Release.gpg"]


def test_save_of_a_component_release_leaves_the_root_signatures() -> None:
    storage = MagicMock()
    r = Release(storage, _params())
    r.setup_component("main", "amd64")

    r.save()

    storage.delete.assert_not_called()


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
    assert r.load() is True
    assert r.filename == "bookworm/Release"


def test_find_file_source_arch_reads_the_source_release() -> None:
    """A source component Release lives under `source/`, like its `Sources` index."""
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get.return_value = b"Origin: x\n"

    r = Release(storage, _params())
    r.load_component("main", "source")

    assert r.filename == "dists/bookworm/main/source/Release"


def test_find_file_flat_component_release_path_carries_no_leading_slash() -> None:
    """An empty prefix must not produce an absolute, root-escaping key."""
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get.return_value = b"Origin: x\n"

    r = Release(storage, _flat_params())
    r.load_component("main", "amd64")

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
    reader.load_component("main", arch)

    assert reader.filename == writer.filename


@pytest.mark.parametrize(("comp", "arch"), [("main", ""), ("", "amd64")])
def test_component_entry_points_refuse_an_empty_coordinate(comp: str, arch: str) -> None:
    """An empty arch composed `binary-/Release`, an empty component the root Release path."""
    storage = MagicMock()
    r = Release(storage, _params())

    with pytest.raises(ValueError, match="needs a component and an arch"):
        r.load_component(comp, arch)
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

    params = _flat_params()
    params.verify_digests = True
    root_release = Release(storage, _flat_params())
    root_release.digests["binary-amd64/Release"] = calc_digests(data)

    sub = Release(storage, params)
    assert sub.load_component("main", "amd64", root_release=root_release) is True


# ---------- Root Release files

_RELEASE_BODY = b"Origin: detached\nDate: Sat, 07 Sep 2024 10:00:00 UTC\n"


def _clearsigned(body: bytes) -> bytes:
    return (
        b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        + body
        + b"-----BEGIN PGP SIGNATURE-----\n\nsig\n-----END PGP SIGNATURE-----\n"
    )


_ROOT_FILES = {
    "dists/bookworm/Release": _RELEASE_BODY,
    "dists/bookworm/Release.gpg": b"detached signature",
    "dists/bookworm/InRelease": _clearsigned(_RELEASE_BODY),
}


def _root_storage(files: dict[str, bytes]) -> MagicMock:
    storage = MagicMock()
    storage.exists.side_effect = lambda path: path in files
    storage.get.side_effect = lambda path: files[path]
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


def _accepting_gpg() -> MagicMock:
    """A GnuPG double accepting every signature and returning what `verify` returns."""
    gpg = MagicMock()
    gpg.verify.side_effect = lambda data, detach_sign=None: (
        data if detach_sign else cleartext_body(data)
    )
    return gpg


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
            "dists/bookworm/InRelease": _clearsigned(_RELEASE_BODY.replace(b"07 Sep", b"10 Jun")),
        },
        {
            "dists/bookworm/Release": _RELEASE_BODY.replace(b"07 Sep", b"10 Jun"),
            "dists/bookworm/InRelease": _clearsigned(_RELEASE_BODY),
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

    assert r.load() is True


def test_load_root_without_a_keyring_does_not_compare_the_signed_forms() -> None:
    """The sync target is not verified; refusing it would drop the Date it floors on."""
    files = {**_ROOT_FILES, "dists/bookworm/InRelease": _clearsigned(b"Origin: other\n")}
    r = Release(_root_storage(files), _params(verify_gpg=True))

    assert r.load() is True
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
_INRELEASE = (
    b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
    + _INRELEASE_BODY
    + b"-----BEGIN PGP SIGNATURE-----\n\nsig\n-----END PGP SIGNATURE-----\n"
)


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

    assert r.load() is True
    assert r.fields["Origin"] == "detached"
    if gpg is not None:
        gpg.verify.assert_not_called()


def test_load_root_reads_an_inrelease_body_unverified_with_verification_disabled() -> None:
    """`--skip-verify-gpg` reads an `InRelease`-only repository as apt's `trusted=yes` does."""
    gpg = MagicMock()
    r = Release(_root_storage({"dists/bookworm/InRelease": _INRELEASE}), _params(), gpg=gpg)

    assert r.load() is True
    assert r.filename == "dists/bookworm/InRelease"
    assert r.fields["Origin"] == "inline"
    gpg.verify.assert_not_called()


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
    with pytest.raises(ValueError, match="not an RFC 2822 date"):
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
