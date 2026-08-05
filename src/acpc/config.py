"""Strict loading of acpc's small global configuration file.

The configuration deliberately contains only housekeeping settings.  Values
that affect an adapter call belong in command-line flags or an agent entry;
keeping that boundary here makes a config file safe to inspect and migrate.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from acpc.paths import config_file

_DURATION_RE = re.compile(r"(?:\d+(?:s|m|h|d|w))+\Z")
_CONFIG_KEYS = frozenset({"retention", "daemon_ttl", "daemon_max_concurrent"})
_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


class ConfigError(ValueError):
    """A config file is missing, malformed, or contains an invalid value."""


@dataclass(frozen=True, slots=True)
class Config:
    """The complete public configuration surface of acpc."""

    retention: str = "90d"
    daemon_ttl: str = "30m"
    daemon_max_concurrent: int = 8

    @property
    def retention_seconds(self) -> float:
        return parse_duration(self.retention)

    @property
    def daemon_ttl_seconds(self) -> float:
        return parse_duration(self.daemon_ttl)


DEFAULT_CONFIG = Config()


def parse_duration(text: str) -> float:
    """Parse a compound duration such as ``1h30m`` into seconds."""
    if not isinstance(text, str) or not _DURATION_RE.fullmatch(text):
        raise ValueError(f"invalid duration: {text!r}")
    seconds = sum(
        float(match.group(1)) * _DURATION_UNITS[match.group(2)]
        for match in re.finditer(r"(\d+)([smhdw])", text)
    )
    if seconds <= 0:
        raise ValueError(f"duration must be greater than zero: {text!r}")
    return seconds


def _validate_duration(path: Path, key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            f"{path}: key '{key}' must be a positive duration such as '90d'; "
            "edit the config file and try again"
        )
    try:
        parse_duration(value)
    except ValueError as error:
        raise ConfigError(
            f"{path}: key '{key}' is {value!r}: {error}; "
            "use a positive duration such as '90d' or '1h30m'"
        ) from None
    return value


def _validate(path: Path, values: dict[str, object]) -> Config:
    unknown = sorted(set(values) - _CONFIG_KEYS)
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        raise ConfigError(
            f"{path}: unknown key(s) {names} in the root section; "
            "remove them (the supported keys are retention, daemon_ttl, "
            "daemon_max_concurrent)"
        )

    retention = _validate_duration(
        path, "retention", values.get("retention", DEFAULT_CONFIG.retention)
    )
    daemon_ttl = _validate_duration(
        path, "daemon_ttl", values.get("daemon_ttl", DEFAULT_CONFIG.daemon_ttl)
    )
    max_concurrent = values.get("daemon_max_concurrent", DEFAULT_CONFIG.daemon_max_concurrent)
    if (
        type(max_concurrent) is not int or max_concurrent < 1
    ):  # bool is not a valid TOML integer here.
        raise ConfigError(
            f"{path}: key 'daemon_max_concurrent' must be a positive integer; "
            "edit the config file and try again"
        )
    return Config(
        retention=retention,
        daemon_ttl=daemon_ttl,
        daemon_max_concurrent=max_concurrent,
    )


def load_config(path: str | Path | None = None) -> Config:
    """Load ``config.toml`` strictly, returning defaults when it is absent."""
    config_path = Path(path) if path is not None else config_file()
    if not config_path.exists():
        return DEFAULT_CONFIG
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{config_path}: cannot read config.toml: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{config_path}: invalid TOML in the root section: {exc}; fix the file and try again"
        ) from None
    if not isinstance(
        raw, dict
    ):  # tomllib currently always returns a dict, but keep the contract explicit.
        raise ConfigError(f"{config_path}: the root section must be a TOML table")
    return _validate(config_path, raw)
