"""Class containing Debian repository parameters."""

from __future__ import annotations

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

# a real dist names a directory of a flat layout; the empty string names none
# -> [VALIDATION.md](VALIDATION.md#distribution-names)
_REAL_DIST = re.compile(r"[A-Za-z0-9]+")


def is_safe_name(name: str) -> bool:
    """Report whether a relative storage subpath is safe to build a key from.

    A dist, component, `Sources` `Directory`, or package `Filename`: it may hold `/`
    (`updates/main`), but never a segment that steers the path out of its directory
    -> [VALIDATION.md](VALIDATION.md#debian-source-strings),
    [VALIDATION.md](VALIDATION.md#filename-safety).

    Args:
        name: A POSIX-normalized relative subpath.

    Returns:
        True when the name is non-empty, not `.`, and passes the unsafe-key rule.
    """
    return name not in ("", ".") and not is_unsafe_path(name)


def pool_path(comp: str | None, source: str, filename: str) -> str:
    """Build the pool key an inserted `.deb` lands on.

    The key follows Debian's source pool layout: the upload's file name nests under its
    source package and that name's prefix, `lib` plus the next letter for a `lib*` name and
    the first letter otherwise, below the component when one is given
    -> [VALIDATION.md](VALIDATION.md#filename-safety).

    Args:
        comp: Component name, or None for a pool without one.
        source: The source package name, a Debian Policy name.
        filename: The upload path inside the incoming storage; only its file name is kept.

    Returns:
        The pool key, e.g. `pool/main/libf/libfoo/libfoo1_1.0_amd64.deb`.
    """
    head = f"pool/{comp}" if comp is not None else "pool"
    prefix = source[:4] if source.startswith("lib") and len(source) > 3 else source[0]
    return f"{head}/{prefix}/{source}/{posixpath.basename(filename)}"


def remove_prefix_from_path(path: str, prefix: str) -> str:
    """Return `path` without the leading `prefix` directory, or unchanged when it lacks it.

    Args:
        path: Storage path.
        prefix: Directory to strip, without a trailing slash.

    Returns:
        The path relative to `prefix`.
    """
    return path.removeprefix(f"{prefix}/")


def is_valid_arch(token: str) -> bool:
    """Report whether a token is a well-formed Debian architecture name.

    Args:
        token: Architecture token from a flag, a Release, or an upload.

    Returns:
        True for lower-case letters, digits, and `-`, not starting with `-`.
    """
    return _ARCH_TOKEN.fullmatch(token) is not None


class RepoParams:
    """Debian Repo input parameters.

    Attributes:
        dist: The distribution name.
        components: The list of components.
        archs: The list of architectures to limit the repo to.
        indices_exclude: Globs of Release-listed index paths to skip.
        verify_gpg: Whether to verify GPG signature of root Release file.
        verify_digests: Whether to verify hex digests like SHA256, SHA512,...
        url: The URL of the repository.
        is_flat: Whether the repository is flat. Default: False (repo with components).
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
            dist: The distribution name. Defaults to None.
            components: The list of components. Defaults to None.
            archs: The list of architectures. Defaults to None.
            indices_exclude: Globs of Release-listed index paths to skip. Defaults to None (mirror
                every listed index).
            verify_gpg: Whether to verify GPG signature. Defaults to True.
            verify_digests: Whether to verify hex digests. Defaults to True.
        """
        self.dist = dist
        self.components = components
        self.archs = archs
        self.indices_exclude = indices_exclude
        self.verify_gpg = verify_gpg
        self.verify_digests = verify_digests
        self.url: str | None = None
        self.is_flat = False

    def from_deb_source(self, deb_source: str | None) -> None:
        """Extract debian repo attributes from a deb source string.

        Args:
            deb_source: The deb source string to extract attributes from; None is refused.

        Raises:
            RepoParamsException: If the deb source string is invalid.
            RepoParamsException: If a componented source names an unsafe dist or component.
        """
        if not isinstance(deb_source, str):
            raise RepoParamsException("no debian source provided")

        # fields are separated by any whitespace run, leading and trailing runs dropped
        parts: list[str] = deb_source.split()
        if len(parts) < 2:
            # the URL may carry credentials -> [CONFIG.md](CONFIG.md#secrets)
            raise RepoParamsException(
                f"there is no dist/components fields in source {redact_url(' '.join(parts))}"
            )

        url, self.dist, *components = parts
        self.url = url
        # a flat dist names a directory only when it is a real dist, which is alphanumeric
        for name in (self.dist, *components) if components else ():
            if not is_safe_name(name):
                raise RepoParamsException(f"unsafe dist or component name: {name!r}")
        if components:
            self.components = components
        else:
            # flat repo without components
            self.components = None
            self.is_flat = True

        logger.debug(
            "Parse deb source: url=%s, dist=%s, components=%s",
            redact_url(url),
            self.dist,
            self.components,
        )

    def __eq__(self, other: object) -> bool:
        """Check if this RepoParams is equal to another RepoParams.

        Args:
            other: The object to compare with.

        Returns:
            True if equal, False otherwise; NotImplemented for an object that is not a RepoParams.
        """
        if not isinstance(other, RepoParams):
            return NotImplemented
        return vars(self) == vars(other)

    @staticmethod
    def arch_dir(arch: str) -> str:
        """Name the per-architecture directory of a componented layout.

        Binary indices nest under `binary-<arch>/`, source indices under `source/`
        -> [VALIDATION.md](VALIDATION.md#repository-index-paths).

        Args:
            arch: Architecture name, or `SOURCE_ARCH`.

        Returns:
            Directory segment, without a trailing slash.
        """
        return SOURCE_ARCH if arch == SOURCE_ARCH else f"binary-{arch}"

    @staticmethod
    def index_name(arch: str) -> str:
        """Name the package index of an architecture.

        Args:
            arch: Architecture name, or `SOURCE_ARCH`.

        Returns:
            `Sources` for the source architecture, `Packages` otherwise.
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
            inner: Path under ``<comp>/`` (or bare filename for flat repos).
            comp: Component name; ``None`` is treated as flat-like.

        Returns:
            The full storage path.
        """
        if self.is_flat:
            return f"{self._flat_dir}/{inner}" if self._flat_dir else inner

        path_short = f"{comp}/{inner}" if comp else inner
        return f"{self.indices_prefix}/{path_short}" if self.indices_prefix else path_short

    def release_key(self, path: str) -> str:
        """Map a full index path back to the key the root Release lists it under.

        The inverse of `indices_path()`: it strips exactly the layout prefix that
        method prepends, so `release_key(indices_path(inner, comp))` is `inner`, under
        `comp/` in a componented layout, for every layout. A path that does not carry
        the prefix is returned unchanged, so an already-relative key round-trips.

        Args:
            path: Full storage path of an index file.

        Returns:
            Path relative to the layout prefix, as the Release lists it.
        """
        if self.is_flat:
            return remove_prefix_from_path(path, self._flat_dir) if self._flat_dir else path

        if self.indices_prefix:
            return remove_prefix_from_path(path, self.indices_prefix)
        return path

    @property
    def indices_prefix(self) -> str:
        """The dist's index tree, derived so it never disagrees with `dist` and `is_flat`.

        Returns:
            `dists/<dist>` for a componented layout with a dist, else empty
            -> [VALIDATION.md](VALIDATION.md#repository-index-paths).
        """
        return "" if self.is_flat or not self.dist else f"dists/{self.dist}"

    def is_index_tree_key(self, path: str) -> bool:
        """Report whether a payload key lands in the `dists/` tree of a componented layout.

        Every dist sharing the root keeps its Release and indices there, so a remote
        `Filename` or `Directory` naming that tree would overwrite another dist's metadata
        -> [VALIDATION.md](VALIDATION.md#filename-safety). A flat layout has no such tree.

        Args:
            path: A POSIX-normalized payload key.

        Returns:
            True for `dists` or a key under `dists/` in a componented layout.
        """
        return not self.is_flat and (path == "dists" or path.startswith("dists/"))

    @property
    def _flat_dir(self) -> str | None:
        """The directory a flat layout's indices sit in: the dist when it is a real one."""
        return self.dist if self.is_real_dist() else None

    def is_real_dist(self) -> bool:
        """Check if the distribution name is a real dist, one naming a flat layout's directory.

        Returns:
            True when the dist is a non-empty run of ASCII letters and digits; False for None,
            the empty string, or a name such as `/` or `./`
            -> [VALIDATION.md](VALIDATION.md#distribution-names).
        """
        return self.dist is not None and _REAL_DIST.fullmatch(self.dist) is not None
