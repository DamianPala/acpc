# Migrating to acpc 1.0

This guide describes the public changes accumulated before acpc 1.0. The command and flag rows were checked against the installed binary on 2026-09-08. Historical spellings are intentionally kept here because this document is the place a user looks when an old call fails.

## Command and flag changes

| Old call or spelling | Current call | What changes |
| --- | --- | --- |
| `acpc agents` | `acpc agents list` | The group now requires an explicit collection verb. |
| `acpc agents NAME` | `acpc agents get NAME` | Named entry views use an explicit verb. |
| `acpc agents NAME --models` | `acpc agents get NAME --models` | The models view belongs to `get`. |
| `acpc agents NAME --commands` | `acpc agents get NAME --commands` | The commands view belongs to `get`. |
| `acpc agents --models` | `acpc agents get NAME --models` | A catalog must be selected for one adapter. |
| `acpc agents --commands` | `acpc agents get NAME --commands` | A catalog must be selected for one adapter. |
| `acpc agents --check [NAME]` | `acpc agents check [NAME]` | Live checks are a subcommand. |
| `acpc agents init NAME --extends BASE` | `acpc agents create NAME --extends BASE` | Variant creation is a strict create. |
| `acpc skills` | `acpc skills list` | The group now requires an explicit collection verb. |
| `acpc skills NAME` | `acpc skills get NAME` | Skill bodies use an explicit verb. |
| `acpc rm ID` | `acpc delete ID --yes` | Session deletion has a confirmation gate. |
| `acpc stop ID` | `acpc cancel ID` | Session cancellation has a verb distinct from daemon control. |
| `acpc status` | `acpc list` | Collection listing is a separate command. |
| `acpc status --json` | `acpc list --json` | Collection-only flags belong to `list`. |
| `acpc status --limit N` | `acpc list --limit N` | Collection limits belong to `list`. |
| `acpc status --plain --limit N` | `acpc list --plain --limit N` | Plain collection output belongs to `list`. |
| `acpc log ID --tail N` | `acpc log ID --tail N` | `--tail` is restored and selects the last N matching records. It cannot be combined with `--limit` or `--follow`. |
| `acpc log ID --limit N` from the earlier 1.0 draft | `acpc log ID --tail N` when the caller needs the last N records | `--limit` changes meaning for the second time in 1.0: it now emits the first N records from the selected position, including after `--since`; it no longer chooses the tail. Keep `--limit` for forward polling. |
| `acpc log ID -f` | `acpc log ID --follow` | `-f` is reserved for daemon `--force`. |
| `acpc run AGENT PROMPT --dry-run` | `acpc resolve AGENT` | Resolution preview is a separate read-only command. |
| `acpc continue ID PROMPT --dry-run` | `acpc resolve AGENT` | A session continuation has no resolution preview. |
| `acpc continue ID PROMPT --resolve` | `acpc resolve AGENT` | Resolution preview is not a continuation flag. |
| `-o FILE` or `--output FILE` | `--output-file FILE` | The flag name is explicit and has no old alias. |
| `--bg` | `--background` | `--bg` remains a compatibility alias. |
| `--yes` | `--yes` or `-y` | `-y` is the short spelling for confirmation. |
| `--force` | `--force` or `-f` | The short spelling belongs to daemon stop. |
| `continue --model` | `acpc run --model` | A continuation uses the session's stored model. |
| `continue --effort` | `acpc run --effort` | A continuation uses the session's stored effort. |
| `continue --mode` | `acpc run --mode` | A continuation uses the session's stored mode. |
| `continue --cwd` | `acpc run --cwd` | A continuation uses the session's stored working directory. |
| `continue --home` | `acpc run --home` | A continuation uses the session's stored vendor home. |
| `continue --name` | `acpc run --name` | A name is assigned when a session is created. |

The old `--bg` spelling is the only command-surface alias retained in this table. The aliases `-y`, `-f` and `-n` are current short spellings, not old spellings: `-y` confirms, `-f` forces daemon stop, and `-n` is the dry-run alias for prune and daemon stop.

## Behavioral changes

### Deadlines

`--timeout` on `run`, `continue` and `steer` now bounds only the observing client's wait. It leaves accepted work alive and returns exit 124 with `kind: timeout`, the session id and the last observed status. `--cancel-after` changes the work itself by sending ACP cancellation. `wait` and `log --wait-new` or `--follow` retain the observation-only deadline behavior.

### Session states

The public states are `starting`, `running`, `preparing`, `succeeded`, `failed`, `canceled` and `unknown`. The reader normalizes historical state names before output. A historical terminal deadline state becomes `failed` with `stop_reason: error` and `exit_code: 1`; exit 124 is reserved for the observing client's deadline. A lost host process becomes `unknown`, which is terminal for management but says that the operation's result was not observed.

### Formats and output

The tool-wide default is text on a TTY and JSON on a non-TTY. Native answer and stream commands explicitly use text in both contexts. Collections use `items` and `has_more`; plain output is one identifier per line and requires an explicit limit. `log --json` is NDJSON, one transcript record per line.

`agents check` always returns a collection, including a named check. `prune` and daemon stop return `targets`, `changed` and `requires_confirmation`. `install` returns `changed: null` on success because the vendor installer does not report whether it changed anything. Failed machine-format calls leave stdout empty and put the failure envelope on stderr.

### State files and transcripts

Metadata is written under the canonical `status` key. The public transcript header is `{"schema": "acpc.transcript/2"}`. A transcript with the previous `/1` header is rejected as unsupported rather than read as if it were current; remove the incompatible session directory after preserving any evidence you need.

## Usage-error hints

The binary names the current command in usage errors for the old command and flag forms above. In particular, the old collection forms point to `list`, named adapter and skill forms point to `get`, the old check form points to `agents check`, the old session verbs point to `delete` or `cancel`, the old log flags point to `--limit` or `--follow`, the old output flag points to `--output-file`, and the old resolution flags point to `resolve` as appropriate.

If a shell script needs to distinguish a migration error from a runtime failure, every recognized removed spelling is `invalid_input` with exit 2. A runtime failure has another `kind` and usually exits 1. A missing resource or a state conflict is exit 1 because the current command spelling is valid.
