"""Tests for daemon target keying in acpc.targets.

The properties SPEC.md demands: stable for identical calls, distinct per
entry/home/declared-env/credential, path-safe, and never containing a secret
value in readable form.
"""

from pathlib import Path

import pytest

from acpc.targets import target_for_call


def test_bare_entry_with_no_environment_is_just_the_name() -> None:
    assert target_for_call("codex") == "codex"


def test_digest_bearing_target_requires_a_valid_policy() -> None:
    with pytest.raises(ValueError, match="permissions is required"):
        target_for_call("codex", home="~/.codex")
    with pytest.raises(ValueError, match="permissions must be one of"):
        target_for_call("codex", permissions=None)


def test_different_policies_are_different_targets() -> None:
    read = target_for_call("codex", permissions="read")
    edit = target_for_call("codex", permissions="edit")

    assert read != edit
    assert target_for_call("codex", permissions="read") == read


def test_identical_calls_produce_identical_targets() -> None:
    a = target_for_call("builder", home="~/.codex-x", declared_env={"K": "v"}, permissions="read")
    b = target_for_call("builder", home="~/.codex-x", declared_env={"K": "v"}, permissions="read")
    assert a == b


def test_different_homes_are_different_targets() -> None:
    a = target_for_call("codex", home="/home/a", permissions="read")
    b = target_for_call("codex", home="/home/b", permissions="read")
    assert a != b


def test_different_declared_env_values_are_different_targets() -> None:
    a = target_for_call(
        "builder", declared_env={"MODEL_PROVIDER": "openrouter"}, permissions="read"
    )
    b = target_for_call("builder", declared_env={"MODEL_PROVIDER": "openai"}, permissions="read")
    assert a != b


def test_different_credentials_are_different_targets_without_leaking_them() -> None:
    secret_a = "sk-aaaaaaaaaaaaaaaa"
    secret_b = "sk-bbbbbbbbbbbbbbbb"
    a = target_for_call(
        "builder", passthrough_values={"OPENROUTER_API_KEY": secret_a}, permissions="read"
    )
    b = target_for_call(
        "builder", passthrough_values={"OPENROUTER_API_KEY": secret_b}, permissions="read"
    )
    assert a != b
    assert secret_a not in a
    assert secret_b not in b


def test_targets_are_path_safe() -> None:
    target = target_for_call("weird/entry name", home="/x/../y", permissions="read")
    assert "/" not in target
    assert " " not in target
    assert Path(target).name == target


def test_long_entry_names_stay_under_the_byte_cap() -> None:
    target = target_for_call("x" * 400, home="/somewhere", permissions="read")
    assert len(target.encode("utf-8")) <= 180
    assert target.startswith("x")
    assert "~" in target


def test_spawn_identity_is_part_of_the_digest() -> None:
    a = target_for_call("grok", permissions="execute", spawn_identity={"effort": "low"})
    b = target_for_call("grok", permissions="execute", spawn_identity={"effort": "high"})
    c = target_for_call("grok", permissions="execute")
    assert a != b
    assert a != c
    assert target_for_call("grok", permissions="execute", spawn_identity={"effort": "low"}) == a


def test_empty_spawn_identity_keeps_the_06_digest() -> None:
    target = target_for_call(
        "codex",
        home="/srv/codex",
        declared_env={"MODEL_PROVIDER": "openai"},
        passthrough_values={"OPENROUTER_API_KEY": "secret"},
        permissions="read",
    )

    assert target == "codex~99efc3ea373a12a6"


def test_spawn_identity_includes_the_cli_effort_flag() -> None:
    without_flag = target_for_call("grok", permissions="execute", spawn_identity={"effort": "high"})
    with_flag = target_for_call(
        "grok",
        permissions="execute",
        spawn_identity={"effort": "high", "effort_cli_flag": "--effort"},
    )

    assert without_flag != with_flag
