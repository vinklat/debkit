"""Index selection rules: which Release-listed indices a mirror carries.

Pure functions over Release keys -> [VALIDATION.md](VALIDATION.md#index-selection).
"""

import fnmatch
import logging
import re

from debkit.repo.params import ARCH_ANY, SOURCE_ARCH, RepoParams

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Index directories no container parses. `Packages` opens only
# `<component>/binary-<arch>/Packages*` and `<component>/source/Sources*`, so an index
# under one of these is mirrored while the payloads it names never join the package
# set -> [VALIDATION.md](VALIDATION.md#index-selection).
UNPARSED_INDEX_DIRS = ("debian-installer",)

# Prefix of an installer image tree segment, `installer-<arch>/`, where `<arch>` is one
# the repository knows. The Release lists the checksum manifests it holds, not the
# images themselves, so mirroring them copies manifests for a tree the mirror does not
# carry.
UNPARSED_INDEX_DIR_PREFIX = "installer-"

# File names listing the same udeb payloads from outside `debian-installer/`:
# `Contents-udeb-<arch>` maps paths to packages the package set never gains.
UNPARSED_INDEX_NAME_PREFIXES = ("Contents-udeb",)

# Directory and file stem of the source indices, `source/Sources*`.
SOURCE_INDEX_NAMES = (RepoParams.arch_dir(SOURCE_ARCH), RepoParams.index_name(SOURCE_ARCH))


def _split_component(key: str, components: set[str] | None) -> tuple[str | None, str]:
    """Split a Release key into its component segment and the path below it.

    The component is a name the repository chose, so the structural rules read only
    the path below it: a component called `extra-armhf` or `installer-extras` says
    nothing about the architecture or the shape of the indices under it. One split
    feeds both the component membership test and those rules, so the two never
    disagree on where a component ends -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        key (str): Path as the Release lists it, relative to the layout prefix.
        components (Optional[Set[str]]): Selected components, or `None` for a flat
            repo whose paths carry no component segment.

    Returns:
        Tuple[Optional[str], str]: The component and the key below it, or `None` and
            the whole key for a flat repo and for a key with no `/`, which belongs to
            no component.
    """
    component, sep, rest = key.partition("/")
    if components is None or not sep:
        return None, key
    return component, rest


def _unselected_arch_token(key: str, archs: set[str], arch_universe: set[str]) -> str | None:
    """Find an architecture token in `key` the repository knows but has not selected.

    A token is a `-<arch>` run ending at a `.`, a `/`, or the end of the key. Several
    known architectures can end at the same position, as `amd64` does inside
    `kfreebsd-amd64`; the longest one owns the token, because that is the name the
    repository publishes the file under. Matching the shorter one would drop
    `binary-kfreebsd-amd64/Packages.xz` whenever `amd64` itself was unselected.
    Alternatives are ordered longest first, so the leftmost-first match `re` returns
    is also the longest at each token.

    Args:
        key (str): Release key below its component segment -> `_split_component`.
        archs (Set[str]): Selected architectures.
        arch_universe (Set[str]): Every architecture this repository knows about.

    Returns:
        Optional[str]: The unselected architecture owning a token, or `None` when every
            token in the key belongs to a selected architecture.
    """
    if not arch_universe:
        return None

    alternatives = "|".join(
        re.escape(arch) for arch in sorted(arch_universe, key=len, reverse=True)
    )
    wanted = archs | {ARCH_ANY}

    for match in re.finditer(rf"-({alternatives})(?=[./]|$)", key):
        if match.group(1) not in wanted:
            return match.group(1)

    return None


def _is_unparsed_index(key: str, arch_universe: set[str]) -> bool:
    """Report whether a Release key names an index nothing parses.

    A `debian-installer` index names `.udeb` payloads that never reach the package
    set, so mirroring it would copy the index, leave the payloads behind, and purge
    any the target already held as orphans: apt would read an index whose objects
    404. This is the rule `Sources` already follows, for the same reason.

    Two shapes carry the same content from outside that directory, and each needs its
    own test: `installer-<arch>/` image trees are a directory segment naming an
    architecture the repository knows, and the `Contents-udeb-<arch>` listing is a
    file name -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        key (str): Release key below its component segment -> `_split_component`.
        arch_universe (Set[str]): Every architecture this repository knows about.

    Returns:
        bool: True when the key names an index nothing parses.
    """
    parts = key.split("/")
    installer_dirs = {f"{UNPARSED_INDEX_DIR_PREFIX}{arch}" for arch in arch_universe}
    if any(part in UNPARSED_INDEX_DIRS or part in installer_dirs for part in parts[:-1]):
        return True
    return any(part.partition(".")[0].startswith(UNPARSED_INDEX_NAME_PREFIXES) for part in parts)


def _is_source_index(key: str) -> bool:
    """Report whether a Release key names a source index.

    In a componented repository they live under `<component>/source/`, in a flat one
    they are the `Sources*` files at the root, and `Contents-source` carries a
    `-source` token. This is the whole rule, so the architecture token rule never has
    to know the source architecture -> [VALIDATION.md](VALIDATION.md#index-selection).

    Args:
        key (str): Release key below its component segment -> `_split_component`.

    Returns:
        bool: True when the key belongs to the source architecture.
    """
    stems = [part.partition(".")[0] for part in key.split("/")]
    return any(stem in SOURCE_INDEX_NAMES or stem.endswith(f"-{SOURCE_ARCH}") for stem in stems)


def select_index_entry(
    key: str,
    *,
    components: set[str] | None,
    archs: set[str],
    arch_universe: set[str],
    excludes: list[str],
) -> bool:
    """Decide whether a Release-listed index belongs in the sync set.

    An entry is dropped when an exclude glob matches it, when it lives under an
    unselected component, when it is a source index and `source` is not selected,
    when nothing parses the index it names, or when its name carries an architecture
    token the repository knows but has not selected. The component segment is never
    read as an architecture or installer token; only the path below it is. An
    architecture token is matched only against `arch_universe`, so name parts that
    merely look like one (`icons-64x64`) are left alone; where several known
    architectures end at one token the longest one owns it. A key with no `/`
    belongs to no component, as a `Contents-<arch>` file at the dists root does, and
    passes the component rule.

    Args:
        key (str): Path as the Release lists it, relative to the layout prefix.
        components (Optional[Set[str]]): Selected components, or `None` for a flat
            repo where paths carry no component segment.
        archs (Set[str]): Selected architectures.
        arch_universe (Set[str]): Every architecture this repository knows about.
        excludes (List[str]): fnmatch globs matched against `key`.

    Returns:
        bool: True when the entry belongs in the sync set.
    """
    for pattern in excludes:
        if fnmatch.fnmatchcase(key, pattern):
            return False

    component, below = _split_component(key, components)
    # a component implies `components` is set; the second test narrows it for mypy
    if component is not None and components is not None and component not in components:
        return False

    if _is_source_index(below) and SOURCE_ARCH not in archs:
        logger.debug("%s is a source index and source is not selected. Skipped.", key)
        return False

    if _is_unparsed_index(below, arch_universe):
        logger.debug("%s is an index nothing parses. Skipped.", key)
        return False

    unselected = _unselected_arch_token(below, archs, arch_universe)
    if unselected is not None:
        logger.debug("%s carries unselected architecture %s. Skipped.", key, unselected)
        return False

    return True
