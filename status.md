# Status — acpc 0.3 rewrite (worktree `rewrite/0.3`)

## Now

Stage 1 (foundation) complete, uncommitted, awaiting review. Stage 2 (Opus + Luna implement PLAN.md slices) is next.

## Done

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

- Stage 2: fresh Opus session with SPEC.md + PLAN.md (+ ARCHITECTURE.md); dispatch S01 per the plan's order and gate rule.
- Stage 3: Fable — full smoke green, real-agent test, final review, landing (reset main, tag, reinstall, drop archive branch; nothing pushed).

## Decisions

- 2026-08-05: `requires-python >= 3.13` kept from the donor (harvested `ipc.py` uses `asyncio.Server.close_clients`, a 3.13+ API); skill's 3.12 floor waived deliberately.
- 2026-08-05: daemon sockets/locks live in `daemon/` next to the per-target logs (spec names only the logs).
- 2026-08-05: adapter definitions deliver `home` via a `home_env` field (delivery mechanism, not spec surface).
- 2026-08-05: S07 (daemon) dispatches after S08/S09 — riskiest slice last among the core, everything else works on the direct path.
