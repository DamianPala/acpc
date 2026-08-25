# Agents

## Ground rules

- `SPEC.md` (repo root) = normative contract. Behavior change land in spec first, same change as code. Code/docs/spec conflict → spec win.
- **Pushing is the user's act.** Origin lives at github.com/DamianPala/acpc and changes land through PRs, but every push, PR merge and tag push happens only on Damian's explicit go in the moment — never on an agent's own judgement. Releases and PyPI stay off-limits to agents entirely.
- **Installed `acpc` = working tool** — non-editable snapshot on purpose: acpc dispatch agents that edit this repo, editable install would change tool under them mid-task. Develop with `uv run acpc ...`; after change land on main, reinstall deliberate with `uv tool install --force "git+file:///home/haz/ai/lab/projects/acpc@main"` — never `-e`.
- All tests + dev runs use isolated state root (temp dir via env override). Never touch `~/.acpc`.

## Testing

- **Test behavior, not implementation.** Assert observable contract: stdout/stderr content + separation, exit codes, files + permissions in session dir, state transitions in `meta.json`, NDJSON events. Never assert internal call order, private helpers, log wording, module structure. Refactor break tests but behavior same → tests were wrong. Fix tests, not refactor.
- **Test edges + errors, not just happy path:** empty/missing input, `kill -9` mid-turn (orphan detection), truncated last NDJSON line, concurrent access under session lock, non-TTY vs TTY, oversized output at truncation boundary, unknown permission kinds.
- **Mock only boundaries.** Agent process = boundary — use ACP mock agent in `tests/`, speaks real protocol over stdio. Never mock acpc own modules to make test pass; test that stubs logic under test proves nothing.
- **Verify test can fail.** Before trusting new test, break covered code, confirm red. Lesson from 2026-08-04: whole daemon test group stayed green while silently exercising wrong source tree; only mutation testing exposed it.
- Every test deterministic + self-contained: own temp state root, no wall-clock timing tighter than spec's own timeouts, no ordering dependence between tests.

## Dispatching agents

Implementation work dispatched to agent → installed `acpc` → `builder` registry variant — gpt-5.6-luna @ xhigh, `permissions all` (agent-full-access; sandbox seccomp blocks test suite), already set up, works:

```bash
acpc run builder --prompt-file <task-prompt.md> --cwd <this worktree>
```

- **Reviews = registry `reviewer` variant** via `acpc run reviewer --prompt-file <prompt>` (convention since 2026-08-10). Same isolated-root dispatch shape as builder.
- Specs + long prompts via `--prompt-file`, never positional arg. Argv visible in `ps`/`/proc/*/cmdline`; on 2026-08-04 dispatch got SIGKILLed — spec text in argv matched cleanup helper's `pgrep -f` pattern.
- Select provider/home by naming registry variant; never export `CODEX_HOME`/`CLAUDE_CONFIG_DIR` around dispatch. Daemons keyed on entry's declared environment, not ambient env — exporting home binds wrong provider into shared daemon.

## Docs contract

Update in same change as code, never after:

- `SPEC.md` — any behavior change. Normative; dispatched implementers never edit.
- `ARCHITECTURE.md` — any structural change (modules, seams, ownership).
- `README.md` + `--help` text — any user-facing surface change.
- `status.md` — breakpoints, decisions, backlog.

Dispatched implementers get target doc wording verbatim in spec; orchestrating agent authors doc text, folds into implementation commit.

## Commit rules

- `docs/plans/` = local-only working docs. Never commit files from there.
- Never commit run artifacts (test-run reports, gate logs, session evidence) — park in `docs/plans/<version>/`.
- No AI attribution trailers (Co-Authored-By, AI-assistant, etc.)
- Conventional Commits format, imperative mood

## Project

- Python >= 3.13, cross-platform (Linux, macOS, Windows)
- Toolchain: uv (build, deps, run); `src/` layout, `uv_build` backend
- Lint/format: `uv run ruff check && uv run ruff format`
- Type check: `uv run pyright`
- Tests: `uv run pytest`
- Acceptance: `smoke.sh` (end-to-end against ACP mock agent)