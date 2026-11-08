"""GnuPG signatures verify / create."""

import logging
import re
import tempfile
from pathlib import Path

import gnupg

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


CLEARSIGN_BEGIN = "-----BEGIN PGP SIGNED MESSAGE-----"
SIGNATURE_BEGIN = "-----BEGIN PGP SIGNATURE-----"
SIGNATURE_END = "-----END PGP SIGNATURE-----"
ARMOR_HEADER_RE = re.compile(r"[A-Za-z]+: ")


class GPGVerifyException(Exception):
    """Exception raised for errors in verify the repository via GPG or checksum.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Repo verify: {message}")


def cleartext_body(data: bytes) -> bytes:
    """Get the signed body of a cleartext signed message without checking its signature.

    gpg authenticates only the body between the armor headers and the signature,
    and hashes each line with trailing spaces, tabs and CR stripped. Every line is
    canonicalized the same way before parsing, so no unsigned byte reaches the
    caller and nothing may follow the signature -> [CRYPTO.md](CRYPTO.md#gpg).

    Args:
        data (bytes): Signed message

    Raises:
        GPGVerifyException: Signed message is not in correct format, has invalid
            armor headers, an unescaped dash line, or data after the signature.

    Returns:
        bytes: The canonical signed body, dash-escaping undone, each line ending in a
            newline.
    """
    lines = [line.rstrip(" \t\r") for line in data.decode("utf-8").split("\n")]
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

    return "".join(body).encode("utf-8")


def canonical_text(data: bytes) -> bytes:
    """Canonicalize text the way `cleartext_body` returns a signed body.

    Trailing spaces, tabs and CR are stripped from every line and each line ends in a
    newline, so a detached `Release` compares equal to the body of an `InRelease`
    clearsigned from it -> [CRYPTO.md](CRYPTO.md#gpg). Works on bytes, so a Release in
    any ASCII-compatible encoding compares alike.

    Args:
        data (bytes): Text to canonicalize.

    Returns:
        bytes: The canonical text, each line ending in a newline.
    """
    lines = [line.rstrip(b" \t\r") for line in data.split(b"\n")]
    if lines[-1] == b"":
        lines.pop()
    return b"".join(line + b"\n" for line in lines)


class GnuPG:
    """Object for GPG signatures operations.

    Attributes:
        gpg (gnupg.GPG): The GPG object.
        passphrase (str | None): The passphrase for the signing key; None when empty.
    """

    def __init__(self, home: str, keyring: str, passphrase: str | None) -> None:
        """Open the keyring `keyring` in the GnuPG home `home`.

        Args:
            home (str): GnuPG home directory.
            keyring (str): Keyring file, relative to `home`.
            passphrase (str | None): Passphrase of the signing key; empty or None sends none.

        Raises:
            GPGVerifyException: If the keyring cannot be opened or the gpg binary cannot be run.
        """
        try:
            self.gnupg = gnupg.GPG(gnupghome=home, keyring=keyring)
        except ValueError as exc:
            raise GPGVerifyException(f"Keyring error. {exc}") from exc
        except OSError as exc:
            raise GPGVerifyException(f"Cannot run gpg; is GnuPG installed? {exc}") from exc

        # An empty string would make python-gnupg pass `--passphrase-fd 0` without writing a
        # passphrase, so gpg reads the first line of the signed data as one and drops it.
        self.passphrase: str | None = passphrase or None

    def verify(self, data: bytes, detach_sign: bytes | None = None) -> bytes:
        """Verifying GPG signature.

        Args:
            data (bytes): Signed message
            detach_sign (Optional[bytes], optional): Detached signature.
                If None, data should be from InRelease file. Defaults to None.

        Raises:
            GPGVerifyException: GPG not valid signature!

        Returns:
            bytes: Original text
        """
        if detach_sign is None:
            # data should be from InRelease
            verified = self.gnupg.verify(data)
        else:
            # The python-gnupg ``verify_data`` API requires the detached
            # signature to live on disk; use a ``NamedTemporaryFile`` that
            # cleans itself up even when verification raises.
            with tempfile.NamedTemporaryFile(suffix=".gpg", delete=False) as tmp:
                tmp.write(detach_sign)
                tmp_sig_path = tmp.name
            try:
                try:
                    verified = self.gnupg.verify_data(tmp_sig_path, data)
                except AttributeError as exc:
                    raise GPGVerifyException("GPG gnupg_home data error.") from exc
            finally:
                Path(tmp_sig_path).unlink(missing_ok=True)

        if not verified:
            raise GPGVerifyException("GPG not valid signature!")

        logger.info("GPG verified. Signed by %s", verified.username)

        if detach_sign is None:
            return cleartext_body(data)

        return data

    def sign(self, data: bytes, detach: bool = False) -> str:
        """Create GPG signature.

        Args:
            data (bytes): Data to sign.
            detach (bool, optional): Detach signature. Defaults to False.

        Returns:
            str: GPG signature.
        """
        if detach:
            logger.info("Generate detached GPG signature.")
        else:
            logger.info("Generate GPG signed data.")

        signed_data = self.gnupg.sign(data, detach=detach, passphrase=self.passphrase)
        return str(signed_data)
