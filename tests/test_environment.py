"""Tests for adapter environment construction in acpc.environment."""

from pathlib import Path

import pytest

from acpc.environment import (
    DAEMON_ENV_PAYLOAD,
    adapter_environment,
    base_environment,
    environment_overrides,
    passthrough_names,
)


def test_base_environment_allowlists_capability_and_acpc_variables() -> None:
    ambient = {
        "ACPC_TEST_VALUE": "from-ambient",
        DAEMON_ENV_PAYLOAD: "secret-payload",
        "SSH_AUTH_SOCK": "/run/agent.sock",
        "https_proxy": "http://proxy:3128",
        "MOCK_API_KEY": "credential",
        "PYTHONPATH": "not-for-adapters",
        "LD_PRELOAD": "not-for-adapters",
    }

    base = base_environment(ambient)

    assert base["ACPC_TEST_VALUE"] == "from-ambient"
    assert base["SSH_AUTH_SOCK"] == "/run/agent.sock"
    assert base["https_proxy"] == "http://proxy:3128"
    assert DAEMON_ENV_PAYLOAD not in base
    assert "MOCK_API_KEY" not in base
    assert "PYTHONPATH" not in base
    assert "LD_PRELOAD" not in base
    assert base["TERM"] == "dumb"


def test_environment_overrides_prefer_declared_values_over_passthrough() -> None:
    ambient = {"MOCK_API_KEY": "ambient", "OTHER": "x"}

    assert environment_overrides({}, ("MOCK_API_KEY",), ambient=ambient) == {
        "MOCK_API_KEY": "ambient"
    }
    assert environment_overrides(
        {"MOCK_API_KEY": "declared"}, ("MOCK_API_KEY",), ambient=ambient
    ) == {"MOCK_API_KEY": "declared"}
    assert environment_overrides({}, ("MISSING",), ambient=ambient) == {}


def test_adapter_environment_layers_and_forwards_resolved_acpc_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("ACPC_HOME", str(link_root / "state"))

    environment = adapter_environment(
        {"MOCK_HOME": "/home/vendor"},
        ("MOCK_API_KEY",),
        ambient={"MOCK_API_KEY": "credential", "PYTHONPATH": "not-for-adapters"},
    )

    assert environment["ACPC_HOME"] == str((real_root / "state").resolve())
    assert environment["MOCK_HOME"] == "/home/vendor"
    assert environment["MOCK_API_KEY"] == "credential"
    assert "PYTHONPATH" not in environment
    assert environment["TERM"] == "dumb"


def test_passthrough_names_deduplicate_preserving_order() -> None:
    assert passthrough_names(("B", "A", "B")) == ("B", "A")
