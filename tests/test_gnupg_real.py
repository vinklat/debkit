"""`GnuPG` and the root Release against the real gpg binary (TESTING.md crypto row).

`tests/test_gnupg.py` mocks python-gnupg; these cases prove what only gpg decides: a signature
covers every line of the data, a changed byte or an unknown signer fails, and the passphrase
reaches the key.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers.gpg import KEYRING, make_gpg_home, requires_gpg, stop_gpg_agent
from txstore import StorageFS

from debkit.repo.gnupg import GnuPG, GPGVerifyException, cleartext_body
from debkit.repo.params import RepoParams
from debkit.repo.release import ReleaseWriteException, RootRelease

pytestmark = requires_gpg

# Multi-line, like a Release: a passphrase read from the data would drop its first line.
_DATA = b"Origin: debkit\nLabel: debkit\nSuite: bookworm\nDate: Sun, 08 Sep 2024 10:00:00 UTC\n"


@pytest.fixture(scope="module")
def homes() -> Iterator[dict[str, Path]]:
    """Signer, unknown signer, and passphrase-locked homes under one short temporary root.

    The root is short because gpg-agent's socket path has a length limit.
    """
    with tempfile.TemporaryDirectory(prefix="dkgpg") as root:
        built = {
            "signer": make_gpg_home(Path(root) / "s", "signer"),
            "other": make_gpg_home(Path(root) / "o", "other"),
            "locked": make_gpg_home(Path(root) / "l", "locked", passphrase="pw"),
        }
        try:
            yield built
        finally:
            for home in built.values():
                stop_gpg_agent(home)


def _gnupg(home: Path, passphrase: str | None = None) -> GnuPG:
    return GnuPG(str(home), KEYRING, passphrase)


def test_gnupg_clearsigned_round_trip_returns_the_body(homes: dict[str, Path]) -> None:
    gpg = _gnupg(homes["signer"], passphrase="")

    signed = gpg.sign(_DATA).encode()

    assert gpg.verify(signed) == cleartext_body(signed) == _DATA


def test_gnupg_detached_round_trip_covers_every_line(homes: dict[str, Path]) -> None:
    """Regression (SETUP.md): with an empty passphrase gpg read the first line as one."""
    gpg = _gnupg(homes["signer"], passphrase="")

    signature = gpg.sign(_DATA, detach=True).encode()

    assert gpg.verify(_DATA, detach_sign=signature) == _DATA
    with pytest.raises(GPGVerifyException):
        gpg.verify(_DATA.split(b"\n", 1)[1], detach_sign=signature)


def test_gnupg_verify_refuses_a_tampered_clearsigned_body(homes: dict[str, Path]) -> None:
    gpg = _gnupg(homes["signer"])
    signed = gpg.sign(_DATA).encode()

    with pytest.raises(GPGVerifyException):
        gpg.verify(signed.replace(b"Suite: bookworm", b"Suite: trixie"))


def test_gnupg_verify_refuses_tampered_detached_data(homes: dict[str, Path]) -> None:
    gpg = _gnupg(homes["signer"])
    signature = gpg.sign(_DATA, detach=True).encode()

    with pytest.raises(GPGVerifyException):
        gpg.verify(_DATA + b"Extra: field\n", detach_sign=signature)


def test_gnupg_verify_refuses_a_clearsigned_message_from_outside_the_keyring(
    homes: dict[str, Path],
) -> None:
    signed = _gnupg(homes["other"]).sign(_DATA).encode()

    with pytest.raises(GPGVerifyException):
        _gnupg(homes["signer"]).verify(signed)


def test_gnupg_verify_refuses_a_detached_signature_from_outside_the_keyring(
    homes: dict[str, Path],
) -> None:
    signature = _gnupg(homes["other"]).sign(_DATA, detach=True).encode()

    with pytest.raises(GPGVerifyException):
        _gnupg(homes["signer"]).verify(_DATA, detach_sign=signature)


def test_gnupg_sign_with_the_passphrase_unlocks_the_key(homes: dict[str, Path]) -> None:
    gpg = _gnupg(homes["locked"], passphrase="pw")

    signature = gpg.sign(_DATA, detach=True).encode()

    assert gpg.verify(_DATA, detach_sign=signature) == _DATA


def test_gnupg_sign_with_a_wrong_passphrase_returns_no_signature(homes: dict[str, Path]) -> None:
    """`Release.save` refuses the empty result → CRYPTO.md#gpg."""
    assert _gnupg(homes["locked"], passphrase="nope").sign(_DATA) == ""


def test_release_signed_save_loads_back_verified(homes: dict[str, Path], tmp_path: Path) -> None:
    """A root Release debkit signs passes its own verified load, inline and detached alike."""
    params = RepoParams(dist="bookworm", components=["main"], verify_gpg=True)
    storage = StorageFS(str(tmp_path))
    written = RootRelease(storage, params, gpg=_gnupg(homes["signer"]))
    written.setup(fields={"Date": "Sun, 08 Sep 2024 10:00:00 UTC"})
    written.save()

    loaded = RootRelease(storage, params, gpg=_gnupg(homes["signer"]))
    loaded.load()

    assert loaded.fields["Origin"] == "bookworm"
    assert sorted(loaded.root_digests) == ["InRelease", "Release", "Release.gpg"]


def test_release_save_with_a_wrong_passphrase_writes_no_inrelease(
    homes: dict[str, Path], tmp_path: Path
) -> None:
    """Regression: a failed clearsign returned its unsigned armor, saved as `InRelease`."""
    params = RepoParams(dist="bookworm", components=["main"], verify_gpg=True)
    release = RootRelease(StorageFS(str(tmp_path)), params, gpg=_gnupg(homes["locked"], "nope"))
    release.setup()

    with pytest.raises(ReleaseWriteException, match="GnuPG sign error"):
        release.save()

    assert not (tmp_path / "dists" / "bookworm" / "InRelease").exists()
