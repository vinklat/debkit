"""Coverage for debkit.repo.index_selection.

The selection rules are pure functions over Release keys, so each case calls
`select_index_entry` directly -> VALIDATION.md#index-selection.
"""

from __future__ import annotations

import pytest

from debkit.repo.index_selection import select_index_entry


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("main/binary-amd64/Packages.xz", True),
        ("main/binary-i386/Packages.xz", False),
        ("main/Contents-udeb-i386.gz", False),
        ("main/Contents-all.gz", True),
        ("main/dep11/icons-64x64.tar.gz", True),
        ("main/dep11/CID-Index-i386.json.gz", False),
        ("main/i18n/Translation-en.xz", True),
        ("main/debian-installer/binary-i386/Packages.gz", False),
        ("contrib/binary-amd64/Packages.xz", False),
        # a Contents file at the dists root belongs to no component
        ("Contents-amd64.gz", True),
        ("Contents-i386.gz", False),
    ],
)
def test_select_index_entry_component_and_arch_rules(key: str, expected: bool) -> None:
    assert (
        select_index_entry(
            key,
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64", "i386"},
            excludes=[],
        )
        is expected
    )


def test_select_index_entry_flat_repo_keeps_component_less_paths() -> None:
    """A flat repo has no component segment, so the component rule does not apply."""
    assert (
        select_index_entry(
            "Packages.xz",
            components=None,
            archs={"amd64"},
            arch_universe={"amd64"},
            excludes=[],
        )
        is True
    )


def test_select_index_entry_exclude_glob_wins_over_selection() -> None:
    assert (
        select_index_entry(
            "main/i18n/Translation-en.bz2",
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64"},
            excludes=["*/i18n/*"],
        )
        is False
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("main/binary-kfreebsd-amd64/Packages.xz", True),
        ("main/Contents-kfreebsd-amd64.gz", True),
        # a selected arch is kept even though its suffix arch is unselected
        ("main/binary-hurd-i386/Packages.xz", False),
        ("main/binary-amd64/Packages.xz", False),
        ("main/binary-i386/Packages.xz", False),
    ],
)
def test_select_index_entry_longest_architecture_token_owns_the_match(
    key: str, expected: bool
) -> None:
    """A hyphenated arch owns its token, so an unselected suffix cannot drop it."""
    assert (
        select_index_entry(
            key,
            components={"main"},
            archs={"kfreebsd-amd64"},
            arch_universe={"kfreebsd-amd64", "amd64", "i386", "hurd-i386"},
            excludes=[],
        )
        is expected
    )


def test_select_index_entry_keeps_every_key_when_no_architecture_is_known() -> None:
    """With an empty universe no name part can be an architecture token."""
    assert (
        select_index_entry(
            "main/binary-amd64/Packages.xz",
            components={"main"},
            archs=set(),
            arch_universe=set(),
            excludes=[],
        )
        is True
    )


@pytest.mark.parametrize(
    ("key", "wanted"),
    [
        ("extra-amd64/binary-arm64/Packages.xz", True),
        ("installer-amd64/binary-arm64/Packages.xz", True),
        ("debian-installer/binary-arm64/Packages.xz", True),
        ("Contents-udeb-extras/binary-arm64/Packages.xz", True),
        # the path below the component still carries the unselected token
        ("extra-amd64/binary-amd64/Packages.xz", False),
    ],
)
def test_select_index_entry_component_name_is_not_read_as_a_token(key: str, wanted: bool) -> None:
    """A component is a chosen name, so only the path below it can trip a structural rule."""
    assert (
        select_index_entry(
            key,
            components={key.partition("/")[0]},
            archs={"arm64"},
            arch_universe={"amd64", "arm64"},
            excludes=[],
        )
        is wanted
    )


@pytest.mark.parametrize(
    ("key", "wanted"),
    [
        ("updates/main/binary-amd64/Packages.xz", True),
        ("updates/main/binary-i386/Packages.xz", False),
        ("updates/contrib/binary-amd64/Packages.xz", False),
        # the longest selected component owns the key
        ("main/debug/binary-amd64/Packages.xz", True),
    ],
)
def test_select_index_entry_component_with_slash_is_matched_whole(key: str, wanted: bool) -> None:
    """A component holding `/` is read whole, the longest selected one first."""
    assert (
        select_index_entry(
            key,
            components={"updates/main", "main", "main/debug"},
            archs={"amd64"},
            arch_universe={"amd64", "i386"},
            excludes=[],
        )
        is wanted
    )


@pytest.mark.parametrize(
    "key",
    [
        "main/source/Sources.xz",
        "main/source/Release",
        "main/source/by-hash/SHA256/0123abcd",
        "main/source/Sources.diff/Index",
        "main/Contents-source.gz",
        "Contents-source.gz",
    ],
)
@pytest.mark.parametrize("selected", [False, True])
def test_select_index_entry_source_indices_follow_the_source_selection(
    key: str, selected: bool
) -> None:
    """An unselected source architecture drops its indices, as any other would.

    The universe omits `source`, as a Debian `Architectures` field does, so the source
    rule alone has to own every shape of the key.
    """
    archs = {"amd64", "source"} if selected else {"amd64"}

    assert (
        select_index_entry(
            key,
            components={"main"},
            archs=archs,
            arch_universe={"amd64", "i386"},
            excludes=[],
        )
        is selected
    )


@pytest.mark.parametrize(
    ("key", "wanted"),
    [
        ("main/debian-installer/binary-amd64/Packages.gz", False),
        ("main/debian-installer/binary-amd64/Release", False),
        ("main/debian-installer/binary-amd64/Packages.diff/Index", False),
        ("main/installer-amd64/current/images/SHA256SUMS", False),
        ("main/installer-amd64/20230607+deb12u12/images/MD5SUMS", False),
        ("main/Contents-udeb-amd64.gz", False),
        ("Contents-udeb-amd64.gz", False),
        ("main/binary-amd64/Packages.gz", True),
        ("main/Contents-amd64.gz", True),
        # an installer tree names an architecture; any other suffix is a plain directory
        ("main/installer-extras/binary-amd64/Packages.gz", True),
    ],
)
def test_select_index_entry_unparsed_index_directory_is_dropped(key: str, wanted: bool) -> None:
    """Nothing parses these, so mirroring one would advertise udebs the mirror lacks.

    The selected architecture is the point: `-amd64` is wanted everywhere else, so
    only the unparsed rule can drop an installer tree or a udeb listing named for it.
    """
    assert (
        select_index_entry(
            key,
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64", "i386"},
            excludes=[],
        )
        is wanted
    )


def test_select_index_entry_unparsed_directory_is_independent_of_user_excludes() -> None:
    """The rule is structural: a user glob replaces the exclude list, not this."""
    assert (
        select_index_entry(
            "main/debian-installer/binary-amd64/Packages.gz",
            components={"main"},
            archs={"amd64"},
            arch_universe={"amd64"},
            excludes=["*/i18n/*"],
        )
        is False
    )


@pytest.mark.parametrize("selected", [False, True])
def test_select_index_entry_flat_sources_index_follows_the_source_selection(
    selected: bool,
) -> None:
    """A flat repo keeps its `Sources` index at the root, with no component segment."""
    archs = {"amd64", "source"} if selected else {"amd64"}

    assert (
        select_index_entry(
            "Sources.xz",
            components=None,
            archs=archs,
            arch_universe={"amd64"},
            excludes=[],
        )
        is selected
    )
