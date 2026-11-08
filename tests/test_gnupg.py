"""Coverage for `debkit.repo.gnupg`.

Tests mock the underlying ``gnupg.GPG`` to keep the matrix hermetic, per
``TESTING.md`` ("no real user GPG home").
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from debkit.repo.gnupg import GnuPG, GPGVerifyException, cleartext_body


def _pars() -> Namespace:
    return Namespace(gnupg_home=".gnupg", gnupg_keyring="trustedkeys.gpg", gnupg_passphrase="pw")


def _gpg_mock(verify_ok: bool = True, sign_out: str = "signed-bytes") -> MagicMock:
    gpg = MagicMock()
    verified = MagicMock()
    verified.__bool__ = lambda self: verify_ok
    verified.username = "signer@example.com"
    gpg.verify.return_value = verified
    gpg.verify_data.return_value = verified
    gpg.sign.return_value = sign_out
    return gpg


def test_init_keyring_failure_raises() -> None:
    with patch("debkit.repo.gnupg.gnupg.GPG", side_effect=ValueError("bad keyring")):
        with pytest.raises(GPGVerifyException):
            GnuPG(_pars())


def test_verify_inrelease_returns_the_signed_body() -> None:
    inrelease = (
        b"-----BEGIN PGP SIGNED MESSAGE-----\n"
        b"Hash: SHA256\n\nOrigin: example\nCodename: bookworm\n\n"
        b"-----BEGIN PGP SIGNATURE-----\n...\n-----END PGP SIGNATURE-----\n"
    )
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock(verify_ok=True)):
        gpg = GnuPG(_pars())
        out = gpg.verify(inrelease)
    assert out == b"Origin: example\nCodename: bookworm\n\n"


def test_verify_invalid_signature_raises() -> None:
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock(verify_ok=False)):
        gpg = GnuPG(_pars())
        with pytest.raises(GPGVerifyException):
            gpg.verify(b"not a signed message")


def test_verify_detached_cleans_tempfile_on_failure() -> None:
    mock_gpg = _gpg_mock(verify_ok=False)
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=mock_gpg):
        gpg = GnuPG(_pars())
        with pytest.raises(GPGVerifyException):
            gpg.verify(b"data", detach_sign=b"sig")
    args, _ = mock_gpg.verify_data.call_args
    assert isinstance(args[0], str)
    assert not Path(args[0]).exists()


def test_verify_detached_cleans_tempfile_when_verify_data_raises() -> None:
    mock_gpg = _gpg_mock()
    mock_gpg.verify_data.side_effect = AttributeError("broken gnupg")
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=mock_gpg):
        gpg = GnuPG(_pars())
        with pytest.raises(GPGVerifyException):
            gpg.verify(b"data", detach_sign=b"sig")
    args, _ = mock_gpg.verify_data.call_args
    assert isinstance(args[0], str)
    assert not Path(args[0]).exists()


def test_sign_returns_str() -> None:
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock()):
        gpg = GnuPG(_pars())
        assert gpg.sign(b"payload") == "signed-bytes"


@pytest.mark.parametrize(("passphrase", "sent"), [("", None), ("pw", "pw")])
def test_sign_sends_no_passphrase_when_empty(passphrase: str, sent: str | None) -> None:
    """An empty passphrase must not reach gpg, which would read the data's first line as one."""
    mock_gpg = _gpg_mock()
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=mock_gpg):
        pars = _pars()
        pars.gnupg_passphrase = passphrase
        GnuPG(pars).sign(b"Architectures: amd64\n", detach=True)
    assert mock_gpg.sign.call_args.kwargs["passphrase"] == sent


def test_cleartext_body_rejects_malformed_message() -> None:
    with pytest.raises(GPGVerifyException):
        cleartext_body(b"not pgp signed")


_SIGNED = (
    b"-----BEGIN PGP SIGNED MESSAGE-----\n"
    b"Hash: SHA512\n\n"
    b"Origin: example\nSHA256:\n " + b"a" * 64 + b" 1 main/binary-amd64/Packages\n"
    b"-----BEGIN PGP SIGNATURE-----\n\nsig\n-----END PGP SIGNATURE-----\n"
)


def test_cleartext_body_returns_signed_body_only() -> None:
    out = cleartext_body(_SIGNED)
    assert out == b"Origin: example\nSHA256:\n " + b"a" * 64 + b" 1 main/binary-amd64/Packages\n"


def test_cleartext_body_undoes_dash_escaping() -> None:
    data = _SIGNED.replace(b"Origin: example\n", b"Origin: example\n- -dashed\n")
    assert b"\n-dashed\n" in cleartext_body(data)


@pytest.mark.parametrize(
    "data",
    [
        _SIGNED.replace(b"Origin: example\n", b"Origin: example \t\n"),
        _SIGNED.replace(b"Origin: example\n", b"Origin: example\r\r\n"),
        _SIGNED.replace(b"Hash: SHA512\n\n", b"Hash: SHA512\n \n"),
        _SIGNED.replace(b"\n", b"\r\n"),
    ],
    ids=["trailing-space-tab", "trailing-cr", "blank-separator", "crlf"],
)
def test_cleartext_body_canonicalizes_like_gpg(data: bytes) -> None:
    """gpg strips trailing spaces, tabs and CR before hashing, so they are not signed."""
    assert cleartext_body(data) == cleartext_body(_SIGNED)


def test_cleartext_body_accepts_a_message_without_armor_headers() -> None:
    out = cleartext_body(_SIGNED.replace(b"Hash: SHA512\n", b""))
    assert out == b"Origin: example\nSHA256:\n " + b"a" * 64 + b" 1 main/binary-amd64/Packages\n"


@pytest.mark.parametrize(
    "data",
    [
        _SIGNED.replace(b"Origin: example\n", b"Origin: example\n-dashed\n"),
        _SIGNED.replace(b"Hash: SHA512\n", b"not a header\n"),
        _SIGNED.replace(b"Hash: SHA512\n", b"Hash: \n"),
        _SIGNED.replace(b"Hash: SHA512\n", b"X-Foo: bar\n"),
        _SIGNED.replace(b"-----END PGP SIGNATURE-----\n", b""),
    ],
    ids=[
        "unescaped-dash",
        "bad-armor-header",
        "empty-header-value",
        "non-letter-header-name",
        "missing-end",
    ],
)
def test_cleartext_body_rejects_malformed_framework(data: bytes) -> None:
    with pytest.raises(GPGVerifyException):
        cleartext_body(data)


@pytest.mark.parametrize(
    "trailer",
    [
        b"SHA256:\n " + b"e" * 64 + b" 6 main/binary-amd64/Packages\n-----BEGIN X-----\n",
        _SIGNED,
    ],
    ids=["digest-block", "second-message"],
)
def test_verify_rejects_data_after_signature(trailer: bytes) -> None:
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock(verify_ok=True)):
        gpg = GnuPG(_pars())
        with pytest.raises(GPGVerifyException, match="after the signature"):
            gpg.verify(_SIGNED + trailer)
