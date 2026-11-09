"""Coverage for `debkit.argparser` parse-time behaviour."""

from __future__ import annotations

import re
from importlib import metadata

import pytest
from txstore import HISTORY_KEEP_DEFAULT, HISTORY_MAX_AGE_DAYS_DEFAULT

from debkit.action import OutputType
from debkit.argparser import (
    build_parser,
    parse_cli,
)


def test_build_parser_returns_parser() -> None:
    parser = build_parser()
    assert parser.prog


def test_version_flag_matches_the_distribution_version(capsys: pytest.CaptureFixture[str]) -> None:
    """SETUP.md#release keeps the version in two files; `--version` reads only one of them."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])

    assert capsys.readouterr().out.strip() == metadata.version("debkit")


def test_parse_cli_list_command() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "list"])
    assert ns.command == "list"
    assert ns.target_url == "fs:///tmp dist main"


def test_parse_cli_remove_requires_pkg_pattern(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "remove"])
    assert excinfo.value.code == 2
    assert "--pkg-pattern or DEBKIT_PKG_PATTERN is required" in capsys.readouterr().err


@pytest.mark.parametrize("env", [None, ""], ids=["unset", "empty"])
def test_parse_cli_insert_requires_incoming_url(
    env: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: an insert without `--incoming-url` returned silently with exit 0."""
    if env is not None:
        monkeypatch.setenv("DEBKIT_INCOMING_URL", env)
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "insert", "--deb-filename", "foo.deb"])
    assert excinfo.value.code == 2
    assert "--incoming-url or DEBKIT_INCOMING_URL is required" in capsys.readouterr().err


def test_parse_cli_remove_compiles_pkg_pattern() -> None:
    ns = parse_cli(
        ["--target-url", "fs:///tmp dist main", "remove", "--pkg-pattern", '{"Package": "^a"}']
    )
    assert ns.pkg_pattern == {"Package": re.compile("^a")}


@pytest.mark.parametrize("value", ['["a"]', '{"Package": 5}', "{bad", '{"Package": "("}'])
def test_parse_cli_remove_rejects_bad_pkg_pattern(value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_cli(["--target-url", "fs:///tmp dist main", "remove", "--pkg-pattern", value])
    assert exc.value.code == 2


def test_parse_cli_remove_converts_pkg_pattern_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_PKG_PATTERN", '{"Package": "^a"}')
    assert parse_cli(["--target-url", "fs:///tmp dist main", "remove"]).pkg_pattern == {
        "Package": re.compile("^a")
    }
    monkeypatch.setenv("DEBKIT_PKG_PATTERN", '["a"]')
    with pytest.raises(SystemExit) as exc:
        parse_cli(["--target-url", "fs:///tmp dist main", "remove"])
    assert exc.value.code == 2


def test_parse_cli_log_command() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "log"])
    assert ns.command == "log"
    assert ns.output is OutputType.brief


def test_parse_cli_log_command_output_json() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "log", "--output", "json"])
    assert ns.command == "log"
    assert ns.output is OutputType.json


def test_parse_cli_log_rejects_invalid_output(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "log", "--output", "yaml"])
    assert excinfo.value.code == 2
    assert "invalid OutputType value: 'yaml'" in capsys.readouterr().err


def test_parse_cli_revert_command_defaults_dry_run_false() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "revert"])
    assert ns.command == "revert"
    assert ns.dry_run is False


def test_parse_cli_revert_command_dry_run_flag() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "revert", "--dry-run"])
    assert ns.command == "revert"
    assert ns.dry_run is True


def test_parse_cli_sync_command() -> None:
    ns = parse_cli(
        [
            "--target-url",
            "fs:///tmp dist main",
            "sync",
            "--source-url",
            "http://example.com/debian dist main",
        ]
    )
    assert ns.command == "sync"
    assert ns.source_url == "http://example.com/debian dist main"
    assert ns.indices_exclude == ""


def test_parse_cli_sync_indices_exclude_flag() -> None:
    ns = parse_cli(
        ["--target-url", "fs:///tmp dist main", "sync", "--indices-exclude", "*/i18n/*,*/dep11/*"]
    )
    assert ns.indices_exclude == "*/i18n/*,*/dep11/*"


def test_parse_cli_sync_indices_exclude_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_INDICES_EXCLUDE", "*/source/*")
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "sync"])
    assert ns.indices_exclude == "*/source/*"


def test_parse_cli_check_carries_the_selection_defaults() -> None:
    """`check` builds the same sets as `sync`, so it takes the same defaults."""
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "check"])

    assert ns.command == "check"
    assert ns.architectures == "amd64,arm64"
    assert ns.indices_exclude == ""


def test_parse_cli_check_selection_flags() -> None:
    """Without these a check disagrees with the sync that wrote the mirror."""
    ns = parse_cli(
        [
            "--target-url",
            "fs:///tmp dist main",
            "check",
            "--architectures",
            "amd64,source",
            "--indices-exclude",
            "*/i18n/*",
        ]
    )

    assert ns.architectures == "amd64,source"
    assert ns.indices_exclude == "*/i18n/*"


@pytest.mark.parametrize("command", ["sync", "check"])
def test_parse_cli_selection_env_fallback_is_shared(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One registration owns both dests and env names, so the two commands agree.

    A drifting env name on either parser would let a check select a different set
    than the sync that wrote the mirror, and `--fix` deletes what falls outside it.
    """
    monkeypatch.setenv("DEBKIT_ARCHITECTURES", "amd64,source")
    monkeypatch.setenv("DEBKIT_INDICES_EXCLUDE", "*/dep11/*")

    ns = parse_cli(["--target-url", "fs:///tmp dist main", command])

    assert ns.architectures == "amd64,source"
    assert ns.indices_exclude == "*/dep11/*"


# ---------- history retention flags


def test_parse_cli_retention_defaults_match_the_contract() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "prune"])
    assert ns.history_keep == HISTORY_KEEP_DEFAULT
    assert ns.history_max_age == HISTORY_MAX_AGE_DAYS_DEFAULT


def test_parse_cli_retention_flags_override_defaults() -> None:
    ns = parse_cli(
        [
            "--target-url",
            "fs:///tmp dist main",
            "--history-keep",
            "5",
            "--history-max-age",
            "0",
            "prune",
        ]
    )
    assert (ns.history_keep, ns.history_max_age) == (5, 0)


def test_parse_cli_retention_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_HISTORY_KEEP", "3")
    monkeypatch.setenv("DEBKIT_HISTORY_MAX_AGE", "14")
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "prune"])
    assert (ns.history_keep, ns.history_max_age) == (3, 14)


def test_parse_cli_retention_rejects_an_invalid_value(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "--history-keep", "0", "prune"])
    assert excinfo.value.code == 2
    assert "must be 1 or greater, got 0" in capsys.readouterr().err


def test_parse_cli_prune_command_defaults() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "prune"])
    assert ns.command == "prune"
    assert ns.dry_run is False
    assert ns.orphans is False
    assert ns.output == OutputType.brief


def test_parse_cli_prune_command_flags() -> None:
    ns = parse_cli(
        ["--target-url", "fs:///tmp dist main", "prune", "--dry-run", "--orphans", "-o", "json"]
    )
    assert (ns.dry_run, ns.orphans, ns.output) == (True, True, OutputType.json)


# ---------- --target-url


def test_target_url_registered(monkeypatch: pytest.MonkeyPatch) -> None:

    assert (
        parse_cli(["--target-url", "fs:///srv/repo dist main", "list"]).target_url
        == "fs:///srv/repo dist main"
    )


# ---------- boolean switches


@pytest.mark.parametrize("flag", ["-n", "--skip-verify"])
def test_parse_cli_skip_verify_is_a_bare_switch(flag: str) -> None:
    assert parse_cli([flag, "--target-url", "fs:///tmp dist main", "list"]).skip_verify is True
    assert parse_cli(["--target-url", "fs:///tmp dist main", "list"]).skip_verify is False


@pytest.mark.parametrize("dest", ["skip_verify", "skip_verify_ssl", "skip_verify_gpg"])
def test_parse_cli_skip_verify_env_switches_on_and_flag_switches_off(
    dest: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"DEBKIT_{dest.upper()}", "yes")
    assert getattr(parse_cli(["--target-url", "fs:///tmp dist main", "list"]), dest) is True
    flag = "--no-" + dest.replace("_", "-")
    assert getattr(parse_cli([flag, "--target-url", "fs:///tmp dist main", "list"]), dest) is False


@pytest.mark.parametrize(
    "env", ["DEBKIT_SKIP_VERIFY", "DEBKIT_SKIP_VERIFY_SSL", "DEBKIT_SKIP_VERIFY_GPG"]
)
def test_parse_cli_skip_verify_env_zero_keeps_verification(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env, "0")
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "list"])
    assert getattr(ns, env.removeprefix("DEBKIT_").lower()) is False


def test_parse_cli_skip_verify_rejects_an_unknown_env_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEBKIT_SKIP_VERIFY", "maybe")
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "list"])
    assert excinfo.value.code == 2
    assert "DEBKIT_SKIP_VERIFY" in capsys.readouterr().err


def test_parse_cli_sync_allow_release_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        parse_cli(["--target-url", "fs:///tmp dist main", "sync"]).allow_release_rollback is False
    )
    monkeypatch.setenv("DEBKIT_ALLOW_RELEASE_ROLLBACK", "1")
    assert parse_cli(["--target-url", "fs:///tmp dist main", "sync"]).allow_release_rollback is True
    ns = parse_cli(["--target-url", "fs:///tmp dist main", "sync", "--no-allow-release-rollback"])
    assert ns.allow_release_rollback is False


def test_parse_cli_switch_rejects_a_value(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--skip-verify-gpg", "1", "--target-url", "fs:///tmp dist main", "list"])
    assert excinfo.value.code == 2
    assert "invalid choice: '1'" in capsys.readouterr().err


# ---------- environment fallback


def test_target_url_from_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_TARGET_URL", "fs:///srv/repo dist main")
    assert parse_cli(["list"]).target_url == "fs:///srv/repo dist main"


def test_target_url_missing_names_flag_and_variable(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["list"])
    assert excinfo.value.code == 2
    assert "--target-url or DEBKIT_TARGET_URL is required" in capsys.readouterr().err


def test_flag_wins_over_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_HISTORY_KEEP", "3")
    ns = parse_cli(["--history-keep", "5", "--target-url", "fs:///tmp dist main", "list"])
    assert ns.history_keep == 5


def test_empty_variable_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_HISTORY_KEEP", "")
    assert parse_cli(["--target-url", "fs:///tmp dist main", "list"]).history_keep == 20


def test_invalid_variable_exits_2_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEBKIT_HISTORY_MAX_AGE", "x")
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "list"])
    assert excinfo.value.code == 2
    assert "DEBKIT_HISTORY_MAX_AGE" in capsys.readouterr().err


def test_variable_its_converter_refuses_exits_2_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEBKIT_OUTPUT", "yaml")
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "log"])
    assert excinfo.value.code == 2
    assert "DEBKIT_OUTPUT" in capsys.readouterr().err


def test_variable_outside_choices_exits_2_naming_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--log-level` has plain choices and no converter, so the choices check decides."""
    monkeypatch.setenv("DEBKIT_LOG_LEVEL", "tracing")
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--target-url", "fs:///tmp dist main", "log"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "DEBKIT_LOG_LEVEL" in err
    assert "invalid choice: 'tracing'" in err


@pytest.mark.parametrize(("value", "expected"), [("debug", "DEBUG"), ("WARNING", "WARNING")])
def test_log_level_from_flag_or_variable(
    value: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert parse_cli(["-l", value, "--target-url", "fs:///tmp d c", "list"]).log_level == expected
    monkeypatch.setenv("DEBKIT_LOG_LEVEL", value)
    assert parse_cli(["--target-url", "fs:///tmp d c", "list"]).log_level == expected


def test_log_level_rejects_tracing(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["-l", "TRACING", "--target-url", "fs:///tmp d c", "list"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_s3_acl_defaults() -> None:
    ns = parse_cli(["--target-url", "fs:///tmp d c", "list"])
    assert ns.s3_default_acl == "public-read"
    assert ns.s3_bucket_acl is None


def test_secrets_never_appear_in_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("DEBKIT_S3_ACCESS", "DEBKIT_S3_SECRET", "DEBKIT_GNUPG_PASSPHRASE"):
        monkeypatch.setenv(name, "hunter2-secret")
    with pytest.raises(SystemExit):
        parse_cli(["--help"])
    out = capsys.readouterr().out
    assert "hunter2-secret" not in out
    assert "DEBKIT_S3_SECRET" in out
    assert "DEBKIT_GNUPG_PASSPHRASE" in out


@pytest.mark.parametrize("flag", ["--s3-secret=x", "-t", "--gnupg-passphrase=x"])
def test_secret_flags_are_refused(flag: str) -> None:
    """CONFIG.md#secrets: a secret on argv is visible to other local users, so it has no flag."""
    with pytest.raises(SystemExit) as excinfo:
        parse_cli([flag, "x", "--target-url", "fs:///tmp d c", "list"])
    assert excinfo.value.code == 2


def test_secrets_resolve_from_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_S3_SECRET", "s3-secret-value")
    monkeypatch.setenv("DEBKIT_GNUPG_PASSPHRASE", "gpg-passphrase-value")
    ns = parse_cli(["--target-url", "fs:///tmp d c", "list"])
    assert ns.s3_secret == "s3-secret-value"
    assert ns.gnupg_passphrase == "gpg-passphrase-value"


def test_unset_secrets_take_their_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEBKIT_S3_SECRET", raising=False)
    monkeypatch.setenv("DEBKIT_GNUPG_PASSPHRASE", "")
    ns = parse_cli(["--target-url", "fs:///tmp d c", "list"])
    assert ns.s3_secret is None
    assert ns.gnupg_passphrase == ""


def test_access_key_id_keeps_its_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_S3_ACCESS", "from-env")
    assert parse_cli(["-u", "AKID", "--target-url", "fs:///tmp d c", "list"]).s3_access == "AKID"


def test_gnupg_home_defaults_to_the_user_home_not_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout must not be able to plant the trusted keyring -> CONFIG.md#global-flags."""
    monkeypatch.delenv("DEBKIT_GNUPG_HOME", raising=False)

    ns = parse_cli(["--target-url", "/srv/repo bookworm main", "list"])

    assert ns.gnupg_home == "~/.gnupg"
