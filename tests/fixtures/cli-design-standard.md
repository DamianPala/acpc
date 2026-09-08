# CLI Design Standard for Humans and Agents

**Version:** 0.1.0-draft.7

## Scope

This document defines the contract for command-line tools, new or adapted, for reliable use by people and software agents. It includes human-facing behavior, machine-readable introspection, and the minimum safety contract for repeated and mutating operations. The requirements are written for greenfield: a tool or path with no released contract. Brownfield adoption, keeping released behavior that callers depend on, is the subject of Existing tools.

The standard is independent of the operating system, shell, implementation language, framework, and backend. Arguments, standard streams, environment variables, exit codes, and TTY refer to the equivalent process interfaces on each platform. Examples are non-normative; command examples use POSIX-like notation for readability.

## Design principles

This section is non-normative. It records the decisions that shape the standard.

- **A process contract.** The standard binds arguments, the standard streams, environment variables, exit codes, and one introspection command. It requires no daemon, socket, or protocol library, so any tool that can be executed can conform.
- **A testable contract.** Each requirement has an observable pass or fail. A preference without a test belongs in guidance. The introspection index and command detail let a reviewer inspect the tool's declared contracts without invoking domain operations (D7, D8). Checking those declarations alone does not establish that the tool's behavior satisfies them; the agreement rule (D1) still applies.
- **An introspection command, not `--help --json`.** The introspection index and command detail (D7, D8) form a versioned contract (D9), fetched without credentials, configuration, or network (D6), and carry `effects`, `confirm`, and an output schema. Help output is a rendering for people and stays free to change.
- **Context from the stdin TTY, an explicit machine-readable format, and `NO_INPUT`.** Terms define the *interactive context* once. Each signal can only force the *non-interactive context*, so none can contradict a TTY; a tool-defined signal for the same context would let two signals disagree. A tool's default format does not force it, so a machine caller that runs the tool in a PTY needs an explicit machine-readable format or `NO_INPUT`.
- **Conditional requirements and named extensions, no profiles.** Applicability is stated per requirement: Stage R when a command mutates, R7 when work outlives the command that started it, O6 when a collection has no documented finite maximum. An extension names one guarantee a tool cannot provide by accident, and the name alone tells a caller what it may rely on: resumable pagination (`continuation`), observable accepted work with controls where cancellation or suspension exists (`managed`). A profile that bundled requirements across stages by choice would let one claim mean many things. An existing tool adopts by adding the introspection command and declaring, per command, what it keeps (Existing tools), so one claim keeps one meaning and the caller reads the exceptions from the index.
- **`--json`, not `--output`.** `--json` is one flag a caller can pass without knowing the tool's format names. `--output` means a file path in some tools and a format in others, so a caller who sees it cannot tell which.
- **An open table of error kinds.** Names in the F3 error table keep their meanings everywhere; any other `kind` is tool-defined and unprefixed. A caller acts on the names it knows either way, and a prefix would only cost the tool.

## Terms

Italics mark a term defined in this section.

- **Interactive context, non-interactive context.** A call runs in an interactive context when stdin is a TTY and the call did not select a machine-readable stdout format explicitly, with `--json` or a `--format` value the tool documents as machine-readable; it runs in a non-interactive context otherwise. A non-empty `NO_INPUT` environment variable forces the non-interactive context under I5a. A machine-readable default format does not by itself make the context non-interactive. These terms govern prompts and consent; a prompt written to the command's own streams goes to a stream that is a TTY under I5a, and an interactive session or a pager requires a *terminal context* under I5b and I5d.
- **Terminal context.** A call runs in a terminal context when it runs in an *interactive context*, stdout is also a TTY, and the selected stdout format is human-readable.
- **Intended state.** The state a command's documented purpose is to bring about. State the tool touches only as an implementation detail, such as caches, indexes, telemetry, or metadata used to compute a dependency closure, is not intended state. A dependency target changed as part of the command's documented outcome remains intended state.
- **Machine-readable, human-readable.** `json` and `ndjson` are machine-readable formats, as is any other format the tool documents as machine-readable. Every other format, including `text`, `plain`, Markdown, and a native document format, is human-readable.
- **Structured output.** Stdout written in a machine-readable format: the O5a document or the O7 record stream. A structured success is a success that writes it.
- **Fail with a `kind`.** To fail with a `kind` is to exit non-zero and, wherever structured errors are demanded (F2), emit a structured error object (F3) with that `kind`; other requirements cite this definition.
- **Document command, stream command, silent command, delegating command.** Four shapes of what a successful call writes to stdout, read from the D8 fields:
  - a document command has `output` and does not declare `stream: true`;
  - a stream command declares `stream: true`;
  - a silent command has neither `output` nor `delegates_stdout: true`;
  - a delegating command declares `delegates_stdout: true`.
- **Introspection command.** The arguments, excluding the program name, that return the D7 index. It is `schema` unless another form is required under B3.
- **Caller.** The person or program that runs the tool and consumes its output.
- **Gated call.** A call that does not carry `--yes` and would reach an effect for which R2b or the tool itself requires confirmation.
- **Managed operation.** Work that continues after the command that started it exits.
- **Accepting call.** A call that exits after the work it started was accepted and before that work finishes.
- **Reserved field name.** A field name this standard defines with a meaning in success output: `changed`, `cursor`, `has_more`, `items`, `next`, `next_cursor`, `output_file`, `requires_confirmation`, `status`, `targets`, and `truncated`.
- **Core requirement.** A normative statement in a section before Extensions.
- **Extension.** A named section under Extensions. An extension is an optional guarantee that a tool enables by declaring its name in D7; its requirements bind only a tool that declares it.

## Conformance

A conforming tool MUST satisfy every applicable core requirement in this standard. A conditional requirement applies only when its stated condition is true.

A full conformance claim is the D7 `conformance` field. It MUST identify the version of this document the tool was checked against and every extension the tool claims. A claim of any `0.1.x` version is valid under every later `0.1.x`, because a `0.1.x` release adds no core requirement, strengthens none, and changes no meaning of a name, field, flag, or `kind` a caller may rely on; a change that would do any of these is `0.2.0`. This version defines two extensions under Extensions, `continuation` and `managed`. A tool MAY claim an extension only if every applicable requirement in its section is satisfied on every command the claim covers.

A tool with a released contract adopts this standard under Existing tools, which defines what such a tool may keep and how the claim excludes incompatible retained commands. Every requirement outside that section remains applicable except where that section states what released behavior may be kept.

Tests and conformance tools can verify these requirements, but this document remains authoritative if they disagree.

## Normative language

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be interpreted as described in BCP 14 (RFC 2119 and RFC 8174) when, and only when, they appear in all capitals. Synonymous BCP 14 terms are not used.

## Maintenance

The non-normative [maintenance guide](cli-design-standard-maintenance.md) defines the admission policy and verification workflow for changes to this standard. It is not part of conformance.

## How to read this standard

This section is not part of conformance. Whether a requirement applies is decided by that requirement's own condition, never by this section; the tables below say where to start and which sections to read. The core is this document up to the end of Stage H; Existing tools and the two extensions follow it, and each says when it applies.

By goal:

| Goal | Where to start | Sections to read |
|---|---|---|
| Implement a new tool | Scope and Terms, then the stages in order; the introspection contracts (D7, D8) are what the later stages fill in | Core |
| Audit a conformance claim | Conformance, then the D7 `conformance` field, then D8 for each covered command | Core; Existing tools; each claimed extension |
| Look up a flag name, a D8 field, or a `kind` | H1b, D8, F3c | Stage H, Stage D, Stage F |
| Adopt a tool with a released contract | Existing tools, B1 to B4, before anything else, then as for a new tool | Existing tools, then core |

By tool shape:

| Tool shape | What the shape adds to the core, and what it sets aside | Sections to read |
|---|---|---|
| Every command read-only | R1, R6, R7 under their own conditions; the mutation requirements (R2 to R5) do not apply | Core |
| Some commands mutate state | R1 for every command; R2 to R5 for the commands that mutate; R6 and R7 under their own conditions | Core |
| A *stream command* | O7 for that command in place of O5a, O5c, and O6, with O5b and O5d per record; the plain-output requirement for collections (H5) does not apply to it | Core |
| An *accepting call* | R7, and I8 for a call that waits | Core |
| `managed` claimed | R7 first, then M1 to M3; the wait command carries a deadline (I8b), and a listing of operations (M1f) is a collection (O6, H5) | Core, then Extension `managed` |
| `continuation` claimed | C1 after O6, for a collection bounded under O6b; C2 after O7, for a *stream command* | Core, then Extension `continuation` |

## Stage D: Discovery and introspection

What a tool publishes about itself and what it does at runtime are one contract: the agreement rule (D1) holds help (D3) and the introspection output (D6 to D9) to actual behavior. Names (D4) and breadcrumbs (D5) make the next call guessable. Discovery has layers (D2); shipped guidance is the layer that adds context and does not repeat the catalogs (D10).

### D1: Published interfaces MUST match actual behavior.

Help, introspection output, static shell-completion candidates, documentation for the installed tool version, and shipped agent guidance MUST agree with the tool and with each other.

### D2: Discovery is layered.

Discovery is layered: `--help` under D3, the introspection command under D6, and optional shipped guidance under D10.

### D3: `--help` MUST be a standalone cheat sheet at every level.

**D3a** Root `--help` MUST state the tool's purpose, list its named commands or groups, if any, and state the literal invocation of the introspection command. When any command has D8 `output`, root help MUST also point to `--json` under O2b.

**D3b** Each command MUST provide `--help` that states its purpose and usage and describes its arguments, command-specific flags, and applicable defaults. When the command list grows, help MUST group commands rather than omit them.

### D4: Command and flag names MUST be predictable.

**D4a** Related commands MUST follow a predictable structure (`tool <noun> <verb>` or `tool <verb>`).

**D4b** For equivalent operations, commands SHOULD prefer `get`, `list`, `create`, and `delete` to `info`, `ls`, and `add`. Managed-operation commands SHOULD use `status`, `wait`, and `cancel`, plus `pause` and `resume` for resumable suspension.

**D4c** A canonical flag name MUST keep the same meaning wherever it appears.

**D4d** Root `--version` MUST exist. `tool_version` SHOULD follow Semantic Versioning 2.0.0 or the platform's canonical form of it, such as PEP 440, in either case without a `v` prefix, and `--version` SHOULD print exactly that string followed by a newline.

### D5: A workflow with one unambiguous next step SHOULD expose it as a breadcrumb.

**D5a** Continuation vectors follow these rules:

- In structured output, `next` MUST be a non-empty array of strings forming a complete argv vector that can be executed without a shell.
- For the executable, `next[0]` is the tool's canonical executable name; a caller that invoked the tool by path substitutes it.
- When the continuation needs a credential or configuration source selected by a flag on the initiating call, `next` MUST carry that flag and a value selecting the same source; it MAY use `-` for stdin, and a path value MUST use the resolved location; the vector never carries stdin contents.
- A command that may emit `next` MUST declare it as an optional field in its D8 output schema.

**D5b** Human-readable output SHOULD render the same invocation as `Next: ...` and safely quote caller-controlled values.

**D5c** Output MUST omit `next` when there is no natural continuation.

A queued deployment renders the breadcrumb at a terminal:

```console
$ mytool deploy service-a --env prod --yes --background
Deployment queued: dep_123
Next: mytool deployments wait dep_123
```

The same call, `mytool deploy service-a --env prod --yes --background --json`, carries it as `next`:

```json
{
  "deployment_id": "dep_123",
  "status": "queued",
  "changed": true,
  "next": ["mytool", "deployments", "wait", "dep_123"]
}
```

### D6: The introspection command MUST expose the command interface as JSON.

**D6a** Entry: the introspection command MUST be unambiguous from the arguments alone and MUST NOT dispatch to a domain operation. `schema` as the first argument MUST be reserved for introspection. The introspection command MUST take precedence over any command name the tool resolves from caller configuration or from an executable on the caller's `PATH`.

**D6b** Consistency: command paths, arguments, flags, and defaults exposed by the introspection output MUST match the parser used at runtime. Shared definitions, generation, and conformance tests are all valid ways to enforce this.

**D6c** Execution: the introspection command MUST write only JSON to stdout and require no application authentication, configuration, network access, or prompts. It MAY accept global flags without applying them.

**D6d** Routing: the introspection command alone MUST return the D7 index, and followed by a command path it MUST return command detail; outside the conformance claim that detail matches the parser under D6b and is not bound by D8 (B2c). A command path's segments are the parts of D7 `commands[].name` split on U+0020; each segment MUST be passed as a separate argument in that order. For a tool with one unnamed command (D7a `command`), any argument after the introspection command is an unknown path.

**D6e** Errors: an unknown path MUST be a usage error and name the nearest valid paths, if any. When the unknown path is a command-path prefix of valid paths, matched at a segment boundary, those paths are the nearest. The error MUST NOT include unrelated paths merely to reproduce the full index; it MAY list the full index when every indexed path is a nearest match.

### D7: The introspection index MUST let callers choose a command safely.

**D7a** The index MUST contain the fields in the table below.

| Field | Type | Contract |
|---|---|---|
| `schema_version` | string | Introspection format version. |
| `tool_version` | string | The version string, which `--version` output MUST contain; D4d recommends printing exactly that string. |
| `global_flags` | array | I1 flag descriptors accepted by every command. D8 `flags` MUST NOT repeat them. Excludes `--help` and `--version`, which D3b and D4d already require. |
| `format_defaults` | object | O2 default format for each output context. |
| `exit_codes` | object | F1 tool-wide exit code meanings. MUST contain the decimal-string keys of the F1a table and the substitutes B4a requires. |
| `conformance` | object | Conformance claim: the standard version checked against, the claimed extensions, and an optional coverage scope (D7c). |
| `commands` | array | Flat routing list of every command the tool itself dispatches, by full path, sorted by `name` in Unicode code point order; excludes the introspection command. Group prefixes that dispatch nothing themselves are not entries. Commands provided by external executables MAY be omitted only when the tool itself dispatches at least one command and the omitted executables are optional additions installed by the caller; the tool MUST document that they are omitted. |
| `command` | object | Present exactly when the tool has one unnamed command: that command's detail. D8 binds it when the claim covers the command; outside the claim it matches the parser under D6b instead (B2c). |

**D7b** Each command entry MUST contain `name`, `description`, and `effects`, and MUST NOT contain invocation details. `name` MUST consist of non-empty segments separated by exactly one U+0020, or be `""` for the unnamed command, and MUST be unique within `commands`. `description` MUST distinguish neighboring commands, and `effects` is the R1a value. An entry MAY contain `conforming: false` under B2a; omitted, it means `true`.

**D7c** In `conformance`, `name` and `standard` MUST equal `cli-design-standard` and the version of this document the tool was checked against, respectively. `extensions` MUST be present and contain each claimed extension exactly once, an empty array when none is claimed; a caller that does not recognize an extension name MUST NOT rely on that extension. `scope` MAY be present as command-path prefixes, with its shape and command coverage defined in B2b, and limits only requirements that apply per command (B2c).

### D8: Command detail MUST expose the command contracts.

Fields marked `always` in the table below MUST be present. A conditional field MUST be present when its condition is true and MUST NOT be present otherwise.

| Field | JSON type | Presence | Contract |
|---|---|---|---|
| `name` | string | always | Full command path, or `""` for a tool with one unnamed command. |
| `description` | string | always | One-line purpose. |
| `args` | array | always | I1 positional argument descriptors, in argument order. |
| `flags` | array | always | I1 command-specific flag descriptors. Effective flags are D7 `global_flags` plus this array. |
| `effects` | string | always | The R1a value: `read_only`, `idempotent`, or `non_idempotent`. |
| `confirm` | boolean | always | MUST be `true` when any valid call may require `--yes` under R3a and `false` otherwise. |
| `interactive` | boolean | always | MUST be `true` when any valid call starts an interactive session under I5b and `false` otherwise. |
| `stream` | boolean | `delegates_stdout` is not `true`, and success emits a record stream | MUST be `true`. `output` describes one record; the stream is bounded and framed under O7. |
| `delegates_stdout` | boolean | every successful call writes to stdout only the unmodified stdout of a process the command runs | MUST be `true`. `output` and `stream` MUST NOT be present; stdout stays the contract of the process the command runs (O2f). |
| `output` | object | either success conveys data beyond the exit status or `changed` is required under R5a, and never when `delegates_stdout` is `true` | O4 JSON Schema for the success document or one O7 record. When R5a requires `changed`, MUST declare it with type `boolean`, or with type `["boolean", "null"]` only when R5a permits `null`. |
| `exit_codes` | object | adds or refines tool defaults | F1 additions or refinements that preserve tool-wide meanings. |
| `format_defaults` | object | differs from tool default | O2 output defaults. |
| `reserved_overrides` | object | the command retains a reserved field name under B4c | B4c map from each such reserved field name to the name that carries this standard's meaning. |

This example shows how the index and command detail fit together; the contract is in D7 and D8. The `mytool` examples in this document are independent fragments; each agrees with its own requirement, not with the others.
Fields follow the table order for readability; JSON object order is not part of the contract.

```console
$ mytool schema
```

```json
{
  "schema_version": "1",
  "tool_version": "2.1.0",
  "global_flags": [
    {"name": "json", "description": "Emit JSON output", "type": "boolean",
     "required": false, "default": false}
  ],
  "format_defaults": {"tty": "text", "non_tty": "json"},
  "exit_codes": {"0": "success", "1": "failure", "2": "usage error"},
  "conformance": {"name": "cli-design-standard", "standard": "0.1.0-draft.7", "extensions": []},
  "commands": [
    {"name": "services get", "description": "Get a deployed service", "effects": "read_only"},
    {"name": "services list", "description": "List deployed services", "effects": "read_only"}
  ]
}
```

`services get` is one entry with two segments, fetched as `mytool schema services get` (D6d):

```console
$ mytool schema services get
```

```json
{
  "name": "services get",
  "description": "Get a deployed service",
  "args": [{"name": "service", "description": "Service name", "type": "string", "required": true}],
  "flags": [{"name": "env", "description": "Environment to read", "type": "string",
             "required": true, "enum": ["dev", "staging", "prod"]}],
  "effects": "read_only",
  "confirm": false,
  "interactive": false,
  "output": {
    "type": "object",
    "required": ["name", "env", "replicas"],
    "properties": {
      "name": {"type": "string"},
      "env": {"type": "string", "enum": ["dev", "staging", "prod"]},
      "replicas": {"type": "integer"}
    }
  }
}
```

A valid call and its result:

```console
$ mytool services get api --env prod --json
{"name":"api","env":"prod","replicas":3}
```

In the call, `api` and `--env prod` fill the detail's required argument and enumerated flag (I1a); `--json` picks the format explicitly (O2b), so the declared defaults do not apply to this call (O2d); the stdout document conforms to the `output` schema (O4a, O5a).

```console
$ mytool services get api --env qa --json
{"error":{"kind":"invalid_input","message":"services get: invalid env 'qa'","hint":"Use --env dev, staging, or prod"}}
$ echo $?
2
```

The invalid enum value is a usage error (I6a) with exit `2` (F1a); under `--json` the error object is required (F2b), sits on stderr as the last non-empty line and not on stdout (F2c), and follows the error envelope (F3a, F3b).

A tool with one unnamed command has no command path, so its index carries the detail:

```console
$ hashfile schema
```

```json
{
  "schema_version": "1",
  "tool_version": "1.0.0",
  "global_flags": [
    {"name": "json", "description": "Emit JSON output", "type": "boolean", "required": false, "default": false}
  ],
  "format_defaults": {"tty": "text", "non_tty": "json"},
  "exit_codes": {"0": "success", "1": "failure", "2": "usage error"},
  "conformance": {"name": "cli-design-standard", "standard": "0.1.0-draft.7", "extensions": []},
  "commands": [{"name": "", "description": "Hash a file", "effects": "read_only"}],
  "command": {
    "name": "",
    "description": "Hash a file",
    "args": [{"name": "file", "description": "File to hash", "type": "string", "required": true}],
    "flags": [{"name": "algorithm", "description": "Digest algorithm", "type": "string", "required": false, "default": "sha256"}],
    "effects": "read_only",
    "confirm": false,
    "interactive": false,
    "output": {"type": "object", "required": ["digest"], "properties": {"digest": {"type": "string"}}}
  }
}
```

`hashfile README.md` runs the command. `hashfile schema README.md` is an unknown path under D6d.

### D9: The introspection contract MUST be stable and versioned.

**D9a** `schema_version` MUST be a positive decimal integer encoded as a string. Callers should ignore unknown fields. A change that adds an optional introspection or output field MAY keep `schema_version` unchanged. A change that adds a required introspection field, removes an introspection field, or changes an introspection field's type or meaning MUST increment it.

**D9b** Tool versions documented as compatible MUST preserve existing command paths, input meanings and defaults, exit code meanings, F3 `kind` meanings, and structured output fields. An incompatible change MUST document its replacement and migration; changing `schema_version` alone is insufficient.

### D10: Agent guidance MUST add context, not repeat the interface.

The tool MAY ship `SKILL.md` or `AGENTS.md` when domain knowledge or workflows need explanation. Guidance MUST point to commands and the introspection output, and MUST NOT duplicate their catalogs.

## Stage I: Invocation and input

A caller may be a sandboxed process with no keyboard, a producer of generated bytes, or both. Input handling treats those bytes as data, not shell syntax. The tool resolves declared inputs, validates them before acting, and keeps external work bounded.

### I1: Every accepted argument and flag MUST have a defined input contract.

**I1a** D7 `global_flags` and D8 `args` and `flags` MUST expose it as a descriptor with the fields in the table below.

| Field | Type | Rule |
|---|---|---|
| `name` | string | MUST be present. Flag names omit leading hyphens; one character renders as `-n`, while longer names render as `--name`. |
| `description` | string | MUST be present. One line stating what the input controls. |
| `type` | string | MUST be present and equal `string`, `integer`, `number`, or `boolean`. |
| `required` | boolean | MUST be present. `true` means the caller must provide the input. |
| `default` | scalar or array | MUST be present when the parser supplies an actual default; run-time and sentinel values are not defaults (I1c). |
| `enum` | array | MUST be present and list every choice when the parser declares a closed choice set. Every item MUST match `type`. |
| `aliases` | array | MAY appear on flags and contain alternative names without leading hyphens. Alias rendering follows `name`. |
| `variadic` | boolean | MAY be `true` only on the last argument. It consumes the remaining positional values as an ordered array; with `required: true`, it consumes at least one. |
| `repeatable` | boolean | MAY be `true` only on flags. It allows repeated occurrences and resolves their values to an ordered array. |
| `accepts_stdin` | boolean | MUST be `true` when `-` selects stdin instead of a file. |

**I1b** A flag with `type: boolean` MUST set its value to `true` when given by its bare name and MUST NOT consume the following argument as its value; it MAY also accept both `--name=true` and `--name=false`.

**I1c** `default` is the built-in default, with these rules:

- A default that applies unless another flag is present MAY be declared as `default` with the condition stated in `description`.
- Scalar defaults MUST match `type`.
- Variadic and repeatable defaults MUST be arrays whose items match `type`.
- Required inputs MUST NOT have a default.
- A value resolved at run time, from I2 sources or from the environment, such as the working directory or the newest available version, MUST be omitted and described in `description`.
- A parser sentinel meaning "not provided", including `null`, is not a default and MUST be omitted.

**I1d** Omitted `variadic`, `repeatable`, and `accepts_stdin` mean `false`; omitted `aliases` means no aliases.

**I1e** Descriptor descriptions follow these rules:

- Any accepted value syntax, bound, path-resolution rule, dependency, conflict, or dynamic value-discovery path not expressed by another descriptor field MUST be stated in `description`.
- When exactly one of two related descriptors is command-specific, its `description` MUST state the relationship.
- When both related descriptors have the same scope, at least one of their descriptions MUST state the relationship.
- A relationship a caller can only observe by running the command, such as an explicit `--limit` ending a `--follow` read under O7b or accepted work outliving its command's deadline under R7d, is a dependency under this clause.

Example descriptors:

- Argument: `{"name": "file", "description": "Files to upload", "type": "string", "required": true, "variadic": true}`
- Flag: `{"name": "tag", "description": "Tag to attach", "type": "string", "required": false, "aliases": ["t"], "repeatable": true}`
- Relationship: when a command accepts both `--follow` and `--limit`, its `--follow` description states that an explicit `--limit` still ends the read under O7b.

Together they accept `mytool upload --tag docs -t archive a.txt b.txt` and resolve both `file` and `tag` to ordered arrays.

### I2: Configuration MUST be declared, deterministic, and inspectable.

**I2a** Sources: accepted configuration files, secret sources, and tool-specific environment variables MUST be documented, and an undeclared tool-specific variable MUST NOT change behavior. Variables SHOULD use a consistent `<TOOL>_<OPTION>` prefix, such as `MYTOOL_LOG_LEVEL`. If a flag selects a configuration file, the tool MUST document whether that file replaces or augments project and user configuration and where it sits in the precedence.

**I2b** Precedence: when more than one source provides a value, resolution MUST be deterministic and documented. It SHOULD follow this order: `flags > environment > project configuration > user configuration > built-in defaults`.

**I2c** Inspection: layered configuration SHOULD expose each resolved value and its source and, when the source has a location, its resolved location, while masking secrets. D5a's complete vector depends on the same per-setting provenance.

Example: `--log-level debug` overrides `MYTOOL_LOG_LEVEL=info`, which overrides `log_level = "warning"` in project configuration and `log_level = "error"` in user configuration.

### I3: Long or generated input MUST use files or stdin, never argv.

**I3a** A command that accepts a document, script, request body, template, or other unbounded text MUST accept a file path and `-` for stdin.

**I3b** The tool MUST reject a call that selects stdin for more than one input.

The same manifest can come from a file or stdin. Its quotes, newlines, and flag-like text remain data rather than argv:

```console
$ mytool deploy service-a --env prod --manifest manifest.json
$ mytool deploy service-a --env prod --manifest - < manifest.json
```

### I4: Secrets MUST NOT be passed as argument or flag values.

**I4a** A command that needs a secret MUST provide a source usable in a *non-interactive context*, such as a credential store, file, stdin, or environment variable. Commands SHOULD use `--token-file PATH` for one secret and `--credentials-file PATH` for structured credentials; either flag SHOULD accept `-` for stdin. Secret-source precedence resolves deterministically and as documented under I2b.

**I4b** A secret MUST NOT appear in help, introspection output, logs, errors, or normal output unless retrieval is the command's documented purpose.

With `--token-file token.txt` under `--verbose`, a diagnostic may name the source, such as `Using token from token.txt`, but not the token.

### I5: Behavior in a non-interactive context MUST be explicit and fail safely.

**I5a** Prompts follow these rules:

- In an *interactive context*, a command MAY prompt; otherwise it MUST NOT prompt. A prompt written to the command's own streams MUST go to a stream that is a TTY.
- Obtaining human input through any other channel, such as an askpass helper or an out-of-process authorization agent that may ask a person, is prompting under this clause, and the tool MUST document each such channel.
- When the environment variable `NO_INPUT` is non-empty, the context is non-interactive regardless of the stdin TTY.
- If required input or confirmation remains unresolved, the command MUST fail with an error that names a flag, file, stdin form, or environment variable that can supply it.
- While stdin supplies an input, a command MUST NOT prompt.
- The absence of a prompt, an unanswered prompt, and end of input MUST NOT be treated as consent.

**I5b** Interactive sessions: a command MAY start an interactive session only in a *terminal context*; otherwise it MUST fail before side effects; the D8 `interactive` field carries the declaration. Starting an external editor is an interactive session.

**I5c** Human action: a workflow MAY require an out-of-band human action, with these rules:

- For such a workflow, its entry point MUST work in a *non-interactive context* and MUST be listed in D7 `commands`.
- Work that continues after that command exits stays identifiable (R7) and, where claimed, falls under the `managed` extension.
- In a *non-interactive context*, a command that would wait for a human action outside the call, such as a browser or device approval, MUST fail before side effects and name the entry point or I4 source that resolves it, unless that wait is the command's documented purpose.

**I5d** Paging: a pager MAY start only in a *terminal context* and follows the user's pager selection under H3.

While stdin supplies the manifest, the command cannot prompt for confirmation, so it fails and names `--yes`:

```console
$ mytool deploy api --env prod --manifest - < manifest.json
Error: Deploying 'api' to prod requires confirmation
Run: mytool deploy api --env prod --manifest - --yes
```

### I6: Invalid input MUST fail before side effects.

**I6a** The tool MUST reject unknown flags, unsupported arguments, invalid values, and conflicting inputs as usage errors. A usage error MUST use F3 `kind` `invalid_input`.

**I6b** `--` MUST end flag parsing, so later tokens are positional. A command SHOULD accept flags before, between, and after positional arguments up to `--`.

**I6c** The tool MUST validate every locally checkable input before changing state.

**I6d** A usage error SHOULD identify the invalid input and show the accepted form or nearest valid name.

### I7: Input MUST stay within its declared bounds.

**I7a** Buffered data: a command that buffers caller-controlled input MUST enforce a maximum size before side effects and state it under I1e in the `description` of each input it bounds.

**I7b** Restricted paths: a command limited to declared roots MUST resolve each supplied path and reject it when it escapes those roots, including through `..` or a symbolic link.

For example, assume `mytool upload` accepts at most 10 MiB and may read files only from `./dist`; both calls fail with an F3 object on stderr:

```console
$ mytool upload ../secrets.txt --json
{"error":{"kind":"invalid_input","message":"Path must resolve inside ./dist"}}

$ cat 20-mib.bin | mytool upload - --json
{"error":{"kind":"invalid_input","message":"Input exceeds the 10 MiB limit"}}
```

### I8: External work MUST be bounded or explicitly unbounded.

**I8a** Connection establishment and non-streaming network operations MUST use finite default timeouts. A command that waits for external state, when waiting is not its documented purpose, MUST use a finite default deadline. A wait for a human action falls under the human-action rule, I5c.

**I8b** Waiting and external work follow these rules:

- A command whose documented purpose is to wait, watch, follow, or run external work to completion MAY be unbounded by default, but MUST accept `--timeout`; its I1 descriptor MUST carry a finite default deadline as `default` or state in `description` that the wait is unbounded by default.
- `--timeout` MUST accept an integer immediately followed by `s`, `m`, or `h` and MAY accept other forms, such as a bare integer with a documented unit.
- A command that accepts `--verbose` and may wait longer than 10 seconds SHOULD, under `--verbose`, emit a liveness line on stderr under O3b at least every 30 seconds while waiting, and MUST document its liveness interval or that it emits no such lines.

**I8c** Any other unbounded mode MUST require explicit selection.

A bounded call that sets a deadline, and an unbounded call that explicitly selects that mode:

```console
$ mytool jobs wait job_123 --timeout 5m
$ mytool logs job_123 --follow
```

The `--timeout` descriptor of a wait that is unbounded by default carries no `default` and says so:

```json
{"name": "timeout", "description": "Give up after this duration, such as 30s or 5m; unbounded by default", "type": "string", "required": false}
```

## Stage R: Repeatability and mutation safety

This stage covers calls that mutate state or may be repeated. Safeguards follow the possible damage, not merely whether a command changes state. A tool whose every command declares `effects: read_only` has no mutation requirements under R2-R5. The rules for safe, bounded retries (R6) apply when the tool retries requests internally. The rules for accepted work (R7) cover identification, completion, and deadlines.

### R1: Effect metadata MUST be conservative.

**R1a** Every D7 command entry and D8 command detail MUST declare one value from the table below.

| `effects` | Contract |
|---|---|
| `read_only` | The command does not change intended state. |
| `idempotent` | The command may change intended state, but repeating a successful call with the same inputs, without an intervening change to the targeted state, MUST succeed unless an independent failure prevents execution, such as rate limiting or an unavailable dependency. A successful repeat MUST leave the intended state as the first call left it and MUST NOT repeat any other documented effect. A failure caused by the first call's effect, such as `not_found` after deletion, violates this guarantee. Recomputing or rewriting only that same intended state does not violate this guarantee. |
| `non_idempotent` | The command meets neither preceding guarantee; a repeat may cause another intended effect or return a stable conflict. |

**R1b** The declaration MUST cover every valid call. Declare `non_idempotent` when any call lacks the repeat guarantee. Otherwise declare `idempotent` when any call can change intended state. Otherwise declare `read_only`. A changing response, incidental telemetry, logs, caches, metering, or rate limiting do not alone change the classification.

For example, a delete by stable identifier whose repeat succeeds with `changed: false` is `idempotent`; a delete whose repeat fails with `not_found` is `non_idempotent`. Runtime target selection alone does not determine the classification; the successful-repeat guarantee (R1a) does. Deleting the newest item is `non_idempotent` when an immediate repeat selects and deletes the next item.

A status read that only records that a worker has already exited can remain `read_only`; stopping the worker as part of the read changes intended state.

### R2: Mutation safeguards MUST match the blast radius.

**R2a** A mutation is wide when it can affect existing targets the caller did not name individually. Targets necessarily included by a documented dependency relationship from individually named targets do not by themselves make a mutation wide. It is irreversible when the tool provides no documented operation that restores the prior state.

**R2b** A mutation MUST carry at least the minimum safeguard listed for its operation in the table below; idempotence does not reduce these safeguards.

| Operation | Minimum safeguard |
|---|---|
| `effects: "read_only"` | None |
| Narrow, reversible mutation | None |
| Narrow, irreversible mutation | Confirmation under R3b |
| Wide mutation | Confirmation under R3b, and `--dry-run` under R4 |

**R2c** An M2 cancellation of exactly one managed operation by its R7 identifier does not require an additional confirmation. Any wider cancellation follows the table.

### R3: Safety gates MUST fail closed and be discoverable.

Four things decide a gate: whether the call may prompt at all, under the prompt policy (I5a); what a *gated call* does when it cannot prompt or the prompt is declined, and when the gate is reached (R3b); what `--dry-run` changes (R3c), with the preview's report (R4c); and how `--yes` differs from `--force` (R3d).

**R3a** Every command that can require `--yes`, including those required under R2b, MUST accept `--yes`; the D8 `confirm` field carries the declaration.

**R3b** A *gated call* that cannot prompt under I5a, or whose prompt is declined, MUST stop before side effects and fail with `kind` `confirmation_required`, naming `--yes`. The gate applies after every check the call performs before side effects: a call that would fail before reaching the gated effect, such as on a missing target, MUST fail with that `kind` without requiring `--yes`.

**R3c** A `--dry-run` call MUST NOT require `--yes`. A command that accepts `--yes` MUST accept it with `--dry-run` and ignore it.

**R3d** `--yes` confirms a prompt; `--force` overrides a documented precondition. Accepting one MUST NOT enable the other. The name of a flag inherited from an upstream contract the tool does not own does not by itself assign either role; its documented behavior determines whether either role applies.

**R3e** Preview binding: a command for a wide mutation under R2a SHOULD accept `--expect-targets N`, where `N` is a non-negative integer. When the flag is given, the mutating call MUST resolve the complete target set by the rules that produce the R4c `targets` array and fail with `kind` `conflict` when `N` differs from the number of distinct targets in that set, all before its first side effect.

A documented precondition that `--force` overrides is not met, so the call fails with an F3 object on stderr and names the override:

```console
$ mytool services delete api --json
{"error":{"kind":"precondition_failed","message":"Service 'api' still has 2 running deployments","hint":"Repeat with --force to delete it anyway"}}
```

A *gated call* in a *non-interactive context*, here with `--json` and no TTY on stdin, stops before side effects and fails with an F3 object on stderr; under R4, the same command previews without `--yes`:

```console
$ mytool services prune --env staging --json < /dev/null
{"error":{"kind":"confirmation_required","message":"Pruning staging services requires confirmation","hint":"Repeat with --yes"}}
```

### R4: Wide mutations MUST be previewable.

**R4a** The command MUST provide `--dry-run`.

**R4b** On any command that accepts it, a `--dry-run` call MUST leave intended state unchanged and follow these outcome rules:

- In all cases, never fail with `confirmation_required` (R3c).
- For any failure other than a permission check that the mutating call would encounter before side effects, fail with the mutating call's `kind`.
- For a permission check the preview performs and that fails, fail with the mutating call's `kind`.
- Otherwise succeed, reporting the gate under R4c.
- The call SHOULD apply the same permission checks as the mutating call; an omitted permission check does not require predicting its result.

**R4c** The structured success of a `--dry-run` call MUST conform to the same D8 `output`, list each target the mutating call would affect as observed during that call in an array field `targets`, whose item schema is tool-defined, return `changed: false`, and return a boolean `requires_confirmation` that is `true` exactly when the same call without `--dry-run` and without `--yes` is a *gated call* in a *non-interactive context*. A value produced only by performing the mutation MUST be omitted or `null`, so the shared schema declares it as optional or nullable.

Preview and mutation share one output schema; this tool also lists `targets` in the mutation's own result, which R4c does not require:

```console
$ mytool services prune --env staging --dry-run --json
{"targets":["api","worker"],"changed":false,"requires_confirmation":true}

$ mytool services prune --env staging --yes --json
{"targets":["api","worker"],"changed":true}
```

### R5: Mutating commands MUST report what happened.

**R5a** `changed` reports whether this call caused a new intended state transition, not whether background work completed, with these rules:

- Every command whose `effects` is not `read_only` MUST return `changed` in every structured success response; the D8 `output` row declares its type.
- Except under the following condition, `changed` is a boolean.
- When the tool cannot observe whether a transition occurred without an additional read it would not otherwise perform, the command MUST return `null` in that case and document the condition; `null` means the tool did not observe, not that nothing changed.

**R5b** When authoritative state reports a concurrent conflict, the tool MUST fail with `kind` `conflict` and MUST NOT silently overwrite it.

**R5c** A `non_idempotent` command whose repetition could duplicate an effect SHOULD accept `--idempotency-key` when the backing service supports idempotency keys.

For example, an idempotent create-or-get may return `changed: true` and then `changed: false`. A strict create may return `changed: true` and then fail with `kind` `conflict`. Under O3c, human-readable output of a repeated idempotent delete reports absence rather than deletion, for example `Already absent: api`.

### R6: Built-in retries MUST be safe and bounded.

A tool that retries requests internally MUST retry only failures that would be reported with `retryable: true` under F3b, and only for `read_only` or `idempotent` commands, or requests protected by an idempotency key. It MUST preserve the original inputs, limit the number of attempts, and remain within the I8 timeout. It SHOULD honor `Retry-After` when the timeout allows.

### R7: Accepted work MUST remain identifiable.

For an *accepting call*, two clauses set the basics: the identifier (R7a) and the meaning of exit `0` (R7b). A command that offers both waiting and returning after acceptance has a clause of its own (R7c), and so does the failure of a call that waits (R7d). Observing and controlling the work after the call exits falls, where claimed, under the `managed` extension (M1 to M3).

**R7a** An *accepting call* MUST return a non-empty canonical identifier in structured success output. Its field name is tool-defined but MUST remain consistent across the command that starts the work and every command that addresses it; in M1-M3, "the identifier" means that value under the same field name. If the command that starts the work fails after obtaining the identifier, any F3 object it emits MUST carry the identifier in `context` under the same field name.

**R7b** Exit `0` means the work was accepted, not completed. The identifier MUST remain usable after the initiating process exits and, until expiry under a documented retention policy, MUST NOT resolve to a different entity. A tool MAY reassign it after expiry. The tool SHOULD document how a caller observes the work, and when a wait command exists, the identifier's D5 breadcrumb SHOULD name it.

**R7c** When a command offers both waiting for completion and returning after acceptance, selecting the latter MUST change only how long the command waits, not the work it starts. The D8 `description` of a command that starts managed work MUST state whether it waits for completion by default.

**R7d** A command that waits for the work it started MUST fail with `kind` `operation_failed` when that work does not succeed. If its deadline passes after the work was accepted, it MUST fail with `kind` `timeout` when the work's state was observed, carrying the identifier under R7a and the observed state in `context`, and with `kind` `outcome_unknown` when it was not. That deadline MUST NOT cancel or otherwise change the work.

Observing and controlling accepted work falls under the `managed` extension (M1-M3).

A `deploy` that waits for its deployment, and the wait command of a tool that declares `managed` observing the same terminal state under M1, both fail on stderr with `operation_failed` and carry the identifier:

```console
$ mytool deploy api --env prod --yes --json
{"error":{"kind":"operation_failed","message":"Deployment dep_123 failed: image pull error","context":{"deployment_id":"dep_123","status":"failed"}}}

$ mytool deployments wait dep_123 --json
{"error":{"kind":"operation_failed","message":"Deployment dep_123 failed","context":{"deployment_id":"dep_123","status":"failed"}}}
```

## Stage O: Output

Three things are decided separately for stdout: which format it uses (O2); what shape the result takes in that format, a schema-backed document (O4, O5), a bounded collection (O6), or a record stream (O7); and how it travels, on streams classified one by one (O1), with distinct roles (O3), and with a closed pipe survived without noise (O8).

### O1: The standard streams MUST be classified independently.

The TTY state of stdin is one input to the prompt policy under I5a, stdout controls the default format under O2c, and stderr controls diagnostic decoration under O3b and whether F2 requires the structured error object. Redirecting one stream does not change how another is classified.

### O2: Output format selection MUST be explicit and predictable.

Which format stdout uses is settled by four clauses together: the declared defaults (O2a), the flag every command takes (O2b), the default when no flag is given (O2c), and precedence between flag and default (O2d). Two situations have clauses of their own: a destination file (O2e) and delegated stdout (O2f).

**O2a** Declaration: D7 `format_defaults` MUST declare `tty` and `non_tty` format names. D8 `format_defaults` MUST appear only when a command differs from those tool-wide defaults. For a *stream command* the machine-readable format name is `ndjson`; that implied name does not by itself make the command differ from the tool-wide defaults.

**O2b** Machine access: every command MUST accept `--json`. On a *silent command*, `--json` selects the `json` format, and a success MUST write no bytes to stdout. If a command also accepts `--format`, `--json` MUST produce the same output as `--format json` or, on a *stream command*, as `--format ndjson`.

**O2c** Defaults: a *document or stream command* SHOULD default to human-readable output on a TTY. On non-TTY stdout, a *document or stream command* MUST default to JSON or NDJSON, except that a command whose primary result is a textual document rather than a set of fields or records MAY default to a declared native format such as text or Markdown.

**O2d** Precedence: an explicit format flag MUST override the detected default. Two explicit format flags are a usage error under I6a.

**O2e** Names: a command MUST NOT accept `--output`. If it accepts a destination path, the flag MUST be named `--output-file`. Under `--output-file`, the file receives exactly what stdout would have received, the declared JSON contract in O5 applies to that file, and a success writes no bytes to stdout. Formats use `--json` or `--format`.

A *silent command* under `--json`, and a *document command* under `--output-file`; both exit `0` and write no bytes to stdout, and the second leaves the result in `services.json`:

```console
$ mytool config validate --json
$ mytool services list --json --output-file services.json
```

**O2f** Delegated stdout: on a *delegating command*, stdout is the contract of the process the command runs. On that command `--json` MUST NOT change stdout; it still forces the *non-interactive context* and selects the format F2 tests. On such a command:

- stdout is not governed by the stream roles, decoration, untrusted-value handling, pipe closure, or color rules (O3a, O3b, O3d, O8, H4);
- requirements on structured success output do not apply (R4c, R5a, R7a);
- stderr still carries failure diagnostics (F2);
- the exit status keeps its tool-wide meaning (F1), so a child exit status passed through unchanged violates F1 where it collides with a tool-wide meaning.

A D8 excerpt for a command that runs a caller-supplied program and passes its stdout through:

```json
{
  "name": "run",
  "effects": "non_idempotent",
  "delegates_stdout": true
}
```

### O3: The output streams MUST have separate roles.

**O3a** Channels: stdout MUST contain only the result; stderr MUST carry logs, warnings, progress, prompts, and errors. `--quiet` and `--verbose`, when accepted, change only stderr diagnostics. A *document or stream command* whose product is a change to a file system or an external system MUST report that product through its D8 `output`; the product itself is not the result on stdout. For example, `mytool init --json` creates a directory and reports `{"path": "/work/app", "changed": true}` rather than writing the directory's contents to stdout.

**O3b** Decoration: machine-readable stdout MUST contain only output in the selected stdout format, without banners, terminal decoration, or ANSI escapes. Animated progress MAY appear only when stderr is a TTY. On non-TTY stderr, progress MUST use complete plain lines or be omitted.

**O3c** Consistency: human and machine renderings MAY differ in detail but MUST NOT contradict each other. When human-readable output is only a bounded preview of the result, it MUST say so and identify how to retrieve the complete result.

**O3d** Untrusted content: machine output MUST serialize caller-controlled and remote values through the selected stdout format. Human output MUST escape terminal control sequences in those values.

### O4: A command schema MUST describe its JSON success value.

**O4a** The D8 `output` field describes one success document, or one record on a *stream command*. It MUST follow JSON Schema Draft 2020-12 and use only `type`, `enum`, `properties`, `required`, and `items`.

**O4b** Every schema MUST contain `type`, except that a schema MAY be the empty object `{}`, meaning any JSON value, where the value's shape depends on the input. `type` MUST be `string`, `integer`, `number`, `boolean`, `array`, or `object`, or a two-element array containing one of those types and `null`.

**O4c** `enum` MAY restrict a value to a finite set. Every member MUST conform to the same schema. `enum` MUST list only values the current version can return.

**O4d** An array schema MUST contain `items`. An object schema MUST contain `properties`, which lists every field the current version may return, and `required`, which lists every always-present field.

Example schema:

```json
{
  "type": "object",
  "required": ["job_id", "status", "progress"],
  "properties": {
    "job_id": {"type": "string"},
    "status": {
      "type": "string",
      "enum": ["queued", "running", "succeeded", "failed", "unknown"]
    },
    "progress": {"type": ["number", "null"]},
    "warnings": {
      "type": "array",
      "items": {"type": "string"}
    }
  }
}
```

### O5: JSON output MUST match its declared contract.

**O5a** Document: a `--json` success of a *document command* MUST contain exactly one JSON value conforming to its D8 `output` schema, encoded as UTF-8 and followed by LF. A failed document call MUST NOT write a JSON value to stdout.

**O5b** Values: values MUST keep their declared JSON types. Machine output MUST NOT silently truncate a value.

**O5c** Bounded values: a command that may cap a single inline value MUST declare a required boolean `truncated` and an optional string `output_file` in its D8 output schema. When `truncated` is `true`, `output_file` MUST identify a file that holds the complete value. The file SHOULD NOT be accessible to other users. The inline value is a preview, and the file's retention MUST be documented. Collections and record streams MUST use their own bounds instead (O6, O7).

**O5d** Time: timestamps MUST use RFC 3339 with a numeric offset or `Z`. Timestamps within one document or stream SHOULD share one precision. Numeric duration field names MUST state their unit.

Output matching the O4 example schema:

```json
{
  "job_id": "job_123",
  "status": "running",
  "progress": 0.4,
  "warnings": []
}
```

A bounded single value may instead report:

```json
{
  "answer": "First part of the answer...",
  "truncated": true,
  "output_file": "/home/user/.cache/mytool/results/job_123.md"
}
```

### O6: Potentially unbounded document collections MUST be bounded.

This requirement applies to document collections; it first sets aside mutation target lists (O6a).

**O6a** An R4 target list, in a preview or in the mutation's own result, is not a collection under this requirement; a command that caps it reports truncation and, when it truncates, a file holding the complete value (O5c).

**O6b** Window: a collection without a documented finite maximum MUST use a finite default limit and accept `--limit`. The tool MUST bound the number of items, not silently omit fields from individual items. An explicit `--limit` is the maximum number of items returned by that call.

**O6c** Shape: a bounded collection MUST be an object with an `items` array and a boolean `has_more` that is `true` exactly when matching items exist beyond those returned. A collection with a documented finite maximum MAY use a bare array. An empty collection MUST use the same shape with an empty array, not `null` or absent output.

Resuming a collection beyond one page falls under the `continuation` extension (C1).

A page whose collection has more items:

```console
$ mytool jobs list --limit 2 --json
```

```json
{
  "items": [
    {"id": "job_123", "status": "running"},
    {"id": "job_456", "status": "queued"}
  ],
  "has_more": true
}
```

### O7: Record streams MUST be bounded and framed.

This requirement applies to a *stream command*.

**O7a** Framing: `--json` MUST emit UTF-8 NDJSON with one complete JSON object per LF-terminated line and no blank lines. Each record MUST conform to D8 `output` and become readable before the command waits for another record or exits. Each record keeps its declared value types without silent truncation (O5b), its timestamp form (O5d), and its field stability across compatible versions (D9).

**O7b** Window: without `--follow`, the command MUST terminate. A stream without a documented finite maximum MUST use a finite default window and accept at least one of `--limit`, `--head`, or `--tail`. `--limit N` is the maximum number of records emitted from the selected position in the documented order; it does not select that position. `--head N` selects the first N matching records and `--tail N` selects the last N matching records. A command MAY accept any subset of these flags. Its D8 descriptions MUST state the default window and how each accepted selector combines with `--follow`; unsupported combinations are usage errors under I6a. `--follow`, an unbounded mode selected explicitly under I8c, removes the default window when offered. An explicit `--limit` remains effective with or without `--follow` and ends the read after N records. Reaching a finite window that ends the read does not end the record source.

**O7c** Ordering: the stream MUST use a stable, documented order and MUST NOT stop before a finite window that ends the read while matching records are available.

**O7d** Completion: exit `0` means the requested read ended successfully, not that the record source is exhausted; empty stdout is valid. After a non-zero exit, prior LF-terminated records remain valid. An unterminated final fragment is not a record. This standard defines no end record; EOF and the exit status are authoritative.

Resuming a stream after a record falls under the `continuation` extension (C2).

A bounded read of two records:

```console
$ mytool logs job_123 --limit 2 --json
{"timestamp":"2026-08-15T10:00:00Z","level":"info","message":"Started"}
{"timestamp":"2026-08-15T10:00:01Z","level":"info","message":"Fetching input"}
```

### O8: Output MUST survive normal pipeline closure.

When a downstream reader closes a pipe, a command that has not otherwise failed MUST stop writing and emit no stack trace or broken-pipe diagnostic. Exit `0` satisfies this requirement. The command MAY instead use the platform's pipe-closure exit status; its non-zero meaning MUST be documented under F1d. A non-zero exit caused only by pipe closure is exempt from the structured error object (F2).

## Stage F: Failure

A failure has two faces: what the process reports through its exit status (F1), and what happened to the operation, which may be unknown, partial, or interrupted (F4, F5). The error object carries the second to the caller: where it is emitted (F2) and what it contains (F3).

### F1: Exit codes MUST be stable and documented.

**F1a** The codes in the table below MUST have these meanings. D7 `exit_codes` carries the tool-wide meaning of every code the tool returns; a code whose meaning belongs to one command alone is added in that command's D8 `exit_codes` under F1e.

| Code | Meaning |
|---|---|
| `0` | Success, including an empty result or no differences. |
| `1` | Generic failure. |
| `2` | Usage error. |

**F1b** A command whose documented result is one predicate (a yes-or-no answer about existing state, such as whether a unit is active or whether two files have no differences) MAY document one additional non-zero code for a false answer under F1d and F1e. For that command, exit `0` MUST mean true and the additional code MUST mean false. Both answers are successful outcomes: the structured error requirements, F2 and F3, do not apply, and F1c's postcondition is that the answer was determined.

**F1c** Exit `0` MUST mean the command observed its documented postcondition through the interface it used. Independent re-verification is not required. For an *accepting call*, acceptance leaves an identifiable operation (R7); when the initiating command waits, it fails when the work does not succeed (R7d), and so does the wait command under `managed` (M1).

**F1d** Additional codes MUST each have one stable meaning. Fine-grained failures MUST use F3 `kind` instead.

**F1e** D8 command `exit_codes` MUST appear only when they add a code or refine a tool-wide description without changing its meaning.

### F2: Structured errors MUST have one location.

The meaning of failing with a `kind` is fixed in Terms; these clauses say where the error goes: which stream carries the diagnostics (F2a), when the object is required (F2b), and on which stream and line the object sits (F2c), in the structured error envelope (F3).

**F2a** When the tool exits with an error, stderr MUST contain the F3 `message` and, when the error has one, its `hint`, as human-readable diagnostics or inside the F3 JSON object. The meaning of fail with a `kind` is defined in Terms.

**F2b** The tool MUST emit the error object when the selected stdout format is machine-readable or stderr is not a TTY, including under `--quiet`; otherwise the object MAY be omitted. When `--json` appears among the arguments before `--`, a failure raised before the format was resolved MUST emit the object.

**F2c** Whenever the object is emitted, it MUST be the last non-empty stderr line, and a requirement that names an F3 `kind` applies to it. The object MUST NOT be written to stdout.

The same failure with `--json`, as an F3 object on stderr, and at a terminal:

```console
$ mytool deploy api --env prod --yes --json
{"error":{"kind":"image_not_found","message":"Cannot deploy 'api': image 'web:v2.1.0' was not found","hint":"Run mytool images list web"}}

$ mytool deploy api --env prod --yes
Error: Cannot deploy 'api': image 'web:v2.1.0' was not found
Run: mytool images list web
```

### F3: Structured errors MUST have a stable envelope.

**F3a** The document MUST contain exactly one top-level field, `error`, whose value is an object with the fields in the table below. Its `kind` is the only stable match target; message text is not a stable interface.

| Field | Presence | Contract |
|---|---|---|
| `kind` | MUST | String with stable machine meaning; the values in the `kind` table below have shared meanings. |
| `message` | MUST | String identifying the failed operation and cause. |
| `retryable` | MAY | Boolean; `true` only when the same call may resolve the failure without duplicating an intended effect (F3b). |
| `action` | MAY | `agent` when the caller can recover autonomously, `user` when human action is required, or `none` when no recovery action exists. |
| `hint` | SHOULD | String recovery step, present when one concrete step is known (F3b). |
| `context` | MAY | Object containing machine-readable values needed for recovery. R7a, R7d, and M1b require it for the failures they name; F3b and F4b recommend fields in it. |

**F3b** Recovery fields follow these rules:

- `retryable` is `true` only when the same call may resolve the failure without duplicating an intended effect; when it is omitted, the caller has no assurance that a retry is safe.
- `hint` is present when one concrete recovery step is known; omit it rather than guess.
- When `retryable` is `true`, `context` SHOULD carry `retry_after_ms`, a non-negative integer giving the shortest delay in milliseconds after which the same call may be sent again, and, when the tool knows an applicable finite retry limit, SHOULD carry `attempts_remaining`, a non-negative integer giving the number of additional times the caller may send the same call before that limit is exhausted.

**F3c** A tool MUST NOT use a `kind` listed below with another meaning. It SHOULD use a listed `kind` when its definition applies. A requirement that names a `kind` keeps its own keyword. Any other `kind` is tool-defined.

| `kind` | Meaning | Named by |
|---|---|---|
| `invalid_input` | The call is a usage error or exceeds an I7 bound. | I6 |
| `not_found` | A target identified by an argument or flag value does not exist in the state the tool manages or targets. | M1 |
| `conflict` | Authoritative state conflicts with the requested change, such as a concurrent modification or a strict create whose target exists. | R3, R5 |
| `permission_denied` | The caller is identified and the action is forbidden. | |
| `unauthenticated` | The caller could not be identified: credentials are missing, invalid, or expired. | |
| `timeout` | The command's own I8 deadline passed; any intended effect either is known not to have happened or is identified in `context` under R7a. | R7, M1 |
| `unavailable` | A dependency rejected or failed the request transiently, including rate limiting. | |
| `outcome_unknown` | The intended effect may have happened and was not observed. | R7, F4 |
| `interrupted` | The user interrupted the command and F4 does not apply. | F5 |
| `cursor_unavailable` | The cursor is invalid, expired, or incompatible, or required history is missing. | C1, C2 |
| `confirmation_required` | R3 stopped a *gated call* before its effect. | R3 |
| `operation_failed` | A managed operation reached a terminal state other than `succeeded`; `context` carries the identifier and `status`. | R7, M1 |
| `precondition_failed` | A documented precondition that `--force` overrides is not met. | |

A complete error object with every field:

```json
{
  "error": {
    "kind": "image_not_found",
    "message": "Cannot deploy 'web-api': image 'web:v2.1.0' was not found",
    "retryable": false,
    "action": "agent",
    "hint": "Run mytool images list web",
    "context": {"service": "web-api", "image": "web:v2.1.0"}
  }
}
```

### F4: Fallbacks and uncertain outcomes MUST be explicit.

**F4a** If a failed command may have caused an intended effect and did not observe the outcome, it MUST use F3 `kind` `outcome_unknown` and MUST NOT report completion or absence. On failure, a document call writes no JSON value to stdout (O5a), and prior LF-terminated stream records remain valid (O7d).

**F4b** If usable partial results or completed effects survive a failure, the structured error SHOULD identify them in `context`; completed effects go under `completed`, an array of the affected targets as the caller named them.

**F4c** After a failure, a command MUST NOT silently replace the requested target, source, or mode. It MUST fail with a `kind` or identify the substitution in its declared structured result.

A timed-out request whose effect was not observed:

```json
{"error":{"kind":"outcome_unknown","message":"The deployment request timed out and its outcome could not be determined","retryable":false,"context":{"deployment_id":"dep_123"}}}
```

### F5: User interruption MUST fail honestly.

A command that receives the platform's normal user-interrupt request MUST exit without a stack trace and fail with `kind` `interrupted`, unless F4 requires `outcome_unknown`. Interrupting observation of managed work MUST NOT cancel or otherwise change that work.

## Stage H: Human interface

A person at the terminal should find familiar flag names, standard environment behavior, and output that remains readable without color.

### H1: Flags MUST have canonical long names.

**H1a** A flag MUST use kebab-case and have one canonical long name per concept.

**H1b** When the tool supports a concept in the table below, it SHOULD use the listed long name; a requirement that names a flag keeps its own keyword. A listed alias MAY be added when it has no conflicting local meaning. Every accepted alias MUST behave exactly like its canonical flag and appear in the I1 descriptor.

| Concept | Long name | Common alias | Related rule |
|---|---|---|---|
| Help | `--help` | `-h` | D3 |
| Version | `--version` | `-V` | D4 |
| Select a configuration file | `--config PATH` | `-c PATH` | I2 |
| Supply one secret | `--token-file PATH` | No recommendation | I4 |
| Supply structured credentials | `--credentials-file PATH` | No recommendation | I4 |
| Bound a wait | `--timeout DURATION` | No recommendation | I8, M1 |
| Override a documented precondition | `--force` | `-f` | R3 |
| Confirm a prompt | `--yes` | `-y` | R3 |
| Preview a mutation | `--dry-run` | `-n` | R4 |
| Bind consent to a previewed target count | `--expect-targets N` | No recommendation | R3 |
| Keep a repeat from duplicating its effect | `--idempotency-key KEY` | No recommendation | R5 |
| Select JSON or NDJSON output | `--json` | No recommendation | O2 |
| Select a named format | `--format NAME` | No recommendation | O2 |
| Write the result to a path | `--output-file PATH` | No recommendation | O2 |
| More diagnostics | `--verbose` | `-v` | O3 |
| Less diagnostics | `--quiet` | `-q` | O3, F2 |
| Bound returned items or records | `--limit N` | No recommendation | O6, O7 |
| Select first returned records | `--head N` | No recommendation | O7 |
| Select last returned records | `--tail N` | No recommendation | O7 |
| Resume a collection | `--cursor CURSOR` | No recommendation | C1 |
| Resume a stream after a record | `--after-cursor CURSOR` | No recommendation | C2 |
| Keep reading new records | `--follow` | No recommendation | O7 |
| Return after accepting managed work | `--background` | `--bg` | R7 |
| Control color | `--color WHEN` | No recommendation | H4 |
| One item per line | `--plain` | No recommendation | H5 |

### H2: Shell completions.

Tools with nested commands SHOULD provide shell completions.

Static command, flag, and alias candidates match actual behavior under D1.

### H3: External editors and pagers MUST respect user selection.

A tool that starts an editor MUST prefer `VISUAL` to `EDITOR` unless a documented setting overrides them. A tool that starts a pager MUST honor `PAGER` unless a documented setting overrides it.

### H4: Color MUST remain optional.

**H4a** This clause does not govern a *delegating command*'s stdout; it still governs the tool's own stderr decoration. A tool that emits terminal escape sequences MUST omit them all when `TERM` is `dumb` or the output stream is not a TTY, and omit color when `NO_COLOR` is non-empty; other styling MAY remain under `NO_COLOR`. A flag or a documented setting overrides that default on human-readable output only, on each stream classified on its own. Color MUST NOT be the only carrier of information.

**H4b** If provided, the color flag MUST be `--color` with the values `auto`, `always`, and `never`: `auto` applies that default, and the other two override it. `--no-color` MAY be provided as `--color=never`. `--color always` has no effect on `--plain` output or on format selection.

At a terminal, `NO_COLOR=1 mytool services list` and `TERM=dumb mytool services list` print no color. In a pipe, a tool that accepts `--format` keeps color with `mytool services list --format text --color always | less -R` because the output is human-readable, while `mytool services list --color always | jq .` emits none because stdout is then machine-readable under O2 and O3.

### H5: Plain collection output.

A command whose result is a complete collection (O6) SHOULD offer `--plain`.

**H5a** A command whose result is bounded under O6b MAY reject `--plain` for a page that was not selected explicitly with `--limit` or, under C1, `--cursor`, because plain output carries no `has_more`.

**H5b** `--plain` MUST emit one item per LF-terminated line, with no heading or terminal decoration. The line format MUST be documented and stable; if items can contain LF, its escaping MUST be documented. If `--format` exists, `--plain` MUST equal `--format plain`.

Three services, one per line:

```console
$ mytool services list --plain
api
worker
scheduler
```

## Existing tools

This section is the brownfield path. A tool with a released contract can adopt this standard without breaking its callers. Requirements outside this section remain applicable except where this section states what released behavior may be kept. This section defines those exceptions and how the tool declares them, so that a caller reading the D7 index knows what the claim covers. It builds on the conformance claim (D7) and command detail (D8), and its terms, *introspection command* and *reserved field name*, are defined in the core.

### B1: Released behavior MAY be kept where changing it would break callers.

**B1a** A command path, flag, environment variable, or F3 `kind` is brownfield when it preserves a released contract and greenfield otherwise; these words describe the subject, not the tool. A path added after a tool-wide contract was released inherits that contract and is brownfield with respect to it, and greenfield with respect to everything the path itself introduces. A clause in this section applies with respect to the subject it names.

**B1b** A brownfield path MAY preserve established behavior when changing it would break existing callers. Behavior that B3 or B4 lets the path keep does not affect the claim; any other retained behavior that violates a requirement makes the command incompatible, and B2 places it outside the claim. A tool that retains an incompatible command SHOULD also provide a documented conforming path for the same operation.

### B2: The claim MUST exclude incompatible retained commands.

**B2a** A retained incompatible command MUST carry `conforming: false` in its D7 entry, and its retained behavior MUST be documented. Omitted, `conforming` means `true`; a marked entry still carries the required entry fields under D7b. A retained `--json` that requires a value is such a command; it conforms once bare `--json` selects the D8 `output` document.

**B2b** `scope` in D7 `conformance` MAY be present as a non-empty array of non-empty command-path prefixes. When present, the claim covers only a command whose `name` equals one of them or begins with one of them followed by U+0020, and a caller MUST NOT infer anything about commands outside it. A command outside `scope` need not carry `conforming: false`.

**B2c** The claim covers every command not marked under B2a and, when `scope` is present, only the commands within it. `scope` limits only requirements that apply per command. These tool-wide requirements bind regardless of it:

- root help (D3a);
- flag-name consistency and the version flag (D4c, D4d);
- the introspection command, its index, and their stability (D6, D7, D9);
- the exit-code table and the declared default formats (F1a, O2a).

`commands` still lists every command the tool dispatches, and each listed command still returns detail under D6d; for a command outside the claim, whether outside `scope` or marked `conforming: false`, that detail still matches the parser under D6b and is not bound by the D8 contract.

### B3: Released names, defaults, and flag positions MAY be kept.

**B3a** A command path, flag, environment variable, or tool MAY keep the released behavior described for it in the table below in place of the requirement named, under the stated condition. Everything else in the named requirement still applies. Aliases MAY remain for compatibility.

| Requirement | What the subject MAY keep | Condition |
|---|---|---|
| D4b | Released names in place of `get`, `list`, `create`, and `delete`, such as `info`, `ls`, or `add`. | None. |
| D4d | The released form of `tool_version` and of `--version` output. | `--version` output still contains `tool_version` under D7a. |
| D6a | `schema` as the first argument of a released command. | The tool MUST provide introspection in another form, such as a prefix (`tool contract schema`) or a flag (`tool --schema`). |
| I2a | Released environment variable names outside the `<TOOL>_<OPTION>` prefix. | They are still documented under I2a. |
| I2b | A released precedence order among configuration sources. | Resolution is still deterministic and documented under I2b. |
| I4a | Released names for the flags that select a secret source. | The source is still usable in a *non-interactive context* under I4a. |
| I6b | Released flag positions relative to positional arguments. | `--` still ends flag parsing. |
| O2c | A released default format. | A retained non-TTY default MUST be declared in `format_defaults`. |
| O2e | A released `--output` flag, and a released name for a destination path. | None. |
| H1a | A released flag name that is not kebab-case, or more than one long name for a concept. | Every accepted alias still behaves like its canonical flag under H1b. |
| R3d | One flag that both confirms a prompt and overrides a precondition, as an exception to D4c. | The tool MUST document, for each command, whether the flag confirms a prompt, overrides a precondition, or does both. A command added later on that tool MAY use the retained flag under the same rule. |

### B4: A released meaning MAY be kept while this standard's meaning stays reachable.

**B4a** When a released contract assigns another meaning to `1` or `2`, the tool MAY keep it. It MUST then document the code it uses for each F1a meaning, and D7 `exit_codes` carries those substitutes under F1a.

**B4b** A released `kind` whose meaning conflicts with the F3c table MAY be retained; the tool MUST document it as not conforming.

**B4c** Reserved names follow these rules:

- A command that already returns, in its success output, a reserved field name with an incompatible type or meaning MAY retain it when it returns this standard's meaning under another name.
- When a command retains such a field, it MUST then declare the collision in D8 `reserved_overrides`, an object mapping each such reserved field name to the name that carries this standard's meaning.
- Where a requirement names a reserved field name in a D8 schema, in success output, in F3 `context`, or in a collection item, a command with a `reserved_overrides` entry for that name satisfies the requirement under the mapped name, MUST use the mapped name at every such location, and the mapped value keeps the type and meaning the requirement states.
- A command that provides no name for that meaning is incompatible and falls under the exclusion in B2a.
- A caller SHOULD consult `reserved_overrides` before reading a reserved field name from that command's output.

A command whose released output uses `status: "archived"` for a lifecycle state can retain it and return this standard's operation outcome as `state: "succeeded"`. Its D8 detail declares the mapping; this excerpt shows the relevant fields:

```json
{
  "name": "jobs status",
  "reserved_overrides": {"status": "state"}
}
```

Its success result is then `{"job_id":"job_123","status":"archived","state":"succeeded"}`: the caller reads the operation outcome from `state`, and the released `status` field keeps its original meaning.

## Extensions

The two extensions below bind a tool that names them in D7 `conformance.extensions`.

## Extension `continuation`: Bounded reads MUST be resumable.

This extension defines cursor-based continuation for collections (C1) and streams (C2). It builds on the bounds of collections (O6) and streams (O7); its terms, *stream command* and failing with a `kind`, are defined in the core.

### C1: Collections MUST page by opaque cursor.

This requirement applies to a collection bounded under O6b; a collection with a documented finite maximum returns every item and has no page to resume.

**C1a** Contract: the command MUST accept `--cursor`, and each JSON page MUST contain `next_cursor`, an opaque string that the caller passes unchanged to `--cursor`, or `null` exactly when `has_more` is `false`.

**C1b** Binding: source (the command path and the data set it reads), filters, and order MUST remain unchanged when using `--cursor`; the limit and output format MAY change.

**C1c** Failure: if the cursor is invalid, expired, incompatible, or cannot continue the same result set, the command MUST fail with `kind` `cursor_unavailable`. A cursor identifies a position in the documented order; a position the documented order can no longer place cannot continue, and a position immediately after the last item MAY yield an empty final page.

**C1d** Order: pagination MUST use a stable, documented order and document whether pages read live state or one fixed snapshot; whether continuation is exact across concurrent insertions and deletions follows from that choice.

The same page from a tool that declares `continuation`:

```console
$ mytool jobs list --limit 2 --json
```

```json
{
  "items": [
    {"id": "job_123", "status": "running"},
    {"id": "job_456", "status": "queued"}
  ],
  "has_more": true,
  "next_cursor": "cur_abc123"
}
```

### C2: Streams MUST resume after a record.

**C2a** `output` MUST be an object schema with `cursor` as a required string property, and each `cursor` MUST be a non-empty opaque string.

**C2b** The command MUST accept `--after-cursor` and resume strictly after that record without skipping any matching record in the same logical stream. A command that accepts a selector which would omit a matching record after `--after-cursor`, including `--tail`, MUST reject their combination as conflicting inputs under I6a. Inputs selecting the source, filters, or order MUST remain the same; limits, following, timeouts, and output format MAY change.

**C2c** If this continuation cannot be guaranteed because the cursor or required history is invalid, expired, or incompatible, the command MUST fail with `kind` `cursor_unavailable`.

A command schema excerpt with the required `cursor` property:

```json
{
  "stream": true,
  "output": {
    "type": "object",
    "required": ["cursor", "timestamp", "level", "message"],
    "properties": {
      "cursor": {"type": "string"},
      "timestamp": {"type": "string"},
      "level": {
        "type": "string",
        "enum": ["debug", "info", "warning", "error"]
      },
      "message": {"type": "string"}
    }
  }
}
```

A bounded read and its continuation:

```console
$ mytool logs job_123 --limit 2 --json
{"cursor":"cur_101","timestamp":"2026-08-15T10:00:00Z","level":"info","message":"Started"}
{"cursor":"cur_102","timestamp":"2026-08-15T10:00:01Z","level":"info","message":"Fetching input"}

$ mytool logs job_123 --after-cursor cur_102 --limit 2 --json
{"cursor":"cur_103","timestamp":"2026-08-15T10:00:04Z","level":"warning","message":"Retrying"}

$ mytool logs job_123 --after-cursor cur_103 --follow --json
{"cursor":"cur_104","timestamp":"2026-08-15T10:00:09Z","level":"info","message":"Recovered"}
```

Non-normative note: cursor-based resume guarantees a continuation position, not exactly-once delivery. Persisting a cursor only after processing its record minimizes replay; a crash before that checkpoint may cause the record to be received again.

## Extension `managed`: Accepted work MUST be observable.

A caller that holds an identifier under R7 can learn the current state of the work, wait for its terminal state, and, where the tool offers them, cancel or suspend it. It builds on accepted work (R7) and wait deadlines (I8), and its terms, *managed operation*, *accepting call*, and *non-interactive context*, are defined in the core.

### M1: Managed work MUST expose status and wait.

**M1a** Commands: the tool MUST provide commands, usable in a *non-interactive context*, for inspecting current status and waiting for a terminal state. Both MUST accept the identifier returned under R7a; their names follow the naming preferences in D4b. Both commands MUST declare `effects: read_only`. The status command MUST report the current state without waiting for a terminal state.

**M1b** Results: structured results from both commands MUST contain `status` and the identifier. The F3 object of a wait that fails after argument parsing MUST carry the identifier and `status` in `context`; `status` is the last observed state, or `null` when no state was observed, including under M1d `not_found`. The structured success results of both commands SHOULD carry, under O5d and with tool-defined field names, timestamps for when the operation started and when it entered the status the result reports.

**M1c** Status values follow these rules:

- For the status command, the D8 output schema MUST declare `status` as a finite enum containing terminal values `succeeded` and `failed`, plus `canceled` when cancellation exists.
- For the wait command, the success schema MUST declare `status` as an enum listing only the values its success can carry under M1e; other terminal values appear in `context.status` of its F3 object.
- For unfinished states, the status command's enum MAY add values.
- When an existing operation's current state can be indeterminate, the status command's enum MUST add `unknown`.
- Only when the tool successfully observes an underlying state that explicitly represents indeterminacy MAY `unknown` be returned.
- A failure to read state MUST fail with the applicable `kind`.

**M1d** Identifiers: any retention or expiry policy for managed operations MUST be documented. An unrecognized or expired identifier MUST fail with `kind` `not_found`, with no operation state reported.

**M1e** Wait follows these rules:

- The wait command MUST exit `0` only when it observes the terminal state `succeeded`; any other terminal state MUST fail with `kind` `operation_failed`.
- Failure to observe the operation MUST fail with the applicable `kind`.
- When the wait command's I8b `--timeout` deadline expires, it fails with `kind` `timeout`.
- When the operation is already terminal, the wait command MUST return immediately.
- Timing out or terminating the wait command MUST NOT cancel or otherwise change the operation, and MUST NOT be reported as a terminal operation outcome.
- A wait failure that names an existing operation SHOULD give, as its F3b `hint`, a concrete status or log invocation when that invocation can advance recovery; other failures follow F3b without a wait-specific `hint` requirement.

**M1f** Listing: the tool SHOULD provide a command, usable in a *non-interactive context*, that lists managed operations with the identifier and `status`; its result is a bounded collection under O6.

### M2: Cancellation MUST be honest.

**M2a** If a managed operation can be canceled, the tool MUST provide a cancellation command, usable in a *non-interactive context*, whose name follows the naming preferences in D4b. It MUST accept the identifier returned under R7a, declare `effects: idempotent`, and fall under Stage R.

**M2b** Structured success MUST contain `status` and the identifier; `changed` reports whether this call caused a new intended state transition, not whether the managed operation completed (R5a). Exit `0` means cancellation was accepted or a terminal state was observed, not necessarily that the operation was canceled. The command MUST NOT report `canceled` until it observes that state; if the operation wins the race, it MUST report the actual terminal state.

A cancellation that is accepted, then observed by the wait command, which fails on stderr because the terminal state is not `succeeded`:

```console
$ mytool jobs status job_123 --json
{"job_id":"job_123","status":"running"}

$ mytool jobs cancel job_123 --json
{"job_id":"job_123","status":"canceling","changed":true}

$ mytool jobs wait job_123 --timeout 5m --json
{"error":{"kind":"operation_failed","message":"Job job_123 was canceled","context":{"job_id":"job_123","status":"canceled"}}}
```

### M3: Resumable suspension MUST remain distinct from cancellation.

**M3a** If managed work can be suspended and later resumed from the same point, the tool MUST expose suspension and resumption commands, usable in a *non-interactive context*, whose names follow the naming preferences in D4b. Both MUST accept the identifier returned under R7a, declare `effects: idempotent`, and return the identifier, `status`, and `changed` in structured success.

**M3b** They MUST report only a status they observed. While suspended, `status` MUST be `paused` and non-terminal. Cancellation under M2 remains terminal and MUST NOT mean suspension. On an operation that is already terminal, they report the observed terminal state under M2b.
