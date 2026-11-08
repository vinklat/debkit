"""Coverage for `debkit.repo.params`.

Mirrors the deb-source rules in ``VALIDATION.md`` § "Debian Source Strings".
"""

from __future__ import annotations

import pytest

from debkit.repo.params import SOURCE_ARCH, RepoParams, RepoParamsException, is_valid_arch


def test_componented_source_parses() -> None:
    p = RepoParams()
    p.from_deb_source("https://example.com/debian bookworm main contrib")
    assert p.url == "https://example.com/debian"
    assert p.dist == "bookworm"
    assert p.components == ["main", "contrib"]
    assert p.is_flat is False
    assert p.indices_prefix == "dists/bookworm"


def test_flat_source_parses() -> None:
    p = RepoParams()
    p.from_deb_source("https://example.com/debian /")
    assert p.url == "https://example.com/debian"
    assert p.dist == "/"
    assert p.components is None
    assert p.is_flat is True
    assert p.indices_prefix == ""


def test_whitespace_collapsed() -> None:
    p = RepoParams()
    p.from_deb_source("   https://example.com/debian   bookworm   main  ")
    assert p.dist == "bookworm"
    assert p.components == ["main"]


def test_one_field_raises() -> None:
    with pytest.raises(RepoParamsException):
        RepoParams().from_deb_source("https://example.com/debian")


def test_non_string_input_raises() -> None:
    with pytest.raises(RepoParamsException):
        RepoParams().from_deb_source(None)


@pytest.mark.parametrize(
    ("dist", "expected"),
    [
        ("bookworm", True),
        ("Bookworm123", True),
        ("", True),  # documented behaviour: empty matches
        ("/", False),
        ("noble-updates", False),
        ("with space", False),
        (None, False),
    ],
)
def test_is_real_dist(dist: str | None, expected: bool) -> None:
    p = RepoParams(dist=dist)
    assert p.is_real_dist() is expected


def test_indices_prefix_with_no_dist() -> None:
    """``dist=None`` must not produce ``dists/None`` (regression guard)."""
    p = RepoParams()
    assert p.indices_prefix == ""


def test_repr_and_equality() -> None:
    a = RepoParams(dist="bookworm")
    b = RepoParams(dist="bookworm")
    assert a == b
    a.url = "https://x"
    assert a != b
    assert "bookworm" in str(a)


_LAYOUTS = [
    # non-flat: dists/<dist>/<comp>/<inner>
    (
        "bookworm",
        False,
        "main",
        "binary-amd64/Packages.xz",
        ("dists/bookworm/main/binary-amd64/Packages.xz", "main/binary-amd64/Packages.xz"),
    ),
    # non-flat, no component (Contents sometimes lives at the dists root)
    (
        "bookworm",
        False,
        None,
        "Contents-amd64.xz",
        ("dists/bookworm/Contents-amd64.xz", "Contents-amd64.xz"),
    ),
    # flat-real-dist: <dist>/<inner>
    ("stable", True, None, "Packages.xz", ("stable/Packages.xz", "Packages.xz")),
    # flat-rootless (e.g. dist == "/")
    ("/", True, None, "Packages.xz", ("Packages.xz", "Packages.xz")),
    # non-flat with empty indices_prefix — falls back to comp-relative
    (
        None,
        False,
        "main",
        "binary-amd64/Packages.xz",
        ("main/binary-amd64/Packages.xz", "main/binary-amd64/Packages.xz"),
    ),
]


@pytest.mark.parametrize(("dist", "is_flat", "comp", "inner", "expected"), _LAYOUTS)
def test_indices_path_layouts(
    dist: str | None, is_flat: bool, comp: str | None, inner: str, expected: tuple[str, str]
) -> None:
    p = RepoParams(dist=dist)
    p.is_flat = is_flat
    assert p.indices_path(inner, comp) == expected[0]


@pytest.mark.parametrize(("dist", "is_flat", "comp", "inner", "expected"), _LAYOUTS)
def test_release_key_round_trips_indices_path(
    dist: str | None, is_flat: bool, comp: str | None, inner: str, expected: tuple[str, str]
) -> None:
    """`release_key` strips exactly the prefix `indices_path` added, in every layout."""
    p = RepoParams(dist=dist)
    p.is_flat = is_flat
    assert p.release_key(p.indices_path(inner, comp)) == expected[1]


def test_release_key_strips_flat_dist_prefix() -> None:
    """A flat repo with a real dist name keys its Release entries without the dist."""
    p = RepoParams(dist="stable")
    p.is_flat = True
    assert p.release_key("stable/Packages.xz") == "Packages.xz"


def test_release_key_leaves_unprefixed_path_unchanged() -> None:
    """A path that never carried the layout prefix round-trips untouched."""
    p = RepoParams(dist="/")
    p.is_flat = True
    assert p.release_key("Packages.xz") == "Packages.xz"


def test_indices_path_flat_without_dist_returns_bare_inner() -> None:
    """A flat repo with no dist has no prefix to add (failure-side of the flat branch)."""
    p = RepoParams()
    p.is_flat = True
    assert p.indices_path("Packages.xz") == "Packages.xz"


@pytest.mark.parametrize(
    ("arch", "expected"),
    [("amd64", "binary-amd64"), ("kfreebsd-amd64", "binary-kfreebsd-amd64"), ("all", "binary-all")],
)
def test_arch_dir_nests_a_binary_architecture(arch: str, expected: str) -> None:
    assert RepoParams.arch_dir(arch) == expected


def test_arch_dir_keeps_the_source_architecture_bare() -> None:
    """Debian publishes source indices under `source/`, never `binary-source/`."""
    assert RepoParams.arch_dir(SOURCE_ARCH) == "source"


@pytest.mark.parametrize(
    ("arch", "expected"), [("amd64", "Packages"), ("all", "Packages"), (SOURCE_ARCH, "Sources")]
)
def test_index_name_is_sources_only_for_the_source_architecture(arch: str, expected: str) -> None:
    assert RepoParams.index_name(arch) == expected


def test_indices_exclude_defaults_to_none() -> None:
    """No exclusion means every Release-listed index is eligible for sync."""
    assert RepoParams().indices_exclude is None


@pytest.mark.parametrize(
    "token", ["amd64", "arm64", "ppc64el", "hurd-i386", "kfreebsd-amd64", "all", "source"]
)
def test_is_valid_arch_accepts_debian_architectures(token: str) -> None:
    assert is_valid_arch(token)


@pytest.mark.parametrize(
    "token", ["", "-amd64", "AMD64", "x/../..", "..", "a.b", "a\\b", "%2e%2e", "amd64\x00"]
)
def test_is_valid_arch_refuses_a_token_that_could_steer_a_path(token: str) -> None:
    assert not is_valid_arch(token)
