"""Tests for action/__init__.py: the output enum, JSON encoder, shared CLI args, and the
`attach_storage_*` helpers that turn the target URL into a storage.

`make_storage()` URL dispatch itself is txstore's; its tests cover it.
"""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest
from helpers.factories import make_action_namespace
from helpers.storage import storage_namespace
from txstore import HistoryRetention, StorageException, StorageFS, StorageHTTP

from debkit._options import EnvArgumentParser
from debkit.action import (
    OUTPUT_DEFAULT,
    Action,
    ActionException,
    OutputType,
    RepoJSONEncoder,
    attach_storage_plain,
    attach_storage_repo,
    attach_storage_root,
    dump_json,
    history_retention,
    open_storage,
    storage_url,
)
from debkit.repo import RepoReadException
from debkit.repo.params import RepoParamsException

# --- OutputType ---------------------------------------------------------------------------------


def test_output_type_str_is_the_flag_value() -> None:
    """`choices=list(OutputType)` renders these in the usage text."""
    assert str(OutputType.brief) == "brief"
    assert str(OutputType.json) == "json"


def test_output_type_is_built_from_the_flag_value() -> None:
    assert OutputType("json") is OutputType.json


def test_output_type_rejects_an_unknown_value() -> None:
    with pytest.raises(ValueError, match="not a valid OutputType"):
        OutputType("yaml")


def test_the_default_output_is_brief() -> None:
    assert OUTPUT_DEFAULT is OutputType.brief


# --- ActionException ----------------------------------------------------------------------------


def test_action_exception_prefixes_its_message() -> None:
    assert str(ActionException("no package matched")) == "Action: no package matched"


# --- JSON output --------------------------------------------------------------------------------


def test_dump_json_serialises_a_plain_structure() -> None:
    assert json.loads(dump_json({"id": 1})) == {"id": 1}


def test_dump_json_delegates_to_a_dump_method() -> None:
    class _Dumpable:
        def dump(self) -> dict[str, int]:
            return {"size": 2}

    assert json.loads(dump_json({"pkg": _Dumpable()})) == {"pkg": {"size": 2}}


def test_dump_json_indents_by_two_by_default() -> None:
    assert dump_json({"id": 1}) == '{\n  "id": 1\n}'


def test_dump_json_honours_an_explicit_indent() -> None:
    assert dump_json({"id": 1}, indent=0) == '{\n"id": 1\n}'


def test_the_encoder_still_rejects_an_unserialisable_object() -> None:
    with pytest.raises(TypeError):
        json.dumps(object(), cls=RepoJSONEncoder)


# --- shared CLI args ----------------------------------------------------------------------------


def test_add_output_cli_args_defaults_to_brief() -> None:
    parser = EnvArgumentParser()
    Action.add_output_cli_args(parser)

    assert parser.parse_args([]).output is OutputType.brief


def test_add_output_cli_args_converts_the_value_to_the_enum() -> None:
    parser = EnvArgumentParser()
    Action.add_output_cli_args(parser)

    assert parser.parse_args(["--output", "json"]).output is OutputType.json


def test_add_output_cli_args_rejects_an_unknown_format() -> None:
    parser = EnvArgumentParser()
    Action.add_output_cli_args(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--output", "yaml"])


def test_add_output_cli_args_falls_back_to_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBKIT_OUTPUT", "json")
    parser = EnvArgumentParser()
    Action.add_output_cli_args(parser)

    assert parser.parse_args([]).output is OutputType.json
    assert parser.parse_args(["--output", "brief"]).output is OutputType.brief


# --- Action.__init__ ----------------------------------------------------------------------------


def _pars(tmp_path: Path, **flags: object) -> Namespace:
    """Namespace `Action.__init__` accepts for an `fs://` target without GPG."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    return make_action_namespace(repo_dir, **flags)


def test_action_init_builds_history_retention_from_pars(tmp_path: Path) -> None:
    action = Action(_pars(tmp_path, history_keep=3, history_max_age=7))
    assert action.history_retention == HistoryRetention(keep=3, max_age_days=7)


def test_action_init_defaults_history_retention_without_the_flags(tmp_path: Path) -> None:
    action = Action(_pars(tmp_path))
    assert action.history_retention == HistoryRetention()
    assert action.history_retention.enabled


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ({"skip_verify": True, "skip_verify_gpg": True}, "--skip-verify is set"),
        ({"skip_verify": False, "skip_verify_gpg": True}, "--skip-verify-gpg is set"),
    ],
    ids=["skip-verify", "skip-verify-gpg"],
)
def test_action_init_warns_once_when_verification_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, flags: dict[str, bool], message: str
) -> None:
    with caplog.at_level(logging.WARNING):
        action = Action(_pars(tmp_path, **flags))

    assert action.gpg is None
    skipped = [rec for rec in caplog.records if "verification skipped" in rec.getMessage()]
    assert len(skipped) == 1
    assert skipped[0].levelno == logging.WARNING
    assert message in skipped[0].getMessage()


def test_action_init_does_not_warn_with_verification_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch("debkit.action.GnuPG") as gnupg,
        caplog.at_level(logging.WARNING),
    ):
        action = Action(_pars(tmp_path, skip_verify=False, skip_verify_gpg=False))

    assert action.gpg is gnupg.return_value
    assert "verification skipped" not in caplog.text


# --- attach_storage_* ----------------------------------------------------------------------------


def test_attach_storage_repo_invalid_url() -> None:
    """A deb-source URL with only one field cannot become a valid RepoParams."""
    pars = storage_namespace(target_url="https://example.com/debian")
    with pytest.raises(RepoParamsException):
        attach_storage_repo(pars)


def test_attach_storage_repo_wraps_storage_url_errors() -> None:
    """Repository attachment preserves read-error CLI semantics."""
    pars = storage_namespace(target_url="http:///no-host bookworm main")
    with pytest.raises(RepoReadException) as exc_info:
        attach_storage_repo(pars)

    assert "Invalid HTTP URL" in str(exc_info.value)


def test_attach_storage_repo_fs() -> None:
    pars = storage_namespace(target_url="fs:///tmp/repo bookworm main")
    storage, params = attach_storage_repo(pars)
    assert isinstance(storage, StorageFS)
    assert params.dist == "bookworm"
    assert params.components == ["main"]


def test_attach_storage_repo_raises_on_empty_url() -> None:
    pars = storage_namespace(target_url="    ")
    with pytest.raises((RepoReadException, RepoParamsException)):
        attach_storage_repo(pars)


def test_attach_storage_plain_skips_deb_source_parsing() -> None:
    pars = storage_namespace()
    storage = attach_storage_plain(pars, url="http://example.com/incoming")
    assert isinstance(storage, StorageHTTP)


def test_attach_storage_plain_raises_on_missing_url() -> None:
    """A missing URL (None after fallback) must fail at the action boundary."""
    pars = storage_namespace(target_url=None)
    with pytest.raises(StorageException):
        attach_storage_plain(pars)


def test_attach_storage_root_accepts_bare_url() -> None:
    """Root-scoped commands need no dist/components fields."""
    pars = storage_namespace(target_url="fs:///tmp/repo")
    storage = attach_storage_root(pars)
    assert isinstance(storage, StorageFS)


def test_attach_storage_root_ignores_dist_components() -> None:
    """The documented `<url> <dist> <component>` form still resolves to the root."""
    bare = attach_storage_root(storage_namespace(target_url="fs:///tmp/repo"))
    suffixed = attach_storage_root(storage_namespace(target_url="fs:///tmp/repo bookworm main"))
    assert isinstance(bare, StorageFS)
    assert isinstance(suffixed, StorageFS)
    assert suffixed.base_dir == bare.base_dir


def test_attach_storage_root_raises_on_blank_url() -> None:
    for url in (None, "", "   "):
        with pytest.raises(StorageException):
            attach_storage_root(storage_namespace(target_url=url))


# --- open_storage / storage_url → STORAGE.md#construction ------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("s3://endpoint/bucket", "s3://endpoint/bucket"),
        ("fs:///srv/repo", "fs:///srv/repo"),
        ("https://deb.debian.org/debian", "https://deb.debian.org/debian"),
    ],
)
def test_storage_url_keeps_a_url(url: str, expected: str) -> None:
    assert storage_url(url) == expected


def test_storage_url_makes_a_bare_path_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert storage_url("repo") == f"fs://{tmp_path.resolve() / 'repo'}"
    assert storage_url("/srv/repo") == "fs:///srv/repo"


def test_open_storage_opens_a_bare_path(tmp_path: Path) -> None:
    storage = open_storage(storage_namespace(), str(tmp_path), create_path=False)
    assert isinstance(storage, StorageFS)


def test_open_storage_refuses_a_relative_fs_url() -> None:
    with pytest.raises(ValueError, match="absolute"):
        open_storage(storage_namespace(), "fs://relative/repo")


def test_open_storage_passes_the_resolved_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_make_storage(url: str, **kwargs: object) -> object:
        calls.append({"url": url, **kwargs})
        return object()

    monkeypatch.setattr("debkit.action.make_storage", fake_make_storage)
    pars = storage_namespace(skip_verify=False, skip_verify_ssl=False, s3_bucket_acl="private")
    open_storage(pars, "s3://endpoint/bucket", create_path=True)
    assert calls == [
        {
            "url": "s3://endpoint/bucket",
            "create_path": True,
            "verify_ssl": True,
            "s3_access": "k",
            "s3_secret": "s",
            "s3_default_acl": "public-read",
            "s3_bucket_acl": "private",
        }
    ]


@pytest.mark.parametrize(("skip", "skip_ssl"), [(True, False), (False, True)])
def test_open_storage_skip_flags_turn_tls_verification_off(
    skip: bool, skip_ssl: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(
        "debkit.action.make_storage", lambda url, **kw: seen.append(kw["verify_ssl"])
    )
    open_storage(storage_namespace(skip_verify=skip, skip_verify_ssl=skip_ssl), "fs:///srv")
    assert seen == [False]


def test_history_retention_reads_the_flags() -> None:
    retention = history_retention(storage_namespace(history_keep=3, history_max_age=7))
    assert (retention.keep, retention.max_age_days) == (3, 7)
