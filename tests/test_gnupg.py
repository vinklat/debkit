"""Coverage for `debkit.repo.gnupg` that a real gpg cannot produce on demand.

These tests mock ``gnupg.GPG`` for the failures and bookkeeping around it: a keyring or
binary that does not open, tempfile cleanup, a sign that creates nothing, the passphrase
handling, and the pure `cleartext_body` parsing. What only gpg decides, a good or a bad
signature, is in ``tests/test_gnupg_real.py`` on throwaway homes.
"""

from __future__ import annotations

import errno
import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest

from debkit.repo.gnupg import GPG_OPTIONS, GnuPG, GPGVerifyException, cleartext_body


def _gnupg(passphrase: str | None = "pw") -> GnuPG:
    return GnuPG(".gnupg", "trustedkeys.gpg", passphrase)


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
    with (
        patch("debkit.repo.gnupg.gnupg.GPG", side_effect=ValueError("bad keyring")),
        pytest.raises(GPGVerifyException),
    ):
        _gnupg()


def test_init_opens_the_given_home_and_keyring() -> None:
    with patch("debkit.repo.gnupg.gnupg.GPG") as gpg_cls:
        _gnupg()
    gpg_cls.assert_called_once_with(
        gnupghome=".gnupg", keyring="trustedkeys.gpg", options=GPG_OPTIONS, env=ANY
    )


def test_init_runs_gpg_without_debkit_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gpg verifies a mirror's data, so it never holds the passphrase or a storage secret.

    A stand-in `gpg` on `PATH` fails python-gnupg's version probe when it sees either, so the
    environment the child really gets is observed, not only the one debkit passes on.
    """
    script = tmp_path / "bin" / "gpg"
    script.parent.mkdir()
    script.write_text(
        '#!/bin/sh\n[ -n "$DEBKIT_GNUPG_PASSPHRASE$TXSTORE_S3_SECRET" ] && exit 1\n'
        'echo "gpg (GnuPG) 2.4.4"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(script.parent))
    monkeypatch.setenv("DEBKIT_GNUPG_PASSPHRASE", "x")
    monkeypatch.setenv("TXSTORE_S3_SECRET", "x")
    (tmp_path / "home").mkdir()

    GnuPG(str(tmp_path / "home"), "trustedkeys.gpg", None)


def test_init_expands_a_home_under_the_user_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """gpg does not expand `~`, so the default `~/.gnupg` reaches it as an absolute path."""
    monkeypatch.setenv("HOME", "/home/builder")
    with patch("debkit.repo.gnupg.gnupg.GPG") as gpg_cls:
        GnuPG("~/.gnupg", "trustedkeys.gpg", None)
    gpg_cls.assert_called_once_with(
        gnupghome="/home/builder/.gnupg", keyring="trustedkeys.gpg", options=GPG_OPTIONS, env=ANY
    )


def test_init_missing_gpg_binary_raises() -> None:
    """python-gnupg raises OSError when it cannot run the gpg binary."""
    with (
        patch("debkit.repo.gnupg.gnupg.GPG", side_effect=OSError("Unable to run gpg (gpg)")),
        pytest.raises(GPGVerifyException, match="Cannot run gpg"),
    ):
        _gnupg()


def test_verify_inrelease_returns_the_signed_body() -> None:
    inrelease = (
        b"-----BEGIN PGP SIGNED MESSAGE-----\n"
        b"Hash: SHA256\n\nOrigin: example\nCodename: bookworm\n\n"
        b"-----BEGIN PGP SIGNATURE-----\n...\n-----END PGP SIGNATURE-----\n"
    )
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock(verify_ok=True)):
        gpg = _gnupg()
        out = gpg.verify(inrelease)
    assert out == b"Origin: example\nCodename: bookworm\n\n"


def test_verify_detached_cleans_tempfile_on_failure() -> None:
    mock_gpg = _gpg_mock(verify_ok=False)
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=mock_gpg):
        gpg = _gnupg()
        with pytest.raises(GPGVerifyException):
            gpg.verify(b"data", detach_sign=b"sig")
    args, _ = mock_gpg.verify_data.call_args
    assert isinstance(args[0], str)
    assert not Path(args[0]).exists()


def test_verify_failure_logs_the_gpg_status_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """The `gnupg` logger is held quiet at debug, so the failure carries gpg's status."""
    mock_gpg = _gpg_mock(verify_ok=False)
    mock_gpg.verify.return_value.status = "signature bad"
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=mock_gpg):
        gpg = _gnupg()
        with (
            caplog.at_level(logging.DEBUG, logger="debkit.repo.gnupg"),
            pytest.raises(GPGVerifyException),
        ):
            gpg.verify(b"-----BEGIN PGP SIGNED MESSAGE-----\n")

    assert "gpg verification failed: signature bad" in caplog.text


def test_verify_detached_returns_the_data() -> None:
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock(verify_ok=True)):
        assert _gnupg().verify(b"data", detach_sign=b"sig") == b"data"


def test_verify_detached_cleans_tempfile_when_verify_data_raises() -> None:
    mock_gpg = _gpg_mock()
    mock_gpg.verify_data.side_effect = AttributeError("broken gnupg")
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=mock_gpg):
        gpg = _gnupg()
        with pytest.raises(GPGVerifyException):
            gpg.verify(b"data", detach_sign=b"sig")
    args, _ = mock_gpg.verify_data.call_args
    assert isinstance(args[0], str)
    assert not Path(args[0]).exists()


def test_sign_returns_nothing_when_gpg_created_no_signature() -> None:
    """A failed clearsign still prints its armor header and body (wrong passphrase)."""

    class FailedSign:
        """python-gnupg's result: false without a created signature, text all the same."""

        def __bool__(self) -> bool:
            return False

        def __str__(self) -> str:
            return "-----BEGIN PGP SIGNED MESSAGE-----\n\nbody\n"

    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock()) as mock_gpg:
        mock_gpg.return_value.sign.return_value = FailedSign()
        assert _gnupg(None).sign(b"payload") == ""


@pytest.mark.parametrize(("passphrase", "sent"), [("", None), (None, None), ("pw", "pw")])
def test_sign_sends_no_passphrase_when_empty(passphrase: str | None, sent: str | None) -> None:
    """An empty passphrase must not reach gpg, which would read the data's first line as one."""
    mock_gpg = _gpg_mock()
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=mock_gpg):
        _gnupg(passphrase).sign(b"Architectures: amd64\n", detach=True)
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


def test_cleartext_body_keeps_a_body_that_is_not_utf_8() -> None:
    """The body keeps its bytes, so a latin-1 `InRelease` reads as a latin-1 `Release` does."""
    out = cleartext_body(_SIGNED.replace(b"Origin: example", b"Origin: Caf\xe9"))
    assert out.startswith(b"Origin: Caf\xe9\n")


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
        gpg = _gnupg()
        with pytest.raises(GPGVerifyException, match="after the signature"):
            gpg.verify(_SIGNED + trailer)


def test_verify_detached_cleans_tempfile_when_staging_the_signature_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk fails the write itself, before gpg is ever asked."""
    real = tempfile.NamedTemporaryFile

    def full_disk(*args: Any, **kwargs: Any) -> Any:
        staged = real(*args, **kwargs)
        failing = MagicMock(side_effect=OSError(errno.ENOSPC, "No space left on device"))
        monkeypatch.setattr(staged, "write", failing)
        return staged

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr("debkit.repo.gnupg.tempfile.NamedTemporaryFile", full_disk)
    with patch("debkit.repo.gnupg.gnupg.GPG", return_value=_gpg_mock()):
        gpg = _gnupg()
        with pytest.raises(OSError, match="No space left"):
            gpg.verify(b"data", detach_sign=b"sig")

    assert list(tmp_path.iterdir()) == []
