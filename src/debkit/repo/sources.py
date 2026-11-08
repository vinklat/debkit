"""Class expanding a Debian `Sources` index into one record per source payload file."""

from __future__ import annotations

import logging
import os
import re
from email.parser import Parser
from typing import Any

from txstore import is_unsafe_path

from debkit.repo.hashes import COMPARE_HASH_LIST, parse_hash_block
from debkit.repo.package import FILENAME_FIELD_KEY, Package
from debkit.repo.packages import Packages, PackagesException

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Sources hash-block field -> the digest key `Package.get_digests()` reads.
HASH_FIELDS = {
    "Files": "MD5Sum",
    "Checksums-Sha1": "SHA1",
    "Checksums-Sha256": "SHA256",
    "Checksums-Sha512": "SHA512",
}

# Stanza fields copied onto every payload record of that stanza.
STANZA_FIELDS = ("Package", "Version")


class Sources(Packages):
    """Read-only package container fed by a `Sources` index.

    A `Sources` stanza names several payload files under one `Directory`; each
    becomes a `Package` keyed by its `Filename`, so the sync diff, the purge and
    repair passes, and checksum verification consume source payloads exactly as they
    consume binary ones. `size` counts payload files, not stanzas.

    The index itself is mirrored as an opaque object and is never regenerated, so
    this container refuses every mutation -> [VALIDATION.md](VALIDATION.md#sources-stanzas).
    """

    def _parse_indices(
        self, data: str, modify_rules: list[tuple[str, str, str]] | None = None
    ) -> int:
        """Expand every stanza into one `Package` per payload file.

        Args:
            data (str): Decoded `Sources` index.
            modify_rules (Optional[List[Tuple[str, str, str]]]): Accepted for signature
                parity with `Packages`; a source index is never rewritten, so they are
                ignored.

        Raises:
            PackagesException: If a stanza names no usable `Directory`, an unsafe file
                name, disagreeing sizes, or a payload without a SHA digest.

        Returns:
            int: The number of payload files known after parsing.
        """
        if modify_rules:
            logger.debug("Modify rules do not apply to a read-only Sources index.")

        for block in re.split(r"\n{2,}", data):
            if not block.strip():
                continue

            for package in self._expand_stanza(block):
                name = str(package)
                previous = self.packages.get(name)
                if previous is not None and previous.get_digests() != package.get_digests():
                    logger.debug("%s is listed with differing digests across stanzas.", name)
                self.packages[name] = package

        self.size = len(self.packages)
        return self.size

    def _expand_stanza(self, block: str) -> list[Package]:
        """Build one `Package` per file named by a single `Sources` stanza.

        `Package.parse` is not reused: it folds multi-line values onto one line, which
        destroys the hash blocks, and it demands a `Filename` no stanza carries.

        Args:
            block (str): One stanza, without its trailing blank line.

        Raises:
            PackagesException: On a missing or unsafe `Directory`, a file name with a
                path segment, a size that differs between hash blocks, or a payload
                carrying no SHA digest.

        Returns:
            List[Package]: One record per payload file, in first-seen order.
        """
        fields = dict(Parser().parsestr(block).items())

        directory = os.path.normpath(fields.get("Directory", "").strip())
        if not directory or directory == "." or is_unsafe_path(directory):
            raise PackagesException(
                f"no usable Directory in Sources stanza: {fields.get('Package')}"
            )

        stanza = {key: fields[key].strip() for key in STANZA_FIELDS if key in fields}
        records: dict[str, dict[str, Any]] = {}

        for field, algorithm in HASH_FIELDS.items():
            if field not in fields:
                continue

            for hashstr, size, name in parse_hash_block(fields[field]):
                filename = f"{directory}/{name}"
                if "/" in name or is_unsafe_path(filename):
                    raise PackagesException(f"unsafe file name in Sources stanza: {filename}")

                record = records.setdefault(
                    name, {**stanza, FILENAME_FIELD_KEY: filename, "Size": size}
                )
                if record["Size"] != size:
                    raise PackagesException(f"{filename} has a different size in each hash block")
                record[algorithm] = hashstr

        for record in records.values():
            # a payload needs an algorithm comparison keeps: an MD5-only record would
            # compare unequal forever and be recopied on every sync
            # -> [CRYPTO.md](CRYPTO.md#checksums)
            if not any(algorithm in record for algorithm in COMPARE_HASH_LIST):
                raise PackagesException(f"{record[FILENAME_FIELD_KEY]} carries no SHA digest")

        return [Package(fields=record) for record in records.values()]

    def save(self) -> dict[str, dict[str, str]] | None:
        """Write nothing: the mirrored `Sources` object is the only source of truth.

        Returns:
            Optional[Dict[str, Dict[str, str]]]: Always `None`, the value `Component.save`
                reads as "nothing was written".
        """
        return None

    def insert(self, package: Package) -> None:
        """Refuse: a `Sources` index is mirrored, never composed.

        Args:
            package (Package): Ignored.

        Raises:
            PackagesException: Always.
        """
        raise PackagesException("Sources index is read-only")

    def remove(self, fields: dict[str, re.Pattern[str]]) -> set[str]:
        """Remove nothing: a `Sources` index is mirrored, never composed.

        Returning the base contract's "nothing removed" lets a caller iterate every
        container without knowing which one is read-only.

        Args:
            fields (Dict[str, re.Pattern[str]]): Ignored.

        Returns:
            Set[str]: Always empty.
        """
        return set()
