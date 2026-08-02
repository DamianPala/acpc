"""Model preset resolution for acpc.

Resolves preset names (fast, standard, max) to vendor-specific model IDs
by reading acpc's config file. Falls back to built-in defaults.
"""

import os
import sys
from pathlib import Path

import platformdirs

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]


def _config_path() -> Path:
    """Return acpc's config file, alongside the agent definitions.

    Read per call rather than captured at import: a module-level constant
    freezes the environment at interpreter startup, so a test harness could
    only redirect the config by also moving HOME.
    """
    configured_dir = os.environ.get("ACPC_CONFIG_DIR")
    base = Path(configured_dir) if configured_dir else Path(platformdirs.user_config_dir("acpc"))
    return base / "config.toml"


# Built-in defaults if the config file doesn't exist or lacks entries
_BUILTIN_PRESETS: dict[str, dict[str, str]] = {
    "claude": {
        "fast": "haiku",
        "standard": "sonnet",
        "max": "opus",
    },
    "codex": {
        "fast": "gpt-5.6-luna/high",
        "standard": "gpt-5.6-terra/xhigh",
        "max": "gpt-5.6-sol/xhigh",
    },
}

PRESET_NAMES = frozenset(("fast", "standard", "max"))


def _load_config() -> dict[str, dict[str, str]]:
    """Load model presets from acpc's config file."""
    config_path = _config_path()
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
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
    from acpc's config file (with built-in fallback).
    Otherwise return the model string as-is (raw model ID passthrough).
    """
    if model not in PRESET_NAMES:
        return model
    return get_presets(agent).get(model, model)
