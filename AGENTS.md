# Agents

## Ground rules

- `SPEC.md` (repo root) is the normative contract. Behavior changes land in the spec first, in the same change as the code. On any conflict between code, docs and spec, the spec wins.
- **Local-only.** Never push to origin, never release, never touch PyPI. Origin is frozen deliberately.
- **The installed `acpc` is the working tool** — a non-editable snapshot on purpose: acpc dispatches the agents that edit this repo, so an editable install would change the tool under them mid-task. Develop with `uv run acpc ...`; after a change lands on main, reinstall deliberately with `uv tool install --force "git+file:///home/haz/ai/lab/projects/acpc@main"` — never `-e`.
- All tests and dev runs use an isolated state root (temp dir via env override). Never touch `~/.acpc`.

## Testing

- **Test behavior, not implementation.** Assert on the observable contract: stdout/stderr content and separation, exit codes, files and permissions in the session dir, state transitions in `meta.json`, NDJSON events. Never assert on internal call order, private helpers, log wording, or module structure. If a refactor breaks tests without changing observable behavior, the tests were wrong — fix the tests, not the refactor.
- **Test edges and errors, not just the happy path:** empty/missing input, `kill -9` mid-turn (orphan detection), truncated last NDJSON line, concurrent access under the session lock, non-TTY vs TTY, oversized output at the truncation boundary, unknown permission kinds.
- **Mock only boundaries.** The agent process is the boundary — use the ACP mock agent in `tests/`, which speaks the real protocol over stdio. Never mock acpc's own modules to make a test pass; a test that stubs out the logic under test proves nothing.
- **Verify a test can fail.** Before trusting a new test, break the code it covers and confirm it goes red. Lesson from 2026-08-04: an entire daemon test group stayed green while silently exercising the wrong source tree; only mutation testing exposed it.
- Every test must be deterministic and self-contained: own temp state root, no dependence on wall-clock timing tighter than the spec's own timeouts, no ordering dependence between tests.

## Dispatching agents

Implementation work dispatched to an agent goes through the installed `acpc` to the `builder` registry variant — gpt-5.6-luna @ xhigh, `permissions write`, OpenRouter home, already set up and working:

```bash
set -a && . ~/.config/secrets/base.env && set +a   # OpenRouter key via env_key
acpc run builder --prompt-file <task-prompt.md> --cwd <this worktree>
```

- **Reviews are Opus** — the orchestrator itself or an Opus subagent, never the registry's `reviewer` variant (that one is a different stack).
- Pass specs and long prompts with `--prompt-file`, never as a positional argument. Argv is visible in `ps`/`/proc/*/cmdline`; on 2026-08-04 a dispatch was SIGKILLed because the spec text in argv matched a cleanup helper's `pgrep -f` pattern.
- Select provider/home by naming a registry variant; never export `CODEX_HOME`/`CLAUDE_CONFIG_DIR` around a dispatch. Daemons are keyed on the entry's declared environment, not ambient env — exporting the home binds the wrong provider into a shared daemon.

## Docs contract

Updated in the same change as the code, never after the fact:

- `SPEC.md` — any behavior change. Normative; never edited by dispatched implementers.
- `ARCHITECTURE.md` — any structural change (modules, seams, ownership).
- `README.md` + `--help` text — any user-facing surface change.
- `status.md` — breakpoints, decisions, backlog.

Dispatched implementers get target doc wording verbatim in their spec; the orchestrating agent authors the doc text and folds it into the implementation commit.

## Commit rules

- `docs/plans/` is local-only working documentation. Never commit files from this directory.
- No AI attribution trailers (Co-Authored-By, AI-assistant, etc.)
- Conventional Commits format, imperative mood

## Project

- Python >= 3.13, cross-platform (Linux, macOS, Windows)
- Toolchain: uv (build, deps, run); `src/` layout, `uv_build` backend
- Lint/format: `uv run ruff check && uv run ruff format`
- Type check: `uv run pyright`
- Tests: `uv run pytest`
- Acceptance: `smoke.sh` (end-to-end against the ACP mock agent)
