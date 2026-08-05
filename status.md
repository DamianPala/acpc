# Status — acpc 0.3 rewrite (worktree `rewrite/0.3`)

## Now

Stage 2, serial tail. S01–S06 are landed on `rewrite/0.3` and gated green: 303 tests, ruff + pyright clean, `./smoke.sh` **156/156** with `S06-run` flipped to ready. Next up: S08 (status + log views).

## Done

- **Stage 2 wave 1 (2026-08-05):**
  - **S02 — session store** (`11f6151`, Opus, tier max): `src/acpc/sessions.py` + 81 tests. Session dirs, `meta.json` under a per-session file lock, pinned id alphabet with collision re-roll via `mkdir` as the claim, turn rotation, name aliases with rebind/hard-error semantics, rm/prune primitives. Orphan detection persists on read and writes the placeholder `answer.md`; the 30 s grace covers only the window before a pid is recorded. Mutation-checked: disabling liveness verification (12 tests red), making the lock a no-op (1), gating orphan detection behind the grace (10), letting rotation clobber an earlier turn (1 — that guard was untested until a test was added to reach it).
  - **S01 — config + registry** (`35a2fab`, Luna, 2 rounds): strict `config.toml` loader, `parse_duration` + `retention_seconds`/`daemon_ttl_seconds`, registry with nearest-wins inheritance, cycle/missing-base diagnostics, presets, effort validation, install status/executor, and the three shipped adapter TOMLs. Vendor facts copied verbatim from the donor; presets match SPEC's `agents --models` examples exactly; gemini ships presetless; `TODO(stage3)` on every guessed value.
  - **S03 — transcript** (`b26be12`, Luna, 2 rounds): `Transcript` with `append`/`read`, versioned header, global index, torn-tail tolerance and repair, `since`/`tail` selection with `next_cursor`.
  - **Review rounds:** both Luna slices were sent back once, for the same two problems. (1) Speculative API surface — S01 shipped 8 unused aliases/entry points, S03 shipped 9 names for 4 operations; the ground rules forbid this and both were cut to one name per operation. (2) A real defect each: S01 typed provenance as `Mapping[str, Path]` and used a fake `Path("<call>")` to mean "came from a flag" (replaced with `FieldSource(kind, path)`, which is what `--dry-run` and `agents <name>` actually need); S03's `append` re-read and re-parsed the whole transcript per event (O(N²) on the streaming path — restructured to scan once at open and track the index). Both fixed in round 2; no escalation to `--effort max` was needed.
  - One reviewer fix applied directly rather than spending a third round: S01's duration error said "must be greater than zero" for any malformed value; it now names the actual value and the accepted syntax.

- **Stage 2 wave 2 + S06 (2026-08-05):**
  - **S04 — ACP client** and **S05 — output/render** landed (both Luna, 2 rounds each). S04's round-2 fix was a vacuous test: `assert "thought" not in client.answer` could never fail because the frozen mock never emits `AgentThoughtChunk`; the client is now driven with a real thought chunk instead (2 tests).
  - **S06 — runner + `run` verb** (Opus, tier max): `runner.py`, `cli.py` (`run` only), `daemon_client.py` and `cache.py` as the sanctioned stubs, plus `tests/test_runner.py` + `tests/test_cli_run.py` (65 tests). Full sync path against the mock: resolve → session create → spawn → client → finalize; exit codes 0/1/2/124/130/143; `--timeout` cancels into state `timeout`; SIGINT/SIGTERM go through ACP `session/cancel` with the bounded ack wait; the direct-child fallback note rides the single `--` summary line. Mutation-checked 8/8 caught (timeout exit code, SIGTERM state, answer finalization, bypass guard, non-TTY permission default, `set_session_mode`, route note, prompt-source count).
  - S06 found a defect in already-landed S01: with no `--model`, the model resolved to `None` because the `standard` preset was not treated as the adapter default. The whole suite passed before *and* after, which is what proved nothing covered it. Fixed in place with 4 tests.
  - `exit_code_for` had no case for `terminated` (SIGTERM on the direct path), so it returned 1 instead of 143. Caught by a new test, fixed.

- **Stage 2 prep (2026-08-05):**
  - Fresh Opus review of PLAN/HANDOFF/AGENTS vs SPEC/ARCHITECTURE. All findings fixed: smoke gates now passable in dispatch order (S08 snippet assertion via `--all`, long-lived prelude guarded on S09/S11 too, orphan kill isolated on the `loner` target), HANDOFF uses the dev1 CLI's real verbs (`run -s`, `status -s --tail`; no `continue`/`log`).
  - PLAN.md precision pass: `meta.pid` semantics pinned (turn-hosting process; daemon kill orphans its whole target), transcript per-type fields pinned, `command` = shlex-split string, S06 ships `daemon_client` + `cache` stubs (sanctioned takeovers by S07/S10), S02 grace only until pid recorded + orphan placeholder `answer.md`, S04 captures advertised data, S05 owns footer variants, S07 builds the cancel transport for S11's `stop`, S01 owns the `install` executor, gemini ships presetless with `TODO(stage3)` on guessed vendor facts, `--json` added to S06/S07/S10/S11 DoDs, injectable time sources + smoke-section reading in ground rules.
  - Tier ladder final: Luna (`builder` @ xhigh) → `builder --effort max` → Opus takeover; tier-max slices (S02/S06/S07) and all reviews are Opus. Recorded in AGENTS.md + PLAN.md.
  - Cosmetics: SPEC daemon log name `<entry>~<hash>` (matches frozen `targets.py`), `help.py` noted in ARCHITECTURE.
  - Verified after edits: shellcheck clean, `./smoke.sh` exit 0 all sections pending.

- **Stage 1 (2026-08-05, Fable):**
  - Scaffold: fresh `uv_build` package, version `0.3.0.dev2` (`uv run acpc -V`), click entry point, dev group (ruff/pyright/pytest/timeout/xdist). `platformdirs` and `pytest-cov` dropped; no new deps.
  - `ARCHITECTURE.md`: module map derived from SPEC.md, layer seams, 6 pinned key decisions (entry schema, daemon dir, id alphabet, transcript header, …).
  - Harvested foundation (frozen for Stage 2): `vocab.py`, `paths.py`, `proc.py`, `spawn.py`, `environment.py`, `permissions.py`, `ipc.py`, `targets.py` + adapted tests. Full harvest log with take/trim/rewrite/drop rationale in PLAN.md.
  - Test harness: `tests/mock_agent.py` speaks **real ACP over stdio** (donor mock extended with MVP scenario keywords `fail`/`perm`/`huge`/`slow`, advertised modes incl. bypass `yolo`, models, efforts with unsupported-level rejection, commands). `tests/test_mock_agent.py` proves initialize → prompt → answer round-trips through the harvested spawn + permission policy.
  - `smoke.sh` ported from the MVP, every assertion re-derived from current SPEC.md (stderr footers, `--tail`, two-level help, …), sectioned by PLAN slices with pending gates. Runs now: all sections pending, exit 0.
  - `PLAN.md`: 12 slices (S01–S12), tiers (S02/S06/S07 = max), DoD + smoke mapping + wire contracts + dispatch order + gate rule + spec gaps.
  - Acceptance: `uv run pytest` 96 passed · ruff clean · pyright clean · shellcheck clean.
  - Testing-rules audit (AGENTS.md *Testing*): replaced private-state assertions in `tests/test_ipc.py` with observable ones (peer-visible EOF, `/proc/self/fd` descriptor accounting, real >10 MiB frame instead of a patched limit, long `ACPC_HOME` instead of a patched path limit). Mutation-checked all foundation tests: 7/9 mutations red; 2 survivors are equivalent mutants (cleanup's socket unlink — asyncio ≥3.13 removes it on server close; spawn's stream limit — acp ≥0.11 reassembles oversized lines itself, comment updated). Documented exceptions: `test_proc` kernel-call ordering test (TOCTOU contract at the OS boundary), `test_ipc` delay-wrap of the connection callback (scheduling instrumentation, real callback still runs).

## Next

- Stage 2: wave 2 (S04 ∥ S05) through the gate, then the serial tail S06 → S08 → S09 → S07 → S10 → S11 → S12. S06 and S07 are Opus (tier max).
- Stage 3: Fable — full smoke green, real-agent test, final review, landing (reset main, tag, reinstall, drop archive branch; nothing pushed).

## Decisions

- 2026-08-05: `requires-python >= 3.13` kept from the donor (harvested `ipc.py` uses `asyncio.Server.close_clients`, a 3.13+ API); skill's 3.12 floor waived deliberately.
- 2026-08-05: daemon sockets/locks live in `daemon/` next to the per-target logs (spec names only the logs).
- 2026-08-05: adapter definitions deliver `home` via a `home_env` field (delivery mechanism, not spec surface).
- 2026-08-05: S07 (daemon) dispatches after S08/S09 — riskiest slice last among the core, everything else works on the direct path. **Superseded below.**
- 2026-08-05: **serial tail reordered to S06 → S08 → S07 → S09 → S10 → S11 → S12.** PLAN claimed S09 was "testable with daemon stopped"; it is not. Probed directly: after `continue` rotates the turn, the direct path spawns a *fresh* adapter process, `session/load` succeeds, and the mock answers "(turn 1)" with no reference to the earlier prompt — its history lives in process memory, which is the honest behavior for an adapter whose process is gone. smoke's `continue's answer references the earlier turn` therefore only passes on the warm path, where the adapter still holds the session and the runner prompts it without re-loading. Every dependency PLAN states is still satisfied by the new order; only the "riskiest last" preference is given up, and the claim that justified it is false.
- 2026-08-05: field provenance is `FieldSource(kind, path)` with `kind` ∈ `entry | adapter-default | call | default | unset`, not a bare file path. SPEC's own `agents <name>` examples label sources that have no file behind them (`(adapter default)`, `(default)`, `(unset)`) and `--dry-run` must name call-site flags, so a `Path` cannot carry the contract. Views render the label; the registry only supplies the data.
- 2026-08-05: the transcript assumes a single writing process per session, which the per-session lock in `sessions.py` already guarantees (ARCHITECTURE decision 4). That is what lets `append` track the index in memory instead of re-parsing the file per event; readers still parse from disk, so another process's writes are always visible.
- 2026-08-05: slices land on `rewrite/0.3` by cherry-pick, not merge — one commit per slice, no merge commits (the repo's commitlint hook rejects merge subjects anyway).
- 2026-08-05: **four edits to frozen files** (`smoke.sh`, `tests/mock_agent.py`), taken under the handoff's "orchestrator decides if the fix is trivially obvious" clause. All four are harness plumbing: none changes an assertion, an expected value, or any SPEC behavior — each one only lets an assertion actually reach the code it claims to test. **Flag for Stage 3 review.**
  1. `smoke.sh`: `export SCRIPT_DIR`. The existing `export -f acpc` is plainly meant to make the `acpc` shell function usable inside `bash -c`, but the function body expands `$SCRIPT_DIR`, which was never exported — so both `bash -c` probes ran `uv run --project "" acpc` and died with exit 2 no matter what the code did.
  2. `smoke.sh`: the stdin probe was `printf … | run_acpc run mock - --quiet`. A pipeline runs `run_acpc` in a subshell, so its `LAST_RC`/`LAST_OUT` never came back and the assertion read the *previous* command's exit 2. Changed to a file redirect; the bytes on stdin are identical.
  3. `smoke.sh`: the SIGINT probe now `exec`s. `bash -c` cannot exec-optimize away when `acpc` is a function, so `kill -INT $!` hit the wrapper shell and never reached acpc. Verified separately that acpc exits 130 under SIGINT delivered directly and via `exec`.
  4. `tests/mock_agent.py`: `slow:`/`chunkslow:` parsed the whole tail as an int, but smoke sends `slow:30 run-timeout probe` (descriptive text so concurrent probes are distinguishable in `status`). Added `_leading_delay` to read only the first token.

