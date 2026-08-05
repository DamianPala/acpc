"""Tests for daemon target keying in acpc.targets.

The properties SPEC.md demands: stable for identical calls, distinct per
entry/home/declared-env/credential, path-safe, and never containing a secret
value in readable form.
"""

from pathlib import Path

from acpc.targets import target_for_call


def test_bare_entry_with_no_environment_is_just_the_name() -> None:
    assert target_for_call("codex") == "codex"


def test_identical_calls_produce_identical_targets() -> None:
    a = target_for_call("builder", home="~/.codex-x", declared_env={"K": "v"})
    b = target_for_call("builder", home="~/.codex-x", declared_env={"K": "v"})
    assert a == b


def test_different_homes_are_different_targets() -> None:
    a = target_for_call("codex", home="/home/a")
    b = target_for_call("codex", home="/home/b")
    assert a != b


def test_different_declared_env_values_are_different_targets() -> None:
    a = target_for_call("builder", declared_env={"MODEL_PROVIDER": "openrouter"})
    b = target_for_call("builder", declared_env={"MODEL_PROVIDER": "openai"})
    assert a != b


def test_different_credentials_are_different_targets_without_leaking_them() -> None:
    secret_a = "sk-aaaaaaaaaaaaaaaa"
    secret_b = "sk-bbbbbbbbbbbbbbbb"
    a = target_for_call("builder", passthrough_values={"OPENROUTER_API_KEY": secret_a})
    b = target_for_call("builder", passthrough_values={"OPENROUTER_API_KEY": secret_b})
    assert a != b
    assert secret_a not in a
    assert secret_b not in b


def test_targets_are_path_safe() -> None:
    target = target_for_call("weird/entry name", home="/x/../y")
    assert "/" not in target
    assert " " not in target
    assert Path(target).name == target


def test_long_entry_names_stay_under_the_byte_cap() -> None:
    target = target_for_call("x" * 400, home="/somewhere")
    assert len(target.encode("utf-8")) <= 180
    assert target.startswith("x")
    assert "~" in target
