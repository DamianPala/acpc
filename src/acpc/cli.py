"""CLI entry point.

SPEC.md *Command surface*. Verbs land slice by slice; this module owns flag
parsing, usage errors (exit 2), the TTY rules and the fixed exit codes, and
delegates everything else to the layer that owns it.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from acpc import (
    __version__,
    cache,
    config,
    daemon_client,
    output,
    paths,
    proc,
    render,
    runner,
    sessions,
    transcript,
    vocab,
)
from acpc.registry import AgentRegistry, CallResolution, FieldSource, RegistryError, ResolvedEntry


class UsageProblem(click.ClickException):
    """A usage error: one actionable line on stderr, exit 2."""

    exit_code = vocab.EXIT_USAGE

    def format_message(self) -> str:
        return self.message


class AgentProblem(click.ClickException):
    """An agent-side failure: one actionable line on stderr, exit 1."""

    exit_code = vocab.EXIT_AGENT_ERROR

    def format_message(self) -> str:
        return self.message


_ROOT_HELP = """acpc — dispatch coding agents over ACP.

Quick reference

Sync run:
  acpc run <agent> "Explain this code"
  acpc run <agent> "Implement the fix" --permissions write

Background run + wait:
  id="$(acpc run <agent> "Run the tests" --bg | head -n1)"
  acpc wait "$id"

Continue:
  acpc continue <id> "Now summarize the result"

Status and log polling:
  acpc status
  acpc log <id> --wait-new --timeout 30

Heredoc prompt:
  acpc run <agent> - --permissions write <<'PROMPT'
  Review the implementation and make the required edits.
  PROMPT

Common commands:
  run, continue, wait, status, log, agents, daemon, stop, rm, prune, install
  Use `acpc <command> --help` for the command's full reference.

Flag → ACP
  --mode         → session/set_mode
  --permissions  → request_permission
  --model        → session/new (model)
  --effort       → session/new (effort)"""


class _CheatSheetGroup(click.Group):
    """Use the compact first-contact page for the root command."""

    def get_help(self, ctx: click.Context) -> str:
        return _ROOT_HELP

    def main(self, *args: Any, **kwargs: Any) -> Any:
        """Render Click usage errors as the CLI's single actionable line."""
        if not kwargs.get("standalone_mode", True):
            return super().main(*args, **kwargs)
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)
        except click.UsageError as error:
            click.echo(f"Error: {error.format_message()}", err=True)
            raise SystemExit(error.exit_code) from None
        except click.ClickException as error:
            error.show()
            raise SystemExit(error.exit_code) from None


class _RootHelpCommand(click.Command):
    """Make a short command's help intentionally reuse the cheat sheet."""

    def get_help(self, ctx: click.Context) -> str:
        return _ROOT_HELP


@click.group(cls=_CheatSheetGroup, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", message="acpc %(version)s")
@click.help_option("-h", "--help")
@click.pass_context
def main(ctx: click.Context) -> None:
    """acpc — dispatch coding agents over ACP."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _read_prompt(prompt_text: str | None, prompt_file: str | None) -> str:
    """Resolve the single prompt source, or fail naming the options."""
    sources = [
        name
        for name, present in (
            ("a prompt argument", prompt_text is not None and prompt_text != "-"),
            ("-", prompt_text == "-"),
            ("--prompt-file", prompt_file is not None),
        )
        if present
    ]
    if len(sources) != 1:
        raise UsageProblem(
            "give exactly one prompt source: a prompt argument, - for stdin, or --prompt-file "
            f"(got {len(sources)})"
        )
    if prompt_text == "-":
        return sys.stdin.read()
    if prompt_file is not None:
        try:
            return Path(prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as error:
            raise UsageProblem(f"--prompt-file {prompt_file}: {error.strerror}") from None
    return prompt_text or ""


def _resolve_permissions(
    explicit: str | None,
    resolution: CallResolution,
    *,
    tty: bool,
) -> str:
    """Apply SPEC's TTY rules to the resolved permission policy."""
    policy = explicit if explicit is not None else resolution.permissions
    if policy is None:
        policy = "prompt" if tty else "read"
    if policy == "prompt" and not tty:
        raise UsageProblem(
            "--permissions prompt needs a TTY (terminal) to ask on and cannot be used with "
            "--bg; "
            "pass --permissions read, write, all or none"
        )
    return policy


def _guard_bypass_mode(mode: str | None, policy: str, resolution: CallResolution) -> None:
    """SPEC *Permissions*: a bypass mode evades the policy unless it is `all`."""
    if mode is None or policy == "all":
        return
    if mode in resolution.entry.bypass_modes:
        raise UsageProblem(
            f"--mode {mode} bypasses permission requests on {resolution.entry.entry}; "
            "it is only accepted with --permissions all"
        )


def _tty_permission_prompt(kind: str, title: str) -> bool:
    """Ask the human on /dev/tty — never on stdin (SPEC *Output contract*)."""
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(f"acpc: allow {kind}? {title} [y/N] ")
            tty.flush()
            return tty.readline().strip().lower() in {"y", "yes"}
    except OSError:
        return False


def _emit_dry_run(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        import json

        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    lines = [
        f"entry        {payload['entry']} ({payload['base_adapter']})",
        f"command      {payload['command']}",
    ]
    for name, item in payload["resolved"].items():
        value = "·" if item["value"] is None else item["value"]
        lines.append(f"{name:<12} {value} ({item['source']})")
    if payload["cwd"]:
        lines.append(f"cwd          {payload['cwd']}")
    if payload["env"]:
        declared = " · ".join(f"{key}={value}" for key, value in payload["env"].items())
        lines.append(f"env          {declared}")
    if payload["env_passthrough"]:
        lines.append(f"passthrough  {' · '.join(payload['env_passthrough'])}")
    click.echo("\n".join(lines))


def _write_stdout(text: str) -> None:
    """Write the answer, turning a closed stdout into SPEC's exit 141."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # Keep the interpreter's own shutdown flush from raising again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(vocab.EXIT_SIGPIPE)


def _display_home(value: str | None) -> str:
    """Render a vendor home in the copy-pastable form used by ``agents``."""
    if value is None:
        return "·"
    expanded = Path(value).expanduser()
    try:
        relative = expanded.relative_to(Path.home())
    except ValueError:
        return value
    return "~" if not relative.parts else str(Path("~") / relative)


def _display_path(value: Path) -> str:
    try:
        relative = value.expanduser().relative_to(Path.home())
    except ValueError:
        return str(value)
    return "~" if not relative.parts else str(Path("~") / relative)


def _source_text(source: FieldSource | None) -> str:
    if source is None:
        return "unset"
    if source.kind == "adapter-default":
        return "adapter default"
    if source.kind == "default":
        return "default"
    if source.kind == "unset":
        return "unset"
    if source.kind == "call":
        return "call"
    return "entry"


def _local_variant_value(entry: ResolvedEntry, field: str) -> str | None:
    """Return a variant field only when that variant directly defines it."""
    source = entry.provenance.get(field)
    if source is None or source.kind != "entry" or source.path is None:
        return None
    if source.path.stem != entry.entry:
        return None
    value = getattr(entry, field)
    if value is None:
        return None
    return _display_home(value) if field == "home" else str(value)


def _agent_row(entry: ResolvedEntry) -> str:
    status = entry.install_status
    if status == "missing":
        status = f"missing → acpc install {entry.entry}"
    return f"{entry.entry:<12} {entry.name:<28} {status}"


def _variant_row(entry: ResolvedEntry) -> str:
    values = {
        field: _local_variant_value(entry, field)
        for field in ("model", "effort", "permissions", "home")
    }
    return (
        f"  {entry.entry:<12} {values['model'] or '·':<20} "
        f"{values['effort'] or '·':<8} {values['permissions'] or '·':<12} "
        f"{values['home'] or '·'}"
    )


def _agent_list_payload(registry: AgentRegistry) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for adapter in registry.adapters:
        rows.append(
            {
                "name": adapter.entry,
                "kind": "adapter",
                "display_name": adapter.name,
                "status": adapter.install_status,
            }
        )
        for variant in registry.variants:
            if variant.base_adapter != adapter.entry:
                continue
            rows.append(
                {
                    "name": variant.entry,
                    "kind": "variant",
                    "base_adapter": variant.base_adapter,
                    "model": _local_variant_value(variant, "model"),
                    "effort": _local_variant_value(variant, "effort"),
                    "permissions": _local_variant_value(variant, "permissions"),
                    "home": _local_variant_value(variant, "home"),
                }
            )
    return {"agents": rows}


def _cache_footer(record: cache.CachedAdvertised | None) -> str:
    if record is None:
        return "-- cached never"
    age = cache.cache_age(record.cached_at)
    return "-- cached now" if age == "now" else f"-- cached {age} ago"


def _commands_footer(
    adapter: str, commands: list[Mapping[str, Any]], record: cache.CachedAdvertised | None
) -> str:
    age = "never" if record is None else cache.cache_age(record.cached_at)
    age_text = "now" if age == "now" else f"{age} ago"
    command_file = _display_path(cache.commands_path(adapter))
    return f"-- {len(commands)} commands (cached {age_text}) | full descriptions: {command_file}"


def _command_name(value: Mapping[str, Any]) -> str:
    name = value.get("name", "")
    return f"/{name.lstrip('/')}" if isinstance(name, str) else "/"


def _mode_name(value: Any) -> str:
    if isinstance(value, Mapping):
        candidate = value.get("id", value.get("name", ""))
    else:
        candidate = value
    return str(candidate)


def _advertised_payload(record: cache.CachedAdvertised | None) -> dict[str, Any]:
    if record is None:
        return {"modes": [], "models": [], "commands": []}
    return record.advertised


def _ensure_cache(entry: ResolvedEntry) -> cache.CachedAdvertised:
    """Return an adapter cache, probing installed adapters on a miss."""
    record = cache.read_advertised(entry.base_adapter)
    if record is not None:
        return record
    if not entry.installed:
        raise cache.ProbeError(
            f"{entry.entry}: '{entry.command_head}' is not installed — "
            f"run 'acpc install {entry.base_adapter}'"
        )
    advertised = asyncio.run(cache.probe_advertised(entry.resolve_call()))
    refreshed = cache.read_advertised(entry.base_adapter)
    return refreshed or cache.CachedAdvertised(advertised=advertised, cached_at=time.time())


def _render_advertised_detail(
    entry: ResolvedEntry, record: cache.CachedAdvertised | None
) -> tuple[str, dict[str, Any]]:
    advertised = _advertised_payload(record)
    modes = [_mode_name(item) for item in advertised.get("modes", [])]
    models = [str(item) for item in advertised.get("models", [])]
    commands = [item for item in advertised.get("commands", []) if isinstance(item, Mapping)]
    visible_modes = modes if len(modes) <= 4 else [*modes[:3], "…"]
    visible_models = models[:3] + (["…"] if len(models) > 3 else [])
    visible_commands = [_command_name(item) for item in commands[:3]]
    if len(commands) > 3:
        visible_commands.append("…")
    lines = [
        f"modes        {len(modes)} · {' · '.join(visible_modes) if visible_modes else '·'}",
        f"models       {len(models)} · {' · '.join(visible_models) if visible_models else '·'}",
        f"commands     {len(commands)} · {' · '.join(visible_commands) if visible_commands else '·'}",
        _cache_footer(record),
    ]
    payload = {
        "advertised": {
            "modes": modes,
            "models": models,
            "commands": [dict(item) for item in commands],
        }
    }
    return "\n".join(lines) + "\n", payload


def _render_entry_detail(
    registry: AgentRegistry, entry: ResolvedEntry
) -> tuple[str, dict[str, Any], str | None]:
    resolution = registry.resolve_call(entry.entry)
    lines: list[str] = []
    if entry.is_variant:
        lines.append(f"extends      {entry.extends}")
    else:
        lines.append(f"adapter      {entry.name} · {entry.install_status} · {entry.command_head}")
    if entry.description is not None:
        lines.append(f"description  {entry.description}")

    resolved_values = {
        "model": resolution.model,
        "effort": resolution.effort,
        "permissions": resolution.permissions,
        "home": resolution.home,
    }
    for field, value in resolved_values.items():
        if field == "home":
            rendered = _display_home(value)
        elif field == "permissions" and value is None:
            rendered = "prompt on TTY, read otherwise"
        else:
            rendered = "·" if value is None else str(value)
        rendered_source = _source_text(resolution.provenance.get(field))
        lines.append(f"{field:<12} {rendered} ({rendered_source})")

    declared = [f"{key}={value}" for key, value in entry.env.items()]
    env_text = " · ".join(declared) if declared else "·"
    env_source = _source_text(entry.provenance.get("env"))
    if entry.env_passthrough:
        env_text += f" ({env_source}) · passthrough: {' · '.join(entry.env_passthrough)}"
    else:
        env_text += f" ({env_source})"
    lines.append(f"env          {env_text}")
    if not entry.is_variant:
        variants = [item.entry for item in registry.variants if item.base_adapter == entry.entry]
        lines.append(f"variants     {' · '.join(variants) if variants else 'none'}")

    payload = {
        "agent": entry.entry,
        "base_adapter": entry.base_adapter,
        "resolved": {
            field: {
                "value": value,
                "source": _source_text(resolution.provenance.get(field)),
            }
            for field, value in resolved_values.items()
        },
        "env": dict(entry.env),
        "env_passthrough": list(entry.env_passthrough),
    }
    if entry.is_variant:
        lines.append(f"-- modes/models/commands: acpc agents {entry.base_adapter}")
        return "\n".join(lines) + "\n", payload, None
    return "\n".join(lines) + "\n", payload, entry.base_adapter


def _render_models(
    entry: ResolvedEntry, record: cache.CachedAdvertised | None
) -> tuple[str, dict[str, Any]]:
    advertised = _advertised_payload(record)
    lines: list[str] = []
    for index, (tier, preset) in enumerate(entry.presets.items()):
        prefix = "          " if index else "presets   "
        lines.append(f"{prefix}{tier:<10} {preset.model:<24} {preset.effort}")
    if not entry.presets:
        lines.append("presets    ·")
    models = [str(item) for item in advertised.get("models", [])]
    lines.append("models    " + ("\n          ".join(models) if models else "·"))
    lines.append(_cache_footer(record))
    return "\n".join(lines) + "\n", {
        "agent": entry.entry,
        "presets": {
            tier: {"model": preset.model, "effort": preset.effort}
            for tier, preset in entry.presets.items()
        },
        "models": models,
    }


def _render_commands(
    entry: ResolvedEntry, record: cache.CachedAdvertised | None
) -> tuple[str, dict[str, Any]]:
    advertised = _advertised_payload(record)
    commands = [item for item in advertised.get("commands", []) if isinstance(item, Mapping)]
    lines = []
    for command in commands:
        description = command.get("description", "")
        text = cache.first_sentence(description) if isinstance(description, str) else ""
        if isinstance(description, str) and text != description:
            text += "…"
        lines.append(f"{_command_name(command):<18} {text}")
    lines.append(_commands_footer(entry.base_adapter, commands, record))
    return "\n".join(lines) + "\n", {
        "agent": entry.entry,
        "commands": [
            {"name": _command_name(item), "description": item.get("description", "")}
            for item in commands
        ],
    }


class _AgentsGroup(click.Group):
    """Treat an unknown first word as the optional agent view name."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            forwarded = list(args)
            for parameter in self.params:
                if not isinstance(parameter, click.Option) or not parameter.is_flag:
                    continue
                if parameter.name is None or not ctx.params.get(parameter.name):
                    continue
                flag = next((option for option in parameter.opts if option.startswith("--")), None)
                if flag is not None:
                    forwarded.append(flag)
            return args[0], _agent_view_command, forwarded
        return super().resolve_command(ctx, args)


def _models_overview(registry: AgentRegistry) -> tuple[str, dict[str, Any], list[str]]:
    lines: list[str] = []
    payload: dict[str, Any] = {"agents": []}
    footer_agents: list[str] = []
    for entry in registry.adapters:
        record = cache.read_advertised(entry.entry)
        advertised = _advertised_payload(record)
        models = [str(item) for item in advertised.get("models", [])]
        lines.append(entry.entry)
        for index, (tier, preset) in enumerate(entry.presets.items()):
            prefix = "  presets " if index == 0 else "          "
            lines.append(f"{prefix}{tier:<10} {preset.model:<24} {preset.effort}")
        lines.append("  models    " + (" · ".join(models) if models else "·"))
        variants = [item for item in registry.variants if item.base_adapter == entry.entry]
        for variant in variants:
            model = _local_variant_value(variant, "model") or "·"
            effort = _local_variant_value(variant, "effort") or "·"
            lines.append(f"  variant   {variant.entry:<12} {model:<24} {effort}")
        payload["agents"].append(
            {
                "name": entry.entry,
                "presets": {
                    tier: {"model": item.model, "effort": item.effort}
                    for tier, item in entry.presets.items()
                },
                "models": models,
                "variants": [
                    {
                        "name": variant.entry,
                        "model": _local_variant_value(variant, "model"),
                        "effort": _local_variant_value(variant, "effort"),
                    }
                    for variant in variants
                ],
            }
        )
        if record is not None:
            footer_agents.append(
                f"{entry.entry} {cache.cache_age(record.cached_at)}"
                if cache.cache_age(record.cached_at) == "now"
                else f"{entry.entry} {cache.cache_age(record.cached_at)} ago"
            )
    lines.append("-- cached: " + " · ".join(footer_agents))
    return "\n".join(lines) + "\n", payload, footer_agents


def _emit_json(payload: Mapping[str, Any]) -> None:
    _write_stdout(json.dumps(dict(payload), ensure_ascii=False) + "\n")


def _run_agents_view(
    name: str | None,
    models: bool,
    commands: bool,
    check_live: bool,
    json_mode: bool,
) -> None:
    """List adapters and variants, or inspect advertised adapter data."""
    if models and commands:
        raise UsageProblem("--models and --commands are mutually exclusive views")
    try:
        registry = AgentRegistry()
        if check_live:
            _agents_check(registry, name, json_mode=json_mode)
        elif models:
            if name is None:
                text, payload, footer = _models_overview(registry)
                if json_mode:
                    _emit_json(payload)
                    click.echo("-- cached: " + " · ".join(footer), err=True)
                else:
                    _write_stdout(text)
            else:
                entry = registry.resolve(name)
                record = _ensure_cache(entry)
                text, payload = _render_models(entry, record)
                if json_mode:
                    _emit_json(payload)
                    click.echo(_cache_footer(record), err=True)
                else:
                    _write_stdout(text)
        elif commands:
            if name is None:
                raise UsageProblem("--commands requires an agent name")
            entry = registry.resolve(name)
            record = _ensure_cache(entry)
            text, payload = _render_commands(entry, record)
            if json_mode:
                _emit_json(payload)
                click.echo(
                    _commands_footer(entry.base_adapter, list(payload["commands"]), record),
                    err=True,
                )
            else:
                _write_stdout(text)
        elif name is None:
            payload = _agent_list_payload(registry)
            if json_mode:
                _emit_json(payload)
            else:
                variants = {
                    adapter.entry: [
                        item for item in registry.variants if item.base_adapter == adapter.entry
                    ]
                    for adapter in registry.adapters
                }
                lines: list[str] = []
                for adapter in registry.adapters:
                    lines.append(_agent_row(adapter))
                    lines.extend(_variant_row(item) for item in variants[adapter.entry])
                _write_stdout("\n".join(lines) + "\n")
        else:
            entry = registry.resolve(name)
            if entry.is_variant:
                text, payload, _ = _render_entry_detail(registry, entry)
                if json_mode:
                    _emit_json(payload)
                else:
                    _write_stdout(text)
            else:
                text, payload, _ = _render_entry_detail(registry, entry)
                record = _ensure_cache(entry)
                advertised_text, advertised_payload = _render_advertised_detail(entry, record)
                payload.update(advertised_payload)
                if json_mode:
                    _emit_json(payload)
                    click.echo(_cache_footer(record), err=True)
                else:
                    _write_stdout(text + advertised_text)
    except cache.ProbeError as error:
        if json_mode:
            _emit_json({"error": str(error)})
            raise SystemExit(vocab.EXIT_AGENT_ERROR) from None
        raise AgentProblem(str(error)) from None
    except RegistryError as error:
        if json_mode:
            _emit_json({"error": str(error)})
            raise SystemExit(vocab.EXIT_USAGE) from None
        raise UsageProblem(str(error)) from None


@main.group(name="agents", cls=_AgentsGroup, invoke_without_command=True)
@click.option("--models", is_flag=True, help="Show full advertised presets and models.")
@click.option("--commands", is_flag=True, help="Show advertised slash commands.")
@click.option("--check", "check_live", is_flag=True, help="Launch and authenticate the adapter.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this view as JSON.")
@click.pass_context
def agents_group(
    ctx: click.Context,
    models: bool,
    commands: bool,
    check_live: bool,
    json_mode: bool,
) -> None:
    """List adapters and variants, or inspect advertised adapter data.

    Example: ``acpc agents mock --models``
    """
    if ctx.invoked_subcommand is None:
        _run_agents_view(None, models, commands, check_live, json_mode)


@click.command(name="agent-view")
@click.argument("name")
@click.option("--models", is_flag=True, help="Show full advertised presets and models.")
@click.option("--commands", is_flag=True, help="Show advertised slash commands.")
@click.option("--check", "check_live", is_flag=True, help="Launch and authenticate the adapter.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this view as JSON.")
def _agent_view_command(
    name: str, models: bool, commands: bool, check_live: bool, json_mode: bool
) -> None:
    """Render one named adapter or variant.

    Example: ``acpc agents mock --commands``
    """
    _run_agents_view(name, models, commands, check_live, json_mode)


def _agents_check(registry: AgentRegistry, name: str | None, *, json_mode: bool) -> None:
    if name is None:
        entries = [entry for entry in registry.adapters if entry.installed]
    else:
        selected = registry.resolve(name)
        entries = [registry.resolve(selected.base_adapter)]
    results: list[dict[str, Any]] = []
    for entry in entries:
        try:
            advertised = asyncio.run(cache.probe_advertised(registry.resolve_call(entry.entry)))
            result = {"agent": entry.entry, "ok": True, "models": len(advertised["models"])}
        except cache.ProbeError as error:
            result = {"agent": entry.entry, "ok": False, "error": str(error)}
        results.append(result)
    if json_mode:
        _emit_json({"checks": results})
    else:
        for result in results:
            if result["ok"]:
                _write_stdout(f"{result['agent']} ok\n")
            else:
                _write_stdout(f"{result['agent']} failed: {result['error']}\n")
    if any(not result["ok"] for result in results):
        raise SystemExit(vocab.EXIT_AGENT_ERROR)


@agents_group.command(name="init")
@click.argument("name")
@click.option("--extends", "parent", required=True, metavar="AGENT")
@click.option("--model", metavar="M")
@click.option("--effort", metavar="E")
@click.option("--permissions", type=click.Choice(vocab.PERMISSION_VALUES), metavar="P")
@click.option("--home", metavar="DIR")
@click.option("--json", "json_mode", is_flag=True, help="Emit the created entry as JSON.")
@click.help_option("-h", "--help")
def agents_init_command(
    name: str,
    parent: str,
    model: str | None,
    effort: str | None,
    permissions: str | None,
    home: str | None,
    json_mode: bool,
) -> None:
    """Scaffold a variant entry.

    Example: ``acpc agents init work --extends mock --permissions write``
    """
    try:
        registry = AgentRegistry()
        registry.resolve(parent)
        if effort is not None:
            registry.resolve_call(parent, effort=effort)
    except RegistryError as error:
        if json_mode:
            _emit_json({"error": str(error)})
            raise SystemExit(vocab.EXIT_USAGE) from None
        raise UsageProblem(str(error)) from None
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise UsageProblem(f"invalid agent name '{name}'")
    target = paths.agents_dir() / f"{name}.toml"
    if target.exists():
        raise UsageProblem(f"agent entry already exists: {target}")
    fields = [
        ("extends", parent),
        ("model", model),
        ("effort", effort),
        ("permissions", permissions),
        ("home", home),
    ]
    contents = (
        "\n".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}"
            for key, value in fields
            if value is not None
        )
        + "\n"
    )
    try:
        paths.ensure_private_dir(paths.agents_dir())
        paths.atomic_write(target, contents)
    except OSError as error:
        raise UsageProblem(f"cannot write {target}: {error}") from None
    payload = {"name": name, "extends": parent, "path": str(target)}
    if json_mode:
        _emit_json(payload)
    else:
        _write_stdout(f"created {target}\n")


@main.command(name="install", cls=_RootHelpCommand)
@click.argument("agent")
@click.option("--json", "json_mode", is_flag=True, help="Emit the install result as JSON.")
@click.help_option("-h", "--help")
def install_command(agent: str, json_mode: bool) -> None:
    """Run an adapter definition's install command."""
    try:
        registry = AgentRegistry()
        registry.resolve(agent)

        def run_installer(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                args,
                cwd=kwargs.get("cwd"),
                env=kwargs.get("env"),
                capture_output=True,
                text=True,
                check=False,
            )

        result = registry.execute_install(agent, runner=run_installer)
    except RegistryError as error:
        if json_mode:
            _emit_json({"agent": agent, "ok": False, "error": str(error)})
            raise SystemExit(vocab.EXIT_USAGE) from None
        raise UsageProblem(str(error)) from None
    except OSError as error:
        message = f"install {agent} failed: {error}"
        if json_mode:
            _emit_json({"agent": agent, "ok": False, "error": message})
            raise SystemExit(vocab.EXIT_AGENT_ERROR) from None
        raise AgentProblem(message) from None

    for stream in (getattr(result, "stdout", None), getattr(result, "stderr", None)):
        if stream:
            click.echo(stream.rstrip("\n"), err=True)
    return_code = getattr(result, "returncode", 1)
    payload = {"agent": agent, "ok": return_code == 0, "returncode": return_code}
    if json_mode:
        _emit_json(payload)
    elif return_code == 0:
        _write_stdout(f"installed {agent}\n")
    else:
        raise AgentProblem(f"install {agent} failed (exit {return_code})")
    if return_code != 0:
        raise SystemExit(vocab.EXIT_AGENT_ERROR)


async def _cancel_with_daemon(target: str, session_id: str) -> bool | None:
    """Request cancellation without allowing a dead daemon to hang ``stop``."""
    try:
        return await asyncio.wait_for(
            daemon_client.cancel_turn(target, session_id),
            timeout=runner.CANCEL_ACK_TIMEOUT,
        )
    except TimeoutError:
        return None
    except Exception:  # noqa: BLE001
        return False


def _wait_for_stop(session_id: str) -> sessions.SessionMeta:
    """Give a daemon's cancellation time to finalize the session on disk."""
    deadline = time.monotonic() + runner.CANCEL_ACK_TIMEOUT
    while True:
        meta = sessions.load(session_id)
        if not meta.is_active or time.monotonic() >= deadline:
            return meta
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _maintenance_json(payload: Mapping[str, Any]) -> None:
    _write_stdout(json.dumps(dict(payload), ensure_ascii=False) + "\n")


@main.command(name="stop", cls=_RootHelpCommand)
@click.argument("selector")
@click.option("--json", "json_mode", is_flag=True, help="Emit the result as JSON.")
@click.help_option("-h", "--help")
def stop_command(selector: str, json_mode: bool) -> None:
    """Cancel an active session, or do nothing when it is already finished."""
    meta = _load_view_session(selector)
    if meta.is_active:
        cancelled = False
        if meta.target is not None:
            cancelled = asyncio.run(_cancel_with_daemon(meta.target, meta.session_id))
        if cancelled is True:
            meta = _wait_for_stop(meta.session_id)
        elif cancelled is None:
            meta = sessions.load(meta.session_id)
        elif meta.pid is None:
            meta = sessions.transition(
                meta.session_id,
                "cancelled",
                exit_code=vocab.EXIT_CANCELLED,
                stop_reason="stopped by user",
            )
        else:
            result = proc.kill_process_tree(meta.pid, meta.process_start_time)
            if result == "refused":
                raise UsageProblem(
                    f"could not stop session {meta.session_id}: refused to signal it"
                )
            meta = _wait_for_stop(meta.session_id)

    payload = {
        "session_id": meta.session_id,
        "state": meta.state,
        "stop_reason": meta.stop_reason,
    }
    if json_mode:
        _maintenance_json(payload)
    else:
        _write_stdout(f"{meta.session_id} {meta.state}\n")
    click.echo(f"-- stop {meta.session_id} · {meta.state}", err=True)


@main.command(name="rm", cls=_RootHelpCommand)
@click.argument("selector")
@click.option("--json", "json_mode", is_flag=True, help="Emit the result as JSON.")
@click.help_option("-h", "--help")
def rm_command(selector: str, json_mode: bool) -> None:
    """Delete a finished session's on-disk state."""
    meta = _load_view_session(selector)
    advertised_paths = sessions.session_paths(meta.session_id)
    try:
        sessions.delete_session(meta.session_id)
    except sessions.SessionStateError as error:
        raise UsageProblem(str(error)) from None
    payload = {"session_id": meta.session_id, "removed": True, "paths": advertised_paths}
    if json_mode:
        _maintenance_json(payload)
    else:
        _write_stdout(f"removed {meta.session_id}\n")
    click.echo(f"-- removed session {meta.session_id}", err=True)


@main.command(name="prune")
@click.option("--older-than", default=None, metavar="D", help="Age threshold, such as 7d.")
@click.option("--dry-run", is_flag=True, help="List candidates without deleting them.")
@click.option("--json", "json_mode", is_flag=True, help="Emit the result as JSON.")
@click.help_option("-h", "--help")
def prune_command(older_than: str | None, dry_run: bool, json_mode: bool) -> None:
    """Delete finished sessions older than the configured retention period.

    Example: ``acpc prune --older-than 7d --dry-run``
    """
    try:
        settings = config.load_config()
        duration = config.parse_duration(older_than or settings.retention)
        candidates = sessions.prune_sessions(older_than=duration, dry_run=dry_run)
    except (config.ConfigError, ValueError, sessions.SessionError) as error:
        raise UsageProblem(str(error)) from None

    session_ids = [meta.session_id for meta in candidates]
    payload = {"sessions": session_ids, "dry_run": dry_run}
    if json_mode:
        _maintenance_json(payload)
    elif session_ids:
        _write_stdout("\n".join(session_ids) + "\n")
    click.echo(
        f"-- prune {'would remove' if dry_run else 'removed'} {len(session_ids)} session(s)",
        err=True,
    )


def _load_view_session(selector: str) -> sessions.SessionMeta:
    """Verify liveness before a targeted view reports a session."""
    try:
        session_id = sessions.resolve_selector(selector, allow_last=_stdout_is_tty())
        return sessions.load(session_id)
    except sessions.SessionError as error:
        raise UsageProblem(str(error)) from None


_LOG_DEFAULT_TAIL = 20
_LOG_WAIT_POLL_INTERVAL = 0.05


def _wait_for_new_events(
    transcript_file: transcript.Transcript,
    *,
    since: int,
    tail: int | None,
    timeout: float | None,
) -> transcript.TranscriptPage | None:
    """Wait for transcript activity at the module's fixed polling interval."""
    deadline = None if timeout is None else time.monotonic() + timeout

    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(_LOG_WAIT_POLL_INTERVAL, remaining))
        else:
            time.sleep(_LOG_WAIT_POLL_INTERVAL)

        available = _read_transcript_page(transcript_file, since=since)
        if available.events:
            return _read_transcript_page(transcript_file, since=since, tail=tail)


def _read_transcript_page(
    transcript_file: transcript.Transcript,
    *,
    since: int = 0,
    tail: int | None = None,
) -> transcript.TranscriptPage:
    """Turn damaged transcript state into the CLI's one-line usage error."""
    try:
        return transcript_file.read(since=since, tail=tail)
    except transcript.TranscriptError as error:
        raise UsageProblem(str(error)) from None


@main.command(name="status")
@click.argument("selector", required=False)
@click.option("--all", "all_sessions", is_flag=True, help="Show every session.")
@click.option("--json", "json_mode", is_flag=True, help="Emit a JSON status object.")
@click.help_option("-h", "--help")
def status_command(selector: str | None, all_sessions: bool, json_mode: bool) -> None:
    """Show liveness-verified session metadata without reading transcripts.

    Example: ``acpc status <session-id> --json``
    """
    if selector is not None and all_sessions:
        raise UsageProblem("--all cannot be used with a session id")

    if selector is not None:
        meta = _load_view_session(selector)
        if json_mode:
            _write_stdout(json.dumps(render.status_detail_json(meta), ensure_ascii=False) + "\n")
        else:
            _write_stdout(render.render_status_detail(meta))
        return

    metas = sessions.list_sessions()
    if json_mode:
        _write_stdout(
            json.dumps(
                render.status_list_json(metas, all_sessions=all_sessions), ensure_ascii=False
            )
            + "\n"
        )
    else:
        _write_stdout(render.render_status_list(metas, all_sessions=all_sessions))


@main.command(name="log")
@click.argument("selector")
@click.option("--since", type=click.IntRange(min=0), default=None, metavar="N")
@click.option("--tail", type=click.IntRange(min=0), default=None, metavar="N")
@click.option("--prose", is_flag=True, help="Render full agent messages.")
@click.option("--json", "json_mode", is_flag=True, help="Emit raw transcript events as NDJSON.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=render.DEFAULT_LOG_MAX_OUTPUT,
    show_default=True,
    metavar="BYTES",
)
@click.option("--wait-new", is_flag=True, help="Wait for new transcript events.")
@click.option("--timeout", type=click.FloatRange(min=0), default=None, metavar="S")
@click.option("--quiet", is_flag=True, help="Suppress the stderr footer.")
@click.help_option("-h", "--help")
def log_command(
    selector: str,
    since: int | None,
    tail: int | None,
    prose: bool,
    json_mode: bool,
    max_output: int,
    wait_new: bool,
    timeout: float | None,
    quiet: bool,
) -> None:
    """Render selected transcript events and keep metadata on stderr.

    Example: ``acpc log <session-id> --prose --since 0``
    """
    if prose and json_mode:
        raise UsageProblem("--prose and --json are mutually exclusive views")
    if timeout is not None and not wait_new:
        raise UsageProblem("--timeout requires --wait-new")

    meta = _load_view_session(selector)
    try:
        transcript_file = transcript.Transcript(sessions.transcript_path(meta.session_id))
    except transcript.TranscriptError as error:
        raise UsageProblem(str(error)) from None
    explicit_since = since is not None
    cursor = 0 if since is None else since
    selection_tail = tail
    if selection_tail is None and not explicit_since:
        selection_tail = _LOG_DEFAULT_TAIL

    if wait_new and not explicit_since:
        cursor = _read_transcript_page(transcript_file).next_cursor
    page = _read_transcript_page(transcript_file, since=cursor, tail=selection_tail)
    if wait_new and not page.events:
        page = _wait_for_new_events(
            transcript_file,
            since=cursor,
            tail=selection_tail,
            timeout=timeout,
        )
        if page is None:
            raise SystemExit(vocab.EXIT_TIMEOUT)

    full_last_message = meta.state in {"failed", "timeout", "orphaned"}
    rendered = render.render_events(
        page.events,
        prose=prose,
        json_mode=json_mode,
        max_output=max_output,
        transcript_path=sessions.transcript_path(meta.session_id),
        cursor=cursor,
        full_last_message=full_last_message,
    )
    _write_stdout(rendered.text)
    if not quiet:
        # The cursor is a global transcript index, not an event count.  Read
        # the actual end so an empty page after a large --since stays honest.
        event_count = _read_transcript_page(transcript_file).next_cursor
        footer = render.format_log_footer(
            meta,
            cursor=rendered.next_cursor,
            event_count=event_count,
        )
        click.echo(footer, err=True)


@main.command(name="run")
@click.argument("agent")
@click.argument("prompt_text", required=False)
@click.option("--prompt-file", "prompt_file", metavar="FILE", help="Read the prompt from a file.")
@click.option("--cwd", metavar="DIR", help="Working directory of the callee.")
@click.option("--model", metavar="M", help="Model tier (fast/standard/max) or a raw model ID.")
@click.option("--effort", metavar="E", help="Reasoning effort level.")
@click.option(
    "--permissions",
    type=click.Choice(vocab.PERMISSION_VALUES),
    help="Approval policy for ACP permission requests.",
)
@click.option("--mode", metavar="M", help="Callee's operating mode (ACP session/set_mode).")
@click.option("--home", metavar="DIR", help="Vendor home override.")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option("--timeout", type=float, metavar="S", help="Cancel the session after S seconds.")
@click.option("--name", "alias", metavar="ALIAS", help="Human-typeable handle for this session.")
@click.option("--dry-run", is_flag=True, help="Print what this call resolves to, then exit.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    show_default=True,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--bg", "background", is_flag=True, help="Dispatch and return the session id.")
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
@click.help_option("-h", "--help")
def run_command(
    agent: str,
    prompt_text: str | None,
    prompt_file: str | None,
    cwd: str | None,
    model: str | None,
    effort: str | None,
    permissions: str | None,
    mode: str | None,
    home: str | None,
    output_file: str | None,
    timeout: float | None,
    alias: str | None,
    dry_run: bool,
    background: bool,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Dispatch one agent; block and print the final answer.

    The default permission policy is ``prompt`` on a TTY and ``read``
    otherwise.  Background calls always use the non-TTY rule.

    Example: ``acpc run codex "Fix the failing test" --permissions write``
    """
    tty = _stdout_is_tty()

    try:
        registry = AgentRegistry()
        resolution = registry.resolve_call(
            agent, model=model, effort=effort, permissions=permissions, home=home
        )
    except RegistryError as error:
        raise UsageProblem(str(error)) from None

    # A background client has already gone away when a permission request
    # arrives, so it follows the non-TTY rule even when stdout is a terminal.
    policy = _resolve_permissions(permissions, resolution, tty=tty and not background)
    _guard_bypass_mode(mode, policy, resolution)
    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else None

    if dry_run:
        payload = runner.resolution_payload(resolution, cwd=resolved_cwd)
        payload["resolved"]["permissions"]["value"] = policy
        _emit_dry_run(payload, json_mode=json_mode)
        return

    prompt = _read_prompt(prompt_text, prompt_file)

    try:
        command_head = runner.adapter_command(resolution)[0]
    except runner.RunnerError as error:
        raise AgentProblem(str(error)) from None
    del command_head

    if alias is not None:
        try:
            warning = sessions.claim_name(alias)
        except sessions.SessionNameError as error:
            raise UsageProblem(str(error)) from None
        if warning:
            click.echo(f"-- {warning}", err=True)

    settings = config.load_config()
    runner.auto_prune(settings.retention_seconds)

    meta = sessions.create_session(
        entry=resolution.entry.entry,
        base_adapter=resolution.entry.base_adapter,
        prompt=prompt,
        resolution=runner.session_resolution(resolution, cwd=resolved_cwd),
        target=runner.call_target(resolution),
        name=alias,
    )

    request = runner.TurnRequest(
        resolution=resolution,
        prompt=prompt,
        cwd=resolved_cwd,
        mode=mode,
        timeout=timeout,
        permission_prompt=_tty_permission_prompt if policy == "prompt" else None,
    )
    # The policy the TTY rules produced is what the client must enforce.
    request = _with_policy(request, policy)

    if background:
        _dispatch_background(meta.session_id, request, json_mode=json_mode)
        return

    try:
        outcome = runner.execute_turn(meta.session_id, request)
    except runner.RunnerError as error:
        raise AgentProblem(str(error)) from None

    final = sessions.read_meta(meta.session_id)
    if output_file is not None:
        output.write_output_file(output_file, outcome.answer)

    result = output.render_result(
        final,
        outcome.answer,
        json_mode=json_mode,
        output_file=output_file,
        max_output=max_output,
    )
    _write_stdout(result.text)

    if outcome.state == "detached":
        # SPEC *Output contract*: the session outlives this client, so the way
        # out has to say which session the caller can still reach.
        click.echo(f"-- {meta.session_id} detached · still running", err=True)
        raise SystemExit(outcome.exit_code)

    if not quiet:
        output.emit_summary(final, route_note=_route_note(outcome))

    raise SystemExit(outcome.exit_code)


def _route_note(outcome: runner.TurnOutcome) -> str | None:
    """Fold the routing and queueing notes into the one `--` summary line.

    SPEC counts `--` lines: a queued turn and a direct-child fallback are both
    notes about how the call was served, so they ride the same line.
    """
    notes = [note for note in (outcome.route_note, _queue_note(outcome)) if note]
    return " · ".join(notes) if notes else None


def _queue_note(outcome: runner.TurnOutcome) -> str | None:
    return "queued for a daemon slot" if outcome.queued else None


def _dispatch_background(session_id: str, request: runner.TurnRequest, *, json_mode: bool) -> None:
    """Hand the turn to the daemon and print what the caller needs to find it.

    SPEC `run --bg`: stdout is exactly the session id and its directory, so a
    shell caller can read both without parsing prose.
    """
    import asyncio

    problem = asyncio.run(runner.dispatch_background(session_id, request))
    if problem is not None:
        raise AgentProblem(problem)
    if json_mode:
        import json

        payload = {"session_id": session_id, "paths": sessions.session_paths(session_id)}
        _write_stdout(json.dumps(payload, ensure_ascii=False) + "\n")
        return
    _write_stdout(f"{session_id}\n{sessions.session_dir(session_id)}\n")


def _with_policy(request: runner.TurnRequest, policy: str) -> runner.TurnRequest:
    """Return the request with the TTY-resolved permission policy applied."""
    from dataclasses import replace

    from acpc.registry import CallResolution as _CallResolution

    resolution: _CallResolution = replace(request.resolution, permissions=policy)
    return replace(request, resolution=resolution)


@main.command(name="continue")
@click.argument("selector")
@click.argument("prompt_text", required=False)
@click.option("--prompt-file", "prompt_file", metavar="FILE", help="Read the prompt from a file.")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option("--bg", "background", is_flag=True, help="Dispatch and return the session id.")
@click.option("--timeout", type=float, metavar="S", help="Cancel the session after S seconds.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    show_default=True,
    metavar="BYTES",
    help="Cap on stdout bytes; 0 disables the cap.",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
@click.option("--permissions", metavar="P", hidden=True)
@click.option("--model", metavar="M", hidden=True)
@click.option("--effort", metavar="E", hidden=True)
@click.option("--mode", metavar="M", hidden=True)
@click.option("--cwd", metavar="DIR", hidden=True)
@click.option("--home", metavar="DIR", hidden=True)
@click.option("--name", "alias", metavar="ALIAS", hidden=True)
@click.option("--dry-run", is_flag=True, hidden=True)
@click.help_option("-h", "--help")
def continue_command(
    selector: str,
    prompt_text: str | None,
    prompt_file: str | None,
    output_file: str | None,
    background: bool,
    timeout: float | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
    permissions: str | None,
    model: str | None,
    effort: str | None,
    mode: str | None,
    cwd: str | None,
    home: str | None,
    alias: str | None,
    dry_run: bool,
) -> None:
    """Continue a finished session using its stored adapter resolution.

    Example: ``acpc continue <session-id> "Run the tests again"``
    """
    run_only = {
        "--permissions": permissions,
        "--model": model,
        "--effort": effort,
        "--mode": mode,
        "--cwd": cwd,
        "--home": home,
        "--name": alias,
        "--dry-run": dry_run,
    }
    for flag, value in run_only.items():
        if value not in (None, False):
            rule = "continue reuses the session's permissions — drop --permissions"
            raise UsageProblem(f"{rule} (run-only flag: {flag})")

    prompt = _read_prompt(prompt_text, prompt_file)
    meta = _load_view_session(selector)
    if meta.is_active:
        raise UsageProblem(
            f"session {meta.session_id} is {meta.state} — wait for the current turn to finish"
        )
    stored_policy = meta.resolution.get("resolved", {}).get("permissions", {}).get("value")
    continue_tty = _stdout_is_tty() and not background
    if stored_policy == "prompt" and not continue_tty:
        raise UsageProblem(
            "this session uses --permissions prompt, which needs a TTY and cannot be used "
            "with --bg; continue it from a terminal or start a read/write session"
        )
    try:
        request = runner.continue_request(
            meta,
            prompt,
            timeout=timeout,
            permission_prompt=(
                _tty_permission_prompt
                if meta.resolution.get("resolved", {}).get("permissions", {}).get("value")
                == "prompt"
                and _stdout_is_tty()
                and continue_tty
                else None
            ),
        )
        rotated = sessions.rotate_turn(meta.session_id)
        sessions.write_prompt(rotated.session_id, prompt)
    except (runner.RunnerError, sessions.SessionError) as error:
        raise UsageProblem(str(error)) from None

    if background:
        _dispatch_background(meta.session_id, request, json_mode=json_mode)
        return

    try:
        outcome = runner.execute_turn(meta.session_id, request)
    except runner.RunnerError as error:
        raise AgentProblem(str(error)) from None

    final = sessions.read_meta(meta.session_id)
    if output_file is not None:
        output.write_output_file(output_file, outcome.answer)

    result = output.render_result(
        final,
        outcome.answer,
        json_mode=json_mode,
        output_file=output_file,
        max_output=max_output,
    )
    _write_stdout(result.text)
    if outcome.state == "detached":
        click.echo(f"-- {meta.session_id} detached · still running", err=True)
        raise SystemExit(outcome.exit_code)
    if not quiet:
        output.emit_summary(final, route_note=_route_note(outcome))
    raise SystemExit(outcome.exit_code)


@main.command(name="wait")
@click.argument("selector")
@click.option("--timeout", type=click.FloatRange(min=0), default=None, metavar="S")
@click.option("-o", "--output", "output_file", metavar="FILE", help="Write the answer to a file.")
@click.option(
    "--max-output",
    type=click.IntRange(min=0),
    default=output.DEFAULT_MAX_OUTPUT,
    show_default=True,
    metavar="BYTES",
)
@click.option("--quiet", is_flag=True, help="Suppress the stderr summary line.")
@click.option("--json", "json_mode", is_flag=True, help="Emit this command's output as JSON.")
@click.help_option("-h", "--help")
def wait_command(
    selector: str,
    timeout: float | None,
    output_file: str | None,
    max_output: int,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Block until a background session finishes, then print its answer.

    Example: ``acpc wait <session-id> --timeout 120``
    """
    meta = _load_view_session(selector)
    state = runner.wait_for_session(meta.session_id, timeout=timeout)
    if state is None:
        # SPEC `wait`: the timeout stops waiting only — the session runs on.
        raise SystemExit(vocab.EXIT_TIMEOUT)

    final = sessions.read_meta(meta.session_id)
    answer = _answer_text(meta.session_id)
    if output_file is not None:
        output.write_output_file(output_file, answer)

    result = output.render_result(
        final,
        answer,
        json_mode=json_mode,
        output_file=output_file,
        max_output=max_output,
    )
    _write_stdout(result.text)
    if not quiet:
        output.emit_summary(final)
    raise SystemExit(runner.exit_code_for(final.state, final.stop_reason))


def _answer_text(session_id: str) -> str:
    try:
        return sessions.answer_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return ""


@main.group(name="daemon", invoke_without_command=False)
@click.help_option("-h", "--help")
def daemon_group() -> None:
    """Inspect and stop the per-target daemons.

    Example: ``acpc daemon status``
    """


@daemon_group.command(name="status")
@click.argument("agent", required=False)
@click.option("--json", "json_mode", is_flag=True, help="Emit the status as JSON.")
@click.help_option("-h", "--help")
def daemon_status_command(agent: str | None, json_mode: bool) -> None:
    """Report each live daemon with its pid, uptime and log path.

    Example: ``acpc daemon status --json``
    """
    import asyncio

    entries = asyncio.run(_collect_daemon_status(agent))
    if json_mode:
        import json

        _write_stdout(json.dumps({"daemons": entries}, ensure_ascii=False) + "\n")
        return
    if not entries:
        click.echo("-- no daemons running", err=True)
        return
    lines = [
        f"{item['target']:<28} pid {item['pid']:<8} up {output.format_duration(item['uptime'])} "
        f"· {len(item['sessions'])} sessions · {item['log']}"
        for item in entries
    ]
    _write_stdout("\n".join(lines) + "\n")


async def _collect_daemon_status(agent: str | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for target in runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets():
        daemon = await daemon_client.connect(target)
        if daemon is None:
            continue
        try:
            reply = await daemon.status()
        finally:
            await daemon.close()
        if reply.get("ok"):
            entries.append({key: value for key, value in reply.items() if key != "ok"})
    return entries


@daemon_group.command(name="stop")
@click.argument("agent", required=False)
@click.help_option("-h", "--help")
def daemon_stop_command(agent: str | None) -> None:
    """Stop daemons; their sessions are failed with a reason, never orphaned.

    Example: ``acpc daemon stop mock``
    """
    import asyncio

    stopped = asyncio.run(_stop_daemons(agent))
    click.echo(f"-- stopped {stopped} daemon(s)", err=True)


async def _stop_daemons(agent: str | None) -> int:
    stopped = 0
    for target in runner.daemon_targets_for(agent) if agent else runner.all_daemon_targets():
        daemon = await daemon_client.connect(target)
        if daemon is None:
            continue
        try:
            reply = await daemon.stop()
        finally:
            await daemon.close()
        if reply.get("ok"):
            stopped += 1
    return stopped
