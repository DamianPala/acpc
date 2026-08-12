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

    assert claude.command_args == ("claude-agent-acp",)
    assert claude.install_command == "npm install -g @agentclientprotocol/claude-agent-acp"
    assert claude.home == "~/.claude"
    assert claude.home_env == "CLAUDE_CONFIG_DIR"
    assert {
        mode: {"grants": spec.grants, "delegates": spec.delegates}
        for mode, spec in claude.modes.items()
    } == {
        "default": {"grants": "read", "delegates": True},
        "plan": {"grants": "edit", "delegates": True},
        "auto": {"grants": "all", "delegates": False},
        "acceptEdits": {"grants": "execute", "delegates": True},
        "dontAsk": {"grants": "none", "delegates": False},
        "bypassPermissions": {"grants": "all", "delegates": False},
    }
    assert {
        mode: {"grants": spec.grants, "delegates": spec.delegates}
        for mode, spec in codex.modes.items()
    } == {
        "read-only": {"grants": "edit", "delegates": False},
        "agent": {"grants": "execute", "delegates": False},
        "agent-full-access": {"grants": "all", "delegates": False},
    }
    assert claude.modes["auto"].escalates is True
    assert codex.modes["read-only"].escalates is True
    assert all(
        not spec.escalates
        for name, spec in (*claude.modes.items(), *codex.modes.items())
        if name not in {"auto", "read-only"}
    )
    assert claude.presets["max"].model == "claude-opus-5"
    assert codex.presets["standard"].effort == "xhigh"
    # claude CLI >=2.1.224 offers no effort option for haiku; see claude.toml.
    assert claude.presets["fast"].model == "claude-haiku-4-5"
    assert claude.presets["fast"].effort is None


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


def test_mode_inherits_through_extends_and_a_child_or_flag_can_override_it(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "base",
        'command = "python -m base"\nmode = "base-mode"\n',
    )
    write_entry(agents, "parent", 'extends = "base"\n')
    write_entry(agents, "child", 'extends = "parent"\nmode = "child-mode"\n')

    registry = AgentRegistry(agents)
    inherited = registry.resolve_call("parent")
    overridden = registry.resolve_call("child")
    flag = registry.resolve_call("child", mode="flag-mode")

    assert inherited.mode == "base-mode"
    assert inherited.provenance["mode"].path == agents / "base.toml"
    assert overridden.mode == "child-mode"
    assert overridden.provenance["mode"].path == agents / "child.toml"
    assert flag.mode == "flag-mode"
    assert flag.provenance["mode"].kind == "call"


def test_mode_is_unset_without_an_entry_or_call_value(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "plain", 'command = "python -m plain"\n')

    resolution = AgentRegistry(agents).resolve_call("plain")

    assert resolution.mode is None
    assert resolution.provenance["mode"].kind == "unset"


def test_variant_merges_modes_by_name_and_mode_field(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "base",
        'command = "python -m base"\n'
        "[modes]\n"
        'default = { grants = "read", delegates = true, escalates = true }\n'
        "[modes.plan]\n"
        'grants = "read"\n'
        "delegates = true\n"
        "escalates = true\n",
    )
    write_entry(
        agents,
        "child",
        'extends = "base"\n[modes]\nplan = { grants = "execute", delegates = true }\n',
    )

    modes = AgentRegistry(agents).resolve("child").modes

    assert modes["default"].grants == "read"
    assert modes["default"].delegates
    assert modes["default"].escalates is True
    assert modes["plan"].grants == "execute"
    assert modes["plan"].delegates
    assert modes["plan"].escalates is True


def test_mode_escalation_defaults_false_and_round_trips_true(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "facts",
        'command = "python"\n'
        "[modes]\n"
        'escalating = { grants = "edit", delegates = false, escalates = true }\n'
        'plain = { grants = "read", delegates = true }\n',
    )

    modes = AgentRegistry(agents).resolve("facts").modes

    assert modes["escalating"].escalates is True
    assert modes["plain"].escalates is False


def test_mode_escalation_rejects_non_boolean_values(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "invalid",
        'command = "python"\n'
        "[modes]\n"
        'default = { grants = "read", delegates = false, escalates = "yes" }\n',
    )

    with pytest.raises(RegistryError, match=r"\[modes\.default\]\.escalates must be a boolean"):
        AgentRegistry(agents)


def test_mode_grants_reject_ask_and_name_the_source_file(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "invalid",
        'command = "python"\n[modes]\ndefault = { grants = "ask", delegates = false }\n',
    )

    with pytest.raises(RegistryError) as error:
        AgentRegistry(agents)

    assert str(agents / "invalid.toml") in str(error.value)
    assert "[modes.default].grants" in str(error.value)


def test_unknown_mode_key_names_the_source_file(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "invalid",
        'command = "python"\n'
        "[modes]\n"
        'default = { grants = "read", delegates = true, typo = false }\n',
    )

    with pytest.raises(RegistryError) as error:
        AgentRegistry(agents)

    assert str(agents / "invalid.toml") in str(error.value)
    assert "[modes.default] unknown key(s) 'typo'" in str(error.value)


def test_permission_alias_is_accepted_and_stored_canonically(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "legacy", 'command = "python"\npermissions = "write"\n')

    registry = AgentRegistry(agents)

    assert registry.resolve("legacy").permissions == "execute"
    assert registry.resolve_call("legacy").permissions == "execute"
    assert registry.resolve_call("legacy", permissions="prompt").permissions == "ask"


def test_description_accepts_long_and_multiline_values_for_resolution(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    long_description = "long " * 100
    multiline_description = "first line\nsecond line\nthird line"
    write_entry(
        agents,
        "long",
        f'command = "python"\ndescription = "{long_description}"\n',
    )
    write_entry(
        agents,
        "multiline",
        'command = "python"\ndescription = """first line\nsecond line\nthird line"""\n',
    )
    write_entry(agents, "empty", 'command = "python"\ndescription = ""\n')

    registry = AgentRegistry(agents)

    assert registry.resolve_call("long").entry.description == long_description
    assert registry.resolve_call("multiline").entry.description == multiline_description
    assert registry.resolve_call("empty").entry.description == ""


def test_description_is_not_inherited_across_two_levels_but_child_keeps_its_own(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "a",
        'command = "python"\ndescription = "The adapter purpose."\n',
    )
    write_entry(agents, "b", 'extends = "a"\n')
    write_entry(agents, "c", 'extends = "b"\n')
    write_entry(
        agents,
        "own",
        'extends = "b"\ndescription = "The child purpose."\n',
    )

    registry = AgentRegistry(agents)

    assert registry.resolve("a").description == "The adapter purpose."
    assert registry.resolve("b").description is None
    assert registry.resolve("c").description is None
    assert registry.resolve("own").description == "The child purpose."


def test_a_variant_inherits_the_parent_adapter_contract_lists(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "base",
        'name = "Base"\ncommand = "python -m base"\n'
        'efforts = ["low", "high"]\n'
        'effort_config_id = "effort"\n'
        'env_passthrough = ["BASE_KEY"]\n',
    )
    write_entry(agents, "worker", 'extends = "base"\nmodel = "base-model"\n')

    registry = AgentRegistry(agents)
    worker = registry.resolve("worker")

    assert worker.efforts == ("low", "high")
    assert worker.effort_config_id == "effort"
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


def test_a_preset_may_pin_the_model_alone(tmp_path: Path) -> None:
    """Effort is a property of the model, not of the preset: a model whose
    vendor exposes no effort setting is pinned by model alone and runs at its
    own built-in level."""
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "local",
        'command = "python -m local"\n[presets]\nfast = { model = "local-fast" }\n',
    )
    registry = AgentRegistry(agents)

    call = registry.resolve_call("local", model="fast")

    assert call.model == "local-fast"
    assert call.effort is None
    assert call.provenance["model"].kind == "entry"
    assert call.provenance["effort"].kind == "unset"


def test_an_effortless_standard_preset_still_supplies_the_default_model(tmp_path: Path) -> None:
    """`standard` doubles as the adapter default, and the model half of that
    fallback must survive the effort half being absent."""
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "local",
        'command = "python -m local"\n[presets]\nstandard = { model = "local-standard" }\n',
    )

    call = AgentRegistry(agents).resolve_call("local")

    assert call.model == "local-standard"
    assert call.effort is None
    assert call.provenance["model"].kind == "adapter-default"
    assert call.provenance["effort"].kind == "unset"


def test_an_explicit_effort_still_applies_over_an_effortless_preset(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "local",
        'command = "python -m local"\n[presets]\nfast = { model = "local-fast" }\n',
    )

    call = AgentRegistry(agents).resolve_call("local", model="fast", effort="xhigh")

    assert call.effort == "xhigh"
    assert call.provenance["effort"].kind == "call"


def test_a_preset_without_a_model_is_still_rejected(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "local",
        'command = "python -m local"\n[presets]\nfast = { effort = "high" }\n',
    )

    with pytest.raises(RegistryError, match=r"\[presets.fast\] requires model"):
        AgentRegistry(agents)


def test_a_preset_effort_that_is_present_is_still_validated(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "local",
        'command = "python -m local"\n[presets]\nfast = { model = "m", effort = "turbo" }\n',
    )

    with pytest.raises(RegistryError, match=r"\[presets.fast\].effort has unsupported level"):
        AgentRegistry(agents)


def test_presetless_adapter_names_the_preset_mechanism(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(agents, "plain", 'command = "python -m plain"\n')

    with pytest.raises(RegistryError, match=r"plain: --model fast requires a \[presets\] table"):
        AgentRegistry(agents).resolve_call("plain", model="fast")


def test_call_resolution_delivers_home_through_the_adapter_home_variable(
    tmp_path: Path,
) -> None:
    call = AgentRegistry(tmp_path / "agents").resolve_call("codex")

    assert call.declared_env == {}
    assert call.adapter_environment["CODEX_HOME"] == str(Path("~/.codex").expanduser())


def test_call_resolution_exports_ask_as_a_read_ceiling(tmp_path: Path) -> None:
    call = AgentRegistry(tmp_path / "agents").resolve_call("codex", permissions="ask")

    environment = call.adapter_environment

    assert environment["ACPC_CEILING"] == "read"


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
    agents = tmp_path / "agents"
    write_entry(agents, "plain", 'command = "python -m plain"\n')

    call = AgentRegistry(agents).resolve_call("plain")

    assert call.model is None
    assert call.effort is None
    assert call.provenance["model"].kind == "unset"


def test_shipped_grok_uses_set_model_and_cli_effort(tmp_path: Path) -> None:
    registry = AgentRegistry(tmp_path / "agents")
    grok = registry.resolve("grok")
    assert grok.command_args == ("grok", "agent", "--always-approve", "stdio")
    assert grok.model_via == "set_model"
    assert grok.effort_via == "cli"
    assert grok.effort_cli_flag == "--reasoning-effort"
    assert "default" in grok.modes
    assert grok.presets["standard"].model == "grok-4.6"
    call = grok.resolve_call(model="fast", permissions="read")
    assert call.model == "grok-4.5"
    assert call.effort == "low"
    assert call.command == (
        "grok",
        "agent",
        "--always-approve",
        "--reasoning-effort",
        "low",
        "stdio",
    )


def test_effort_via_cli_injects_flag_before_transport(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    write_entry(
        agents,
        "tool",
        """
name = "Tool"
command = "tool agent stdio"
efforts = ["low", "high"]
effort_via = "cli"
effort_cli_flag = "--effort"
model_via = "set_model"
[modes]
default = { grants = "read", delegates = true }
""",
    )
    entry = AgentRegistry(agents).resolve("tool")
    call = entry.resolve_call(effort="high", permissions="read")
    assert call.command == ("tool", "agent", "--effort", "high", "stdio")
