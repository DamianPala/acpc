# acpc

Dispatch coding agents (codex, claude, gemini) over the Agent Client Protocol, from the command line.

acpc is built for a specific primary user: **another agent calling it through a shell tool**. Everything follows from that — blocking calls that print the answer once, plain text with no spinners or ANSI, state on disk that can be grepped, and exit codes that mean something. A human at a terminal is the secondary audience and still gets an honest tool.

`SPEC.md` is the normative contract; this README is the tour.

> **Status: 0.3, local install.** Not on PyPI. Requires Python ≥ 3.13.

## The whole mental model

*`run` blocks and prints the answer; `--bg` returns an ID; `status`/`log`/`wait`/`continue`/`stop` operate on that ID; everything is on disk under a predictable path.*

```bash
# 90% of usage is this:
acpc run codex "fix the failing test in tests/test_auth.py" --cwd ~/repo --permissions write

# Background + collect later. --bg prints the session id, then its dir:
acpc run codex "run the full suite and summarize" --bg
acpc wait x7k2

# Follow up in the same session, context preserved:
acpc continue x7k2 "now apply the same fix to the v2 API"

# Long prompts via heredoc:
acpc run claude - --permissions write <<'PROMPT'
Review the implementation against SPEC.md and make the required edits.
PROMPT
```

An agent caller reads the session id straight from the `--bg` output — shell variables don't survive across its tool calls anyway. A script that really chains in one shell uses `--bg --json` and takes `jq -r .session_id`.

## Command surface

```
run <agent> (prompt | - | --prompt-file) [options]   # default: block, stdout = final answer
continue <id> (prompt | - | --prompt-file)           # follow-up in the same session context
status [id]                # no id: active + recent; with id: one session's vitals
log <id> [--since CURSOR] [--tail N] [--prose] [--wait-new]   # incremental transcript access
wait <id> [--timeout S]    # block until done, print the answer
stop <id>
rm <id> | prune [--older-than D] [--dry-run]
agents [name] [--models|--commands|--check]   # adapters + variants; resolved definitions
agents init <name> --extends <agent>          # scaffold a variant
install <agent>
daemon status|stop [target]   # plumbing escape hatch — never needed in the happy path
```

`acpc --help` is a self-contained cheat sheet; `acpc <cmd> --help` is that command's full reference. `<id>` accepts a session id or a `--name` alias; `last` works on a TTY only.

## Reading a run

- **stdout carries exactly one thing**: the answer (default), a confirmation (`-o`), a JSON envelope (`--json`), or id + session dir (`--bg`). Never spinners, logs, or diagnostics.
- **stderr carries acpc's own metadata**, every line prefixed `--`: the end-of-run summary (duration, tokens, exit, session id, dir) and `log`/`status` footers. Harnesses that merge streams can still separate the two mechanically.
- **`log <id>`** is the progress view — condensed one-liners, tool calls and prose interleaved. **`log <id> --prose`** is the content view — clean markdown of what the agent wrote. `--since CURSOR` never re-emits events, so polling is cheap and stateless.

```
$ acpc log x7k2 --since 42
[12:01:05] tool  Bash "pytest -x" → exit 1 (2.3s)
[12:01:20] msg   "Tests fail because the fixture assumes..." (280 chars)
-- running 3m12s | 45 events | cursor: 45
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (`end_turn`) |
| 1 | agent error — crash, refusal, missing auth |
| 2 | usage error — bad flags, unknown session, rejected mode/permissions combination |
| 124 | timeout (`run`: session cancelled; `wait`/`log --wait-new`: gave up waiting, session runs on) |
| 130 | cancelled — SIGINT or `stop` |
| 141 / 143 | SIGPIPE / SIGTERM |

**Client death ≠ session death.** SIGINT cancels the session. SIGTERM — a harness killing the tool call on its own timeout, the normal case for an agent caller — *detaches*: the session keeps running under the daemon, the client prints the session id to stderr on the way out, and `wait <id>` collects the answer later.

## Permissions

`--permissions all|write|read|none|prompt` decides how acpc answers ACP permission requests, classified by tool-call kind. Default: `prompt` on a TTY, `read` otherwise (`--bg` counts as non-TTY).

Two edges worth internalizing:

- **The non-TTY default is a silent read-only trap.** A caller that passes no `--permissions` gets a read-only callee — write requests are denied without an error and the turn exits 0. Pass `--permissions write` whenever the task should modify anything.
- **This is an approval policy, not a sandbox.** It answers the requests the adapter emits; it cannot stop an adapter that never asks. Concretely: codex's default `agent` mode auto-allows edits inside the workspace without asking, so a policy below `all` only bites in its `read-only` mode. A vendor mode that suppresses requests entirely (`agent-full-access`) is rejected at parse time unless `--permissions all`. A real boundary means confining the adapter itself: a container, a dedicated user, or the vendor's own sandbox.

## Agent variants

A named TOML entry bundles model, effort, permissions, home and environment, so `run builder "task"` replaces four flags:

```toml
# ~/.acpc/agents/builder.toml — hand-editable; `agents init` scaffolds this
extends = "codex"
model = "gpt-5.6-luna"
effort = "xhigh"
permissions = "write"
home = "~/.codex-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]   # names read from the caller's env, never stored

[env]
MODEL_PROVIDER = "openrouter"
```

The `home` field is the provider switch: OpenAI vs OpenRouter vs a local endpoint is just a different vendor home. Resolution stays fully inspectable — `acpc agents builder` shows what the entry resolves to with per-field provenance, `run --dry-run` shows one concrete call.

`--model fast|standard|max` resolves through the adapter's preset table (overridable per adapter in `agents/`). The adapter's environment is **constructed, not inherited**: a base system set, capability variables (ssh agent, proxies, CA bundles), and the entry's declared env — the rest of your ambient environment never reaches the adapter.

## State on disk

```
~/.acpc/                     # ACPC_HOME overrides it — the only env var acpc reads for itself
  config.toml                # retention, daemon_ttl, daemon_max_concurrent — the whole file
  agents/<name>.toml         # variants, adapter overrides, new adapters
  cache/<agent>/             # advertised models, modes, commands
  daemon/<target>.log        # adapter stderr per target
  sessions/<id>/
    meta.json                # full resolved invocation + state, timing, tokens, exit code
    prompt.md                # the prompt as sent (earlier turns: prompt.<n>.md)
    transcript.ndjson        # full event stream, versioned public format
    answer.md                # final answer (earlier turns: answer.<n>.md)
```

File-based state is a feature: grep it, read fragments selectively, depend on nothing but the filesystem. `answer.md` exists whatever the final state — partial answers for failed or cancelled turns, a placeholder naming what died for orphaned ones. Session states are verified, not trusted: a `running` session whose process is gone reports `orphaned`, never a stale `running`.

## The daemon

A performance cache, nothing more: it keeps adapters warm so the next turn starts in ~2 s instead of a cold start. Auto-managed — starts on first use, expires after 30 min idle, restarts itself on version skew, heals itself if its adapter dies. One daemon per *target* (agent + home + declared env), so different providers or credentials never share a process. If a daemon cannot start at all, `run` falls back to a direct child and says so on stderr.

```bash
acpc daemon status          # pid, uptime, per-target log path
acpc daemon stop codex      # controlled nuke; beats pkill, which kills mid-task dispatches
```

## Trust model

Entry TOMLs are trusted at the level of shell config: an adapter definition names the command acpc executes and the env delivered to it. Only place files you trust in `agents/`. State dirs are owner-only (0700/0600) — prompts and transcripts routinely carry sensitive material. Secrets travel via `env_passthrough` names read at call time; values are never written to disk.

## Development

```bash
uv run pytest && uv run ruff check && uv run ruff format --check && uv run pyright
./smoke.sh        # end-to-end acceptance against the ACP mock agent
```

`docs/live-test-plan.md` is the checklist for verifying against real adapters.

## License

MIT
