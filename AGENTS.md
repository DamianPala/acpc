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

## Build contract

Roles and loop per `~/ai/lab/knowledge/build-contract.md` (main session briefs, builder implements, reviewer verifies on a fresh context, max 2 rounds). Local mapping, in force since slice 15 of the 1.0 alignment (2026-09-19):

- builder = Agent tool `builder` (Sonnet, high); reviewer = Agent tool `reviewer` (Opus, high, adversarial with mutation testing on a `git archive` copy — never in the worktree); explorer = `explorer` (Sonnet). acpc dispatch (`builder-sub`, `codex`) only for vendor-specific or heavier slices, on Damian's call.
- Brief, builder report, reviewer verdicts and SPEC delta live as files under `docs/plans/<version>/` (`slice-N.md`, `slice-N-report.md`, `slice-N-review-<round>.md`, `slice-N-spec-delta.md`). Agents return a few lines; the file is the record.
- SPEC/README/help wording is authored by the orchestrator before dispatch and pasted verbatim into the brief; builders never edit `SPEC.md`.
- One commit per slice. After `approve` plus green gate (`pytest` with `ACPC_STANDARD_CHECKOUT` set, ruff, pyright, `smoke.sh`) and the orchestrator's own diff read, commit without asking; pushing stays Damian's act.
- One writer per tree: no builder or reviewer edits while a live-run or gate runs on the same worktree, and vice versa.
- Every test and dev run: short isolated `ACPC_HOME` (daemon socket path limit 108 B), `env -u ACPC_CEILING`, `uv run --frozen`. `pgrep`/`kill` only by the full repo path; never bare `git stash` (shared across worktrees).
- Long prompts via `--prompt-file`, never argv (visible in `ps`; a 2026-08-04 dispatch matched a cleanup `pgrep -f` pattern and was killed). Never export `CODEX_HOME`/`CLAUDE_CONFIG_DIR` around an acpc dispatch; daemons key on the entry's declared environment.

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

## Release page

Only on Damian's explicit go, after he pushed main + tag. `gh release create v<x> --title "acpc <x>" --notes-file <file>`. Notes very concise: one opening line naming the release's theme (+ PR ref if any), then `### Added` / `### Changed` / `### Fixed` — one line per item, only sections that apply. Breaking changes prefixed `**BREAKING:**` under Changed. What it does now, never process/journey. Model: v0.7.0 and v0.7.1 pages.

## Project

- Python >= 3.13, Linux and macOS (the declared classifiers; CI runs the gate on both). Windows is a design port, not a runner: Unix sockets, `fcntl` locks and process groups have no Windows path yet
- Toolchain: uv (build, deps, run); `src/` layout, `uv_build` backend
- Lint/format: `uv run ruff check && uv run ruff format`
- Type check: `uv run pyright`
- Tests: `uv run pytest`
- Acceptance: `smoke.sh` (end-to-end against ACP mock agent)