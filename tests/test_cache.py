"""Behavioral tests for the advertised-data cache."""

from pathlib import Path

import pytest

from acpc import cache


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def test_refresh_publishes_adapter_data() -> None:
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


def test_refresh_writes_full_command_descriptions(state_root: Path) -> None:
    cache.refresh_advertised(
        "mock",
        {
            "commands": [
                {
                    "name": "plan",
                    "description": "Make a plan. The complete explanation stays in the cache.",
                }
            ]
        },
    )

    assert (state_root / "cache" / "mock" / "commands.md").read_text(encoding="utf-8") == (
        "# /plan\n\nMake a plan. The complete explanation stays in the cache.\n"
    )


def test_cache_age_uses_the_injected_clock() -> None:
    assert cache.cache_age(100.0, clock=lambda: 7_300.0) == "2h"


def test_empty_warm_turn_data_preserves_the_last_catalogs() -> None:
    cache.refresh_advertised("mock", {"models": ["first-model"]})
    cache.refresh_advertised("mock", {"modes": [], "models": [], "commands": []})

    record = cache.read_advertised("mock")
    assert record is not None
    assert record.advertised["models"] == ["first-model"]


def test_refresh_keeps_cached_at_when_advertised_data_is_unchanged(state_root: Path) -> None:
    advertised = {"models": ["same-model"]}
    cache.refresh_advertised("mock", advertised, clock=lambda: 100.0)
    first = cache.read_advertised("mock")
    assert first is not None
    cache_file = state_root / "cache" / "mock" / "advertised.json"
    first_bytes = cache_file.read_bytes()

    cache.refresh_advertised("mock", advertised, clock=lambda: 200.0)

    second = cache.read_advertised("mock")
    assert second is not None
    assert second.cached_at == first.cached_at == 100.0
    assert cache_file.read_bytes() == first_bytes


def test_refresh_swallows_a_non_serializable_cache_value(state_root: Path) -> None:
    cache.refresh_advertised("mock", {"models": [object()]})

    assert not (state_root / "cache" / "mock" / "advertised.json").exists()


def test_effort_rows_merge_by_model_and_ignore_observation_age(state_root: Path) -> None:
    first = {"levels": ["low", "high"], "current": "high", "observed_at": 100.0}
    other = {"levels": [], "current": None, "observed_at": 200.0}
    cache.refresh_advertised("mock", {"efforts": {"first": first}}, clock=lambda: 100.0)
    cache.refresh_advertised("mock", {"efforts": {"other": other}}, clock=lambda: 200.0)
    cache_file = state_root / "cache/mock/advertised.json"
    before = cache_file.read_bytes()
    cache.refresh_advertised("mock", {"efforts": {"first": {**first, "observed_at": 300.0}}})
    assert cache_file.read_bytes() == before
    record = cache.read_advertised("mock")
    assert record is not None
    assert record.advertised["efforts"] == {"first": first, "other": other}
    changed = {**first, "current": "low", "observed_at": 400.0}
    cache.refresh_advertised("mock", {"efforts": {"first": changed}})
    record = cache.read_advertised("mock")
    assert record is not None
    assert record.advertised["efforts"] == {"first": changed, "other": other}


def test_forced_refresh_publishes_fresh_timestamps(state_root: Path) -> None:
    row = {"levels": [], "current": None, "observed_at": 100.0}
    cache.refresh_advertised("mock", {"efforts": {"model": row}}, clock=lambda: 100.0)
    row = {**row, "observed_at": 200.0}
    cache.refresh_advertised("mock", {"efforts": {"model": row}}, clock=lambda: 200.0, force=True)
    record = cache.read_advertised("mock")
    assert record is not None
    assert record.cached_at == 200.0
    assert record.advertised["efforts"]["model"]["observed_at"] == 200.0


@pytest.mark.parametrize("legacy_efforts", [None, [], "broken"])
def test_legacy_cache_reads_and_gains_effort_rows(state_root: Path, legacy_efforts: object) -> None:
    import json

    cache_file = state_root / "cache/mock/advertised.json"
    cache_file.parent.mkdir(parents=True)
    advertised: dict[str, object] = {"models": ["legacy"]}
    if legacy_efforts is not None:
        advertised["efforts"] = legacy_efforts
    cache_file.write_text(json.dumps({"cached_at": 100.0, "advertised": advertised}))
    record = cache.read_advertised("mock")
    assert record is not None
    assert record.advertised["efforts"] == {}
    row = {"levels": [], "current": None, "observed_at": 200.0}
    cache.refresh_advertised("mock", {"efforts": {"legacy": row}})
    record = cache.read_advertised("mock")
    assert record is not None
    assert record.advertised["efforts"] == {"legacy": row}
    assert record.advertised["models"] == ["legacy"]
