"""Markers for tests that run an external binary.

Skip rule → [TESTING.md](../../TESTING.md#harness-and-gates).
"""

from __future__ import annotations

import os
import shutil

import pytest


def requires_binary(name: str) -> pytest.MarkDecorator:
    """Skip where the `name` binary is missing, but never in CI.

    A CI job without the binary then fails instead of passing on skipped tests.

    Args:
        name: The binary looked up on `PATH`.

    Returns:
        The skip marker.
    """
    return pytest.mark.skipif(
        shutil.which(name) is None and not os.environ.get("CI"),
        reason=f"needs the {name} binary",
    )
