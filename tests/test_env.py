"""Coverage for `debkit._env` → [VALIDATION.md](../VALIDATION.md#environment-values)."""

from __future__ import annotations

import pytest

from debkit._env import env_bool, env_str


def test_env_str_reads_a_set_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_X", "value")
    assert env_str("DEBKIT_X") == "value"


@pytest.mark.parametrize("value", [None, ""])
def test_env_str_unset_or_empty_is_none(value: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if value is not None:
        monkeypatch.setenv("DEBKIT_X", value)
    assert env_str("DEBKIT_X") is None


def test_env_str_refuses_another_prefix() -> None:
    with pytest.raises(ValueError, match="lacks the DEBKIT_ prefix"):
        env_str("TARGET_URL")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), (" Yes ", True), ("on", True), ("TRUE", True), ("0", False), ("off", False)],
)
def test_env_bool_vocabulary(value: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_X", value)
    assert env_bool("DEBKIT_X") is expected


def test_env_bool_unset_is_none() -> None:
    assert env_bool("DEBKIT_X") is None


@pytest.mark.parametrize("value", ["2", "enabled", "maybe"])
def test_env_bool_rejects_other_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_X", value)
    with pytest.raises(ValueError, match="expected a boolean"):
        env_bool("DEBKIT_X")
