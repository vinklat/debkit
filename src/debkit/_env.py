"""Read ``DEBKIT_*`` environment variables.

The only module that reads `os.environ`. Each helper returns one variable's raw or parsed value,
or None when it is unset or empty; the CLI resolves a flag first, then the variable, then the
default → [CONFIG.md](CONFIG.md#resolution). Grammar →
[VALIDATION.md](VALIDATION.md#environment-values). `child_env` builds the environment a child
process runs with → [CONFIG.md](CONFIG.md#secrets).
"""

from __future__ import annotations

import os

ENV_PREFIX = "DEBKIT_"
# prefixes of the variables that carry debkit's and txstore's secrets and credentialed URLs
_CHILD_DROPPED_PREFIXES = (ENV_PREFIX, "TXSTORE_")

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def env_str(name: str) -> str | None:
    """Return the value of variable `name`, or None when unset or empty.

    Raises:
        ValueError: If `name` lacks the ``DEBKIT_`` prefix (a programming error).
    """
    if not name.startswith(ENV_PREFIX):
        raise ValueError(f"environment variable {name!r} lacks the {ENV_PREFIX} prefix")
    value = os.environ.get(name)
    return value if value else None


def env_bool(name: str) -> bool | None:
    """Return the boolean value of `name`, or None when unset or empty.

    Accepts ``1/0``, ``true/false``, ``yes/no``, and ``on/off`` in any case.

    Raises:
        ValueError: If the value is outside that vocabulary.
    """
    value = env_str(name)
    if value is None:
        return None
    folded = value.strip().lower()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise ValueError(f"expected a boolean (1/0, true/false, yes/no, on/off), got {value!r}")


def child_env() -> dict[str, str]:
    """Return the environment a child process runs with: this one without debkit's variables.

    A child such as `zstd` or `gpg` reads untrusted data and needs none of them, so it never
    holds a secret or a credentialed URL → [CONFIG.md](CONFIG.md#secrets). `PATH` and the
    rest stay, so the child is found and runs as before.

    Returns:
        A copy of the environment without the ``DEBKIT_*`` and ``TXSTORE_*`` variables.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_CHILD_DROPPED_PREFIXES)
    }
