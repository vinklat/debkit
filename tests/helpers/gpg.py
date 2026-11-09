"""Throwaway GnuPG homes for tests that run the real gpg binary.

Each home holds one generated ed25519 signing key and a `trustedkeys.gpg` keyring with its public
half, the layout `GnuPG` opens → [TESTING.md](../../TESTING.md#harness-and-gates). A home never
touches the user's own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from helpers.binaries import requires_binary

KEYRING = "trustedkeys.gpg"

requires_gpg = requires_binary("gpg")


def _gpg(home: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", *args],
        input=data,
        check=True,
        capture_output=True,
    ).stdout


def make_gpg_home(home: Path, name: str, passphrase: str = "") -> Path:
    """Create `home` with one signing key for `name`, locked by `passphrase` when given.

    The agent caches no passphrase, so a wrong one fails even after a right one.
    """
    home.mkdir(mode=0o700, parents=True)
    (home / "gpg-agent.conf").write_text(
        "allow-loopback-pinentry\ndefault-cache-ttl 0\nmax-cache-ttl 0\n"
    )
    return add_signing_key(home, name, passphrase)


def add_signing_key(home: Path, name: str, passphrase: str = "") -> Path:
    """Generate another signing key for `name` in `home` and trust every key it holds."""
    _gpg(
        home,
        "--pinentry-mode",
        "loopback",
        "--passphrase",
        passphrase,
        "--quick-gen-key",
        f"debkit {name} <{name}@example.invalid>",
        "ed25519",
        "sign",
        "never",
    )
    _gpg(
        home, "--no-default-keyring", "--keyring", KEYRING, "--import", data=_gpg(home, "--export")
    )
    return home


def export_public_key(home: Path) -> bytes:
    """The armored public keys `home` holds, as a keyserver would serve them."""
    return _gpg(home, "--armor", "--export")


def import_keys(home: Path, keyring: str, keys: bytes) -> None:
    """Import armored `keys` into the keyring file `keyring` of `home`."""
    _gpg(home, "--no-default-keyring", "--keyring", keyring, "--import", data=keys)


def sign_with_key_block(home: Path, data: bytes) -> bytes:
    """Clearsign `data` with the key of `home`, embedding that key in the signature.

    The gpg CLI exits non-zero when it creates no signature, so a failed sign fails here.
    """
    return _gpg(home, "--clearsign", "--include-key-block", data=data)


def stop_gpg_agent(home: Path) -> None:
    """Stop the agent and dirmngr `home` started, so no process outlives the test session."""
    subprocess.run(
        ["gpgconf", "--homedir", str(home), "--kill", "all"], check=False, capture_output=True
    )
