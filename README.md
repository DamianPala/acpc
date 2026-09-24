# acpc

Dispatch coding agents (codex, claude, grok, …) over the Agent Client Protocol, from the command line.

acpc is built for a specific primary user: **another agent calling it through a shell tool**. Everything follows from that — blocking calls that print the answer once, plain text with no spinners or ANSI, state on disk that can be grepped, and exit codes that mean something. A human at a terminal is the secondary audience and still gets an honest tool.

`SPEC.md` is the normative contract; this README is the tour.

## The whole mental model

*`run` blocks and prints the answer; `--background` (alias `--bg`) prints a receipt naming the ID (`--json` carries it as `session_id`); `status`/`list`/`log`/`wait`/`continue`/`steer`/`cancel` operate on that ID; everything is on disk under a predictable path.*

`list` is the bounded collection view; active sessions show `idle <age>` since their newest transcript event, while finished sessions show `·`. `status <id>` is the fixed-cost detail view. The JSON view exposes the same idle value as `idle_seconds` (`null` when unavailable or finished). Every row also names the model the session resolved to, because entry names hide it — two variants that both `extend` the same parent run the same model, and only the column says so. Each `status` detail and foreground turn result also carries cumulative `usage` when the adapter reports consumption. `usage.drift` stays `null` until a per-turn check finds that the adapter's reports violate the entry's profile. The first mismatch is kept, `quality` stays `estimate`, and the command that prints that turn's summary writes one note on stderr. Token totals continue to follow the profile.

A failed run is not lost: `acpc status <id>` names the failure on its `failure` line, and `acpc continue <id>` resumes the session with its transcript context intact — the failed turn's partial answer is parked as `answer.<n>.md` in the session dir. `acpc continue <id>` with no message at all picks up an interrupted turn (after `canceled`, `failed` or `unknown`) with acpc's own continuation instruction, which names how the previous turn ended; after `succeeded` there is nothing to pick up and a message is required.

```bash
# 90% of usage is this:
acpc run codex "fix the failing test in tests/test_auth.py" --cwd ~/repo --permissions execute

# Background + collect later. On a terminal --background prints the session id, then its dir:
acpc run codex "run the full suite and summarize" --background
acpc wait x7k2

# Follow up in the same session, context preserved:
acpc continue x7k2 "now apply the same fix to the v2 API"

# Long prompts via heredoc:
acpc run claude - --permissions execute <<'PROMPT'
Review the implementation against SPEC.md and make the required edits.
PROMPT
```

An agent caller reads the session id straight from the `--background` output — shell variables don't survive across its tool calls anyway. When stdout is not a terminal, that output is the tagged receipt below (the id sits in the `<result>` tag's `session_id` attribute); a script that really chains in one shell uses `--background --json` and takes `jq -r .session_id`.

When stdout is not a terminal, `run`, `continue`, `steer` and `wait` print the answer inside a tagged document acpc builds, so a caller can tell the metadata from the agent's words:

```text
<result session_id="x7k2" status="succeeded" partial="false">
<metadata>
{"turn":1,"capabilities":{"steer_mode":"in-place","continue_without_message":true},"context":{"used":1834,"size":200000,"peak":1834},"stop_reason":"end_turn","next":["acpc","continue","x7k2"]}
</metadata>
<answer>
The answer, verbatim Markdown.
</answer>
</result>
```

The answer text is untouched apart from control-byte escaping. When it contains one of the wrapper's own tag strings, the answer section uses counted tags, `<answer-N>` and `</answer-N>` with N the number of answer lines, so a reader takes exactly N lines and validates the boundary with the closing tag. `--json` returns the original answer string and the complete document; it is a choice for programmatic parsing, not a requirement for reading an answer. On a terminal the answer stays raw on stdout and the metadata goes to the `--` summary line on stderr.

## Command surface

```
run <agent> (prompt | - | --prompt-file) [options]   # default: block, stdout = final answer
resolve <agent> [options]                             # preview one call, dispatch nothing
continue <id> (prompt | - | --prompt-file)           # follow-up in the same session context
steer <id> (instruction | - | --prompt-file)         # correct the running turn in place, or cancel and redirect it
status <id>                # one session's liveness-verified vitals
list                       # active + recent sessions, bounded by 20
log <id> [--since CURSOR] [--limit N | --tail N] [--prose] [--wait-new | --follow]   # incremental transcript access
wait <id> [--timeout S]    # block until the turn selected at call start ends, print its answer
cancel <id>
delete <id> --yes | prune [--older-than D] [--dry-run] [--yes]
agents list|get|check|create|delete          # adapters + variants
agents create <name> --extends <agent>       # scaffold a variant
agents delete <name> [--yes]                 # remove one local entry
probe <entry> --discover [--json]             # read the adapter's advertised modes; report only
skills list|get                               # bundled how-to skills; get prints body + dir on stderr
install <agent>
daemon status|stop [target] [--force]   # plumbing escape hatch — never needed in the happy path
```

`acpc --help` is a self-contained cheat sheet; `acpc <cmd> --help` is that command's full reference. `<id>` accepts a session id or a `--name` alias; `last` works on a TTY only.

`list` uses this order for its first N entries: active sessions first, newest by creation time, then finished sessions, newest by finish time, falling back to creation time when a session has none; ties are broken by session id, descending in both groups.
`agents list` orders adapter names ascending, followed by each adapter's variants in name order; its default window is the first 20 entries in that order.
`agents check` orders entries by name, ascending, with the default window being the first 20 entries in that order.
`skills list` orders entries by name, ascending, with the default window being the first 20 entries in that order.
`daemon status` orders entries by target name, ascending, with the default window being the first 20 entries in that order.
`prune` fixes its target set before confirmation, then locks and rechecks every target before clearing any session; a target that changed meanwhile fails with `conflict` and leaves the set intact.

A vendor usage limit that blocks a turn is waited out inside that turn: the session shows `waiting`, `status` names the reason and the reset time under `limit`, and acpc resends on the same adapter session after the reset (within `limit_wait_max`, 8h by default; `"0s"` in the config turns waiting off, so every limit ends the turn as `failed` with `stop_reason: rate_limit`). `cancel` during the wait drops the resumption. acpc reads the limits of claude-agent-acp and codex-acp; a codex plan with no access at all stays a plain failure.

For one-time changes from earlier releases, see [MIGRATION.md](MIGRATION.md).

## Bundled skills

Recipes that would go stale in `AGENTS.md` live as package skills. List them
with `acpc skills list`; print one body with `acpc skills get <name>` (the skill
directory path is on stderr — that is how you find `references/`).

| Skill | For | Source |
|-------|-----|--------|
| `adapter-bringup` | New base adapter for any ACP agent (`command`, no `extends`): binary, entry TOML, modes via discovery or product docs | [`src/acpc/data/skills/adapter-bringup/SKILL.md`](src/acpc/data/skills/adapter-bringup/SKILL.md) |
| `provider-bringup` | Existing harness + new provider/model (OpenRouter, gateway, local endpoint); variants that fail on the selected model | [`src/acpc/data/skills/provider-bringup/SKILL.md`](src/acpc/data/skills/provider-bringup/SKILL.md) |
| `refresh-adapter-models` | Vendor shipped new model ids: upgrade the adapter binary first, then overlay `$ACPC_HOME/agents/<name>.toml` presets and `[effort_by_model]` | [`src/acpc/data/skills/refresh-adapter-models/SKILL.md`](src/acpc/data/skills/refresh-adapter-models/SKILL.md) |

```bash
acpc skills list
acpc skills get adapter-bringup
acpc skills get provider-bringup
acpc skills get refresh-adapter-models
```

`acpc probe <entry> --discover` opens a session, reads the modes the adapter advertises, releases it, and prints that catalogue alongside a two-sided diff against the entry's recorded `[modes]`: modes the adapter advertises that the entry does not list, and entry modes the adapter no longer advertises. It costs zero turns and never edits the registry — applying anything it reports is a separate, explicit act. Measuring what a mode actually *permits* is not in this release, so `--discover` is required and a bare `probe` says so rather than answering a question you did not ask. Probe refuses to run on Windows because its commands are POSIX shell commands and would measure the shell rather than the sandbox.

`steer <id> "…"` corrects the turn in flight. When the session's adapter supports it (codex-acp and claude-agent-acp do), the correction goes **in-place**: the adapter adds it to the running turn at its next safe point, usually after the current tool call, and the turn keeps its number, prompt and answer files. Otherwise, or with `--steer-mode cancel-then-start`, acpc cancels the turn, waits for it to end and starts the next turn with the instruction under the fixed interruption preamble — `cancel` plus `continue` without the race in the middle. `status` and every `run`, `continue` and `steer` result show the session's default mode under `capabilities.steer_mode`, and every `steer` result says what happened in `correction_result`: `accepted` means the adapter took the instruction, not that the model obeyed it. During daemon-owned `continue` preparation nothing has reached the callee yet: in-place is a `conflict`, and cancel-then-start cancels that preparation, reports that nothing was interrupted, and sends the instruction plainly.

`status` reports that daemon-owned window as `preparing`. `cancel` and Ctrl-C finish a canceled preparation with a no-prompt placeholder, releasing the session reservation and the adapter binding. What acpc cannot do is unsend a restore already in flight — ACP defines no cancellation for `session/load` or `session/resume` — so the promise is about what a turn runs against rather than about what the adapter does: the next `continue` waits for that restore to settle before preparing its own, and no turn ever runs against a half-restored session.

## Reading a run

- **stdout carries exactly one thing**: the answer (raw on a terminal, inside the tagged document otherwise), a JSON envelope (`--json`), or the `--background` receipt (id + session dir on a terminal, the tagged document without an answer section otherwise). With `--output-file`, stdout stays empty and the exact selected payload goes to the file, on success and on a failure that returns a result; a call that returns no result creates no file. Never spinners, logs, or diagnostics.
- **A failure still answers when there is an answer.** `run`, `continue` and `wait` print their result document — in `--json` too — whenever they observed the end of the turn: a failed or canceled turn, an unobserved outcome. `partial` says whether the answer is complete (`false`) or the turn ended before it did (`true`); a result with `partial: true` always exits non-zero. The structured error stays on stderr either way. A `--timeout` deadline is not an observed end: it exits 124 with an empty stdout, no file, and a `timeout` error whose hint points at `log --tail` and `status` (the answer so far stays in the session files). A call that never observed a turn — an unknown agent, a bad flag, a missing session — writes nothing to stdout.
- **Textual acpc metadata and footers on stderr**: end-of-run summaries are prefixed `--` and show duration, context occupancy, usage totals, exit, session id and dir. A detected usage drift adds one `acpc:` note for that session. `log`/`status` footers are also prefixed `--`. Error envelopes are unprefixed JSON. Harnesses that merge streams can still separate the two mechanically.
- **`log <id>`** is the progress view — condensed one-liners, tool calls and prose interleaved. The condensed view skips repeated `usage` events with the same `used` and `size` in one turn. **`log <id> --prose`** is the content view — clean markdown of what the agent wrote, with terminal control bytes shown as text and one blank line between messages, error records and turns. Its default non-follow window is the last 20 events, emitted in transcript order. `--since CURSOR --limit N` emits the first N later events, so polling never skips the middle. `--follow` without a selector starts at the transcript beginning; `--since CURSOR --follow` starts after the cursor; `--tail N --follow` replays the last N matching events in transcript order and then keeps reading. `--limit` conflicts with `--tail` and ends a follow after N emitted events.

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
| 2 | usage error — malformed flags or a rejected mode/permissions combination |
| 4 | output budget exhausted — `log --follow` stopped because `--max-output` ran out before the session ended |
| 124 | timeout (`run`: gave up waiting, session runs on; `wait`/`log --wait-new`/`log --follow`: same) |
| 130 | canceled — SIGINT or `cancel` |
| 141 / 143 | SIGPIPE / SIGTERM |

**Client death and session death depend on ownership.** SIGINT cancels the session. SIGTERM — a harness killing the tool call on its own timeout, the normal case for an agent caller — detaches only work already taken over by the daemon. A direct-worker turn ends with the client. A detached session's last stderr line names the id and its options (`wait` for the answer, `cancel` to cancel), and `wait <id>` collects the answer later.

## Permissions

`--permissions none|read|edit|execute|all|ask` names a ceiling for ACP permission requests, classified by tool-call kind. `ask` is off the scale: reads are allowed and other categories ask on `/dev/tty`. The default is `ask` when stdin and stdout are terminals and the context permits a question, and `read` otherwise. A terminal `run --background` without an explicit policy asks once which policy to detach with. No policy question is asked under `--json`, when stdin is not a terminal, or when `NO_INPUT` is non-empty. `write` and `prompt` remain accepted as deprecated aliases for `execute` and `ask`. `acpc status <id>` shows the policy in force for a session under `permissions` (policy, adapter mode, where it came from, any clamp).

Three edges worth internalizing:

- **The non-TTY default is a silent read-only trap.** A caller that passes no `--permissions` gets a read-only callee — write requests are denied without an error and the turn exits 0. Pass `edit` for file changes, or `execute` when the task must also run commands.
- **The policy selects the vendor mode.** Adapter `[modes]` tables measure each mode's `grants`, whether it `delegates`, and whether it `escalates` — the last being informational, a flag that an in-vendor auto-approver can raise that mode's ceiling unasked, so its `grants` is a measurement rather than a bound. acpc always sends `session/set_mode`, and refuses a mode that grants more than the policy. An advertised mode missing from `[modes]` is admitted only under `all`. `--mode` is normally unnecessary, but an explicit value is checked by the same ceiling and comes from `acpc agents get <name>`.
- **This is an approval policy, not a sandbox.** It answers the requests the adapter emits; a delegating mode can still classify some work as safe and emit no request. A real boundary means confining the adapter itself: a container, a dedicated user, or the vendor's own sandbox.

## Agent variants

A named TOML entry bundles model, effort, permissions, an optional mode override, home and environment, so `run builder "task"` replaces five flags:

```toml
# ~/.acpc/agents/builder.toml — hand-editable; `agents create` scaffolds this
extends = "codex"
model = "gpt-5.6-luna"
effort = "xhigh"
permissions = "execute"
home = "~/.codex-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]   # names read from the caller's env, never stored

[env]
MODEL_PROVIDER = "openrouter"
```

The `home` field is the provider switch: OpenAI vs OpenRouter vs a local endpoint is just a different vendor home. Resolution stays fully inspectable — `acpc agents get builder` shows what the entry resolves to with per-field provenance, `acpc resolve builder` shows one concrete call.

`--model fast|standard|max` resolves through the adapter's preset table (overridable per adapter in `agents/`). When the vendor advertises new model ids, upgrade that adapter's binary first, then `acpc skills get refresh-adapter-models` — it writes only the operator overlay, after a confirm. The adapter's environment is **constructed, not inherited**: a base system set, capability variables (ssh agent, proxies, CA bundles), and the entry's declared env — the rest of your ambient environment never reaches the adapter.

## Teaching agents about acpc

acpc documents itself: the root `--help` is a complete cheat sheet, and every command's `--help` names its own defaults. Don't copy usage documentation into your agent instructions — it goes stale the first time a flag moves, and the copy is what the agent will believe.

Add only the routing knowledge an agent cannot infer from the tool, e.g. in your global `AGENTS.md`:

```markdown
## acpc — dispatch external coding agents (codex, claude, grok, agy)
- Models available in your harness's own subagent tool → use that tool.
  acpc is for external agents only.
- First contact: `acpc --help` (complete cheat sheet).
- Entries and what each is for: `acpc agents list`.
```

Give every entry in `~/.acpc/agents/` a one-line `description`. `acpc agents list` prints it beside each entry, so an agent reading the roster learns what `builder` is *for* from the tool rather than from documentation you have to keep in sync. Use `acpc agents get NAME` for one entry's details.

If your roster is stable, add a purpose table so the agent knows the roles before its first call — this is routing knowledge, not usage documentation:

```markdown
| Entry | For |
|-------|-----|
| builder | implements against an existing plan |
| explorer | cheap read-only research |
| codex | the real vendor CLI for heavier work |
```

Keep it to purpose only. Models, efforts and flags belong to the tool — `acpc agents list` always shows the current roster, and `acpc agents get NAME` shows one entry's current truth.

## State on disk

```
~/.acpc/                     # ACPC_HOME overrides it — the only env var acpc reads for itself
  config.toml                # retention, daemon_ttl, daemon_max_concurrent — the whole file
  agents/<name>.toml         # variants, adapter overrides, new adapters
  cache/<agent>/             # advertised models, modes, commands
  daemon/<target>.log        # adapter stderr per target
  sessions/<id>/
    meta.json                # full resolved invocation + state, timing, context occupancy, exit code
    prompt.md                # the prompt as sent (earlier turns: prompt.<n>.md)
    transcript.ndjson        # full event stream, versioned public format
    answer.md                # final answer (earlier turns: answer.<n>.md)
```

File-based state is a feature: grep it, read fragments selectively, depend on nothing but the filesystem. `answer.md` exists whatever the final state — partial answers for failed or canceled turns, or a placeholder naming an unobserved outcome. A failed session also records why it failed: an `error` event carrying what acpc observed, one next step, and — for a turn that ran under a daemon — the tail that turn added to the target's log, surfaced by `log` and by `wait`. Session states are verified, not trusted: a `running` session whose process is gone reports `unknown`, never a stale `running`.

A cold `continue` reports whether the adapter session was verified: the summary and `--json` contain `resume: verified` when a check passes, or `resume: unverified — ...` when neither available check could run. If acpc could not account for every delivered prompt, it reports `resume: unverified — delivery record incomplete` even when replay comparison passes. An unverified resume still runs, but is visible to callers.

## The daemon

A performance cache, nothing more: it keeps adapters warm so the next turn starts in ~2 s instead of a cold start. Auto-managed — starts on first use, expires after 30 min idle, restarts itself on version skew, heals itself if its adapter dies. One daemon per *target* (agent entry + vendor home + literal declared env + named passthrough values + resolved permission policy + process-level spawn identity when `effort_via = "cli"`), so different providers, credentials or permission ceilings never share a process. Secret values affect the target hash but are never exposed. If a daemon cannot start at all, `run` falls back to a direct child and says so on stderr.

```bash
acpc daemon status          # acpc version, pid, uptime, idle age, per-target log path
acpc daemon stop codex      # controlled nuke; beats pkill, which kills mid-task dispatches
acpc daemon stop codex --force  # ... and take its running sessions down with it
```

## Trust model

Entry TOMLs are trusted at the level of shell config: an adapter definition names the command acpc executes and the env delivered to it. Only place files you trust in `agents/`. State dirs are owner-only (0700/0600) — prompts and transcripts routinely carry sensitive material. Secrets travel via `env_passthrough` names read at call time; values are never written to disk.

## Development

Python ≥ 3.13; uv drives everything:

```bash
uv run pytest && uv run ruff check && uv run ruff format --check && uv run pyright
./smoke.sh        # end-to-end acceptance against the ACP mock agent
```

`docs/live-test-plan.md` is the checklist for verifying against real adapters.

## License

MIT
