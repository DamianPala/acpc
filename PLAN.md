# PLAN.md — Stage 2 implementation contract

SPEC.md is normative; ARCHITECTURE.md fixes the module map and key decisions. This file cuts the work into slices for Stage 2 (Opus orchestrates, Luna implements via the installed `acpc`). Every slice is dispatched with a self-contained prompt built from its entry here.

## Ground rules (every slice, non-negotiable)

- **Forbidden for implementer agents, always:**
  - The frozen foundation files (below) — read them, never edit them.
  - Other slices' modules (each slice's "Modules" list is exclusive).
  - The donor repo `/home/haz/ai/lab/projects/acpc` — read-only reference at most.
  - `~/.acpc` and any real user state. All tests run against a temp `ACPC_HOME`.
  - Global installs (`uv tool install`), pushes, releases. Run via `uv run acpc`.
- **Green bar:** `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright` — all clean before a slice is submitted for review.
- Tests follow the repo's style: behavior-focused, mock only boundaries, temp `ACPC_HOME` per test (`monkeypatch.setenv("ACPC_HOME", ...)`).

### Frozen foundation (Stage 1 harvests — read-only)

```
src/acpc/vocab.py         src/acpc/paths.py        src/acpc/proc.py
src/acpc/spawn.py         src/acpc/environment.py  src/acpc/permissions.py
src/acpc/ipc.py           src/acpc/targets.py
tests/mock_agent.py       tests/test_mock_agent.py
tests/test_ipc.py         tests/test_proc.py       tests/test_paths.py
tests/test_permissions.py tests/test_environment.py tests/test_targets.py
smoke.sh                  (except flipping SECTION_READY values as slices land)
```

If a frozen file turns out to be wrong, the slice **stops and reports**; the orchestrator decides (that is a Stage 2 review event, not a slice-local edit).

## Harvest log (Stage 1 decisions)

| Donor code | Decision | Where it went | Why |
|------------|----------|---------------|-----|
| `ipc.py` (NDJSON-over-UDS transport, role claiming, oversized-frame drain, socket-path length handling) | **take** (trim: paths + error text) | `src/acpc/ipc.py` | Battle-tested transport, spec-neutral. Only repointed to `daemon/` under `ACPC_HOME` and updated error guidance. Tests came along (`test_ipc.py`). |
| `runner.py` spawn block (`_spawn_agent`, 10 MB stream limit, teardown ladder, stderr forwarding) | **trim** | `src/acpc/spawn.py` | The 10 MB limit and reaped-leader guard are hard-won. Trimmed: caller now passes the complete env explicitly, decoupling spawn from entry resolution. |
| `runner.py` process-tree kill (`kill_process_tree`, pidfd group signalling, identity checks) + `sessions.py` process identity/liveness (`process_start_time`, `_process_liveness`) | **take** (merged) | `src/acpc/proc.py` | PID-reuse-safe identity + kill; exactly what SPEC's "liveness is verified wherever state is read" needs. Donor tests ported (`test_proc.py`). |
| `sessions.py` `_atomic_write` | **take** | `src/acpc/paths.py` | The no-torn-reads primitive (mkstemp 0600 + replace; `exclusive` via link). |
| `sessions.py` everything else (sessions.json registry, PPID-scoped "last", output dir, metadata files) | **drop** | — | Built for the 0.2 state model. SPEC 0.3 replaced it wholesale with per-session dirs + `meta.json`; nothing to salvage. |
| `client.py` permission machinery (`READ/WRITE/DELETE_KINDS`, `_classify_kind`, `_should_allow`, `_find_option`, `_minimum_policy`) | **take** (extended) | `src/acpc/permissions.py` | Matches SPEC's permission table verbatim. Extended with the bypass-mode `switch_mode` guard (spec: treated like an unknown kind). Fresh focused tests (donor's were entangled with OutputHandler). |
| `client.py` rest (AcpcClient, MilestoneTracker, snapshots) | **rewrite** in S04 | — | Coupled to the 0.2 output/status model (milestone ring, stream files). The 0.3 client writes a transcript instead; cheaper to redo against SPEC. |
| `environment.py` (base allowlist env, capability vars, TERM pin, overrides) | **trim** | `src/acpc/environment.py` | Core matches SPEC's constructed-env contract. Trimmed: the three `ACPC_*_DIR` directory vars collapsed to resolved `ACPC_HOME`; registry-coupled failure hints dropped (S01 may rebuild on the new registry if wanted). |
| `daemon_client.py` `target_for_environment` | **trim/rewrite** (small) | `src/acpc/targets.py` | Keying idea and path-safety kept; the env-template machinery (`${effort}` placeholders, `home_env` ambience) died with the 0.2 entry model. New signature: entry + home + declared env + call-time passthrough values, hashed, never stored. |
| `daemon.py`, `daemon_client.py` (rest) | **rewrite** in S07 | — | Donor daemon protocol carries 0.2 concepts (memory capacity management, model probes, stream files) the spec dropped; the 0.3 daemon is simpler (warm adapters + turn slots + TTL). Reuse ideas, not code. |
| `agents.py` (registry, inheritance resolution, provenance) | **rewrite** in S01 | — | The resolution *shape* (nearest-wins, cycle detection, `field_sources` provenance) is right and S01 should follow it, but the schema changed too much to edit in place: `identity` field → filename, `home_env` indirection → `home` + delivery field, no `[presets]`/`bypass_modes`/`efforts` in donor TOMLs, platformdirs → `ACPC_HOME`. |
| `models_cache.py` | **rewrite** in S10 | — | Donor cache is per-(target, cwd-hash) with eviction and reverse-model hints; SPEC 0.3 wants a simple per-adapter `cache/<agent>/` refreshed on every run. Keep only the atomic-write + lock pattern (already in foundation). |
| `output.py`, `status.py`, `presets.py`, `cli.py` | **rewrite** in S05/S08/S01/S12 | — | All shaped by the 0.2 surface (stream files, status snapshots, config-file presets, old verb set). The 0.3 contracts are different enough that porting would cost more than writing. |
| `tests/mock_agent.py` | **take** (extended) | `tests/mock_agent.py` | Already a real ACP agent (the MVP's mock was not). Extended with the MVP's scenario keywords (`fail`/`perm`/`huge`/`slow`), the advertised dataset (modes incl. bypass `yolo`, models, efforts with rejection of unsupported levels, commands), usage updates, and history-aware default answers. |
| MVP `smoke.sh` | **take** (re-derived) | `smoke.sh` | Structure, helpers and scenario flow kept; every assertion re-derived from current SPEC (log footers → stderr, `--last` → `--tail`, two-level help, `agents --check` semantics, permission tier details). Sectioned by slice with pending gates. |
| MVP production internals (`worker.py`, `session.py`, `render.py`, …) | **drop** | — | Deliberately oversimplified and written to an older frozen spec (per HANDOFF). |

Dependency note: `platformdirs` (donor dependency) dropped — SPEC 0.3 pins the state root to `~/.acpc`/`ACPC_HOME`. `pytest-cov` dropped from the dev group (nothing consumes coverage in this pipeline). No new dependencies added.

## Wire contracts (pinned — smoke.sh asserts these)

Slices must implement these shapes exactly; they are what makes independently built slices compose.

- **Session id**: 4 chars from `abcdefghijkmnpqrstuvwxyz23456789`, re-rolled on collision.
- **`meta.json`** (S02 owns; written atomically, always parseable): at minimum
  `session_id`, `name`, `entry`, `base_adapter`, `state` (vocab.SESSION_STATES verbatim),
  `pid`, `process_start_time`, `created_at`/`started_at`/`finished_at` (epoch seconds, null until known),
  `turns` (int), `exit_code`, `stop_reason`, `tokens`, `cost`, `prompt_snippet`,
  `resolution` (model/effort/permissions/home/env + per-field source), `adapter_session_id`, `target`.
- **`transcript.ndjson`** (S03 owns): line 1 header `{"schema": "acpc.transcript/1"}` (no `i`);
  every event is one line with a global 1-based index `i` (continuous across turns), `ts`,
  `type` ∈ `msg | thought | tool | permission | error | state | usage`, type-specific fields.
  Consumers ignore unknown fields.
- **`run`/`continue`/`wait` `--json` envelope** (S05/S06): `state`, `session_id`, `stop_reason`,
  `paths` (dir/prompt/transcript/answer), `cost`, `answer`, `truncated` (bool). `--bg`: only
  `session_id`, `state`, `paths`. `-o`: adds `output_file`, omits `answer`.
- **`status --json`**: with id → one object incl. `session_id`, `state`, `pid`, `turns`; without →
  `{"sessions": [...]}` with `session_id`, `entry`, `state`, `name`, `prompt_snippet`, `runtime`.
- **`log --json`**: raw transcript events (with `i`) on stdout; footer stays on stderr.
- **Footers/summaries**: stderr + `--` prefix for `run` summary and `log` footers; part of the
  stdout view for `status`/`agents`. Segments `|`, peers `·`.
- **Entry TOML schema**: per ARCHITECTURE.md Key decision 2 (smoke.sh's setup writes
  `mock.toml`/`builder.toml`/`explorer.toml`/`phantom.toml` in exactly that schema).
- **Adapter env delivery**: resolved `home` is exported to the adapter as the definition's
  `home_env` variable; declared `[env]` + passthrough go through `environment.adapter_environment`.

## Slices

Tier `fast` = Luna (`acpc run codex --model fast` equivalent per Stage 2 setup); `max` from the first dispatch for the marked slices — subtle-race territory where post-hoc review is weak.

### S01 — config + registry (tier: fast)

- **Scope:** SPEC *Agent variants*, *State on disk* (config.toml, adapter definitions), `agents init` scaffolding data model. Strict config loader per the python-app skill (unknown key = hard error; the file has exactly 3 keys: `retention`, `daemon_ttl`, `daemon_max_concurrent`).
- **Modules:** `src/acpc/config.py`, `src/acpc/registry.py`, `src/acpc/data/agents/{claude,codex,gemini}.toml`, `tests/test_config.py`, `tests/test_registry.py`.
- **DoD:** entries resolve with nearest-wins inheritance, cycle + missing-base diagnostics, per-field provenance (field → defining file) exposed for `agents <name>` and `--dry-run`; presets resolve `--model fast|standard|max` with `--effort` override; effort values validated against the adapter's `efforts` (error lists supported levels); bypass-mode list readable; user file under an adapter name overrides fields; new adapter = `command` without `extends`; install status via `shutil.which` on the command head. Unit tests cover: inheritance, provenance, override/new-adapter/variant trichotomy, preset overrides via `[presets]`, malformed TOML → clean error.
- **Forbidden beyond ground rules:** nothing extra.
- **Dependencies:** none.

### S02 — session store (tier: **max**)

- **Scope:** SPEC *State on disk* (session dirs, meta, rotation, no torn reads, owner-only), *Session states* (verified liveness, persisted transitions, 30s grace, who-accepts-what), `rm`/`prune` primitives (not the CLI verbs), `--name` aliases (`last` resolution TTY-gated at CLI level, storage here).
- **Modules:** `src/acpc/sessions.py`, `tests/test_sessions.py`.
- **DoD:** id allocation (pinned alphabet, collision re-roll); create/read/update `meta.json` per the wire contract (atomic, 0600, under a per-session lock); state transitions enforced (vocab verbatim); liveness check via `proc.process_liveness` with start-time token; orphan detection persisted on read, 30s startup grace; turn rotation renames `prompt.md`/`answer.md` → `.<n>` exactly once per file at next-turn start; name → id resolution with rebind-warning/hard-error-on-running semantics; delete/prune primitives (age from `finished_at`, running never touched). Mutation-tested: killing the process behind a `running` session must flip every reader to `orphaned`.
- **Dependencies:** none (uses frozen `paths`/`proc`/`vocab`).

### S03 — transcript (tier: fast)

- **Scope:** SPEC *State on disk* (transcript is a public versioned format), `log` cursor semantics (global index, stateless, continuous across turns).
- **Modules:** `src/acpc/transcript.py`, `tests/test_transcript.py`.
- **DoD:** append whole lines only (header on create); events carry `i`/`ts`/`type` per the wire contract; reader supports `since`/`tail` selection returning events + next cursor; a truncated trailing line (crash artifact) is ignored, never a crash; concurrent appends from one process are serialized. Tests include the torn-tail case and cursor continuity across simulated turns.
- **Dependencies:** none.

### S04 — ACP client (tier: fast)

- **Scope:** SPEC *Output contract* ("the answer" definition), *Permissions* (runtime answering incl. bypass-mode switch guard), `log` event source. The ACP `Client` implementation the runner and daemon share.
- **Modules:** `src/acpc/client.py`, `tests/test_client.py` (drive it against `tests/mock_agent.py` via frozen `spawn`, like `tests/test_mock_agent.py` does).
- **DoD:** session updates → transcript events (msg/thought/tool/permission/error/usage) with correct condensable payloads; answer assembly = agent-message chunks concatenated in stream order (thoughts/tool output excluded, interleaved narration kept); `request_permission` answered via frozen `permissions` with the adapter's bypass list applied to `switch_mode` targets; denials recorded as transcript `permission`+`error` events; tokens/cost accumulated from usage updates; never prompts on stdin (the `prompt` policy's TTY ask is a callback the CLI injects — non-TTY callers reject `prompt` before this layer).
- **Dependencies:** S03.

### S05 — output + render (tier: fast)

- **Scope:** SPEC *Output contract* (stdout discipline, stderr summary, `--json` shapes, `--max-output`), `log`/`status` view rendering.
- **Modules:** `src/acpc/output.py`, `src/acpc/render.py`, `tests/test_output.py`, `tests/test_render.py`.
- **DoD:** truncation keeps the head, cuts on a UTF-8 boundary, appends the marker naming the answer path (test with the mock's straddling emoji: cap 2000 must not split it into invalid bytes); `log` truncation at event granularity with the cursor covering only printed events, `--json` truncation as a typed `truncated` event; footers per the pinned stream rules; JSON envelopes per the wire contract; condensed event lines (tool + args summary + status + duration; msg 200-char snippet + length; errors never filtered, never truncated except by the budget); `--prose` renders messages untruncated with error lines kept.
- **Dependencies:** S03 (event shapes).

### S06 — runner: sync run on the direct path (tier: **max**)

- **Scope:** SPEC `run` (sync semantics, all flags except `--bg`), *Output contract* (exit codes, SIGINT/SIGTERM behavior on the direct path), `wait`-style finalization. The daemon seam is stubbed: routing tries `daemon_client.ensure()` if present, else direct child — in this slice, always direct, with the visible fallback note on stderr.
- **Modules:** `src/acpc/runner.py`, `src/acpc/cli.py` (the `run` verb wiring only), `tests/test_runner.py`, `tests/test_cli_run.py`.
- **DoD:** full sync path against the mock: resolve (S01) → session create (S02) → spawn (frozen) → client (S04) → answer/meta finalization → output (S05); exit codes 0/1/2/124/130 + 141 exact; `--timeout` cancels (state `timeout`); SIGINT cancels gracefully (ACP `session/cancel`, bounded ack wait); SIGTERM on the direct path cancels too (detach is S07); prompt source rules (exactly one of arg/`-`/`--prompt-file`); `--dry-run` prints the resolution with provenance and touches no state; `--mode` bypass guard (reject at parse unless `--permissions all`); non-TTY permission default `read`, `prompt` rejected non-TTY (exit 2); auto-prune hook (opportunistic, from config retention). **Smoke:** flip `S06-run` — the section must pass.
- **Dependencies:** S01–S05.

### S07 — daemon: warm adapters, --bg, detach (tier: **max**)

- **Scope:** SPEC `daemon` (auto-start, TTL, `daemon_max_concurrent` + queueing note, per-target logs, stop-fails-sessions-never-orphans, version-skew restart, fallback), `run --bg`, `wait`, SIGTERM detach, `daemon status|stop`.
- **Modules:** `src/acpc/daemon.py`, `src/acpc/daemon_client.py`, `src/acpc/cli.py` (`wait`, `daemon` verbs + `--bg` wiring), `tests/test_daemon.py`, `tests/test_daemon_client.py`.
- **DoD:** daemon spawn race-safe (lock via frozen `ipc.lock_path_for_target`); one daemon per target serves multiple sessions; turn slots per `daemon_max_concurrent`, queueing noted on stderr; idle TTL expiry (idle = no active sessions; detached sessions keep it alive); `daemon stop` transitions active sessions to `failed` with reason in meta; version skew self-restarts on connect; `--bg` returns id + dir immediately, session runs under the daemon; SIGTERM on a daemon-routed client detaches (client exits 143, session keeps running, id printed to stderr); `wait` blocks/returns per spec (timeout 124 leaves the session running; mirrors the session result incl. 130 for cancelled); adapter stderr goes to the per-target `daemon/<target>.log`. Daemon-side coverage must be proven by mutation (break daemon code, watch the test fail) — a client-view-only assertion does not count. **Smoke:** flip `S07-daemon-bg`.
- **Dependencies:** S06.

### S08 — status + log verbs (tier: fast)

- **Scope:** SPEC `status`, `log` (all flags), footer contracts.
- **Modules:** `src/acpc/cli.py` (`status`, `log` verbs), `tests/test_cli_views.py`.
- **DoD:** `status` list (running + 5 recent, `--all`, one line per session with id/entry/state/runtime/name/snippet) and detail views; every state read verifies liveness (S02 API); `log` default/`--since`/`--tail`/`--prose`/`--json`/`--wait-new [--timeout]` (124 on expiry)/`--max-output`/`--quiet`; footers on stderr with cursor; finished-footer variant (exit code, tokens, answer path); failed/timeout/orphaned print the last agent message in full; `--prose --json` usage error. **Smoke:** flip `S08-views` and `S13-permissions` (the latter also exercises S04/S06 behavior; it flips here because this slice completes the observability it asserts through).
- **Dependencies:** S02, S03, S05 (+S06 landed so there are sessions to view).

### S09 — continue (tier: fast)

- **Scope:** SPEC `continue` (session/load resume, stored resolution, running = error, run-only-flag usage errors naming the rule).
- **Modules:** `src/acpc/cli.py` (`continue` verb), `src/acpc/runner.py` (continue path), `tests/test_cli_continue.py`.
- **DoD:** follow-up turn with context (mock's history-aware answer proves it); resume through `session/load` after daemon expiry (adapter without `loadSession` capability → actionable error); turn rotation at start-of-turn (S02 API); stored resolution used (editing the entry between turns changes nothing); `continue` on `running` → exit 2; run-only flags (`--permissions`, `--model`, …) → usage error naming the rule; cursor space continuous across turns. **Smoke:** flip `S09-continue`.
- **Dependencies:** S06 (+S07 for the warm-vs-load distinction; testable with daemon stopped).

### S10 — agents family + cache + install (tier: fast)

- **Scope:** SPEC `agents` (list/detail/`--models`/`--commands`/`--check`/`init`), `install`, advertised-data cache.
- **Modules:** `src/acpc/cache.py`, `src/acpc/cli.py` (`agents`, `install` verbs), `tests/test_cache.py`, `tests/test_cli_agents.py`.
- **DoD:** list view (aligned rows, variants indented with deltas, `missing → acpc install X`); detail views with provenance; variant view ends with the pointer, no catalog repeat; advertised data cached under `cache/<agent>/`, refreshed on every real run (S06 hook), auto-probed on cache miss; capped lists (first 3 + count) in detail view; `--models` full (presets + models), cross-agent overview; `--commands` first-sentence truncation + full text in `cache/<agent>/commands.md`; single cache-age footer exactly on the views that show advertised data; `--check` live probe (with name: one adapter incl. missing ones; without: every installed; any failure → exit 1); `agents init` scaffolds a variant TOML; `install` runs the definition's `install_command` (exit 1 on failure, 2 on unknown agent). **Smoke:** flip `S10-agents`.
- **Dependencies:** S01, S04 (probe), S06 (refresh hook).

### S11 — stop, rm, prune verbs (tier: fast)

- **Scope:** SPEC `stop` (graceful cancel, 10s ack bound, no-op on finished, `--force` reserved), `rm`, `prune` (+auto-prune already hooked in S06).
- **Modules:** `src/acpc/cli.py` (verbs), `tests/test_cli_maintenance.py`.
- **DoD:** per SPEC's who-accepts-what table exactly; `stop` on starting/running cancels via daemon (or process kill on direct/orphan edge), transcript+meta+partial answer preserved; `rm` errors on active; `prune --older-than/--dry-run`, age from `finished_at`. **Smoke:** flip `S11-maintenance`.
- **Dependencies:** S02, S06, S07.

### S12 — help, TTY rules, CLI hardening (tier: fast)

- **Scope:** SPEC *`--help` as first-contact documentation*, *TTY vs non-TTY*, error message contract ("one line and actionable"), hostile-input hardening.
- **Modules:** `src/acpc/cli.py` (final), `src/acpc/help.py` if the text warrants its own module, `tests/test_cli_help.py`.
- **DoD:** root cheat sheet ≤100 lines (examples grouped by task, write-task examples carry `--permissions write`, flag→ACP mapping table at the end); per-command full references; `stop`/`rm`/`install` print the root page; `-h`/`--help`/`-V`/`--version`; `last` TTY-only with a reasoned rejection; every usage error exit 2, no tracebacks anywhere (corrupt state → clean actionable errors); unknown flags name the flag. **Smoke:** flip `S12-cli` — and at this point **the whole suite must be green**.
- **Dependencies:** all previous.

## Dispatch order and gates

```
S01 → S02 → S03 → S04 → S05 → S06 → S08 → S09 → S07 → S10 → S11 → S12
```

(S07 after S09: the daemon slice is the riskiest; everything except bg/detach/wait works on the direct path, so the fast slices land and stabilize the surface first. S06 must build the routing seam so S07 plugs in without touching S06's files.)

**Gate rule (every slice):** Opus reviews the diff against SPEC.md + this file's DoD, runs `uv run pytest` + ruff + pyright, and runs the smoke sections the slice claims (plus all previously green sections — no regressions). Only then is the next slice dispatched. Two failed review rounds on one slice → that slice escalates to `max` tier for the rework dispatch.

**Slice prompts must include:** the slice entry from this file, the wire contracts section, the frozen-files list, and the ground rules. Nothing else from this file is needed in-context; SPEC.md and ARCHITECTURE.md ship whole.

## Spec gaps

The spec is decision-complete for behavior; these are the points where Stage 1 had to fix an implementation detail the spec deliberately or accidentally leaves open. **All six were folded back into SPEC.md on 2026-08-05** — the spec is the authority again; this list stays only as the record of where the decisions came from:

1. **How `home` reaches the adapter process.** SPEC defines `--home`/entry `home` but not the delivery mechanism. Decision: adapter definitions carry `home_env` (the vendor's env var, e.g. `CODEX_HOME`); resolution exports `home` as that variable. (ARCHITECTURE.md decision 2.)
2. **Daemon sockets/locks location.** SPEC's state-on-disk lists only `daemon/<entry>-<hash>.log`. Decision: sockets and locks live in the same `daemon/` dir (plumbing, not surface).
3. **Transcript event vocabulary.** SPEC pins the header/versioning/index semantics and the *views*, not the event field names. Pinned here under Wire contracts.
4. **`log --tail` with `--wait-new`.** Interplay unspecified. Decision: after waking, the normal selection (`--since`, then `--tail`) applies to the new events.
5. **Corrupt/unreadable state files.** SPEC fixes exit codes for usage errors and agent errors, not for damaged state. Decision: clean one-line actionable error, non-zero exit, never a traceback (smoke asserts non-zero + no traceback without pinning 1 vs 2).
6. **Cosmetic spec typo:** the `status kq8w` example showed `dir ~/.acpc/sessions/kq81` (id mismatch). Fixed in SPEC.md.

## Stage 1 status (for the record)

- Scaffold: `acpc 0.3.0.dev2`, `uv run acpc -V` works; ruff + pyright + pytest green (96 tests).
- Mock agent speaks real ACP: `tests/test_mock_agent.py` completes initialize → session/new → prompt → answer round-trips through the frozen spawn path, including permission traffic through the frozen policy.
- `smoke.sh` runs end to end with every section pending, exit 0.
