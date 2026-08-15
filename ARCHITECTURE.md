# Architecture — acpc 0.3

Module map derived from SPEC.md's contract (state on disk, session lifecycle, daemon targets, stream discipline): modules exist because the spec's contract needs them.

Structural changes — new modules, moved seams, changed ownership — update this file in the same change as the code.

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
  ├── skills.py                 bundled skill discovery, frontmatter, and body loading
  ├── output.py                 output contract: stdout modes, stderr summary, --json
  ├── probe.py                  direct ACP mode discovery and registry diff
  ├── cache.py                  advertised models/modes/commands cache under cache/<agent>/
  └── config.py                 config.toml (retention, daemon_ttl, daemon_max_concurrent)

paths.py (frozen)               ~/.acpc layout, ACPC_HOME, atomic_write, 0700/0600
proc.py (frozen)                process identity/liveness, kill_process_tree, pidfd
permissions.py (frozen)         kind classification + approval policy
vocab.py (frozen)               efforts, permission values, session states, exit codes
```

"(frozen)" = harvested from 0.2 verbatim with their tests; the marker records provenance, not an edit restriction (build history in status.md).

## Module ownership

| Module | Owns | SPEC.md sections |
|--------|------|------------------|
| `cli.py` | Verb surface, flag parsing, usage errors (exit 2), two-level `--help`, `-V`, TTY vs non-TTY rules, `last` selector | *Command surface*, *`--help`*, *TTY vs non-TTY* |
| `skills.py` | Bundled `data/skills/*/SKILL.md` discovery, hand-parsed descriptions, directory-name identity, body loading | *Bundled skills* |
| `config.py` | `config.toml` strict load (3 keys, unknown key = hard error), defaults | *State on disk* (config) |
| `registry.py` | Shipped adapter TOMLs (`data/agents/`), user entries (`agents/`), `extends` resolution with per-field provenance, presets (tier → model+effort), effort superset mapping, entry mode and measured mode tables, install status, `agents init` scaffolding | *Agent variants*, *`agents`*, *`install`* |
| `sessions.py` | Session ids (4 chars, 32-glyph alphabet, re-roll on collision), session dirs, `meta.json` lifecycle (atomic replace), state machine + orphan detection (via `proc`), 30s startup grace, per-session lock and ephemeral cross-process reservations, `--name` aliases, pre-slot turn rotation and ownership tokens, delivered-prompt markers, locked answer/state finalization including delivery-record incompleteness, rm/prune | *State on disk*, *Session states*, *`rm`*, *`prune`* |
| `transcript.py` | `transcript.ndjson`: versioned header, whole-line appends, event schema, cursor = global event index, reader with `--since`/`--tail` selection, bounded read-only tail timestamp for `status` | *State on disk* (transcript), *`log`* (cursor semantics), *`status`* (idle age) |
| `client.py` | `AcpcClient` (ACP Client impl): session/update → transcript events, `request_permission` answering via `permissions`, mode-switch handling, answer assembly (message chunks in stream order, thoughts/tools excluded), tokens/cost accounting; connection-owned routing captures validated session identity on every raw update, replay generations add per-frame identities, suppression uses validated session/generation tags, and accounted history has a bounded closed-generation backstop | *Output contract* ("the answer"), *Permissions* (runtime), replay silence |
| `runner.py` | One turn end to end: reserved pre-slot cold-resume verification and rotation, route daemon-vs-direct (visible fallback), prompt dispatch, delivery tracking, `--timeout` (cancel, 124), SIGINT → cancel (130), SIGTERM → detach (143, direct child: cancel) with the handler recording only which signal arrived and its meaning resolved once the route is known, token-checked finalization after a claim, preparation-cancel placeholders, exit codes from stop_reason, `answer.md` + meta finalization, delivery-record incompleteness propagation, auto-prune hook | *`run`*, *`continue`* (turn machinery), *Output contract* (exit codes, client death) |
| `daemon.py` | Daemon process per target: in-memory turn registration and preparation phase, pre-slot resume preparation with in-memory rollback for failed verification, keeps one adapter warm, serves sessions, generation-aware callback demultiplexing, `daemon_max_concurrent` prompt slots + queueing, idle TTL expiry, version-skew self-restart, per-target log (adapter stderr), `daemon stop` state transitions | *`daemon`* |
| `daemon_client.py` | Client side of the daemon protocol: ensure-running (spawn+connect race-safe via lock), request/response framing over `ipc`, preparation-only waits, orphan-never rules | *`daemon`*, *Session states* |
| `render.py` | Condensed event lines, `--prose` view, footers (stderr, `--` prefix, `|`/`·` separators), `--max-output` truncation (UTF-8 boundary, marker line, event-granular for `log`), status line/detail views with idle age | *`log`*, *`status`*, *Output contract* |
| `output.py` | stdout discipline (answer / confirmation / JSON envelope / id+dir), stderr summary line, `--json` shapes, `-o` atomic write | *Output contract* |
| `probe.py` | Direct adapter mode discovery: one ACP connection, one session opened and released, the advertised catalogue and a two-sided diff against the entry's `[modes]`; never writes registry entries. Measuring what a mode permits is 0.7 work and lives on `probe-engine-r13` | *`probe`* |
| `cache.py` | `cache/<agent>/` advertised data (models/modes/commands), refresh on every run, cache-age footers, `commands.md` full-text render | *`agents`* (advertised data) |

## How the layers talk

**`run` happy path:** `cli` parses → `registry` resolves the entry + call flags into a full resolution (model, effort, mode, permissions, home, env — each with provenance; `--dry-run` prints exactly this) → `sessions` allocates id + dir, writes `prompt.md` and `meta.json` (state `starting`) → `runner` computes the daemon target (`targets.target_for_call`), asks `daemon_client` for a connection (spawning the daemon if needed), sends the turn → daemon's adapter streams ACP updates → `client` appends transcript events and accumulates the answer → turn ends: `runner` writes `answer.md`, finalizes meta (state, exit code, stop_reason, tokens), `output` prints the answer on stdout + summary on stderr.

**`continue` cold-resume path:** the daemon registers the turn in memory before acquiring the ephemeral per-session reservation. It claims the normal on-disk `starting`/`running` turn before adapter startup and restore, while exposing the daemon-only `preparing` phase through `status`; the reservation covers that synchronous snapshot-and-claim and nothing more, and is released before adapter startup and restore so one session's slow restore cannot stall the daemon's event loop. A competing continuation loses with the normal session-busy error — during preparation that exclusion comes from the durable `running` claim rather than from the lock. The daemon keeps the prior session files in memory so a failed verification can roll the claim back exactly; cancellation instead finalizes the claimed turn as `cancelled` with a no-prompt placeholder. Adapter mux bindings and replay generations close in their cancellation finalizers, and the next continuation restores from the prior adapter session rather than inheriting a partial prompt. `runner` independently checks the listed session and replayed user prompts when those capabilities exist. The claim's turn number guards finalization, while setup failures before ownership finalize that token; the ACP outgoing boundary records only prompts that were actually delivered, so a pre-dispatch crash does not poison a later resume, and a failed or unavailable delivery record permanently marks the session incomplete so a later resume cannot claim verification from an empty or partial record. Queued `--bg` requests wait for this preparation claim, without waiting for the prompt slot.

**Daemon fallback:** if the daemon can't start, `runner` spawns the adapter as a direct child via `spawn.spawn_adapter` — same `client`, same transcript; the stderr summary says so and SIGTERM then cancels instead of detaching.

**`probe` path:** `cli` resolves the registry entry → `probe` spawns one direct ACP adapter connection, opens a session, reads the advertised mode catalogue, and closes the session when the adapter advertises `session/close` → the catalogue is diffed against the entry's recorded `[modes]` from both sides. Zero turns, no daemon, no registry write. Its client answers every callback `method_not_found`: with no turn running, a callback would mean the adapter did something discovery never asked for.

**State reads (`status`/`log`/`wait`/every gate):** read `meta.json`, and for `running` sessions verify liveness via `proc` (pid + start-time token); a dead process ⇒ persist `orphaned` (atomic, under the session lock). `status` additionally overlays the daemon's ephemeral `preparing` register; no preparation marker is written to disk, and no command trusts a stored `running`.


**Streams:** stdout carries exactly one thing per the output contract; everything acpc says about itself goes to stderr with the `--` prefix; adapter stderr goes to the per-target daemon log (or acpc's stderr on the direct path). The transcript file is the only streaming channel. Cold resumes expose `resume: verified`, `resume: unverified — ...`, or `resume: unverified — delivery record incomplete` in the summary and JSON envelope.

## Key decisions

1. **Entry names come from filenames**: `agents/<name>.toml`, no `identity` field (spec shows `builder.toml` without one). A user file under an adapter's own name overrides that adapter's fields; with `command` and no `extends` it defines a new adapter.
2. **Adapter TOML schema** (shipped and user, same parser): `name`, `author`, `command`, `install_command` (trusted one-liner for `acpc install`, optional), `install_docs` (vendor URL when there is no trusted installer), `home` (default vendor home), `home_env` (the env var that delivers `home` to the adapter process — delivery mechanism, not spec surface), `[effort_by_model]` (optional per-model allowlists; the adapter union is derived from the table), `effort_config_id` (config-option id for effort when not `reasoning_effort`), `model_via` / `effort_via` (per-field apply path; omitted means `config_option` — see SPEC *Agent variants*), `effort_cli_flag` (CLI effort flag, default `--reasoning-effort`), `env_passthrough`, `[modes]` (the measured per-mode facts: `grants`, `delegates`, optional `escalates`), `[presets]` (`tier = { model, effort }`), and for variants `extends`, `description`, `model`, `effort`, `mode`, `permissions`, `[env]`.
3. **Daemon runtime files live under `daemon/`**: `<target>.sock`, `<target>.lock`, `<target>.log` — one dir for all per-target runtime state (spec names only the logs; sockets/locks are plumbing).
4. **The daemon hosts sessions, the client is a thin viewer**: a `--bg` session belongs to the daemon process; `wait`/`log` read files and the daemon is never needed to *read* state. Session files are written by whichever process runs the turn (daemon, or the direct-path client).
5. **Session id alphabet**: `abcdefghijkmnpqrstuvwxyz23456789` (32 glyphs, minus `0/o/1/l`), 4 chars, re-rolled on collision.
6. **Transcript schema**: header line `{"schema": "acpc.transcript/1"}`; events carry a global 1-based index `i`; consumers ignore unknown fields.
