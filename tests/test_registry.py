"""Behavioral tests for shipped adapters and user entry resolution."""

from pathlib import Path

import pytest

from acpc.registry import AgentRegistry, RegistryError


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "state"))


def write_entry(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(text, encoding="utf-8")


def test_shipped_adapter_facts_and_presets_are_available(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents")

    claude = registry.resolve("claude")
    codex = registry.resolve("codex")
    gemini = registry.resolve("gemini")

    assert claude.command_args == ("claude-agent-acp",)
    assert claude.install_command == "npm install -g @agentclientprotocol/claude-agent-acp"
    assert claude.home == "~/.claude"
    assert claude.home_env == "CLAUDE_CONFIG_DIR"
    assert "bypassPermissions" in claude.bypass_modes
    assert claude.presets["max"].model == "claude-opus-5"
    assert codex.presets["standard"].effort == "xhigh"
    assert gemini.command_args == ("gemini", "--acp")
    assert gemini.home == "~/.gemini"
    assert gemini.bypass_modes == ("yolo",)
    assert gemini.presets == {}


def test_variant_inherits_and_reports_nearest_field_provenance(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "base",
        'name = "Base"\ncommand = "python -m base"\nhome = "~/.base"\nefforts = ["low", "high"]\n',
    )
    write_entry(
        agents,
        "parent",
        'extends = "base"\nmodel = "base-model"\neffort = "low"\n',
    )
    write_entry(
        agents,
        "child",
        'extends = "parent"\neffort = "high"\n',
    )

    child = AgentRegistry(agents).resolve("child")

    assert child.command_args == ("python", "-m", "base")
    assert child.model == "base-model"
    assert child.effort == "high"
    assert child.base_adapter == "base"
    assert child.provenance["command"].kind == "entry"
    assert child.provenance["command"].path is not None
    assert child.provenance["command"].path.name == "base.toml"
    assert child.provenance["model"].kind == "entry"
    assert child.provenance["model"].path is not None
    assert child.provenance["model"].path.name == "parent.toml"
    assert child.provenance["effort"].kind == "entry"
    assert child.provenance["effort"].path is not None
    assert child.provenance["effort"].path.name == "child.toml"


def test_a_variant_inherits_the_parent_adapter_contract_lists(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "base",
        'name = "Base"\ncommand = "python -m base"\n'
        'efforts = ["low", "high"]\nbypass_modes = ["yolo"]\n'
        'env_passthrough = ["BASE_KEY"]\n',
    )
    write_entry(agents, "worker", 'extends = "base"\nmodel = "base-model"\n')

    registry = AgentRegistry(agents)
    worker = registry.resolve("worker")

    assert worker.efforts == ("low", "high")
    assert worker.bypass_modes == ("yolo",)
    assert worker.env_passthrough == ("BASE_KEY",)
    with pytest.raises(RegistryError, match="low, high"):
        registry.resolve_call("worker", effort="medium")


def test_user_override_new_adapter_and_variant_are_distinct_cases(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "codex", 'home = "~/.codex-local"\n')
    write_entry(agents, "new", 'command = "python -m new"\n')
    write_entry(agents, "variant", 'extends = "codex"\nmodel = "local-model"\n')

    registry = AgentRegistry(agents)
    override = registry.resolve("codex")
    new = registry.resolve("new")
    variant = registry.resolve("variant")

    assert override.home == "~/.codex-local"
    assert override.command == "codex-acp"
    assert override.provenance["home"].kind == "entry"
    assert override.provenance["home"].path is not None
    assert override.provenance["home"].path.name == "codex.toml"
    assert new.base_adapter == "new"
    assert new.command_args == ("python", "-m", "new")
    assert variant.base_adapter == "codex"
    assert variant.model == "local-model"
    assert variant.command == "codex-acp"


def test_inheritance_reports_missing_base_and_cycles(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "missing", 'extends = "does-not-exist"\n')
    write_entry(agents, "a", 'extends = "b"\n')
    write_entry(agents, "b", 'extends = "a"\n')

    registry = AgentRegistry(agents)
    with pytest.raises(RegistryError, match="missing base 'does-not-exist'"):
        registry.resolve("missing")
    with pytest.raises(RegistryError, match="a -> b -> a"):
        registry.resolve("a")


def test_preset_resolution_and_explicit_effort_override_keep_sources(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "codex",
        '[presets]\nfast = { model = "local-fast", effort = "high" }\n',
    )
    registry = AgentRegistry(agents)

    call = registry.resolve_call("codex", model="fast", effort="xhigh")

    assert call.model == "local-fast"
    assert call.effort == "xhigh"
    assert call.provenance["model"].kind == "entry"
    assert call.provenance["model"].path is not None
    assert call.provenance["model"].path.name == "codex.toml"
    assert call.provenance["effort"].kind == "call"
    assert call.provenance["effort"].path is None


def test_presetless_adapter_names_the_preset_mechanism(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match=r"gemini: --model fast requires a \[presets\] table"):
        AgentRegistry(tmp_path / "agents").resolve_call("gemini", model="fast")


def test_call_resolution_delivers_home_through_the_adapter_home_variable(
    tmp_path: Path,
) -> None:
    call = AgentRegistry(tmp_path / "agents").resolve_call("codex")

    assert call.declared_env == {}
    assert call.adapter_environment["CODEX_HOME"] == str(Path("~/.codex").expanduser())


def test_effort_validation_lists_supported_levels(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "limited", 'command = "python"\nefforts = ["low", "medium"]\n')

    with pytest.raises(RegistryError, match=r"supported levels: low, medium"):
        AgentRegistry(agents).resolve_call("limited", effort="xhigh")

    entry = AgentRegistry(agents).resolve("limited")
    assert entry.provenance["permissions"].kind == "unset"
    assert entry.preset_models == ()


def test_install_status_checks_command_head_and_install_helper_is_injectable(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "local",
        'command = "python -m local"\ninstall_command = "python -m installer"\n',
    )
    calls: list[tuple[object, ...]] = []

    def fake_runner(*args: object, **kwargs: object) -> object:
        calls.append(args)
        assert kwargs["cwd"] is None
        return "result"

    registry = AgentRegistry(agents)
    entry = registry.resolve("local")
    result = registry.execute_install("local", runner=fake_runner)

    assert entry.installed
    assert registry.install_command("local") == "python -m installer"
    assert result == "result"
    assert calls == [(["python", "-m", "installer"],)]


def test_malformed_entry_is_a_clean_registry_error(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "broken", "this is not valid toml [[[\n")

    with pytest.raises(RegistryError, match="invalid TOML") as error:
        AgentRegistry(agents)
    assert "Traceback" not in str(error.value)


def test_the_standard_preset_is_the_adapter_default_when_no_model_is_given(
    tmp_path: Path,
) -> None:
    # SPEC's `agents claude` view prints `model claude-sonnet-5 (adapter default)`
    # and claude's standard preset is claude-sonnet-5/high: the standard preset
    # *is* what "adapter default" names.
    call = AgentRegistry(tmp_path / "agents").resolve_call("claude")

    assert call.model == "claude-sonnet-5"
    assert call.effort == "high"
    assert call.provenance["model"].kind == "adapter-default"
    assert call.provenance["effort"].kind == "adapter-default"
    assert call.provenance["model"].path is not None


def test_an_entry_model_beats_the_adapter_default(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "variant", 'extends = "claude"\nmodel = "claude-opus-5"\n')

    call = AgentRegistry(agents).resolve_call("variant")

    assert call.model == "claude-opus-5"
    assert call.provenance["model"].kind == "entry"
    # effort was not set on the variant, so it still falls back independently
    assert call.effort == "high"
    assert call.provenance["effort"].kind == "adapter-default"


def test_an_explicit_effort_overrides_the_adapter_default(tmp_path: Path) -> None:
    call = AgentRegistry(tmp_path / "agents").resolve_call("claude", effort="low")

    assert call.effort == "low"
    assert call.provenance["effort"].kind == "call"
    assert call.model == "claude-sonnet-5"


def test_a_presetless_adapter_has_no_default_model(tmp_path: Path) -> None:
    call = AgentRegistry(tmp_path / "agents").resolve_call("gemini")

    assert call.model is None
    assert call.effort is None
    assert call.provenance["model"].kind == "unset"
