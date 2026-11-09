"""Real repository trees on disk, for tests that read them through `StorageFS`.

Each dist is a componented `main/binary-amd64` layout with a gzipped `Packages` index whose
digest its root `Release` records, and one payload per package whose bytes are `deb <version>`
→ [TESTING.md](../../TESTING.md#harness-and-gates).
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

DATE = "Tue, 01 Sep 2026 00:00:00 UTC"


def payload_path(name: str, version: str, component: str = "main") -> str:
    """The pool path a package of `name` and `version` in `component` is written to."""
    return f"pool/{component}/{name[0]}/{name}/{name}_{version}_amd64.deb"


def payload_bytes(version: str) -> bytes:
    """The bytes of a payload of `version`."""
    return f"deb {version}".encode()


def write_dist(
    base: Path,
    dist: str = "bookworm",
    packages: dict[str, str] | None = None,
    *,
    date: str = DATE,
    detached_signature: bool = False,
    indices: dict[str, bytes | None] | None = None,
    component: str = "main",
) -> None:
    """Write one dist of `packages` (name → version) under `base`, payloads included.

    Args:
        base: The repository root.
        dist: The dist, written under `dists/<dist>/`.
        packages: Package name → version; defaults to `foo` at version `1`.
        date: The root Release `Date`.
        detached_signature: Also write a placeholder `Release.gpg`.
        indices: More Release-listed indices, key → content; None lists one the dist does not
            publish, recorded with the digest of empty content.
        component: The one component, which may hold `/`; defaults to `main`.
    """
    stanzas: list[str] = []
    files: dict[str, bytes] = {}
    for name, version in (packages or {"foo": "1"}).items():
        deb, data = payload_path(name, version, component), payload_bytes(version)
        files[deb] = data
        stanzas.append(
            f"Package: {name}\nVersion: {version}\nArchitecture: amd64\nFilename: {deb}\n"
            f"Size: {len(data)}\nSHA256: {hashlib.sha256(data).hexdigest()}\n"
        )
    listed: dict[str, bytes | None] = {
        f"{component}/binary-amd64/Packages.gz": gzip.compress(
            "\n".join(stanzas).encode(), mtime=0
        ),
        **(indices or {}),
    }
    block = ""
    for key, content in listed.items():
        recorded = content if content is not None else b""
        block += f" {hashlib.sha256(recorded).hexdigest()} {len(recorded)} {key}\n"
        if content is not None:
            files[f"dists/{dist}/{key}"] = content
    files[f"dists/{dist}/Release"] = (
        f"Origin: t\nSuite: {dist}\nDate: {date}\nArchitectures: amd64\nComponents: {component}\n"
        f"SHA256:\n{block}"
    ).encode()
    if detached_signature:
        files[f"dists/{dist}/Release.gpg"] = b"signature"
    for path, data in files.items():
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        (base / path).write_bytes(data)
