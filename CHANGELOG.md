# Changelog

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

[1.1.0]: https://github.com/DamianPala/acpc/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/DamianPala/acpc/compare/v0.7.1...v1.0.1
