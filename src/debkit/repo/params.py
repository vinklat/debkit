"""Class containing Debian repository parameters."""

import logging
import posixpath
import re

from txstore import is_unsafe_path, redact_url

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class RepoParamsException(Exception):
    """Exception raised for errors in the RepoParams class."""

    def __init__(self, message: str) -> None:
        """Initialize with error message."""
        super().__init__(f"Repo parameters: {message}")


# Debian's pseudo-architecture for source packages: its indices live under
# `<component>/source/` and are named `Sources`, never `binary-source/Packages`.
SOURCE_ARCH = "source"

# Debian's architecture-independent pseudo-architecture: its packages serve every
# binary architecture, so it is always selected and never loaded as its own container.
ARCH_ANY = "all"

# The component key of a flat repository whose Release names no `Components`: a flat
# layout holds one index set, so it has exactly one component slot.
FLAT_COMPONENT = "/"

# A Debian architecture name. The token becomes the `binary-<arch>/` directory of a
# storage path, so anything else could steer that path
# -> [VALIDATION.md](VALIDATION.md#architectures).
_ARCH_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]*")


def is_safe_name(name: str) -> bool:
    """Report whether a dist or component name is safe to build a storage path from.

    A name may hold `/` (`updates/main`), but never a segment that steers the path out of
    its directory -> [VALIDATION.md](VALIDATION.md#debian-source-strings).

    Args:
        name (str): A dist or component name.

    Returns:
        bool: True when the name is non-empty, not `.`, and passes the unsafe-key rule.
    """
    return name not in ("", ".") and not is_unsafe_path(name)


def pool_path(comp: str | None, source: str, filename: str) -> str:
    """Build the pool key an inserted `.deb` lands on.

    The key follows Debian's source pool layout: the upload's file name nests under its
    source package and that name's prefix, `lib` plus the next letter for a `lib*` name and
    the first letter otherwise, below the component when one is given
    -> [VALIDATION.md](VALIDATION.md#filename-safety).

    Args:
        comp (str | None): Component name, or None for a pool without one.
        source (str): The source package name, a Debian Policy name.
        filename (str): The upload path inside the incoming storage; only its file name is
            kept.

    Returns:
        str: The pool key, e.g. `pool/main/libf/libfoo/libfoo1_1.0_amd64.deb`.
    """
    head = f"pool/{comp}" if comp is not None else "pool"
    prefix = source[:4] if source.startswith("lib") and len(source) > 3 else source[0]
    return f"{head}/{prefix}/{source}/{posixpath.basename(filename)}"


def remove_prefix_from_path(path: str, prefix: str) -> str:
    """Return `path` without the leading `prefix` directory, or unchanged when it lacks it.

    Args:
        path (str): Storage path.
        prefix (str): Directory to strip, without a trailing slash.

    Returns:
        str: The path relative to `prefix`.
    """
    return path.removeprefix(f"{prefix}/")


def is_valid_arch(token: str) -> bool:
    """Report whether a token is a well-formed Debian architecture name.

    Args:
        token (str): Architecture token from a flag, a Release, or an upload.

    Returns:
        bool: True for lower-case letters, digits, and `-`, not starting with `-`.
    """
    return _ARCH_TOKEN.fullmatch(token) is not None


class RepoParams:
    """Debian Repo input parameters.

    Attributes:
        dist (str): The distribution name.
        components (List[str]): The list of components.
        archs (List[str]): The list of architectures to limit the repo to.
        indices_exclude (List[str]): Globs of Release-listed index paths to skip.
        verify_gpg (bool): Whether to verify GPG signature of root Release file.
        verify_digests (bool): Whether to verify hex digests like SHA256, SHA512,...
        url (str): The URL of the repository.
        is_flat (bool): Whether the repository is flat. Default: False (repo with components).
    """

    def __init__(
        self,
        dist: str | None = None,
        components: list[str] | None = None,
        archs: list[str] | None = None,
        indices_exclude: list[str] | None = None,
        verify_gpg: bool = True,
        verify_digests: bool = True,
    ) -> None:
        """Initialize RepoParams.

        Args:
            dist (Optional[str]): The distribution name. Defaults to None.
            components (Optional[List[str]]): The list of components. Defaults to None.
            archs (Optional[List[str]]): The list of architectures. Defaults to None.
            indices_exclude (Optional[List[str]]): Globs of Release-listed index paths to
                skip. Defaults to None (mirror every listed index).
            verify_gpg (bool): Whether to verify GPG signature. Defaults to True.
            verify_digests (bool): Whether to verify hex digests. Defaults to True.
        """
        self.dist = dist
        self.components = components
        self.archs = archs
        self.indices_exclude = indices_exclude
        self.verify_gpg = verify_gpg
        self.verify_digests = verify_digests
        self.url: str | None = None
        self.indices_prefix = f"dists/{dist}" if dist else ""
        self.is_flat = False

    def from_deb_source(self, deb_source: str | None) -> None:
        """Extract debian repo attributes from a deb source string.

        Args:
            deb_source (str | None): The deb source string to extract attributes from.

        Raises:
            RepoParamsException: If the deb source string is invalid.
            RepoParamsException: If a componented source names an unsafe dist or component.
        """
        if not isinstance(deb_source, str):
            raise RepoParamsException("no debian source provided")

        # deduplicate repeating spaces in deb_source
        deb_source = re.sub(r"\s+", " ", deb_source)

        # remove trailing and leading spaces
        deb_source = re.sub(r"^\s+|\s+$", "", deb_source)

        parts: list[str] = deb_source.split(" ")
        if len(parts) < 2:
            # the URL may carry credentials -> [CONFIG.md](CONFIG.md#secrets)
            raise RepoParamsException(
                f"there is no dist/components fields in source {redact_url(deb_source)}"
            )

        url, self.dist, *components = parts
        self.url = url
        # a flat dist names a directory only when it is a real dist, which is alphanumeric
        for name in (self.dist, *components) if components else ():
            if not is_safe_name(name):
                raise RepoParamsException(f"unsafe dist or component name: {name!r}")
        if components:
            self.indices_prefix = f"dists/{self.dist}"
            self.components = components
        else:
            # flat repo without components
            self.components = None
            self.is_flat = True
            self.indices_prefix = ""

        logger.debug(
            "Parse deb source: url=%s, dist=%s, components=%s",
            redact_url(url),
            self.dist,
            self.components,
        )

    def __eq__(self, other: object) -> bool:
        """Check if this RepoParams is equal to another RepoParams.

        Args:
            other (object): The other RepoParams to compare with.

        Returns:
            bool: True if equal, False otherwise.
        """
        return vars(self) == vars(other)

    def __ne__(self, other: object) -> bool:
        """Check if this RepoParams is not equal to another RepoParams.

        Args:
            other (object): The other RepoParams to compare with.

        Returns:
            bool: True if not equal, False otherwise.
        """
        return vars(self) != vars(other)

    @staticmethod
    def arch_dir(arch: str) -> str:
        """Name the per-architecture directory of a componented layout.

        Binary indices nest under `binary-<arch>/`, source indices under `source/`
        -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            arch (str): Architecture name, or `SOURCE_ARCH`.

        Returns:
            str: Directory segment, without a trailing slash.
        """
        return SOURCE_ARCH if arch == SOURCE_ARCH else f"binary-{arch}"

    @staticmethod
    def index_name(arch: str) -> str:
        """Name the package index of an architecture.

        Args:
            arch (str): Architecture name, or `SOURCE_ARCH`.

        Returns:
            str: `Sources` for the source architecture, `Packages` otherwise.
        """
        return "Sources" if arch == SOURCE_ARCH else "Packages"

    def indices_path(self, inner: str, comp: str | None = None) -> str:
        """Build the full storage path of an indices file.

        The Debian repo layout splits into:
          - non-flat: ``<indices_prefix>/<comp>/<inner>``
            where ``indices_prefix == dists/<dist>``.
            ``inner`` is e.g. ``binary-amd64/Packages.xz`` or ``Contents-amd64.xz``.
          - flat-real-dist: ``<dist>/<inner>``
          - flat-rootless:  ``<inner>``

        `release_key()` maps the result back to what a Release file references,
        relative to the layout prefix, for ``digests`` lookups.

        Args:
            inner (str): Path under ``<comp>/`` (or bare filename for flat repos).
            comp (Optional[str]): Component name; ``None`` is treated as flat-like.

        Returns:
            str: The full storage path.
        """
        if self.is_flat:
            if self.is_real_dist() and self.dist:
                return f"{self.dist}/{inner}"
            return inner

        path_short = f"{comp}/{inner}" if comp else inner
        return f"{self.indices_prefix}/{path_short}" if self.indices_prefix else path_short

    def release_key(self, path: str) -> str:
        """Map a full index path back to the key the root Release lists it under.

        The inverse of `indices_path()`: it strips exactly the layout prefix that
        method prepends, so `release_key(indices_path(inner, comp))` is `inner`, under
        `comp/` in a componented layout, for every layout. A path that does not carry
        the prefix is returned unchanged, so an already-relative key round-trips.

        Args:
            path (str): Full storage path of an index file.

        Returns:
            str: Path relative to the layout prefix, as the Release lists it.
        """
        if self.is_flat:
            if self.is_real_dist() and self.dist:
                return remove_prefix_from_path(path, self.dist)
            return path

        if self.indices_prefix:
            return remove_prefix_from_path(path, self.indices_prefix)
        return path

    def is_real_dist(self) -> bool:
        """Check if this RepoParams has a real distribution name.

        Returns:
            bool: True if the distribution name is not None, or emty or slash (/),....
        """
        return bool(re.match("^[A-Za-z0-9]*$", self.dist)) if isinstance(self.dist, str) else False
