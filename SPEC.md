# acpc Specification

What a CLI for dispatching agents (codex, claude, gemini) should look like from the perspective of its primary user: another agent calling it through a shell tool.
Normative: the implementation is adjusted to match this document; behavior changes land here first, in the same change as the code.

## How an agent consumes a CLI (design constraints)

1. **It sees command output once, at the end, as a text blob.** Anything "live" (spinners, progress, streaming) is invisible or noise. Streaming only makes sense as append-to-file, read incrementally later.
2. **Tool output limits.** Harnesses cap what a tool call returns inline; past the cap, output is diverted to a file with a short preview, or hard-truncated. Large results belong in files the tool writes itself; stdout carries the answer when short, and always the file path.
3. **The calling harness has background execution with completion notification.** A blocking sync mode is therefore the default: the caller runs `acpc run ...` in the background and gets woken when it exits. `--bg` is the second option.

## Command surface

Small verb set. The whole mental model in one sentence:
*"`run` blocks and prints the answer; `--bg` returns an ID; `status`/`log`/`wait`/`continue`/`stop` operate on that ID; everything is on disk under a predictable path."*

```
run <agent> [prompt | -] [options]    # default: block, stdout = final answer
continue <id> [prompt | -]            # follow-up in the same session context
status [id]                           # no id: list active + recent; with id: one session's vitals
log <id> [--since CURSOR] [--tail N] [--wait-new]  # incremental transcript access
wait <id> [--timeout S]               # block until done, print the answer
stop <id>
rm <id> | prune [--older-than D]      # session cleanup (auto-prune per config retention)
agents [name] [--models|--commands|--check]   # adapters + variants; with name: resolved definition (cached)
agents init <name> --extends <agent>  # scaffold a variant
install <agent>                       # one-step fix for "not installed"
daemon status|stop [target]           # plumbing escape hatch — never needed in the happy path
```

`<id>` everywhere accepts a session id or a `--name` alias; `last` works too, but only on a TTY (see *TTY vs non-TTY*). Session ids are 4 lowercase characters from an alphabet without ambiguous glyphs (no `0`/`o`, `1`/`l`), re-rolled on collision — a local handle; the adapter's own long session id stays internal, mapped in `meta.json`.

90% of usage is `run codex "do X" --cwd /path/to/repo` and reading stdout. That path must be trivial; everything else is optional.

Command reference below is alphabetical. Sections open with their synopsis; `--json` applies uniformly (see *Output contract*) and is listed only where its semantics differ (`log`).

### `agents`

```
agents [name] [--models | --commands | --check]
agents init <name> --extends <agent> [--model M] [--effort E] [--permissions P] [--home DIR]
```

| Option | Purpose |
|--------|---------|
| `--models` / `--commands` | Dump the full advertised list. Accepted on any name — a variant delegates to its parent |
| `--check` | Live probe: launch + auth. With name one adapter, without every installed one; one line per adapter, any failure → exit 1 |
| `init --extends <agent>` | Scaffold a variant; the flags mirror the entry's fields |

Without name: one aligned row per adapter and variant. Variants (indented) show only their delta in fixed columns — model, effort, permissions, home (`·` = unset; home `~`-abbreviated, copy-able into `--home`). Status is `installed`/`missing` only; auth is not shown — cached auth state rots; the truth surfaces at `run` time as an actionable error. `agents --check` is the opt-in live probe.

With name: the resolved definition, field by field with provenance — the entry in general; one concrete call's resolution, with call-site flags applied, is `run --dry-run`.

Advertised data — modes, models, slash commands — is adapter-level (variants inherit their parent's):

- It appears in the adapter's detail view only; a variant's view ends with a pointer instead of repeating the catalogs.
- Advertised lists are capped in the adapter view (first 3 + count). Model lists are short and curated, so `--models` prints them in full. Commands can be 50+ with paragraph-length descriptions, so each truncates to its first sentence; complete text lives in the cache file the footer names.
- `agents --models` without a name: cross-agent overview, variants collapsed to one line each.
- All of it is cached and refreshed on every real run (ACP announces it only after session creation); on a cache miss — `agents <name>` before the first ever run — the live probe runs automatically instead of printing empty fields.
- Every view that prints advertised data ends with one cache-age footer; views built from live state alone (the bare list, a variant's resolution) have none.

```
$ acpc agents
claude   Claude Code (Anthropic)   installed
codex    Codex CLI (OpenAI)        installed
  builder    gpt-5.6-luna   xhigh  write  ~/.codex-openrouter
  explorer   gpt-5.6-luna   low    read   ~/.codex-openrouter
  planner    gpt-5.6-sol    xhigh  write  ~/.codex-openrouter
gemini   Gemini CLI (Google)       missing → acpc install gemini

$ acpc agents builder           # what this entry resolves to
extends      codex
model        gpt-5.6-luna (entry)
effort       xhigh (entry)
permissions  write (entry)
home         ~/.codex-openrouter (entry)
cwd          . (default)

$ acpc agents codex --commands
/init            Create an AGENTS.md file with instructions for Codex
/review          Review current changes and find issues
/$image-gen      Generate or transform bitmap image assets from text prompts or references…
/$openai-docs    Use when the user asks how to build with OpenAI products or APIs…
…
-- 47 commands (cached 2h ago) | full descriptions: ~/.acpc/cache/codex/commands.md

$ acpc agents claude            # base adapter: same view, defaults instead of overrides
adapter      Claude Code (Anthropic) · installed · claude-code-acp 0.5.1
model        claude-sonnet-5 (adapter default)
effort       high (adapter default)
permissions  prompt on TTY, read otherwise (unset)
home         ~/.claude (default)
modes        default · acceptEdits · plan · bypassPermissions
models       9 · claude-opus-5 claude-sonnet-5 claude-haiku-4-5 …   (--models for all)
commands     52 · /review /init /compact …          (--commands for all)
variants     none
-- cached 30m ago

$ acpc agents claude --models
presets   fast      claude-haiku-4-5   high
          standard  claude-sonnet-5    high
          max       claude-opus-5      max
models    claude-opus-5
          claude-sonnet-5
          claude-haiku-4-5
          claude-opus-4-8
-- cached 30m ago

$ acpc agents --models
codex
  presets   fast      gpt-5.6-luna    high
            standard  gpt-5.6-terra   xhigh
            max       gpt-5.6-sol     xhigh
  models    gpt-5.6-sol · gpt-5.6-terra · gpt-5.6-luna · gpt-5.5 · gpt-5.4
  variants  builder   gpt-5.6-luna    xhigh
            explorer  gpt-5.6-luna    low
            planner   gpt-5.6-sol     xhigh
claude
  presets   fast      claude-haiku-4-5   high
            standard  claude-sonnet-5    high
            max       claude-opus-5      max
  models    claude-opus-5 · claude-sonnet-5 · claude-haiku-4-5 · claude-opus-4-8
-- cached: codex 2h ago · claude 30m ago

$ acpc agents init builder --extends codex --model gpt-5.6 --effort xhigh
```

### `continue`

```
continue <id> [prompt | -] [--prompt-file F] [-o FILE] [--bg] [--timeout S] [--max-output BYTES]
```

| Option | Purpose |
|--------|---------|
| prompt as arg, `-` (stdin), or `--prompt-file` | Exactly one source, as in `run` |
| `-o` / `--bg` / `--timeout` / `--max-output` | As in `run` — same machinery, same semantics |

Follow-up turn in an existing session, full context preserved. The caller can send feedback on the callee's own work instead of restarting from scratch.

- **Sessions are durable on the adapter side**: resume goes through ACP `session/load`, so `continue` works after the daemon expired or the machine rebooted — the daemon only makes the next turn start warm. The history `session/load` replays is already in the transcript and is not re-appended. Adapters without the `loadSession` capability fail with an actionable error.
- **`continue` on a `running` session is an error**, not a queue.
- **The turn runs with the session's stored resolution**: model, effort, permissions and home come from `meta.json`, not from re-resolving the agent entry — editing an entry never changes a session mid-conversation.

A separate verb only because `run` takes an agent and `continue` takes a session. Each turn's prompt and answer are kept on disk (see *State on disk*). A `run`-only flag here is a usage error that names the rule — `continue reuses the session's permissions — drop --permissions` — never a bare "unrecognized argument".

```
acpc continue researcher "expand section 3, it's too thin"
acpc continue last "now apply the same fix to the v2 API"   # TTY only
```

### `daemon`

Plumbing, deliberately minimal. The daemon is a performance cache — it keeps adapters warm, nothing more.

- **Auto-managed**: starts on first use, expires after an idle TTL (default `30m`, `daemon_ttl` in the global config; idle = no active sessions, so a detached session keeps its daemon alive). No `start`/`restart` verbs — `daemon stop <target>` plus the next run *is* the restart.
- **Keyed per target** (agent + home + declared env — see *Agent variants*): one daemon serves any number of concurrent sessions on its target; different homes/providers are separate targets, so fan-out never serializes.
- **Two uses**: a wedged or stale daemon (`daemon stop`), and debugging (`daemon status` prints PID, uptime and the per-target log path — the only place adapter stderr goes in daemon mode).
- **`daemon stop` with active sessions** transitions them to `failed` with the reason recorded in meta — never orphans.
- **Version skew self-heals**: a daemon that doesn't match the client version restarts itself on connect.
- **Fallback**: if the daemon cannot start at all (restricted sandboxes), `run` spawns the adapter as a direct child — visibly: the stderr summary says so, and SIGTERM then cancels instead of detaching.

```
acpc daemon status
acpc daemon stop codex          # controlled nuke; beats pkill, which kills mid-task dispatches
```

### `install`

Install an adapter.

```
acpc install codex
```

### `log`

Incremental transcript view; the main progress-tracking tool.

```
log <id> [--since CURSOR] [--tail N] [--prose] [--json] [--max-output BYTES] [--wait-new [--timeout S]] [--quiet]
```

| Option | Purpose |
|--------|---------|
| (default) | Last 20 events, condensed — the progress view: "what is it doing" |
| `--since CURSOR` | Only events after the cursor, never re-emitted; combinable with `--tail` |
| `--tail N` | Just the last N of the selected events |
| `--prose` | The content view: "what is it thinking/writing" — agent messages only, untruncated, no tool lines; agents write markdown natively, so this reads as clean markdown. The event window (`--since`/`--tail`, default last 20) selects; `--prose` only renders — a full-history dump is always an explicit `--since 0` |
| `--json` | Raw transcript events for `jq`, each carrying its index; not for reading — lossless inspection is `transcript.ndjson` itself. With `--prose` a usage error — one view per call |
| `--max-output <bytes>` | As in `run` (default 128 KiB, 0 disables), but applied at event granularity: whole events until the budget, then a marker line naming `transcript.ndjson`. The footer cursor covers only what was printed, so a poller never skips content; a single over-budget event is the exception — head + marker, cursor advances past it |
| `--wait-new [--timeout S]` | Long-poll: block until new events appear or the timeout expires (exit 124); without `--timeout` it blocks indefinitely. Waits for *activity* (vs `wait` for completion) — enables mid-run intervention, e.g. `stop` an agent that drifted off task |
| `--quiet` | Suppress the stderr footer, as in `run` |

One chronological stream, tool calls and agent prose interleaved — the sequence is the causal narrative. Events are condensed one-liners: tool call with arg summary, result status, duration; agent message as a 200-char snippet + length; permission requests; errors; state changes. Errors are never filtered and never truncated — in every view, including `--prose`. Full content stays in `transcript.ndjson`.

```
$ acpc log x7k2 --since 42
[12:01:05] tool  Bash "pytest -x" → exit 1 (2.3s)
[12:01:20] msg   "Tests fail because the fixture assumes..." (1 240 chars)
[12:02:10] error permission denied: write outside cwd
-- running 3m12s | 45 events | cursor: 45

$ acpc log x7k2 --prose         # same events, the content question: full messages, no tool lines
Tests fail because the fixture assumes a clean database. Two options:

1. Reset the schema in `conftest.py` — simplest, but slows the whole suite.
2. Wrap each test in a transaction and roll back.

Going with 2; `test_auth` needs its own fixture either way.
-- running 3m12s | 45 events | cursor: 45
```

- **Footer doubles as status**: state, runtime, event count, new cursor. It goes to **stderr** (prefixed `--`, like the run summary): stdout stays pure transcript content, so `log --prose > file.md` yields clean markdown, while an agent caller still sees the cursor — harnesses merge the streams. Agent prose can itself contain `--`-prefixed lines, so stream, not prefix, is what separates content from metadata.
- **Cursor = event number, stateless.** The caller carries the cursor; two pollers on one session cannot corrupt each other. The index is global across views — `--prose` and the default share one cursor space.
- **Finished session**: same output, footer becomes `-- done exit 0 | 3m12s | 41k tok | answer: <path> | cursor: 45` — duration and tokens/cost included, because a `--bg` caller never sees the stderr summary; the cursor stays, so a poller's final call needs no special casing. When the state is `failed`, `timeout` or `orphaned`, the last agent message prints in full — it usually contains the reason.

### `prune`

```
prune [--older-than D] [--dry-run]
```

| Option | Purpose |
|--------|---------|
| `--older-than <D>` | Age threshold, e.g. `7d` |
| `--dry-run` | List what would go, delete nothing |

Delete finished sessions older than the threshold — age measured from when the session finished, not when it started. Auto-prune: the `retention` key in the global config (default `90d`) is applied opportunistically on `run`. Running sessions are never touched.

```
acpc prune --older-than 7d
```

### `rm`

Delete one session's on-disk state. Errors on `running` — `stop` it first.

```
acpc rm x7k2
```

### `run`

Dispatch one agent. Blocks, prints the final answer on stdout, exits with a meaningful code. `--bg` returns a session ID immediately instead.

```
acpc run codex "fix the failing test in tests/test_auth.py" --cwd ~/repo
acpc run claude - --bg --name researcher --effort high <<'EOF'
Research X. Write findings to ./findings.md.
EOF
```

| Option | Purpose |
|--------|---------|
| prompt as arg, `-` (stdin), or `--prompt-file` | Heredoc/stdin for long prompts with quotes and backticks. Exactly one source — zero or two is a usage error naming the options; stdin is never read implicitly |
| `--cwd <dir>` | Working directory of the callee. Long flag on purpose: `-C`/`-c` invites confusion with `continue` |
| `--model <tier\|id>` | A tier (`fast`/`standard`/`max`, resolved through the preset table — presets are (model, effort) pairs) or a raw model ID from `agents <name> --models`. Explicit `--effort` overrides the preset's effort |
| `--effort <level>` | Reasoning effort, orthogonal to `--model`. Superset scale (none/minimal/low/medium/high/xhigh/max/ultra) mapped per adapter; a level the resolved model doesn't support is a hard usage error listing the supported levels — never a silent fallback |
| `--permissions all\|write\|read\|none\|prompt` | Approval policy for ACP permission requests (defined below). Default: agent entry if set, else `prompt` on a TTY and `read` otherwise; `--bg` counts as non-TTY here (see *TTY vs non-TTY*) |
| `--mode <name>` | Callee's operating mode (ACP `session/set_mode`), vendor pass-through, adapter default if omitted. Behavioral hint; a mode that suppresses permission requests is rejected unless `--permissions all` (see below). Values via `agents` |
| `--home <dir>` | Vendor home override (the dir with the vendor's config + credentials). The provider switch (see *Agent variants*); ad-hoc counterpart of a variant's `home` field |
| `-o <file>` | Write the answer to the given path; stdout then carries only a short confirmation (path, size, session id). `answer.md` in the session dir is always written regardless |
| `--bg` | Return immediately with session ID + session dir path. With `-o`, the file is written when the session finishes |
| `--timeout <s>` | Cancels the session on expiry (state `timeout`, exit 124). No default — wall-clock limits belong to the calling harness |
| `--name <alias>` | Human-typeable handle for `continue`/`status`/`log`. Reusing a name rebinds it to the new session with a warning — hard error while the old session is `running`. `last` is reserved |
| `--dry-run` | Print what this call would resolve to (model, effort, permissions, cwd — and where each value came from), then exit |
| `--max-output <bytes>` | Cap on stdout bytes (default 128 KiB, 0 disables). Truncation keeps the head, cuts on a UTF-8 boundary, and ends with a marker line naming the full answer path. The marker sits at the tail, which some harness previews clip — the stderr summary repeats the session dir, so the path always survives. Shapes stdout only: `-o` files and `answer.md` are always complete. With `--json`, truncation applies to the `answer` field and sets `truncated: true`; the envelope is always valid JSON |
| `--quiet` | Suppress the stderr summary line (see *Output contract*) |

**Permissions.** Each ACP `request_permission` is classified by the tool call's `kind`:

- `read` — allow read-only kinds: `read`, `search`, `fetch`, `think`; also `switch_mode`, unless the target mode is on the adapter's bypass list (below)
- `write` — read + `edit`, `execute`; never `delete`/`move`
- `all` — allow everything, including `allow_always` options
- `none` — deny every request
- `prompt` — read kinds auto-allowed; everything else asks the human on `/dev/tty`

Unknown kinds are denied under everything but `all`; denials appear in `log`. `read` and `write` answer with `allow_once` only — `allow_always` is reserved to `all`. Where the model's edges are:

- **`execute` subsumes `delete`/`move`** in practice — excluding those kinds only constrains adapters that classify honestly.
- **`fetch` is network egress** — under the non-TTY default a callee processing untrusted input can reach the network; use `none` when that matters.
- **An approval policy, not a sandbox**: it answers the requests the adapter emits, so a `--mode` that stops the callee from asking (vendor bypass modes) would evade it — such combinations are rejected at parse time unless `--permissions all`.
- **The same door exists at runtime**: a callee can *request* a mode switch (kind `switch_mode`), so a switch into a bypass mode is treated like an unknown kind — denied below `all`, asked under `prompt` — while switches between ordinary modes (e.g. plan → default) stay in the read tier.
- **Bypass lists are adapter-declared**: ACP does not mark modes as bypass; a vendor mode absent from the adapter definition's list passes both guards until the definition is updated.

Not needed: file-attachment flags (paths in the prompt suffice), system-prompt injection.

### `status`

```
status [id] [--all]
```

| Option | Purpose |
|--------|---------|
| `--all` | Every session, not just running + the 5 most recent finished; with an id it's a usage error |

Without id: one line per session — id, the entry it ran on (variant or adapter), state, runtime, name, prompt snippet (five backgrounded codex runs must not look identical). Defaults to all running + the 5 most recent finished; `--all` for everything.

With id: one session's vitals — state (exit code once finished), runtime, tokens/cost so far (cumulative across the session's turns), the entry with its base adapter, name, session dir and answer path.

A pulse, not a dump: reads `meta.json` + process liveness, never the transcript — "what is it doing right now" is `log <id> --tail 1`. State is verified, not trusted: a `running` session whose daemon or adapter is gone reports `orphaned`, never a stale `running` (see *Session states*).

```
$ acpc status
x7k2  codex     running  3m12s   researcher   "Research X and write findings to ./findings…"
p9d4  claude    running  0m41s   ·            "Fix the failing test in tests/test_auth.py"
kq81  reviewer  done     12m40s  spec-review  "Review the diff against the spec and report…"
b3nn  codex     failed   2m05s   ·            "Summarize the repository changes"
m2w7  claude    done     8m19s   docs         "Update the README quick-start for the new CLI"
ze1a  codex     timeout  30m00s  ·            "Migrate the config loader to TOML and run the…"
q4hf  builder   done     22m03s  ·            "Implement the session lock and its tests per…"
-- 2 running · 5 recent · --all for all 17

$ acpc status kq81
state    done · exit 0 · 12m40s · 41k tok
agent    reviewer (codex) · name: spec-review
dir      ~/.acpc/sessions/kq81 · answer: answer.md
```

### `stop`

Stop a running session. Graceful (ACP `session/cancel`) with a bounded wait for the ack (10s) — if the callee doesn't wind down in time, the connection is torn down anyway. Transcript, meta and partial answer stay on disk for post-mortem. A hard variant, if ever needed, is `stop --force`, not a new verb.

```
acpc stop x7k2
```

### `wait`

```
wait <id> [--timeout S] [-o FILE] [--max-output BYTES]
```

| Option | Purpose |
|--------|---------|
| `--timeout <s>` | Stops *waiting* only (exit 124): the session keeps running, unlike `run --timeout`, which cancels it |
| `-o` / `--max-output` | As in `run` — `wait` prints an answer, so it shapes it the same way |

Block until a background session finishes, then print its answer; exit code mirrors the session result. On an already-finished session it returns immediately — the free way to reprint an answer.

```
acpc wait x7k2 --timeout 600
```

## Agent variants

A named agent entry can bundle model, effort, permissions and environment, so `run builder "task"` replaces four flags. The one acceptable form of configuration, under one condition: resolution stays fully inspectable — `agents <name>` shows what an entry resolves to, `--dry-run` what a specific call resolves to and why.

```
# ~/.acpc/agents/builder.toml — hand-editable; `agents init` scaffolds this
extends = "codex"
description = "Implements a task against a plan; writes code and runs commands."
model = "gpt-5.6-luna"
effort = "xhigh"
permissions = "write"
home = "~/.codex-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]   # names read from the caller's env at call time, never stored

[env]                                      # literal values declared in the entry
MODEL_PROVIDER = "openrouter"

$ acpc run builder "implement the parser per SPEC.md"
# ≡ acpc run codex … --model gpt-5.6-luna --effort xhigh --permissions write --home ~/.codex-openrouter
```

The `home` field is also the provider dimension: OpenAI vs OpenRouter vs a local endpoint is just a different vendor home (own config, own credentials). A variant is the named, permanent form; `--home` on `run` the one-off form.

Environment is part of the entry, in two fields. An `[env]` table holds literal values declared in the entry (e.g. the vendor home path). `env_passthrough` lists variable *names* read from the caller's environment at call time — values are never stored on disk, which is how API keys travel. Both are part of the daemon target key ("declared env"), so two entries with different env are two targets that cannot serve each other's traffic; ambient environment not named in the entry never reaches the adapter.

## Output contract

- **stdout carries exactly one thing, chosen by flags**: the answer (default), a short confirmation (`-o`), a JSON envelope (`--json`), a session ID + dir path (`--bg`). Never spinners, ANSI, logs or diagnostics — those go to stderr or the session log file.
- **"The answer" is defined**: the turn's ACP agent-message content, chunks concatenated in stream order; thought chunks and tool output excluded; markdown passed through verbatim. Narration interleaved between tool calls is part of it — never silently dropped. stdout and `answer.md` carry identical bytes; per turn, it equals what `log --prose` shows.
- **Fixed exit codes** (Unix conventions; finer-grained ACP `stop_reason` lives in `meta.json` and the `--json` envelope):

  | Code | Meaning |
  |------|---------|
  | 0 | success (`end_turn`) |
  | 1 | agent error — crash, `refusal`, `max_tokens`, missing auth |
  | 2 | usage error — bad flags, unknown session, rejected mode/permissions combination |
  | 124 | timeout (`run`: session cancelled; `wait`/`log --wait-new`: gave up waiting, session still runs) |
  | 130 | cancelled — SIGINT or `stop`. Answer-printing commands mirror the session result, so `wait` on a cancelled session also exits 130, whoever cancelled it and whenever; the finer distinction lives in `stop_reason` |
  | 141 / 143 | SIGPIPE / SIGTERM (SIGTERM detaches — see below) |

- **Client death ≠ session death.** SIGINT (a human's Ctrl-C) cancels the session (`session/cancel`, state `cancelled`). SIGTERM (a harness killing the tool call on its own timeout — the *normal* case for an agent caller) detaches: the session keeps running under the daemon, the client prints the session id to stderr on the way out, and `wait <id>` collects the answer. When the adapter ran as a direct child because the daemon couldn't start (see `daemon`), detach is impossible — SIGTERM cancels there too.
- **`--json` means "this command's output as JSON"**, uniformly. Three shapes:
  - **Answer-printing commands** (`run`, `continue`, `wait`): a result envelope — `state`, `session_id`, `stop_reason`, `paths`, `cost`, `answer`. Two flags reshape it: `--bg` leaves only what exists at dispatch time (`session_id`, `state`, `paths`); `-o` names the output file and omits `answer`.
  - **Everything else** (`status`, `agents`, `daemon status`, `stop`, `rm`, `prune`, `install`, `--dry-run`): the same data the text view shows, as JSON.
  - **The one exception**: `log --json` emits raw transcript events (see `log`), not an envelope.
- **End-of-run summary, one line, on stderr, prefixed `--`**: duration, tokens/cost, exit status, session ID, session dir. Harnesses merge stderr into the same blob as the answer — the fixed prefix keeps it mechanically separable. `--quiet` suppresses it. A `--bg` dispatch prints none — nothing has finished; the finished `log` footer carries the same data. `log` footers follow the same rule — stderr, `--` prefix — the general principle being: when stdout carries agent content, acpc's own metadata goes to stderr; when stdout is acpc's own view (`status`, `agents`), the footer is part of the view and stays there.
- **Errors are one line and actionable**: not a stack trace, but `codex: not authenticated, run 'codex login'`.
- **Never prompt interactively on stdin.** If something is missing, fail with instructions.

## TTY vs non-TTY

Behavior differs between a human at a terminal and an agent behind a shell tool in exactly these places. "TTY" means `isatty` on stdout.

| | TTY (human) | non-TTY (agent) |
|---|-------------|-----------------|
| `--permissions` default | `prompt` | `read` |
| Permission prompting | asks on `/dev/tty` | never; out-of-policy → denied. Explicit `--permissions prompt` is a usage error (exit 2), not a silent downgrade |
| `last` selector | works | rejected — a stale "last" misleads an agent; name sessions explicitly |

`--bg` counts as non-TTY for permissions regardless of the terminal: once the client has returned, a prompt could never be answered — so the default is `read`, and explicit `--permissions prompt --bg` is the same usage error.

## State on disk

File-based state is a feature: the agent can grep it, read fragments selectively, and doesn't depend on the tool's own commands to inspect anything.

```
~/.acpc/                     # root; ACPC_HOME overrides it — deliberately the only env var acpc reads
  config.toml                # the few global knobs (retention = "90d", daemon_ttl = "30m")
  agents/<name>.toml         # variant definitions — hand-editable; `agents init` is just a scaffold
  cache/<agent>/             # advertised models, modes, commands
  daemon/<target>.log        # adapter stderr, per target
  sessions/<id>/
    meta.json                # full resolved invocation (everything --dry-run shows) + state, timing, tokens/cost, exit code, stop_reason, prompt snippet, adapter session id
    prompt.md                # the prompt as sent, latest turn; earlier turns: prompt.<n>.md
    transcript.ndjson        # full event stream (this is where "streaming" lives)
    answer.md                # final answer, latest turn; earlier turns: answer.<n>.md
```

- **`ACPC_HOME` ≠ `--home`**: the state root vs the vendor config dir a callee runs against — they share a word, nothing else.
- **Owner-only**: 0700 dirs, 0600 files — prompts and transcripts routinely carry sensitive material.
- **No torn reads**: `meta.json` is replaced atomically, `transcript.ndjson` grows by whole lines only, `cache/` files and `-o` targets are written atomically too — a mid-write reader never sees garbage. A per-session lock serializes turns, so `run`, `continue` and `stop` on one session never interleave.
- **`answer.md` is written whatever the final state**: for `failed`/`timeout`/`cancelled` it holds the partial answer; for `orphaned`, where the dead process wrote nothing, detection writes a one-line placeholder naming what died — the advertised path always exists and explains itself.
- **Turn rotation happens at the *start* of the next turn**: `continue` renames the previous `prompt.md`/`answer.md` to their `.<n>` names, then writes the new `prompt.md` — one rename per file, ever (turn numbers are fixed, no logrotate-style cascade), so a mid-turn session has no `answer.md` until the turn produces one.
- **The transcript is a public, versioned format**: a header line names the schema version, consumers ignore unknown fields. It is the programmatic layer, not the reading path — for reading, `log`, `log --prose` and `answer.md` are markdown; raw JSON costs several times more tokens than the content it carries. The markdown views are rendered on demand from the transcript, never materialized as a second on-disk copy: the only per-turn artifacts are the answers (`acpc log <id> --prose > file.md` if a file is wanted).
- **Relative paths** in flags (`--cwd`, `--prompt-file`, `-o`) resolve against the caller's working directory; `~` is expanded by acpc.

**Session states** — one vocabulary, used verbatim by `status`, `log` footers and `meta.json`:

```
starting → running → done | failed | cancelled | timeout
running → orphaned              # process behind it died; detected on any state read, never self-reported
```

- **Liveness is verified wherever state is read**, not only in `status`: every command that gates on or reports state checks the process behind a `running` session and treats a dead one as `orphaned` — a stale `running` in `meta.json` never blocks anything.
- **Detected transitions are persisted**: whichever command observes the dead process writes `orphaned` back to `meta.json` (atomic replace, under the session lock), so later readers agree without re-probing.
- **30s startup grace** from `started_at`: below it, a session with no live process still counts as `starting` — the process may not have recorded its pid yet — never a false `orphaned`.
- **`orphaned` counts as finished**: `continue` resumes it through `session/load`, `rm` and `prune` delete it, `wait` returns immediately with exit 1 and the reason.
- **Who accepts what**: `stop` acts on `starting`/`running`, is a no-op on finished states, errors on unknown IDs. `continue` accepts any finished state, errors on `running`. `wait`/`log`/`status` accept everything.

## `--help` as first-contact documentation

The recommended primary channel for usage docs is a short snippet in the caller's own context (AGENTS.md or a skill) — but the tool cannot assume it's there, so `--help` is the self-contained fallback. Two levels, one source:

- **`acpc --help`** — the cheat sheet, ≤100 lines: canonical examples grouped by task (sync run, bg run + wait, continue, status/log polling, heredoc prompt), complete for the 90% path on its own; ends with a flag → ACP mapping table, 3-4 lines (`--mode` → `session/set_mode`, `--permissions` → `request_permission`, …).
- **`acpc <cmd> --help`** — progressive disclosure: that command's full reference — synopsis, options table, semantics, one example. Generated from this spec's command section, one source, so help and contract cannot drift.
- **No stub pages**: a command whose full reference wouldn't exceed its cheat-sheet entry (`stop`, `rm`, `install`) prints the root page instead.
- `--help`/`-h` and `--version`/`-V` both accepted.

## Anti-features

Out of scope — none of these deliver value to an agent caller:

- **TUI, colors, spinners.** The caller never sees them.
- **Built-in orchestration** (pipelines, DAGs, agent teams). The caller *is* the orchestrator; loops, retries and fan-out happen in its shell.
- **Terminal streaming as a primary mode.** Append to the transcript file; the terminal shows the final answer.
- **Rich configuration system.** Anything important is a flag — flags are visible in `--help`, config state is not. `config.toml` holds housekeeping knobs only (retention), never anything that changes a call's behavior.
- **MCP server wrapping.** A plain CLI via shell is cheaper in context, standard, composable.
