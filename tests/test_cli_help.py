"""Behavioral tests for first-contact help and CLI hardening."""

import json
import re
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from acpc import __version__, interaction, schema, vocab
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
plan = {{ grants = "read", delegates = true }}

[presets]
standard = {{ model = "mock-sonnet-5", effort = "high" }}
'''


@pytest.fixture(autouse=True)
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "mock.toml").write_text(MOCK_ENTRY, encoding="utf-8")
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str):
    return runner.invoke(main, list(args), catch_exceptions=False)


def _command_tree(command: click.Command, path: tuple[str, ...] = ()):
    """Discover the registered Click command tree for the help contract."""
    yield path, command
    if isinstance(command, click.Group):
        for name in sorted(command.commands):
            yield from _command_tree(command.commands[name], (*path, name))


def _rendered_option_record(rendered: str, option: click.Option) -> str:
    """Return one option row, including its wrapped help continuation."""
    lines = rendered.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("  ") and any(line[2:].startswith(label) for label in option.opts)
    )
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("  -")),
        len(lines),
    )
    return " ".join(lines[start:end])


def test_root_help_is_a_compact_cheat_sheet(runner: CliRunner) -> None:
    result = invoke(runner, "--help")

    assert result.exit_code == vocab.EXIT_OK
    assert len(result.stdout.splitlines()) <= 100
    assert "--permissions execute" in result.stdout
    assert "request_permission" in result.stdout
    assert "status <id>       liveness-verified metadata for one session" in result.stdout
    assert (
        "list              running + the 20 most recent sessions (--limit N to change)"
        in result.stdout
    )
    assert (
        "cancel <id>       cancel a running session; it stays resumable with continue"
        in result.stdout
    )


def test_root_help_describes_every_group_and_command(runner: CliRunner) -> None:
    result = invoke(runner, "--help")

    assert result.exit_code == vocab.EXIT_OK
    lines = result.stdout.splitlines()
    for group in ("agents", "daemon", "skills"):
        line = next(line for line in lines if line.startswith(f"  {group} "))
        assert len(line.split()) > 1
    for entry in schema.index(main)["commands"]:
        prefix = f"  {entry['name']}"
        line = next(line for line in lines if line.startswith(prefix))
        assert len(line.split()) > len(entry["name"].split())


def test_root_help_command_catalog_does_not_present_groups_as_commands(
    runner: CliRunner,
) -> None:
    result = invoke(runner, "--help")

    assert result.exit_code == vocab.EXIT_OK
    assert "Common commands:" not in result.stdout
    commands = result.stdout.split("Commands:\n", maxsplit=1)[1].split(
        "  Use `acpc <command> --help`", maxsplit=1
    )[0]
    assert "agents list" in commands
    assert "skills list" in commands
    assert "daemon status" in commands
    assert all(line.strip() not in {"agents", "skills", "daemon"} for line in commands.splitlines())


def test_permission_help_names_the_tier_gloss(runner: CliRunner) -> None:
    root_help = invoke(runner, "--help")
    run_help = invoke(runner, "run", "--help")

    assert "none · read · edit · execute · all · ask" in root_help.stdout
    assert "none, read, edit, execute, all or ask" in run_help.stdout
    assert "write and prompt are deprecated aliases" in run_help.stdout


def test_short_help_matches_root_help(runner: CliRunner) -> None:
    long_help = invoke(runner, "--help")
    short_help = invoke(runner, "-h")

    assert short_help.stdout == long_help.stdout


def test_every_registered_command_has_helpful_options_and_both_help_spellings(
    runner: CliRunner,
) -> None:
    """The Click tree is the source of truth for the option-help sweep."""
    for path, command in _command_tree(main):
        for spelling in ("-h", "--help"):
            result = invoke(runner, *path, spelling)
            assert result.exit_code == vocab.EXIT_OK, (
                f"{' '.join(path) or 'acpc'} {spelling} failed: {result.stderr}"
            )

        # The root is a deliberately custom cheat sheet, so it does not render
        # Click's option table. Its two help spellings are checked above.
        if not path:
            continue
        rendered = invoke(runner, *path, "--help").stdout
        for option in command.params:
            if not isinstance(option, click.Option) or option.hidden:
                continue
            labels = "/".join(option.opts)
            record = _rendered_option_record(rendered, option)
            for label in option.opts:
                record = record.replace(label, " ")
            if option.metavar is not None:
                record = record.replace(option.metavar, " ")
            record = re.sub(r"\[[^]]*\]", " ", record)
            record = re.sub(r"\b[A-Z][A-Z0-9_-]*\b", " ", record)
            assert re.search(r"[A-Za-z]{2,}", record), (
                f"{' '.join(path)} {labels} has no rendered help text"
            )


@pytest.mark.parametrize("spelling", ["-h", "--help"])
def test_named_agents_view_accepts_both_help_spellings(runner: CliRunner, spelling: str) -> None:
    result = invoke(runner, "agents", "get", "mock", spelling)

    assert result.exit_code == vocab.EXIT_OK
    assert "Show one adapter or variant" in result.stdout


def test_help_names_behavioral_defaults_and_global_output_default(runner: CliRunner) -> None:
    def normalized(text: str) -> str:
        return " ".join(text.split()).replace("wall- clock", "wall-clock").replace("-- ", "--")

    run_help = normalized(invoke(runner, "run", "--help").stdout)
    continue_help = normalized(invoke(runner, "continue", "--help").stdout)
    steer_help = normalized(invoke(runner, "steer", "--help").stdout)
    wait_help = normalized(invoke(runner, "wait", "--help").stdout)
    log_help = normalized(invoke(runner, "log", "--help").stdout)

    assert "the session keeps running" in run_help
    assert "the session keeps running" in continue_help
    assert "the session keeps running" in steer_help
    assert "--cancel-after" in run_help
    assert "--cancel-after" in continue_help
    assert "--cancel-after" in steer_help
    assert "Unbounded by default" in wait_help
    assert "unbounded by default" in log_help
    assert "Without --since, --limit, or --tail this shows the last 20 events" in log_help
    assert "emit selected records in transcript order" in log_help
    assert "Conflicts with --tail" in log_help
    assert "Select the last N matching records and emit them in transcript order" in log_help
    assert "--tail replays its last N records first" in log_help
    assert (
        "absent, ask when acpc could put the question — stdin and stdout both terminals, "
        "no --json, NO_INPUT unset — and read in every other case"
    ) in run_help
    assert "[default: 131072" in run_help
    assert "[default: 131072" in wait_help
    assert "[default: 131072" in log_help


def test_help_explains_session_lifecycle_and_retention(runner: CliRunner) -> None:
    def normalized(text: str) -> str:
        return " ".join(text.split()).replace("mid- conversation", "mid-conversation")

    stop_help = normalized(invoke(runner, "cancel", "--help").stdout)
    list_help = normalized(invoke(runner, "list", "--help").stdout)
    status_help = normalized(invoke(runner, "status", "--help").stdout)
    wait_help = normalized(invoke(runner, "wait", "--help").stdout)
    continue_help = normalized(invoke(runner, "continue", "--help").stdout)
    steer_help = normalized(invoke(runner, "steer", "--help").stdout)
    prune_help = normalized(invoke(runner, "prune", "--help").stdout)

    assert "Cancel the running turn; the session stays usable with ``acpc continue``." in stop_help
    assert (
        "Selects the turn in flight when the call starts and sends ACP ``session/cancel``"
        in stop_help
    )
    assert "an unknown id is ``not_found``" in stop_help
    assert "List liveness-verified sessions as a bounded collection." in list_help
    assert "Return at most N sessions" in list_help
    assert "Show liveness-verified metadata for one session" in status_help
    assert "absent, text on a TTY and JSON on non-TTY" in status_help
    assert "exits 124: the session keeps running" in wait_help
    assert "The exit code mirrors the turn's result." in wait_help
    assert "the free way to reprint an answer." in wait_help
    assert "editing an entry never changes a session mid-conversation." in continue_help
    assert (
        "``--permissions`` is the one ``run`` resolution flag ``continue`` accepts" in continue_help
    )
    assert (
        "A finished session is a ``conflict``; the follow-up verb is ``acpc continue``."
        in steer_help
    )
    assert (
        "the ``retention`` key in the global config (``~/.acpc/config.toml``, default 90d; ``ACPC_HOME`` moves the root)"
        in prune_help
    )
    assert "deleting every finished session takes an explicit ``--older-than 0d``." in prune_help


def test_run_help_uses_a_neutral_permission_metavar(runner: CliRunner) -> None:
    result = invoke(runner, "run", "--help")

    option_line = next(
        line for line in result.stdout.splitlines() if line.lstrip().startswith("--permissions")
    )
    option_usage = option_line.strip().split("  ", maxsplit=1)[0]
    assert option_usage == "--permissions P"
    assert "write" not in option_usage
    assert "prompt" not in option_usage


def test_run_help_is_a_distinct_reference_page(runner: CliRunner) -> None:
    root_help = invoke(runner, "--help")
    run_help = invoke(runner, "run", "--help")

    assert run_help.stdout != root_help.stdout
    assert "--max-output" in run_help.stdout
    assert "Example" in run_help.stdout


def test_log_help_documents_activity_waiting(runner: CliRunner) -> None:
    result = invoke(runner, "log", "--help")

    assert "--wait-new" in result.stdout
    assert "Example" in result.stdout


@pytest.mark.parametrize("verb", ["cancel", "delete", "install"])
def test_short_verbs_have_real_help_pages(runner: CliRunner, verb: str) -> None:
    root_help = invoke(runner, "--help")
    command_help = invoke(runner, verb, "--help")

    assert command_help.stdout != root_help.stdout
    assert "Example" in command_help.stdout


def test_no_command_redirects_to_root_help_and_root_keeps_verb_one_liners(
    runner: CliRunner,
) -> None:
    root_help = invoke(runner, "--help").stdout
    redirected = [
        name for name in main.commands if invoke(runner, name, "--help").stdout == root_help
    ]

    assert redirected == []
    for name in ("cancel", "delete", "prune", "install"):
        described = [
            line
            for line in root_help.splitlines()
            if line.strip().startswith(f"{name} ") and len(line.split()) >= 3
        ]
        assert described, f"the root page needs a one-liner for `{name}`"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ("--follow",),
            ("Error: --follow is a log flag; use: acpc log <id> --follow [--timeout S]"),
        ),
        (
            ("-f",),
            ("Error: -f is not accepted; use --follow: acpc log <id> --follow [--timeout S]"),
        ),
        (
            ("status", "--detach"),
            (
                "Error: --detach is not an acpc flag — background dispatch is: acpc run "
                '<agent> "<prompt>" --background'
            ),
        ),
        (
            ("-d",),
            (
                "Error: --detach is not an acpc flag — background dispatch is: acpc run "
                '<agent> "<prompt>" --background'
            ),
        ),
        (
            ("status", "-C", "/tmp"),
            "Error: -C is not an acpc flag — the working-directory flag is --cwd DIR",
        ),
        (
            ("logs", "q7x2"),
            "Error: no such command 'logs' — the viewing command is: acpc log <id>",
        ),
        (
            ("daemon", "list"),
            "Error: no such command 'list' — the daemon view is: acpc daemon status",
        ),
        (
            ("daemon", "ls"),
            "Error: no such command 'ls' — the daemon view is: acpc daemon status",
        ),
        (
            ("daemon", "ps"),
            "Error: no such command 'ps' — the daemon view is: acpc daemon status",
        ),
        (
            ("daemon", "stop", "--all"),
            (
                "Error: --all is not a daemon flag — bare acpc daemon stop already addresses "
                "every daemon"
            ),
        ),
        (
            ("daemon", "start"),
            (
                "Error: no such command 'start' — daemons start on first use; acpc daemon stop "
                "<agent> and the next run is the restart"
            ),
        ),
        (
            ("daemon", "restart"),
            (
                "Error: no such command 'restart' — daemons start on first use; acpc daemon stop "
                "<agent> and the next run is the restart"
            ),
        ),
    ],
)
def test_neighboring_tool_aliases_are_one_line_usage_errors(
    runner: CliRunner, args: tuple[str, ...], expected: str
) -> None:
    result = invoke(runner, *args)

    assert result.exit_code == vocab.EXIT_USAGE
    # stderr is a pipe here, so the actionable line arrives inside the envelope.
    assert len(result.stderr.splitlines()) == 1
    envelope = json.loads(result.stderr)["error"]
    assert envelope["kind"] == "invalid_input"
    assert f"Error: {envelope['message']}" == expected
    assert "Traceback" not in result.stderr


def test_status_without_an_id_names_the_collection_command(runner: CliRunner) -> None:
    result = invoke(runner, "status")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "acpc list" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("agents",),
        ("skills",),
        ("daemon",),
        ("probe",),
        ("probe", "mock"),
        ("agents", "create"),
        ("agents", "get"),
        ("agents", "delete"),
        ("skills", "get"),
        ("cancel",),
        ("continue",),
        ("continue", "abcd"),
        ("delete",),
        ("install",),
        ("log",),
        ("resolve",),
        ("run",),
        ("status",),
        ("steer",),
        ("steer", "abcd"),
        ("wait",),
    ],
)
def test_missing_required_input_is_one_enveloped_error_with_a_hint(
    runner: CliRunner, args: tuple[str, ...]
) -> None:
    result = invoke(runner, *args)

    assert result.exit_code == vocab.EXIT_USAGE
    assert len(result.stderr.splitlines()) == 1
    error = json.loads(result.stderr)["error"]
    assert error["kind"] == "invalid_input"
    assert error["message"]
    assert error["hint"]
    assert "Usage:" not in error["message"]


@pytest.mark.parametrize(
    "args",
    [("status", "--limit", "1"), ("status", "--plain", "--limit", "1")],
)
def test_status_collection_flags_name_the_replacement_command(
    runner: CliRunner, args: tuple[str, ...]
) -> None:
    result = invoke(runner, *args)

    assert result.exit_code == vocab.EXIT_USAGE
    assert "acpc list" in result.stderr


def test_short_version_matches_long_version(runner: CliRunner) -> None:
    short_version = invoke(runner, "-V")
    long_version = invoke(runner, "--version")

    assert short_version.stdout == long_version.stdout
    # The version string alone: a caller that reads it should not have to
    # strip a tool name off the front of it.
    assert short_version.stdout.strip() == __version__


def test_background_prompt_policy_is_rejected_on_a_tty(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(interaction, "stdout_is_tty", lambda: True)

    result = invoke(runner, "run", "mock", "hello", "--permissions", "prompt", "--bg")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--bg" in result.stderr


def test_fresh_nested_state_root_lists_no_sessions(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACPC_HOME", str(tmp_path / "nested" / "state"))

    result = invoke(runner, "list")

    assert result.exit_code == vocab.EXIT_OK


def test_last_selector_is_rejected_without_a_tty(runner: CliRunner) -> None:
    invoke(runner, "run", "mock", "echo:hello", "--quiet")

    result = invoke(runner, "status", "last")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "TTY" in result.stderr


def test_unknown_flag_is_a_usage_error(runner: CliRunner) -> None:
    result = invoke(runner, "list", "--bogus")

    assert result.exit_code == vocab.EXIT_USAGE
    assert "--bogus" in result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.stderr


def test_corrupt_meta_is_a_clean_cli_error(runner: CliRunner, state_root: Path) -> None:
    created = invoke(runner, "run", "mock", "echo:hello", "--quiet", "--json")
    session_id = json.loads(created.stdout)["session_id"]
    (state_root / "sessions" / session_id / "meta.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    result = invoke(runner, "status", session_id)

    assert result.exit_code != vocab.EXIT_OK
    assert "meta.json" in result.stderr
    assert "Traceback" not in result.stderr


def test_truncated_transcript_tail_is_ignored_by_log(runner: CliRunner, state_root: Path) -> None:
    created = invoke(runner, "run", "mock", "echo:hello", "--quiet", "--json")
    session_id = json.loads(created.stdout)["session_id"]
    with (state_root / "sessions" / session_id / "transcript.ndjson").open("ab") as file:
        file.write(b'{"broken')

    result = invoke(runner, "log", session_id)

    assert result.exit_code == vocab.EXIT_OK
    assert "Traceback" not in result.stderr
