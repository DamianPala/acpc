I’ll inspect the three requested sources in order, then run the two help commands and use only that evidence plus the stated facts for the design. I won’t modify files or git state.# acpc agent-facing calling surface

## 1. What exists

Observed from the requested files and help output:

- `acpc run` is an alias for `prompt`.
- The default non-TTY permission policy is `read`.
- The default TTY permission policy is `prompt`.
- Non-TTY calls use the daemon unless bypassed.
- `--permissions` answers ACP permission requests and is not a sandbox.
- `--mode` passes an adapter-specific session mode without validation or enforcement.
- `--json` forwards ACP session-update events as NDJSON.
- `--quiet` emits only collected final text.
- Session IDs are currently reported on stderr.
- `--last` is scoped by parent process and is unsafe for concurrent orchestration.
- `acpc sessions` is still a stub.
- `--print-session-id` requires the surprising combination `--quiet -o FILE`.
- `-o FILE` currently tees output despite its underspecified help text.
- `EXIT_PERMISSION_DENIED = 3` is documented but unreachable.
- A timeout returns immediately, but the adapter may continue generating and keep the session busy.
- The daemon saves adapter startup cost, not model inference latency.

The current help is readable for a human who already knows ACP, but it does not state the default policy, daemon routing, mode semantics, output contract, or failure taxonomy where the caller needs them.

## 2. Self-introspection

### What makes a tool usable first try

Observed from my tool schemas and instructions:

- Required arguments are explicit.
- Enumerated values are safer than free-form strings.
- Side effects, sandbox scope, working directory, timeout, and output shape are visible before invocation.
- Structured results expose exit status separately from textual output.
- Long-running calls expose a resumable execution identifier.
- Approval requirements are attached to the operation instead of hidden in prose.
- Examples clarify whether a result is a final answer, a stream, or a handle.

Inference:

- A CLI for an orchestrator needs the same information in `--help`, not in a README.
- The output schema matters more than command brevity.
- A stable handle is more useful than a human-readable session ID buried in stderr.
- Defaults must be safe and predictable when the caller supplies nothing.

### How I misuse CLIs

Observed or strongly inferred failure patterns:

- I treat a flag named `--permissions` as a boundary unless the help prominently says it is only a callback policy.
- I assume similarly named modes are comparable across adapters.
- I omit flags when defaults are undocumented, then discover that non-TTY input silently means `read`.
- I select `--mode read-only` and infer enforcement from acceptance, although codex-acp 1.1.9 ignores it.
- I use `--last` in concurrent calls and accidentally resume the wrong logical conversation.
- I parse human stderr to recover a session ID because no structured metadata is available.
- I select `--json` expecting a concise result and receive every low-level event instead.
- I assume a timeout releases the session immediately.
- I assume `-o FILE` replaces stdout when it actually duplicates output.
- I mistake an adapter error, an acpc transport error, and an agent refusal because they share exit code 1.
- I spend context on progress and tool-call events that do not change my decision.
- I retry a command after an ambiguous failure and may duplicate an edit or create a second session.

### What I delegate

I pass:

- The concrete task and success condition.
- Relevant repository or working-directory context.
- Constraints such as read-only, no network, or no destructive changes.
- The desired model or capability tier.
- A requested output shape, usually a concise result with paths, tests, and unresolved issues.
- A stable session or job handle when continuation is expected.

I need back:

- A machine-readable status.
- The final answer or a durable result handle.
- The session ID and job ID.
- Whether the agent completed, failed, timed out, was cancelled, or was blocked by permission.
- Changed paths and verification results when relevant.
- A concise error with the failing layer and whether retrying is safe.

I do not need every internal thought, token, tool argument, or plan transition.

### Permission and sandbox inheritance

Observed:

- The calling tool surface gives me an execution sandbox and may require approval.
- The spawned adapter inherits the process environment and host access available to acpc.
- `--permissions` only controls acpc's response to ACP permission callbacks.
- `--mode` belongs to the adapter session and has adapter-specific meaning.

Inference:

- Inheriting the caller's logical permission policy would be wrong because acpc's policy and the adapter's sandbox are different mechanisms.
- Inheriting the caller's filesystem and network boundary would be right only if acpc can actually enforce that boundary.
- Until it can, acpc must never imply that inheritance happened.
- The safe contract is explicit adapter mode plus explicit acpc permission policy, with a prominent statement that neither substitutes for process isolation.
- A default that grants broader access than the caller's known boundary is unsafe.
- A default that silently promises read-only behavior while codex ignores its mode is dishonest.

## 3. Communication during a turn

### What should be visible

The default non-TTY synchronous call should emit only the final agent answer on stdout and sparse lifecycle diagnostics on stderr.

Every additional byte of progress is context consumed by the orchestrator, so progress must be opt-in and coalesced.

| ACP update | Default | Explicit stream |
|---|---|---|
| Agent message chunks | Accumulate | Emit as `message.delta` |
| Tool call started/finished | Hide | Emit a short summary |
| Tool arguments and raw output | Hide | Emit only with debug mode |
| Plan updates | Hide | Emit the latest plan snapshot, not every transition |
| Permission requests | Surface as control events | Always surface to an approval-capable caller |
| Usage counters | End summary only | Sparse cumulative updates |
| Adapter logs | Never stdout | Stderr or daemon log |
| Final result | One result | One terminal `result` event |

Token counters are useful for budgeting when the adapter provides them, but estimated cost must not be invented.

The default should not stream low-level ACP events merely because ACP carries them.

### Streaming

Use a stable acpc event envelope instead of forwarding raw ACP by default:

```json
{"type":"started","job_id":"j_123","session_id":"s_123"}
{"type":"message.delta","job_id":"j_123","text":"..."}
{"type":"tool.finished","job_id":"j_123","kind":"edit","summary":"updated src/app.py"}
{"type":"result","job_id":"j_123","status":"completed","text":"..."}
```

`--events` should select this stable schema.

Keep `--json` as a compatibility alias or deprecate it toward `--events`.

Reserve `--raw-acp` for protocol debugging.

### Non-blocking dispatch

Add a persistent job layer:

```text
acpc run codex "fix the tests" --detach
{"job_id":"j_123","session_id":"s_123","status":"running"}
```

Add these commands:

```text
acpc job status JOB_ID
acpc job events JOB_ID [--follow]
acpc job result JOB_ID
acpc job cancel JOB_ID
acpc job approve JOB_ID REQUEST_ID
acpc job deny JOB_ID REQUEST_ID
acpc job send JOB_ID "additional instruction"
```

`job result` should return the final answer once and remain idempotent.

`job events --follow` should stream selected stable events and exit when the job reaches a terminal state.

The job record must persist status, session ID, result location, failure stage, timestamps, and ownership.

The cost is a new persistent state model, cleanup policy, access-control problem, and recovery path after daemon or adapter crashes.

The current synchronous `run` command should remain available as a convenience over submit plus wait.

### Mid-turn intervention

The minimum useful intervention set is:

- Cancel the active turn.
- Approve or deny a pending permission request.
- Queue one follow-up message for the next turn boundary.
- Inspect whether the job is waiting, running, cancelling, or finished.

Do not claim arbitrary mid-generation correction until ACP exposes a reliable input mechanism for it.

A normal agent message is not a safe substitute for a structured question request.

The current `stop` command can provide cancellation, but it needs a stable job handle and must report that cancellation may not stop adapter generation immediately.

### Multi-turn sessions

Keep `-s SESSION_ID` for compatibility, but make sessions first-class:

```text
acpc session send SESSION_ID "follow up"
acpc session status SESSION_ID
acpc session events SESSION_ID [--follow]
acpc session cancel SESSION_ID
acpc session close SESSION_ID
```

Every new session should return its ID in structured stdout, not only stderr.

`--last` should remain a human convenience and be explicitly discouraged for orchestration.

Real back-and-forth additionally needs:

- A stable session owner.
- Per-session locking or a defined message queue.
- Explicit busy and waiting states.
- Structured permission and input-request events.
- Durable result and event storage.
- A transcript or reliable adapter-backed history contract.

## 4. Proposed calling surface

### Command shape

Keep the short synchronous form:

```text
acpc run AGENT [PROMPT]
```

Add explicit machine controls:

```text
acpc run AGENT [PROMPT] \
  --cwd DIR \
  --model PRESET_OR_ID \
  --mode ADAPTER_MODE \
  --permissions POLICY \
  --events \
  --timeout SECONDS
```

Add `--detach` for non-blocking dispatch.

Keep `prompt` as an alias, but document `run` as the agent-facing name.

Rename `--permissions` conceptually to “permission-response policy” while retaining the old flag as a compatibility alias.

Do not add a vendor-neutral sandbox tier until acpc can enforce it.

### Defaults

When all optional flags are omitted:

| Property | Non-TTY caller | TTY caller |
|---|---|---|
| Permission-response policy | `read` | `prompt` |
| Adapter mode | Unset, adapter default | Unset, adapter default |
| Output | Final text only | Streaming text |
| Daemon | Enabled | Enabled after interactive daemon support |
| Model | Adapter default | Adapter default |

The non-TTY output change costs compatibility with callers that currently expect streamed text.

`--stream` should restore streaming explicitly.

`--permissions prompt` should fail clearly in a non-TTY process unless a separate control channel is supplied.

### Permissions and modes

Help must show this exact conceptual split:

```text
--permissions POLICY
  How acpc answers an ACP permission request.
  This is not a filesystem, network, or process sandbox.
  Default: read for non-TTY callers, prompt for TTY callers.

--mode MODE
  Adapter-specific session mode passed to session/set_mode.
  It is not comparable across agents and may not be enforced by the adapter.
  Use `acpc modes AGENT` to discover advertised values.
```

Add `acpc modes AGENT` with advertised values, descriptions, and enforcement warnings when known.

Validate an explicitly supplied mode against advertised values when possible.

Warn, or fail in a strict mode, when the adapter accepts a mode but is known not to enforce it.

Do not automatically map `read` to `read-only`, because Claude and Codex modes have different semantics.

Do not call `--permissions none --mode bypassPermissions` a universal contradiction, but explain that the first controls ACP callbacks while the second controls adapter prompting.

The cost is more adapter metadata, more help text, and less vendor-neutral convenience.

### Output and status

Stdout has exactly one role:

- Final text in default synchronous mode.
- Stable NDJSON events with `--events`.
- One machine-readable job or session handle for `--detach`.

Stderr contains diagnostics, warnings, sparse opt-in progress, and adapter logs when running directly.

`-o FILE` should write the selected final result and not repeat it on stdout unless `--tee` is explicitly supplied.

A terminal structured result should look like:

```json
{
  "type": "result",
  "status": "completed",
  "job_id": "j_123",
  "session_id": "s_123",
  "text": "...",
  "permissions_denied": 0
}
```

### Exit codes

Use distinct layers:

| Code | Meaning |
|---:|---|
| 0 | Agent completed its turn |
| 1 | Agent turn failed |
| 2 | acpc usage or configuration error |
| 3 | Permission request denied or could not be answered |
| 4 | Adapter unavailable or failed to start |
| 5 | ACP protocol, daemon, or acpc internal failure |
| 124 | Timeout |
| 130 | Interrupted |
| 141 | Broken pipe |
| 143 | Terminated |

The structured result must additionally contain `status` values such as `completed`, `completed_with_denials`, `agent_error`, `timeout`, `cancelled`, and `acpc_error`.

A permission denial that still lets the agent produce a useful answer should be represented as `completed_with_denials` and remain machine-visible.

Splitting today's code 1 breaks scripts that only test zero versus nonzero, but it prevents unsafe retries and makes orchestration decisions possible.

### Failure messages

Every failure should identify the layer, operation, and retry guidance:

```text
acpc: permission request denied: edit src/app.py
acpc: agent failed during turn: max_tokens
acpc: adapter failed to start: command not found: codex-acp
acpc: daemon protocol error for codex; retry with --no-daemon
acpc: timeout after 60s; cancellation requested, session may remain busy
```

Structured errors should include:

```json
{
  "type":"error",
  "status":"acpc_error",
  "stage":"daemon_connect",
  "retryable":true,
  "message":"..."
}
```

### Zero-knowledge help

`acpc --help` should list the agent-facing commands, including `job`, `session`, `modes`, `status`, `stop`, and `daemon`.

`acpc run --help` should include:

- The prompt argument and stdin behavior.
- The exact defaults for TTY and non-TTY callers.
- The distinction between permission policy and adapter mode.
- A warning that neither flag creates a sandbox.
- Examples for Codex and Claude showing different mode vocabularies.
- The synchronous versus `--detach` contract.
- The stdout, stderr, and `--events` contract.
- The result and failure schema.
- Exit-code meanings.
- The fact that timeout cancellation may leave a session busy.
- The recommendation to use explicit session IDs instead of `--last`.
- The semantics of `-o FILE`.
- Incompatible combinations such as `--events` with plain text output.
- A reminder that adapter mode acceptance does not prove enforcement.

The current cheat sheet should be replaced or corrected because it presents `--permissions none` as a dry run and implies that mode mappings are more meaningful than they are.

## Decision

Make synchronous non-TTY `run` return one final answer with a stable status contract, add explicit event streaming and detached jobs, and keep permission policy and adapter mode visibly separate.

The largest deliberate break is changing the default non-TTY stream from incremental text to final-only text.

That break buys deterministic orchestration, lower context cost, safe retries, and a surface I can use correctly without reading the README.