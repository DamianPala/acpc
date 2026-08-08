"""Agent adapter and variant registry.

Registry files are trusted TOML configuration.  Shipped adapter definitions
live in the package and user files under ``ACPC_HOME/agents`` overlay or
extend them.  Resolution is immutable and records the source file for every
value that survived inheritance, so later CLI views can explain a call
without reimplementing the merge rules.
"""

import copy
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Final, Literal

from acpc.environment import adapter_environment
from acpc.paths import agents_dir
from acpc.vocab import EFFORT_VALUES, PERMISSION_ALIASES, PERMISSION_VALUES, normalize_permission

_TIERS: Final = frozenset({"fast", "standard", "max"})
_NON_INHERITABLE_FIELDS: Final = frozenset({"description"})
_ENTRY_KEYS: Final = frozenset(
    {
        "name",
        "author",
        "command",
        "install_command",
        "home",
        "home_env",
        "bypass_modes",
        "efforts",
        "effort_config_id",
        "env_passthrough",
        "extends",
        "description",
        "model",
        "effort",
        "mode",
        "permissions",
        "env",
        "presets",
    }
)
_PRESET_KEYS: Final = frozenset({"model", "effort"})
_FIELD_NAMES: Final = (
    "name",
    "author",
    "command",
    "install_command",
    "home",
    "home_env",
    "bypass_modes",
    "efforts",
    "effort_config_id",
    "env_passthrough",
    "description",
    "model",
    "effort",
    "mode",
    "permissions",
    "env",
    "presets",
)


class RegistryError(ValueError):
    """A registry entry cannot be parsed or resolved."""


@dataclass(frozen=True, slots=True)
class FieldSource:
    """Where a resolved field value came from."""

    kind: Literal["entry", "adapter-default", "call", "default", "unset"]
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class Preset:
    """An adapter-level model tier.

    ``effort`` is optional: a model whose vendor exposes no effort setting is
    pinned by model alone, and the model then runs at its own built-in level.
    """

    model: str
    effort: str | None
    source: Path


@dataclass(frozen=True, slots=True)
class CallResolution:
    """One concrete call after entry and command-line values are resolved."""

    entry: "ResolvedEntry"
    model: str | None
    effort: str | None
    mode: str | None
    permissions: str | None
    home: str | None
    declared_env: Mapping[str, str]
    env_passthrough: tuple[str, ...]
    provenance: Mapping[str, FieldSource]

    @property
    def adapter_environment(self) -> dict[str, str]:
        """Build the exact allowlisted environment delivered to an adapter."""
        declared = dict(self.declared_env)
        if self.home is not None and self.entry.home_env is not None:
            declared[self.entry.home_env] = str(Path(self.home).expanduser())
        return adapter_environment(declared, self.env_passthrough)

    @property
    def command(self) -> tuple[str, ...]:
        return self.entry.command_args


@dataclass(frozen=True, slots=True)
class ResolvedEntry:
    """An adapter or variant with all inherited fields filled in."""

    entry: str
    base_adapter: str
    name: str
    author: str | None
    command: str
    install_command: str | None
    home: str | None
    home_env: str | None
    bypass_modes: tuple[str, ...]
    efforts: tuple[str, ...]
    # The session config option id that carries effort — a vendor fact like
    # home_env (codex speaks `reasoning_effort`, claude speaks `effort`).
    effort_config_id: str | None
    env_passthrough: tuple[str, ...]
    description: str | None
    model: str | None
    effort: str | None
    mode: str | None
    permissions: str | None
    env: Mapping[str, str]
    presets: Mapping[str, Preset]
    extends: str | None
    provenance: Mapping[str, FieldSource]

    @property
    def is_variant(self) -> bool:
        return self.extends is not None

    @property
    def is_adapter(self) -> bool:
        return not self.is_variant

    @property
    def preset_models(self) -> tuple[str, ...]:
        """Model IDs mentioned by the entry's preset table."""
        return tuple(dict.fromkeys(preset.model for preset in self.presets.values()))

    @property
    def command_args(self) -> tuple[str, ...]:
        try:
            args = tuple(shlex.split(self.command))
        except ValueError as exc:
            raise RegistryError(
                f"agent '{self.entry}': invalid command quoting in "
                f"{self.source_for('command')}: {exc}"
            ) from None
        if not args:
            raise RegistryError(f"agent '{self.entry}': command must not be empty")
        return args

    @property
    def command_head(self) -> str:
        return self.command_args[0]

    @property
    def installed(self) -> bool:
        """Whether the executable at the head of ``command`` is on PATH."""
        return shutil.which(self.command_head) is not None

    @property
    def install_status(self) -> str:
        return "installed" if self.installed else "missing"

    @property
    def source(self) -> Path:
        source = self.source_for("command").path
        if source is None:
            raise RegistryError(f"agent '{self.entry}' has no file source for command")
        return source

    def source_for(self, field: str) -> FieldSource:
        return self.provenance[field]

    def resolve_call(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        mode: str | None = None,
        permissions: str | None = None,
        home: str | None = None,
    ) -> CallResolution:
        """Resolve optional run flags and validate the resulting combination."""
        resolved_model: str | None
        resolved_effort: str | None
        sources: dict[str, FieldSource] = {}

        if model is not None and model in _TIERS:
            preset = self.presets.get(model)
            if preset is None:
                if not self.presets:
                    raise RegistryError(
                        f"{self.entry}: --model {model} requires a [presets] table; "
                        "this adapter defines no presets (use a raw model ID or add [presets])"
                    )
                supported = ", ".join(sorted(self.presets))
                raise RegistryError(
                    f"{self.entry}: unknown model tier '{model}'; supported tiers: {supported}"
                )
            resolved_model = preset.model
            resolved_effort = preset.effort
            sources["model"] = self.provenance[f"presets.{model}.model"]
            if resolved_effort is not None:
                sources["effort"] = self.provenance[f"presets.{model}.effort"]
        elif model is not None:
            resolved_model = model
            resolved_effort = self.effort
            sources["model"] = FieldSource("call")
            if resolved_effort is not None:
                sources["effort"] = self.source_for("effort")
        else:
            # No --model: the entry's own value wins, and where it is silent the
            # adapter's `standard` preset *is* the adapter default. SPEC's
            # `agents claude` view shows exactly that pairing —
            # `model claude-sonnet-5 (adapter default)` with claude's standard
            # preset being claude-sonnet-5. Model and effort fall back
            # independently: --effort is orthogonal to --model.
            default = self.presets.get("standard")
            resolved_model = self.model
            resolved_effort = self.effort
            if resolved_model is not None:
                sources["model"] = self.source_for("model")
            elif default is not None:
                resolved_model = default.model
                sources["model"] = FieldSource("adapter-default", default.source)
            if resolved_effort is not None:
                sources["effort"] = self.source_for("effort")
            elif default is not None and default.effort is not None:
                resolved_effort = default.effort
                sources["effort"] = FieldSource("adapter-default", default.source)

        if effort is not None:
            resolved_effort = effort
            sources["effort"] = FieldSource("call")
        elif resolved_effort is None:
            sources["effort"] = FieldSource("unset")

        if resolved_model is None:
            sources["model"] = FieldSource("unset")

        if resolved_effort is not None:
            self._validate_effort(resolved_effort)

        resolved_mode = self.mode if mode is None else mode
        if resolved_mode is not None:
            sources["mode"] = self.source_for("mode") if mode is None else FieldSource("call")
        else:
            sources["mode"] = FieldSource("unset")

        resolved_permissions = normalize_permission(
            self.permissions if permissions is None else permissions
        )
        if resolved_permissions is not None and resolved_permissions not in PERMISSION_VALUES:
            supported = ", ".join(PERMISSION_VALUES)
            raise RegistryError(
                f"{self.entry}: unsupported permissions '{resolved_permissions}'; "
                f"supported values: {supported}"
            )
        if resolved_permissions is not None:
            sources["permissions"] = (
                self.source_for("permissions") if permissions is None else FieldSource("call")
            )
        else:
            sources["permissions"] = FieldSource("unset")

        resolved_home = self.home if home is None else home
        if resolved_home is not None:
            sources["home"] = self.source_for("home") if home is None else FieldSource("call")
        else:
            sources["home"] = FieldSource("unset")
        sources["env"] = self.source_for("env")

        return CallResolution(
            entry=self,
            model=resolved_model,
            effort=resolved_effort,
            mode=resolved_mode,
            permissions=resolved_permissions,
            home=resolved_home,
            declared_env=dict(self.env),
            env_passthrough=self.env_passthrough,
            provenance=sources,
        )

    def _validate_effort(self, value: str) -> None:
        if value not in EFFORT_VALUES:
            supported = ", ".join(self.efforts) if self.efforts else ", ".join(EFFORT_VALUES)
            raise RegistryError(
                f"{self.entry}: unsupported effort '{value}'; supported levels: {supported}"
            )
        # An empty list means the adapter has no verified advertisement yet,
        # rather than that it supports no efforts at all.
        if self.efforts and value not in self.efforts:
            supported = ", ".join(self.efforts)
            raise RegistryError(
                f"{self.entry}: effort '{value}' is not supported; supported levels: {supported}"
            )


@dataclass(frozen=True, slots=True)
class _ParsedEntry:
    data: Mapping[str, Any]
    source: Path
    provenance: Mapping[str, FieldSource]
    permission_alias: str | None = None


def _flatten_sources(
    values: Mapping[str, Any], source: FieldSource, prefix: str = ""
) -> dict[str, FieldSource]:
    result: dict[str, FieldSource] = {}
    for key, value in values.items():
        field = f"{prefix}.{key}" if prefix else key
        result[field] = source
        if isinstance(value, dict):
            result.update(_flatten_sources(value, source, field))
    return result


def _merge_values(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(parent))
    for key, value in child.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_values(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _merge_parsed(parent: _ParsedEntry, child: _ParsedEntry) -> _ParsedEntry:
    data = _merge_values(parent.data, child.data)
    provenance = dict(parent.provenance)
    provenance.update(child.provenance)
    permission_alias = (
        child.permission_alias if "permissions" in child.data else parent.permission_alias
    )
    return _ParsedEntry(
        data=data,
        source=child.source,
        provenance=provenance,
        permission_alias=permission_alias,
    )


def _expect_string(path: Path, key: str, value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise RegistryError(f"{path}: key '{key}' must be a non-empty string")
    return value


def _expect_string_list(path: Path, key: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RegistryError(f"{path}: key '{key}' must be an array of non-empty strings")
    return tuple(value)


def _parse_entry(
    path: Path,
    *,
    allow_partial: bool = False,
    source_kind: Literal["entry", "adapter-default"] = "entry",
) -> _ParsedEntry:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"{path}: cannot read agent definition: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path}: invalid TOML: {exc}") from None
    unknown = sorted(set(raw) - _ENTRY_KEYS)
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        raise RegistryError(f"{path}: unknown key(s) {names} in agent definition")

    for key in (
        "name",
        "author",
        "command",
        "install_command",
        "home",
        "home_env",
        "extends",
        "description",
        "model",
        "mode",
    ):
        if key in raw:
            _expect_string(path, key, raw[key], allow_empty=key == "description")
    if "effort" in raw:
        value = _expect_string(path, "effort", raw["effort"])
        if value not in EFFORT_VALUES:
            raise RegistryError(f"{path}: key 'effort' has unsupported level '{value}'")
    permission_alias: str | None = None
    if "permissions" in raw:
        permission = _expect_string(path, "permissions", raw["permissions"])
        canonical = normalize_permission(permission)
        if canonical not in PERMISSION_VALUES:
            supported = ", ".join(PERMISSION_VALUES)
            raise RegistryError(f"{path}: key 'permissions' must be one of: {supported}")
        if permission in PERMISSION_ALIASES:
            permission_alias = permission
    for key in ("bypass_modes", "efforts", "env_passthrough"):
        if key in raw:
            _expect_string_list(path, key, raw[key])
    if "env" in raw and (
        not isinstance(raw["env"], dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw["env"].items()
        )
    ):
        raise RegistryError(f"{path}: table '[env]' must contain only string values")
    if "presets" in raw:
        presets = raw["presets"]
        if not isinstance(presets, dict):
            raise RegistryError(f"{path}: table '[presets]' must contain model tiers")
        for tier, preset in presets.items():
            if not isinstance(tier, str) or not isinstance(preset, dict):
                raise RegistryError(f"{path}: each [presets] value must be a table")
            unknown_preset = sorted(set(preset) - _PRESET_KEYS)
            if unknown_preset:
                names = ", ".join(repr(key) for key in unknown_preset)
                raise RegistryError(f"{path}: [presets.{tier}] unknown key(s) {names}")
            if "model" not in preset:
                raise RegistryError(f"{path}: [presets.{tier}] requires model")
            _expect_string(path, f"presets.{tier}.model", preset["model"])
            if "effort" in preset:
                preset_effort = _expect_string(path, f"presets.{tier}.effort", preset["effort"])
                if preset_effort not in EFFORT_VALUES:
                    raise RegistryError(
                        f"{path}: [presets.{tier}].effort has unsupported level '{preset_effort}'"
                    )

    if not allow_partial and "command" not in raw and "extends" not in raw:
        raise RegistryError(f"{path}: an entry must define 'command' or 'extends'")
    source = FieldSource(source_kind, path)
    return _ParsedEntry(
        data=raw,
        source=path,
        provenance=_flatten_sources(raw, source),
        permission_alias=permission_alias,
    )


def _resource_path(resource: Any) -> Path:
    return Path(str(resource))


def _shipped_files() -> Iterator[tuple[str, Path]]:
    root = files("acpc").joinpath("data", "agents")
    try:
        resources = sorted(root.iterdir(), key=lambda item: item.name)
    except (FileNotFoundError, OSError) as exc:
        raise RegistryError(f"package agent definitions are unavailable: {exc}") from None
    for resource in resources:
        if resource.name.endswith(".toml"):
            yield resource.name[:-5], _resource_path(resource)


def _to_resolved(
    name: str, parsed: _ParsedEntry, extends: str | None, base_adapter: str
) -> ResolvedEntry:
    data = parsed.data
    command = data.get("command")
    if not isinstance(command, str) or not command:
        raise RegistryError(
            f"{parsed.source}: agent '{name}' resolves without a command; "
            "define command on the adapter or extend an adapter that has one"
        )
    presets: dict[str, Preset] = {}
    raw_presets = data.get("presets", {})
    if not isinstance(raw_presets, dict):  # validated earlier, defensive for merged values.
        raise RegistryError(f"{parsed.source}: [presets] must be a table")
    for tier, raw_preset in raw_presets.items():
        if not isinstance(raw_preset, dict):
            raise RegistryError(f"{parsed.source}: [presets.{tier}] must be a table")
        source = parsed.provenance[f"presets.{tier}.model"].path
        if source is None:
            raise RegistryError(f"{parsed.source}: preset '{tier}' has no file source")
        raw_effort = raw_preset.get("effort")
        presets[tier] = Preset(
            model=str(raw_preset["model"]),
            effort=None if raw_effort is None else str(raw_effort),
            source=source,
        )

    def string_or_none(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    def strings(key: str) -> tuple[str, ...]:
        # Both shapes are real: a file yields a list, an inherited parent
        # field arrives as the parent ResolvedEntry's tuple.
        value = data.get(key, ())
        return tuple(value) if isinstance(value, (list, tuple)) else ()

    raw_env = data.get("env", {})
    env = dict(raw_env) if isinstance(raw_env, dict) else {}
    provenance = dict(parsed.provenance)
    default_fields = {"bypass_modes", "efforts", "env_passthrough", "env", "presets"}
    for field in _FIELD_NAMES:
        if field not in provenance:
            kind = "default" if field in default_fields or field == "name" else "unset"
            provenance[field] = FieldSource(kind)
    return ResolvedEntry(
        entry=name,
        base_adapter=base_adapter,
        name=string_or_none("name") or name,
        author=string_or_none("author"),
        command=command,
        install_command=string_or_none("install_command"),
        home=string_or_none("home"),
        home_env=string_or_none("home_env"),
        bypass_modes=strings("bypass_modes"),
        efforts=strings("efforts"),
        effort_config_id=string_or_none("effort_config_id"),
        env_passthrough=strings("env_passthrough"),
        description=string_or_none("description"),
        model=string_or_none("model"),
        effort=string_or_none("effort"),
        mode=string_or_none("mode"),
        permissions=normalize_permission(string_or_none("permissions")),
        env=env,
        presets=presets,
        extends=extends,
        provenance=provenance,
    )


class AgentRegistry:
    """Load, inspect, and resolve shipped and user agent entries."""

    def __init__(self, user_dir: str | Path | None = None) -> None:
        self.user_dir = Path(user_dir) if user_dir is not None else agents_dir()
        self._entries: dict[str, _ParsedEntry] = {}
        self._shipped_names: set[str] = set()
        for name, path in _shipped_files():
            parsed = _parse_entry(path, source_kind="adapter-default")
            self._entries[name] = parsed
            self._shipped_names.add(name)
        self._load_user_entries()

    def _load_user_entries(self) -> None:
        if not self.user_dir.exists():
            return
        if not self.user_dir.is_dir():
            raise RegistryError(f"{self.user_dir}: agent entries path is not a directory")
        try:
            paths = sorted(self.user_dir.glob("*.toml"))
        except OSError as exc:
            raise RegistryError(f"{self.user_dir}: cannot list agent entries: {exc}") from None
        for path in paths:
            name = path.stem
            parsed = _parse_entry(
                path, allow_partial=name in self._shipped_names, source_kind="entry"
            )
            if name in self._shipped_names and "extends" not in parsed.data:
                parsed = _merge_parsed(self._entries[name], parsed)
            self._entries[name] = parsed

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    @property
    def adapters(self) -> tuple[ResolvedEntry, ...]:
        return tuple(entry for entry in self if entry.is_adapter)

    @property
    def variants(self) -> tuple[ResolvedEntry, ...]:
        return tuple(entry for entry in self if entry.is_variant)

    def __iter__(self) -> Iterator[ResolvedEntry]:
        for name in self.names:
            yield self.resolve(name)

    def resolve(self, name: str) -> ResolvedEntry:
        return self._resolve(name, ())

    def permission_alias(self, name: str) -> str | None:
        """Return the deprecated permissions alias declared by an entry, if any."""
        if name not in self._entries:
            raise RegistryError(f"unknown agent '{name}'")
        parsed = self._entries[name]
        if "permissions" in parsed.data:
            return parsed.permission_alias
        extends = parsed.data.get("extends")
        if isinstance(extends, str) and extends:
            return self.permission_alias(extends)
        return None

    def _resolve(self, name: str, stack: tuple[str, ...]) -> ResolvedEntry:
        if name not in self._entries:
            if stack:
                parent = stack[-1]
                raise RegistryError(f"agent '{parent}': missing base '{name}'")
            raise RegistryError(f"unknown agent '{name}'")
        if name in stack:
            cycle = " -> ".join((*stack, name))
            raise RegistryError(f"agent inheritance cycle: {cycle}")
        parsed = self._entries[name]
        extends = parsed.data.get("extends")
        if extends is not None and not isinstance(extends, str):
            raise RegistryError(f"{parsed.source}: key 'extends' must be a string")
        if extends:
            parent = self._resolve(extends, (*stack, name))
            parent_data = {
                key: getattr(parent, key)
                for key in _FIELD_NAMES
                if key not in {"presets", "env"} and key not in _NON_INHERITABLE_FIELDS
            }
            # Rebuild nested values from the parent's public representation;
            # provenance is kept separately and then overlaid with the child.
            parent_data["presets"] = {
                tier: {"model": item.model, "effort": item.effort}
                for tier, item in parent.presets.items()
            }
            parent_data["env"] = dict(parent.env)
            parent_provenance = {
                field: source
                for field, source in parent.provenance.items()
                if field not in _NON_INHERITABLE_FIELDS
            }
            parent_parsed = _ParsedEntry(
                data=parent_data,
                source=parent.source,
                provenance=parent_provenance,
            )
            merged = _merge_parsed(parent_parsed, parsed)
            return _to_resolved(name, merged, extends, parent.base_adapter)
        return _to_resolved(name, parsed, None, name)

    def resolve_call(
        self,
        name: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        mode: str | None = None,
        permissions: str | None = None,
        home: str | None = None,
    ) -> CallResolution:
        return self.resolve(name).resolve_call(
            model=model,
            effort=effort,
            mode=mode,
            permissions=permissions,
            home=home,
        )

    def install_command(self, name: str) -> str:
        command = self.resolve(name).install_command
        if not command:
            raise RegistryError(f"agent '{name}' does not define an install_command")
        return command

    def execute_install(
        self,
        name: str,
        *,
        runner: Callable[..., Any] = subprocess.run,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Any:
        """Execute an entry's trusted installer, returning its process result."""
        command = self.install_command(name)
        try:
            args = shlex.split(command)
        except ValueError as exc:
            raise RegistryError(f"agent '{name}': invalid install_command quoting: {exc}") from None
        if not args:
            raise RegistryError(f"agent '{name}': install_command must not be empty")
        return runner(
            args, cwd=str(cwd) if cwd is not None else None, env=dict(env) if env else None
        )
