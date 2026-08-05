"""Behavioral tests for the advertised-data cache."""

import json
from pathlib import Path

import pytest

from acpc import cache


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def test_refresh_publishes_adapter_data_and_full_command_descriptions(state_root: Path) -> None:
    cache.refresh_advertised(
        "mock",
        {
            "modes": [{"id": "default"}],
            "models": ["mock-model"],
            "commands": [
                {
                    "name": "plan",
                    "description": "Make a plan. The complete explanation stays in the cache.",
                }
            ],
        },
    )

    record = cache.read_advertised("mock")
    assert record is not None
    assert record.advertised["models"] == ["mock-model"]
    assert (state_root / "cache" / "mock" / "commands.md").read_text(encoding="utf-8") == (
        "# /plan\n\nMake a plan. The complete explanation stays in the cache.\n"
    )
    assert json.loads(
        (state_root / "cache" / "mock" / "advertised.json").read_text(encoding="utf-8")
    )["advertised"]["modes"] == [{"id": "default"}]


def test_cache_age_uses_the_injected_clock() -> None:
    assert cache.cache_age(100.0, now=lambda: 7_300.0) == "2h"


def test_empty_warm_turn_data_preserves_the_last_catalogs() -> None:
    cache.refresh_advertised("mock", {"models": ["first-model"]})
    cache.refresh_advertised("mock", {"modes": [], "models": [], "commands": []})

    record = cache.read_advertised("mock")
    assert record is not None
    assert record.advertised["models"] == ["first-model"]


def test_refresh_swallows_a_non_serializable_cache_value(state_root: Path) -> None:
    cache.refresh_advertised("mock", {"models": [object()]})

    assert not (state_root / "cache" / "mock" / "advertised.json").exists()
