# Architecture — acpc 0.3

Module map for the 0.3 rewrite, derived from SPEC.md's contract (state on disk, session lifecycle, daemon targets, stream discipline). Not a mirror of the 0.3.0.dev1 layout: modules exist because the spec's contract needs them.

## Layers

```
cli.py                          argument parsing, verbs, help, exit codes, TTY detection
  │
  ├── registry.py               entry resolution (adapter TOMLs + user entries, provenance)
  ├── sessions.py               session store: ids, meta.json, states, locks, rotation
  ├── runner.py                 turn orchestration: route, signals, timeout, answer
  │     ├── daemon_client.py    connect-or-spawn per target, request protocol
  │     │     ├── targets.py    (frozen) target keying
  │     │     └── ipc.py        (frozen) NDJSON-over-UDS transport
  │     ├── daemon.py           daemon process: warm adapters, TTL, turn slots
  │     ├── spawn.py            (frozen) adapter subprocess spawn, direct fallback
  │     ├── client.py           AcpcClient: ACP updates → transcript, permissions, answer
  │     └── environment.py      (frozen) adapter env construction
  ├── transcript.py             NDJSON transcript: schema, append, cursors
  ├── render.py                 log/status views, footers, --max-output truncation
  ├── output.py                 output contract: stdout modes, stderr summary, --json
  ├── cache.py                  advertised models/modes/commands cache under cache/<agent>/
  └── config.py                 config.toml (retention, daemon_ttl, daemon_max_concurrent)
  └── help.py (optional)        S12 may split the help text out of cli.py if it warrants it

paths.py (frozen)               ~/.acpc layout, ACPC_HOME, atomic_write, 0700/0600
proc.py (frozen)                process identity/liveness, kill_process_tree, pidfd
permissions.py (frozen)         kind classification + approval policy
vocab.py (frozen)               efforts, permission values, session states, exit codes
```

"(frozen)" = Stage 1 harvested foundation; read-only for Stage 2 implementer agents (see PLAN.md).

## Module ownership

| Module | Owns | SPEC.md sections |
|--------|------|------------------|
| `cli.py` | Verb surface, flag parsing, usage errors (exit 2), two-level `--help`, `-V`, TTY vs non-TTY rules, `last` selector | *Command surface*, *`--help`*, *TTY vs non-TTY* |
| `config.py` | `config.toml` strict load (3 keys, unknown key = hard error), defaults | *State on disk* (config) |
| `registry.py` | Shipped adapter TOMLs (`data/agents/`), user entries (`agents/`), `extends` resolution with per-field provenance, presets (tier → model+effort), effort superset mapping, bypass-mode lists, install status, `agents init` scaffolding | *Agent variants*, *`agents`*, *`install`* |
| `sessions.py` | Session ids (4 chars, 32-glyph alphabet, re-roll on collision), session dirs, `meta.json` lifecycle (atomic replace), state machine + orphan detection (via `proc`), 30s startup grace, per-session lock, `--name` aliases, turn rotation (`prompt.<n>.md` at next-turn start), rm/prune | *State on disk*, *Session states*, *`rm`*, *`prune`* |
| `transcript.py` | `transcript.ndjson`: versioned header, whole-line appends, event schema, cursor = global event index, reader with `--since`/`--tail` selection | *State on disk* (transcript), *`log`* (cursor semantics) |
| `client.py` | `AcpcClient` (ACP Client impl): session/update → transcript events, `request_permission` answering via `permissions`, bypass-mode switch guard, answer assembly (message chunks in stream order, thoughts/tools excluded), tokens/cost accounting | *Output contract* ("the answer"), *Permissions* (runtime) |
| `runner.py` | One turn end to end: route daemon-vs-direct (visible fallback), prompt dispatch, `--timeout` (cancel, 124), SIGINT → cancel (130), SIGTERM → detach (143, direct child: cancel), exit codes from stop_reason, `answer.md` + meta finalization, auto-prune hook | *`run`*, *`continue`* (turn machinery), *Output contract* (exit codes, client death) |
| `daemon.py` | Daemon process per target: keeps one adapter warm, serves sessions, `daemon_max_concurrent` turn slots + queueing, idle TTL expiry, version-skew self-restart, per-target log (adapter stderr), `daemon stop` state transitions | *`daemon`* |
| `daemon_client.py` | Client side of the daemon protocol: ensure-running (spawn+connect race-safe via lock), request/response framing over `ipc`, orphan-never rules | *`daemon`*, *Session states* |
| `render.py` | Condensed event lines, `--prose` view, footers (stderr, `--` prefix, `|`/`·` separators), `--max-output` truncation (UTF-8 boundary, marker line, event-granular for `log`), status line/detail views | *`log`*, *`status`*, *Output contract* |
| `output.py` | stdout discipline (answer / confirmation / JSON envelope / id+dir), stderr summary line, `--json` shapes, `-o` atomic write | *Output contract* |
| `cache.py` | `cache/<agent>/` advertised data (models/modes/commands), refresh on every run, cache-age footers, `commands.md` full-text render | *`agents`* (advertised data) |

## How the layers talk

**`run` happy path:** `cli` parses → `registry` resolves the entry + call flags into a full resolution (model, effort, permissions, home, env — each with provenance; `--dry-run` prints exactly this) → `sessions` allocates id + dir, writes `prompt.md` and `meta.json` (state `starting`) → `runner` computes the daemon target (`targets.target_for_call`), asks `daemon_client` for a connection (spawning the daemon if needed), sends the turn → daemon's adapter streams ACP updates → `client` appends transcript events and accumulates the answer → turn ends: `runner` writes `answer.md`, finalizes meta (state, exit code, stop_reason, tokens), `output` prints the answer on stdout + summary on stderr.

**Daemon fallback:** if the daemon can't start, `runner` spawns the adapter as a direct child via `spawn.spawn_adapter` — same `client`, same transcript; the stderr summary says so and SIGTERM then cancels instead of detaching.

**State reads (`status`/`log`/`wait`/every gate):** read `meta.json`, and for `running` sessions verify liveness via `proc` (pid + start-time token); a dead process ⇒ persist `orphaned` (atomic, under the session lock). No command trusts a stored `running`.

**Streams:** stdout carries exactly one thing per the output contract; everything acpc says about itself goes to stderr with the `--` prefix; adapter stderr goes to the per-target daemon log (or acpc's stderr on the direct path). The transcript file is the only streaming channel.

## Key decisions (fixed for Stage 2)

1. **Entry names come from filenames**: `agents/<name>.toml`, no `identity` field (spec shows `builder.toml` without one). A user file under an adapter's own name overrides that adapter's fields; with `command` and no `extends` it defines a new adapter.
2. **Adapter TOML schema** (shipped and user, same parser): `name`, `author`, `command`, `install_command`, `home` (default vendor home), `home_env` (the env var that delivers `home` to the adapter process — delivery mechanism, not spec surface), `bypass_modes`, `efforts` (supported superset levels), `env_passthrough`, `[presets]` (`tier = { model, effort }`), and for variants `extends`, `description`, `model`, `effort`, `permissions`, `[env]`.
3. **Daemon runtime files live under `daemon/`**: `<target>.sock`, `<target>.lock`, `<target>.log` — one dir for all per-target runtime state (spec names only the logs; sockets/locks are plumbing).
4. **The daemon hosts sessions, the client is a thin viewer**: a `--bg` session belongs to the daemon process; `wait`/`log` read files and the daemon is never needed to *read* state. Session files are written by whichever process runs the turn (daemon, or the direct-path client).
5. **Session id alphabet**: `abcdefghijkmnpqrstuvwxyz23456789` (32 glyphs, minus `0/o/1/l`), 4 chars, re-rolled on collision.
6. **Transcript schema**: header line `{"schema": "acpc.transcript/1"}`; events carry a global 1-based index `i`; consumers ignore unknown fields.
