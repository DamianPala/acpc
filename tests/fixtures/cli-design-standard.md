# CLI Design Standard for Humans and Agents

**Version:** 0.2.0-draft.10

## Scope

This document defines the contract for command-line tools, new or adapted, for reliable use by people and software agents. It includes human-facing behavior, machine-readable introspection, and the minimum safety contract for repeated and mutating operations. The requirements are written for greenfield: a tool or path with no released contract. Brownfield adoption, keeping released behavior that callers depend on, is the subject of [Existing tools](#existing-tools).

Tools may define their own commands and domain-specific behavior.
This standard defines shared interface requirements, not a complete list of tool functions; every command covered by a conformance claim must satisfy the requirements that apply to its behavior.

The standard is independent of the operating system, shell, implementation language, framework, and backend. Arguments, standard streams, environment variables, exit codes, and TTY refer to the equivalent process interfaces on each platform. Examples are non-normative: they explain requirements and add no obligations or exceptions.
Command examples use POSIX-like notation for readability.

## Design principles

This section is non-normative. It records the decisions that shape the standard.

- **A process contract.** The standard binds arguments, the standard streams, environment variables, exit codes, and one introspection command. It requires no daemon, socket, or protocol library, so any tool that can be executed can conform.
- **A testable contract.** Each requirement has an observable pass or fail. A preference without a test belongs in guidance. The introspection index and command detail let a reviewer inspect the tool's declared contracts without invoking domain operations (D6, D7). Checking those declarations alone does not establish that the tool's behavior satisfies them; the agreement rule (D1) still applies.
- **An introspection command, not `--help --json`.** The introspection index and command detail (D6, D7) form a versioned contract (D8), fetched without credentials, configuration, or network (D5), and carry `effects`, `confirm`, and an output schema. Help output is a rendering for people and stays free to change.
- **Context from the stdin TTY, an explicit machine-readable format, and `NO_INPUT`.** [Terms](#terms) define the *interactive context* once. This standard defines `NO_INPUT` as an environment variable whose non-empty value disables prompting regardless of the stdin TTY. Each signal can only force the *non-interactive context*, so none can contradict a TTY; a tool-defined signal for the same context would let two signals disagree. A tool's default format does not force it, so a machine caller that runs the tool in a PTY needs an explicit machine-readable format or `NO_INPUT`.
- **Explicit applicability.** Each requirement states when it applies. Optional extensions name additional guarantees, such as cursor-based continuation or observable background work.
- **An open table of error kinds.** Names in the F3 error table keep their meanings everywhere; any other `kind` is tool-defined and unprefixed. A caller acts on the names it knows either way, and a prefix would only cost the tool.

## Terms

Italics mark a term defined in this section.

- **Caller.** The person or program that runs the tool and consumes its output.
- **Machine-readable, human-readable.** `json` and `ndjson` are machine-readable formats, as is any other format the tool documents as machine-readable. Every other format, including `text`, `plain`, Markdown, and a native document format, is human-readable.
- **Interactive context, non-interactive context.** A call runs in a non-interactive context if stdin is not a TTY, if the call explicitly selects a machine-readable stdout format with `--json` or a `--format` value the tool documents as machine-readable, or if `NO_INPUT` is non-empty (I5a). Otherwise it runs in an interactive context. A machine-readable default format does not by itself make the context non-interactive. These terms govern prompts and consent; the rules for prompts, interactive sessions, and pagers are in I5.
- **Terminal context.** A call runs in a terminal context when it runs in an *interactive context*, stdout is also a TTY, and the selected stdout format is human-readable.
- **Introspection command.** The arguments, excluding the program name, that return the introspection index describing the tool's commands and shared interface settings (D6). It is `schema` unless that name belongs to a released command and another form is required by the retained-name rules (B3).
- **Document command, stream command, silent command, delegating command.** Four shapes of result output: a document, a record stream, no output, or output from a delegated process. The command detail's fields (D7) identify them:
  - a document command has `output` and does not declare `stream: true`;
  - a stream command declares `stream: true`;
  - a silent command has neither `output` nor `delegates_stdout: true`;
  - a delegating command runs another program and passes its stdout through unchanged; it declares `delegates_stdout: true`.
- **Structured output.** Stdout written in a machine-readable format: a single document (O5a) or a record stream (O7). A structured success is a success that writes it.
- **Fail with a `kind`.** To fail with a `kind` is to exit non-zero and, wherever structured errors are demanded (F2), emit a structured error object (F3) with that `kind`; other requirements cite this definition.
- **Reserved field name.** A field name this standard defines with a meaning in result output: `changed`, `correction_result`, `cursor`, `has_more`, `items`, `message_state`, `next`, `next_cursor`, `output_file`, `partial`, `requires_confirmation`, `status`, `targets`, and `truncated`.
- **Intended state.** The state a command's documented purpose is to bring about. State the tool touches only as an implementation detail, such as caches, indexes, telemetry, or metadata used to compute a dependency closure, is not intended state. A dependency target changed as part of the command's documented outcome remains intended state.
- **Gated call.** A call that does not carry `--yes` and would reach an effect requiring confirmation under the mutation safeguards (R2b) or the tool's own policy.
- **Managed operation.** Work that continues after the command that started it exits.
- **Accepting call.** A call that exits after the work it started was accepted and before that work finishes.
- **Core requirement.** A normative statement in a section before [Extensions](#extensions).
- **Extension.** A named section under [Extensions](#extensions). An extension is an optional guarantee that a tool enables by declaring its name in the introspection index's conformance claim (D6); its requirements bind only a tool that declares it.

## Conformance

A conforming tool MUST satisfy every applicable core requirement in this standard. A conditional requirement applies only when its stated condition is true.

A full conformance claim is the D6 `conformance` field. It MUST identify the version of this document the tool was checked against and every extension the tool claims. A claim of any released `0.1.x` version is valid under every later `0.1.x`, because a `0.1.x` release adds no core requirement, strengthens none, and changes no meaning of a name, field, flag, or `kind` a caller may rely on; a change that would do any of these is `0.2.0`.

Draft versions are not covered by this compatibility promise. A claim of a draft version names that draft only; when the release it precedes is published, the tool checks the contract changes recorded between the two before updating its claim, and equivalence of a draft and a release is not assumed. This version defines three extensions under [Extensions](#extensions): [`continuation`](#extension-continuation-bounded-reads-must-be-resumable), [`managed`](#extension-managed-accepted-work-must-be-observable), and [`conversational`](#extension-conversational-agent-conversations-must-support-follow-up-and-correction). A tool MAY claim an extension only if every applicable requirement in its section is satisfied on every command the claim covers.

A tool with a released contract adopts this standard under [Existing tools](#existing-tools), which defines what such a tool may keep and how the claim excludes incompatible retained commands. Every requirement outside that section remains applicable except where that section states what released behavior may be kept.

Tests and conformance tools can verify these requirements, but this document remains authoritative if they disagree.

## Normative language

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be interpreted as described in BCP 14 (RFC 2119 and RFC 8174) when, and only when, they appear in all capitals. Synonymous BCP 14 terms are not used.

## Maintenance

The non-normative [maintenance guide](cli-design-standard-maintenance.md) defines the admission policy and verification workflow for changes to this standard. It is not part of conformance.

## How to read this standard

This section is not part of conformance. Whether a requirement applies is decided by that requirement's own condition, never by this section; the tables below say where to start and which sections to read. For a new tool, start with the sections through [Stage H](#stage-h-human-interface). [Existing tools](#existing-tools) and the extensions follow, and each says when it applies.

An **Applies when:** line under a heading gives the condition for that block; beside a clause ID, it covers only that clause. Conditions within a list or table keep their local scope.

By goal:

| Goal | Where to start | Sections to read |
|---|---|---|
| Implement a new tool | [Scope](#scope) and [Terms](#terms), then the stages in order; the introspection contracts (D6, D7) are what the later stages fill in | [Scope](#scope) through [Stage H](#stage-h-human-interface) |
| Audit a conformance claim | [Conformance](#conformance), then the D6 `conformance` field, then D7 for each covered command | [Scope](#scope) through [Stage H](#stage-h-human-interface); [Existing tools](#existing-tools); each claimed extension |
| Look up a flag name, a command-detail field, or an error `kind` | Canonical flag names (H1b), command-detail fields (D7), and error kinds (F3c) | [Stage H](#stage-h-human-interface), [Stage D](#stage-d-discovery-and-introspection), [Stage F](#stage-f-failure) |
| Adopt a tool with a released contract | [Existing tools](#existing-tools), B1 to B4, before anything else, then as for a new tool | [Existing tools](#existing-tools), then [Scope](#scope) through [Stage H](#stage-h-human-interface) |

By tool shape:

| Tool shape | How the tool shape affects applicability | Sections to read |
|---|---|---|
| Every command read-only | Effect classification (R1), built-in retries (R6), and accepted work (R7) apply under their own conditions; the mutation safeguards and reporting rules (R2 to R5) do not apply | [Scope](#scope) through [Stage H](#stage-h-human-interface) |
| Some commands mutate state | Effect classification (R1) applies to every command; the mutation safeguards and reporting rules (R2 to R5) apply to commands that mutate; built-in retries (R6) and accepted work (R7) apply under their own conditions | [Scope](#scope) through [Stage H](#stage-h-human-interface) |
| A *stream command* | The record-stream rules (O7) apply to that command in place of document encoding, bounded inline values, and collection bounds (O5a, O5c, O6), with the value and timestamp rules (O5b, O5d) applying per record; plain collection output (H5) does not apply | [Scope](#scope) through [Stage H](#stage-h-human-interface) |
| An *accepting call* | The accepted-work rules (R7) apply, as do the external-work bounds (I8) when a call waits | [Scope](#scope) through [Stage H](#stage-h-human-interface) |
| [`managed`](#extension-managed-accepted-work-must-be-observable) claimed | Read the accepted-work rules (R7) first, then the managed-operation rules (M1 to M3); the wait command accepts a timeout (I8b), and operation listings (M1f) follow the collection bounds (O6) and plain-output recommendation (H5) | [Scope](#scope) through [Stage H](#stage-h-human-interface), then Extension [`managed`](#extension-managed-accepted-work-must-be-observable) |
| [`continuation`](#extension-continuation-bounded-reads-must-be-resumable) claimed | Read collection continuation (C1) after collection bounds (O6), for a collection bounded under the finite-window rule (O6b); read stream continuation (C2) after the record-stream rules (O7), for a *stream command* | [Scope](#scope) through [Stage H](#stage-h-human-interface), then Extension [`continuation`](#extension-continuation-bounded-reads-must-be-resumable) |
| [`conversational`](#extension-conversational-agent-conversations-must-support-follow-up-and-correction) claimed | Read managed work first, then session and turn identity, correction, and answer presentation (V1 to V6), and, when a queue is offered, the queue requirements: explicit enqueueing, receipts and inspection, execution policy, and stop and restart (V7) | [Scope](#scope) through [Stage H](#stage-h-human-interface), then Extensions `managed` and `conversational` |

## Stage D: Discovery and introspection

What a tool publishes about itself and what it does at runtime are one contract: the agreement rule (D1) holds help (D3) and the introspection output (D5 to D8) to actual behavior. Names (D4) and breadcrumbs (suggested next commands, D9) make the next call guessable. Discovery has layers (D2); shipped guidance is the layer that adds context beyond the catalogs (D10).

### D1: Published interfaces MUST match actual behavior.

Help, introspection output, static shell-completion candidates, documentation for the installed tool version, and shipped agent guidance MUST agree with the tool and with each other.

### D2: Discovery is layered.

Discovery is layered: `--help` under D3, the introspection command under D5, and optional shipped guidance under D10.

### D3: `--help` MUST be a standalone cheat sheet at every level.

**D3a:** Root `--help` MUST include:

- the tool's purpose and usage;
- its root-level arguments and global flags, with descriptions and applicable defaults;
- its named commands or groups, if any, with a short description of each;
- the literal invocation of the introspection command.

When any command has D7 `output`, root help MUST also point to `--json` under O2b.
Root help SHOULD show how to open help for an individual command.

A catalog may list `services` as "Manage services". When that group requires a subcommand, an executable example uses `mytool services list`, not `mytool services` alone.

**D3b:** Each command MUST provide `--help` that states its purpose and usage and describes its arguments, command-specific flags, and applicable defaults. When the command list grows, help MUST group commands rather than omit them.

### D4: Command and flag names MUST be consistent.

**D4a:** Related commands SHOULD follow a predictable structure (`tool <noun> <verb>` or `tool <verb>`).

**D4b:** For equivalent operations, commands SHOULD prefer `get`, `list`, `create`, and `delete` to `info`, `ls`, and `add`. Managed-operation commands SHOULD use `status`, `wait`, and `cancel`, plus `pause` and `resume` for resumable suspension.

**D4c:** A canonical flag name MUST keep the same meaning wherever it appears.

**D4d:** Root `--version` MUST exist. `tool_version` SHOULD follow Semantic Versioning 2.0.0 or the platform's canonical form of it, such as PEP 440, in either case without a `v` prefix, and `--version` SHOULD print exactly that string followed by a newline.

### D5: The introspection command MUST expose the command interface as JSON.

**D5a:** Entry: the introspection command MUST be unambiguous from the arguments alone and MUST NOT dispatch to a domain operation. `schema` as the first argument MUST be reserved for introspection. The introspection command MUST take precedence over any command name the tool resolves from caller configuration or from an executable on the caller's `PATH`.

**D5b:** Consistency: command paths, arguments, flags, and defaults exposed by the introspection output MUST match the parser used at runtime.
Shared definitions and generation help keep introspection synchronized with the runtime parser. They do not by themselves establish that the tool's behavior satisfies this standard.

**D5c:** Execution: the introspection command MUST write only JSON to stdout and require no application authentication, configuration, network access, or prompts. It MAY accept global flags without applying them.

**D5d:** Routing: the introspection command alone MUST return the introspection index (D6).
When followed by a command path, it MUST return command detail.
For a command outside the conformance claim, that detail still matches the parser (D5b), but the D7 requirements do not apply (B2c).
A command path consists of the segments in D6 `commands[].name`, separated by spaces (U+0020).
Each segment MUST be passed as a separate argument in that order.
For a tool with one unnamed command (D6a `command`), any argument after the introspection command is an unknown path.

**D5e:** Errors: an unknown path MUST be a usage error. The error MAY suggest valid command paths. A recovery hint can direct the caller to invoke the introspection command without a path to read the command index (D5d, F3b).

### D6: The introspection index MUST let callers choose a command safely.

**D6a:** The index fields are defined below.

| Field | JSON type | Presence | Value and meaning |
|---|---|---|---|
| `schema_version` | string | MUST, always | Introspection format version. |
| `tool_version` | string | MUST, always | The version string, which `--version` output MUST contain; D4d recommends printing exactly that string. |
| `conformance` | object | MUST, always | Conformance claim: the standard version checked against, the claimed extensions, and an optional coverage scope (D6c). |
| `global_flags` | array | MUST, always | I1 flag descriptors accepted by every command. D7 `flags` MUST NOT repeat them. Excludes `--help` and `--version`, which D3b and D4d already require. |
| `format_defaults` | object | MUST, always | O2 default format for each output context. |
| `exit_codes` | object | MUST, always | F1 tool-wide exit code meanings. MUST contain the decimal-string keys of the F1a table and the substitutes B4a requires. |
| `commands` | array | MUST, always | Flat routing list of every command the tool itself dispatches, by full path, sorted by `name` in Unicode code point order; excludes the introspection command. Group prefixes that dispatch nothing themselves are not entries. Commands provided by external executables MAY be omitted only when the tool itself dispatches at least one command and the omitted executables are optional additions installed by the caller; the tool MUST document that they are omitted. |
| `command` | object | MUST, exactly when the tool has one unnamed command | Detail of the unnamed command. D7 binds it when the claim covers the command; outside the claim it matches the parser under D5b instead (B2c). |

**D6b:** Each command entry MUST contain `name`, `description`, and `effects`, and MUST NOT contain invocation details. `name` MUST consist of non-empty segments separated by exactly one space (U+0020), or be `""` for the unnamed command, and MUST be unique within `commands`. `description` MUST distinguish neighboring commands, and `effects` is the R1a value. An entry MAY contain `conforming: false` under B2a; omitted, it means `true`.
A routing shortcut that dispatches an argument given without a command name to a named command is described in the root help (D3) and in that command's `description`, not as an index entry.

**D6c:** In the `conformance` object, the fields `name` and `standard` MUST equal `cli-design-standard` and the version of this document the tool was checked against, respectively. `extensions` MUST be present and list each claimed extension exactly once.
It is an empty array when none is claimed.
A caller that does not recognize an extension name MUST NOT rely on that extension. `scope` MAY be present as command-path prefixes, with its shape and command coverage defined in B2b, and limits only requirements that apply per command (B2c).

### D7: Command detail MUST expose the command contracts.

For conditional fields in this table, the presence condition is exact: the field MUST NOT be present when its condition is false.

| Field | JSON type | Presence | Value and meaning |
|---|---|---|---|
| `name` | string | MUST, always | Full command path, or `""` for a tool with one unnamed command. |
| `description` | string | MUST, always | Command purpose and any additional command behavior explicitly required in this field. |
| `args` | array | MUST, always | I1 positional argument descriptors, in argument order. |
| `flags` | array | MUST, always | I1 command-specific flag descriptors. Effective flags are D6 `global_flags` plus this array. |
| `effects` | string | MUST, always | The R1a value: `read_only`, `idempotent`, or `non_idempotent`. |
| `confirm` | boolean | MUST, always | MUST be `true` when at least one valid call may require `--yes` under R3a and `false` otherwise. |
| `interactive` | boolean | MUST, always | MUST be `true` when at least one valid call starts an interactive session under I5b and `false` otherwise. |
| `stream` | boolean | MUST, when `delegates_stdout` is not `true`, and success emits a record stream | MUST be `true`. `output` describes one record; the stream is bounded and framed under O7. |
| `delegates_stdout` | boolean | MUST, when every successful call writes to stdout only the unmodified stdout of a process the command runs | MUST be `true`. `output` and `stream` MUST NOT be present; stdout stays the contract of the process the command runs (O2f). |
| `output` | object | MUST, when the command can return result data on success or failure, or `changed` is required under R5a, and never when `delegates_stdout` is `true` | O4 JSON Schema shared by success and failure result documents, or describing one O7 record. When R5a requires `changed`, MUST declare it with type `boolean`, or with type `["boolean", "null"]` only when R5a permits `null`. |
| `output_description` | string | MAY, when `output` is present; required for success-only fields or failure results (O4d, O5a) | Result behavior and field meanings not expressed by the schema or this standard. |
| `exit_codes` | object | MUST, when the command adds or refines tool defaults | F1 additions or refinements that preserve tool-wide meanings. |
| `format_defaults` | object | MUST, when the command default differs from the tool default | O2 output defaults. |
| `reserved_overrides` | object | MUST, when the command retains a reserved field name under B4c | B4c map from each such reserved field name to the name that carries this standard's meaning. |

**Example: the index, command detail, and calls.** This example shows how the index and command detail fit together; the contract is in D6 and D7. The `mytool` examples in this document are independent fragments; each agrees with its own requirement, not with the others.
Fields follow the table order for readability; JSON object order is not part of the contract.

```console
$ mytool schema
```

```json
{
  "schema_version": "1",
  "tool_version": "2.1.0",
  "conformance": {"name": "cli-design-standard", "standard": "0.2.0-draft.10", "extensions": []},
  "global_flags": [
    {"name": "json", "description": "Emit JSON output", "type": "boolean",
     "required": false, "default": false}
  ],
  "format_defaults": {"tty": "text", "non_tty": "json"},
  "exit_codes": {"0": "success", "1": "failure", "2": "usage error"},
  "commands": [
    {"name": "services get", "description": "Get a deployed service", "effects": "read_only"},
    {"name": "services list", "description": "List deployed services", "effects": "read_only"}
  ]
}
```

`services get` is one entry with two segments, fetched as `mytool schema services get` (D5d):

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

**Example: a tool with one unnamed command.** There is no command path, so its index carries the detail:

```console
$ hashfile schema
```

```json
{
  "schema_version": "1",
  "tool_version": "1.0.0",
  "conformance": {"name": "cli-design-standard", "standard": "0.2.0-draft.10", "extensions": []},
  "global_flags": [
    {"name": "json", "description": "Emit JSON output", "type": "boolean", "required": false, "default": false}
  ],
  "format_defaults": {"tty": "text", "non_tty": "json"},
  "exit_codes": {"0": "success", "1": "failure", "2": "usage error"},
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

`hashfile README.md` runs the command. `hashfile schema README.md` is an unknown path under D5d.

### D8: The introspection contract MUST be stable and versioned.

**D8a:** `schema_version` MUST be a positive decimal integer encoded as a string. Callers should ignore unknown fields. A change that adds an optional introspection or output field MAY keep `schema_version` unchanged. A change that adds a required introspection field, removes an introspection field, or changes an introspection field's type or meaning MUST increment it.

**D8b:** Tool versions documented as compatible MUST preserve existing command paths, input meanings and defaults, exit code meanings, F3 `kind` meanings, and structured output fields. An incompatible change MUST document its replacement and migration; changing `schema_version` alone is insufficient.

### D9: A workflow with one unambiguous next step SHOULD expose it as a breadcrumb.

**D9a:** The `next` field follows these rules:

- In structured result output or a structured error's `error.next` field (F3), `next` MUST be a non-empty array of strings forming a complete argument list (`argv`): the executable followed by its arguments, ready to execute without a shell.
- For the executable, `next[0]` is the tool's canonical executable name; a caller that invoked the tool by path substitutes it.
- If the next call needs a credential or configuration source selected by a flag on the initiating call, `next` MUST include that flag with a value selecting the same source. It MAY use `-` for stdin. A path value MUST use the resolved location. `next` never contains the data read from stdin.
- A command that may emit `next` in result output MUST declare it as an optional field in its D7 output schema. Emitting `error.next` alone does not require that declaration; the error field is defined by the shared error envelope (F3a).

**D9b:** Human-readable output SHOULD render the same invocation as `Next: ...` and safely quote caller-controlled values.

**D9c:** Output MUST omit `next` when there is no natural continuation.
In an error, `next` names a recovery step. It does not supply consent to execute that step or bypass its safeguards.

**Example: a suggested next command.** A queued deployment renders the breadcrumb at a terminal:

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

### D10: Agent guidance MUST add context.

The tool MAY ship `SKILL.md` or `AGENTS.md` when domain knowledge or workflows need explanation. Guidance MUST point to commands and the introspection output, and SHOULD NOT duplicate their catalogs.

## Stage I: Invocation and input

A caller may be a sandboxed process with no keyboard, a producer of generated bytes, or both. Input handling treats those bytes as data, not shell syntax. The tool resolves declared inputs, validates them before acting, and keeps external work bounded.

### I1: Every accepted argument and flag MUST have a defined input contract.

**I1a:** D6 `global_flags` and D7 `args` and `flags` MUST describe each input as a JSON object, called a descriptor, with the fields below.

| Field | JSON type | Presence | Value and meaning |
|---|---|---|---|
| `name` | string | MUST, always | The input's name. Flag names omit leading hyphens. A one-character name such as `n` renders as `-n`; a longer name such as `verbose` renders as `--verbose`. |
| `description` | string | MUST, always | One line stating what the input controls. |
| `type` | string | MUST, always | Type of each input value: `string`, `integer`, `number`, or `boolean`. For a variadic argument or repeatable flag, this is the type of each array item. |
| `required` | boolean | MUST, always | `true` means the caller must provide the input. |
| `default` | string, number, boolean, or array | MUST, when the parser supplies an actual default | The built-in default; values calculated at run time and internal markers for missing input are excluded (I1c). |
| `enum` | array | MUST, when the parser restricts values to a list of choices | Lists every choice. Every item MUST match `type`. |
| `aliases` | array | MAY, on flags | Alternative names without leading hyphens, rendered by the same rule as `name`. If omitted, there are no aliases. |
| `variadic` | boolean | MAY | MUST NOT be `true` except on the last positional argument. When `true`, that argument collects the remaining positional values into an array in input order; with `required: true`, at least one value is required. If omitted, means `false`. |
| `repeatable` | boolean | MAY | MUST NOT be `true` except on flags. When `true`, the flag can appear multiple times, and its values form an array in input order. If omitted, means `false`. |
| `accepts_stdin` | boolean | MUST, when `-` selects stdin instead of a file | `true` when `-` selects stdin instead of a file. If omitted, means `false`. |

**Example: multiple files and tags.** An upload command declares a positional argument for files and a repeatable flag for tags.
The following JSON is a fragment of its command detail:

```json
{
  "args": [
    {
      "name": "file",
      "description": "Files to upload",
      "type": "string",
      "required": true,
      "variadic": true
    }
  ],
  "flags": [
    {
      "name": "tag",
      "description": "Tag to attach",
      "type": "string",
      "required": false,
      "aliases": ["t"],
      "repeatable": true
    }
  ]
}
```

```console
$ mytool upload --tag docs -t archive a.txt b.txt
```

This call resolves `file` to `["a.txt", "b.txt"]` and `tag` to `["docs", "archive"]`.
`--tag` and `-t` are two names for the same flag, so their values go into the same array.

**I1b:** A boolean flag given without an explicit value, such as `--verbose`, MUST set its value to `true` and MUST NOT consume the next argument as its value.
It MAY also accept both `--name=true` and `--name=false`.

For example, if the upload command also accepts a boolean `--verbose` flag, `mytool upload --verbose a.txt` sets `verbose` to `true` and keeps `a.txt` as a file argument.

**I1c:** The `default` field describes a built-in default, with these rules:

- Required inputs MUST NOT have a default.
- A default that is not an array MUST match `type`.
- Defaults for variadic arguments and repeatable flags MUST be arrays whose items match `type`.
- A default that applies unless another flag is present MAY be declared as `default`, with that condition stated in `description`.
- Values resolved at run time, from configuration sources (I2) or the environment, such as the working directory or the newest available version, MUST be omitted from `default` and described in `description`.
- An internal parser marker meaning "not provided", including `null`, is not a default and MUST be omitted from `default`.

For example, an optional integer flag can declare `"default": 3`; an optional repeatable string flag can declare `"default": ["docs"]`.
If an omitted path is resolved to the current working directory, its descriptor leaves out `default` and explains that behavior in `description`.

**I1d:** The `description` field explains input rules that the other fields do not express:

- Any accepted value syntax, limit, rule for resolving paths, dependency, conflict, or way to look up currently available values not expressed by another descriptor field MUST be stated in `description`.
- Dependencies include how inputs affect a running command. For example, an explicit `--limit` ends a `--follow` read (O7b). An observation deadline ends the wait but does not cancel work that can continue after the call exits (R7d).
- For a relationship between a global flag and a command-specific input, the command-specific input's `description` MUST state the relationship.
- When both related inputs are global or both are command-specific, at least one of their descriptions MUST state the relationship.

For example, when `--follow` and `--limit` are both command-specific flags, the `--follow` description can say: "Read new records as they arrive. An explicit --limit stops the read after that many records."
This explains their interaction before the caller runs the command.

### I2: Configuration MUST be declared, deterministic, and inspectable.

**I2a:** Sources: accepted configuration files, secret sources, and tool-specific environment variables MUST be documented, and an undeclared tool-specific variable MUST NOT change behavior. Tool-specific variables SHOULD use a consistent `<TOOL>_<OPTION>` prefix, such as `MYTOOL_LOG_LEVEL`.
A tool whose behavior `NO_INPUT` changes, through prompting (I5a), editors (I5b), pagers (I5e), or another interactive feature, MUST also document that effect.
If a flag selects a configuration file, the tool MUST document whether that file replaces or augments project and user configuration and where it sits in the precedence.

**I2b:** Precedence: when more than one source provides a value, resolution MUST be deterministic and documented. It SHOULD follow this order: `flags > environment > project configuration > user configuration > built-in defaults`.

**I2c:** Inspection: layered configuration SHOULD expose each resolved value and its source and, when the source has a location, its resolved location, while masking secrets. The complete argument list for the next call relies on the same record of where each setting came from (D9a).

For example, `--log-level debug` overrides `MYTOOL_LOG_LEVEL=info`, which overrides `log_level = "warning"` in project configuration and `log_level = "error"` in user configuration.

### I3: Long or generated input MUST use files or stdin, never argv.

**I3a:** **Applies when the command accepts text it interprets itself, such as a document, script, request body, or template, of unbounded length.** The command MUST accept a file path and `-` for stdin.
An input whose format the tool detects from the file rather than text it interprets itself, such as a PDF or an image identified by path, meets this rule's purpose through the path alone; the command MAY also accept `-` for it.

**I3b:** The tool MUST reject a call that selects stdin for more than one input.

**Example: a manifest from a file or stdin.** Its quotes, newlines, and flag-like text remain data rather than argv:

```console
$ mytool deploy service-a --env prod --manifest manifest.json
$ mytool deploy service-a --env prod --manifest - < manifest.json
```

### I4: Secrets MUST NOT be passed as argument or flag values.

**I4a:** A command that needs a secret MUST provide a source usable in a *non-interactive context*, such as a credential store, file, stdin, or environment variable. Commands SHOULD use `--token-file PATH` for one secret and `--credentials-file PATH` for structured credentials; either flag SHOULD accept `-` for stdin. Secret-source precedence resolves deterministically and as documented under I2b.

**I4b:** A secret MUST NOT appear in help, introspection output, logs, errors, or normal output unless retrieval is the command's documented purpose.

With `--token-file token.txt` under `--verbose`, a diagnostic may name the source, such as `Using token from token.txt`, but not the token.

### I5: Behavior in a non-interactive context MUST be explicit and fail safely.

**I5a:** Prompts ask a person for input or confirmation and follow these rules:

- A command MAY prompt in an *interactive context*; in a *non-interactive context*, it MUST NOT prompt. As defined in [Terms](#terms), stdin that is not a TTY, an explicitly selected machine-readable stdout format, or a non-empty `NO_INPUT` makes the context non-interactive. A machine-readable default alone does not.
- While stdin supplies an input, a command MUST NOT prompt, even if stdin is a TTY.
- A prompt written to the command's own streams MUST go to a stream that is a TTY.
- Asking through another channel also counts as prompting. This includes an askpass helper or a separate authorization process that may ask a person. The tool MUST document each such channel.
- If required input or confirmation is still missing, the command MUST fail with an error that names a flag, file, stdin form, or environment variable that can supply it. Requests first discovered after work was accepted follow the late-input rule (I5d).
- A command MUST NOT treat a missing prompt, an unanswered prompt, or end of input as consent.

**Example: confirmation when stdin supplies data.** Assume deployment requires confirmation and stdin supplies the manifest. The command cannot prompt, so it fails and names the explicit confirmation flag, `--yes` (R3):

```console
$ mytool deploy api --env prod --manifest - < manifest.json
Error: Deploying 'api' to prod requires confirmation
Run: mytool deploy api --env prod --manifest - --yes < manifest.json
```

**I5b:** Interactive sessions include starting an external editor.
A command MAY start an interactive session only in a *terminal context*: an interactive context where stdout is also a TTY and the selected stdout format is human-readable.
If the call would start a session outside that context, it MUST fail before side effects.
The command's `interactive` field declares whether at least one valid call starts such a session (D7).

For example, if `mytool config edit` opens an editor, `mytool config edit --json` fails before opening it or changing configuration.

**I5c:** Actions outside the CLI: a workflow MAY require a person to act elsewhere, such as approving access in a browser or on another device.
Such a workflow follows these rules:

- The command that starts the workflow (its entry point) MUST work in a *non-interactive context* and MUST be listed in the introspection index's `commands` array (D6).
- Work that continues after that command exits stays identifiable under R7. If the tool claims the `managed` extension for that work, the extension also applies.
- In a *non-interactive context*, a command that would wait for that action MUST fail before side effects with an error naming the workflow's entry point or a secret source that resolves the requirement (I4). This restriction does not apply when waiting for that action is the command's documented purpose. Requests first discovered after work was accepted follow the late-input rule (I5d).

**Example: browser authorization.** Suppose a tool offers a browser authorization workflow through `auth start` and a command documented to wait for approval, `auth wait`.
The following calls run in a non-interactive context because they use `--json`:

| Call | Behavior in this example |
|---|---|
| `mytool deploy api --json` without credentials | Fails before deployment and points to `mytool auth start`. It does not wait for browser approval. |
| `mytool auth start --json` | Starts authorization, returns its identifier and the URL for the person to open, then exits. |
| `mytool auth wait auth_123 --timeout 5m --json` | Waits for approval of `auth_123`, returned by `auth start`. Waiting is this command's documented purpose, so it is allowed; the timeout bounds the wait (I8). |

**I5d:** **Applies when accepted work later needs caller input or permission.** The tool MUST handle the request in one of these ways:

- Resolve it under the selected policy.
- Prompt through an available channel when the prompt rules permit it (I5a).
- Reject it.
- Expose it with a documented way to answer in a *non-interactive context*.

**Authorization:**
The requested action MUST NOT run without the required input or permission.
This does not undo earlier authorized effects.
A machine-readable request is not itself a prompt. A channel that asks a person still follows the prompt rules (I5a).

**Waiting for a response:**
In a *non-interactive context*, the initiating call MUST return control instead of waiting for a person.
It can wait only when that is its documented purpose under the rule for actions outside the CLI (I5c).
Work retained for a separate response remains identifiable under the accepted-work rules (R7).

**Rejection:**
If work cannot continue after rejection and the caller has no way to answer, the tool MUST initiate termination instead of waiting indefinitely.
It MUST report the missing input and any observed work state.
Requesting termination does not establish a terminal state.

This rule requires no deferred-input queue or particular response command.

**Example: a permission request after acceptance.** An agent has read a file and now requests a write.
The selected policy rejects the write, but the agent can finish its answer and explain the restriction. The tool does not need to terminate the work.
If the agent instead needs an environment choice to proceed, the tool provides a supported response path or starts termination and reports the missing choice.
It does not wait for input from a terminal that is no longer connected.

**I5e:** Pagers display output one screen at a time.
A pager MAY start only in a *terminal context*, the same condition used for sessions in I5b.
The choice of pager follows the user's selection (H3).

### I6: Invalid input MUST fail before side effects.

**I6a:** The tool MUST reject unknown flags, unsupported arguments, invalid values, and conflicting inputs as usage errors. A usage error MUST use F3 `kind` `invalid_input`.

**I6b:** `--` MUST end flag parsing, so later tokens are positional. A command SHOULD accept flags before, between, and after positional arguments up to `--`.

**I6c:** The tool MUST validate every locally checkable input before changing state.

**I6d:** A usage error SHOULD identify the invalid input and show the accepted form or nearest valid name.

### I7: Input MUST stay within its declared bounds.

**I7a:** **Applies when the command buffers caller-controlled input.** It MUST enforce a maximum size before side effects and state it under I1d in the `description` of each input it bounds.

**I7b:** **Applies when the command limits paths to declared roots.** It MUST resolve each supplied path and reject it when it escapes those roots, including through `..` or a symbolic link.

**Example: input size and path limits.** Assume `mytool upload` accepts at most 10 MiB and may read files only from `./dist`; both calls fail with an F3 object on stderr:

```console
$ mytool upload ../secrets.txt --json
{"error":{"kind":"invalid_input","message":"Path must resolve inside ./dist"}}

$ cat 20-mib.bin | mytool upload - --json
{"error":{"kind":"invalid_input","message":"Input exceeds the 10 MiB limit"}}
```

### I8: External work MUST be bounded or explicitly unbounded.

**I8a:** Connection establishment and non-streaming network operations MUST use finite default timeouts. A command that waits for external state, when waiting is not its documented purpose, MUST use a finite default deadline. A wait for a human action falls under the human-action rule, I5c.

**I8b:** Waiting and external work follow these rules:

- A command whose documented purpose is to wait, watch, follow, or run external work to completion MAY wait indefinitely by default, but MUST accept `--timeout`. The flag's input descriptor (I1) MUST either declare a finite default deadline in `default` or say in `description` that the wait is unbounded by default.
- `--timeout` MUST accept an integer immediately followed by `s`, `m`, or `h` and MAY accept other forms, such as a bare integer with a documented unit.
- For a command that accepts `--verbose` and may wait longer than 10 seconds, enabling `--verbose` SHOULD produce a stderr line at least every 30 seconds to show that the command is still waiting (O3b). That command SHOULD document the interval or state that it emits no such lines.

**I8c:** Any other unbounded mode MUST require explicit selection.

**Example: bounded and unbounded waits.** The first call sets a deadline; the second explicitly selects an unbounded mode:

```console
$ mytool jobs wait job_123 --timeout 5m
$ mytool logs job_123 --follow
```

**Example: declaring an unbounded default wait.** The `--timeout` descriptor carries no `default` and says the wait is unbounded by default:

```json
{"name": "timeout", "description": "Give up after this duration, such as 30s or 5m; unbounded by default", "type": "string", "required": false}
```

## Stage R: Repeatability, mutation safety, and accepted work

This stage covers effect classification, safeguards and reporting for mutations, internal retries, and identification and outcomes of started work.
Safeguards follow the possible damage, not merely whether a command changes state. A tool whose every command declares `effects: read_only` has no mutation requirements under R2-R5. The rules for safe, bounded retries (R6) apply when the tool retries requests internally. The rules for accepted work (R7) cover identification, completion, and deadlines.

### R1: Declared effects MUST cover every valid call.

**R1a:** Every D6 command entry and D7 command detail MUST declare one value from the table below.

| `effects` | Contract |
|---|---|
| `read_only` | The command does not change intended state. |
| `idempotent` | The command may change intended state, but repeating a successful call with the same inputs, without an intervening change to the targeted state, MUST succeed unless an independent failure prevents execution, such as rate limiting or an unavailable dependency. A successful repeat MUST leave the intended state as the first call left it and MUST NOT repeat any other documented effect. A failure caused by the first call's effect, such as `not_found` after deletion, violates this guarantee. Recomputing or rewriting only that same intended state does not violate this guarantee. |
| `non_idempotent` | The command meets neither the `read_only` nor the `idempotent` guarantee; a repeat may cause another intended effect or return a stable conflict. |

**R1b:** The declaration MUST cover every valid call. Declare `non_idempotent` when any call lacks the repeat guarantee. Otherwise declare `idempotent` when any call can change intended state. Otherwise declare `read_only`. A changing response, incidental telemetry, logs, caches, metering, or rate limiting do not alone change the classification.
A command whose repeat deterministically fails with `kind` `conflict` and changes nothing stays `non_idempotent`; its command-detail `description` (D7) SHOULD say so, because the value alone does not tell a caller a harmless refusal from a duplicated effect.

For example, a delete by stable identifier whose repeat succeeds with `changed: false` is `idempotent`; a delete whose repeat fails with `not_found` is `non_idempotent`. Runtime target selection alone does not determine the classification; the successful-repeat guarantee (R1a) does. Deleting the newest item is `non_idempotent` when an immediate repeat selects and deletes the next item.
A deploy that starts a second deployment on repeat and an import that refuses to replace an existing named resource on repeat both declare `non_idempotent`; only the second command's description can say "A repeat with the same inputs fails with `conflict` and changes nothing".

A status read that only records that a worker has already exited can remain `read_only`; stopping the worker as part of the read changes intended state.

### R2: Mutation safeguards MUST match the possible damage.

**R2a:** A mutation is wide when it can affect existing targets the caller did not name individually. Targets necessarily included by a documented dependency relationship from individually named targets do not by themselves make a mutation wide. It is irreversible when the tool provides no documented operation that restores the prior state.
Retaining input or an execution history does not by itself make a mutation irreversible; assess the state changes the input requests or authorizes.

**R2b:** A mutation MUST carry at least the minimum safeguard listed for its operation in the table below; idempotence does not reduce these safeguards.
For separately governed agent actions, the permission-policy alternative (R2d) applies instead.

| Operation | Minimum safeguard |
|---|---|
| `effects: "read_only"` | None |
| Narrow, reversible mutation | None |
| Narrow, irreversible mutation | Confirmation under R3b |
| Narrow, irreversible mutation that only overwrites a destination the same call selects, is refused by default with `kind` `conflict` before any effect, and is enabled by `--force` | `--force` under the precondition-override rule (R3d); confirmation under the gated-call rule (R3b) is not required |
| Wide mutation | Confirmation under R3b, and `--dry-run` under R4 |

**R2c:** When the tool claims the `managed` extension, canceling exactly one managed operation does not require confirmation under the mutation-safeguard table (R2b).
This exemption applies only when the caller uses the operation's full identifier (R7a), or a documented selector bound to that operation for the call.
Any wider cancellation follows the table.
The tool's own policy may still require confirmation.
If a command cancels work and performs another action, the exemption covers only the cancellation. The other effects keep their safeguards.

**R2d:** **Applies when a command lets an agent choose actions under a separate permission policy.** The command MAY use that policy instead of previewing and confirming the agent's whole future plan through the CLI (R3, R4), under these conditions:

- **Selection:** The policy MUST define the permitted resources and actions, and either be explicitly selected by the caller or default to no changes beyond recording the conversation. Read-only actions permitted by the policy do not violate this default.
- **Enforcement:** The tool MUST enforce the policy before an action can exceed its limits, including an action through a tool or subprocess. An action outside the policy requires separate explicit permission.
- **Inspection:** Command detail MUST explain how to select and inspect the policy in a *non-interactive context*. This explanation includes any restrictions the tool cannot enforce and any conditions needed for enforcement. For accepted work, inspection MUST show the policy that applies to that work. This inspection can be a field of any documented session read or a separate command; no particular command is required.
- **Unavailable enforcement:** If the tool cannot enforce the selected policy, the command MUST fail before starting agent work. If the tool detects that enforcement was lost during work, it MUST prevent every further action it can still control and report, in the call's result or error or through the documented inspection of accepted work, the loss, the last known state, and every running tool or subprocess whose further actions it could not prevent or verify. It cannot claim that the work stopped or complied with the policy unless it verified that fact.

Documenting an unsupported restriction does not make it an enforced boundary.
The tool can rely on controls in its execution environment; no particular enforcement mechanism is required.
Later input requests follow the rule for input needed after acceptance (I5d).
Effect declarations and direct CLI mutations keep their safeguards (R1, R2b).
An offered `--dry-run` still follows the preview rules (R4b).

**Example: a policy with an unsupported restriction.** A tool can enforce workspace-only writes but cannot block network access.
Its command detail states that limitation.
A caller selecting a policy that also requires network isolation receives an error before agent work starts.

### R3: Safety gates MUST fail closed and be discoverable.

Confirmation follows four rules:

1. The prompting policy determines whether the command may ask the user for confirmation (I5a).
2. A call requiring confirmation stops before side effects if it cannot prompt or the user declines. Checks that precede the protected action still run first (R3b).
3. A `--dry-run` call does not require confirmation; its result reports whether the mutation would require it (R3c, R4c).
4. `--yes` supplies confirmation. `--force` overrides a documented precondition (R3d).

**R3a:** Every command that can require `--yes`, including those required under R2b, MUST accept `--yes`; the D7 `confirm` field carries the declaration.

**R3b:** A *gated call* that cannot prompt under I5a, or whose prompt is declined, MUST stop before side effects and fail with `kind` `confirmation_required`, naming `--yes`. The gate applies after every check the call performs before side effects. A call that would fail before reaching the gated effect, such as on a missing target, MUST fail with the `kind` for that earlier failure without requiring `--yes`.
Before deciding whether a wide mutation without `--yes` needs confirmation, the call MUST resolve its target set.
If that set is empty, no other effect requires confirmation, and no other check fails, the call MUST succeed without prompting or mutating targets that appear later.
Confirmation protects an effect; a call that has established that no protected effect will occur does not need consent for it.

For example, pruning an empty set succeeds with `changed: false`; pruning a non-empty set without consent fails with `confirmation_required`.
A failed target read still reports its own error. A preview of the empty no-op reports `requires_confirmation: false` under the preview-result rules (R4c).

**R3c:** A `--dry-run` call MUST NOT require `--yes`. A command that accepts `--yes` MUST accept it with `--dry-run` and ignore it.

**R3d:** `--yes` confirms a prompt; `--force` overrides a documented precondition. Accepting one MUST NOT enable the other. The overwrite row of the mutation-safeguard table (R2b) is the one case where `--force` alone is a sufficient safeguard, because the refused precondition is the only damage the call can do. The name of a flag inherited from an upstream contract the tool does not own does not by itself assign either role; its documented behavior determines whether either role applies.

**R3e:** Checking the expected target count: **Applies when a command for a wide mutation under R2a accepts `--expect-targets N`,** where `N` is a non-negative integer. When the flag is given, the mutating call MUST complete these checks before its first side effect:

- resolve the full target set using the same rules as the preview's `targets` array (R4c);
- count the distinct targets and fail with `kind` `conflict` if the count differs from `N`.

The count does not bind target identities: a preview of A and B and a later mutation of C and D both have two targets.

**Example: an unmet precondition.** A documented precondition that `--force` overrides is not met, so the call fails with an F3 object on stderr and names the override:

```console
$ mytool services delete api --json
{"error":{"kind":"precondition_failed","message":"Service 'api' still has 2 running deployments","hint":"Repeat with --force to delete it anyway"}}
```

**Example: missing confirmation.** A *gated call* in a *non-interactive context*, here selected by `--json` with stdin redirected from `/dev/null`, stops before side effects and fails with a structured error object on stderr (F3):

```console
$ mytool services prune --env staging --json < /dev/null
{"error":{"kind":"confirmation_required","message":"Pruning staging services requires confirmation","hint":"Repeat with --yes"}}
```

### R4: Wide mutations under CLI safeguards MUST be previewable.

**R4a:** **Applies when the command performs a wide mutation governed by the CLI safeguards (R2b).** The command MUST provide `--dry-run`.

**R4b:** **Applies when the command accepts `--dry-run`.** A `--dry-run` call MUST leave intended state unchanged and follow these outcome rules:

- The call never fails with `confirmation_required` (R3c).
- If the mutating call would fail before side effects for a reason other than a permission check, fail with the mutating call's `kind`.
- If the preview performs a permission check and it fails, fail with the mutating call's `kind`.
- Otherwise succeed, reporting the gate under R4c.
- The call SHOULD apply the same permission checks as the mutating call; an omitted permission check does not require predicting its result.

**R4c:** **Applies when a `--dry-run` call returns structured success.** The result MUST do all of the following:

- conform to the same D7 `output`;
- list each target the mutating call would affect as observed during that call in an array field `targets`, whose item schema is tool-defined;
- return `changed: false`;
- return a boolean `requires_confirmation` that is `true` exactly when the same call without `--dry-run` and without `--yes` is a *gated call* in a *non-interactive context*;
- omit a value that is produced only by performing the mutation, or return it as `null`. The shared schema declares the field as optional or nullable to match that choice.

For example, if the backend generates a resource ID only when creating the resource, the preview omits the ID field or returns it as `null`.

**Example: preview and mutation results.** Preview and mutation share one output schema; this tool also lists `targets` in the mutation's own result, which R4c does not require:

```console
$ mytool services prune --env staging --dry-run --json
{"targets":["api","worker"],"changed":false,"requires_confirmation":true}

$ mytool services prune --env staging --yes --json
{"targets":["api","worker"],"changed":true}
```

### R5: Mutating commands MUST report what happened.

**R5a:** `changed` reports whether this call caused a new intended state transition, not whether background work completed, with these rules:

- Every command whose `effects` is not `read_only`, except a *delegating command*, MUST declare `output` with `changed`; the D7 `output` row declares its type.
- In a machine-readable format, each successful call of such a command MUST emit a result document or at least one stream record, including when it makes no change.
- Every structured success document and every emitted stream record of such a command MUST contain `changed`.
- In a stream record, `changed` describes the whole call as observed when that record is emitted, not just the event that record describes.
- Except under the following condition, `changed` is a boolean.
- If determining whether this call changed intended state would require an extra read just for that purpose, the command MUST return `changed: null` and document when this happens. `null` means the tool could not determine whether it changed state.

For example, a backend may confirm that a create-if-absent request succeeded without saying whether it created the object or found it already present.
If the command would need an extra read just to distinguish those outcomes, it returns `changed: null`.

**R5b:** When authoritative state, including state the tool itself owns, reports a concurrent conflict, the tool MUST fail with `kind` `conflict` and MUST NOT silently overwrite it.

**R5c:** A `non_idempotent` command whose repetition could duplicate an effect SHOULD accept `--idempotency-key` when the backing service supports idempotency keys and the tool's client can send them.

For example, an idempotent create-or-get may return `changed: true` and then `changed: false`. A strict create may return `changed: true` and then fail with `kind` `conflict`. Under O3c, human-readable output of a repeated idempotent delete reports absence rather than deletion, for example `Already absent: api`.

### R6: Built-in retries MUST be safe and bounded.

**Applies when:** the tool retries requests internally.

A tool's internal retries MUST meet both conditions:

- The retry may resolve the failure without duplicating an intended effect.
- The command is `read_only` or `idempotent`, or an idempotency key or equivalent protocol guarantee protects the request against duplicate effects.

Retries MUST preserve all request inputs, including keys and protocol identifiers, stay within the call's timeout or deadline (I8), and use a limited number of attempts.
The tool SHOULD honor `Retry-After` when the timeout allows.

Safe internal retries do not imply `retryable: true`, which concerns repeating the whole CLI call (F3b).

For example, suppose `PUT /jobs/job_123` creates a job once and returns the same job on repeated identical requests.
The tool can resend the same request after losing the response; a new CLI call using `/jobs/job_124` would create another job.
Polling for completion follows the waiting rules (I8).

### R7: Accepted work MUST remain identifiable.

An *accepting call* returns an identifier (R7a), and its exit `0` means acceptance (R7b).
Choosing whether the command waits changes how long the call lasts, not which work it starts (R7c).
A command that waits for work it started reports failure or timeout under R7d.
Observing and controlling the work after the call exits falls under the `managed` extension when claimed (M1 to M3).

**R7a:** An *accepting call* MUST return a non-empty canonical identifier in structured success output.
The identifier MAY consist of several fields, such as a resource identifier and an operation number. Together, those fields identify the work.
Its field names and types are tool-defined. They MUST be documented in `output_description` and remain consistent across the command that starts the work and every command that addresses it.

In the managed-operation rules (M1 to M3), "the identifier" means all components under those names.
Conversational tools can identify work through its session under the session-addressing rule (V1b).

If the command that starts the work fails after obtaining any components, its structured error MUST carry those known components in `context` under the same names.

When the initiating command observes acceptance of work that can continue after it exits, it MUST obtain that identifier as part of observing acceptance.
The identifier can come from the acceptance response. It need not exist before the request.
A lost response can leave both acceptance and the identifier unknown, which follows the uncertain-outcome rule (F4a).

**R7b:** Exit `0` means the work was accepted, not completed. The identifier MUST remain usable after the initiating process exits and, until expiry under a documented retention policy, MUST NOT resolve to a different entity. A tool MAY reassign it after expiry. The tool SHOULD document how a caller observes the work, and when a wait command exists, the identifier's D9 breadcrumb SHOULD name it.

**R7c:** When a command offers both waiting for completion and returning after acceptance, choosing to return after acceptance MUST change only how long the command waits, not the work it starts. The D7 `description` of a command that starts managed work MUST state whether it waits for completion by default.

**R7d:** **Applies when the command waits for work it started.** This includes observing a test run, build, deployment, agent turn, or another task.
The task may finish during the call or continue after it; this rule does not require the `managed` extension.
A failure to read a resource is not by itself a failed task.

Unless an execution deadline has already ended the wait, the command MUST fail with `kind` `operation_failed` when it observes that the work finished unsuccessfully.

The error's `context` MUST include the task's identifier if one was obtained and its observed status if the task's interface exposes one.
A task that finishes during the call does not need an artificial identifier just to report its failure.

**Work that can continue after the call:**
If the deadline passes while waiting for this accepted work, the command MUST fail as follows:

- If the command observes that the work is still nonterminal when the deadline passes, use `kind` `timeout`. Include the identifier under R7a and the observed state in `context`.
- If the command cannot determine the current outcome, use `kind` `outcome_unknown`. Include the identifier and any last observed state in `context`. An earlier observation alone does not establish the current outcome.

The observation deadline MUST NOT cancel or otherwise change the work.

**Other started tasks:**
Command detail MUST state whether the deadline stops execution or only stops waiting.
If the deadline ends the wait before the command observes a task outcome, and the execution outcome can be determined, the call MUST:

- Fail with `kind` `timeout`.
- Include any obtained identifier and observed status in `context`.

Possible unobserved effects follow the uncertain-outcome rule (F4a).
No identifier is invented for a synchronous task.

**Example: execution and observation deadlines.** A synchronous `mytool test --timeout 30s` can stop its test process if command detail declares that behavior.
By contrast, `mytool wait job_123 --timeout 30s` stops waiting and leaves the managed job running under the wait rule (M1e).

**Example: observing a failed deployment.** A `deploy` that waits for its deployment, and the wait command of a tool that declares `managed` observing the same terminal state under M1, both fail with `operation_failed` and carry the identifier.
This example shows only their stderr; any result document follows the documented failure-output contract (O5a).

```console
$ mytool deploy api --env prod --yes --json
{"error":{"kind":"operation_failed","message":"Deployment dep_123 failed: image pull error","context":{"deployment_id":"dep_123","status":"failed"}}}

$ mytool deployments wait dep_123 --json
{"error":{"kind":"operation_failed","message":"Deployment dep_123 failed","context":{"deployment_id":"dep_123","status":"failed"}}}
```

## Stage O: Output

Output has three separate concerns:

1. Format: how stdout selects human-readable or machine-readable output (O2).
2. Shape: a document described by a schema (O4, O5), a bounded collection (O6), or a record stream (O7).
3. Delivery: classifying each standard stream (O1), keeping results separate from diagnostics (O3), and handling a reader that closes the pipe (O8).

### O1: The standard streams MUST be classified independently.

The TTY state of stdin is one input to the prompt policy under I5a, stdout controls the default format under O2c, and stderr controls diagnostic decoration under O3b and whether F2 requires the structured error object. Redirecting one stream does not change how another is classified.

### O2: Output format selection MUST be explicit and predictable.

The tool declares its default formats (O2a), accepts `--json` (O2b), and chooses a default when no format flag is given (O2c).
An explicit format flag overrides the default (O2d).
The remaining clauses cover writing the result to a file (O2e) and passing through another process's stdout (O2f).

**O2a:** Declaration: the introspection index's `format_defaults` field (D6) MUST declare `tty` and `non_tty` format names. A command detail's `format_defaults` field (D7) MUST appear only when that command differs from the tool-wide defaults. For a *stream command*, the machine-readable format name is `ndjson`; that implied name does not by itself make the command differ from the tool-wide defaults.

**O2b:** Machine access: every command MUST accept `--json`. On a *silent command*, `--json` selects the `json` format, and a success MUST write no bytes to stdout. If a command also accepts `--format`, `--json` MUST produce the same output as `--format json` or, on a *stream command*, as `--format ndjson`.

**O2c:** Defaults: a *document or stream command* SHOULD default to human-readable output on a TTY. On non-TTY stdout, a *document or stream command* MUST default to JSON or NDJSON, except that a command whose primary result is a textual document rather than a set of fields or records MAY default to a declared native format such as text or Markdown.

A textual answer with identifying metadata is also a document for this exception.
A command returning either that answer or its acceptance receipt MAY keep the same declared native default for both.
The answer presentation in [`conversational`](#extension-conversational-agent-conversations-must-support-follow-up-and-correction) defines that extension's default.

**O2d:** Precedence: an explicit format flag MUST override the detected default. Two explicit format flags are a usage error under I6a.

**O2e:** Names: a command MUST NOT accept `--output`. If it accepts a destination path, the flag MUST be named `--output-file`. When a call emits result data under `--output-file`, the destination file contains exactly what stdout would have received, and the declared JSON contract in O5 applies to that file. Under that flag, the call writes no bytes to stdout, on success or failure. Formats use `--json` or `--format`.
An existing destination file alone does not show that the current call produced a result.
Writing under `--output-file` is result delivery, not a mutation of intended state: it replaces an existing destination without confirmation and does not change the command's `effects`.

`--json` is one flag a caller can pass without knowing the tool's format names. `--output` means a file path in some tools and a format in others, so a caller who sees it cannot tell which.

**Example: successful calls with empty stdout.** A *silent command* under `--json`, and a *document command* under `--output-file`; both exit `0` and write no bytes to stdout, and the second leaves the result in `services.json`:

```console
$ mytool config validate --json
$ mytool services list --json --output-file services.json
```

**O2f:** **Applies when the command is a *delegating command*.** Stdout is the contract of the process the command runs. On that command `--json` MUST NOT change stdout; it still forces the *non-interactive context* and selects the format F2 tests. On such a command:

- stdout is not governed by the stream roles, decoration, untrusted-value handling, pipe closure, or color rules (O3a, O3b, O3d, O8, H4);
- requirements on structured success output do not apply (R4c, R5a, R7a);
- stderr still carries failure diagnostics (F2);
- the exit status keeps its tool-wide meaning (F1), so a child exit status passed through unchanged violates F1 where it collides with a tool-wide meaning.

**Example: passing through another program's stdout.** Part of the command detail (D7) for a command that runs a caller-supplied program:

```json
{
  "name": "run",
  "effects": "non_idempotent",
  "delegates_stdout": true
}
```

If `report.py` prints `3 services`, this command passes that text through even with `--json`:

```console
$ mytool run --json -- python report.py
3 services
```

### O3: The output streams MUST have separate roles.

**O3a:** Channels: stdout MUST contain only the result; stderr MUST carry logs, warnings, progress, prompts, and errors.
`--quiet` and `--verbose`, when accepted, change only stderr diagnostics.
A *document or stream command* that is intended to change a file system or an external system MUST report that change in its structured success result described by the `output` schema (D7).
For example, `mytool init --json` creates a directory and reports `{"path": "/work/app", "changed": true}` rather than writing the directory's contents to stdout.

**O3b:** Decoration: machine-readable stdout MUST contain only output in the selected stdout format, without banners, terminal decoration, or ANSI escapes. Animated progress MAY appear only when stderr is a TTY. On non-TTY stderr, progress MUST use complete plain lines or be omitted.

**O3c:** Consistency: human and machine renderings MAY differ in detail but MUST NOT contradict each other. When human-readable output is only a bounded preview of the result, it MUST say so and identify how to retrieve the complete result.

**O3d:** Untrusted content: values supplied by the caller or received from a remote source need different handling in machine-readable and human-readable output:

- Machine-readable output MUST encode these values using the selected stdout format's serialization rules.
- Human-readable output MUST escape terminal control sequences in these values so the terminal displays them as text instead of acting on them.

A result whose whole payload is a document in a native format, not a text answer under the tagged presentation (V6b), is not changed by this escaping on non-TTY stdout; under `--output-file`, the destination receives that non-TTY representation under the result-delivery rule (O2e).
When such a document is displayed on a TTY, the escaping applies.

For example, a name containing a double quote, `team "blue"`, becomes `"team \"blue\""` as a JSON string.
If a remote message contains an escape sequence that clears the screen, human-readable output shows a visible representation of that sequence instead of clearing the screen.

### O4: A command schema MUST describe its JSON result value.

**O4a:** The D7 `output` field describes one result document, whether returned on success or failure, or one record on a *stream command*. It MUST follow JSON Schema Draft 2020-12 and use only `type`, `enum`, `properties`, `required`, and `items` for validation. It MAY additionally use the annotation keywords `description` and `title`.

**O4b:** Every schema MUST contain `type`, except that a schema MAY be the empty object `{}`, meaning any JSON value, where the value's shape depends on the input. `type` MUST be `string`, `integer`, `number`, `boolean`, `array`, or `object`, or a two-element array containing one of those types and `null`.

**O4c:** `enum` MAY restrict a value to a finite set. Every member MUST conform to the same schema. `enum` MUST list only values the current version can return.

**O4d:** An array schema MUST contain `items`. An object schema MUST contain `properties`, which lists every field the current version may return, and `required`, which lists every always-present field.
For a document command, these lists cover both success and failure result documents, not the separate structured error on stderr.
A field required only on success can be optional in the shared schema when a failure result may omit it; the success requirement still applies.
The command MUST state any such success-only presence requirements in its `output_description`.

**Example: required, optional, and nullable fields.** This output schema declares a job status result:

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

In this schema, `progress` is required but nullable, while `warnings` is optional and accepts an array when present. `{"job_id":"job_123","status":"running","progress":null}` matches the schema. Omitting `progress` does not; adding `"warnings":null` does not either. An omitted field and a field containing `null` are different JSON shapes.

### O5: JSON output MUST match its declared contract.

**O5a:** **Applies when a *document command* uses the `json` format.** The command MUST follow these rules:

- Encoding: each result is exactly one complete JSON value conforming to `output`, encoded as UTF-8 and followed by LF.
- Success: emit a result.
- Failure: emit the available result in the cases stated in `output_description`; otherwise emit none. A command that returns results on failure states those cases in that field; other commands need no statement that failures return none.
- Incomplete data: if the command can return it, declare an object schema with a required boolean `partial`. Every emitted result then includes `partial`: `false` for complete data, `true` otherwise. A call emitting `partial: true` exits non-zero.

Completeness concerns the result promised for that call. A full report about a failed job or failed batch targets is complete data. More pages existing does not make the requested page incomplete.
The structured error still goes to stderr under the error rules (F2).

**Example: complete and partial results.** This part of a generator's command detail declares its output and when it returns a result on failure:

```json
{
  "output": {
    "type": "object",
    "required": ["text", "partial"],
    "properties": {"text": {"type": "string"}, "partial": {"type": "boolean"}}
  },
  "output_description": "Returns generated text also when a token limit stops generation."
}
```

| Call outcome | Exit | Stdout |
|---|---|---|
| Generation completes | `0` | `{"text":"The complete answer.","partial":false}` |
| A token limit stops generation | Non-zero, with a structured error on stderr | `{"text":"The generated fragment...","partial":true}` |

Both JSON documents are complete; only the second contains unfinished text.

**O5b:** Values: values MUST keep their declared JSON types. Machine output MUST NOT silently truncate a value.

**O5c:** Bounded values: a command that may cap a single inline value MUST declare a required boolean `truncated` and an optional string `output_file` in its D7 output schema. When `truncated` is `true`, `output_file` MUST identify a file that holds the complete value. The file SHOULD NOT be accessible to other users. The inline value is a preview, and the file's retention MUST be documented. Collections and record streams MUST use their own bounds instead (O6, O7).

These fields describe a preview of an existing complete value. Unfinished data uses the partial-result marker (O5a), with no requirement to provide a completion that never existed.

**Example: a shortened answer with its full value in a file.**

```json
{
  "answer": "First part of the answer...",
  "truncated": true,
  "output_file": "/home/user/.cache/mytool/results/job_123.md"
}
```

**O5d:** Time: timestamps MUST use RFC 3339 with a numeric offset or `Z`. Timestamps within one document or stream SHOULD share one precision. Numeric duration field names MUST state their unit.

### O6: Potentially unbounded document collections MUST be bounded.

This requirement limits how many items a document collection returns in one call.
Lists of mutation targets are handled separately (O6a).

**O6a:** The list of mutation targets described in the preview rules (R4) is exempt from this requirement, whether returned by a preview or by the mutation itself.
A command that may shorten this list follows the single-value truncation rules (O5c): report whether the list was shortened and, if so, provide a file containing the complete list.

For example, a deletion preview may show 20 of its 500 targets and provide a file listing all 500.
That preview uses the truncation fields, not the collection's `items` and `has_more` wrapper.

**O6b:** Window: if a collection has no documented finite maximum size, the command MUST use a finite default limit and accept `--limit`.
The tool MUST limit the number of items returned, without silently removing fields from individual items.
An explicit `--limit N` allows at most N items in that call's result.
The command MUST document how it selects the default window and orders its items, including when the order is unspecified.
It SHOULD use an order that is stable when the matching data has not changed.

**O6c:** Shape: a bounded collection MUST be an object with an `items` array and a boolean `has_more`.
`has_more` is `true` exactly when more matching items exist beyond those returned, and `false` otherwise.
A collection with a documented finite maximum MAY use a bare array.
An empty collection MUST keep its usual shape with an empty array, rather than return `null` or no output.

Resuming a collection beyond one page falls under the `continuation` extension (C1).

**Example: a collection page with more items available.**

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

**Example: a fixed maximum versus a page limit.** Assume one tool exposes both commands and does not claim `continuation`:

- `regions list` documents a maximum of three regions and returns all configured regions, here `["east","west"]`. The collection limit rule (O6b) does not require `--limit` for this command.
- `jobs list` has two matching jobs today but no documented finite maximum. It accepts `--limit` and uses a finite default limit of 100, so its page is `{"items":[{"id":"job_1"},{"id":"job_2"}],"has_more":false}`.

The current item count is not a documented maximum.

### O7: Record streams MUST be bounded and framed.

**Applies when:** the command is a *stream command*.

**O7a:** Framing: `--json` MUST emit UTF-8 NDJSON with one complete JSON object per LF-terminated line and no blank lines. Each record MUST conform to D7 `output` and become readable before the command waits for another record or exits. Each record keeps its declared value types without silent truncation (O5b), its timestamp form (O5d), and its field stability across compatible versions (D8).

**O7b:** Window rules:

- Without `--follow`, the command MUST terminate.
- A stream without a documented finite maximum MUST use a finite default window and accept at least one of `--limit`, `--head`, or `--tail`.
- `--limit N` is the maximum number of records emitted from the selected position in the documented order; it does not select that position.
- `--head N` selects the first N matching records and `--tail N` selects the last N matching records. A command MAY accept any subset of `--limit`, `--head`, and `--tail`.
- The command's descriptions in command detail (D7) MUST state the default window and how each accepted selector combines with `--follow`; unsupported combinations are usage errors under the conflicting-input rule (I6a).
- `--follow`, an unbounded mode selected explicitly under I8c, removes the default window when offered. An explicit `--limit` remains effective with or without `--follow` and ends the read after N records.
- Reaching a finite window that ends the read does not end the record source.

**O7c:** Ordering: the stream MUST use a stable, documented order and MUST NOT stop before a finite window that ends the read while matching records are available.

**O7d:** Completion: exit `0` means the requested read ended successfully, not that the record source has no more records; an empty stream is valid except when mutation reporting requires at least one record (R5a). After a non-zero exit, prior LF-terminated records remain valid. An unterminated final fragment is not a record. This standard defines no end record; EOF and the exit status are authoritative.

Resuming a stream after a record falls under the `continuation` extension (C2).

**Example: a bounded read of two records.**

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

**F1a:** The codes in the table below MUST have these meanings.
Tools can also define additional codes with stable meanings.
Detailed failure reasons belong in the structured error's `kind`, rather than a separate exit code for each reason (F1d).
The introspection index's `exit_codes` field (D6) carries the tool-wide meaning of every code the tool returns. A code whose meaning belongs to one command alone is added to that command's `exit_codes` field in command detail (D7), under the command-specific-code rule (F1e).

| Code | Meaning |
|---|---|
| `0` | Success, including an empty result or no differences. |
| `1` | Generic failure. |
| `2` | Usage error. |

**F1b:** **Applies when the command's documented result is a single yes-or-no answer about existing state.** The command MAY document one additional non-zero exit code for a false answer (F1d, F1e).
Examples include checking whether a unit is active or whether two files have no differences.
For that command, exit `0` MUST mean true and the additional code MUST mean false.
Both answers are successful outcomes, so neither requires a structured error under F2 or F3.
The observed result required by F1c is the answer itself.

**F1c:** Exit `0` MUST mean the command observed, through the interface it used, the documented condition that defines success. Independent re-verification is not required. For an *accepting call*, acceptance leaves an identifiable operation under the accepted-work rules (R7). When the initiating command waits, it fails if the work does not succeed (R7d), as does the wait command under the `managed` extension (M1).

**F1d:** Additional codes MUST each have one stable meaning. Fine-grained failures MUST use F3 `kind` instead.
Use the exit code for broad control flow and, when a structured error is emitted, its `kind` for recovery decisions.
That object is on stderr's last non-empty line under the error-location rule (F2c); different kinds can share the same exit code.

**F1e:** D7 command `exit_codes` MUST appear only when they add a code or refine a tool-wide description without changing its meaning.

### F2: Structured errors MUST have one location.

The meaning of failing with a `kind` is fixed in Terms; these clauses say where the error goes: which stream carries the diagnostics (F2a), when the object is required (F2b), and on which stream and line the object sits (F2c), in the structured error envelope (F3).

**F2a:** When the tool exits with an error, stderr MUST contain the F3 `message` and, when the error has one, its `hint`, as human-readable diagnostics or inside the F3 JSON object. The meaning of fail with a `kind` is defined in Terms.

**F2b:** The tool MUST emit the error object when the selected stdout format is machine-readable or stderr is not a TTY, including under `--quiet`; otherwise the object MAY be omitted. When `--json` appears among the arguments before `--`, a failure raised before the format was resolved MUST emit the object.

**F2c:** Whenever the object is emitted, it MUST be the last non-empty stderr line, and a requirement that names an F3 `kind` applies to it. The object MUST NOT be written to stdout.

**Example: structured and human-readable errors.** The same failure with `--json`, as an F3 object on stderr, and at a terminal:

```console
$ mytool deploy api --env prod --yes --json
{"error":{"kind":"image_not_found","message":"Cannot deploy 'api': image 'web:v2.1.0' was not found","hint":"Run mytool images list web"}}

$ mytool deploy api --env prod --yes
Error: Cannot deploy 'api': image 'web:v2.1.0' was not found
Run: mytool images list web
```

**Example: when a structured error is required.** Assume `services get` is a document command with `format_defaults` of `{"tty":"text","non_tty":"json"}`. Each call below fails to find `missing`. All streams are TTYs unless redirected, and `NO_INPUT` is unset except where shown.

| Call | Context for prompting | Selected stdout format | F3 error object |
|---|---|---|---|
| `mytool services get missing --json` | Non-interactive | `json` | Required on stderr |
| `NO_INPUT=1 mytool services get missing` | Non-interactive | `text` | May be omitted |
| `mytool services get missing < /dev/null` | Non-interactive | `text` | May be omitted |
| `mytool services get missing 2>error.log` | Interactive | `text` | Required in `error.log` |

The final row places the object on the last non-empty line of `error.log`. `NO_INPUT` changes the prompting context without changing the selected stdout format.

### F3: Structured errors MUST have a stable envelope.

**F3a:** The document MUST contain exactly one top-level field, `error`, whose value is an object with the fields in the table below. Only `kind` is a stable value for a caller to match on; message text is not a stable interface.

| Field | JSON type | Presence | Value and meaning |
|---|---|---|---|
| `kind` | string | MUST, always | Stable machine meaning; the values in the `kind` table below have shared meanings. |
| `message` | string | MUST, always | Identifies the failed operation and cause. |
| `retryable` | boolean | MAY | `true` only when the same call may resolve the failure without duplicating an intended effect; if omitted, the caller has no guarantee that repeating the call is safe (F3b). |
| `action` | string | MAY | `agent` when the caller can recover autonomously, `user` when human action is required, or `none` when no recovery action exists. |
| `hint` | string | SHOULD, when one concrete recovery step is known | Human-readable guidance on what to do or correct (F3b); it can describe a step outside the CLI. |
| `next` | array | MAY | One recovery command: the executable followed by its arguments, each a string, ready to execute without a shell (D9). Absent when no natural recovery step exists. Does not require a field in the result schema unless result output also uses `next`. |
| `context` | object | MAY; required for accepted-work or wait-command failures as specified in R7a, R7d, and M1b | Machine-readable values needed for recovery. The recovery-field and partial-result rules (F3b, F4b) recommend fields in it. |

**F3b:** Recovery fields follow these rules:

- `retryable` is `true` only when the same call may resolve the failure without duplicating an intended effect; when it is omitted, the caller has no guarantee that a retry is safe.
- `hint` is present when one concrete recovery step is known; omit it rather than guess.
- When `retryable` is `true`, `context` SHOULD carry `retry_after_ms`: a non-negative integer giving the minimum wait, in milliseconds, before repeating the call.
- When `retryable` is `true` and the tool knows a finite retry limit that applies to this call, `context` SHOULD carry `attempts_remaining`: a non-negative integer giving the number of further attempts allowed by that limit.

**F3c:** A tool MUST NOT use a `kind` listed below with another meaning. It SHOULD use a listed `kind` when its definition applies. A requirement that names a `kind` keeps its own keyword. Any other `kind` is tool-defined.

| `kind` | Meaning | Named by |
|---|---|---|
| `invalid_input` | The call is a usage error or exceeds an I7 bound. | I6 |
| `not_found` | A target identified by an argument or flag value does not exist in the state the tool manages or targets. | M1, V2 |
| `conflict` | Authoritative state conflicts with the requested change, such as a concurrent modification or a strict create whose target exists. | R3, R5, V2 |
| `permission_denied` | The caller is identified and the action is forbidden. | |
| `unauthenticated` | The caller could not be identified: credentials are missing, invalid, or expired. | |
| `timeout` | The command's own I8 deadline passed. Work continuing after the call remains identified in `context` under R7a; possible unobserved effects follow F4a. A synchronous task needs no artificial identifier (R7d). | R7, M1 |
| `unavailable` | A dependency rejected or failed the request transiently, including rate limiting. | |
| `outcome_unknown` | The intended effect may have happened and was not observed. | R7, F4 |
| `interrupted` | The user interrupted the command and F4 does not apply. | F5 |
| `cursor_unavailable` | The cursor is invalid, expired, or incompatible, or required history is missing. | C1, C2 |
| `confirmation_required` | R3 stopped a *gated call* before its effect. | R3 |
| `operation_failed` | A started task reached an observed unsuccessful outcome under the started-work or managed-wait rules (R7d, M1e). `context` carries its identifier if one was obtained, and its observed status if the task's interface exposes one. | R7, M1 |
| `precondition_failed` | A documented precondition that `--force` overrides is not met. | |

**Example: error recovery guidance.** Here, `hint` explains what to look for, and `next` provides the command to list the available images.

```json
{
  "error": {
    "kind": "image_not_found",
    "message": "Cannot deploy 'web-api': image 'web:v2.1.0' was not found",
    "retryable": false,
    "action": "agent",
    "hint": "Choose an existing image tag before retrying the deployment.",
    "next": ["mytool", "images", "list", "web"],
    "context": {"service": "web-api", "image": "web:v2.1.0"}
  }
}
```

### F4: Fallbacks and uncertain outcomes MUST be explicit.

**F4a:** If a failed command may have caused an intended effect and did not observe the outcome, it MUST use F3 `kind` `outcome_unknown` and MUST NOT report completion or absence. Any result document follows the failure-output and partial-data rules (O5a); prior LF-terminated stream records remain valid (O7d).

**F4b:** If usable partial results or completed effects survive a failure, the structured error SHOULD identify them in `context`.
Completed effects go in `error.context.completed`, an array of the affected targets, each identified as the result document identifies that target, or as the caller named it when there is no result document.
This array identifies effects already performed; the result's `partial` field describes incomplete data (O5a), not whether every intended effect was performed.

**F4c:** After a failure, a command MUST NOT silently replace the requested target, source, or mode. It MUST fail with a `kind` or identify the substitution in its declared structured result.

**Example: a timeout with an unknown outcome.** The request timed out and its effect was not observed:

```json
{"error":{"kind":"outcome_unknown","message":"The deployment request timed out and its outcome could not be determined","retryable":false,"context":{"deployment_id":"dep_123"}}}
```

### F5: User interruption MUST fail honestly.

A command that receives the platform's normal user-interrupt request MUST exit without a stack trace and fail with `kind` `interrupted`, unless F4 requires `outcome_unknown`. Interrupting observation of managed work MUST NOT cancel or otherwise change that work.

## Stage H: Human interface

A person at the terminal should find familiar flag names, standard environment behavior, and output that remains readable without color.

### H1: Flags MUST have canonical long names.

**H1a:** A flag MUST use kebab-case and have one canonical long name per concept.

**H1b:** When the tool supports a concept in the table below, it SHOULD use the listed long name; a requirement that names a flag keeps its own keyword. A listed alias MAY be added when it has no conflicting local meaning. Every accepted alias MUST behave exactly like its canonical flag and appear in the I1 descriptor.

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

The fixed list of command names, flags, and aliases suggested by shell completions matches what the installed tool supports, as required by the published-interface agreement rule (D1).

### H3: External editors and pagers MUST respect user selection.

A tool that starts an editor MUST prefer `VISUAL` to `EDITOR` unless a documented setting overrides them. A tool that starts a pager MUST honor `PAGER` unless a documented setting overrides it.

### H4: Color MUST remain optional.

**H4a:** This clause does not govern a *delegating command*'s stdout; it still governs the tool's own stderr decoration. A tool that emits terminal escape sequences MUST apply these defaults to each stream based on that stream's TTY state:

- omit all terminal escape sequences when the `TERM` environment variable is set to `dumb` or the stream is not a TTY;
- omit color when `NO_COLOR` is non-empty; other styling MAY remain.

`TERM` identifies the terminal type; `dumb` means a basic terminal without advanced display capabilities.
A flag or documented setting overrides these defaults only on human-readable output. Color MUST NOT be the only carrier of information.

**H4b:** If provided, the color flag MUST be `--color` with the values `auto`, `always`, and `never`: `auto` applies that default, and the other two override it. `--no-color` MAY be provided as `--color=never`. `--color always` has no effect on `--plain` output or on format selection.

At a terminal, `NO_COLOR=1 mytool services list` and `TERM=dumb mytool services list` print no color. In a pipe, a tool that accepts `--format` keeps color with `mytool services list --format text --color always | less -R` because the output is human-readable, while `mytool services list --color always | jq .` emits none because stdout is then machine-readable under O2 and O3.

### H5: Plain collection output.

A command whose result is a document collection bounded under the finite-window rule (O6b) SHOULD offer `--plain`, including when the collection is paginated.
This recommendation does not cover record streams, mutation-target lists exempt under O6a, or arrays that are merely fields of another result.

**H5a:** A command whose result is bounded under O6b MAY reject `--plain` for a page that was not selected explicitly with `--limit` or, under C1, `--cursor`, because plain output carries no `has_more`.

**H5b:** **Applies when the command supports `--plain`.** `--plain` MUST emit one item per LF-terminated line, with no heading or terminal decoration. The line format MUST be documented and stable; if items can contain LF, its escaping MUST be documented. If `--format` exists, `--plain` MUST equal `--format plain`.

**Example: plain output.** Three services, one per line:

```console
$ mytool services list --plain
api
worker
scheduler
```

## Existing tools

This section helps tools adopt the standard while preserving released behavior that callers depend on. The conformance claim is the tool's declaration of compliance, stored in the index's `conformance` object (D6). B1 explains which behavior qualifies, B2 defines which commands the declaration covers, and B3 and B4 list the permitted exceptions. Requirements outside these exceptions still apply. Individual command contracts appear in command detail (D7).

### B1: Released behavior MAY be kept where changing it would break callers.

**B1a:** A command path, flag, environment variable, or error `kind` is brownfield if it keeps a previously released contract; otherwise it is greenfield. These labels describe individual parts of the interface, not the whole tool. A new command inherits any previously released tool-wide contract: its inherited behavior is brownfield, and anything it introduces is greenfield. Each rule below applies to the part of the interface it names.

**B1b:** A brownfield command path MAY keep established behavior when changing it would break existing callers. Behavior kept under the permitted exceptions (B3, B4) does not make a command incompatible. Any other retained behavior that violates a requirement excludes the command from the conformance declaration (B2). A tool that keeps an incompatible command SHOULD also provide a documented conforming command path for the same operation.

For example, a tool's released interface uses `--output PATH` across its commands. A new `reports export` command inherits that destination flag and can keep it under the released-name exception (B3, O2e row). If it also introduces an archive-mode option, that new flag follows the canonical naming rule (H1a), for example `--archive-mode`. The inherited flag is brownfield; the new option is greenfield.

### B2: The conformance declaration MUST exclude incompatible retained commands.

`scope` selects commands by path prefix; `conforming: false` excludes individual commands.

**B2a:** A command that retains incompatible behavior MUST carry `conforming: false` in its index entry, unless it is outside `scope` (B2b). That behavior MUST be documented. Omitting `conforming` means `true`; the entry still includes the required name, description, and effects (D6b).
For example, a command that requires `--json compact` instead of accepting bare `--json` is incompatible with the required machine-output flag (O2b).

**B2b:** The `conformance` object MAY contain `scope`, a non-empty array of non-empty command-path prefixes. A command is within `scope` when its `name` equals a listed prefix or starts with that prefix followed by a space (U+0020). A caller MUST NOT infer conformance for commands outside `scope` from this declaration.

**B2c:** The tool declares conformance for every command except those marked `conforming: false` and, when `scope` is present, those outside it. With no `scope` and no commands marked `conforming: false`, all commands are covered.
`scope` limits only requirements that apply per command. These tool-wide requirements still apply:

- root help (D3a);
- flag-name consistency and the version flag (D4c, D4d);
- the introspection command, its index, and their stability (D5, D6, D8);
- the exit-code table and the declared default formats (F1a, O2a).

The index still lists every command the tool dispatches, and each listed command still provides introspection detail (D5d). For excluded commands, that detail still matches the runtime parser (D5b), but need not satisfy the command-detail contract (D7).

**Example: limiting the conformance declaration to a group.** This part of the index selects the `services` group:

```json
{
  "conformance": {
    "name": "cli-design-standard",
    "standard": "0.2.0-draft.10",
    "extensions": [],
    "scope": ["services"]
  }
}
```

Assume the tool lists these commands in its index:

| Command name | `conforming` in its entry | Covered by the declaration? |
|---|---|---|
| `services get` | Omitted | Yes. |
| `services list` | Omitted | Yes. |
| `services legacy` | `false` | No: explicitly excluded. |
| `jobs list` | Omitted | No: outside `scope`. |
| `services-admin get` | Omitted | No: `services-admin` is not `services`. |

All five commands remain discoverable, and the tool-wide requirements still apply.

### B3: Released names, defaults, and flag positions MAY be kept.

**B3a:** A command path, flag, environment variable, or tool MAY keep the released behavior listed below if it meets the condition in that row. Only the listed behavior is an exception; the rest of the named requirement still applies. Aliases MAY remain for compatibility.

| Requirement | Released behavior that MAY be kept | Condition |
|---|---|---|
| D4b | Released names in place of `get`, `list`, `create`, and `delete`, such as `info`, `ls`, or `add`. | No additional condition. |
| D4d | The released form of `tool_version` and of `--version` output. | `--version` output still contains `tool_version` under D6a. |
| D5a | `schema` as the first argument of a released command. | The tool MUST provide introspection in another form, such as a prefix (`tool contract schema`) or a flag (`tool --schema`). |
| I2a | Released environment variable names outside the `<TOOL>_<OPTION>` prefix. | They are still documented under I2a. |
| I2b | A released precedence order among configuration sources. | Resolution is still deterministic and documented under I2b. |
| I4a | Released names for the flags that select a secret source. | The source is still usable in a *non-interactive context* under I4a. |
| I6b | Released flag positions relative to positional arguments. | `--` still ends flag parsing. |
| O2c | A released default format. | A retained non-TTY default MUST be declared in `format_defaults`. |
| O2e | A released `--output` flag, and a released name for a destination path. | No additional condition. |
| H1a | A released flag name that is not kebab-case, or more than one long name for a concept. | Every accepted alias still behaves like its canonical flag under H1b. |
| R3d | One flag that both confirms a prompt and overrides a precondition, as an exception to D4c. | The tool MUST document, for each command, whether the flag confirms a prompt, overrides a precondition, or does both. A command added later on that tool MAY use the retained flag under the same rule. |

### B4: A released meaning MAY be kept while this standard's meaning stays reachable.

**B4a:** If a released contract gives exit code `1` or `2` a different meaning, the tool MAY keep that meaning. It MUST document which exit code it uses for each meaning in the standard exit-code table (F1a). The index's `exit_codes` includes those substitute codes (D6).

**B4b:** A tool MAY keep a released error `kind` that has a different meaning from the shared error-kind table (F3c). It MUST document that difference.

**B4c:** Reserved field names and their replacements follow these rules:

- A command MAY keep a reserved field name already used with an incompatible type or meaning in its result output, `error.next`, or `error.context` if it also returns this standard's meaning under another name.
- The command MUST declare that mapping in `reserved_overrides` in its command detail (D7). Each key is a *reserved field name*; its value is the replacement name that carries the standard's meaning.
- The command MUST use the replacement name wherever the standard uses that field name: in the output schema, result output, the error's `next` or `context`, or a collection item. The replacement field keeps the standard's required type and meaning.
- For `next`, the replacement name is also allowed in the error envelope; the existing `next` field keeps its released meaning.
- A command that does not provide the standard's meaning under any name is incompatible and is excluded from the conformance declaration (B2a).
- A caller SHOULD check `reserved_overrides` before reading a reserved field name from that command's output.

**Example: mapping a reserved field name.** A released `jobs status` command uses `status` to say whether a job is archived. The standard uses that name for the job's execution outcome. The tool keeps its existing `status` and supplies the standard's meaning in `state`. This part of the command detail tells callers to read `state` wherever the standard requires `status`:

```json
{
  "name": "jobs status",
  "reserved_overrides": {"status": "state"}
}
```

The result shows both: the job is archived, and its execution succeeded.

```json
{"job_id":"job_123","status":"archived","state":"succeeded"}
```

## Extensions

A tool enables an extension by naming it in `conformance.extensions`; the extension's requirements then apply under their stated conditions.
The conformance claim's `scope` limits the commands covered by the whole declaration, including its extensions.
It cannot exclude a command from just one extension (B2).

## Extension `continuation`: Bounded reads MUST be resumable.

**Applies when:** the tool names `continuation` in `conformance.extensions`.

This extension resumes collection reads (C1) and stream reads (C2) from a saved position.
It builds on collection bounds (O6) and streams (O7); *stream command* and failing with a `kind` retain their core definitions.

| Term | Meaning in this extension |
|---|---|
| **Cursor** | A value returned by the tool that identifies a position for continuing a read. |
| **Opaque cursor** | A cursor whose internal structure the caller does not need to understand. The caller passes the returned value back to the tool. |

### C1: Collections MUST page by opaque cursor.

**Applies when:** the collection is bounded under the finite-window rule (O6b).

A collection with a documented finite maximum returns every item and has no page to resume.

For example, a tool claiming `continuation` can return its complete catalog of five built-in types without a cursor, while its job listing has no documented maximum collection size and supports cursor-based pages.
A limit of 20 jobs per response sets the page size; it does not set a maximum for the whole collection.

**C1a:** Contract: the command MUST accept `--cursor`, and each JSON page MUST contain `next_cursor`, an opaque string that the caller passes unchanged to `--cursor`, or `null` exactly when `has_more` is `false`.

**C1b:** Binding: source (the command path and the data set it reads), filters, and order MUST remain unchanged when using `--cursor`; the limit and output format MAY change.

**C1c:** Failure: if the cursor is invalid, expired, incompatible, or cannot continue the same result set, the command MUST fail with `kind` `cursor_unavailable`.
A cursor identifies a position in the documented order.
If that position can no longer be located in that order, continuation is unavailable.
For a position immediately after the last item, the command MAY return an empty final page.

**C1d:** Order: pagination MUST use a stable, documented order and document whether it reads live state or one fixed snapshot. That choice determines whether continuation is exact across concurrent insertions and deletions.

**Example: a collection page with a continuation cursor.**

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

**Applies when:** the command is a *stream command*.

**C2a:** `output` MUST be an object schema with `cursor` as a required string property, and each `cursor` MUST be a non-empty opaque string.

**Example: a cursor in each record.** This part of a command schema declares the required `cursor` property:

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

**C2b:** The command MUST accept `--after-cursor` and resume strictly after that record without skipping any matching record in the same logical stream. A command that accepts a selector which would omit a matching record after `--after-cursor`, including `--tail`, MUST reject their combination as conflicting inputs under I6a. Inputs selecting the source, filters, or order MUST remain the same; limits, following, timeouts, and output format MAY change.

**C2c:** If this continuation cannot be guaranteed because the cursor or required history is invalid, expired, or incompatible, the command MUST fail with `kind` `cursor_unavailable`.

**Example: resuming a read.** The second and third calls pass the last received cursor to `--after-cursor`, so each resumes with the next record:

```console
$ mytool logs job_123 --limit 2 --json
{"cursor":"cur_101","timestamp":"2026-08-15T10:00:00Z","level":"info","message":"Started"}
{"cursor":"cur_102","timestamp":"2026-08-15T10:00:01Z","level":"info","message":"Fetching input"}

$ mytool logs job_123 --after-cursor cur_102 --limit 2 --json
{"cursor":"cur_103","timestamp":"2026-08-15T10:00:04Z","level":"warning","message":"Retrying"}

$ mytool logs job_123 --after-cursor cur_103 --follow --json
{"cursor":"cur_104","timestamp":"2026-08-15T10:00:09Z","level":"info","message":"Recovered"}
```

**Example: combining a cursor with limits.** The table below uses a separate `logs` *stream command* with these assumptions:

- The tool declares `continuation`, and the command accepts `--after-cursor`, `--limit`, `--tail`, and `--follow`.
- The command documents chronological order. At the start of each call, matching records 1 through 100 are available, and the opaque cursor `cur_a7` identifies record 40.
- `--tail N` selects the last N records available when the read starts. The command supports combining `--follow` with `--after-cursor` and `--limit`.
- Each call uses the same source, filters, and order. No failure or deadline interrupts the valid reads.

| Call | Result |
|---|---|
| `mytool logs job_123 --tail 2 --json` | Records 99 and 100, then exit `0`. |
| `mytool logs job_123 --after-cursor cur_a7 --limit 20 --json` | Records 41 through 60, then exit `0`. |
| `mytool logs job_123 --after-cursor cur_a7 --limit 20 --follow --json` | Records 41 through 60, then exit `0`; the explicit limit still ends the read. |
| `mytool logs job_123 --after-cursor cur_a7 --tail 2 --json` | Exit `2`, with `invalid_input` in the F3 object on the last non-empty stderr line. Selecting only 99 and 100 would skip records 41 through 98. |

In this example, the cursor selects where to resume, and `--limit` sets the maximum number of records to return, even with `--follow`.
A successful exit ends that read; the source can still produce new records.

**Receiving a record again after a crash.** Resuming from a cursor does not guarantee that the caller will process each record only once.
Saving the cursor after processing its record reduces repeated delivery of records already processed.
If the caller crashes after processing a record but before saving its cursor, resuming from the previously saved cursor can return that record again.

## Extension `managed`: Accepted work MUST be observable.

**Applies when:** the tool names `managed` in `conformance.extensions`.

This extension lets a caller check the state of a *managed operation* and wait for a terminal state, using the identifier returned when the work was accepted (R7).
A **terminal state** means the work has finished.
Where the tool supports cancellation or suspension, the caller can also request those actions.
The wait command follows the timeout rules (I8).

The roles below do not cover every operation a tool can offer for managed work.
Additional commands follow the requirements that apply to their behavior.

### M1: Managed work MUST expose status and wait.

**M1a:** The tool MUST provide commands for these two roles:

- **Status command:** it MUST report the current state without waiting for a terminal state.
- **Wait command:** waits for the operation to reach a terminal state.

Both commands MUST be usable in a *non-interactive context* and accept the identifier returned when the work was accepted (R7a).
Both commands MUST declare `effects: read_only`.
These names describe the commands' roles; the naming recommendation (D4b) prefers `status` and `wait`.

**M1b:** Structured results from the status and wait commands MUST contain `status` and the operation identifier.

If the wait command fails after argument parsing, its F3 error object MUST carry these facts in `context`:

- Every identifier component supplied or obtained. Unresolved components MUST be omitted.
- `status`: the last observed state, or `null` if no state was observed. This includes an identifier that was not found (M1d).

The structured success results of the status and wait commands SHOULD carry timestamps for two events: when the operation started and when it entered the reported state.
The timestamp field names are tool-defined; their format follows O5d.

**M1c:** Status values follow these rules:

- For the status command, the D7 output schema MUST declare `status` as a finite enum containing terminal values `succeeded` and `failed`, plus `canceled` when cancellation exists.
- For the wait command, the D7 output schema MUST declare `status` as an enum listing all states its result documents can carry, including failure results under its documented emission conditions (O5a). Exit `0` still requires `succeeded` under M1e; a failure result's status MUST match `context.status` in its F3 object.
- For unfinished states, the status command's enum MAY add values.
- When an existing operation's current state can be indeterminate, the status command's enum MUST add `unknown`.
- The tool MAY return `unknown` only when it successfully reads an underlying state that explicitly says the operation's state is indeterminate.
- If either the status command or the wait command cannot read the operation's state, it MUST fail with the applicable `kind`.

**M1d:** Identifiers: any retention or expiry policy for managed operations MUST be documented. An unrecognized or expired identifier MUST fail with `kind` `not_found`, with no operation state reported.

**M1e:** Wait follows these rules:

- The wait command MUST exit `0` only when it observes the terminal state `succeeded`; any other terminal state MUST fail with `kind` `operation_failed`.
- When the wait command's I8b `--timeout` deadline expires, it MUST fail with `kind` `timeout`.
- When the operation is already terminal, the wait command MUST return immediately.
- Timing out or terminating the wait command MUST NOT cancel or otherwise change the operation, and MUST NOT be reported as a terminal operation outcome.
- If a wait failure names an existing operation and a status or log command can help recovery, the error SHOULD include that concrete command in its human-readable `hint` (F3b). The same command can be provided as executable arguments in `next` under the recovery-command rules (D9). Other failures follow the general hint rule in F3b.

**M1f:** Listing: the tool SHOULD provide a command, usable in a *non-interactive context*, that lists managed operations with the identifier and `status`; its result is a bounded collection under O6.

**M1g:** **Applies when status or wait accepts a selector whose target can change over time.** The command SHOULD select one operation at the start of the call and keep that target until the call ends.
For each status or wait command this clause applies to, the tool MUST document whether that command keeps its selection or follows the selector to newer work.
Its structured result MUST return the public identifier of the operation it reports on.
Any structured error MUST include the known components of that identifier in `context`.
For conversational work, that identifier can be the session alone under the session-addressing rule (V1b).
The command MUST document the selection rule and, if the selector can be omitted, its default.

**Example: job state and command failure.** The table below uses a tool with these assumptions:

- The tool declares `managed`, uses `job_id` as the operation identifier, and provides document commands `jobs status` and `jobs wait`.
- It uses the standard exit codes (F1a). When the backend temporarily rejects a state read, the command fails with `kind` `unavailable`.
- The backend can explicitly report that a job's state cannot be determined, so the status schema includes `unknown`.
- The wait command's `output_description` says it returns a result after observing a terminal state, including a failed one. Its output schema includes those terminal states. Other failures return no result.

Each row is a separate call.
Error objects appear on the last non-empty stderr line; an error row has empty stdout unless it shows a result there.

| Call | Observation | Result |
|---|---|---|
| `mytool jobs status job_123 --json` | The backend explicitly reports that the job's state cannot be determined. | Exit `0`; stdout contains `{"job_id":"job_123","status":"unknown"}`. |
| `mytool jobs status job_123 --json` | The backend temporarily rejects the state read. | Exit `1`; error `kind` is `unavailable`. |
| `mytool jobs wait job_123 --json` | The backend reports terminal state `failed`. | Exit `1`; stdout contains `{"job_id":"job_123","status":"failed"}`. Error `kind` is `operation_failed`, with `context: {"job_id":"job_123","status":"failed"}`. |
| `mytool jobs wait job_123 --timeout 5s --json` | The last observed state is `running` when the wait deadline expires. | Exit `1`; error `kind` is `timeout`, with `context: {"job_id":"job_123","status":"running"}`. The wait does not stop the job. |
| `mytool jobs wait missing --json` | The identifier is unrecognized; no state was observed. | Exit `1`; error `kind` is `not_found`, with `context: {"job_id":"missing","status":null}`. |

In this example:

- `unknown` means the backend reported that it could not determine the job's state. The state read itself succeeded.
- `null` means the command did not observe a job state.
- `timeout` means the wait deadline expired. It does not mean the job finished or failed.

### M2: Cancellation MUST be honest.

**M2a:** **Applies when a managed operation can be canceled.** The tool MUST provide a cancellation command, usable in a *non-interactive context*, whose name follows the naming preferences in D4b. It MUST accept the identifier returned under R7a and fall under Stage R.
The command MUST declare `effects: idempotent`, except that a conversational cancellation command offering session-based selection follows the effect-classification rule (R1).
Repeated session-based cancellation can select newly started work, so it does not always meet the successful-repeat guarantee.

**M2b:** Structured success MUST contain `status` and the identifier; `changed` reports whether this call caused a new intended state transition, not whether the managed operation completed (R5a). Exit `0` means cancellation was accepted or a terminal state was observed, not necessarily that the operation was canceled. The command MUST NOT report `canceled` until it observes that state; if the operation reaches another terminal state before cancellation completes, the command MUST report that observed terminal state.

**M2c:** Cancellation MUST include stopping any scheduled automatic resumption of the same operation.
Accepting the request does not establish completion. The rule for observed cancellation state still applies (M2b).
Cancellation does not by itself undo effects already performed or delete retained results.

**Example: requesting cancellation and waiting for it to finish.** The cancellation command first reports `canceling`.
The wait command later observes `canceled` and exits non-zero because the job did not succeed.
The first two calls below show stdout; the final call shows only stderr.
Any result on the wait command's stdout follows its documented rules for returning a result on failure (O5a).

```console
$ mytool jobs status job_123 --json
{"job_id":"job_123","status":"running"}

$ mytool jobs cancel job_123 --json
{"job_id":"job_123","status":"canceling","changed":true}

$ mytool jobs wait job_123 --timeout 5m --json
{"error":{"kind":"operation_failed","message":"Job job_123 was canceled","context":{"job_id":"job_123","status":"canceled"}}}
```

### M3: Resumable suspension MUST remain distinct from cancellation.

**M3a:** **Applies when managed work can be suspended and later resumed from the same point.** The tool MUST provide commands for these two roles:

- **Pause command:** suspends the operation so it can later continue from the same point.
- **Resume command:** continues the suspended operation from that point.

Both commands MUST be usable in a *non-interactive context*, accept the identifier returned when the work was accepted (R7a), declare `effects: idempotent`, and return the identifier, `status`, and `changed` in structured success.
These names describe the commands' roles; the naming recommendation (D4b) prefers `pause` and `resume`.

**M3b:** The pause and resume commands MUST report only a status they observed.
While suspended, `status` MUST be `paused` and non-terminal.
Cancellation under M2 remains terminal and MUST NOT mean suspension.
For an operation that is already terminal, both commands report the observed terminal state under the cancellation-result rule (M2b).

**Example: pausing and resuming a job.** Assume `job_123` is running and each command observes the requested state change before returning.
Both calls show stdout:

```console
$ mytool jobs pause job_123 --json
{"job_id":"job_123","status":"paused","changed":true}

$ mytool jobs resume job_123 --json
{"job_id":"job_123","status":"running","changed":true}
```

## Extension `conversational`: Agent conversations MUST support follow-up and correction.

**Applies when:** the tool names `conversational` in `conformance.extensions`.

This extension lets a caller keep a conversation, receive an agent's answer, and correct ongoing work.
It applies whether the tool calls a model API, runs its own agent loop, or connects to another agent system.

| Term | Meaning in this extension |
|---|---|
| **Session** | A conversation and its retained context, shared across turns. The caller addresses it by a session identifier. |
| **Turn** | One cycle of agent work, accepted for execution. It can include multiple model requests, messages, and tool calls. |
| **Active turn** | A turn accepted for execution that has not ended, including while waiting to start, for tools, subagents, caller input, or a temporary limit to clear. |
| **Pending input** | An accepted message waiting for delivery. It can guide the active turn or wait in a follow-up queue. |
| **Follow-up queue** | Optional storage for messages to execute later. Accepting a queued message does not yet accept a turn for execution. |

**When a turn ends:**
A turn ends when its work completes, is canceled, or fails permanently. The tool reports that state under the managed-operation rules (M1, M2).
A completed turn does not end the session or prove that the caller's goal was achieved.

**What stays in the same turn:**
Progress messages, tool calls, and waiting for other work do not end the turn.
An in-place correction adds guidance to that turn; a follow-up starts another.
Returning control to the caller while work continues in the background does not end the turn either.

For example, an agent can run a tool, receive a correction, apply it after the tool finishes, and complete the same turn.

**Minimum interface:**
The tool MUST also claim [`managed`](#extension-managed-accepted-work-must-be-observable).
A turn is the managed operation; its public address is the session under the session-addressing rule (V1b).
The tool MUST provide these roles: start a conversation, follow up, read status, wait, cancel, and correct work using at least one shared mode from V3a.

The tool SHOULD use these role names:

| Role | Name | Caller's intent | Availability |
|---|---|---|---|
| Start | `run` | Start a conversation and its first turn. | Required. |
| Follow up | `continue` | Start another turn with retained context. | Required. |
| Correct | `steer` | Change the approach to active work. | Required. |
| Queue | `queue` | Save a message to execute after current work. | Optional; when offered, enqueueing is explicit and queue execution stays under caller control (V7). |

The minimum applies to the agent configuration selected for the session.
The tool MUST establish this support before accepting the session's first work.
If it cannot, the start call MUST fail before agent work executes.
Acceptance can follow backend initialization, so a receipt returned after acceptance reports the session's correction mode (V1c); returning after acceptance does not require returning before initialization.
Temporary unavailability can still prevent a supported operation from succeeding.
Additional commands, history reads, and selectors are allowed and follow the general requirements for their behavior.

### V1: Conversation identity and capabilities MUST be explicit.

**V1a:** Starting a conversation and sending a follow-up MUST meet both conditions:

- Be usable in a *non-interactive context*.
- Offer a way to return after acceptance without waiting for the answer.

The tool can also offer an interactive conversation in a *terminal context* under the interactive-session rules (I5b).
Interactive use does not replace these non-interactive operations.

A follow-up starts a new turn using retained context and recorded progress.
The follow-up role SHOULD support continuing canceled or failed work without requiring a new message from the caller.
The tool MUST document whether it supports this no-message request and how it handles it; a tool that does not support it rejects the request as a usage error with `kind` `invalid_input` (I6a).
It can use a native continuation mechanism or supply its own instruction to continue the unfinished work.
Pausing and resuming the same operation follow the separate suspension rules (M3).

**V1b:** A session MUST have a non-empty public identifier.
Follow-up, status, wait, cancellation, and correction MUST accept that identifier alone.
No public turn or message identifier is required.

The session identifier is the public identifier of accepted work and managed commands for that conversation (R7, M1 to M3).
A structured result about a turn MUST contain the session identifier and `status`, including an acceptance receipt.
The status describes the work selected for the call under the call-target rules (V2), not the end of the conversation.

A structured error MUST include the session identifier in `context` when supplied or obtained.
Field names for the session identifier, answer, and session-capability object are tool-defined.
They MUST be consistent across these roles and identified in each command detail's `output_description`.
The answer field, when present, MUST be a string containing the answer text alone.

**Session choices:**
Follow-up, correction, queued work, and automatic resumption MUST NOT silently broaden the session's selected permission policy or substitute its selected model.

**Example: starting a conversation.**
It uses `session_id`, `answer`, and `capabilities` for the tool-defined fields.
The agent can read its workspace; recording the conversation is the only mutation the call authorizes.
`--background` selects returning after acceptance.

```console
$ mytool run worker 'Explain why login fails' --background --json
```

```json
{
  "session_id": "c1",
  "status": "running",
  "changed": true,
  "partial": false,
  "capabilities": {"steer_mode": "cancel-then-start", "continue_without_message": true},
  "next": ["mytool", "wait", "c1"]
}
```

`partial: false` means this is a complete acceptance receipt, not a completed answer.
The caller can now use `wait c1`, `steer c1 ...`, or `cancel c1`.
After this work ends, `continue c1 ...` starts new work in the same conversation.

**V1c:** The session-capability object MUST appear in structured receipts from the start, follow-up, correction, and queue roles, and in the status command's results.
It describes the current session, even when status reports work that has already ended.

The object MUST contain `steer_mode`: a non-empty string naming the default correction mode for the session.
The field name is defined here only within the session-capability object.
The default is `in-place` when supported for the session's configuration, otherwise `cancel-then-start`.
A correction without an explicit mode choice MUST use that declared mode.
A busy turn or temporary backend failure does not change the declared mode.

The object SHOULD also contain `continue_without_message`: a boolean stating whether the follow-up role accepts a request without a message (V1a).
Like `steer_mode`, the name is defined only within the session-capability object.

Command detail MUST document the default correction behavior.
Receipts and status report the session's mode; they do not require the caller to choose one.

**Example: inspecting a session.** Work in session `c1` succeeded, and the session uses in-place correction by default.
With non-TTY stdout, this call returns JSON by default:

```console
$ mytool status c1
```

```json
{
  "session_id": "c1",
  "status": "succeeded",
  "capabilities": {"steer_mode": "in-place", "continue_without_message": true}
}
```

During active work, the caller uses `steer c1 MESSAGE` without a mode flag.

### V2: Each call MUST select its work by role.

**V2a:** A session MUST have at most one active turn.
When the caller addresses the session, the tool MUST select work as follows:

| Role | Work selected at the start of the call |
|---|---|
| Status or wait | The active turn; otherwise the most recent terminal turn. |
| Cancellation | The same selection as status or wait. A terminal turn keeps its observed state. |
| Correction | The active turn. With none active, fail with `kind` `conflict`. |
| Pause or resume, when supported | The same selection as status or wait; suspension behavior follows M3. |

An unknown or expired session MUST fail with `kind` `not_found` for every role under the identifier rule (M1d).
A status, wait, cancellation, pause, or resume call with no work to select MUST fail the same way.
A `null` status in a wait error means no state was observed (M1b).

**V2b:** Cancellation and correction MUST keep their selected work until the call ends. Status and wait follow the changing-selector rule (M1g).
A control request MUST NOT act on newer work if its selected work ends before the request takes effect.
A correction whose selected work is no longer eligible MUST fail with `kind` `conflict`, except for the completed-work case in the cancel-then-start rule (V3c).

A new call selects again and can observe or control newer work.
The tool MUST document this behavior for cancellation and correction.

**Example: a wait and a late cancellation.** In this tool, `mytool wait c1` keeps observing the work selected at the start, even if new work is later accepted.
After a timeout, another `wait c1` selects again and can observe newer work.

If `cancel c1` selects work that finishes just before cancellation takes effect, the command reports its actual terminal state.
It does not cancel a newer turn or claim that the selected work was canceled.

**V2c:** A direct follow-up MUST fail with `kind` `conflict` while work remains non-terminal.
The error's `hint` SHOULD name the correction role and, when the tool offers a queue, the queue role as the alternatives.

**Example: choosing when a message applies.** While the agent works on login, a caller can use `steer c1 'Check the expiry first'` to correct that work.
A tool with a follow-up queue can accept `queue c1 'Then add tests'` for later.
`continue c1 'Then add tests'` fails with `conflict` while work is still running; its `hint` points to `steer c1` for the running work and `queue c1` for later.

### V3: Correction MUST report what was accepted or delivered.

Correction changes the approach to current work.
The supported mode determines whether that work stays in the same turn or continues in a new one.

**V3a:** These shared mode names have the following meanings:

| Mode | Meaning |
|---|---|
| `in-place` | Adds the instruction to the active turn, possibly at the next supported point after tool use. It does not start a new turn. |
| `cancel-then-start` | Requests cancellation of the selected turn, then starts a new turn using the same session's retained context and recorded progress after the selected turn is known to have ended. |

A tool whose only shared mode is `cancel-then-start` satisfies this extension in full.
A tool MAY offer additional modes or an explicit choice with documented behavior.
After a failure, the tool cannot switch modes silently under the no-silent-substitution rule (F4c).

The declaration for the whole command still follows the effect-classification rule (R1); idempotent cancellation does not make cancel-then-start idempotent.

**V3b:** A structured correction result MUST include `correction_result` with the fields below.
The table defines the fields inside `correction_result`, including when the object uses a replacement name under the retained-name rule (B4c).
`target_status` is not reserved outside this object.
`steer_mode` also names the default mode in the session-capability object (V1c); here it reports the mode selected for this call.
`message_state` also reports acceptance of a queued message in an enqueue receipt (V7b).

| Field | JSON type | Presence | Value and meaning |
|---|---|---|---|
| `steer_mode` | string | MUST, always | Selected mode. |
| `target_status` | string or null | MUST, always | Last observed state of the selected work, or `null` if none was observed. |
| `message_state` | string | MUST, always | `accepted`: responsibility for handling the instruction was accepted; `delivered`: inclusion in the intended turn's context was confirmed; `not_delivered`: the instruction is known not to have reached that context; `unknown`: acceptance or delivery cannot be determined. |

**Session and turn results:**
The enclosing result or error `context` identifies the session under the session-addressing rule (V1b).
On success, the outer `status` describes the turn to observe next. This is the selected turn for in-place, or the accepted new turn for cancel-then-start.
`target_status` always describes the originally selected turn.
If correction fails after selecting its target, the error's `context` MUST include `correction_result` with the last known facts.
A correction that fails before selecting a target, such as with no active turn (V2a), does not require `correction_result`.

**Acceptance and delivery:**
For in-place, the instruction is intended for the selected turn. For cancel-then-start, it is intended for the new turn.
Command detail states whether success means that the tool accepted the instruction or confirmed delivery.
An in-place receipt can report `accepted` before delivery is confirmed.

**Evidence:**
The tool MUST NOT infer `delivered` from a transport write alone or report it as a guarantee that the agent obeyed the instruction.
Uncertain effects follow the uncertain-outcome rule (F4a).

**Example: accepting an in-place correction.** This tool reports acceptance before delivery is confirmed.
`mytool steer c1 'Check the expiry first' --json` succeeds with this stdout:

```json
{
  "session_id": "c1",
  "status": "running",
  "changed": true,
  "partial": false,
  "capabilities": {
    "steer_mode": "in-place"
  },
  "correction_result": {
    "steer_mode": "in-place",
    "target_status": "running",
    "message_state": "accepted"
  }
}
```

The caller can use `wait c1` to observe the work.

**V3c:** **Applies when using `cancel-then-start`.** The tool MUST coordinate these steps:

1. Request cancellation of the selected work and establish that it ended, by observation or a documented guarantee of the interface used.
2. Start the corrected work in the same session.

A cancellation timeout alone MUST NOT permit the second step.
If the selected work finishes before cancellation takes effect, the tool MAY proceed when no other work has since been accepted for execution. It reports the old work's actual terminal state.
If another call starts different work before the correction can proceed, the correction MUST fail with `conflict` without changing that newer work.

**Preserving progress:**
The new turn MUST receive the correction instruction, retained conversation context, and access to recorded progress, including retained tool results and existing work artifacts.
Switching turns MUST NOT by itself reset the conversation, undo completed effects, or discard retained results.
The tool does not have to preserve a stopped process's memory or unrecorded model reasoning.
Unavailable context follows the recovery rules (V5c).

**When a step fails:**
A deadline does not undo cancellation already requested.
The cancellation step follows the narrow-cancellation safeguard (R2c); other effects retain their safeguards.

**Example: redirecting work without losing progress.** This tool uses `cancel-then-start` as its default correction mode.
The current work has changed five files and recorded test results during 30 minutes of execution.

```console
$ mytool steer c1 'Use the existing OAuth helper' --background --json
```

```json
{
  "session_id": "c1",
  "status": "running",
  "changed": true,
  "partial": false,
  "capabilities": {"steer_mode": "cancel-then-start"},
  "correction_result": {
    "steer_mode": "cancel-then-start",
    "target_status": "canceled",
    "message_state": "accepted"
  },
  "next": ["mytool", "wait", "c1"]
}
```

The new turn uses the helper while retaining the files, conversation, and recorded test results.
The caller uses `wait c1` to observe the corrected work.
In this tool, a `wait c1` that was already running follows the session and returns the corrected work's answer, as its documentation states.

**Example: a correction that did not complete.** Each row is a separate cancel-then-start call on an active session, with non-TTY stdin and stderr.
Each call exits non-zero with empty stdout. The last non-empty stderr line contains the error.
Its `context` includes `session_id` and `correction_result`.

| What the tool knows | `kind` | `target_status` | `message_state` |
|---|---|---|---|
| The selected work was canceled; the backend temporarily rejected the next prompt. | `unavailable` | `canceled` | `not_delivered` |
| The deadline expired after requesting cancel; a read still shows the selected work running. No new instruction was sent. | `timeout` | `running` | `not_delivered` |
| Cancel was requested, but its effect could not be observed. No new instruction was sent. | `outcome_unknown` | Last observed state or `null` | `not_delivered` |
| The next prompt may have been accepted, but its receipt was lost. | `outcome_unknown` | Last observed state or `null` | `unknown` |
| Another call started different work after the selected work ended and before the correction could proceed. | `conflict` | Its terminal state, such as `canceled` | `not_delivered` |

The first row produces this error:

```json
{"error":{"kind":"unavailable","message":"The previous work was canceled; the correction prompt was not accepted.","context":{"session_id":"c1","correction_result":{"steer_mode":"cancel-then-start","target_status":"canceled","message_state":"not_delivered"}},"hint":"Inspect the session before deciding whether to send the prompt again.","next":["mytool","status","c1"]}}
```

A lost receipt does not prove that the new instruction was rejected.

### V4: Pending input MUST have a clear delivery policy.

**Applies when:** the tool accepts a message before it can deliver that message to the agent.
This includes in-place corrections waiting for tool use to finish. It does not require a follow-up queue.

**V4a:** The tool MUST document these parts of its pending-input policy:

- The delivery point and ordering.
- Storage bounds, an explicit choice to have no bound, or limits the tool cannot determine for corrections forwarded to the system running the agent.
- What happens to undelivered corrections when their selected work ends.

An in-place correction MUST NOT silently become input to a successor.
Another message MUST NOT silently overwrite accepted input.
An explicit replacement feature MAY be provided with documented behavior.

**V4b:** A successful receipt MAY report `message_state: accepted` without waiting for delivery.
The tool MUST document whether the caller can later distinguish delivery, non-delivery, and an unknown outcome for that instruction, and how to do so.
If later per-instruction receipts are offered, the tool MUST provide an unambiguous way to retrieve each receipt after other messages are accepted.

No particular message identifier or receipt command is required.

**Example: two corrections during one tool call.** This tool accepts pending corrections in order and does not offer later individual delivery receipts:

1. The agent starts a tool call.
2. The caller sends corrections X and Y. Both calls return `accepted`.
3. The tool call ends, and the tool adds X and Y before the next model step in the same turn.
4. `wait c1` returns that turn's answer, not separate answers for X and Y.

The model may still ignore either instruction. Successful completion alone does not establish delivery.

**V4c:** For corrections whose pending state it can determine, the tool MUST provide a non-consuming read of the session's pending messages, usable in a *non-interactive context*.
The read MUST use a list or at least a non-negative integer count.
The tool MUST document what the list or count includes.

If the tool cannot determine whether forwarded corrections are still pending in the system running the agent, it MUST document that limitation and MUST NOT report the unknown state as zero pending corrections.
The read MUST then report that state explicitly rather than omit it, for example with `null` in place of the count or with a documented field, and the command's `output_description` MUST name that representation.

The read can be part of status or a separate command. A list follows the bounded-collection rules (O6).
When the read returns a list, each item SHOULD include a preview of the message's text; shortened message text follows the bounded-value rules (O5c), and the `truncated` and `output_file` fields belong to the same list item as its preview.

A read describes the state when observed, not a promise that it will remain unchanged.
No particular field name, message identifier, full prompt text, or individual removal command is required.
A count does not establish delivery of a particular message under the receipt rule (V4b).

### V5: Waiting and context MUST preserve the conversation contract.

**V5a:** Wait MUST return the retained answer when it observes a terminal turn and an answer is available, including a partial answer after failure.

**Observation timeouts:** Wait and commands waiting for work they started follow these output rules when they fail with `kind` `timeout`:

- By default, the command MUST emit no result, even if a partial answer was recorded. Stdout is empty.
- The tool MAY offer an explicit choice to return an available partial answer on that timeout, using `partial: true` under the incomplete-result rule (O5a).

Returning a fragment does not change the failure kind required by the wait or started-work rules (M1e, R7d), including `outcome_unknown` when required.

Reading a retained answer MUST NOT consume it.
Once a wait has observed a terminal turn with an available answer, that call MUST NOT switch to a later turn; a change of target before that follows the changing-selector rule (M1g).
The tool MUST document its retention policies for answers and for context needed to continue the conversation. These policies can differ.

**Example: checking work after a wait timeout.**

```console
$ mytool wait c1 --timeout 30s
```

If work is still running at the deadline, stdout is empty and the last non-empty stderr line reports `timeout`.
This tool's hint points at its progress log, a tool-specific command, before the state read:

```json
{"error":{"kind":"timeout","message":"Work is still running after 30s.","context":{"session_id":"c1","status":"running"},"hint":"Read new progress with mytool log c1 --tail, then check the state with mytool status c1.","next":["mytool","status","c1"]}}
```

The caller checks the current state:

```console
$ mytool status c1 --json
{"session_id":"c1","status":"running","capabilities":{"steer_mode":"in-place"}}
```

None of these calls starts new work or repeats an ever-growing answer fragment; new log lines are a cheaper breadcrumb than a repeated partial answer.

**V5b:** **Applies when the tool knows a limit is blocking work.** Status MUST report:

- The reason work cannot continue.
- Whether automatic continuation is enabled.

Field names are tool-defined.
The condition is assessed for the agent configuration selected for the session.
A reported reason or expected return time MUST come from observed information. Silence alone is not evidence.
If the tool waits for a temporary limit and will automatically continue, that wait MUST remain non-terminal in the same turn.
Permanent failure to continue ends the turn as `failed`.

Sending the prompt again is a request retry under the safe-retry rules (R6), not another observation of the turn.

**Example: waiting for a rate limit to clear.** This session-only tool can observe the limit and uses domain-specific `waiting`, `reason`, and `auto_continue` fields in its status result:

```json
{"session_id":"c1","status":"waiting","reason":"rate_limit","auto_continue":true,"capabilities":{"steer_mode": "cancel-then-start"}}
```

The caller runs `mytool wait c1 --timeout 30s` in a non-interactive context with non-TTY stderr.
If the turn still waits for the limit when that deadline expires, the command exits non-zero with empty stdout and this stderr error:

```json
{"error":{"kind":"timeout","message":"Work is still waiting for the rate limit to clear.","context":{"session_id":"c1","status":"waiting"},"hint":"Check the state with mytool status c1."}}
```

When work resumes, it continues in the same turn.
The caller does not send a new prompt merely to resume waiting.

**V5c:** The tool MUST support reopening a retained session after the initiating process exits or restarts, subject to its documented context-retention policy (V5a).
Reopening a session does not by itself start work or prove that previous work ended.
Loss of a connection MUST NOT by itself be reported as cancellation or be treated as a reason to start the same work again.

**Applies when a follow-up or correction needs restored context:**
The tool MUST document the conditions needed for restoration.
If required context cannot be restored, it MUST fail instead of silently using a new conversation or an incomplete reconstruction.
A separately requested new conversation based on retained history MAY be offered without claiming identical context.

**Example: continuing stopped work after reopening the tool.** This session's recorded context remains available.
The caller previously stopped the work with `mytool cancel c1`, then closed the laptop.
After reopening the tool, status confirms `canceled`:

```console
$ mytool continue c1 --background
```

This tool supports the no-message follow-up, so no new message is required. The new turn continues the unfinished task using the retained instruction, conversation, and recorded progress.
It does not recreate the old process or undo files already changed.
After a network interruption, the caller checks status first: work might still be running.

**V5d:** **Applies when a turn needs caller input or permission to proceed.** Requests follow the late-input rule (I5d).
Denying a tool action can still let the agent continue.
The tool MUST NOT report cancellation of the whole turn unless the turn actually ends that way.
A question in a completed answer uses a normal follow-up instead.

### V6: Answers MUST remain readable as documents.

**V6a:** The following roles MUST provide *document commands* for non-interactive use:

| Role | Recommended command name |
|---|---|
| Start a conversation | `run` |
| Follow up | `continue` |
| Correct active work | `steer` |
| Wait for work | `wait` |

On non-TTY stdout, these commands MUST default to the tagged `text` presentation below.
When this differs from the tool-wide `format_defaults`, each of these commands declares its own `format_defaults` in its command detail (O2a).
This applies to both answers and acceptance receipts; a receipt with no answer omits the answer section.
Status and cancellation follow the general format-selection rule (O2c), normally JSON on non-TTY stdout.
Explicit JSON returns the original answer string rather than the text wrapper and can include more metadata under the text-selection rule (V6c).

Help MUST describe `--json` as a choice for programmatic parsing, not as a prerequisite for an agent to read an answer.
An answer-reading breadcrumb SHOULD use the default presentation unless the workflow needs programmatic parsing.

**V6b:** The tool, not the responding agent, MUST build the text wrapper:

**Layout:**

- The `<result>` opening tag carries the session identifier and `status` as double-quoted attributes. Their attribute names are the JSON field names.
- If the JSON contract requires `partial`, the opening tag MUST include the same boolean value as JSON.
- Fields selected for text metadata under the selection rule (V6c) MUST appear as one JSON object in `<metadata>`, serialized on a single line. Fields already shown as attributes and the answer field are excluded. With no selected fields, this section can be omitted. When both sections are present, `<metadata>` precedes the answer section.
- An available answer MUST appear in an answer section with its text, line breaks, and Markdown as they are. Its tags are `<answer>` and `</answer>` unless Answer boundary below requires `<answer-N>` and `</answer-N>`. Beyond the terminal-control escaping of O3d, no string in it is replaced. With no answer, omit the section; do not invent an empty answer. An empty answer string is an available answer and produces a section with one empty line.
- Each section's opening and closing tags and the closing `</result>` MUST occupy their own lines. After a section's opening tag the tool writes one newline, then the section content, then one newline, then the closing tag. These wrapper newlines are not part of the content. The answer is everything between the newline after the opening answer tag and the newline before the closing one, so an answer that ends with a newline shows an empty line before the closing answer tag.

Attribute names MUST start with an ASCII letter or underscore and contain only ASCII letters, digits, underscores, or hyphens.

**Escaping:**

- Attribute values MUST escape `&`, `<`, `>`, and `"` as `&amp;`, `&lt;`, `&gt;`, and `&quot;`. Tabs, CR, and LF use `&#9;`, `&#13;`, and `&#10;`. Other values use their documented text form.
- Metadata uses ordinary JSON serialization; no additional escaping of `<` is required.
- In the answer, terminal-control escaping still applies (O3d). Nothing else in the answer is escaped; the wrapper's own tag names are written as they appear in the answer.

**Answer boundary:**

- When the answer text contains any of the exact strings `<result>`, `</result>`, `<metadata>`, `</metadata>`, `<answer>`, or `</answer>`, the tool MUST write `<answer-N>` as the section's opening tag and `</answer-N>` as its closing tag. N is the decimal integer, without leading zeros, equal to one plus the number of LF characters (`U+000A`) in the displayed answer text. A CR before an LF is answer text and is not counted. Matching is case-sensitive and covers only these six strings; `<answers>`, `<Answer>`, `<answer/>`, and these tag names with attributes do not count. Without such a string the tags are plain.
- With counted tags, the answer consists of exactly the N lines after the `<answer-N>` line, split on LF with any CR kept in its line. The line after them consists of the matching `</answer-N>`, compared as a whole tag name rather than a prefix. An earlier answer line equal to `</answer-N>` remains answer text: a reader uses N to locate the boundary and the closing tag to validate it. With plain tags, the first line consisting of `</answer>` closes the section.

For example, an answer of five lines whose code sample contains the wrapper's closing tag is presented as:

````text
<result session_id="c5" status="succeeded" partial="false">
<answer-5>
Close each section explicitly:

```text
</answer>
```
</answer-5>
</result>
````

The reader takes the five lines after `<answer-5>`; the `</answer>` inside the code sample is one of them, and `</answer-5>` on the sixth line validates the boundary. The same call with `--json` returns the original string.
The counted form avoids collisions with the six trigger strings without changing the text: N defines the answer extent, the matching closing tag after those N lines validates the boundary, and wrapper-like lines within that extent remain untrusted answer text.
A reader that only matches the closing tag can be misled by an answer written to imitate the wrapper; a reader that counts the lines cannot.
An answer without these strings uses the plain tags, so the common case has no counter.
This is a readable presentation, not strict XML. A caller that acts on `next` or other metadata programmatically uses JSON, which returns the original answer text.
The tags do not make an agent's answer trusted instructions.

**V6c:** The text presentation MUST retain these details when present in the JSON result:

- The session-capability object, `correction_result`, and `next`.
- Reported permission denials or policy changes, and recovery warnings.
- Reported limit-related waiting reasons and whether automatic continuation is enabled.
- Result-preview notices and information for retrieving the complete result.

When the JSON result reports usage, such as tokens or cost, the text SHOULD retain it.
Other JSON fields MAY be omitted from text when this preserves the result's meaning under the rendering-consistency rule (O3c).
Command detail SHOULD document the selection of text metadata.
For example, a tool can keep timing fields only in JSON.

Tool activity records, reasoning traces, and the full transcript MUST NOT be included automatically in the default result of any format, including metadata.
They require an explicit read or selection; `--json` selects only the format.
Concise status, usage, permission, and recovery metadata can accompany the answer.

**Example: reading an answer and asking a follow-up.** Continuing the start example, `mytool wait c1` returns this stdout with exit `0`:

````text
<result session_id="c1" status="succeeded" partial="false">
<answer>
## Login failure

The OAuth client secret has expired.

| Check | Result |
|---|---|
| Client ID | Valid |
| Client secret | Expired |

Check the expiry before attempting login:

```python
if credentials.expires_at <= now:
    raise AuthenticationError("Client secret expired")
```
</answer>
</result>
````

The table and code remain readable without decoding a JSON string.
Calling the same wait with `--json` returns an object with `session_id`, `status`, `partial`, and the original Markdown in `answer`. Reading it does not rerun the agent.

The caller can now ask a follow-up using the same conversation:

```console
$ mytool continue c1 'Explain how to test the expiry check' --background
```

This tool's text receipt omits `changed`, which remains in its JSON result, and has no answer section because the new answer is not ready:

```text
<result session_id="c1" status="running" partial="false">
<metadata>
{"capabilities":{"steer_mode": "cancel-then-start"},"next":["mytool","wait","c1"]}
</metadata>
</result>
```

**Example: a failed turn with a retained answer fragment.** In this separate conversation, the agent has stopped permanently and cannot finish its response.
`mytool wait c2` exits non-zero. Its stdout contains the available answer:

```text
<result session_id="c2" status="failed" partial="true">
<answer>
## Login failure

The client secret has expired. The second check also found
</answer>
</result>
```

With non-TTY stderr, the last non-empty stderr line is:

```json
{"error":{"kind":"operation_failed","message":"The work failed before finishing its answer.","context":{"session_id":"c2","status":"failed"}}}
```

Unlike the default observation-timeout result, this returns the available answer because the turn has ended. The completeness signal still describes the answer.

### V7: A follow-up queue MUST stay under explicit control.

**Applies when:** the tool offers a follow-up queue.
Session addressing, call targets, correction, pending input, waiting, and presentation keep their rules (V1 to V6); this requirement adds what a queue changes.
A tool without a queue has nothing to do here.

**V7a:** Queueing MUST require an explicit caller choice, separate from correcting active work or starting a direct follow-up.
An option on another command can provide the same explicit choice.
The enqueue, inspection, and restart roles MUST accept the session identifier alone under the session-addressing rule (V1b), and MUST be usable in a *non-interactive context*.
The tool MUST provide the pending-message read (V4c) for the queue, wherever messages are stored.

**V7b:** A successful enqueue MUST return the session identifier and `message_state: accepted` in its structured receipt.
This confirms responsibility for the message, not acceptance or completion of a turn. The receipt MUST NOT use a turn's `status` to describe a queued message.

Correction, enqueue, and follow-up replies SHOULD include the pending-message count and whether automatic execution is stopped, when these facts can be determined.
This applies to text and structured replies; on failure, the information belongs in the error's `context`.
Enqueue and queue reads follow the general format-selection rule (O2c), normally JSON on non-TTY stdout.
A separate restart command uses the tagged text presentation for answers (V6a) when it accepts a turn for execution.

**V7c:** Each queued message MUST start a new turn when selected for execution. Only then do the turn's acceptance, status, and wait rules apply.
The tool SHOULD execute messages in acceptance order and automatically start the next one when current work ends without an explicit stop.
The tool MUST document execution order, automatic-start conditions, and queue retention across process exits and restarts.
If that policy does not permit automatic execution after work ends, the queue MUST remain stopped until the caller explicitly restarts it.

When the session is idle, an enqueue can start execution immediately unless the queue is stopped; it still returns the queue receipt.
Automatic execution can start newer work before a later `wait` call reads the earlier answer.
A wait that follows the session selector (M1g) MUST NOT cross from its selected work into a turn started automatically from the queue; observing that turn takes a new wait call.
A tool can offer a history read for those answers; this extension does not require one.

**V7d:** Pending follow-up messages MUST NOT prevent correction.
A direct follow-up MUST fail with `kind` `conflict` while the queue holds pending messages that can still start automatically; this adds to the direct-follow-up rule (V2c).
The tool MUST handle stopping and restarting as follows:

| Event | Automatic execution | Queued messages |
|---|---|---|
| An explicit session cancellation, including when the selected work is already terminal | Stops | Preserved under the declared retention policy |
| A message queued while execution is stopped | Stays stopped | Added |
| Cancellation requested inside a cancel-then-start correction (V3c) | Stops until the corrected work is accepted; a successful correction does not stop later execution by itself | Preserved |
| The correction request fails after cancellation was requested | Stays stopped until the caller explicitly restarts | Preserved |
| A direct follow-up, with or without a message, not yet confirmed as accepted | Stays stopped | Preserved |
| That follow-up accepted | Re-enabled; queued messages run after the follow-up ends, under the documented policy | Preserved |
| A direct follow-up rejected with `kind` `conflict` (V2c, this rule) | Unchanged | Unchanged |
| A restart requested while work is active | Fails with `kind` `conflict` | Unchanged |
| A no-message follow-up after the previous work succeeded, with pending messages | Starts the next queued message directly | The first message starts |

The failure row concerns the two correction steps, not the agent's later execution of the correction.
The tool MUST document how to restart execution without resubmitting the messages. The follow-up role provides this restart; a separate restart command is optional.

**Example: queueing later work and reading pending input.** This tool offers `queue` and reports its follow-up count and stopped state in `queue` in replies and status.
The count excludes corrections waiting for delivery in the active turn. Queued messages execute in acceptance order after current work ends, unless the caller stops the queue.
While the agent is working:

```console
$ mytool queue c1 'Then add tests' --json
{"session_id":"c1","message_state":"accepted","changed":true,"partial":false,"capabilities":{"steer_mode": "in-place"},"queue":{"pending_messages":1,"stopped":false}}
$ mytool queue c1 'Then update the docs' --json
{"session_id":"c1","message_state":"accepted","changed":true,"partial":false,"capabilities":{"steer_mode": "in-place"},"queue":{"pending_messages":2,"stopped":false}}
$ mytool status c1 --json
{"session_id":"c1","status":"running","queue":{"pending_messages":2,"stopped":false},"capabilities":{"steer_mode": "in-place"}}
```

The caller can still use `steer` to correct the current work without removing those two messages:

```console
$ mytool steer c1 'Check the expiry first' --json
{"session_id":"c1","status":"running","changed":true,"partial":false,"capabilities":{"steer_mode":"in-place"},"correction_result":{"steer_mode":"in-place","target_status":"running","message_state":"accepted"},"queue":{"pending_messages":2,"stopped":false}}
```

The follow-up count remains two; it does not count the correction.
An enqueue can report zero pending follow-ups if its message has already started execution.
A tool offering more detail could expose this queue read.
Here `preview` holds each message's text, shortened under the bounded-value rules (O5c) when long.
Both messages fit, so neither preview is truncated:

```console
$ mytool pending c1 --json
```

```json
{
  "session_id": "c1",
  "items": [
    {"preview": "Then add tests", "truncated": false, "delivery": "follow-up"},
    {"preview": "Then update the docs", "truncated": false, "delivery": "follow-up"}
  ],
  "has_more": false
}
```

For a longer message, its item would contain `truncated: true` and an `output_file` such as `/home/user/.cache/mytool/queued-message.txt` holding that message's full text.

**Example: stopping and restarting the queue.** Continuing the queue example, assume cancellation finishes during the call:

```console
$ mytool cancel c1 --json
{"session_id":"c1","status":"canceled","changed":true}
$ mytool pending c1 --json
{"session_id":"c1","items":[{"preview":"Then add tests","truncated":false,"delivery":"follow-up"},{"preview":"Then update the docs","truncated":false,"delivery":"follow-up"}],"has_more":false}
```

Nothing starts automatically while execution is stopped, even if another message is queued.
Later, with the session's recorded context still available:

```console
$ mytool continue c1 --background --json
{"session_id":"c1","status":"running","changed":true,"partial":false,"capabilities":{"steer_mode":"in-place"},"queue":{"pending_messages":2,"stopped":false}}
$ mytool wait c1
```

This tool supports the no-message follow-up; the accepted follow-up continues the unfinished work and re-enables the queue.
After that work finishes, this tool automatically starts the queued tests, then the documentation task.
No separate queue restart is needed.
If the earlier work had already succeeded before cancellation, `continue c1` would start the queued tests directly.
Each wait observes the work it selects under the call-target rules (V2), not the whole queue.
Reopening the session after the tool's own process restarts follows the recovery rules (V5c).
