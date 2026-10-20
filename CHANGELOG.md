# Changelog

## [1.2.0] - 2026-10-20

### Added

- Judge-style entries: `system_prompt_file`, `tools` and `setting_sources` entry keys and `run --system-prompt-file`. claude carries them in the session `_meta`; codex carries the prompt as `model_instructions_file` inside `CODEX_CONFIG`; a key the adapter has no route for is refused instead of running under the vendor prompt. The prompt in force is stored in the session's `system.md` and sent again on a cold resume; `continue` refuses with `conflict` when a codex entry's prompt file no longer matches it
- `one_shot` entry key: when the turn ends, the session is reduced to `meta.json`, `answer.md` and `transcript.ndjson`, delivered once by the foreground `run` or the first `wait`; later `wait`, `continue`, `log` and `steer` fail with `conflict`; results carry `retained`
- `--transcript-file FILE` on `run` and `wait` copies the finished transcript as NDJSON; the result reports `transcript_file`
- `served_model` in results and `status`: the model that actually answered, with one stderr note per turn when it differs from the requested one
- `--limit-wait DURATION` on `run` and `continue`, and the `limit_wait_max` entry key, cap how long a call waits out a usage limit; `0s` fails fast with `failure.class` `rate_limit` and the resume time in `next_step`
- Observed effort levels: `agents check` probes every model the entry can reach and records the levels the adapter advertises under `advertised.efforts`; `agents get` shows them per model
- Subscription windows: claude's 5-hour and 7-day utilization is kept per vendor home in `cache/claude/limits.json` and shown by `agents get` (`windows` row) and `agents check`
- `data_residency` entry key (`eu` or `global`), inherited through `extends`, shown by `resolve` and `agents get`; declarative only, nothing enforces it

### Changed

- **BREAKING:** effort is resolved against what the adapter advertises: the allowed levels are the effort scale ∩ the adapter's observed row for the model ∩ the entry's `[effort_by_model]` row, and an entry that names a model without an `effort` sends none (`vendor default`) instead of inheriting the adapter's default. Migration: codex entries lose the inherited `xhigh`, claude and grok entries the inherited `high`; set `effort` where a level is wanted, pass `--effort` with an explicit `--model`, and drop levels the adapter does not advertise (claude's `default` among them)
- **BREAKING:** `failure` in results and `status` is an object `{class, message, next_step}` with `class` one of `auth`, `rate_limit`, `adapter_limit`, `connection`, `adapter_error`, `acpc`, and `schema_version` is `2`. Migration: read `failure.message` for the old text and accept schema version 2
- `--permissions edit` on claude selects the default mode instead of `plan`, so the agent can edit and answers on the entry's model; the mode fact `selectable = false` marks modes a policy never lands on (`--mode` still reaches them)
- `agents list`, `agents check` and `skills list` show up to 100 entries by default and fit the text roster to the terminal width (long models and homes cut in the middle, descriptions truncated with one footer); machine formats keep every field
- Read-only commands start faster: the ACP library is imported only by commands that talk to an adapter; `import acpc.cli` drops from about 500 ms to under 80 ms and `--version` answers in under 0.1 s

## [1.1.1] - 2026-10-19

### Changed

- README: on `claude`, `edit` selects `plan` today, which refuses ordinary file writes and can answer on a different model than the entry names; a claude entry that must write uses `execute`
- `claude` presets: `fast` runs Haiku 5.5 at `low` and `max` runs Opus 5.5 at `max`; Haiku 5.5 and Opus 5.5 gain their effort rows (`low` to `max`), so `--effort` works on them
- `grok` presets: `standard` and `max` run grok-4.7 (`high` and `xhigh`); `fast` stays on grok-4.5 at `low`
- `delete --help` and the spec name `status <id>` as the preview of a deletion; there is no `--dry-run`

### Fixed

- An option given without its value (`log --tail`, `wait --timeout`, `list --limit`, `run --model`, …) fails as a one-line `invalid_input` usage error with a hint and exit 2, instead of a traceback
- Ctrl-C during a command's start-up no longer escapes as a bare `KeyboardInterrupt`: SIGINT is held while the event loop is built, and for a turn until the turn's own handler is installed, so the turn ends with the `interrupted` envelope; daemons and adapters spawned during start-up begin with SIGINT unblocked

## [1.1.0] - 2026-09-24

### Added

- `usage` in `status`, result documents and `meta.json`: what the session consumed, cumulative over its turns, per model (`total_tokens`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`), with `calls`, `compactions`, `gaps`, `quality` (`exact` or `estimate`), `source` and `billing`. Entries declare it through `usage_profile` and `billing`; `usage` is `null` for an entry without a profile and for sessions written before 1.1. acpc still prices nothing
- Codex usage limits are waited out like Claude's: the resume time comes from codex's "try again at …" text, read in the local time zone; a plan with no access still fails at once
- `status` shows the current `context` while a turn runs, refreshed at most every 2 s, instead of the value from the end of the previous turn
- `continue` accepts `--model`, `--effort`, `--mode`, `--cwd`, `--home` and `--name` as checks: a value equal to the stored one is a no-op, a different one is a usage error naming both
- Each turn's transcript ends with a `usage` event holding the adapter's raw usage report and its name and version
- `usage.drift`: when an adapter's usage reports break the invariants of the entry's usage profile, acpc records the first violation, keeps `quality` at `estimate` for the rest of the session and prints one note on stderr
- `python -m acpc` runs the CLI

### Changed

- **BREAKING:** `--max-output` on `run`, `continue`, `steer` and `wait` accepts `0` (no cap) or at least 4096 bytes; 1–4095 is a usage error. Migration: use `0` or a value of 4096 or more
- `run` and `resolve` refuse a working directory that does not exist (`invalid_input`), and `continue` and `steer` refuse a session whose recorded directory was removed (`conflict`), before any state is written or anything is sent
- A relative `--home` on `run`, `resolve` and `continue` resolves against the caller's directory, like the other path flags
- An inherited `ACPC_CEILING` below every mode of the entry fails naming the ceiling, instead of suggesting a `--permissions` value the ceiling would clamp again
- The `--max-output` cap never cuts the result's envelope: when the envelope alone exceeds it, only the answer shrinks to the truncation marker
- `permissions.source` no longer carries the clamp as text; the structured `clamp` field is the only place it appears

### Fixed

- Removing the directory a call was started from no longer breaks the daemon or the adapters it keeps warm: they now run from acpc's own state directory
- The answer, the condensed `log` view and `log --prose` split messages at the same points, and updates that only report state (context usage, available commands, configuration, mode, session info) no longer cut one message in two
- `--output-file` works with `/dev/null` and FIFOs, and a target acpc cannot write fails with `permission_denied` naming the path
- The condensed `log` view no longer repeats an unchanged context reading within a turn
- macOS: a liveness check forks one `ps` instead of two, and only one column when the session has no start time to compare; the pty tests now run on macOS in CI

## [1.0.1] - 2026-09-22

### Added

- `resolve <agent>` previews how a call resolves (model, effort, mode, permissions, spawn command) without dispatching anything
- `list` is the bounded session collection (`--limit`, default 20, `has_more`); `status <id>` is the single-session detail view
- `schema [command...]` publishes the whole command surface as JSON generated from the parser: arguments, flags, effects, confirmation gates and output shapes
- Off a terminal, `run`, `continue`, `steer` and `wait` print a tagged `<result>` document with `<metadata>` and `<answer>` sections; `--json` carries the same answer and document
- Every failure is one structured error envelope on stderr with a stable `kind`, a `hint` and a `next` command; exit 2 for usage, 124 for a client deadline, 1 otherwise
- `waiting` state: a vendor usage limit with a known reset is waited out inside the same turn and the prompt resent (cap `limit_wait_max`, 8h by default, `"0s"` disables)
- `continue <id>` with no message picks up an interrupted turn (`canceled`, `failed`, `unknown`) with acpc's own continuation instruction
- `steer` corrects a running turn in place when the adapter supports it; `--steer-mode cancel-then-start` keeps the old behavior, and every receipt names the session's correction mode
- `--cancel-after S` cancels the work on a deadline; `--timeout S` now only bounds the client's wait and leaves the turn running (exit 124, `kind: timeout`)
- Failed and canceled turns return the same result document as a success, with the partial answer and `status`, `stop_reason` and `next` in the metadata
- `status` and result documents report the applied permission policy, any inherited-ceiling clamp and permission denials as structured fields
- `agents delete NAME` removes a locally created entry; `daemon stop --dry-run` previews its targets
- CI runs the full gate on Linux and macOS

### Changed

- **BREAKING:** `tokens` and `cost` are gone. `context {used, size, peak}` replaces them, `null` until the adapter has reported usage; acpc publishes no cost figure. Migration: read `context.used` where you read `tokens`; there is no replacement for `cost`
- **BREAKING:** command surface: `stop` → `cancel`, `rm` → `delete --yes`, bare `status` → `list`, `agents [NAME]` → `agents list` / `agents get NAME`, `agents --check` → `agents check`, `agents init` → `agents create`, `skills [NAME]` → `skills list` / `skills get NAME`, `run --dry-run` → `resolve`, `-o`/`--output` → `--output-file`, `--bg` → `--background` (`--bg` stays as an alias). Old spellings fail as `invalid_input` naming the replacement
- **BREAKING:** session states: `done` → `succeeded`, `cancelled` → `canceled`, `orphaned` → `unknown`, plus the new `waiting`; the JSON field is `status`, not `state`. A client deadline is no longer a terminal state. Migration: match the new names and treat `waiting` as non-terminal
- **BREAKING:** `delete`, `prune`, `install`, `agents delete` and a bare `daemon stop` require `--yes` (or a terminal prompt) before acting. Migration: add `--yes` in scripts
- **BREAKING:** `log`: `--tail N` selects the last N records, `--limit N` the first N after the cursor, never both; `--follow` replays the whole transcript unless `--tail` is given; `-f` is reserved for `--force`; `--json` is NDJSON. Migration: use `--tail N` where `--limit N` meant the last N records
- **BREAKING:** collections (`list`, `agents list`, `agents check`, `daemon status`) return `items` and `has_more`; plain output is one id per line and needs an explicit limit
- **BREAKING:** timestamps in status and log are RFC 3339 text; the transcript header is `acpc.transcript/2` and `/1` transcripts are rejected. A pre-1.0 `meta.json` is still read. Migration: remove incompatible session directories after keeping any evidence you need
- **BREAKING:** `continue` uses the session's stored model, effort, mode, cwd, home and name; its overriding flags are gone. Migration: set them on `run`
- `wait`, `cancel` and `steer` are pinned to the turn selected at call start; a turn that already moved on is reported as a `conflict`, never touched. `cancel` waits for the daemon's acknowledgment and reports whether anything changed
- Deleted session ids are tombstoned and never reused; `prune` rechecks every session's eligibility right before removing it
- The default permission policy reads stdin and stdout as separate signals; `--background` on a terminal asks which policy to detach with
- Prompts, continuations and steering text over 1 MiB are refused before any session state exists, from an argument, stdin or `--prompt-file` alike
- Text views escape control bytes and ANSI sequences in adapter- or caller-supplied text
- `--version` prints the bare version string

### Fixed

- Process identity and liveness on macOS: status, cancel and daemon checks no longer misreport a live or dead session on Darwin
- `daemon status` no longer risks stopping a daemon started by a different acpc version
- An adapter stuck in its initialize handshake fails with a clear error instead of hanging the session; a background `run` returns its receipt only after the handshake
- A directly spawned session killed during its first turn stays resumable
- A state root whose socket path exceeds the platform limit is refused up front for background runs and falls back to a direct child process for blocking ones
- `--output-file` no longer hides the answer from the JSON result, and a call without a result leaves no stray file
- `log --max-output` truncation is reported on stderr instead of as a fake event inside the stream
- Shipped `claude` and `codex` adapter facts refreshed to what the vendors currently support

[1.2.0]: https://github.com/DamianPala/acpc/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/DamianPala/acpc/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/DamianPala/acpc/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/DamianPala/acpc/compare/v0.7.1...v1.0.1
