"""Behavioral tests for the strict global configuration loader."""

from pathlib import Path

import pytest

from acpc.config import Config, ConfigError, load_config, parse_duration


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))


def test_missing_config_uses_the_spec_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path))

    assert load_config() == Config(retention="90d", daemon_ttl="30m", daemon_max_concurrent=8)


def test_config_loads_the_complete_three_key_schema(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'retention = "7d"\ndaemon_ttl = "45m"\ndaemon_max_concurrent = 3\n',
        encoding="utf-8",
    )

    assert load_config(path) == Config(retention="7d", daemon_ttl="45m", daemon_max_concurrent=3)


def test_config_exposes_duration_values_as_seconds(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('retention = "1h30m"\ndaemon_ttl = "2m"\n', encoding="utf-8")

    config = load_config(path)

    assert config.retention_seconds == 5400.0
    assert config.daemon_ttl_seconds == 120.0


def test_parse_duration_accepts_compound_values() -> None:
    assert parse_duration("1h30m") == 5400.0


@pytest.mark.parametrize("text", ["0", "0s", "0s0m", "garbage", "1x"])
def test_parse_duration_rejects_zero_and_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_unknown_config_key_is_a_hard_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('retention = "7d"\nfuture_setting = true\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="future_setting") as error:
        load_config(path)
    assert str(error.value).startswith(str(path))


def test_malformed_config_is_a_clean_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('retention = "7d"\n[', encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TOML") as error:
        load_config(path)
    assert str(path) in str(error.value)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("retention", '"not-a-duration"', "retention"),
        ("daemon_ttl", "0", "daemon_ttl"),
        ("daemon_max_concurrent", '"eight"', "positive integer"),
    ],
)
def test_invalid_config_value_names_key_and_expected_type(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"{key} = {value}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)
