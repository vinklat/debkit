"""Class containing Debian Release file data."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from email.parser import Parser
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

from debian.deb822 import Deb822
from txstore import StorageNotFoundException, calc_digests, compare_digests

from debkit.repo.compress import SizeLimitError, read_capped
from debkit.repo.gnupg import canonical_text, cleartext_body
from debkit.repo.hashes import (
    SUPPORT_HASH_LIST,
    ChecksumException,
    HexDigests,
    parse_hash_block,
)

if TYPE_CHECKING:
    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.params import RepoParams


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Fields for which bare unindented continuation tokens are folded onto the
# previous line.  Hash-block fields (MD5Sum, SHA1, SHA256, SHA512, …) are
# intentionally excluded: folding a stray token into a hash block shifts the
# (digest, size, filename) triple alignment and silently maps wrong digests.
_CONTINUATION_FIELDS = frozenset({"Architectures", "Components"})

# Largest Release file read when no signed Release records its size; it is read before its
# signature is checked -> [VALIDATION.md](VALIDATION.md#object-sizes)
MAX_RELEASE_BYTES = 16 * 1024 * 1024

# Every root Release file a load fetches and records digests for; no index stanza may vouch
# for one -> [CRYPTO.md](CRYPTO.md#verification-order)
ROOT_RELEASE_KEYS = frozenset({"Release", "InRelease", "Release.gpg"})
_HASH_BLOCK_FIELDS = frozenset(SUPPORT_HASH_LIST)


class SignatureMode(Enum):
    """How a root Release treats its signatures -> [CRYPTO.md](CRYPTO.md#gpg).

    Attributes:
        VERIFY_AND_SIGN: GPG verification on with a `GnuPG`: a load verifies every
            signature present and a save signs.
        READ_SIGNED_UNVERIFIED: GPG verification on without a `GnuPG`, as the `sync`
            target: a load reads the signed form unverified and a save is refused.
        OFF: GPG verification off: a load reads either form unverified and a save writes
            unsigned, removing the stale signatures.
    """

    VERIFY_AND_SIGN = "verify-and-sign"
    READ_SIGNED_UNVERIFIED = "read-signed-unverified"
    OFF = "off"


class IndexEntry(NamedTuple):
    """One index file listed in the digest block of a Release file.

    Attributes:
        key: Path as the Release lists it, relative to the layout prefix.
        path: Full storage path of the same object.
        digests: Digests and `Size` the Release records for it.
    """

    key: str
    path: str
    digests: dict[str, str]


class ReleaseReadException(Exception):
    """Exception raised when a Release cannot be read, parsed, or verified."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Release read: {message}")


class ReleaseNotFoundException(ReleaseReadException):
    """Exception raised when a repository has neither a root `Release` nor an `InRelease`.

    The only read failure that means "no repository here": every other one is a
    repository that exists but cannot be trusted or parsed.
    """


class ReleaseWriteException(Exception):
    """Exception raised when a Release cannot be signed."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Release write: {message}")


class ReleaseFreshnessException(Exception):
    """Exception raised when a source Release is stale, expired, or badly dated."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Release freshness: {message}")


# Zone tokens apt accepts in a Release date; any other name would silently read as UTC
_RELEASE_DATE_ZONES = frozenset({"UTC", "GMT", "UT", "Z"})
_NUMERIC_ZONE = re.compile(r"[+-]\d{4}")

# a continuation line of only blanks: apt continues the value over it, but a deb822 reader
# ends the stanza there, so it is written as the ` .` that stands for an empty line
_BLANK_CONTINUATION = re.compile(r"^[ \t]+$", re.MULTILINE)


def parse_release_date(value: str) -> datetime:
    """Parse a Release date field such as `Date` or `Valid-Until`.

    The zone must be `UTC`, `GMT`, `UT`, `Z`, or a numeric offset; `-0000` is read as
    UTC -> [VALIDATION.md](VALIDATION.md#release-dates).

    Args:
        value: Raw field value.

    Raises:
        ValueError: If *value* is not an RFC 2822 date with an accepted zone.

    Returns:
        The zone-aware date.
    """
    tokens = value.split()
    zone = tokens[-1] if tokens else ""
    if zone.upper() not in _RELEASE_DATE_ZONES and not _NUMERIC_ZONE.fullmatch(zone):
        raise ValueError(f"not an RFC 2822 date with a UTC or numeric zone: {value!r}")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        raise ValueError(f"not an RFC 2822 date: {value!r}") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class Release(HexDigests):
    """Object to store Debian Release file data.

    Attributes:
        storage: The storage class of the repo.
        params: The input parameters of the repo.
        fields: The fields of the release.
        filename: The filename of the release.
        found_indices_files: List of all found Release files.
        hashfuns: Available hexdigest algorithms in data.
        hash: Hexdigest of a file content (for changes detection).
        modified: Whether `save` has anything to write: set by a setup, or by whoever changes
            a loaded Release.
    """

    def __init__(self, storage: Storage, params: RepoParams) -> None:
        """Initialize Release object.

        Args:
            storage: The storage class of the repo.
            params: The input parameters of the repo.
        """
        self.storage = storage
        self.params = params
        super().__init__()
        self.reset()

    def reset(self) -> None:
        """Set everything a load or a setup fills to its empty value.

        `__init__` runs it too, so an override resets only its own fields and reads no state
        its subclass sets after `super().__init__()`.
        """
        self.modified = False
        self.fields: dict[str, str] = {}
        self.digests = {}
        self.filename: str | None = None
        self.found_indices_files: list[str] = []
        self.hashfuns: list[str] = []
        self.hash = ""

    def __repr__(self) -> str:
        """Return string representation of the Release object."""
        return "<empty>" if self.filename is None else self.filename

    @staticmethod
    def _fix_unindented_continuations(text: str) -> str:
        """Re-join bare continuation tokens that lack a leading space.

        Works around non-RFC-2822 Release files.

        RFC 2822 requires multi-value continuation lines to begin with whitespace (space or tab).
        Some repositories (e.g. ProxySQL) instead emit bare tokens on their own line without any
        leading whitespace:

            Architectures: amd64
            arm64
            Components: main

        Python's ``email.parser.Parser`` treats ``arm64`` as a new (invalid) field name and
        consumes ``Components: main`` as its value, silently dropping ``Components`` from the
        parsed result.  Without ``Components``, ``get_components()`` falls through to the
        flat-repo fallback (``yield "/"``), which then raises
        ``ComponentException: Cannot load /: <arch>``.

        This method detects such lines — non-blank, no leading whitespace, no ``:`` — and folds
        them back onto the previous line as space-separated tokens before the parser runs,
        **but only when the active field is in** ``_CONTINUATION_FIELDS``
        (``Architectures`` and ``Components``).  Hash-block fields (``MD5Sum``, ``SHA256``,
        etc.) are excluded: folding a stray token into a hash block shifts the
        ``(digest, size, filename)`` triple alignment and silently maps wrong digests to files
        without raising any error.
        It is a no-op for fully conforming Release files.
        """
        lines = text.splitlines(keepends=True)
        fixed: list[str] = []
        warned = False
        current_field: str | None = None
        for line in lines:
            stripped = line.rstrip("\n\r")
            if stripped and not stripped[0].isspace() and ":" in stripped:
                # normal header line — track which field we are in
                current_field = stripped.split(":", 1)[0].strip()
            elif stripped and not stripped[0].isspace() and ":" not in stripped:
                # bare continuation token — fold only for known safe simple-value fields
                if fixed and current_field in _CONTINUATION_FIELDS:
                    fixed[-1] = fixed[-1].rstrip("\n\r") + " " + stripped + "\n"
                    if not warned:
                        logger.warning(
                            "Release: non-standard unindented continuation lines detected "
                            "and normalised"
                        )
                        warned = True
                    continue
                if current_field in _HASH_BLOCK_FIELDS:
                    # Discard stray bare tokens inside hash blocks.  If kept, the email
                    # parser treats the following indented hash entry as the stray token's
                    # continuation, silently emptying the hash field.
                    continue
            fixed.append(line)
        return "".join(fixed)

    def parse(self, enc_data: bytes) -> None:
        """Parse Release file fields from headers format.

        Args:
            enc_data: The encoded data to parse.

        Note:
            Some repositories produce non-RFC-2822-compliant Release files where multi-value
            fields span several lines without the required leading whitespace on continuation
            lines.  ``_fix_unindented_continuations`` normalises such lines before the email
            parser runs; see that method's docstring for the full failure chain this prevents.
        """
        try:
            data = enc_data.decode("utf-8")

        # because of possible encoding errors in some repositories
        # 'utf-8' codec can't decode byte 0x8b in position 1: invalid start byte
        except UnicodeDecodeError:
            data = enc_data.decode("latin-1")

        data = re.sub("\n\n", "\n", data)  # fix - remove blank lines
        data = self._fix_unindented_continuations(data)
        parser = Parser()
        parsed = parser.parsestr(data)
        # `dict(parsed)` would keep the first of duplicate fields; `items()` lets the last win
        self.fields = dict(parsed.items())

        # a reload replaces the manifest: a key or algorithm the new file drops must
        # not survive from the previous parse
        self.digests = {}
        self.hashfuns = []
        for hashfun in list(SUPPORT_HASH_LIST):
            if hashfun in self.fields:
                self.hashfuns.append(hashfun)
                for hashstr, size, filename in parse_hash_block(self.fields[hashfun]):
                    if filename not in self.digests:
                        self.update_digests(filename, {"Size": str(size)})
                    self.update_digests(filename, {hashfun: hashstr})
                del self.fields[hashfun]

    def _component_release_path(self, comp: str, arch: str) -> str:
        """Compose the path of a per-component Release.

        One composition for the write and the read alike, so a written Release lands
        where the loader looks -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            comp: The component the Release belongs to.
            arch: The architecture the Release belongs to.

        Returns:
            The storage path of the component Release.
        """
        filename = self.params.indices_path(f"{self.params.arch_dir(arch)}/Release", comp)
        return filename

    @staticmethod
    def _require_coordinates(comp: str, arch: str) -> None:
        """Refuse an empty component or architecture for a per-component Release.

        Either one empty would compose a path over the root Release or under
        `binary-/` -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Raises:
            ValueError: `comp` or `arch` is empty.
        """
        if not comp or not arch:
            raise ValueError(f"component Release needs a component and an arch: {comp!r} {arch!r}")

    @property
    def is_loaded(self) -> bool:
        """Whether a load read Release bytes that a reset has not cleared since.

        Returns:
            True after a successful `load`, `load_component`, or parse of loaded bytes.
        """
        return bool(self.hash)

    def _parse_loaded(self, data: bytes) -> None:
        """Parse loaded Release bytes and record their hash for change detection."""
        if data:
            self.parse(data)
            self.hash = hashlib.sha256(data).hexdigest()
        if self.hashfuns:
            logger.debug("Available hexdigests: %s", self.hashfuns)

    def load_component(self, comp: str, arch: str, root_release: Release) -> bool:
        """Load the per-component Release of one architecture.

        Args:
            comp: The component to load.
            arch: The architecture to load.
            root_release: The root Release listing its digests, checked when digest verification
                is on.

        Raises:
            ValueError: `comp` or `arch` is empty.
            ChecksumException: Checksum mismatch against the root Release, or a file larger than
                the size the root Release records.
            ReleaseReadException: The file is larger than `MAX_RELEASE_BYTES` and no checked
                root Release records its size.

        Returns:
            True if the Release file was loaded, False when storage lacks it.
        """
        self._require_coordinates(comp, arch)
        filename = self._component_release_path(comp, arch)
        if not self.storage.exists(filename):
            self.filename = None
            return False
        self.filename = filename
        self.found_indices_files = [filename]

        key = self.params.release_key(filename)
        checked = self.params.verify_digests
        declared = root_release.declared_size(key) if checked else None
        try:
            data = read_capped(
                self.storage, filename, MAX_RELEASE_BYTES if declared is None else declared
            )
        except SizeLimitError as exc:
            if declared is None:
                raise ReleaseReadException(f"{exc}.") from exc
            raise ChecksumException(f"{key} is larger than the root Release records.") from exc
        if checked and not root_release.verify_digests(key, calc_digests(data)):
            raise ChecksumException(f"{key} checksum mismatch.")

        self._parse_loaded(data)
        logger.info(
            "Component=%s Storage=%s Release=%s", comp, self.storage.get_type(), self.fields
        )
        return True

    def serialize(self) -> str:
        """Serialize Release file data to headers format.

        Values are written verbatim, as `Package.serialize` writes a stanza: multi-line values
        keep their continuation lines, non-ASCII text stays UTF-8, and nothing is rewrapped;
        only a blank continuation line becomes ` .`
        -> [VALIDATION.md](VALIDATION.md#release-fields).

        Returns:
            The serialized Release file fields.
        """
        fields = {
            field: _BLANK_CONTINUATION.sub(" .", value) for field, value in self.fields.items()
        }
        ret = Deb822(fields).dump().rstrip()

        if self.hashfuns:
            for hashfun in self.hashfuns:
                # a record sits in the blocks its digests cover: a parsed upstream Release
                # may list a file in one block only, and is written back the same way
                hlist = [
                    f"{filesums[hashfun]} {filesums['Size']:>16} {filename}"
                    for filename, filesums in self.digests.items()
                    if hashfun in filesums
                ]
                if hlist:
                    ret += f"\n{hashfun}:\n " + "\n ".join(hlist)

        return ret

    def save(self) -> dict[str, str] | None:
        """Save Release file when it changed.

        A per-component Release is written where it was read and never signed; a root one
        -> `RootRelease`.

        Raises:
            ReleaseWriteException: A root Release cannot be signed.

        Returns:
            The digests of the saved file, or None when nothing was written.
        """
        if self.filename is None:
            return None

        if not self.modified:
            logger.debug("%s not modified.", self.filename)
            return None

        return calc_digests(self._put())

    def _put(self) -> bytes:
        """Write the serialized Release to its file.

        Returns:
            The bytes written.
        """
        assert self.filename is not None, "put without a file"
        enc_data = self.serialize().encode("utf-8")
        logger.info("Save %s...", self.filename)
        self.storage.put(self.filename, enc_data)
        return enc_data

    def dump(self, brief: bool = False) -> dict[str, Any]:
        """Dump Release object to a dict.

        Args:
            brief: Whether to dump a brief version output. Defaults to False.

        Returns:
            A dict representation of the Release object. The dictionary includes the indices
            filenames, attributes, and digests (if not brief).
        """
        return {
            "files": self.found_indices_files,
            "data": self.fields,
            "digests": self.digests if not brief else {},
        }

    def get_indices_paths(self) -> list[str]:
        """Get list of all found indices filenames.

        Returns:
            A list of filenames.
        """
        return self.found_indices_files

    def listed_indices(self) -> list[IndexEntry]:
        """Get every index file the digest block of this Release lists.

        The Release lists index paths relative to the layout prefix; this maps each
        one to its full storage path. Entries are not checked against storage, so a
        path listed but not published by the repository is still returned.

        Returns:
            One entry per digest block record, sorted by key.
        """
        return [
            IndexEntry(key, self.params.indices_path(key), digests)
            for key, digests in sorted(self.digests.items())
        ]

    def date(self, field: str) -> datetime | None:
        """Get a date field of the Release.

        Args:
            field: Field name, `Date` or `Valid-Until`.

        Raises:
            ValueError: If the field is present but malformed.

        Returns:
            The zone-aware date, or None when the field is absent.
        """
        value = self.fields.get(field)
        return None if value is None else parse_release_date(value)

    def is_empty(self) -> bool:
        """Check if Release object is empty.

        Returns:
            True if the Release object is empty, False otherwise.
        """
        return bool(len(self.fields) == 0)

    def setup_component(self, comp: str, arch: str) -> None:
        """Start a new per-component Release of one architecture.

        Args:
            comp: The component.
            arch: The architecture.

        Raises:
            ValueError: `comp` or `arch` is empty.
        """
        self._require_coordinates(comp, arch)
        self.fields = {"Component": comp, "Architecture": arch}
        self._start_file(self._component_release_path(comp, arch))

    def _start_file(self, filename: str) -> None:
        """Point this Release at a new file to write with every supported digest."""
        self.hashfuns = list(SUPPORT_HASH_LIST)
        self.filename = filename
        self.found_indices_files = [filename]
        self.modified = True


class RootRelease(Release):
    """The root Release of a repository: loaded and signed at the layout's root paths.

    A root save writes the layout's `Release` whichever form the load read, then signs
    it or removes its stale signatures -> [CRYPTO.md](CRYPTO.md#gpg).

    Attributes:
        gpg: The GnuPG object, None where none verifies or signs.
        root_digests: Release key → digests of each root Release file as the last load fetched it.
    """

    root_digests: dict[str, dict[str, str]]

    def __init__(self, storage: Storage, params: RepoParams, gpg: GnuPG | None = None) -> None:
        """Initialize RootRelease object.

        Args:
            storage: The storage class of the repo.
            params: The input parameters of the repo.
            gpg: Optional GnuPG object. Defaults to None.
        """
        self.gpg: GnuPG | None = gpg
        super().__init__(storage, params)

    def reset(self) -> None:
        """Reset RootRelease object to defaults, dropping the fetched root digests too."""
        super().reset()
        self.root_digests = {}

    def _put(self) -> bytes:
        """Write the layout's root `Release`, then sign it or drop its stale signatures.

        Raises:
            ReleaseWriteException: In `READ_SIGNED_UNVERIFIED` no `GnuPG` signs it, and
                saving unsigned would drop the signatures it was read with.

        Returns:
            The bytes written.
        """
        mode = self.signature_mode
        if mode is SignatureMode.READ_SIGNED_UNVERIFIED:
            raise ReleaseWriteException(
                "GPG verification is on but no GnuPG signs the root Release; "
                "refusing to save it unsigned"
            )
        self.filename = self.params.indices_path("Release")
        enc_data = super()._put()
        if mode is SignatureMode.VERIFY_AND_SIGN:
            self._sign_gpg(enc_data)
        else:
            self._remove_root_signatures()
        return enc_data

    @property
    def signature_mode(self) -> SignatureMode:
        """How a root load and save treat signatures, from the GPG switch and the `GnuPG`.

        GPG verification also turns signing on -> [CRYPTO.md](CRYPTO.md#verification-switches).

        Returns:
            The mode; the one place `params.verify_gpg` and `gpg` are combined.
        """
        if not self.params.verify_gpg:
            return SignatureMode.OFF
        if self.gpg is None:
            return SignatureMode.READ_SIGNED_UNVERIFIED
        return SignatureMode.VERIFY_AND_SIGN

    def _find_root_files(self) -> tuple[bool, str]:
        """Find the root Release files in storage and pick the one to parse.

        Raises:
            ReleaseNotFoundException: Neither `Release` nor `InRelease` exists.
            ReleaseReadException: Signatures are checked and neither `Release.gpg` nor
                `InRelease` exists.

        Returns:
            Whether to parse `InRelease`, and the file to parse.
        """
        # the layout decides the prefix -> [VALIDATION.md](VALIDATION.md#repository-index-paths)
        release_path = self.params.indices_path("Release")
        release_gpg_path = self.params.indices_path("Release.gpg")
        inrelease_path = self.params.indices_path("InRelease")

        release_path_exists = self.storage.exists(release_path)
        release_gpg_path_exists = self.storage.exists(release_gpg_path)
        inrelease_path_exists = self.storage.exists(inrelease_path)

        # Release or InRelease not found
        if not (release_path_exists or inrelease_path_exists):
            raise ReleaseNotFoundException(f"File not found: {release_path} or {inrelease_path}")

        # a detached signature signs `Release` alone, so without one it is never verified and
        # never mirrored -> [CRYPTO.md](CRYPTO.md#gpg)
        if release_gpg_path_exists and not release_path_exists:
            logger.warning("%s signs no %s. Skipped.", release_gpg_path, release_path)
            release_gpg_path_exists = False

        # an unsigned Release reads as-is when no signature is checked
        # -> [CRYPTO.md](CRYPTO.md#verification-switches)
        mode = self.signature_mode
        if mode is SignatureMode.VERIFY_AND_SIGN and not (
            release_gpg_path_exists or inrelease_path_exists
        ):
            raise ReleaseReadException(
                f"GPG signature file missing: {release_gpg_path} or {inrelease_path}"
            )

        self.found_indices_files = [
            path
            for path, exists in (
                (release_path, release_path_exists),
                (release_gpg_path, release_gpg_path_exists),
                (inrelease_path, inrelease_path_exists),
            )
            if exists
        ]

        # with verification on, a Release without its detached signature is read from InRelease
        # even when no GnuPG checks it here, as the sync target: the verified source reads the
        # same form, so both hash the same body -> [CRYPTO.md](CRYPTO.md#gpg)
        prefers_signed_form = mode is not SignatureMode.OFF and not release_gpg_path_exists
        use_inrelease = inrelease_path_exists and (not release_path_exists or prefers_signed_form)
        return use_inrelease, inrelease_path if use_inrelease else release_path

    def _verify_gpg(self, data: bytes, detach_sign: bytes | None = None) -> bytes:
        """Verify GPG signature.

        Args:
            data: The data to verify.
            detach_sign: The detached signature. Defaults to None.

        Returns:
            The verified data.
        """
        # only reached in `VERIFY_AND_SIGN`, so a missing GnuPG is a caller bug,
        # never an unverified load
        assert self.gpg is not None, "signature check without GnuPG"

        if detach_sign is None:
            logger.debug("Verify signed InRelease...")
            return self.gpg.verify(data)
        logger.debug("Verify detached GPG signature...")
        return self.gpg.verify(data, detach_sign=detach_sign)

    def _load_root_files(self, use_inrelease: bool) -> bytes:
        """Fetch every root Release file once and verify each signature present.

        No digest block lists these files, so their digests are recorded here: a sync
        copy must match the bytes this load verified, not a second fetch of them
        -> [CRYPTO.md](CRYPTO.md#verification-order).

        Args:
            use_inrelease: Whether `InRelease` is the file to parse.

        Raises:
            ChecksumException: Both `InRelease` and `Release` are present and their
                verified contents differ.
            ReleaseReadException: A root Release file is larger than `MAX_RELEASE_BYTES`.

        Returns:
            The verified content of the file to parse.
        """
        raw: dict[str, bytes] = {}
        for path in self.found_indices_files:
            try:
                raw[self.params.release_key(path)] = read_capped(
                    self.storage, path, MAX_RELEASE_BYTES
                )
            except SizeLimitError as exc:
                raise ReleaseReadException(f"{exc}.") from exc
        self.root_digests = {key: calc_digests(data) for key, data in raw.items()}

        if self.signature_mode is SignatureMode.VERIFY_AND_SIGN:
            if "InRelease" in raw:
                raw["InRelease"] = self._verify_gpg(raw["InRelease"])
            if "Release" in raw and "Release.gpg" in raw:
                self._verify_gpg(raw["Release"], detach_sign=raw["Release.gpg"])
            # only the parsed form is checked for freshness, and apt prefers `InRelease`,
            # so a mirrored pair must say the same -> [CRYPTO.md](CRYPTO.md#gpg)
            if (
                "InRelease" in raw
                and "Release" in raw
                and raw["InRelease"] != canonical_text(raw["Release"])
            ):
                raise ChecksumException("Release and InRelease carry different content.")
        elif use_inrelease:
            # unverified, as under `--skip-verify-gpg` or for a sync target, yet the signed
            # body still holds the fields and the Date a source must not undercut
            # -> [CRYPTO.md](CRYPTO.md#release-freshness)
            raw["InRelease"] = cleartext_body(raw["InRelease"])

        return raw["InRelease" if use_inrelease else "Release"]

    def verify_root_digests(self, key: str, incoming_digests: dict[str, str]) -> bool:
        """Verify a root Release file against the bytes the last load fetched.

        Args:
            key: `Release`, `InRelease`, or `Release.gpg`.
            incoming_digests: The digests to verify.

        Returns:
            True when the last load fetched the file with these digests.
        """
        return compare_digests(self.root_digests.get(key, {}), incoming_digests)

    def load(self) -> None:
        """Load the root Release file, verifying its signatures when they are checked.

        Raises:
            ReleaseNotFoundException: Neither a root `Release` nor an `InRelease` exists.
            ReleaseReadException: Signatures are checked and the root Release has none.
            GPGVerifyException: A root Release signature fails, or an `InRelease` is malformed.
            ChecksumException: `Release` and `InRelease` carry different content.
        """
        use_inrelease, self.filename = self._find_root_files()
        self._parse_loaded(self._load_root_files(use_inrelease))
        logger.info("Repo: Storage=%s Release=%s", self.storage.get_type(), self.fields)

    def setup(self, fields: dict[str, str] | None = None) -> None:
        """Start a new root Release.

        Args:
            fields: Initial fields, copied; the generated identity fields are added to them.
                Defaults to None (keep the current ones).
        """
        # a new root Release is named after its dist, which every repo that saves one has
        dist = self.params.dist
        assert dist is not None
        if fields is not None:
            self.fields = dict(fields)
        self.fields.update(
            {
                "Origin": dist,
                "Label": dist,
                "Suite": dist,
                "Codename": dist,
                "Description": "Generated by debkit",
            }
        )
        self._start_file(self.params.indices_path("Release"))

    def _sign_gpg(self, enc_data: bytes) -> None:
        """Sign the root Release into the layout's `InRelease` and `Release.gpg`.

        Args:
            enc_data: The encoded root Release to sign.

        Raises:
            ReleaseWriteException: GnuPG sign error
        """
        GPG_ERR_MSG = "GnuPG sign error (maybe wrong passphrase?)"

        # only reached in `VERIFY_AND_SIGN`, so a missing GnuPG is a caller bug
        assert self.gpg is not None, "signing without GnuPG"

        signed_data = self.gpg.sign(enc_data).encode("utf-8")
        if len(signed_data):
            signed_filename = self.params.indices_path("InRelease")
            logger.info("Save signed InRelease...")
            self.storage.put(signed_filename, signed_data)
        else:
            raise ReleaseWriteException(GPG_ERR_MSG)

        detached_signature = self.gpg.sign(enc_data, detach=True).encode("utf-8")
        if len(detached_signature):
            detached_filename = self.params.indices_path("Release.gpg")
            logger.info("Save detached GPG signature...")
            self.storage.put(detached_filename, detached_signature)
        else:
            raise ReleaseWriteException(GPG_ERR_MSG)

    def _remove_root_signatures(self) -> None:
        """Delete the root `InRelease` and `Release.gpg` an unsigned save leaves stale.

        Both sign the previous content, so apt would read a hash mismatch or a bad
        signature -> [CRYPTO.md](CRYPTO.md#verification-switches).
        """
        for path in (
            self.params.indices_path("InRelease"),
            self.params.indices_path("Release.gpg"),
        ):
            try:
                self.storage.delete(path)
            except StorageNotFoundException:
                continue
            logger.warning("Release saved unsigned: removed stale signature %s", path)
            self.found_indices_files = [f for f in self.found_indices_files if f != path]

    def _signed_date(self, field: str) -> datetime | None:
        """Get a date field of this source Release, refusing a malformed one."""
        try:
            return self.date(field)
        except ValueError as exc:
            raise ReleaseFreshnessException(f"source {field} is {exc}") from None

    def check_fresh_against(self, target_date: str | None, now: datetime) -> None:
        """Refuse this source Release when it is expired or older than the target.

        A signature proves who signed a Release, not that it is current: a replayed
        older Release would roll the target back, and the orphan pass would purge every
        newer object -> [CRYPTO.md](CRYPTO.md#release-freshness).

        Args:
            target_date: The target Release `Date` as read, None when it has none or was not
                loaded.
            now: Zone-aware current time.

        Raises:
            ReleaseFreshnessException: This Release is expired, has no `Date` while the
                target has one, is older than the target, or carries a malformed date.
        """
        valid_until = self._signed_date("Valid-Until")
        source_date = self._signed_date("Date")

        if valid_until is not None and valid_until < now:
            raise ReleaseFreshnessException(
                f"source Release expired at {self.fields['Valid-Until']}"
            )

        if target_date is None:
            return
        try:
            target_dated = parse_release_date(target_date)
        except ValueError as exc:
            logger.warning("Target Release Date is %s; not compared with the source", exc)
            return

        if source_date is None:
            raise ReleaseFreshnessException(
                f"source Release has no Date; the target Release is dated {target_date}"
            )
        if source_date < target_dated:
            raise ReleaseFreshnessException(
                f"source Release dated {self.fields['Date']} is older than the target Release "
                f"dated {target_date}"
            )
