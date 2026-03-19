"""Model cache for acpc.

Caches available_models from ACP new_session() responses.
Cache refreshes as a side effect of every new session (zero extra cost).
TTL: 7 days. After expiry, acpc models triggers a live fetch.
"""

import json
import time
from pathlib import Path
from typing import Any

from platformdirs import user_state_dir

from acpc.presets import PRESET_NAMES, _BUILTIN_PRESETS, _load_config

_CACHE_DIR = Path(user_state_dir("acpc")) / "models"
_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _cache_path(agent: str) -> Path:
    return _CACHE_DIR / f"{agent}.json"


_FILTERED_MODEL_IDS = frozenset(("default",))


def save_models(agent: str, available_models: list[dict[str, Any]]) -> None:
    """Cache available_models from a new_session() response."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    filtered = [m for m in available_models if m.get("model_id") not in _FILTERED_MODEL_IDS]
    data = {
        "agent": agent,
        "updated": time.time(),
        "available_models": filtered,
    }
    _cache_path(agent).write_text(json.dumps(data), encoding="utf-8")


def load_cached_models(agent: str) -> dict[str, Any] | None:
    """Load cached models for agent. Returns None if missing or unreadable."""
    path = _cache_path(agent)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "available_models" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def is_cache_fresh(agent: str) -> bool:
    """Check if cache exists and is within TTL."""
    data = load_cached_models(agent)
    if data is None:
        return False
    updated = data.get("updated", 0)
    return (time.time() - updated) < _TTL_SECONDS


def get_presets(agent: str) -> dict[str, str]:
    """Get preset mappings for agent (config.toml + builtin fallback)."""
    config = _load_config()
    presets = config.get(agent, {})
    builtin = _BUILTIN_PRESETS.get(agent, {})
    # Merge: config overrides builtin
    merged = {**builtin, **presets}
    # Only return known preset names
    return {k: v for k, v in merged.items() if k in PRESET_NAMES}


def reverse_model_to_agent() -> dict[str, tuple[str, str]]:
    """Build reverse mapping: model_name → (agent, model_or_preset).

    Used by agents.py for hints when model name is used as agent identity.
    Sources (in priority order):
    1. Presets (config.toml + builtins): e.g. "sonnet" → ("claude", "standard")
    2. Cached available_models: e.g. "gpt-5.4/high" → ("codex", "gpt-5.4/high")
    """
    result: dict[str, tuple[str, str]] = {}
    config = _load_config()

    # 1. Cached models (lower priority, added first so presets override)
    for cache_file in _CACHE_DIR.glob("*.json"):
        agent = cache_file.stem
        cached = load_cached_models(agent)
        if cached:
            for m in cached.get("available_models", []):
                model_id = m.get("model_id", "")
                if model_id:
                    lower = model_id.lower()
                    if lower not in result:
                        result[lower] = (agent, model_id)

    # 2. Presets (higher priority, override cached entries)
    for agent in {*_BUILTIN_PRESETS, *config}:
        presets = get_presets(agent)
        for preset_name, model_id in presets.items():
            lower = model_id.lower()
            result[lower] = (agent, preset_name)

    return result
