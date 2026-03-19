"""Model preset resolution for acpc.

Resolves preset names (fast, standard, max) to vendor-specific model IDs
by reading ~/.agents/config.toml. Falls back to built-in defaults.

The config file is global and shared across tools (not acpc-specific).
"""

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

_GLOBAL_CONFIG = Path.home() / ".agents" / "config.toml"

# Built-in defaults if ~/.agents/config.toml doesn't exist or lacks entries
_BUILTIN_PRESETS: dict[str, dict[str, str]] = {
    "claude": {
        "fast": "haiku",
        "standard": "sonnet",
        "max": "opus",
    },
    "codex": {
        "fast": "gpt-5.1-codex-mini/medium",
        "standard": "gpt-5.1-codex-max",
        "max": "gpt-5.4/high",
    },
}

PRESET_NAMES = frozenset(("fast", "standard", "max"))


def _load_config() -> dict[str, dict[str, str]]:
    """Load model presets from ~/.agents/config.toml."""
    if not _GLOBAL_CONFIG.exists():
        return {}
    try:
        with open(_GLOBAL_CONFIG, "rb") as f:
            data = tomllib.load(f)
        models = data.get("models", {})
        return {agent: dict(tiers) for agent, tiers in models.items() if isinstance(tiers, dict)}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def get_presets(agent: str, config: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    """Get preset mappings for agent (config.toml + builtin fallback).

    Pass config to avoid repeated file reads in loops.
    """
    if config is None:
        config = _load_config()
    builtin = _BUILTIN_PRESETS.get(agent, {})
    merged = {**builtin, **config.get(agent, {})}
    return {k: v for k, v in merged.items() if k in PRESET_NAMES}


def resolve_model(agent: str, model: str) -> str:
    """Resolve a model string, checking presets first.

    If model matches a preset name (fast/standard/max), resolve it
    from ~/.agents/config.toml (with built-in fallback).
    Otherwise return the model string as-is (raw model ID passthrough).
    """
    if model not in PRESET_NAMES:
        return model
    return get_presets(agent).get(model, model)
