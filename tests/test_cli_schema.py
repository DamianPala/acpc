"""Behavioral tests for `acpc schema`: the published command surface.

The point of the command is that a caller can trust it, so most of these
tests compare the published document against the parser that actually runs —
every flag, including the ones `--help` hides — rather than against a list
written here.
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from acpc import effects, schema, vocab
from acpc.cli import main

D7_FIELDS = {
    "schema_version",
    "tool_version",
    "global_flags",
    "format_defaults",
    "exit_codes",
    "conformance",
    "commands",
}

D8_ALWAYS = {"name", "description", "args", "flags", "effects", "confirm", "interactive"}

DESCRIPTOR_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}

# What each Click type name has to be published as. Anything absent here is a
# string whose accepted syntax belongs in the description.
CLICK_TYPES = {
    "integer": "integer",
    "integer range": "integer",
    "float": "number",
    "float range": "number",
    "boolean": "boolean",
}


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True)
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str):
    return runner.invoke(main, list(args), catch_exceptions=False)


def read_index(runner: CliRunner) -> dict[str, Any]:
    result = invoke(runner, "schema")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def read_detail(runner: CliRunner, name: str) -> dict[str, Any]:
    result = invoke(runner, "schema", *name.split(" "))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def click_command(name: str) -> click.Command:
    """The command the parser dispatches for a published path."""
    command: click.Command = main
    for segment in name.split(" "):
        assert isinstance(command, click.Group)
        found = command.commands[segment]
        command = found
    return command


def descriptors(detail: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from detail["args"]
    yield from detail["flags"]


def test_index_carries_every_field(runner: CliRunner) -> None:
    index = read_index(runner)
    assert set(index) == D7_FIELDS
    assert index["schema_version"] == "1"
    assert index["schema_version"].isdecimal() and int(index["schema_version"]) > 0
    assert index["conformance"] == {
        "name": "cli-design-standard",
        "standard": "0.1.0-draft.7",
        "extensions": ["managed"],
    }
    assert index["exit_codes"] == vocab.EXIT_DESCRIPTIONS
    assert set(index["format_defaults"]) == {"tty", "non_tty"}


def test_index_commands_are_sorted_by_code_point(runner: CliRunner) -> None:
    names = [entry["name"] for entry in read_index(runner)["commands"]]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_index_entries_carry_nothing_but_routing(runner: CliRunner) -> None:
    for entry in read_index(runner)["commands"]:
        assert set(entry) == {"name", "description", "effects"}
        assert entry["effects"] in effects.VALUES
        assert entry["description"].strip()
        assert "\n" not in entry["description"]


def test_index_descriptions_distinguish_neighbours(runner: CliRunner) -> None:
    descriptions = [entry["description"] for entry in read_index(runner)["commands"]]
    assert len(descriptions) == len(set(descriptions))


def test_tool_version_is_what_version_prints(runner: CliRunner) -> None:
    printed = invoke(runner, "--version").stdout.strip()
    assert read_index(runner)["tool_version"] == printed
    # D4d: the version string alone, with no tool name in front of it.
    assert not printed.startswith("acpc")


def test_schema_is_not_a_command_entry(runner: CliRunner) -> None:
    names = {entry["name"] for entry in read_index(runner)["commands"]}
    assert "schema" not in names
    # Nor is a group that dispatches nothing of its own.
    assert "daemon" not in names
    assert {
        "agents check",
        "agents create",
        "agents delete",
        "agents get",
        "agents list",
        "daemon status",
        "daemon stop",
        "skills get",
        "skills list",
    } <= names
    assert "agents" not in names
    assert "skills" not in names


def test_every_indexed_command_has_detail(runner: CliRunner) -> None:
    for entry in read_index(runner)["commands"]:
        detail = read_detail(runner, entry["name"])
        assert D8_ALWAYS <= set(detail)
        assert detail["name"] == entry["name"]
        assert detail["effects"] == entry["effects"]
        assert isinstance(detail["confirm"], bool)
        # Every acpc command is non-interactive: the prompts it may raise ask
        # for one missing input, they do not start a session.
        assert detail["interactive"] is False
        assert isinstance(detail["output"], dict)


def test_only_log_declares_a_record_stream(runner: CliRunner) -> None:
    streaming = {
        entry["name"]
        for entry in read_index(runner)["commands"]
        if read_detail(runner, entry["name"]).get("stream")
    }
    assert streaming == {"log"}


def expected_flag(parameter: click.Option) -> dict[str, Any]:
    """What a descriptor for this option has to say, read off the parser itself."""
    spellings = [*parameter.opts, *parameter.secondary_opts]
    longs = [item for item in spellings if item.startswith("--")]
    canonical = longs[0] if longs else spellings[0]
    expected: dict[str, Any] = {
        "name": canonical.lstrip("-"),
        "type": "boolean" if parameter.is_flag else CLICK_TYPES.get(parameter.type.name, "string"),
        "required": parameter.required,
    }
    aliases = [item.lstrip("-") for item in spellings if item != canonical]
    if aliases:
        expected["aliases"] = aliases
    if isinstance(parameter.type, click.Choice):
        expected["enum"] = [str(choice) for choice in parameter.type.choices]
    if parameter.multiple:
        expected["repeatable"] = True
    return expected


def accepted_options(name: str) -> list[click.Option]:
    """Every flag the parser takes for a path, including the hidden ones."""
    return [
        parameter
        for parameter in click_command(name).params
        if isinstance(parameter, click.Option) and parameter.name != "help"
    ]


def test_flag_descriptors_match_the_parser(runner: CliRunner) -> None:
    """Every flag the parser takes, published with the contract the parser has.

    A count and a name are not enough: a flag published with the wrong type,
    the wrong `required`, a stale `enum` or a missing alias is exactly the
    drift a caller cannot see and the generator exists to prevent.
    """
    global_names = {flag["name"] for flag in read_index(runner)["global_flags"]}
    for entry in read_index(runner)["commands"]:
        accepted = accepted_options(entry["name"])
        published = {flag["name"]: flag for flag in read_detail(runner, entry["name"])["flags"]}
        assert len(published) == len(accepted) - len(global_names), entry["name"]
        for parameter in accepted:
            expected = expected_flag(parameter)
            where = (entry["name"], expected["name"])
            if expected["name"] in global_names:
                # D7a: a global flag is published once, in the index.
                assert expected["name"] not in published, where
                continue
            descriptor = published.get(expected["name"])
            assert descriptor is not None, where
            for field, value in expected.items():
                assert descriptor.get(field) == value, (where, field)


def test_global_flags_are_exactly_the_flags_every_command_accepts(runner: CliRunner) -> None:
    """Global flags have one descriptor; command-specific choices stay local."""
    index = read_index(runner)
    per_command = {
        entry["name"]: {
            expected_flag(parameter)["name"]: expected_flag(parameter)
            for parameter in accepted_options(entry["name"])
        }
        for entry in index["commands"]
    }
    common = set.intersection(*(set(flags) for flags in per_command.values()))
    uniform = {
        name
        for name in common
        if len(
            {
                tuple((key, repr(value)) for key, value in sorted(flags[name].items()))
                for flags in per_command.values()
            }
        )
        == 1
    }
    assert {flag["name"] for flag in index["global_flags"]} == uniform
    for flag in index["global_flags"]:
        assert set(flag) >= {"name", "description", "type", "required"}
        assert flag["description"].strip()


def test_continue_has_no_hidden_flags(runner: CliRunner) -> None:
    hidden = {
        parameter.opts[0].lstrip("-")
        for parameter in click_command("continue").params
        if isinstance(parameter, click.Option) and parameter.hidden
    }
    assert hidden == set()


def test_argument_descriptors_cover_every_accepted_argument(runner: CliRunner) -> None:
    for entry in read_index(runner)["commands"]:
        command = click_command(entry["name"])
        if isinstance(command, click.Group):
            # A group's positional argument is parsed elsewhere; that its
            # descriptor is honoured is what the next test settles.
            continue
        accepted = [
            parameter.name for parameter in command.params if isinstance(parameter, click.Argument)
        ]
        detail = read_detail(runner, entry["name"])
        assert [arg["name"] for arg in detail["args"]] == accepted, entry["name"]


@pytest.mark.parametrize("group", ["agents", "skills"])
def test_every_listed_name_reaches_the_get_command(runner: CliRunner, group: str) -> None:
    listed = json.loads(invoke(runner, group, "list", "--json").stdout)["items"]
    names = [item["name"] for item in listed]
    assert names
    for name in names:
        result = invoke(runner, group, "get", name, "--help")
        assert result.exit_code == 0, (name, result.output)
        assert "Show" in result.stdout or "Print" in result.stdout, (name, result.stdout)


def test_group_prefixes_have_no_command_detail(runner: CliRunner) -> None:
    for name in ("agents", "skills"):
        result = invoke(runner, "schema", name)
        assert result.exit_code == vocab.EXIT_USAGE
        assert name in result.stderr


def test_help_only_group_is_not_a_command_entry(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    @main.group(name="help-only", invoke_without_command=True)
    @click.pass_context
    def help_only(ctx: click.Context) -> None:
        click.echo(ctx.get_help())

    main.commands.pop("help-only")
    monkeypatch.setitem(main.commands, "help-only", help_only)
    names = {entry["name"] for entry in read_index(runner)["commands"]}

    assert "help-only" not in names


def test_public_schema_includes_a_group_that_dispatches_without_a_subcommand(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    @effects.read_only
    @schema.dispatches_without_command
    @main.group(name="useful", invoke_without_command=True)
    @click.pass_context
    def useful(ctx: click.Context) -> None:
        """Run useful work when no subcommand is given."""
        if ctx.invoked_subcommand is None:
            click.echo("useful result")

    main.commands.pop("useful")
    monkeypatch.setitem(main.commands, "useful", useful)
    result = invoke(runner, "schema")

    assert result.exit_code == 0, result.output
    names = {entry["name"] for entry in json.loads(result.stdout)["commands"]}
    assert "useful" in names
    assert invoke(runner, "useful").stdout == "useful result\n"


def test_background_alias_and_timeout_contract_are_published(runner: CliRunner) -> None:
    flags = {flag["name"]: flag for flag in read_detail(runner, "run")["flags"]}

    assert flags["background"]["aliases"] == ["bg"]
    assert "the session keeps running" in flags["timeout"]["description"]
    assert "changes the work itself" in flags["cancel-after"]["description"]
    assert "block by default" in read_detail(runner, "run")["description"]
    assert "--background" in read_detail(runner, "run")["description"]


def test_resolve_publishes_the_shared_resolution_flags(runner: CliRunner) -> None:
    detail = read_detail(runner, "resolve")
    assert detail["effects"] == "read_only"
    assert [argument["name"] for argument in detail["args"]] == ["agent"]
    flags = {flag["name"] for flag in detail["flags"]}
    assert {"cwd", "model", "effort", "mode", "permissions", "home"} <= flags


def test_agents_check_publishes_its_scope_and_conflicts(runner: CliRunner) -> None:
    detail = read_detail(runner, "agents check")
    argument = detail["args"][0]["description"]
    flags = {flag["name"]: flag for flag in detail["flags"]}

    assert detail["effects"] == "read_only"
    assert "every registered adapter and variant" in argument
    assert "adapter is unavailable" in argument
    assert "only valid without NAME" in flags["limit"]["description"]
    assert "only valid without NAME" in flags["plain"]["description"]


def test_descriptors_are_well_formed(runner: CliRunner) -> None:
    for entry in read_index(runner)["commands"]:
        for descriptor in descriptors(read_detail(runner, entry["name"])):
            where = (entry["name"], descriptor["name"])
            assert descriptor["description"].strip(), where
            assert "\n" not in descriptor["description"], where
            assert descriptor["type"] in DESCRIPTOR_TYPES, where
            assert isinstance(descriptor["required"], bool), where
            assert not descriptor["name"].startswith("-"), where
            for alias in descriptor.get("aliases", []):
                assert not alias.startswith("-"), where


def test_no_descriptor_publishes_a_sentinel_default(runner: CliRunner) -> None:
    for entry in read_index(runner)["commands"]:
        for descriptor in descriptors(read_detail(runner, entry["name"])):
            if "default" not in descriptor:
                continue
            where = (entry["name"], descriptor["name"])
            value = descriptor["default"]
            assert value is not None, where
            assert not descriptor["required"], where
            # `bool` and not `int`: a boolean is an int in Python, but a
            # `type: "string"` descriptor carrying `default: 5` is a lie.
            assert isinstance(value, DESCRIPTOR_TYPES[descriptor["type"]] | bool), where


def test_run_time_resolved_values_are_not_published_as_defaults(runner: CliRunner) -> None:
    """A value that comes from the registry or the environment is not a default."""
    flags = {flag["name"]: flag for flag in read_detail(runner, "run")["flags"]}
    for name in ("permissions", "model", "effort", "cwd", "home", "timeout", "cancel-after"):
        assert "default" not in flags[name], name
        assert flags[name]["description"].strip()
    # A real built-in default, on the other hand, is published.
    assert flags["max-output"]["default"] == 131072


def test_permissions_enum_lists_the_deprecated_aliases(runner: CliRunner) -> None:
    flags = {flag["name"]: flag for flag in read_detail(runner, "run")["flags"]}
    permissions = flags["permissions"]
    assert permissions["enum"] == [*vocab.PERMISSION_VALUES, *vocab.PERMISSION_ALIASES]
    assert set(vocab.PERMISSION_ALIASES) <= set(permissions["enum"])
    assert "deprecated" in permissions["description"]


def test_prompt_argument_accepts_stdin(runner: CliRunner) -> None:
    for name, argument in (("run", "prompt_text"), ("continue", "prompt_text"), ("steer", None)):
        args = {arg["name"]: arg for arg in read_detail(runner, name)["args"]}
        target = argument or "instruction_text"
        assert args[target]["accepts_stdin"] is True
        assert vocab.MAX_PROMPT_LABEL in args[target]["description"]


def test_confirm_marks_every_command_that_accepts_yes(runner: CliRunner) -> None:
    gated = set()
    for entry in read_index(runner)["commands"]:
        detail = read_detail(runner, entry["name"])
        accepts_yes = any(
            isinstance(parameter, click.Option) and "--yes" in parameter.opts
            for parameter in click_command(entry["name"]).params
        )
        assert detail["confirm"] == accepts_yes, entry["name"]
        if accepts_yes:
            gated.add(entry["name"])
    assert gated == {"agents delete", "delete", "prune", "install", "daemon stop"}


def test_effects_match_the_declaration_on_the_command(runner: CliRunner) -> None:
    for entry in read_index(runner)["commands"]:
        assert entry["effects"] == effects.of(click_command(entry["name"]))


def test_unknown_path_names_the_nearest_valid_paths(runner: CliRunner) -> None:
    result = invoke(runner, "schema", "agents", "nope")
    assert result.exit_code == vocab.EXIT_USAGE
    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "invalid_input"
    message = envelope["error"]["message"]
    assert "agents create" in message and "agents delete" in message
    assert "wait" not in message


def test_a_group_prefix_names_the_paths_under_it(runner: CliRunner) -> None:
    """`schema daemon` is the natural next move after reading `daemon status`."""
    result = invoke(runner, "schema", "daemon")
    assert result.exit_code == vocab.EXIT_USAGE
    message = json.loads(result.stderr.strip().splitlines()[-1])["error"]["message"]
    assert "daemon status" in message and "daemon stop" in message
    assert "run" not in message


def test_unknown_first_word_names_every_path(runner: CliRunner) -> None:
    result = invoke(runner, "schema", "nope")
    assert result.exit_code == vocab.EXIT_USAGE
    message = json.loads(result.stderr.strip().splitlines()[-1])["error"]["message"]
    for name in ("run", "agents create", "daemon status"):
        assert name in message


def test_a_quoted_path_is_not_a_path(runner: CliRunner) -> None:
    """Segments are separate arguments; one quoted word is not two segments."""
    result = invoke(runner, "schema", "agents create")
    assert result.exit_code == vocab.EXIT_USAGE
    message = json.loads(result.stderr.strip().splitlines()[-1])["error"]["message"]
    assert "acpc schema agents create" in message
    # The shell ate the quotes, so the message has to name the difference
    # rather than repeat the string the caller can already see.
    assert "one argument" in message and "pass 2 arguments" in message


def test_root_help_names_the_introspection_command(runner: CliRunner) -> None:
    help_text = invoke(runner, "--help").stdout
    assert "acpc schema" in help_text
    assert "--json" in help_text


def run_out_of_process(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    """Run acpc in a fresh process, so nothing this test set up leaks into it."""
    script = "import sys; from acpc.cli import main; sys.argv = ['acpc', *sys.argv[1:]]; main()"
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "", "ACPC_HOME": str(home), "PYTHONPATH": str(_source_root())},
        cwd=str(home.parent),
        timeout=60,
    )


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src"


def test_schema_needs_no_configuration_and_writes_only_json(tmp_path: Path) -> None:
    """D6c: no configuration, no state, no network, nothing on stdout but JSON."""
    absent = tmp_path / "not-there"
    assert not absent.exists()

    result = run_out_of_process("schema", home=absent)

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert set(document) == D7_FIELDS
    assert result.stderr == ""
    # It read nothing and left nothing: no state root came into being.
    assert not absent.exists()


def test_schema_detail_needs_no_configuration(tmp_path: Path) -> None:
    absent = tmp_path / "not-there"
    result = run_out_of_process("schema", "run", home=absent)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "run"
    assert not absent.exists()
