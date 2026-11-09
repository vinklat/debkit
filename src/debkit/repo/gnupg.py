"""GnuPG signatures verify / create."""

from __future__ import annotations

import logging
import os
import re
import tempfile

import gnupg

from debkit._env import child_env

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


CLEARSIGN_BEGIN = "-----BEGIN PGP SIGNED MESSAGE-----"
SIGNATURE_BEGIN = "-----BEGIN PGP SIGNATURE-----"
SIGNATURE_END = "-----END PGP SIGNATURE-----"
ARMOR_HEADER_RE = re.compile(r"[A-Za-z]+: ")
# No gpg run fetches, locates, or imports a key, whatever a config file says; for verify this
# backs up `VERIFY_ARGS`, which already skips gpg.conf -> [CRYPTO.md](CRYPTO.md#gpg).
GPG_OPTIONS = ("--no-auto-key-retrieve", "--no-auto-key-locate", "--no-auto-key-import")
# Verify reads no gpg.conf, whose `keyring` lines would add keys that authenticate; signing still
# reads it for `default-key` -> [CRYPTO.md](CRYPTO.md#gpg).
VERIFY_ARGS = ("--no-options",)


class GPGVerifyException(Exception):
    """Exception raised for errors in verify the repository via GPG or checksum."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Repo verify: {message}")


def cleartext_body(data: bytes) -> bytes:
    """Get the signed body of a cleartext signed message without checking its signature.

    gpg authenticates only the body between the armor headers and the signature,
    and hashes each line with trailing spaces, tabs and CR stripped. Every line is
    canonicalized the same way before parsing, so no unsigned byte reaches the
    caller and nothing may follow the signature -> [CRYPTO.md](CRYPTO.md#gpg).
    Works on bytes like `canonical_text`, so the body keeps its encoding and each
    caller decodes it as it decodes an unsigned file.

    Args:
        data: Signed message

    Raises:
        GPGVerifyException: Signed message is not in correct format, has invalid
            armor headers, an unescaped dash line, or data after the signature.

    Returns:
        The canonical signed body, dash-escaping undone, each line ending in a newline.
    """
    # latin-1 maps every byte to one character and back, so no input fails to decode
    lines = [line.rstrip(" \t\r") for line in data.decode("latin-1").split("\n")]
    if lines[0] != CLEARSIGN_BEGIN:
        raise GPGVerifyException("Signed message is not in correct format.")

    try:
        body_start = lines.index("", 1) + 1
        sig_start = lines.index(SIGNATURE_BEGIN, body_start)
        sig_end = lines.index(SIGNATURE_END, sig_start + 1)
    except ValueError:
        raise GPGVerifyException("Signed message is not in correct format.") from None

    if not all(ARMOR_HEADER_RE.match(line) for line in lines[1 : body_start - 1]):
        raise GPGVerifyException("Signed message has invalid armor headers.")
    if any(line.strip() for line in lines[sig_end + 1 :]):
        raise GPGVerifyException("Signed message has unsigned data after the signature.")

    body: list[str] = []
    for line in lines[body_start:sig_start]:
        if line.startswith("- "):
            line = line[2:]
        elif line.startswith("-"):
            raise GPGVerifyException("Signed message has an unescaped dash line.")
        body.append(f"{line}\n")

    return "".join(body).encode("latin-1")


def canonical_text(data: bytes) -> bytes:
    """Canonicalize text the way `cleartext_body` returns a signed body.

    Trailing spaces, tabs and CR are stripped from every line and each line ends in a
    newline, so a detached `Release` compares equal to the body of an `InRelease`
    clearsigned from it -> [CRYPTO.md](CRYPTO.md#gpg). Works on bytes, so a Release in
    any ASCII-compatible encoding compares alike.

    Args:
        data: Text to canonicalize.

    Returns:
        The canonical text, each line ending in a newline.
    """
    lines = [line.rstrip(b" \t\r") for line in data.split(b"\n")]
    if lines[-1] == b"":
        lines.pop()
    return b"".join(line + b"\n" for line in lines)


class GnuPG:
    """Object for GPG signatures operations.

    Attributes:
        gnupg: The python-gnupg `GPG` object.
        passphrase: The passphrase for the signing key; None when empty.
        sign_key: The key that signs, a fingerprint, key ID, or user ID; None leaves the
            choice to gpg.
    """

    def __init__(
        self, home: str, keyring: str, passphrase: str | None, sign_key: str | None = None
    ) -> None:
        """Open the keyring `keyring` in the GnuPG home `home`.

        Args:
            home: GnuPG home directory; a leading `~` is expanded, as a shell would.
            keyring: Keyring file, relative to `home`.
            passphrase: Passphrase of the signing key; empty or None sends none.
            sign_key: The key that signs; empty or None leaves the choice to gpg
                -> [CRYPTO.md](CRYPTO.md#gpg).

        Raises:
            GPGVerifyException: If the keyring cannot be opened or the gpg binary cannot be run.
        """
        try:
            self.gnupg = gnupg.GPG(
                gnupghome=os.path.expanduser(home),
                keyring=keyring,
                options=list(GPG_OPTIONS),
                env=child_env(),
            )
        except ValueError as exc:
            raise GPGVerifyException(f"Keyring error. {exc}") from exc
        except OSError as exc:
            raise GPGVerifyException(f"Cannot run gpg; is GnuPG installed? {exc}") from exc

        # An empty string would make python-gnupg pass `--passphrase-fd 0` without writing a
        # passphrase, so gpg reads the first line of the signed data as one and drops it.
        self.passphrase: str | None = passphrase or None
        self.sign_key: str | None = sign_key or None

    def verify(self, data: bytes, detach_sign: bytes | None = None) -> bytes:
        """Verifying GPG signature.

        Args:
            data: Signed message
            detach_sign: Detached signature. If None, data should be from InRelease file. Defaults
                to None.

        Raises:
            GPGVerifyException: GPG not valid signature!

        Returns:
            Original text
        """
        if detach_sign is None:
            # data should be from InRelease
            verified = self.gnupg.verify(data, extra_args=list(VERIFY_ARGS))
        else:
            # python-gnupg's `verify_data` reads the detached signature from a path, so it is
            # staged in a file closed before gpg opens it and removed when the context exits,
            # a failed write included
            with tempfile.NamedTemporaryFile(suffix=".gpg", delete_on_close=False) as tmp:
                tmp.write(detach_sign)
                tmp.close()
                try:
                    verified = self.gnupg.verify_data(tmp.name, data, extra_args=list(VERIFY_ARGS))
                except AttributeError as exc:
                    raise GPGVerifyException("GPG gnupg_home data error.") from exc

        if not verified:
            # the `gnupg` logger is held quiet at debug, so the reason is logged here
            logger.debug("gpg verification failed: %s", getattr(verified, "status", None))
            raise GPGVerifyException("GPG not valid signature!")

        logger.info("GPG verified. Signed by %s", verified.username)

        if detach_sign is None:
            return cleartext_body(data)

        return data

    def sign(self, data: bytes, detach: bool = False) -> str:
        """Create GPG signature.

        Args:
            data: Data to sign.
            detach: Detach signature. Defaults to False.

        Returns:
            GPG signature; empty when gpg created none, e.g. on a wrong passphrase.
        """
        if detach:
            logger.info("Generate detached GPG signature.")
        else:
            logger.info("Generate GPG signed data.")

        # python-gnupg's `keyid` is gpg's `--default-key`, which falls back to another key when
        # the named one is unusable; `--local-user` signs with that key or with none
        extra_args = ["--local-user", self.sign_key] if self.sign_key else None
        signed_data = self.gnupg.sign(
            data, detach=detach, passphrase=self.passphrase, extra_args=extra_args
        )
        # a failed clearsign still prints the armor header and body, so the text alone does
        # not tell a signature; python-gnupg is true only once gpg reported one created
        if not signed_data:
            logger.debug("gpg created no signature: %s", getattr(signed_data, "status", None))
            return ""
        return str(signed_data)
