"""Executable evidence for the CLI Design Standard claim.

The command set and its input descriptors come from ``acpc schema`` and the
Click tree.  The few scenario helpers below create state needed to exercise a
published command; they are never used to decide which commands exist.
"""

from __future__ import annotations

import json
import os
import pty
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

import acpc.cli as cli_module
from acpc import daemon_client, errors, proc, sessions, transcript, vocab
from acpc.cli import main

MOCK_AGENT_SCRIPT = str(Path(__file__).with_name("mock_agent.py"))
MOCK_ENTRY = f'''
name = "Mock Agent"
command = "{sys.executable} {MOCK_AGENT_SCRIPT}"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"

[modes]
default = {{ grants = "read", delegates = true }}
'''

STANDARD_PATH = Path(
    "/home/haz/ai/lab/projects/skills-starter/haz-skills/cli-design/references/"
    "cli-design-standard.md"
)
SCHEMA_KEYS = {"type", "enum", "properties", "required", "items"}


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def invoke(cli: CliRunner, *args: str, input_text: str | None = None):
    return cli.invoke(main, list(args), input=input_text, catch_exceptions=False)


def read_index(cli: CliRunner) -> dict[str, Any]:
    result = invoke(cli, "schema")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    return json.loads(result.stdout)


def read_detail(cli: CliRunner, name: str) -> dict[str, Any]:
    result = invoke(cli, "schema", *name.split())
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    return json.loads(result.stdout)


def click_command(name: str) -> click.Command:
    command: click.Command = main
    for segment in name.split():
        assert isinstance(command, click.Group)
        command = command.commands[segment]
    return command


def accepted_options(name: str) -> list[click.Option]:
    return [
        parameter
        for parameter in click_command(name).params
        if isinstance(parameter, click.Option) and parameter.name != "help"
    ]


def _canonical_option(option: click.Option) -> str:
    spellings = [*option.opts, *option.secondary_opts]
    longs = [spelling for spelling in spellings if spelling.startswith("--")]
    return (longs[0] if longs else spellings[0]).lstrip("-")


def _descriptor_for(option: click.Option) -> dict[str, Any]:
    spellings = [*option.opts, *option.secondary_opts]
    canonical = _canonical_option(option)
    descriptor: dict[str, Any] = {
        "name": canonical,
        "type": "boolean" if option.is_flag else _click_type(option),
        "required": option.required,
    }
    if isinstance(option.default, (bool, int, float, str)):
        descriptor["default"] = option.default
    aliases = [spelling.lstrip("-") for spelling in spellings if spelling != f"--{canonical}"]
    if aliases:
        descriptor["aliases"] = aliases
    if isinstance(option.type, click.Choice):
        descriptor["enum"] = [str(choice) for choice in option.type.choices]
    if option.multiple:
        descriptor["repeatable"] = True
    return descriptor


def _click_type(option: click.Option) -> str:
    return {
        "integer": "integer",
        "integer range": "integer",
        "float": "number",
        "float range": "number",
        "boolean": "boolean",
    }.get(option.type.name, "string")


def _error(result: Any) -> dict[str, Any]:
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines, result.output
    return json.loads(lines[-1])["error"]


def _finished_session(state: str = "succeeded") -> str:
    meta = sessions.create_session(entry="mock", base_adapter="mock", prompt="fixture")
    sessions.mark_running(
        meta.session_id,
        pid=os.getpid(),
        process_start_time=proc.process_start_time(),
    )
    sessions.transition(meta.session_id, state, exit_code=0, stop_reason="test")
    return meta.session_id


def _active_session(*, target: str | None = None, pid: int | None = None) -> str:
    meta = sessions.create_session(
        entry="mock",
        base_adapter="mock",
        prompt="active fixture",
        target=target,
    )
    if pid is None:
        sessions.transition(meta.session_id, "canceled", exit_code=vocab.EXIT_CANCELLED)
        return meta.session_id
    sessions.mark_running(
        meta.session_id,
        pid=pid,
        process_start_time=proc.process_start_time(pid),
    )
    return meta.session_id


def _wait_for_state(session_id: str, state: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sessions.load(session_id).state == state:
            return
        time.sleep(0.05)
    pytest.fail(f"session {session_id} never became {state}")


def _background_session(cli: CliRunner, prompt: str = "slow:1 background") -> str:
    result = invoke(cli, "run", "mock", prompt, "--background", "--json", "--quiet")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    session_id = json.loads(result.stdout)["session_id"]
    _wait_for_state(session_id, "running")
    return session_id


def _log_fixture() -> str:
    session_id = _finished_session()
    events = transcript.Transcript(sessions.transcript_path(session_id))
    events.append("msg", text="schema conformance")
    return session_id


def _probe_fixture(root: Path) -> None:
    entry = MOCK_ENTRY.replace(
        '[modes]\ndefault = { grants = "read", delegates = true }',
        '[modes]\ndefault = { grants = "none", delegates = false }\nlegacy = '
        '{ grants = "all", delegates = false }',
    )
    (root / "agents" / "mock.toml").write_text(entry, encoding="utf-8")


def _format_selection(name: str, selection: str) -> list[str]:
    if selection == "default":
        return []
    if selection == "json":
        return ["--json"]
    if selection == "format":
        return ["--format", "ndjson" if name == "log" else "json"]
    return ["--format", selection]


def _quiet_if_supported(name: str) -> list[str]:
    return ["--quiet"] if any(option.name == "quiet" for option in accepted_options(name)) else []


def _success_call(
    cli: CliRunner,
    name: str,
    selection: str,
    label: str,
    root: Path,
    *,
    format_args: list[str] | None = None,
) -> Any:
    output_flags = _format_selection(name, selection) if format_args is None else format_args
    if selection == "plain":
        output_flags.extend(("--limit", "1"))
    quiet = _quiet_if_supported(name)
    if name == "agents check":
        return invoke(cli, "agents", "check", *output_flags, *quiet)
    if name == "agents create":
        return invoke(
            cli,
            "agents",
            "create",
            f"variant-{label}",
            "--extends",
            "mock",
            *output_flags,
        )
    if name == "agents delete":
        invoke(cli, "agents", "create", f"delete-{label}", "--extends", "mock", "--json")
        return invoke(cli, "agents", "delete", f"delete-{label}", *output_flags)
    if name == "agents get":
        return invoke(cli, "agents", "get", "mock", *output_flags)
    if name == "agents list":
        return invoke(cli, "agents", "list", *output_flags)
    if name == "cancel":
        session_id = sessions.create_session(
            entry="mock", base_adapter="mock", prompt="cancel fixture"
        ).session_id
        return invoke(cli, "cancel", session_id, *output_flags)
    if name == "continue":
        started = invoke(cli, "run", "mock", "echo:first", "--json", "--quiet")
        session_id = json.loads(started.stdout)["session_id"]
        return invoke(cli, "continue", session_id, "echo:next", *output_flags, *quiet)
    if name == "daemon status":
        return invoke(cli, "daemon", "status", *output_flags)
    if name == "daemon stop":
        return invoke(cli, "daemon", "stop", "never-started", *output_flags)
    if name == "delete":
        return invoke(cli, "delete", _finished_session(), "--yes", *output_flags)
    if name == "install":
        return invoke(cli, "install", "mock", "--yes", *output_flags)
    if name == "list":
        return invoke(cli, "list", *output_flags)
    if name == "log":
        return invoke(cli, "log", _log_fixture(), *output_flags, *quiet)
    if name == "probe":
        _probe_fixture(root)
        return invoke(cli, "probe", "mock", "--discover", *output_flags)
    if name == "prune":
        return invoke(cli, "prune", "--yes", *output_flags)
    if name == "resolve":
        return invoke(cli, "resolve", "mock", *output_flags)
    if name == "run":
        return invoke(cli, "run", "mock", "echo:conformance", *output_flags, *quiet)
    if name == "skills get":
        skills = json.loads(invoke(cli, "skills", "list", "--json").stdout)["items"]
        return invoke(cli, "skills", "get", skills[0]["name"], *output_flags)
    if name == "skills list":
        return invoke(cli, "skills", "list", *output_flags)
    if name == "status":
        return invoke(cli, "status", _finished_session(), *output_flags)
    if name == "steer":
        session_id = _background_session(cli, "slow:1 steer fixture")
        return invoke(cli, "steer", session_id, "echo:steered", *output_flags, *quiet)
    if name == "wait":
        session_id = _background_session(cli)
        return invoke(cli, "wait", session_id, *output_flags, *quiet)
    raise AssertionError(f"no scenario for schema command {name!r}")


def _assert_schema_value(schema: dict[str, Any], value: Any, path: str) -> None:
    if not schema:
        return
    assert set(schema) <= SCHEMA_KEYS, path
    if "enum" in schema:
        assert value in schema["enum"], (path, value, schema["enum"])
    declared_type = schema.get("type")
    allowed = declared_type if isinstance(declared_type, list) else [declared_type]
    if value is None:
        assert "null" in allowed, path
        return
    if "object" in allowed:
        assert isinstance(value, dict), path
        properties = schema.get("properties", {})
        assert set(schema.get("required", [])) <= set(value), path
        assert set(value) <= set(properties), (path, set(value) - set(properties))
        for key, item in value.items():
            _assert_schema_value(properties[key], item, f"{path}.{key}")
        return
    if "array" in allowed:
        assert isinstance(value, list), path
        for index, item in enumerate(value):
            _assert_schema_value(schema["items"], item, f"{path}[{index}]")
        return
    if "boolean" in allowed:
        assert isinstance(value, bool), path
    elif "integer" in allowed:
        assert isinstance(value, int) and not isinstance(value, bool), path
    elif "number" in allowed:
        assert isinstance(value, (int, float)) and not isinstance(value, bool), path
    elif "string" in allowed:
        assert isinstance(value, str), path
    else:
        raise AssertionError((path, schema))


def _assert_machine_output(detail: dict[str, Any], result: Any) -> None:
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    if detail.get("stream"):
        lines = result.stdout.splitlines()
        assert all(line.strip() for line in lines), result.stdout
        for index, line in enumerate(lines):
            value = json.loads(line)
            _assert_schema_value(detail["output"], value, f"stdout[{index}]")
    else:
        value = json.loads(result.stdout)
        _assert_schema_value(detail["output"], value, "stdout")


def test_D1_D3b_help_and_SPEC_match_the_generated_command_set(cli: CliRunner) -> None:
    index = read_index(cli)
    names = [entry["name"] for entry in index["commands"]]
    assert names == sorted(names)
    assert len(names) == len(set(names))

    root_help = invoke(cli, "--help").stdout
    assert "acpc schema" in root_help
    assert "--json" in root_help
    spec = Path("SPEC.md").read_text(encoding="utf-8")
    section = re.search(r"^## Command surface\n(.*?)(?=^## |\Z)", spec, re.MULTILINE | re.DOTALL)
    assert section is not None
    rows = re.findall(
        r"^\| `([^`]+)` \| `(read_only|idempotent|non_idempotent)` \|$",
        section.group(1),
        re.MULTILINE,
    )
    assert dict(rows) == {entry["name"]: entry["effects"] for entry in index["commands"]}
    surface_blocks = re.findall(r"```text\n(.*?)```", section.group(1), re.DOTALL)

    def spec_syntax(name: str) -> str:
        for block in surface_blocks:
            lines = block.splitlines()
            for index, line in enumerate(lines):
                if not (line == name or line.startswith(f"{name} ")):
                    continue
                syntax = [line]
                for continuation in lines[index + 1 :]:
                    if continuation.startswith(("    ", "\t")):
                        syntax.append(continuation)
                    else:
                        break
                return "\n".join(syntax)
        raise AssertionError(f"SPEC has no syntax for {name}")

    global_spellings = {
        spelling.lstrip("-")
        for flag in index["global_flags"]
        for spelling in (f"--{flag['name']}", *flag.get("aliases", []))
    }
    for entry in index["commands"]:
        name = entry["name"]
        spec_flags = set(re.findall(r"--([a-z][a-z0-9-]*)", spec_syntax(name)))
        options = accepted_options(name)
        parser_flags = {_canonical_option(option) for option in options}
        aliases = {
            spelling.lstrip("-")
            for option in options
            for spelling in (*option.opts, *option.secondary_opts)
            if spelling.startswith("--") and spelling.lstrip("-") not in parser_flags
        }
        spec_flags -= aliases
        assert spec_flags | global_spellings == parser_flags | global_spellings, name

    for name in names:
        command = click_command(name)
        result = invoke(cli, *name.split(), "--help")
        assert result.exit_code == vocab.EXIT_OK, (name, result.stderr)
        assert "Usage:" in result.stdout
        assert (command.short_help or command.help or "").split()[0] in result.stdout
        for option in accepted_options(name):
            assert not option.hidden, (name, _canonical_option(option))
            assert any(
                spelling.startswith("--") and spelling in result.stdout for spelling in option.opts
            )


def test_D6b_parser_descriptors_and_D7a_global_flags_are_generated(cli: CliRunner) -> None:
    index = read_index(cli)
    global_flags = {flag["name"]: flag for flag in index["global_flags"]}
    per_command: dict[str, set[str]] = {}
    for entry in index["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        command = click_command(name)
        args = [
            parameter.name for parameter in command.params if isinstance(parameter, click.Argument)
        ]
        assert [argument["name"] for argument in detail["args"]] == args
        published = {flag["name"]: flag for flag in detail["flags"]}
        accepted = accepted_options(name)
        per_command[name] = {_canonical_option(option) for option in accepted}
        assert len(published) == len(accepted) - len(global_flags)
        for option in accepted:
            expected = _descriptor_for(option)
            if expected["name"] in global_flags:
                assert expected["name"] not in published
                continue
            assert expected["name"] in published, (name, expected["name"])
            for key, value in expected.items():
                assert published[expected["name"]].get(key) == value, (name, key)

    common = set.intersection(*per_command.values())
    uniform = {
        name
        for name in common
        if len(
            {
                repr(next(flag for flag in read_detail(cli, path)["flags"] if flag["name"] == name))
                if name not in global_flags
                else repr(global_flags[name])
                for path in per_command
            }
        )
        == 1
    }
    assert set(global_flags) == uniform
    assert global_flags


def test_D6d_routing_D7b_flat_entries_and_D7c_shape(cli: CliRunner) -> None:
    index = read_index(cli)
    assert set(index) == {
        "schema_version",
        "tool_version",
        "global_flags",
        "format_defaults",
        "exit_codes",
        "conformance",
        "commands",
    }
    assert index["schema_version"].isdecimal()
    names = [entry["name"] for entry in index["commands"]]
    for entry in index["commands"]:
        assert set(entry) == {"name", "description", "effects"}
        assert entry["name"] and "  " not in entry["name"]
        assert entry["effects"] in {"read_only", "idempotent", "non_idempotent"}
        detail = read_detail(cli, entry["name"])
        assert detail["name"] == entry["name"]
        assert detail["effects"] == entry["effects"]
        output = detail.get("output", {})
        if "next" in output.get("properties", {}):
            assert "next" not in output.get("required", [])

    prefixes = {parts[0] for parts in (name.split() for name in names) if len(parts) > 1}
    for prefix in prefixes:
        result = invoke(cli, "schema", prefix)
        assert result.exit_code == vocab.EXIT_USAGE
        message = _error(result)["message"]
        assert any(name.startswith(f"{prefix} ") for name in names)
        assert prefix in message
    unknown = invoke(cli, "schema", "__no_such_command__")
    assert unknown.exit_code == vocab.EXIT_USAGE
    assert _error(unknown)["kind"] == errors.INVALID_INPUT


def _check_schema_shape(schema: dict[str, Any], path: str) -> None:
    assert set(schema) <= SCHEMA_KEYS, path
    if not schema:
        return
    assert "type" in schema, path
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    assert set(types) <= {"string", "integer", "number", "boolean", "array", "object", "null"}
    if "object" in types:
        assert "properties" in schema and "required" in schema, path
        for key, child in schema["properties"].items():
            _check_schema_shape(child, f"{path}.{key}")
    if "array" in types:
        assert "items" in schema, path
        _check_schema_shape(schema["items"], f"{path}[]")
    if "enum" in schema:
        assert isinstance(schema["enum"], list) and schema["enum"], path


@pytest.mark.timeout(120)
def test_D8_O4a_O4d_R1a_R1b_R5a_schema_and_success_matrix(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
    live_daemon: None,
) -> None:
    index = read_index(cli)
    for selection in ("json", "format"):
        for entry in index["commands"]:
            name = entry["name"]
            detail = read_detail(cli, name)
            _check_schema_shape(detail["output"], f"{name}.output")
            assert detail["effects"] == entry["effects"]
            assert detail["effects"] in {"read_only", "idempotent", "non_idempotent"}
            accepts_yes = any("--yes" in option.opts for option in accepted_options(name))
            assert detail["confirm"] is accepts_yes
            if detail["effects"] == "read_only":
                assert "changed" not in detail["output"].get("required", [])
            else:
                assert "changed" in detail["output"].get("required", [])
            if detail.get("stream"):
                assert name == "log"
                assert detail["output"]["type"] == "object"
            result = _success_call(cli, name, selection, f"{selection}-{name}", state_root)
            try:
                _assert_machine_output(detail, result)
            except AssertionError as error:
                raise AssertionError(f"{name} ({selection}): {error}") from error
            if any(option.name == "quiet" for option in accepted_options(name)):
                assert result.stderr == "" or result.stderr.endswith("\n")
        monkeypatch.setenv("NO_INPUT", "1")


def test_O2a_O2b_default_format_is_exercised_for_every_indexed_command(
    cli: CliRunner,
    state_root: Path,
    live_daemon: None,
) -> None:
    for entry in read_index(cli)["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        result = _success_call(cli, name, "default", f"default-{name}", state_root)
        assert result.exit_code == vocab.EXIT_OK, (name, result.stderr)
        if detail.get("format_defaults", {}).get("non_tty") == "json":
            _assert_machine_output(detail, result)
        else:
            assert result.stdout or result.stderr, name


def _read_fd(fd: int) -> bytes:
    data = bytearray()
    try:
        while chunk := os.read(fd, 4096):
            data.extend(chunk)
    except OSError:
        pass
    finally:
        os.close(fd)
    return bytes(data)


def _run_with_stream_context(
    args: list[str], *, stdout_tty: bool, stderr_tty: bool
) -> tuple[int, bytes, bytes]:
    masters: list[int] = []
    slaves: list[int | None] = []
    targets: list[Any] = []
    for is_tty in (stdout_tty, stderr_tty):
        if is_tty:
            master, slave = pty.openpty()
            masters.append(master)
            slaves.append(slave)
            targets.append(slave)
        else:
            slaves.append(None)
            targets.append(subprocess.PIPE)
    process = subprocess.Popen(
        [sys.executable, "-c", "from acpc.cli import main; raise SystemExit(main())", *args],
        stdin=subprocess.DEVNULL,
        stdout=targets[0],
        stderr=targets[1],
        env=os.environ.copy(),
    )
    for slave in slaves:
        if slave is not None:
            os.close(slave)
    pipe_stdout, pipe_stderr = process.communicate(timeout=10)
    tty_output = [_read_fd(master) for master in masters]
    output = iter(tty_output)
    stdout = next(output) if stdout_tty else pipe_stdout
    stderr = next(output) if not stdout_tty and stderr_tty else pipe_stderr
    if stdout_tty and stderr_tty:
        stderr = next(output)
    return process.returncode, stdout or b"", stderr or b""


@pytest.mark.skipif(sys.platform == "win32", reason="PTY is not available")
def test_O1_stream_contexts_are_independent_for_a_machine_document() -> None:
    contexts = ((False, False), (True, False), (False, True), (True, True))
    for stdout_tty, stderr_tty in contexts:
        code, stdout, stderr = _run_with_stream_context(
            ["list", "--json"],
            stdout_tty=stdout_tty,
            stderr_tty=stderr_tty,
        )
        assert code == vocab.EXIT_OK, stderr
        assert json.loads(stdout) == {"items": [], "has_more": False}
        assert stderr == b""


@pytest.mark.parametrize("no_input", [False, True], ids=["input-allowed", "no-input"])
def test_O1_O2b_O3a_F2a_F2c_machine_matrix(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
    no_input: bool,
    live_daemon: None,
) -> None:
    if no_input:
        monkeypatch.setenv("NO_INPUT", "1")
    else:
        monkeypatch.delenv("NO_INPUT", raising=False)
    index = read_index(cli)
    for entry in index["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        result = _success_call(cli, name, "json", f"matrix-{no_input}-{name}", state_root)
        _assert_machine_output(detail, result)
        assert "Traceback" not in result.stderr

    missing = invoke(cli, "run", "no-such-agent", "probe", "--json", "--quiet")
    assert missing.exit_code == vocab.EXIT_AGENT_ERROR
    assert missing.stdout == ""
    assert _error(missing)["kind"] == errors.NOT_FOUND
    assert json.loads([line for line in missing.stderr.splitlines() if line.strip()][-1])["error"]

    preformat = invoke(cli, "list", "--json", "--no-such-flag")
    assert preformat.exit_code == vocab.EXIT_USAGE
    assert preformat.stdout == ""
    assert _error(preformat)["kind"] == errors.INVALID_INPUT


def test_H5b_plain_alias_is_byte_identical_for_every_plain_collection(cli: CliRunner) -> None:
    for name, path in (
        ("agents list", ("agents", "list")),
        ("agents check", ("agents", "check")),
        ("list", ("list",)),
        ("daemon status", ("daemon", "status")),
        ("skills list", ("skills", "list")),
    ):
        limit = ["--limit", "1"]
        first = invoke(cli, *path, "--plain", *limit)
        second = invoke(cli, *path, "--format", "plain", *limit)
        assert first.exit_code == vocab.EXIT_OK, (name, first.stderr)
        assert second.exit_code == vocab.EXIT_OK, (name, second.stderr)
        assert first.stdout.encode() == second.stdout.encode(), name
        assert all(line and "--" not in line for line in first.stdout.splitlines()), name


def test_O7a_O7b_O7c_O7d_log_stream_is_framed_bounded_ordered_and_complete(
    cli: CliRunner,
) -> None:
    session_id = _finished_session()
    events = transcript.Transcript(sessions.transcript_path(session_id))
    for number in range(3):
        events.append("msg", text=f"event-{number}")

    result = invoke(cli, "log", session_id, "--json", "--since", "0", "--limit", "2", "--quiet")
    assert result.exit_code == vocab.EXIT_OK, result.stderr
    framed = result.stdout.splitlines(keepends=True)
    assert len(framed) == 2
    assert all(line.endswith("\n") and line.strip() for line in framed)
    records = [json.loads(line) for line in framed]
    assert all(record["type"] == "msg" for record in records)
    assert [record["i"] for record in records] == sorted(record["i"] for record in records)
    assert result.stderr == ""

    followed = invoke(
        cli,
        "log",
        session_id,
        "--json",
        "--follow",
        "--since",
        "0",
        "--limit",
        "1",
        "--quiet",
    )
    assert followed.exit_code == vocab.EXIT_OK, followed.stderr
    assert len(followed.stdout.splitlines()) == 1


def test_F1e_O2f_exit_table_and_delegated_stdout_decision(cli: CliRunner) -> None:
    index = read_index(cli)
    assert set(index["exit_codes"]) == set(vocab.EXIT_DESCRIPTIONS)
    for entry in index["commands"]:
        detail = read_detail(cli, entry["name"])
        assert "exit_codes" not in detail
        assert "delegates_stdout" not in detail
    result = _success_call(cli, "run", "json", "delegated", Path(os.environ["ACPC_HOME"]))
    assert result.exit_code == vocab.EXIT_OK
    assert json.loads(result.stdout)["status"] == "succeeded"


def test_R7a_R7b_R7c_background_changes_wait_only_and_keeps_identifier(
    cli: CliRunner,
    live_daemon: None,
) -> None:
    detail = read_detail(cli, "run")
    assert "block by default" in detail["description"]
    started = invoke(
        cli,
        "run",
        "mock",
        "slow:1 accepted work",
        "--background",
        "--permissions",
        "read",
        "--json",
        "--quiet",
    )
    assert started.exit_code == vocab.EXIT_OK, started.stderr
    session_id = json.loads(started.stdout)["session_id"]
    assert sessions.read_meta(session_id).is_active
    waited = invoke(cli, "wait", session_id, "--json", "--quiet")
    assert waited.exit_code == vocab.EXIT_OK, waited.stderr
    assert json.loads(waited.stdout)["session_id"] == session_id
    assert sessions.read_meta(session_id).state == "succeeded"


def _enum_values(value: Any, path: str = "") -> list[tuple[str, list[Any]]]:
    found: list[tuple[str, list[Any]]] = []
    if isinstance(value, dict):
        if "enum" in value:
            found.append((path, list(value["enum"])))
        for key, child in value.get("properties", {}).items():
            found.extend(_enum_values(child, f"{path}.{key}"))
        if "items" in value:
            found.extend(_enum_values(value["items"], f"{path}[]"))
    return found


def _observe_flag_enums(
    cli: CliRunner, index: dict[str, Any], state_root: Path, observed: dict[str, set[Any]]
) -> None:
    for flag in index["global_flags"]:
        if "enum" in flag:
            for value in flag["enum"]:
                result = invoke(cli, "list", "--json", "--color", value)
                assert result.exit_code == vocab.EXIT_OK, result.stderr
                observed[f"flag:{flag['name']}"] = observed.get(f"flag:{flag['name']}", set()) | {
                    value
                }
    for entry in index["commands"]:
        name = entry["name"]
        detail = read_detail(cli, name)
        for flag in detail["flags"]:
            if "enum" not in flag:
                continue
            key = f"{name}.flag:{flag['name']}"
            for value in flag["enum"]:
                if flag["name"] == "format":
                    result = _success_call(
                        cli,
                        name,
                        value,
                        f"enum-{name}-{value}",
                        state_root,
                        format_args=["--format", value],
                    )
                elif flag["name"] == "permissions":
                    safe_value = str(value).replace("-", "_")
                    args = [
                        "agents",
                        "create",
                        f"enum-{name.replace(' ', '-')}-{safe_value}",
                        "--extends",
                        "mock",
                        "--permissions",
                        value,
                        "--json",
                    ]
                    result = invoke(cli, *args)
                else:
                    continue
                assert result.exit_code == vocab.EXIT_OK, (key, value, result.stderr)
                observed[key] = observed.get(key, set()) | {value}


def _observe_session_statuses(cli: CliRunner, observed: dict[str, set[Any]]) -> None:
    for state in vocab.SESSION_STATES:
        session_id = sessions.create_session(
            entry="mock", base_adapter="mock", prompt=state
        ).session_id
        if state == "starting":
            pass
        elif state == "running":
            sessions.mark_running(
                session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
            )
        elif state == "preparing":
            sessions.mark_running(
                session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
            )
            path = sessions.meta_path(session_id)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = "preparing"
            path.write_text(json.dumps(payload), encoding="utf-8")
        elif state == "unknown":
            sessions.mark_running(session_id, pid=999999, process_start_time="gone")
        else:
            sessions.mark_running(
                session_id, pid=os.getpid(), process_start_time=proc.process_start_time()
            )
            sessions.transition(session_id, state, exit_code=0, stop_reason="test")
        result = invoke(cli, "status", session_id, "--json")
        assert result.exit_code == vocab.EXIT_OK
        observed["session.status"] = observed.get("session.status", set()) | {
            json.loads(result.stdout)["status"]
        }


def _observe_log_types(cli: CliRunner, observed: dict[str, set[Any]]) -> None:
    log_id = sessions.create_session(entry="mock", base_adapter="mock", prompt="events").session_id
    events = transcript.Transcript(sessions.transcript_path(log_id))
    event_fields = {
        "msg": {"text": "msg"},
        "thought": {"text": "thought"},
        "tool": {"name": "tool", "args_summary": "", "status": "done", "duration_ms": 0},
        "permission": {"kind": "read", "decision": "allowed"},
        "error": {"message": "error"},
        "state": {"from": "running", "to": "succeeded"},
        "usage": {"tokens": 0, "cost": 0.0},
    }
    for event_type in sorted(transcript.EVENT_TYPES):
        events.append(event_type, **event_fields[event_type])
    log_result = invoke(cli, "log", log_id, "--json")
    assert log_result.exit_code == vocab.EXIT_OK
    observed["log.type"] = {json.loads(line)["type"] for line in log_result.stdout.splitlines()}


def _observe_discovery_enums(
    cli: CliRunner, state_root: Path, observed: dict[str, set[Any]]
) -> None:
    _probe_fixture(state_root)
    probe_result = invoke(cli, "probe", "mock", "--discover", "--json")
    assert probe_result.exit_code == vocab.EXIT_OK, probe_result.stderr
    observed["probe.diff.status"] = {
        item["status"] for item in json.loads(probe_result.stdout)["diff"]
    }
    agents_result = invoke(cli, "agents", "list", "--json")
    assert agents_result.exit_code == vocab.EXIT_OK, agents_result.stderr
    observed["agents.kind"] = {item["kind"] for item in json.loads(agents_result.stdout)["items"]}


def _assert_reachable_output_enums(
    cli: CliRunner, index: dict[str, Any], observed: dict[str, set[Any]]
) -> None:
    for entry in index["commands"]:
        detail = read_detail(cli, entry["name"])
        for path, values in _enum_values(detail.get("output", {}), entry["name"] + ".output"):
            key = "session.status" if path.endswith("status") else path.rsplit(".", 1)[-1]
            if path.endswith("type"):
                key = "log.type"
            if path.endswith("kind"):
                key = "agents.kind"
            if "probe" in path and path.endswith("status"):
                key = "probe.diff.status"
            assert set(values) <= observed.get(key, set()), (path, values, observed)


@pytest.mark.timeout(120)
def test_O4c_declared_enums_have_reachable_values(
    cli: CliRunner,
    state_root: Path,
    live_daemon: None,
) -> None:
    index = read_index(cli)
    observed: dict[str, set[Any]] = {}
    _observe_flag_enums(cli, index, state_root, observed)
    _observe_session_statuses(cli, observed)
    _observe_log_types(cli, observed)
    _observe_discovery_enums(cli, state_root, observed)
    _assert_reachable_output_enums(cli, index, observed)


def _start_daemon_pair(cli: CliRunner) -> tuple[str, str, int]:
    first = _background_session(cli, "slow:30 cancel victim")
    second = _background_session(cli, "slow:30 cancel sibling")
    first_meta = sessions.load(first)
    second_meta = sessions.load(second)
    assert first_meta.pid is not None and first_meta.pid == second_meta.pid
    return first, second, first_meta.pid


def _assert_process_alive(pid: int) -> None:
    assert proc.process_liveness(pid, proc.process_start_time(pid)) == "verified"


@pytest.mark.parametrize(
    "layout",
    [
        "accepted_pending",
        "rpc_unconfirmed",
        "terminal_before_read",
        "terminal_between_rpc",
        "preparing_stale",
        "cmdline_unknown",
    ],
)
def test_M2_cancel_races_are_forced_and_preserve_the_daemon_and_sibling(
    cli: CliRunner,
    live_daemon: None,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    victim, sibling, daemon_pid = _start_daemon_pair(cli)
    victim_meta = sessions.read_meta(victim)

    if layout == "accepted_pending":

        async def accepted(_target: str, _session_id: str) -> Any:
            return cli_module._DaemonCancelReply(accepted=True, turn_token=victim_meta.turns)

        monkeypatch.setattr(cli_module, "_cancel_with_daemon", accepted)
        monkeypatch.setattr(
            cli_module, "_wait_for_cancel", lambda *_args, **_kwargs: sessions.read_meta(victim)
        )
        result = invoke(cli, "cancel", victim, "--json")
        assert result.exit_code == vocab.EXIT_OK
        assert json.loads(result.stdout)["changed"] is True
        assert json.loads(result.stdout)["status"] == "running"
        assert sessions.read_meta(victim).state == "running"
    elif layout == "rpc_unconfirmed":

        async def unconfirmed(_target: str, _session_id: str) -> Any:
            return None

        monkeypatch.setattr(cli_module, "_cancel_with_daemon", unconfirmed)
        result = invoke(cli, "cancel", victim, "--json")
        assert result.exit_code == vocab.EXIT_AGENT_ERROR
        error = _error(result)
        assert error["kind"] == errors.OUTCOME_UNKNOWN
        assert error["context"] == {"session_id": victim, "status": "running"}
        assert sessions.read_meta(victim).state == "running"
    elif layout == "terminal_before_read":
        sessions.transition(victim, "succeeded", exit_code=0, stop_reason="race")

        async def no_preparation(_targets: Any) -> set[str]:
            return set()

        monkeypatch.setattr(cli_module, "_collect_preparing_sessions", no_preparation)
        result = invoke(cli, "cancel", victim, "--json")
        assert result.exit_code == vocab.EXIT_OK
        assert json.loads(result.stdout) == {
            "session_id": victim,
            "status": "succeeded",
            "stop_reason": "race",
            "changed": False,
        }
    elif layout == "terminal_between_rpc":

        async def loses_race(_target: str, _session_id: str) -> Any:
            sessions.transition(victim, "succeeded", exit_code=0, stop_reason="race")
            return None

        monkeypatch.setattr(cli_module, "_cancel_with_daemon", loses_race)
        result = invoke(cli, "cancel", victim, "--json")
        assert result.exit_code == vocab.EXIT_OK
        assert json.loads(result.stdout)["status"] == "succeeded"
        assert json.loads(result.stdout)["changed"] is False
    elif layout == "preparing_stale":
        sessions.transition(victim, "succeeded", exit_code=0, stop_reason="old turn")

        async def preparing(_targets: Any) -> set[str]:
            return {victim}

        async def accepted_preparation(_target: str, _session_id: str) -> Any:
            return cli_module._DaemonCancelReply(accepted=True, turn_token=victim_meta.turns)

        monkeypatch.setattr(cli_module, "_collect_preparing_sessions", preparing)
        monkeypatch.setattr(cli_module, "_cancel_with_daemon", accepted_preparation)
        monkeypatch.setattr(
            cli_module, "_wait_for_cancel", lambda *_args, **_kwargs: sessions.read_meta(victim)
        )
        result = invoke(cli, "cancel", victim, "--json")
        assert result.exit_code == vocab.EXIT_OK
        assert json.loads(result.stdout)["status"] == "succeeded"
        assert json.loads(result.stdout)["changed"] is True
        assert sessions.read_meta(victim).state == "succeeded"
    else:

        async def unavailable(_target: str, _session_id: str) -> Any:
            return daemon_client.DaemonUnavailable("forced fallback")

        monkeypatch.setattr(cli_module, "_cancel_with_daemon", unavailable)
        monkeypatch.setattr(cli_module.proc, "process_cmdline", lambda _pid: None)
        result = invoke(cli, "cancel", victim, "--json")
        assert result.exit_code == vocab.EXIT_AGENT_ERROR
        error = _error(result)
        assert error["kind"] == errors.OUTCOME_UNKNOWN
        assert error["context"] == {"session_id": victim, "status": "running"}
        assert sessions.read_meta(victim).state == "running"

    _assert_process_alive(daemon_pid)
    assert sessions.read_meta(sibling).is_active


def _wait_for_ready(process: subprocess.Popen[bytes]) -> None:
    assert process.stderr is not None
    assert process.stderr.readline() == b"READY\n"


def _read_pty(fd: int, marker: bytes) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + 5
    while marker not in data and time.monotonic() < deadline:
        try:
            data.extend(os.read(fd, 4096))
        except OSError as error:
            raise AssertionError((bytes(data), error)) from error
    assert marker in data
    return bytes(data)


def _drain_pty(fd: int) -> bytes:
    data = bytearray()
    os.set_blocking(fd, False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        except OSError:
            break
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_F2b_F5_wait_interrupts_in_pipe_and_pty_contexts(cli: CliRunner, live_daemon: None) -> None:
    session_id = _background_session(cli, "slow:5 wait interruption")
    ready_code = (
        "from acpc.cli import main; import sys; sys.stderr.write('READY\\n'); "
        "sys.stderr.flush(); raise SystemExit(main())"
    )
    command = [
        sys.executable,
        "-c",
        ready_code,
        "wait",
        session_id,
        "--json",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    _wait_for_ready(process)
    time.sleep(0.2)
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == vocab.EXIT_CANCELLED
    assert stdout == b""
    assert json.loads(stderr.splitlines()[-1])["error"]["kind"] == errors.INTERRUPTED
    _wait_for_state(session_id, "succeeded", timeout=10)

    session_id = _background_session(cli, "slow:5 wait pty interruption")
    master, slave = pty.openpty()
    pty_process = subprocess.Popen(
        [*command[:-2], session_id, "--json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=slave,
        start_new_session=True,
        env=os.environ.copy(),
    )
    os.close(slave)
    ready = _read_pty(master, b"READY")
    time.sleep(0.2)
    os.killpg(pty_process.pid, signal.SIGINT)
    stdout, _ = pty_process.communicate(timeout=10)
    stderr = ready + _drain_pty(master)
    os.close(master)
    assert pty_process.returncode == vocab.EXIT_CANCELLED
    assert stdout == b""
    assert json.loads(stderr.splitlines()[-1])["error"]["kind"] == errors.INTERRUPTED
    _wait_for_state(session_id, "succeeded", timeout=10)


def test_D7c_claim_is_bound_to_the_standard_header(cli: CliRunner) -> None:
    version = re.search(r"^\*\*Version:\*\*\s+(\S+)$", STANDARD_PATH.read_text(), re.MULTILINE)
    assert version is not None
    assert read_index(cli)["conformance"]["standard"] == version.group(1)
