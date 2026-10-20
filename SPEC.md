# acpc Specification

This is the normative contract for the acpc command-line tool.
It describes the installed binary in this repository, and behavior changes land here in the same change as the implementation.

## Command surface

The primary caller is another agent using a shell tool.
The default path is `run`, which waits and prints the answer once.
`--background` returns a session id and directory; `wait` collects the answer later.
All session state is inspectable under `ACPC_HOME`.

The following table is the machine-readable command index embedded in this document. It is deliberately small: the conformance test compares these paths and effects with `acpc schema`, while the prose and help text may evolve independently.

| Command path | Effect |
| --- | --- |
| `agents check` | `read_only` |
| `agents create` | `non_idempotent` |
| `agents delete` | `non_idempotent` |
| `agents get` | `read_only` |
| `agents list` | `read_only` |
| `cancel` | `non_idempotent` |
| `continue` | `non_idempotent` |
| `daemon status` | `read_only` |
| `daemon stop` | `idempotent` |
| `delete` | `non_idempotent` |
| `install` | `non_idempotent` |
| `list` | `read_only` |
| `log` | `read_only` |
| `probe` | `read_only` |
| `prune` | `non_idempotent` |
| `resolve` | `read_only` |
| `run` | `non_idempotent` |
| `skills get` | `read_only` |
| `skills list` | `read_only` |
| `status` | `read_only` |
| `steer` | `non_idempotent` |
| `wait` | `read_only` |

`agents`, `daemon` and `skills` are command groups, not command entries, when invoked without the subcommand that performs useful work.
`schema` is reserved for introspection and is not listed in the index.
The root command prints help.

Every indexed command accepts `--json` and `--color`.
These two flags are published once as `global_flags` by `acpc schema`.
`--help` and `--version` are available through Click but are not command-surface flags.
Command-specific flags are published by `acpc schema <path>`, with names without leading hyphens and short spellings in `aliases`.

Every command declares one effect.
`read_only` does not change intended state.
An observation may persist an observed liveness change and its placeholder response, such as `status` recording `unknown` after a daemon disappears.
`idempotent` may change state but a repeated successful call with unchanged inputs does not perform a second transition.
`non_idempotent` has no such promise.

Every session selector accepts a four-character session id, a `--name` alias created by `run`, or `last`. `last` is resolved only in an interactive context, so an agent or script must use an id or an alias.

Every prompt-bearing command accepts exactly one prompt source: a positional argument, `-` for stdin, or `--prompt-file FILE`. The prompt limit is 1 MiB (1 048 576 UTF-8 bytes), enforced before a session directory or daemon is created. `--prompt-file` is read under the same cap. The limit is unrelated to the stdout cap `--max-output`.

### `agents`

`agents` manages shipped adapter entries and local variants through explicit subcommands.

```text
agents list [--limit N] [--plain] [--format text|json|plain]
agents get NAME [--models] [--commands] [--format text|json]
agents check [NAME] [--limit N] [--plain] [--timeout S] [--format text|json|plain]
agents create NAME --extends AGENT [--model M] [--effort E] [--mode MODE] [--permissions P] [--home DIR] [--format text|json]
agents delete NAME [--yes] [--format text|json]
```

`agents list` returns a bounded collection with `items` and `has_more`, defaulting to 100 entries. Entries are ordered by adapter name, ascending; each adapter is followed by its variants in name order, and the default window is the first 100 of that order. `--plain` prints one adapter or variant name per line and requires an explicit `--limit`. The text rendering fits the terminal width: each adapter heads its variants, a model longer than 35 characters and a home longer than 24 are cut in the middle, the description takes the width that remains, the home column is left out when that remainder is too short for a description, and a roster whose descriptions were cut says so once in a footer naming `agents get`; the machine formats carry every field in full.

`agents get` renders one adapter or variant.
The detail contains resolved values with provenance, environment declarations, and the adapter's advertised modes, models, slash commands, the effort levels the adapter offers per model acpc has observed, and the subscription windows last observed for the entry's home.
`--models` and `--commands` select the corresponding advertised view, and they are mutually exclusive; `--models` lists every observed effort row in full.
A variant points at its base adapter rather than duplicating the catalog.

`agents check` runs the adapter connection check without sending a prompt. Without `NAME` it checks every registered adapter and variant, bounded by the default limit of 100. Entries are ordered by name, ascending, and the default window is the first 100 of that order. With `NAME` it checks exactly one entry and returns a one-item collection with `has_more: false`; `NAME` cannot be combined with `--limit` or `--plain`. The default connection timeout is 30 seconds. A check that reaches the adapter and reports `ok: false` is still a successful command with exit code 0; failed checks are summarized on stderr. An `ok` item carries `models` and `efforts`, the counts of advertised models and of observed effort rows, and `windows`, the cached windows of the entry's home or `null`.

The advertised effort levels are observed, never configured. acpc records, per model, the values of the adapter's effort option as the adapter reports them: in the `session/new` response, in the response to each `session/set_config_option` and in every `config_option_update`, keyed by the model id acpc selected, or by the adapter's current model id when acpc selected none. Each row holds `levels`, the option's values in the adapter's order, `[]` when the adapter offers no effort option for that model; `current`, the value the option holds after acpc applied the entry's options, so the vendor's own default when the entry sends none; and `observed_at`. A run observes only the model it uses, and a turn that fails while the options are applied still publishes the rows it observed. `agents check`, and the probe behind a cache miss in `agents get`, switch the probe session through every model the adapter advertises, every preset model and every `[effort_by_model]` key, record a row for each the adapter accepts, skip the rest, and always rewrite the cache, so `agents check` is the forced refresh; a run rewrites the cache only when its data changed. The rows live under `advertised.efforts` in `cache/<adapter>/advertised.json` next to `modes`, `models` and `commands`, merged by model so a row survives until the same model is observed again; `agents get` shows them with the cache age and `agents check` counts them. An adapter that advertises no effort option has no rows and `[effort_by_model]` stays its only table. An observed row bounds the levels an entry may send for that model (see Agent variants).

The subscription windows are observed, never configured. When a run's adapter reports its rate-limit windows, acpc keeps the latest observation per base adapter and vendor home in `cache/<adapter>/limits.json`: `five_hour` and `seven_day`, each with `utilization` as the fraction the adapter reports and `resets_at` as epoch seconds, together with `observed_at` and the `session_id` that saw it. A newer observation replaces the home's row; a turn without the report changes nothing. Only runs refresh the rows: `agents check` sends no prompt and shows the cached row. `agents get` shows the row of the entry's home with its age, for adapters and variants alike, and `null` when none was observed; an adapter that reports no windows has none.

`agents create` writes a strict local variant under `ACPC_HOME/agents`. Existing names are a `conflict`, and names that are path-like or collide with an `agents` subcommand are invalid. It requires `--extends` and does not ask for confirmation.

`agents delete` removes only a local file under `ACPC_HOME/agents`, including a local override of a shipped adapter. It never removes a shipped adapter. The file is usually hand-written and acpc has no operation that restores it, so deletion is a narrow irreversible mutation and requires confirmation: a terminal caller is asked, and every other caller must pass `--yes`. An unknown name fails as `not_found` before the gate and does not require `--yes`.

### `cancel`

```text
cancel SELECTOR [--format text|json]
```

`cancel` selects the session's active turn when the call starts and requests ACP cancellation of that turn only. It reports the state actually observed afterward: a successful request may report `running` while the adapter is settling, and `changed` is `true` exactly when this call sent the request. If the selected turn ended before the request took effect, the result reports that turn's observed terminal state with `changed: false`, and a newer turn started meanwhile by `continue` or `steer` is left untouched; the next `cancel` selects again and can stop that newer turn, which is why the command is `non_idempotent`. Canceling one session never asks for confirmation. A session in `waiting` is canceled without contacting the adapter: the scheduled resumption is dropped and the turn ends `canceled`. On a finished session it is a successful no-op with `changed: false`; an unknown selector is `not_found`. A daemon-owned continuation preparation can be canceled before its prompt is sent and receives a no-prompt placeholder.

### `continue`

```text
continue SELECTOR [PROMPT | -] [--prompt-file FILE] [--permissions P]
    [--model M] [--effort E] [--mode MODE] [--cwd DIR] [--home DIR] [--name ALIAS] [--system-prompt-file FILE]
    [--output-file FILE] [--format text|json] [--background]
    [--timeout S] [--cancel-after S] [--limit-wait DURATION]
    [--max-output BYTES] [--quiet]
```

`continue` starts the next turn on a finished session and preserves the adapter context. It blocks and prints the answer unless `--background` is given. Model, effort, mode, home and the stored policy come from the session. A session whose first turn sent no effort sends none on `continue`. The sole resolution override is `--permissions`, which applies to this and later turns and re-selects a mode against the current adapter table.

A `continue` without a message, neither `PROMPT`, `-` nor `--prompt-file`, continues unfinished work: it is accepted when the session's last turn ended `canceled`, `failed` or `unknown`, and sends acpc's continuation instruction as the new turn's prompt. The instruction states how the previous turn ended, `canceled`, `failed` or `unknown`, and asks the agent to continue without repeating finished work; the usage-limit resumption sends the same instruction with the limit as the cause. The instruction is recorded in `prompt.md` like any prompt. After a `succeeded` turn there is nothing to continue and the call fails with `invalid_input`, exit 2, and a hint to supply a message. The capability object reports this support as `continue_without_message: true`.

The adapter session is verified before a cold resume sends the new prompt. A listing check and a replay check are independent; an unavailable check leaves the resume unverified rather than inventing certainty. If acpc cannot account for every prompt known to have crossed the adapter boundary, the result says `resume: unverified — delivery record incomplete`. A mismatch fails before the new prompt is sent. A one-shot session cannot be continued in any state; the call fails with `conflict` naming `one_shot`, before anything is sent.

Replay from `session/load` or `session/resume` is silent. It does not add old messages to the answer, transcript, stderr or context occupancy. A session in `starting`, `running`, `preparing` or `waiting` cannot be continued; the `conflict` error's hint names `steer` and `wait`, and for a session that has nothing in flight to correct in place (`starting`, `preparing`, `waiting`) it names `--steer-mode cancel-then-start`, so the hinted command never fails with a second `conflict`. Resolution flags that belong to a new dispatch (`--model`, `--effort`, `--mode`, `--cwd`, `--home`, `--name`, `--system-prompt-file`) are accepted only when they name the session's stored value, compared after the normalization `run` applies (a path resolved against the caller's directory, a preset name resolved through the session's adapter table); such a flag changes nothing. A different value, including one given where the session stores none, is a usage error that prints the stored and the given value, with a hint to drop the flag or use `run`. `--timeout` and `--cancel-after` behave as for `run`, counted from the call: a deadline that expires during the cold resume, before the new turn was observed, leaves stdout empty with `error.context.status` `starting` or `preparing`. A usage limit on the turn this call starts is handled as for `run`, `--limit-wait` included. A cold resume sends the session's stored system prompt and tool list again with `session/load` or `session/resume`, so a restored session runs under the same prompt as the first turn.
A session whose recorded working directory no longer exists cannot be continued: the call fails with `conflict` naming the directory, before anything is sent or any turn file rotates.

### `daemon`

Daemons are an automatic performance cache, one per concrete target consisting of an agent entry, vendor home, literal declared environment, named passthrough values, resolved permission policy and process-level spawn identity: the CLI effort when `effort_via = "cli"`, and the system prompt file path when `system_prompt_via = "codex_config"`.
Secret values affect the target hash but are never exposed.
A daemon starts on demand, keeps one adapter warm, serves up to `daemon_max_concurrent` turns at once, queues further turns, and expires after `daemon_ttl` of idleness.
The daemon and the adapter it keeps warm run in acpc's state directory, not in the directory of the call that started them. Each session's working directory reaches the adapter through ACP when the session is created or restored, so removing the directory a daemon was started from does not affect later calls.
A detached session keeps its daemon alive.
A version-skewed daemon stands down before the next mutating request; read-only status observes the running version instead.
A daemon socket path that exceeds the platform limit (108 bytes on Linux, 104 on macOS) even in its hashed form starts no daemon: the call reports `unavailable` naming the limit and a shorter `ACPC_HOME` as the remedy, and a blocking call takes the direct path as for any unavailable daemon.

```text
daemon status [AGENT] [--limit N] [--plain] [--format text|json|plain]
daemon stop [AGENT] [--force] [--dry-run] [--yes] [--format text|json]
```

`daemon status` reports bounded items with `target`, `version`, `pid`, `uptime_seconds`, `log`, `sessions`, `preparing`, `restoring`, `max_concurrent` and `idle_seconds`. Entries are ordered by target name, ascending, and the default window is the first 20 of that order. It connects only to the addressed daemon and does not restart it.

Named `daemon stop AGENT` stops the targeted daemon without a confirmation gate. Bare `daemon stop` resolves every daemon before confirmation; an empty target set succeeds unchanged without confirmation, while a non-empty set requires `--yes`. `--dry-run` lists the targets and requires confirmation exactly when that set is non-empty. Active `starting` or `running` sessions refuse the stop with `precondition_failed` unless `--force` is supplied. The active-session precondition is checked again after confirmation before the stop is sent. `--yes` confirms the action and `--force` overrides that precondition; neither substitutes for the other. A forced stop finalizes affected sessions as `failed`.

### `delete`

```text
delete SELECTOR [--yes] [--format text|json]
```

`delete` clears the selected session directory only after the session is finished and the caller has supplied `--yes`, then leaves a permanent tombstone marker in that directory. It does not prompt at a terminal. The session's transcript, prompt, answer and metadata are removed together. The identifier remains reserved forever, so repeating the call reports `not_found` and no later session can reuse it. The structured result contains `session_id`, `removed`, `changed` and `paths`. `delete` offers no `--dry-run`; `status <id>` is its preview, naming the state, paths and timestamps the deletion acts on.

### `install`

```text
install AGENT [--yes] [--format text|json]
```

`install` resolves the registry entry, then runs its trusted `install_command`. An entry without one is `not_supported`; an unknown entry is `not_found`. A terminal caller is asked for confirmation, and every other caller must pass `--yes`. The installer owns the effect, so success reports `changed: null` rather than guessing whether the vendor changed anything. An installer failure is exit 1 with the structured result or failure envelope appropriate to the selected format.

### `list`

```text
list [--limit N] [--plain] [--format text|json|plain]
```

`list` returns liveness-verified sessions, active first and then the most recent finished sessions, bounded by 20 by default. The window is the first N of this order: active sessions first, newest by creation time, then finished sessions, newest by finish time, falling back to creation time when a session has none; ties are broken by session id, descending in both groups. The collection has `items` and `has_more`. Each item contains `session_id`, `entry`, `model`, `status`, `name`, `prompt_snippet`, `runtime_seconds`, `idle_seconds`, `created_at`, `started_at` and `finished_at`. A daemon-owned continuation preparation appears as `preparing`. `--plain` emits one session id per line and requires an explicit `--limit`.

### `log`

```text
log SELECTOR [--since CURSOR] [--limit N | --tail N] [--prose]
    [--format text|ndjson] [--max-output BYTES]
    [--wait-new | --follow] [--timeout S] [--quiet]
```

`log` is a read-only record stream. On a one-shot session whose turn has ended, `log` fails with `conflict` naming `one_shot`, because the transcript is pending delivery or gone, and a `log` that is reading when the turn ends, `--follow` and `--wait-new` included, ends with the same `conflict`; `status` still serves it and `wait` delivers it once.
Its default non-follow window is the last 20 transcript events.
`--since` selects events after a global cursor.
`--limit N` then emits the first N records from that selected position in transcript order, while `--tail N` selects the last N matching records and emits them in transcript order.
`--limit` and `--tail` conflict.
With `--follow`, omitting both `--limit` and `--tail` starts after `--since`, or at the beginning of the transcript, without a default window.
`--since X --follow` starts after X without a default window.
`--tail N --follow` replays the last N records matching any `--since` selection in transcript order, then continues without a default window.
An explicit `--limit` ends observation after N emitted records, including with `--follow`.
`--prose` renders full agent messages with terminal control bytes escaped and their line breaks kept, retains error records, puts one blank line between messages, error records and turns, and is mutually exclusive with `--json` and `--format ndjson`. A message is delimited as in the answer: `usage` events between two message records do not split it, in `--prose` or in the condensed view's continuation marker.
`--format ndjson` emits one transcript record per line, as stored, and is the stream format selected by `--json`. The one exception is a `usage` record: the adapter's `cost` and `meta` stay on disk and are not published, every other stored field is, a record written before 1.0 has its `tokens` published as `used`, and `size` is `null` when the record has none.

`--wait-new` waits for activity.
`--follow` collects transcript-order events until the session ends.
They are mutually exclusive.
A wait deadline leaves the session unchanged and exits 124.
A follow stopped by `--max-output` exits 4.
The cursor in the stderr footer covers exactly what stdout emitted, so a caller can resume with `--since CURSOR`.
The footer, diagnostics and truncation note go to stderr.
A truncation never fabricates a transcript record.

### `probe`

```text
probe ENTRY --discover [--format text|json]
```

`probe` reads the mode catalog advertised by an adapter, opens and releases one ACP session, sends zero turns, and reports a two-sided diff against the entry's `[modes]` table. It never edits the registry. Measuring what modes permit is outside this release, so `--discover` is required. The JSON report contains `entry`, `base_adapter`, `discover_only`, `turns`, `current_mode`, `advertised_modes`, `mode_reports`, `verdicts`, `refusal_violations`, `implied_modes`, `unmeasured`, `current_modes` and `diff`.

### `prune`

```text
prune [--older-than D] [--dry-run] [--yes] [--format text|json]
```

`prune` clears only finished sessions whose age from `finished_at` exceeds the threshold and leaves a permanent identifier tombstone in each selected directory. Without `--older-than`, the session threshold is `config.toml`'s `retention`, default `90d`. A zero retention value requires an explicit `--older-than 0d`; active sessions are never candidates. Removing session data never releases its identifier. `prune` resolves its target set before the confirmation gate. When the set is empty it succeeds with `changed: false` and `requires_confirmation: false` without asking, because no protected effect remains; a failure to read the candidates still reports its own error rather than an empty success. When the set is not empty, the mutating call requires confirmation. After that gate, the full target set is locked by session id order and every session is read and verified again before any directory is cleared. A session that no longer qualifies reports `conflict`, and the call clears none of the set. `--dry-run` reports the same `targets`, `changed` and `requires_confirmation` without deleting and does not require `--yes`; `requires_confirmation` is `true` exactly when the target set is not empty. The result uses `targets`, `changed` and `requires_confirmation`, not a collection envelope.

### `resolve`

```text
resolve AGENT [--cwd DIR] [--model M] [--effort E] [--permissions P]
    [--mode MODE] [--home DIR] [--format text|json]
```

`resolve` previews the same call resolution that a later `run` would use. It creates no session, starts no daemon, and asks no question. The result contains `entry`, `base_adapter`, `command`, `cwd`, `env`, `env_passthrough` and `resolved`; each resolved field contains its value and provenance, with mode facts `grants`, `delegates`, `escalates` and `selectable` where applicable; `effort` also carries `levels`, the values the entry may send for the resolved model and the factors that bounded them (`scale`, `advertised`, `entry`, `shipped`), and its value is `null` with provenance `vendor default` when the entry sends none. `resolved` also carries `system_prompt` (the file path and its size in bytes) and `tools` when the entry sets them, with their provenance; and `data_residency`, the entry's declaration with its provenance; the adapter facts `system_prompt_via` and `tools_via` appear beside `base_adapter`. A policy that cannot be served by a declared mode is `permission_denied` with exit 2.

### `run`

```text
run AGENT [PROMPT | -] [--prompt-file FILE] [--system-prompt-file FILE] [--cwd DIR] [--model M]
    [--effort E] [--permissions P] [--mode MODE] [--home DIR]
    [--output-file FILE] [--transcript-file FILE] [--format text|json] [--timeout S]
    [--cancel-after S] [--limit-wait DURATION] [--name ALIAS]
    [--max-output BYTES] [--background | --bg] [--quiet]
```

`run` resolves an adapter or variant, creates a session, dispatches one turn and blocks by default. `--background` and its alias `--bg` dispatch and return the acceptance receipt without waiting for the answer: on a terminal the session id and its directory on two lines, on a non-terminal stdout the tagged document without an answer section (see *Text presentation*), and with `--json` the receipt document. The receipt is returned once the daemon has completed the adapter's `initialize` handshake, so it names the session's correction mode under `capabilities`; a warm daemon adds no delay, a cold one adds the adapter start. It does not wait for the prompt to be sent. The daemon bounds that handshake to 60 s: an adapter that has not answered `initialize` by then is torn down and the session ends as `failed` with `agent_error`, the way a crashed adapter does. Until the handshake completes, `status` and `list` show the accepted session as `preparing`. A blocking call prints an early session line to stderr before the turn starts so the caller can inspect or cancel it mid-run.

`--timeout` bounds only how long this client waits, counted from the call and covering acceptance by the daemon, the adapter handshake and the turn itself; absent, the wait is unbounded. It never cancels or changes accepted work. After the deadline the session remains alive under the daemon, or under a detached direct worker when the daemon fallback was used, and the command exits 124 with `kind: timeout`, an empty stdout and no result document, even when a partial answer has already been recorded. `context` carries `session_id`, `turn` and the observed `status`: `running` for an observed turn, `starting` or `preparing` when the deadline expired while a cold daemon was still starting the adapter. `retryable` is `false`, because repeating the call would start new work; `hint` points at `log --tail` for the progress so far and at `status`, and `next` is `acpc status <id>`. `--cancel-after` bounds the work itself. When it expires, ACP cancellation is sent and the observing command reports `operation_failed` with the observed `canceled` status and the partial answer on stdout.

A usage limit that blocks the turn is handled in the same turn. acpc recognizes it when the adapter fails `session/prompt` with a JSON-RPC error whose `data.errorKind` is `rate_limit`, or whose message is the vendor's usage-limit text, or when a preceding `usage_update` carried `_meta["_claude/rateLimit"]` with `status: rejected`; codex-acp marks a usage limit only with `data.codexErrorInfo` `usageLimitExceeded`, which it also sends when the plan has no access at all, so acpc treats it as a limit only when `data.message` is the usage-limit text. The expected return time comes from `resetsAt` when the adapter sent it, otherwise from the message's `resets <time> (<zone>)` clause or codex's `try again at <time>` clause, read in the local time zone and dated today when it names no date, otherwise it is unknown. A limit whose return time is known and no further away than the turn's limit wait cap moves the session to `waiting`: the turn stays open with the same number and files, `status` reports `waiting` with a `limit` object, and the daemon sends the prompt again to the same adapter session five seconds after the return time, the original prompt when the turn had recorded no assistant message or tool call yet, otherwise acpc's continuation instruction naming the usage limit as the cause. A second limit in the same turn waits again while the turn's total waiting stays within `limit_wait_max`. With an unknown return time, or beyond the cap, the turn ends `failed` with `stop_reason: rate_limit` and the same `limit` object; the answer recorded so far is kept. Waiting is never reported as a terminal outcome and repeating the prompt is the caller's decision, not acpc's.

`--timeout` is invalid with `--background`, because a background call does not wait. `--cancel-after` remains valid with `--background`. `--permissions` selects a ceiling and the adapter mode; absent, its value comes from the registry or the TTY rules below. Deprecated permission spellings remain accepted as aliases and are reported as such.

`--output-file` writes exactly what stdout would have received and leaves stdout empty, on success and on a failure that returns a result; a call that returns no result creates no file. It expands a leading `~`, creates a missing parent directory and overwrites the target; a target that exists and is not a regular file, such as `/dev/null` or a FIFO, is written in place rather than replaced. A target acpc cannot write fails with `permission_denied` naming the output path. The complete `answer.md` remains in the session directory. `--transcript-file` writes the session's transcript, the same NDJSON records `log --json` would emit, to the file, with the same path rules as `--output-file`, once the turn has ended; on a one-shot session it is the only way to keep the transcript, and the file is written before the pending files are removed. `run --background` rejects it with `invalid_input`, because the receipt returns before any transcript exists; the hint names `wait`. The result document then carries `transcript_file`. `--max-output` caps stdout bytes at 131 072 by default, preserves a UTF-8 boundary, sets `truncated: true` in a machine result and names the complete answer path. For `run`, `continue`, `steer` and `wait` it accepts `0`, which disables the cap, or at least 4 096 bytes; a smaller value is a usage error. The cap never cuts the result's envelope: when the envelope alone exceeds it, the answer is reduced to the truncation marker.

`--system-prompt-file FILE` replaces the system prompt for this call and overrides the entry's `system_prompt_file`. It follows the prompt-file rules: the path resolves against the caller's directory with a leading `~` expanded, the content is limited to 1 MiB, an unreadable file is `permission_denied`, a missing one a usage error. It needs an adapter whose `system_prompt_via` is `acp_meta`; on `codex_config` it is `not_supported`, exit 2, naming `system_prompt_file` on the entry as the route. The system prompt in force is written to the session directory as `system.md` before dispatch and named by `paths.system`.

### `skills`

```text
skills list [--limit N] [--plain] [--format text|json|plain]
skills get NAME [--format text|json]
```

`skills list` returns a bounded collection of bundled skills with `name`, `description` and `path`. Entries are ordered by name, ascending, and the default window is the first 100 of that order. `--plain` prints one name per line and requires an explicit limit. The text rendering cuts a description to the terminal width and says so once in a footer naming `skills get`. `skills get` prints the skill body in text mode and writes its source directory to stderr; its machine result contains `name`, `description`, `path` and `body`. Bundled skills are read-only package data.

### `status`

```text
status SELECTOR [--format text|json]
```

`status` reports one liveness-verified session without reading the transcript. Its detail result contains `session_id`, `status`, `pid`, `turns`, `entry`, `base_adapter`, `model`, `served_model`, `name`, `runtime_seconds`, `idle_seconds`, `context`, `usage`, `exit_code`, `stop_reason`, `failure`, `capabilities`, `limit`, `permissions`, `pending_corrections`, `paths`, `retained`, `created_at`, `started_at` and `finished_at`. `capabilities` holds `steer_mode` and `continue_without_message` as described under `steer`. `retained` is `false` for a one-shot session whose turn has ended and `true` otherwise; `paths` then still names the files that were removed or are pending delivery. `limit` is `null` unless a usage limit touched the current turn; otherwise it holds `reason` (`rate_limit`), `resume_at` (RFC 3339 or `null` when unknown), `auto_continue` (`true` while the session is `waiting`, `false` once the turn ended) and `source` (`error_kind`, `rate_limit_info` or `text`, where the return time and reason were observed). `served_model` is the model the adapter last named in the current turn, exactly as the adapter reported it (claude-agent-acp's `_claude/model` on its usage updates, codex-acp's `model_usage` rows on the prompt response), recorded when the turn ends; it is `null` while the turn runs and when the adapter named none. When the requested model names a version (its identifier contains a digit) and the served identifier is neither it nor an extension of it, compared after a trailing `[...]` suffix is dropped from both, the command that first reports the turn's end prints one stderr note naming both; `--quiet` suppresses it. `failure` is `null` unless the turn ended `failed` for a cause acpc observed; then it is an object with `class` (one of the classes listed under *Failures*), `message` (the one-line observation the turn's `error` event carries) and `next_step` (one sentence naming the remedy, or `null`). A turn ended by the entry's permission policy (`stop_reason: permission_denied`) records no failure. `context` is the session's context occupancy as last reported by the adapter, refreshed at most every 2 s while a turn runs: an object with `used` (tokens in the context at the last report), `size` (the context window, `null` when the adapter did not report it) and `peak` (the largest `used` observed over the session, earlier turns included); it is `null` as a whole until the adapter has reported usage for the session. It measures what the context holds, not what the session consumed. `usage` is what the session consumed as far as the adapter reported it, cumulative over all turns, or `null` when the entry declares no usage profile or nothing has been reported yet. It holds `models`, keyed by the model identifier exactly as the adapter reported it, each with `total_tokens`, `input_tokens` (uncached input), `cache_read_tokens`, `cache_write_tokens` and `output_tokens`; `calls`, the number of model calls when the adapter reports them; `compactions` with `count`, `unaccounted` (compactions whose consumption is not included in `models`), `context_before` and `context_after` (the context occupancy just before and just after each compaction, summed); `gaps`, the number of turns whose consumption the adapter never reported; `quality`, `exact` only when every turn and every compaction is covered by adapter-reported figures split by token class and no drift was detected, otherwise `estimate`; `source`, naming the adapter, its version and the field read; and `billing` as declared by the entry (`subscription`, `api` or `null`). `drift` is `null` unless a turn's reports broke an invariant of the entry's usage profile, such as a codex response total that matches the turn's sum rather than its last call; it then records the check, the declared and observed semantics, the adapter and version and the turn, `quality` stays `estimate` for the rest of the session, one note on stderr names the mismatch, and the figures are still derived as the profile declares. A figure acpc did not observe is `null`, never `0`. `usage` is the input for a caller's own price list; acpc prices nothing. acpc reports no cost anywhere: the adapter's own cost figure, when it sends one, stays in the transcript's `usage` events and is never rendered. `permissions` shows the policy that applies to the session's work: `policy` (the effective permission policy), `mode` (the adapter mode serving it), `source` (where the policy came from: the flag, the registry, acpc's non-interactive default or the terminal question) and `clamp` (the requested policy, the ceiling and the effective policy when a clamp occurred, otherwise `null`). This is the non-interactive inspection of the policy for accepted work; `resolve` previews it before a `run`. `pending_corrections` is always `null`, as described under `steer`. Use `list` for the collection view. `status` follows the selector: each call reports the session's current turn at the time of the read, so a session that rotated to a newer turn is reported as that newer turn.

### `steer`

```text
steer SELECTOR [INSTRUCTION | -] [--prompt-file FILE]
    [--steer-mode in-place|cancel-then-start]
    [--output-file FILE] [--format text|json] [--background]
    [--timeout S] [--cancel-after S]
    [--max-output BYTES] [--quiet]
```

`steer` delivers a correction to the session's active turn in one of two modes. `--steer-mode` selects the mode; without it acpc uses the session's default mode, published as `capabilities.steer_mode`: `in-place` when the session supports it, `cancel-then-start` otherwise. The capability object appears in `status` and in every result and receipt of `run`, `continue` and `steer`. It describes the session, not the turn: a finished session still reports the mode its next correction would use. The object also carries `continue_without_message`, `true` for every acpc session (see `continue`). `cancel-then-start` is always supported, so an explicit `--steer-mode cancel-then-start` never fails for lack of support. An explicit `--steer-mode in-place` on a session whose mode is `cancel-then-start` fails with `not_supported` before any effect; acpc never falls back to the other mode.

`in-place` adds the instruction to the active turn through the adapter's `_session/steering` extension, at the next point the adapter supports, usually after the running tool call ends. The turn keeps its number, its prompt file and its answer file; nothing rotates and no preamble is added. Support is read from the adapter's `initialize` metadata (`_meta.steering.supported`) and recorded on the session as soon as the daemon has completed that handshake, before any receipt for the session is returned; every later turn served by the same daemon reads it again. A session served by a direct child records `cancel-then-start`, because no channel reaches its process. A session that reaches no adapter at all, because the start failed before the handshake, has no active turn to correct and reports `cancel-then-start`. acpc always asks the adapter not to start a turn of its own (`idleBehavior: promptRequired`); an adapter that starts one anyway is reported as such, never presented as in-place. Several in-place corrections queue in the adapter in the order acpc sent them.

`cancel-then-start` cancels the turn in flight, waits for its end and starts the next turn on the same session with the instruction under the fixed interruption preamble. During daemon-owned preparation there is no prompt to interrupt; acpc cancels preparation, reports that fact and sends the instruction plainly. `--cancel-after` bounds the new turn's work and is accepted only with `--steer-mode cancel-then-start`. A usage limit on the new turn of `cancel-then-start` is handled as for `run` under the entry's or the global cap; `steer` takes no `--limit-wait`. An `in-place` correction of a session in `waiting` fails with `conflict`, because nothing is in flight to correct; `cancel-then-start` cancels the wait, which stops the scheduled resumption, and starts the new turn.

The cancellation step selects the turn active when `steer` started and waits up to 10 s for its end. If that turn is still running at the deadline, `steer` fails with `timeout`, `target_status: running` and `message_state: not_delivered`; the cancellation stays requested and no instruction is sent. If the turn ended on its own before the cancellation took effect, `steer` reports that terminal state as `target_status` and sends the instruction plainly, without the interruption preamble. If another call started a new turn before the instruction could be sent, `steer` fails with `conflict` and leaves that turn untouched. Every failure of the cancellation step carries `session_id`, `capabilities` and `correction_result` in `context`, like a failure after it.

A finished session is a `conflict` naming `continue` as the follow-up operation in either mode. A `starting` or `preparing` session is a `conflict` for `in-place`, because no prompt is in flight yet.

Every `steer` result carries `turn`, the turn to observe next, `capabilities`, and `correction_result` with `steer_mode`, `target_turn`, `target_status` and `message_state`. `target_turn` is the turn the correction selected; for `in-place` it equals `turn`, for `cancel-then-start` `turn` is the new turn. `target_status` is the last state observed for the selected turn. `message_state` is `accepted` when the adapter acknowledged the instruction (`injected`) or acpc accepted the new turn, `not_delivered` when the instruction is known not to have reached the selected turn, and `unknown` when acpc cannot tell. acpc never reports `delivered`: an acknowledgement does not show that the model used the instruction. Every in-place correction is appended to the transcript as a `steer` event carrying `mode`, `text` and the adapter's `outcome`; a cancel-then-start correction is recorded by the new turn's prompt file and its state events.

Without `--background`, `steer` blocks until the observed turn ends and prints its answer: the corrected turn's answer for `in-place`, the new turn's for `cancel-then-start`. With `--background` it returns after acceptance. `--timeout` only bounds this client's wait; absent, the wait is unbounded. On expiry `steer` exits 124 with `kind: timeout` and no result document, with the same `retryable: false`, `hint` and `next` as `run`, plus `capabilities` and `correction_result` in the error context. Ctrl-C during an `in-place` steer behaves like `wait`: exit 130 and the turn keeps running. Ctrl-C during `cancel-then-start` cancels the turn it started.

A failure after the target was selected carries `session_id` and `correction_result` in `context`:

| What acpc knows | `kind` | `message_state` |
| --- | --- | --- |
| The adapter answered `promptRequired`: the turn ended before the instruction arrived. | `conflict` | `not_delivered` |
| The adapter answered `startedNewTurn`: it started a turn acpc does not own. acpc sends `session/cancel` for it and reports the outcome as unknown. | `outcome_unknown` | `unknown` |
| The adapter rejected `_session/steering` despite declaring it. | `not_supported` | `not_delivered` |
| The daemon serving the session could not be reached; nothing was sent. | `unavailable` | `not_delivered` |
| The request was sent and no reply arrived within 10 s, or the connection was lost. | `outcome_unknown` | `unknown` |
| The observed turn ended `failed` or `canceled` while `steer` was blocking after acceptance. | `operation_failed` | `accepted` |
| The cancellation deadline passed with the selected turn still running (`cancel-then-start`); exit 1. | `timeout` | `not_delivered` |
| The cancellation's effect could not be observed, or the session's process could not be signaled (`cancel-then-start`). | `outcome_unknown` or `unavailable` | `not_delivered` |
| Another call started a new turn before the corrected turn could start (`cancel-then-start`). | `conflict` | `not_delivered` |

A `not_delivered` or `unknown` outcome never triggers an automatic `cancel-then-start`; repeating the instruction is the caller's decision.

**Pending input.** An in-place correction is forwarded to the adapter as soon as `steer` is called and acknowledged with `message_state: accepted`; the adapter delivers it before the model's next step in the same turn and acpc imposes no bound of its own on how many corrections it forwards. Whether a forwarded correction is still pending inside the adapter is not observable to acpc, so `status` reports `pending_corrections: null` rather than a count, always. A correction the adapter could not deliver because the turn ended is reported as `not_delivered`; a correction that made the adapter start a turn acpc does not own is canceled and reported as `outcome_unknown`, so an instruction never silently becomes input to a later turn. acpc offers no later per-instruction receipt: the transcript's `steer` event keeps the adapter's acknowledgement, and delivery to the model is never established by a successful turn.

### `wait`

```text
wait SELECTOR [--timeout S] [--output-file FILE] [--transcript-file FILE]
    [--format text|json] [--max-output BYTES] [--quiet]
```

`wait` selects the session's current turn when the call starts and observes that turn until it ends. It keeps that selection even when a later `continue` or `steer` opens a newer turn meanwhile, and its result names the observed turn in `turn`. A turn that has already finished is returned immediately, and its answer is returned on stdout whatever the terminal state was: exit 0 for `succeeded`, otherwise `operation_failed` with the session id and the observed status. On a one-shot session this is the one delivery: the answer, partial or complete, and the transcript go out once, and a second `wait` fails with `conflict` naming `one_shot`. A `--timeout` that expires delivers nothing. `--timeout` stops waiting only, leaves the session unchanged and exits 124 with `kind: timeout` and no result document, even when a partial answer has been recorded; absent, the wait is unbounded. The error's `context` carries `session_id`, `turn` and the observed `status`, `retryable: true` with `retry_after_ms`, a `hint` pointing at `log --tail` and `status`, and `next` is `acpc status <id>`. `log <id> --since <cursor>` reads the progress a deadline interrupted without repeating it. A session in `waiting` is still active: `wait` keeps waiting through the limit and a deadline reports `timeout` with `error.context.status: waiting`. If the status cannot be observed, the result is `outcome_unknown` with `status: null` and no result document.

### `acpc schema`

`schema` is the introspection interface. Bare `acpc schema` emits an index containing `schema_version`, `tool_version`, `global_flags`, `format_defaults`, `exit_codes`, `conformance` and sorted command entries. `acpc schema PATH` emits `name`, `description`, `args`, `flags`, `effects`, `confirm`, `interactive` and `output`, plus `output_description` for a command that returns results on failure or has success-only fields; `log` also has `stream: true`, and commands whose format differs from the index include `format_defaults`.

The output field is a JSON Schema subset using only `type`, `enum`, `properties`, `required` and `items`. It describes one result document shared by the success and failure results, or one record for `log`. The generator walks the Click tree that actually parses the command. A group is indexed only when explicitly marked as dispatching useful work without a subcommand. An unknown schema path is an exit-2 usage error naming the nearest valid paths. Path segments are separate arguments.

The installed binary currently publishes schema version `1`, tool version `1.1.1`, format defaults `{"tty": "text", "non_tty": "json"}`, and conformance name `cli-design-standard` at `0.2.0-draft.11` with extensions `["managed", "conversational"]`; `conversational` covers `run`, `continue`, `steer`, `wait` and the capability object. `tests/test_conformance.py` verifies the claim against the standard header and the behavior of every indexed command.

## Output contract

### Formats and streams

The output format is selected by `--format` or the `--json` alias. The tool-wide default is text on a TTY and JSON on a non-TTY. `run`, `continue`, `steer`, `wait`, `log`, `resolve` and `skills get` explicitly default to text in both contexts. `log` uses `ndjson` as its machine format. Collections additionally offer `plain`, also selected by `--plain`; it requires an explicit `--limit` and emits one identifier per line.

For `run`, `continue`, `steer` and `wait` the `text` format has two presentations selected by the stdout stream: the tagged document described under *Text presentation* when stdout is not a terminal, and the human layout when it is. `--output-file` receives the presentation stdout would have received. `--json` is a choice for programmatic parsing, not a prerequisite for reading an answer; it returns the original answer string and the complete document.

The global `--color auto|always|never` policy affects human text only. The precedence is the explicit flag, `NO_COLOR`, `TERM=dumb`, then whether stdout is a terminal. Machine formats never contain ANSI or control bytes.

stdout carries only the selected result: the answer, a machine document, a record stream, or the acceptance receipt from `--background`.
`--output-file` leaves stdout empty and writes the exact payload of the selected format to the file, on success and on a failure that returns a result. A call that returns no result creates no file.
stderr carries acpc metadata, summaries, footers, diagnostics and adapter noise according to the command.
A log footer never contaminates a prose or NDJSON stdout stream.

### Success documents

Machine success documents are the shapes published by `acpc schema`. Collections use exactly `{"items": [...], "has_more": boolean}`. This applies to `agents list`, `agents check`, `skills list`, `list` and `daemon status`.

`agents create` returns `name`, `extends`, `path` and `changed`. `agents delete` returns `name`, `path` and `changed`. `agents get` publishes the union of its detail, advertised, models and commands views and always requires `agent`. `cancel` returns `session_id`, `status`, `stop_reason` and `changed`. `install` returns `agent`, `ok`, `returncode` and `changed`, where `changed` may be null.

`prune` and `daemon stop` return `targets`, `changed` and `requires_confirmation`; the same shape covers preview and mutation. `delete` returns `session_id`, `removed`, `changed` and `paths`. `resolve` returns its full resolution document. `probe` returns its discovery report. `log` returns one record at a time with required `i`, `ts` and `type`, plus event-specific fields.

The shared answer result for `run` and `continue` has required `status`, `session_id`, `turn`, `created_at`, `started_at`, `finished_at`, `paths`, `retained`, `truncated`, `partial`, `denied`, `permissions_clamp`, `capabilities` and `changed`. Every foreground document adds `served_model` and `failure`, the objects `status` shows; foreground success adds `stop_reason`, `context`, `usage` and `answer`; a background receipt omits all of them and points `next` at `wait`. `context` is the context occupancy recorded for the session so far, the same object `status` shows; it is `null` until the adapter has reported usage for the session, never an object of zeros in that case, so a turn that ended before any usage arrived reports `null`. `usage` is the session's cumulative consumption, the same object `status` shows. `resume`, `next`, `output_file`, `transcript_file` and `limit` are optional; `limit` appears when a usage limit touched the turn and has the shape described under `status`; `next` is omitted for a one-shot session, which no command can continue, and the stderr summary then carries no `Next:` segment. `steer` uses the same document with `status` limited to `running` and `succeeded`, without `partial`, plus required `correction_result`. `capabilities` is the session-capability object described under `steer`, `{"steer_mode": "in-place" | "cancel-then-start", "continue_without_message": true}`, and is the same object in every command that carries it. `wait` is read-only, so its result has no `changed` field. `turn` is the one-based number of the turn the document describes. Each schema entry declares one `output` shared by the success and failure results of that command, and `run`, `continue` and `wait` state in `output_description` which failures return a result and which fields are required only on success. Every command with a conversation role names its identifier field `session_id`, its answer field `answer` and its capability object `capabilities` in `output_description`.

`paths` contains `dir`, `prompt`, `transcript` and `answer`, and `system` when the session has a system prompt. `denied` records permission denials by category, and `permissions_clamp` records a requested policy, the entry ceiling and the effective policy when a clamp occurred. `run`, `continue` and `wait` return their result document on stdout when acpc observed the end of the turn and holds its content, including when the turn failed or was canceled. A client deadline never produces a result document: the answer recorded so far stays in the session files and `log` reads it. `partial` is `false` when the content is the complete answer for that call, a refusal included, and `true` when the turn ended before the answer did; a result carrying `partial: true` always exits non-zero. A call that never observed a turn — an unknown agent, a rejected flag combination, a session that does not exist, a start that failed — writes nothing to stdout. The structured error stays on stderr in every case.

### Text presentation

On a non-terminal stdout, `run`, `continue`, `steer` and `wait` print their result as one tagged document that acpc builds, never the agent:

```text
<result session_id="q7x2" status="succeeded" partial="false">
<metadata>
{"turn":1,"capabilities":{"steer_mode":"in-place","continue_without_message":true},"context":{"used":1834,"size":200000,"peak":1834},"stop_reason":"end_turn","next":["acpc","continue","q7x2"]}
</metadata>
<answer>
The answer, verbatim Markdown.
</answer>
</result>
```

The opening tag carries `session_id`, `status` and, for documents that have it, `partial`, with `&`, `<`, `>`, `"`, tab, CR and LF escaped as `&amp;`, `&lt;`, `&gt;`, `&quot;`, `&#9;`, `&#13;` and `&#10;`. `<metadata>` holds one JSON object on a single line with the fields the text keeps from the JSON document: `turn`, `capabilities`, `next`, `stop_reason`, `context` and `served_model` when known (omitted while `null`), `failure` when the turn recorded one, `correction_result` for `steer`, `denied` when any denial was recorded, `permissions_clamp` and `resume` when present, `limit` when a usage limit touched the turn, and `truncated` with `output_file` when the answer was cut by `--max-output`. Timestamps, `paths`, `changed` and the untruncated answer stay in JSON. `<metadata>` always precedes the answer section. The answer section holds the answer text with its line breaks and Markdown as they are; terminal control bytes are escaped as in every text output and nothing else is replaced. Each tag sits on its own line: after an opening tag acpc writes one newline, then the content, then one newline, then the closing tag, so an answer that ends with a newline shows an empty line before its closing tag and an empty answer string is a section with one empty line. The answer tags are `<answer>` and `</answer>` unless the displayed answer contains one of the exact strings `<result>`, `</result>`, `<metadata>`, `</metadata>`, `<answer>` or `</answer>`; then acpc writes `<answer-N>` and `</answer-N>`, where N is one plus the number of LF characters in the displayed answer (a CR before an LF is answer text and is not counted), and the answer is exactly the N lines after the opening tag, validated by the matching closing tag on the line after them. A line inside those N lines that looks like a wrapper tag remains answer text. A background receipt is the same document without an answer section, with `paths` added to the metadata so the session directory stays visible. `--max-output` bounds the whole document; a truncated answer ends with the usual marker and the metadata names `output_file`. The document is a readable presentation, not strict XML: a caller that acts on `next` or other metadata programmatically uses `--json`, which returns the original answer string. The tags do not make an agent's answer trusted instructions.

Example of the counted form, an answer of five lines whose code sample contains the wrapper's closing tag:

````text
<result session_id="q7x2" status="succeeded" partial="false">
<answer-5>
Close each section explicitly:

```text
</answer>
```
</answer-5>
</result>
````

On a terminal, the same fields are laid out for a person: the answer on stdout with terminal control bytes escaped exactly as in the tagged document, and the metadata on stderr, in the `--` summary line, which also names the steer mode, a partial answer, a usage limit and a `Next:` command. Reading the answer never reruns the agent.

### Failures

Every classified failure is one JSON document on stderr with exactly one top-level key, `error`. The nested object always has `kind` and `message`; optional fields are `retryable`, `action`, `hint` and `context`. The document is the last non-empty stderr line, including when stdout is machine-readable, stderr is not a terminal, or `--quiet` suppresses normal metadata. It never goes to stdout. In a human terminal without a machine format, acpc prints a one-line diagnostic and an optional recovery hint instead.

The produced kinds are:

| Kind | Meaning |
| --- | --- |
| `invalid_input` | The command, flag combination, value or prompt source is malformed. |
| `not_found` | A named session, alias, agent or skill does not exist. |
| `conflict` | Existing session state, a lock or a name binding rejects the operation. |
| `permission_denied` | An entry policy or filesystem refused access. |
| `timeout` | The observing client deadline expired without changing accepted work. |
| `unavailable` | An adapter, installer, daemon, write or other dependency could not serve the request. |
| `outcome_unknown` | Observation stopped without a reliable terminal outcome. |
| `interrupted` | This client was interrupted, for example by Ctrl-C. |
| `precondition_failed` | A documented condition that `--force` can override was not met. |
| `operation_failed` | Work ended in a state other than the success the observing command expected. |
| `confirmation_required` | A gated call lacked the required confirmation; exit code 1. |
| `agent_error` | An external program reported an error that acpc cannot classify more narrowly. |
| `corrupt_state` | State acpc owns exists but cannot be trusted. |
| `not_supported` | The target exists but acpc does not offer the requested operation for it. |

A `timeout` error of `run`, `continue`, `steer` or `wait` carries `hint` with the two commands that show what happened meanwhile, `acpc log <id> --tail 20` and `acpc status <id>`, and `next` with `acpc status <id>` as executable arguments. `retryable` is `true` only for `wait`, whose repetition observes the same turn again; it is `false` for `run`, `continue` and `steer`, whose repetition would start new work. In a human terminal the same recovery command is rendered as a `Next:` line.

A turn that ends `failed` for a cause acpc observed records it as the `failure` object of the result document and of `status`. `class` is one of:

| Class | Meaning | `next_step` |
| --- | --- | --- |
| `auth` | The adapter refused the session or the prompt for lack of a valid login. | the vendor's login command |
| `rate_limit` | A recognized usage limit ended the turn and acpc did not wait (`stop_reason: rate_limit`, `limit` set). | retry after `resume_at`, or raise the cap; retry later when the time is unknown |
| `adapter_limit` | The adapter stopped at a limit of its own (`max_tokens`, `max_turn_requests`). | split the task, or raise the adapter's own limit |
| `connection` | The adapter process died, closed its pipe, or never completed the `initialize` handshake. | the daemon log, or a direct re-run whose adapter stderr comes back |
| `adapter_error` | The adapter reported an error acpc classifies no further: a refusal, a rejected model, mode or effort, another JSON-RPC error, a limit-like refusal acpc does not recognize as a usage limit. | the daemon log, or a direct re-run |
| `acpc` | acpc ended or refused the turn: a daemon stop, an adapter that is not installed, a resume that could not be prepared. | `continue` the session when it was only interrupted; otherwise the message says what to fix |

`stop_reason: rate_limit` marks a turn that ended because a usage limit blocked it and acpc did not wait; `wait` reports a failed turn as `operation_failed` with `error.context.status: failed`, and the result document on stdout carries the object. A turn ended by `--cancel-after` or `cancel` is `canceled`, not `failed`, and records no failure; a turn whose host process disappeared is `unknown` and records none; a turn ended by the permission policy records none. Metadata written before 1.2 holds only the message: such a record reads with `class` derived from `stop_reason` (`rate_limit`; `adapter_limit` for the adapter's own limits; otherwise `adapter_error`) and `next_step` `null`.

A failure of `status`, `wait`, `cancel`, `steer`, `continue`, `log` or `delete` carries the session identifier in `error.context.session_id` whenever the caller supplied one or acpc resolved one. For a selector that matches no session, `context` holds the selector as supplied under `session_id` and `status: null`, because no state was observed. A selector that is an alias or `last` is reported as resolved when resolution succeeded and as supplied when it did not.

`confirmation_required` is produced when a gated call reaches the confirmation step without the required consent.
Target validation and documented preconditions happen before the confirmation question.
`unauthenticated` and `cursor_unavailable` are reserved kinds, not currently produced.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success, including an empty result or a data record with `ok: false`. |
| 1 | Generic failure or a command that could not complete the requested operation. |
| 2 | Usage error, including malformed flags and a policy no declared mode satisfies. |
| 4 | `log --follow` reached its output budget before the session ended. |
| 124 | An observing deadline expired and the observed work was left unchanged. |
| 130 | The client was canceled by SIGINT or `cancel`; answer commands mirror a canceled session. |
| 141 | SIGPIPE because a downstream reader closed the pipe. |
| 143 | SIGTERM detached from daemon-owned work or ended a direct turn. |

A `cancel-then-start` correction whose cancellation deadline passes exits 1, not 124: the deadline belonged to a request that changed the work, not to an observation. Missing named resources and session conflicts are exit 1, because the command was spelled correctly. Exit 2 is reserved for a call that cannot be accepted in the form given, including an unsupported permission policy for the selected entry.

Ctrl-C always produces exit 130 with `kind: interrupted` for the command that was interrupted.
Interrupting `wait` or `log` never changes the observed session.
Interrupting `run`, `continue` or a `cancel-then-start` `steer` cancels the owned turn; an `in-place` `steer` owns no turn and leaves it running.
A SIGTERM from a harness detaches a session already taken over by the daemon and ends a direct-worker turn.
The client reports the id and the commands that can wait or cancel a detached session.

## TTY vs non-TTY

acpc classifies standard streams separately. stdin decides whether a question can be answered, stdout decides the selected output format and whether the default policy may ask, and stderr decides decoration and whether a failure uses the structured envelope.

The interactive context is all of the following: stdin is a terminal, the selected output is human-readable rather than JSON, and `NO_INPUT` is empty or unset. acpc never asks outside that context. The default `ask` permission policy is narrower: stdout must also be a terminal. Redirecting stdout therefore changes an omitted permission default to `read`, but an explicit `--permissions ask` remains allowed when stdin can answer and the selected format is human-readable.

`NO_INPUT` is a non-empty environment value that forces the non-interactive context. An empty value does not. This covers permission prompts, the install confirmation and the policy question for a terminal `run --background` without an explicit policy.

| Invocation | Omitted permission default | Explicit `--permissions ask` |
| --- | --- | --- |
| stdin and stdout are terminals | `ask` | asks on the terminal |
| stdout redirected, stdin terminal | `read` | asks on the terminal |
| machine output selected | `read` | rejected |
| stdin is a pipe, heredoc or `-` prompt | `read` | rejected |
| `NO_INPUT` non-empty | `read` | rejected |
| terminal `run --background` without a policy | asks once which detached policy to use | rejected |

The background policy question offers `none`, `read`, `edit`, `execute` and `all`, defaulting to `read`; `ask` is not offered because no later caller is attached to answer. Silence or an invalid answer is a refusal. If no terminal can be opened, acpc takes the non-interactive `read` default instead of mistaking an unavailable question for consent. An explicit policy is recorded as `answered`, while an omitted non-interactive policy is recorded as acpc's `default`.

`install` asks `Install AGENT? [Y/n]` at a terminal and requires `--yes` elsewhere. End of input is never consent. Windows has no `/dev/tty`, so it follows the non-interactive branch until console input support is added.

The `last` selector is a convenience for the person at the keyboard and is rejected outside the interactive context. Use a session id or `--name` in scripts and agent calls.

## State on disk

`ACPC_HOME` is the acpc state root; it defaults to `~/.acpc` and is the only environment variable that selects that root. It is distinct from `--home`, which selects the vendor configuration directory passed to the adapter.

```text
ACPC_HOME/
  config.toml
  agents/<name>.toml
  cache/<agent>/
  daemon/<target>.log
  sessions/<id>/
    meta.json
    prompt.md
    prompt.<n>.md
    system.md
    transcript.ndjson
    answer.md
    answer.<n>.md
    meta.<n>.json
```

The complete global config is:

```toml
retention = "90d"
daemon_ttl = "30m"
daemon_max_concurrent = 8
limit_wait_max = "8h"
```

`limit_wait_max` caps how long one turn may wait for usage limits in total; a duration with the `--timeout` syntax, and `"0s"` disables waiting so that every recognized limit ends the turn `failed` at once. An entry's `limit_wait_max` overrides it for that entry's sessions, and `--limit-wait` overrides both for one turn. Unknown config keys are hard errors. Relative paths in `--cwd`, `--prompt-file` and `--output-file`, and in `--home` on `run`, `resolve` and `continue`, resolve against the caller's working directory; a leading `~` is expanded.
The resolved working directory, from `--cwd` or the caller's directory, must exist; otherwise the call fails with `invalid_input` before it creates anything. Directories are mode 0700 and files are mode 0600 where the platform supports those permissions. Metadata and cache writes are atomic, and transcript appends are whole lines.

`meta.json` uses `status`, not a second state field, and stores the resolved invocation, timestamps, turn count, context occupancy (`context`, the object `status` reports), consumption (`usage`, likewise), exit code, stop reason, failure observation with its class and next step, served model, prompt snippet, adapter session id, target and the steer mode. Metadata written before 1.0 carried `tokens` and `cost` instead; on read, `tokens` becomes `context` with `used` and `peak` equal to it and `size` `null`, and `cost` is dropped. Metadata written before 1.1 has no `usage` and reads as `null`. The adapter session id is recorded as soon as the adapter has accepted the session, before the prompt is sent, so a host process lost mid-turn leaves a session that can still be continued. Timestamps are RFC 3339 with a consistent microsecond precision. A per-session lock serializes turns, cleanup and metadata transitions.

`transcript.ndjson` starts with `{"schema": "acpc.transcript/2"}`. Event records use a global one-based cursor `i`, an RFC 3339 timestamp and one of `msg`, `thought`, `tool`, `permission`, `error`, `state`, `usage`, `steer` or `limit`. A `limit` event records `reason`, `resume_at`, `action` (`wait` or `fail`), `source` and the adapter's text; the surrounding `state` events record `running → waiting` and `waiting → running`. A `usage` event records `used` and `size` as observed at that point, the adapter's own `cost` figure when it sent one, and the notification's `_meta` object under `meta` when present; a figure the adapter did not report is `null`, never `0`. When `session/prompt` returns a response, acpc appends one `usage` event for it that carries the last observed `used` and `size` and, under `meta`, the response's usage report as sent (`prompt_usage` with the ACP `usage` object and the response `_meta`), the adapter's name and version from `initialize` under `adapter`, and `scope: "turn"`. Transcripts written before 1.0 record `tokens` in place of `used` and are read as such. `log` publishes a usage event as context occupancy only, in every format: the condensed view shows `used` and `size` and skips a usage event whose `used` and `size` repeat the previous usage event of the same turn, and the NDJSON stream carries the record without `cost` and `meta`. Unknown fields are preserved. A damaged or unsupported transcript is `corrupt_state`; an older transcript format is not silently upgraded and must be replaced by deleting the incompatible session state.

Turn files rotate at the start of the next turn. The previous prompt and answer receive their fixed turn suffix once, the finished turn's metadata is parked as `meta.<n>.json` with the same suffix, then the new prompt is written. The parked metadata is what `wait` reports for a turn that ended before the session rotated; that document's `paths.prompt`, `paths.answer`, `output_file` and the truncation marker name the parked files of that turn (`prompt.<n>.md`, `answer.<n>.md`), never the files of the turn the session moved on to. `answer.md` exists for every finished state. The answer is the turn's agent message text in arrival order. Where the agent's narrative forks, at a tool call, a thought, a permission request, a plan, an update kind acpc does not recognize, an in-place steering instruction or a usage-limit wait, consecutive messages are separated by one blank line. Updates that only report state, such as context usage, available commands, configuration options, the current mode or session information, are not boundaries: text on both sides of one is a single message. A failed or canceled turn keeps its partial prose; when no prose exists, the recorded failure explains the file. If the host process disappears before the result is observed, acpc writes a placeholder that says the outcome is unknown. A one-shot session whose turn ended holds `meta.json`, `lock` and, until the first delivery, `answer.md` and `transcript.ndjson`; after it only `meta.json` and `lock`. `delete` and `prune` treat it like any finished session.

### Session states

The public vocabulary is exactly:

```text
starting, running, preparing, waiting, succeeded, failed, canceled, unknown
```

`starting`, `running`, `preparing` and `waiting` are active; `waiting` is a turn holding for a usage limit that acpc will resume by itself. `preparing` is a daemon-only continuation phase before the new prompt is sent. `succeeded`, `failed`, `canceled` and `unknown` are finished for session management. `unknown` is therefore terminal for `wait`, `continue`, `delete` and `prune`, but it is not a claim about how the adapter operation ended: it records that acpc observed the host process disappear without observing a result. `wait` reports it as `operation_failed` with `error.context.status: "unknown"`.

Liveness is checked whenever a command reports or gates on a session. A dead process changes an active session to `unknown`, sets `stop_reason` to `unknown`, uses exit code 1 for the session record, persists the transition and writes the placeholder. A 30-second startup grace applies when no process id has been recorded yet.

Historical metadata is normalized when read. The former terminal deadline state is read as `failed`, with canonical `stop_reason: "error"` and `exit_code: 1`; client wait deadlines use `kind: timeout` and exit 124 but never become a session state. Historical state spellings are normalized to the public vocabulary before output and subsequent writes. A canceled turn records `stop_reason: canceled` whichever side canceled it, or `canceled during preparation` when the cancel landed before any prompt was sent; the adapter's `cancelled` stop reason is normalized when the turn ends and historical spellings of both reasons are normalized on read.

`cancel` accepts active sessions and is a no-op on finished sessions. `continue` accepts finished sessions, including `unknown` on a session's first turn, and refuses active sessions. `delete` and `prune` remove only finished sessions. `status`, `list`, `log` and `wait` can observe any existing session.

Retention is measured from `finished_at`.
Auto-prune runs opportunistically after a run according to `retention`.
Explicit delete always needs confirmation, while prune uses `--dry-run` to preview and `--yes` for the mutation. A prune that resolves an empty target set needs neither.
Once allocated, a session id is never assigned to another session. The allocator reserves ids by creating their directories; delete and every prune path release session data but never the identifier reservation. If the finite id space is exhausted, allocation fails with `unavailable` and does not suggest that pruning will free an id.

## Agent variants

An adapter TOML shipped in the package or defined locally supplies `command`, optional `install_command` and `install_docs`, default `home`, `home_env`, modes, presets, effort tables, environment declarations, pass-through variable names and the model/effort application paths. `usage_profile` names how acpc reads the adapter's consumption reports (`claude_model_usage`, `codex_usage_updates`, `grok_meta_usage`, or `none`, the default); `billing` declares whether the entry's sessions are paid by subscription (`subscription`) or per token (`api`) and is `null` when undeclared. Both are inherited through `extends`. `data_residency` declares where the entry's vendor processes data, `eu` or `global`, `null` when undeclared, inherited through `extends`; `resolve` and `agents get` show it, and acpc enforces nothing with it. `limit_wait_max`, with the syntax of the global key, caps the usage-limit waiting of the entry's turns and is inherited through `extends`; undeclared, the global value applies. An entry may send an effort level only when it is in acpc's effort scale, is advertised by the adapter for the resolved model when acpc has observed that model's levels, and is listed in the row for that model in the entry's own `[effort_by_model]` when the entry's files define one; a shipped adapter row applies only while no advertised row has been observed, a model with no row anywhere is checked against the scale alone and the adapter's answer decides, the adapter's `default` is never a selectable level, and an entry that names no effort sends none, so the vendor's default applies. A preset's effort rides only with the preset's model: `--model <tier>` and an entry that names neither model nor effort take both from the preset. A refused level is `invalid_input` with exit 2, naming the model, the allowed levels and what excluded it. `system_prompt_file` names a file whose content replaces the vendor's own system prompt in every session of the entry; `tools` lists the vendor tools those sessions may use, and `[]` leaves none. Both are inherited through `extends`, and a child's `tools = []` overrides the parent's list. The adapter declares the routes it has: `system_prompt_via` is `acp_meta` (sent as `_meta.systemPrompt` on `session/new`), `codex_config` (the entry's file is named as `model_instructions_file` in the `CODEX_CONFIG` JSON object of the adapter's environment, merged into any value the entry or the caller already sets, so only the path travels; the adapter reads the file at every session start and resume, and the replacement covers the vendor's base instructions only, not the instruction files of the vendor home; a `CODEX_CONFIG` that is not a JSON object is `invalid_input` before any session exists) or `none`, the default; `tools_via` is `acp_meta` (sent as `_meta.claudeCode.options.tools`) or `none`, the default. An entry that sets a key its adapter has no route for fails `run` with `not_supported`, exit 2, before any session exists, naming the adapter and the alternative where one exists; `resolve` reports the same refusal. `agents get` shows such an entry with the refusal in a `refused` row and field (`null` when the keys have a route), and `agents check`, `agents get --models`, `probe` and `continue` work on it, because they send no prompt and no tool list of their own. acpc never falls back to the vendor's own prompt. On `codex_config`, `system.md` is acpc's copy of the entry's file at the first turn; `continue` refuses with `conflict`, naming both paths, when that file is missing or no longer matches it. `agents check` tests the adapter connection only and sends no system prompt. `one_shot = true` declares that each session of the entry is consumed once and keeps no data. When its turn ends, in any terminal state, the session directory is reduced under the session lock to `meta.json`, with `prompt_snippet` empty, plus the pending `answer.md` and `transcript.ndjson`: the prompt, the system prompt and any parked turn files are removed, `retained` becomes `false`, and the id stays reserved like a deleted one. The reduction is published together with the turn's final metadata, so no reader sees a finished one-shot session that is still whole, and an adapter update that arrives after it is dropped rather than written. The pending files are delivered once: by `run` in the foreground, or by the first `wait` after `run --background`. Once that command has written its result to stdout or to `--output-file`, and the transcript to `--transcript-file` when given, and closed them, it removes both files under the session lock; a delivery that is cut short, by a broken stdout or a killed process, leaves them in place. A later `wait` fails with `conflict` naming `one_shot`; `status` and `list` serve the session from its metadata. `continue` refuses a one-shot session with `conflict` naming `one_shot` in every state, because the adapter keeps no session to resume either, and `log` refuses it once the turn has ended. A one-shot session dispatched with `--background` keeps its pending files until a `wait` collects them; `delete` and `prune` remove them like any session's. On an adapter whose `tools_via` is `acp_meta`, a one-shot session sends `persistSession: false` in the same options object as the tool list, so the vendor keeps no transcript of its own; on any other adapter the vendor keeps whatever it stores itself, and `one_shot` governs acpc's files only. `one_shot` is inherited through `extends`; `resolve` and `agents get` show it. `setting_sources` lists which of the vendor's setting sources a session loads: any of `user`, `project` and `local`, in any order, each at most once. Unset, the vendor's own default applies and nothing is sent; `[]` loads none and also sets `strictMcpConfig: true`, so no MCP server comes from those sources either. It travels as `settingSources` in the same options object as the tool list, so an entry that sets it on an adapter whose `tools_via` is `none` fails `run` with `not_supported` like `tools` does. A child's list replaces the parent's through `extends`; `resolve` and `agents get` show it, `null` when unset. A local file with `extends` is a variant; a local file with `command` and no `extends` is a new adapter; a local file under a shipped adapter name is an override.

```toml
extends = "codex"
model = "gpt-5.6-luna"
effort = "xhigh"
permissions = "execute"
home = "~/.codex-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]

[env]
MODEL_PROVIDER = "openrouter"
```

A judge entry leaves the model, effort and system prompt to the call (`run judge --model M --effort E --system-prompt-file FILE`); `system_prompt_file` on the entry pins a prompt instead:

```toml
extends = "bare"
permissions = "read"
tools = []
one_shot = true
setting_sources = []
```

`acpc agents get NAME` shows each resolved field and its source. `resolve` shows one concrete call. The adapter environment is constructed from a base system set, declared variables and explicitly named pass-through variables; the rest of the caller environment is not inherited. Secrets are read at dispatch time and are never written to state.

The permission scale is `none`, `read`, `edit`, `execute`, `all`, with `ask` as a separate policy that prompts for categories above read. `write` and `prompt` are deprecated aliases for `execute` and `ask`. Categories come from ACP tool-call kinds: reads, searches, fetches and thoughts are read; edits are edit; execute, delete and move are execute; unknown kinds are unknown. A mode is eligible only when its measured `grants` does not exceed the requested policy. Among eligible modes acpc prefers delegation and then the highest grant. A mode declared `selectable = false` is never chosen by policy selection and is reached only through `--mode`, under the same grants check. `delegates`, `escalates` and `selectable` are descriptive facts, not extra policy levels.

`ACPC_CEILING` carries an inherited ceiling into nested dispatch. A child cannot request more authority than its parent. When the ceiling is below the lowest policy an entry runs under, the refusal names the ceiling rather than suggesting `--permissions`, which the ceiling would clamp again. A denied category is recorded in the answer result and does not itself make the turn fail; the adapter can also suppress a request by handling an action internally.

## Bundled skills

The package ships the skills `adapter-bringup`, `provider-bringup` and `refresh-adapter-models`. `skills list` provides their names, descriptions and package paths. `skills get NAME` prints the body and identifies the source directory on stderr. Bundled skills are not a user skill marketplace and are not installed into a harness-wide directory.

## `--help` as first-contact documentation

`acpc --help` is a compact cheat sheet for the common blocking, background, wait, log, steering, continuation, maintenance and machine-readable paths. `acpc COMMAND --help` provides progressive disclosure for every command and group. Each option has a description and either a real default or a statement of where its value comes from. `-h` and `--help` are equivalent; `-V` and `--version` print only the version string, currently `1.1.1`.

Help is generated from the parser's live surface. The schema includes flags that are accepted but deliberately not shown in human help only when the parser still accepts them; the current `continue` command has no hidden resolution flags. A migration hint for a removed spelling is part of the usage error whenever acpc can identify the replacement.

## Anti-features

acpc does not provide a TUI, spinners, terminal streaming as the primary result, built-in pipelines or agent teams, a rich behavior-changing config file, a user skill marketplace or an MCP wrapper. The caller is the orchestrator. Live activity belongs in the transcript and is read through `log`; the terminal result remains bounded and machine-readable.

## How an agent consumes a CLI

The caller sees a shell-tool result once, so stdout is the answer or a bounded structured record stream, not progress noise. A result that may exceed the tool window belongs in `answer.md`, `transcript.ndjson` or an explicit `--output-file`. The session id and directory are available at dispatch for blocking calls and immediately for background calls.

The caller should select `--permissions edit` for file changes and `--permissions execute` when the agent must also run commands. A non-TTY omitted policy defaults to `read`, so write-capable tasks must say what they need. The caller should use `wait` for a background result, `log` for progress or a cursor, `status` for fixed-cost metadata, and `continue` for a new turn with the same session context.
