"""Coverage for `debkit.repo.compress` round-trips."""

from __future__ import annotations

import pytest

from debkit.repo.compress import COMPRESS_MAP, DECOMPRESS_MAP


@pytest.mark.parametrize("suffix", sorted(COMPRESS_MAP))
def test_round_trip(suffix: str) -> None:
    data = b"Package: foo\nVersion: 1.0\n" * 32
    encoded = COMPRESS_MAP[suffix](data)
    assert encoded != data  # actually compressed
    assert DECOMPRESS_MAP[suffix](encoded) == data


def test_maps_share_suffixes() -> None:
    assert set(COMPRESS_MAP) == set(DECOMPRESS_MAP)
