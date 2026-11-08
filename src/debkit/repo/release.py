"""Class containing Debian Release file data."""

import hashlib
import logging
import re
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from txstore import calc_digests, compare_digests

from debkit.repo.gnupg import canonical_text, cleartext_body
from debkit.repo.hashes import (
    SUPPORT_HASH_LIST,
    ChecksumException,
    HexDigests,
    parse_hash_block,
)

if TYPE_CHECKING:
    from email.message import Message

    from txstore import Storage

    from debkit.repo.gnupg import GnuPG
    from debkit.repo.params import RepoParams


# create logger
logging.getLogger(__name__).addHandler(logging.NullHandler())

# Fields for which bare unindented continuation tokens are folded onto the
# previous line.  Hash-block fields (MD5Sum, SHA1, SHA256, SHA512, …) are
# intentionally excluded: folding a stray token into a hash block shifts the
# (digest, size, filename) triple alignment and silently maps wrong digests.
_CONTINUATION_FIELDS = frozenset({"Architectures", "Components"})
_HASH_BLOCK_FIELDS = frozenset(SUPPORT_HASH_LIST)


class IndexEntry(NamedTuple):
    """One index file listed in the digest block of a Release file.

    Attributes:
        key (str): Path as the Release lists it, relative to the layout prefix.
        path (str): Full storage path of the same object.
        digests (Dict[str, str]): Digests and `Size` the Release records for it.
    """

    key: str
    path: str
    digests: dict[str, str]


class ReleaseReadException(Exception):
    """Exception raised for errors in the Release class.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Release: {message}")


class ReleaseWriteException(Exception):
    """Exception raised for write errors in the Release class.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Release: {message}")


class ReleaseFreshnessException(Exception):
    """Exception raised when a source Release is stale, expired, or badly dated.

    Attributes:
        message (str): explanation of the error
    """

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Release: {message}")


# Zone tokens apt accepts in a Release date; any other name would silently read as UTC
_RELEASE_DATE_ZONES = frozenset({"UTC", "GMT", "UT", "Z"})
_NUMERIC_ZONE = re.compile(r"[+-]\d{4}")


def parse_release_date(value: str) -> datetime:
    """Parse a Release date field such as `Date` or `Valid-Until`.

    The zone must be `UTC`, `GMT`, `UT`, `Z`, or a numeric offset; `-0000` is read as
    UTC -> [VALIDATION.md](VALIDATION.md#release-dates).

    Args:
        value (str): Raw field value.

    Raises:
        ValueError: If *value* is not an RFC 2822 date with an accepted zone.

    Returns:
        datetime: The zone-aware date.
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
        storage (Storage): The storage class of the repo.
        params (Params): The input parameters of the repo.
        gpg (GnuPG): The GnuPG object.
        fields (dict): The fields of the release.
        filename (str): The filename of the release.
        found_indices_files List[str]: List of all found Release files.
        hashfuns List[str]: Available hexdigest algorithms in data.
        is_flat (bool): Whether it's a repo without components.
        hash (str): Hexdigest of a file content (for changes detection).
        root_digests (Dict[str, Dict[str, str]]): Release key → digests of each root
            Release file as the last load fetched it.
    """

    def __init__(
        self, storage: "Storage", params: "RepoParams", gpg: "GnuPG | None" = None
    ) -> None:
        """Initialize Release object.

        Args:
            storage (Storage): The storage class of the repo.
            params (RepoParams): The input parameters of the repo.
            gpg (GnuPG | None): Optional GnuPG object. Defaults to None.
        """
        self.storage = storage
        self.params = params
        self.gpg: GnuPG | None = gpg
        self.fields: dict[str, Any] = {}
        self.filename: str | None = None
        self.found_indices_files: list[str] = []
        self.hashfuns: list[str] = []
        self.is_flat = False
        self.hash = ""
        self.root_digests: dict[str, dict[str, str]] = {}
        super().__init__()

    def reset(self) -> None:
        """Reset Release object to defaults."""
        self.fields = {}
        self.digests = {}
        self.filename = None
        self.found_indices_files = []
        self.hashfuns = []
        self.hash = ""
        self.root_digests = {}

    def __copy__(self) -> "Release":
        """Create a shallow copy of this Release.

        Returns:
            Release: A shallow copy of this Release.
        """
        ret = Release(self.storage, self.params, gpg=self.gpg)
        ret.fields = self.fields.copy()
        ret.digests = self.digests.copy()
        ret.filename = self.filename
        ret.found_indices_files = self.found_indices_files.copy()
        ret.hashfuns = self.hashfuns.copy()
        ret.modified = self.modified
        ret.is_flat = self.is_flat
        ret.hash = self.hash
        ret.root_digests = self.root_digests.copy()
        return ret

    def __repr__(self) -> str:
        """Return string representation of the Release object."""
        return "<empty>" if self.filename is None else self.filename

    def __parsed2dict(self, parsed: "Message") -> dict[str, str]:
        """Convert parsed headers to a dictionary.

        If there are multiple headers with the same name, the last one is used.

        Args:
            parsed (Message): The parsed headers.

        Returns:
            Dict[str, str]: The dictionary of parsed headers.
        """
        ret: dict[str, str] = {}

        for field, data in parsed.items():
            ret[field] = data

        return ret

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
                        logging.getLogger(__name__).warning(
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
            enc_data (bytes): The encoded data to parse.

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
        self.fields = self.__parsed2dict(parsed)

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
            comp (str): The component the Release belongs to.
            arch (str): The architecture the Release belongs to.

        Returns:
            str: The storage path of the component Release.
        """
        filename = self.params.indices_path(f"{self.params.arch_dir(arch)}/Release", comp)
        return filename

    def __find_file(
        self, comp: str | None = None, arch: str | None = None
    ) -> tuple[bool, bool, str | None]:
        """Find the right Release file in storage.

        Args:
            comp (Optional[str]): The component to find. Defaults to None (root Release).
            arch (Optional[str]): The architecture to find. Defaults to None (root Release).

        Raises:
            ReleaseReadException: File not found.

        Returns:
            Tuple[bool, bool, str|None]: A boolean indicating whether it's a detached signature,
                a boolean indicating whether to use InRelease, and the found filename.
        """
        # detched signature, use InRelease instead of Release if True
        use_inrelease = False

        # not root release
        if comp and arch:
            filename = self._component_release_path(comp, arch)
            if self.storage.exists(filename):
                self.found_indices_files = [filename]
                return False, False, filename
            # else:
            return False, False, None

        # the layout decides the prefix -> [VALIDATION.md](VALIDATION.md#repository-index-paths)
        release_path = self.params.indices_path("Release")
        release_gpg_path = self.params.indices_path("Release.gpg")
        inrelease_path = self.params.indices_path("InRelease")

        release_path_exists = self.storage.exists(release_path)
        release_gpg_path_exists = self.storage.exists(release_gpg_path)
        inrelease_path_exists = self.storage.exists(inrelease_path)

        # Release or InRelease not found
        if not (release_path_exists or inrelease_path_exists):
            raise ReleaseReadException(f"File not found: {release_path} or {inrelease_path}")

        # Release.gpg or InRelease not found
        if not (release_gpg_path_exists or inrelease_path_exists):
            raise ReleaseReadException(
                f"GPG signature file missing: {release_gpg_path} or {inrelease_path}"
            )

        self.found_indices_files = []

        # Release found
        if release_path_exists:
            self.found_indices_files.append(release_path)

        # Release.gpg found
        if release_gpg_path_exists:
            self.found_indices_files.append(release_gpg_path)

        # InRelease found
        if inrelease_path_exists:
            self.found_indices_files.append(inrelease_path)

        # not Release or without Release.gpg if required -> use InRelease
        if (not release_path_exists) or (self.params.verify_gpg and not release_gpg_path_exists):
            use_inrelease = True
            filename = inrelease_path
        else:
            filename = release_path

        return True, use_inrelease, filename

    def _verify_gpg(self, data: bytes, detach_sign: bytes | None = None) -> bytes:
        """Verify GPG signature.

        Args:
            data (bytes): The data to verify.
            detach_sign (Optional[bytes]): The detached signature. Defaults to None.

        Returns:
            bytes: The verified data.
        """
        ret_data: bytes = b""

        if self.gpg is None:
            return ret_data

        if detach_sign is None:
            logging.debug("Verify signed InRelease...")
            ret_data = self.gpg.verify(data)
        else:
            logging.debug("Verify detached GPG signature...")
            ret_data = self.gpg.verify(data, detach_sign=detach_sign)

        return ret_data

    def _load_root_files(self, use_inrelease: bool) -> bytes:
        """Fetch every root Release file once and verify each signature present.

        No digest block lists these files, so their digests are recorded here: a sync
        copy must match the bytes this load verified, not a second fetch of them
        -> [CRYPTO.md](CRYPTO.md#verification-order).

        Args:
            use_inrelease (bool): Whether `InRelease` is the file to parse.

        Raises:
            ChecksumException: Both `InRelease` and `Release` are present and their
                verified contents differ.

        Returns:
            bytes: The verified content of the file to parse.
        """
        raw = {
            self.params.release_key(path): self.storage.get(path)
            for path in self.found_indices_files
        }
        self.root_digests = {key: calc_digests(data) for key, data in raw.items()}

        if self.params.verify_gpg and self.gpg is not None:
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
            # a sync target is read without a keyring, yet its signed body still holds the
            # Date a source must not undercut -> [CRYPTO.md](CRYPTO.md#release-freshness)
            raw["InRelease"] = cleartext_body(raw["InRelease"])

        return raw["InRelease" if use_inrelease else "Release"]

    def verify_root_digests(self, key: str, incoming_digests: dict[str, str]) -> bool:
        """Verify a root Release file against the bytes the last load fetched.

        Args:
            key (str): `Release`, `InRelease`, or `Release.gpg`.
            incoming_digests (Dict[str, str]): The digests to verify.

        Returns:
            bool: True when the last load fetched the file with these digests.
        """
        return compare_digests(self.root_digests.get(key, {}), incoming_digests)

    def load(
        self,
        comp: str | None = None,
        arch: str | None = None,
        root_release: Optional["Release"] = None,
    ) -> bool:
        """Load Release file.

        Args:
            comp (str, optional): The component to load. Defaults to None (root Release)
            arch (str, optional): The architecture to load. Defaults to None (root Release).
            root_release (Optional[Release]): The root Release object. Defaults to None.

        Raises:
            ReleaseReadException: Only InRelease file found, but GnuPG verification is disabled.
            GPGVerifyException: A root Release signature fails, or an `InRelease` is malformed.
            ChecksumException: Checksum mismatch.

        Returns:
            bool: True if the Release file was loaded successfully, False otherwise.
        """
        is_root_release, use_inrelease, self.filename = self.__find_file(comp, arch)

        if self.filename is None:
            return False

        if is_root_release and use_inrelease and not self.params.verify_gpg:
            raise ReleaseReadException(
                "Only InRelease file found, but GnuPG verification is disabled"
            )

        data: bytes = (
            self._load_root_files(use_inrelease)
            if is_root_release
            else self.storage.get(self.filename)
        )

        if self.params.verify_digests and root_release is not None and not is_root_release:
            filename = self.params.release_key(self.filename)
            digests = calc_digests(data)
            if not root_release.verify_digests(filename, digests):
                raise ChecksumException(f"{filename} checksum mismatch.")

        # logging.debug("release length=%d", len(data))

        if data:
            self.parse(data)
            self.hash = hashlib.sha256(data).hexdigest()
            # if root_release:
            #     logging.debug("hash=%s", self.hash)

        if is_root_release:
            logging.info("Repo: Storage=%s Release=%s", self.storage.get_type(), self.fields)
        else:
            logging.info(
                "Component=%s Storage=%s Release=%s", comp, self.storage.get_type(), self.fields
            )
        if self.hashfuns:
            logging.debug("Available hexdigests: %s", self.hashfuns)

        # TODO: return None ?
        return True

    def serialize(self) -> str:
        """Serialize Release file data to headers format.

        Returns:
            str: The serialized Release file fields.
        """
        msg = EmailMessage(policy=policy.default.clone(max_line_length=160))
        for field, data in self.fields.items():
            msg.add_header(field, data)

        ret: str = msg.as_string().rstrip()

        # logging.debug("hashfuns=%s", self.hashfuns)

        if self.hashfuns:
            for hashfun in self.hashfuns:
                hlist: list[str] = []
                for filename, filesums in self.digests.items():
                    try:
                        hlist.append(
                            " ".join([filesums[hashfun], f"{filesums['Size']:>16}", filename])
                        )
                    except KeyError as exc:
                        logging.debug("key error: %s", exc)
                if hlist:
                    ret += f"\n{hashfun}:\n " + "\n ".join(hlist)

        return ret

    def _sign_gpg(self, filename: str, enc_data: bytes) -> None:
        """Sign Release file with GnuPG.

        Args:
            filename (str): The filename to sign.
            enc_data (bytes): The encoded data to sign.

        Raises:
            ReleaseWriteException: GnuPG sign error
        """
        GPG_ERR_MSG = "GnuPG sign error (maybe wrong passphrase?)"

        if self.gpg is None:
            return

        signed_data = self.gpg.sign(enc_data).encode("utf-8")
        if len(signed_data):
            signed_filename = re.sub(r"Release", "InRelease", filename)
            logging.info("Save signed InRelease...")
            self.storage.put(signed_filename, signed_data)
        else:
            raise ReleaseWriteException(GPG_ERR_MSG)

        detached_signature = self.gpg.sign(enc_data, detach=True).encode("utf-8")
        if len(detached_signature):
            detached_filename = f"{filename}.gpg"
            logging.info("Save detached GPG signature...")
            self.storage.put(detached_filename, detached_signature)
        else:
            raise ReleaseWriteException(GPG_ERR_MSG)

    def save(self) -> dict[str, str] | None:
        """Save Release file.

        Returns:
            Dict[str, str] | None: The digests of the saved file.
        """
        if self.filename is None:
            return None

        if not self.modified:
            logging.debug("%s not modified.", self.filename)
            return None

        data = self.serialize()
        enc_data = data.encode("utf-8")
        logging.info("Save %s...", self.filename)
        self.storage.put(self.filename, enc_data)
        digests = calc_digests(enc_data)

        if self.gpg and self.params.verify_gpg:
            self._sign_gpg(self.filename, enc_data)

        return digests

    def dump(self, brief: bool = False) -> dict[str, Any]:
        """Dump Release object to a dict.

        Args:
            brief (bool): Whether to dump a brief version output. Defaults to False.

        Returns:
            Dict[str, Any]: A dict representation of the Release object. The dictionary includes the
                indices filenames, attributes, and digests (if not brief).
        """
        return {
            "files": self.found_indices_files,
            "data": self.fields,
            "digests": self.digests if not brief else {},
        }

    def get_indices_paths(self) -> list[str]:
        """Get list of all found indices filenames.

        Returns:
            List[str]: A list of filenames.
        """
        return self.found_indices_files

    def listed_indices(self) -> list[IndexEntry]:
        """Get every index file the digest block of this Release lists.

        The Release lists index paths relative to the layout prefix; this maps each
        one to its full storage path. Entries are not checked against storage, so a
        path listed but not published by the repository is still returned.

        Returns:
            List[IndexEntry]: One entry per digest block record, sorted by key.
        """
        return [
            IndexEntry(key, self.params.indices_path(key), digests)
            for key, digests in sorted(self.digests.items())
        ]

    def date(self, field: str) -> datetime | None:
        """Get a date field of the Release.

        Args:
            field (str): Field name, `Date` or `Valid-Until`.

        Raises:
            ValueError: If the field is present but malformed.

        Returns:
            datetime | None: The zone-aware date, or None when the field is absent.
        """
        value = self.fields.get(field)
        return None if value is None else parse_release_date(value)

    def _signed_date(self, field: str) -> datetime | None:
        """Get a date field of this source Release, refusing a malformed one."""
        try:
            return self.date(field)
        except ValueError as exc:
            raise ReleaseFreshnessException(f"source {field} is {exc}") from None

    def check_fresh_against(self, target: "Release", now: datetime) -> None:
        """Refuse this source Release when it is expired or older than the target.

        A signature proves who signed a Release, not that it is current: a replayed
        older Release would roll the target back, and the orphan pass would purge every
        newer object -> [CRYPTO.md](CRYPTO.md#release-freshness).

        Args:
            target (Release): The loaded target Release; an unloaded one has no fields.
            now (datetime): Zone-aware current time.

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

        try:
            target_date = target.date("Date")
        except ValueError as exc:
            logging.warning("Target Release Date is %s; not compared with the source", exc)
            return
        if target_date is None:
            return

        if source_date is None:
            raise ReleaseFreshnessException(
                f"source Release has no Date; the target Release is dated {target.fields['Date']}"
            )
        if source_date < target_date:
            raise ReleaseFreshnessException(
                f"source Release dated {self.fields['Date']} is older than the target Release "
                f"dated {target.fields['Date']}"
            )

    def is_empty(self) -> bool:
        """Check if Release object is empty.

        Returns:
            bool: True if the Release object is empty, False otherwise.
        """
        return bool(len(self.fields) == 0)

    def setup(
        self,
        comp: str | None = None,
        arch: str | None = None,
        fields: dict[str, str] | None = None,
    ) -> None:
        """Reset indices attributes to defaults.

        Args:
            comp (Optional[str]): The component to set. Defaults to None.
            arch (Optional[str]): The architecture to set. Defaults to None.
            fields (Optional[Dict[str, str]]): The fields to set. Defaults to None.
        """
        if fields is not None:
            self.fields = fields

        self.hashfuns = list(SUPPORT_HASH_LIST)

        if comp and arch:
            self.filename = self._component_release_path(comp, arch)
            self.fields = {"Component": comp, "Architecture": arch}
        else:
            self.filename = self.params.indices_path("Release")
            self.fields.update(
                {
                    "Origin": self.params.dist,
                    "Label": self.params.dist,
                    "Suite": self.params.dist,
                    "Codename": self.params.dist,
                    "Description": "Generated by debkit",
                }
            )
        self.found_indices_files = [self.filename]
        self.modified = True
