# acpc Specification

What a CLI for dispatching agents (codex, claude) should look like from the perspective of its primary user: another agent calling it through a shell tool.
Normative: the implementation is adjusted to match this document; behavior changes land here first, in the same change as the code.

## How an agent consumes a CLI (design constraints)

1. **It sees command output once, at the end, as a text blob.** Anything "live" (spinners, progress, streaming) is invisible or noise. Streaming only makes sense as append-to-file, read incrementally later.
2. **Tool output limits.** Harnesses cap what a tool call returns inline; past the cap, output is diverted to a file with a short preview, or hard-truncated. Large results belong in files the tool writes itself; stdout carries the answer when short, and always the file path.
3. **The calling harness has background execution with completion notification.** A blocking sync mode is therefore the default: the caller runs `acpc run ...` in the background and gets woken when it exits. `--bg` is the second option.

## Command surface

Small verb set. The whole mental model in one sentence:
*"`run` blocks and prints the answer; `--bg` returns an ID; `status`/`log`/`wait`/`continue`/`steer`/`stop` operate on that ID; everything is on disk under a predictable path."*

```
run <agent> (prompt | - | --prompt-file) [options]   # default: block, stdout = final answer
continue <id> (prompt | - | --prompt-file)           # follow-up in the same session context
steer <id> (instruction | - | --prompt-file)         # interrupt the running turn and redirect it
status [id]                           # no id: list active + recent; with id: one session's vitals
log <id> [--since CURSOR] [--tail N] [--wait-new | --follow]   # incremental transcript access
wait <id> [--timeout S]               # block until done, print the answer
stop <id>
rm <id> | prune [--older-than D] [--dry-run]   # session cleanup (auto-prune per config retention)
agents [name] [--models|--commands|--check]   # adapters + variants; with name: resolved definition (cached)
agents init <name> --extends <agent>  # scaffold a variant
skills [name] [--json]                # bundled skills; with name: skill body and directory
install <agent>                       # one-step fix for "not installed"
daemon status|stop [target] [--force]   # plumbing escape hatch — never needed in the happy path
```

`<id>` everywhere accepts a session id or a `--name` alias; `last` works too, but only on a TTY (see *TTY vs non-TTY*). Session ids are 4 characters from a 32-glyph alphabet — lowercase letters and digits minus the ambiguous `0`/`o` and `1`/`l` — re-rolled on collision — a local handle; the adapter's own long session id stays internal, mapped in `meta.json`.

90% of usage is `run codex "do X" --cwd /path/to/repo --permissions write` and reading stdout. That path must be trivial; everything else is optional.

Command reference below is alphabetical. Sections open with their synopsis; `--json` applies uniformly (see *Output contract*) and is listed only where its semantics differ (`log`).

### `agents`

```
agents [name] [--models | --commands | --check]
agents init <name> --extends <agent> [--model M] [--effort E] [--permissions P] [--home DIR]
```

| Option | Purpose |
|--------|---------|
| `--models` / `--commands` | Dump the full advertised list. Accepted on any name — a variant delegates to its parent |
| `--check` | Live probe: launch + auth + apply the resolved options (mode/model/effort), so a config the adapter would reject fails the check rather than the next run; no prompt is sent, so model access itself still surfaces at `run` time. With name one adapter, without every installed one; one line per adapter, any failure → exit 1 |
| `init --extends <agent>` | Scaffold a variant; the flags mirror the entry's fields |

Without name: one aligned row per adapter and variant. Variants (indented) show only their delta, under a header naming its columns — model, effort, permissions, home, description (`·` = unset; home `~`-abbreviated, copy-able into `--home`); widths are computed from the rows, per *Output contract*. Adapter rows are a different shape — entry, display name, install status — and carry no header of their own, since one header cannot describe both. Status is `installed`/`missing` only; auth is not shown — cached auth state rots; the truth surfaces at `run` time as an actionable error. `agents --check` is the opt-in live probe.

With name: the resolved definition, field by field with provenance — the entry in general; one concrete call's resolution, with call-site flags applied, is `run --dry-run`.

Advertised data — modes, models, slash commands — is adapter-level (variants inherit their parent's):

- It appears in the adapter's detail view only; a variant's view ends with a pointer instead of repeating the catalogs.
- Model and command lists are capped in the adapter view (first 3 + count); modes always print in full — the list is short and this view is where legal `--mode` values come from, with no fuller view behind it. Model lists are short and curated, so `--models` prints them in full. Commands can be 50+ with paragraph-length descriptions, so each truncates to its first sentence; complete text lives in the cache file the footer names.
- `agents --models` without a name: cross-agent overview, variants collapsed to one line each.
- All of it is cached and refreshed on every real run (ACP announces it only after session creation); a run whose merged catalogs are unchanged leaves the cache file and its age untouched. On a cache miss — `agents <name>` before the first ever run — the live probe runs automatically instead of printing empty fields.
- Every view that prints advertised data ends with one cache-age footer; views built from live state alone (the bare list, a variant's resolution) have none.

```
$ acpc agents
claude  Claude Code (Anthropic)  installed
codex   Codex CLI (OpenAI)       installed
  ENTRY     MODEL         EFFORT  PERMISSIONS  HOME                 DESCRIPTION
  builder   gpt-5.6-luna  xhigh   write        ~/.codex-openrouter  Implements a task against a plan; writes the code and runs the commands the...
  explorer  gpt-5.6-luna  low     read         ~/.codex-openrouter  Answers a question, reading only.
  planner   gpt-5.6-sol   xhigh   write        ~/.codex-openrouter  Decomposes a problem into a plan.
  reviewer  gpt-5.6-sol   xhigh   read         ~/.codex-openrouter  Hunts defects in a change.

$ acpc agents builder           # what this entry resolves to
extends      codex
description  Implements a task against a plan; writes the code and runs the commands the plan calls for.
model        gpt-5.6-luna (entry)
effort       xhigh (entry)
permissions  write (entry)
home         ~/.codex-openrouter (entry)
env          MODEL_PROVIDER=openrouter (entry) · passthrough: OPENROUTER_API_KEY
-- modes/models/commands: acpc agents codex

$ acpc agents codex --commands
/init          Create an AGENTS.md file with instructions for Codex
/review        Review current changes and find issues
/$image-gen    Generate or transform bitmap image assets from text prompts or references…
/$openai-docs  Use when the user asks how to build with OpenAI products or APIs…
…
-- 47 commands (cached 2h ago) | full descriptions: ~/.acpc/cache/codex/commands.md

$ acpc agents claude            # base adapter: same view, defaults instead of overrides
adapter      Claude Code (Anthropic) · installed · claude-code-acp 0.5.1
model        claude-sonnet-5 (adapter default)
effort       high (adapter default)
permissions  prompt on TTY, read otherwise (unset)
home         ~/.claude (default)
modes        auto · default · acceptEdits · plan · dontAsk · bypassPermissions
models       4 · claude-opus-5 · claude-sonnet-5 · claude-haiku-4-5 · …   (--models for all)
commands     52 · /review · /init · /compact · …          (--commands for all)
variants     none
-- cached 30m ago

$ acpc agents claude --models
presets   TIER      MODEL             EFFORT
          fast      claude-haiku-4-5  ·
          standard  claude-sonnet-5   high
          max       claude-opus-5     max
models    claude-opus-5
          claude-sonnet-5
          claude-haiku-4-5
          claude-opus-4-8
-- cached 30m ago

$ acpc agents --models
codex
  presets   TIER      MODEL          EFFORT
            fast      gpt-5.6-luna   high
            standard  gpt-5.6-terra  xhigh
            max       gpt-5.6-sol    xhigh
  models    gpt-5.6-sol · gpt-5.6-terra · gpt-5.6-luna · gpt-5.5 · gpt-5.4
  variants  ENTRY     MODEL         EFFORT
            builder   gpt-5.6-luna  xhigh
            explorer  gpt-5.6-luna  low
            planner   gpt-5.6-sol   xhigh
            reviewer  gpt-5.6-sol   xhigh
claude
  presets   TIER      MODEL             EFFORT
            fast      claude-haiku-4-5  ·
            standard  claude-sonnet-5   high
            max       claude-opus-5     max
  models    claude-opus-5 · claude-sonnet-5 · claude-haiku-4-5 · claude-opus-4-8
-- cached: codex 2h ago · claude 30m ago

$ acpc agents init builder --extends codex --model gpt-5.6-luna --effort xhigh
```

### `continue`

```
continue <id> (prompt | - | --prompt-file F) [-o FILE] [--bg] [--timeout S] [--max-output BYTES] [--quiet]
```

| Option | Purpose |
|--------|---------|
| prompt as arg, `-` (stdin), or `--prompt-file` | Exactly one source, as in `run` |
| `-o` / `--bg` / `--timeout` / `--max-output` / `--quiet` | As in `run` — same machinery, same semantics |

Follow-up turn in an existing session, full context preserved. The caller can send feedback on the callee's own work instead of restarting from scratch.

- **Sessions are durable on the adapter side**: resume goes through ACP `session/load`, so `continue` works after the daemon expired or the machine rebooted — the daemon only makes the next turn start warm. The history `session/load` replays is already in the transcript and is not re-appended. Adapters without the `loadSession` capability fail with an actionable error.
- **`continue` on a `running` session is an error**, not a queue.
- **The turn runs with the session's stored resolution**: model, effort, permissions and home come from `meta.json`, not from re-resolving the agent entry — editing an entry never changes a session mid-conversation.
- **The early session line applies here too**: a blocking `continue` prints `-- session <id> | dir <path>` at dispatch, exactly as `run` does (see *Output contract*).

A separate verb only because `run` takes an agent and `continue` takes a session. Each turn's prompt and answer are kept on disk (see *State on disk*). A `run`-only flag here is a usage error that names the rule — `continue reuses the session's permissions — drop --permissions` — never a bare "unrecognized argument".

```
acpc continue researcher "expand section 3, it's too thin"
acpc continue last "now apply the same fix to the v2 API"   # TTY only
```

### `daemon`

Plumbing, deliberately minimal. The daemon is a performance cache — it keeps adapters warm, nothing more.

- **Auto-managed**: starts on first use, expires after an idle TTL (default `30m`, `daemon_ttl` in the global config; idle = no active sessions, so a detached session keeps its daemon alive). No `start`/`restart` verbs — `daemon stop <target>` plus the next run *is* the restart.
- **Keyed per target** (agent + home + declared env — see *Agent variants*): one daemon serves any number of sessions on its target; concurrent *turns* run up to `daemon_max_concurrent` (default 8; further turns queue and start when a slot opens, noted on stderr) — an idle or detached session holds no slot. Different homes/providers are separate targets, so fan-out never serializes.
- **The `[target]` argument** to `daemon status`/`stop` is an agent or variant name and addresses every target under it; `daemon status` lists each concrete target with its log path.
- **Two uses**: a wedged or stale daemon (`daemon stop`), and debugging (`daemon status` prints PID, uptime, idle age and the per-target log path — the only place adapter stderr goes in daemon mode). The **idle age** is the TTL's own clock: time since the target last had an active session, rendered `idle <age>` in the vocabulary session `status` uses, and `·` while the target is currently serving one. Uptime cannot answer what the view is usually opened for — a daemon reporting `up 1h48m` under a 30 m TTL looks leaked and may simply have been busy until a moment ago. The TTL measures idle time, so idle time is what says how close the daemon is to being reaped.
- **`daemon stop` refuses a target with active sessions.** A target serving sessions in state `running` or `starting` is not stopped: one error line naming the count and the ids, exit 2, nothing signalled — stopping a daemon under a live dispatch is nearly always a mistake, and the ids are exactly what the caller needs in order to `wait` or `stop` them first. Liveness is verified as everywhere else, so a session whose process is already gone reads `orphaned` and does not block the stop. When the argument addresses several targets, the guard is evaluated across all of them before anything is stopped: one blocking session refuses the whole command, because a partial stop would leave the caller guessing which half happened. `--force` stops anyway, and the sessions it takes down transition to `failed` with the reason recorded in meta — never orphaned.
- **Version skew self-heals**: a daemon that doesn't match the client version restarts itself on connect.
- **Fallback**: if the daemon cannot start at all (restricted sandboxes), `run` spawns the adapter as a direct child — visibly: the stderr summary says so, and SIGTERM then cancels instead of detaching.

```
acpc daemon status
acpc daemon stop codex          # controlled nuke; beats pkill, which kills mid-task dispatches
acpc daemon stop codex --force  # ... and take its running sessions down with it
```

### `install`

Install an adapter.

```
acpc install codex
```

### `log`

Incremental transcript view; the main progress-tracking tool.

```
log <id> [--since CURSOR] [--tail N] [--prose] [--json] [--max-output BYTES]
    [--wait-new [--timeout S]] [-f | --follow [--timeout S]] [--quiet]
```

| Option | Purpose |
|--------|---------|
| (default) | Last 20 events, condensed — the progress view: "what is it doing" |
| `--since CURSOR` | Only events after the cursor, never re-emitted; combinable with `--tail`. A cursor past the transcript's end adds one stderr note naming the highest cursor there is — `-- --since 999 is past the transcript's end (highest cursor: 45)`. A note and not a usage error: a poller that overshoots by one is doing nothing wrong and has to keep working, so stdout and the exit code are unchanged. Only an explicitly passed `--since` is checked; the implicit start points (`--wait-new`, `--follow --tail 0`) are not caller mistakes |
| `--tail N` | Just the last N of the selected events |
| `--prose` | The content view: "what is it thinking/writing" — agent messages only, untruncated, no tool lines; agents write markdown natively, so this reads as clean markdown. The event window (`--since`/`--tail`) selects; `--prose` only renders — a full-history dump is always an explicit `--since 0` |
| `--json` | Raw transcript events for `jq`, each carrying its index; not for reading — lossless inspection is `transcript.ndjson` itself. With `--prose` a usage error — one view per call |
| `--max-output <bytes>` | As in `run` (default 128 KiB, 0 disables), but applied at event granularity: whole events until the budget, then a marker line naming `transcript.ndjson`. The footer cursor covers only what was printed, so a poller never skips content; a single over-budget event is the exception — head + marker, cursor advances past it. With `--json` the stream stays valid NDJSON: truncation appears as a final typed `truncated` event naming `transcript.ndjson`, never a bare marker line |
| `--wait-new [--timeout S]` | Long-poll: block until new events appear or the timeout expires (exit 124); without `--timeout` it blocks indefinitely. Waits for *activity* (vs `wait` for completion) — enables mid-run intervention, e.g. `stop` an agent that drifted off task. After waking, the normal selection applies to the new events: `--since`, then `--tail`. On a finished session it returns immediately, the `logs -f` convention — following a stopped stream ends: anything past the cursor prints as usual, and with nothing new it exits 124 with the finished footer naming the state (a silent exit is indistinguishable from a hang); completion is `wait`'s job. A timeout on a *running* session says so on stderr — `-- still running (gave up waiting after Ns) — session continues; acpc stop <id> to cancel`. With `--follow` a usage error: one waiting mode per call |
| `-f` / `--follow [--timeout S]` | Collect events until the session ends — one bounded call in place of a hand-rolled `--wait-new` polling loop. Starts with a bounded replay for orientation: the last **10** events by default, `--tail N` to change that, `--tail 0` for new events only; an explicit `--since` resumes exactly and replays nothing. `--prose` and `--json` render as they do everywhere else, and `--max-output` budgets the whole stream, not each page. Events are rendered once, as they arrive, so a failed session's last message is not expanded the way the snapshot view expands it — the footer names the state and the answer path |
| `--quiet` | Suppress the stderr footer, as in `run` |

One chronological stream, tool calls and agent prose interleaved — the sequence is the causal narrative. Events are condensed one-liners: tool call with arg summary, result status, duration; agent message as a 200-char snippet; permission requests; errors; state changes. The snippet cuts at the last whitespace before the limit, never mid-word — a broken word is a token the reader has to repair — with a hard cut reserved for a single token longer than the limit itself. Length is reported only when it is worth knowing: at 1024 characters or more, compactly (`(2.7k chars)`), and not at all below, where it was noise on every line and the footer's token count already carries the aggregate. A `msg` or `thought` event that directly follows one of the same type continues it across no boundary (see *Output contract*), and says so with a continuation mark after its label — `msg ↪` — so a message split across several lines is not misread as several messages. Errors are never filtered — in every view; in `--prose` they keep their condensed `[time] error …` line form amid the markdown. Nor are they truncated, with one exception: `--max-output` may head-truncate a single over-budget event, errors included — the budget wins. Full content stays in `transcript.ndjson`.

```
$ acpc log x7k2 --since 42
[12:01:05] tool  Bash "pytest -x" → exit 1 (2.3s)
[12:01:20] msg   "Tests fail because the fixture assumes..." (280 chars)
[12:02:10] error permission denied: write outside cwd
-- running 3m12s | 45 events | cursor: 45

$ acpc log x7k2 --prose         # same events, the content question: full messages, no tool lines
Tests fail because the fixture assumes a clean database. Two options:

1. Reset the schema in `conftest.py` — simplest, but slows the whole suite.
2. Wrap each test in a transaction and roll back.

Going with 2; `test_auth` needs its own fixture either way.

[12:02:10] error permission denied: write outside cwd
-- running 3m12s | 45 events | cursor: 45
```

- **Footer doubles as status**: state and runtime, the range this page covered (`events 60–79 of 79` — always present, so the reader never has to work out whether history is hidden; the total is the event count, which is why it is not also printed on its own), new cursor; a finished footer adds exit code, tokens and the answer path. Footers separate unlike segments with `|`, peer items with `·`. It goes to **stderr** (prefixed `--`, like the run summary): stdout stays pure transcript content, so `log --prose > file.md` yields clean markdown, while an agent caller still sees the cursor — harnesses merge the streams. Agent prose can itself contain `--`-prefixed lines, so stream, not prefix, is what separates content from metadata.
- **Cursor = event number, stateless.** The caller carries the cursor; two pollers on one session cannot corrupt each other. The index is global across views — `--prose` and the default share one cursor space.
- **Finished session**: same output, footer becomes `-- done exit 0 | 3m12s | 41k tok | answer: <path> | events 26–45 of 45 | cursor: 45` — duration and tokens/cost included, because a `--bg` caller never sees the stderr summary; the cursor stays, so a poller's final call needs no special casing. When the state is `failed`, `timeout` or `orphaned`, the last agent message prints in full — it usually contains the reason.
- **`--follow` ends exactly three ways**, and the exit code says which: the session finished (**0**, finished footer — following a stopped stream ends, the `logs -f` convention, so a session already finished at the call returns its replay at once); `--timeout` expired (**124**, still-running footer plus the `--wait-new` timeout note — the session is untouched and keeps running); `--max-output` ran out before either (**4**, the truncation marker on stdout and, on stderr, `-- stopped: --max-output <N> exhausted — resume with: acpc log <id> --follow --since <cursor>`). The third code exists because a cut stream is not a completed follow: with 0 or 124 alone a caller cannot tell "the run is still going" from "I stopped reading it". Every ending prints the footer, whose cursor covers exactly what stdout carried, so `--since <cursor>` resumes without a gap or a repeat.
- **`--follow` is for one case**: supervising a run you intend to steer or stop mid-flight. It is not a live view — a foreground tool call returns its output when it exits, so what a caller gets is a bounded digest of what happened while it blocked. Checking in on a run is a plain `log` snapshot; waiting for a result is `wait`. Follow costs a blocked call and puts every event it collects into the caller's context, which is the expensive way to ask a question the other two answer for free.

### `prune`

```
prune [--older-than D] [--dry-run]
```

| Option | Purpose |
|--------|---------|
| `--older-than <D>` | Age threshold, e.g. `7d` |
| `--dry-run` | List what would go, delete nothing |

Delete finished sessions older than the threshold — age measured from when the session finished, not when it started. Bare `prune` — no `--older-than` — uses the config `retention` threshold; it is never "delete everything". A retention that resolves to zero makes bare `prune` a usage error naming the config key and turns the auto-prune sweep off; deleting every finished session takes an explicit `--older-than 0d`. Auto-prune: the `retention` key in the global config (default `90d`) is applied opportunistically on `run`. Running sessions are never touched.

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
run <agent> (prompt | - | --prompt-file F) [--cwd DIR] [--model M] [--effort E] [--permissions P]
    [--mode M] [--home DIR] [-o FILE] [--bg] [--timeout S] [--name ALIAS] [--dry-run]
    [--max-output BYTES] [--quiet] [--json]
```

```
acpc run codex "fix the failing test in tests/test_auth.py" --cwd ~/repo --permissions write
acpc run claude - --bg --name researcher --effort high <<'EOF'
Research X. Write findings to ./findings.md.
EOF
```

| Option | Purpose |
|--------|---------|
| prompt as arg, `-` (stdin), or `--prompt-file` | Heredoc/stdin for long prompts with quotes and backticks. Exactly one source — zero or two is a usage error naming the options; stdin is never read implicitly |
| `--cwd <dir>` | Working directory of the callee. Long flag on purpose: `-C`/`-c` invites confusion with `continue` |
| `--model <tier\|id>` | A tier (`fast`/`standard`/`max`, resolved through the adapter's preset table — see *Agent variants*) or a raw model ID from `agents <name> --models`. Explicit `--effort` overrides the preset's effort, and supplies one where the preset has none |
| `--effort <level>` | Reasoning effort, orthogonal to `--model`. Superset scale (none/minimal/low/medium/high/xhigh/max/ultra) mapped per adapter; a level the resolved model doesn't support is a hard usage error listing the supported levels — never a silent fallback |
| `--permissions all\|write\|read\|none\|prompt` | Approval policy for ACP permission requests (defined below). Default: agent entry if set, else `prompt` on a TTY and `read` otherwise; `--bg` counts as non-TTY here (see *TTY vs non-TTY*) |
| `--mode <name>` | Callee's operating mode (ACP `session/set_mode`), vendor pass-through, adapter default if omitted. Behavioral hint; a mode that suppresses permission requests is rejected unless `--permissions all` (see below). Values via `agents` |
| `--home <dir>` | Vendor home override (the dir with the vendor's config + credentials). The provider switch (see *Agent variants*); ad-hoc counterpart of a variant's `home` field |
| `-o <file>` | Write the answer to the given path; stdout then carries only a short confirmation (path, size, session id). `answer.md` in the session dir is always written regardless |
| `--bg` | Return immediately with session ID + session dir path. With `-o`, the file is written when the session finishes |
| `--timeout <s>` | Cancels the session on expiry (state `timeout`, exit 124). No default — wall-clock limits belong to the calling harness. A bare number is seconds; a suffixed value is a duration (`90s`, `5m`, `1h`, `1h30m`), the vocabulary the config file already uses. Every `--timeout` in the CLI reads the same way |
| `--name <alias>` | Human-typeable handle for `continue`/`status`/`log`. Reusing a name rebinds it to the new session with a warning — hard error while the old session is `running`. `last` is reserved |
| `--dry-run` | Print what this call would resolve to (model, effort, permissions, home, declared env, cwd — and where each value came from), then exit |
| `--max-output <bytes>` | Cap on stdout bytes (default 128 KiB, 0 disables). Truncation keeps the head, cuts on a UTF-8 boundary, and ends with a marker line naming the full answer path. The marker sits at the tail, which some harness previews clip — the stderr summary repeats the session dir, so the path always survives. Shapes stdout only: `-o` files and `answer.md` are always complete. With `--json`, truncation applies to the `answer` field and sets `truncated: true`; the envelope is always valid JSON |
| `--quiet` | Suppress acpc's own stderr lines for this call — the early session line and the end-of-run summary (see *Output contract*) |

**Early session line.** A blocking `run` prints `-- session <id> | dir <path>` to stderr at
dispatch, before the turn has produced anything, so the id is reachable mid-run — `log`,
`stop` — from output the caller has already captured. See *Output contract*.

**Permissions.** Each ACP `request_permission` is classified by the tool call's `kind`:

- `read` — allow read-only kinds: `read`, `search`, `fetch`, `think`; also `switch_mode`, unless the target mode is on the adapter's bypass list (below)
- `write` — read + `edit`, `execute`; never `delete`/`move`
- `all` — allow everything, including `allow_always` options
- `none` — deny every request
- `prompt` — read kinds auto-allowed; everything else asks the human on `/dev/tty`

Unknown kinds are denied under `read`, `write` and `none`, asked under `prompt`, allowed under `all`; denials appear in `log`. `read` and `write` answer with `allow_once` only — `allow_always` is reserved to `all`. Where the model's edges are:

- **The non-TTY default is a silent read-only trap**: a caller that neither passes `--permissions` nor runs an entry with a permission default gets a read-only callee — writes are denied without an error, the turn ends normally, exit 0, nothing changed. Pass `--permissions write` (or `all`) whenever the task is supposed to modify anything. When the policy was defaulted, the end-of-run summary counts what it denied and names the flag to pass (see *Output contract*).
- **`execute` subsumes `delete`/`move`** in practice — excluding those kinds only constrains adapters that classify honestly.
- **`fetch` is network egress** — under the non-TTY default a callee processing untrusted input can reach the network; use `none` when that matters.
- **An approval policy, not a sandbox**: it answers the requests the adapter emits, so a `--mode` that stops the callee from asking (vendor bypass modes) would evade it — such combinations are rejected at parse time unless `--permissions all`. A real boundary means confining the adapter itself: a container, a dedicated user, or the vendor's own sandbox.
- **The same door exists at runtime**: a callee can *request* a mode switch (kind `switch_mode`), so a switch into a bypass mode is treated like an unknown kind — denied below `all`, asked under `prompt` — while switches between ordinary modes (e.g. plan → default) stay in the read tier.
- **Bypass lists are adapter-declared**: ACP does not mark modes as bypass; a vendor mode absent from the adapter definition's list passes both guards until the definition is updated.

**Slash commands, skills and prompt-defined agents** need no flag: the callee resolves them from the prompt body — `run claude "/commit"` just works. They resolve against the vendor home the callee runs with (`--home`/entry), not against `--cwd`. The flip side: an unknown command comes back as ordinary agent output ("Unknown command: /x") with exit 0 — the exit code cannot tell the caller it never existed; `agents <name> --commands` shows what's advertised.

Not needed: file-attachment flags (paths in the prompt suffice), system-prompt injection.

### `status`

```
status [id] [--all]
```

| Option | Purpose |
|--------|---------|
| `--all` | Every session, not just running + the 5 most recent finished; with an id it's a usage error |

Without id: one line per session — id, the entry it ran on (variant or adapter), the model it resolved to, state, runtime, idle age, name, prompt snippet (five backgrounded codex runs must not look identical). Defaults to all running + the 5 most recent finished.

The **resolved model** is the one the session actually ran on, read from the resolution stored in `meta.json` at dispatch. Entry names hide this: a variant inherits its model through `extends`, so `builder` and `explorer` can both be running `gpt-5.6-luna` while `reviewer` runs `gpt-5.6-terra`, and nothing in the entry name says so. Every session resolves a model — an adapter default counts — so the column is populated in practice; a session whose `meta.json` predates this field or lost it to a torn write renders `·` rather than failing the view.

The **idle age** is the time since the session's newest transcript event, shown on active sessions and `·` on finished ones. Runtime alone cannot tell a slow turn from a hung one; an age that keeps growing while the state stays `running` is the signal that something is stuck. It is a fixed-cost read of the transcript's tail — the last complete line, never a parse of the stream — and it is read-only: a damaged or torn transcript yields no age rather than an error or a repair.

With id: one session's vitals — state (exit code once finished), runtime, idle age while active, tokens/cost so far (cumulative across the session's turns), the entry with its base adapter and resolved model, name, session dir and answer path.

A pulse, not a dump: reads `meta.json`, process liveness and the transcript's last line — never the event stream, so the cost per session is fixed no matter how long the run got. "What is it doing right now" is still `log <id> --tail 1`; `status` answers only "is it still moving". State is verified, not trusted: a `running` session whose daemon or adapter is gone reports `orphaned`, never a stale `running` (see *Session states*).

```
$ acpc status
ID    ENTRY     MODEL          STATE    RUNTIME  IDLE   NAME         PROMPT
x7k2  codex     gpt-5.6-terra  running  3m12s    0m04s  ·            "Fix the failing test in tests/test_auth.py"
p9d4  claude    claude-opus-5  running  0m41s    0m38s  researcher   "Research X and write findings to ./findings…"
kq8w  reviewer  gpt-5.6-terra  done     12m40s   ·      spec-review  "Review the diff against the spec and report…"
b3nn  codex     gpt-5.6-terra  failed   2m05s    ·      ·            "Summarize the repository changes"
m2w7  claude    claude-opus-5  done     8m19s    ·      docs         "Update the README quick-start for the new CLI"
ze6a  codex     gpt-5.6-terra  timeout  30m00s   ·      ·            "Migrate the config loader to TOML and run the…"
q4hf  builder   gpt-5.6-luna   done     22m03s   ·      ·            "Implement the session lock and its tests per…"
-- 2 running · 5 recent · --all for all 17

$ acpc status kq8w
state    done · exit 0 · 12m40s · 41k tok
agent    reviewer (codex) · model: gpt-5.6-terra · name: spec-review
dir      ~/.acpc/sessions/kq8w · answer: answer.md
```

### `steer`

```
steer <id> (instruction | - | --prompt-file F) [-o FILE] [--bg] [--timeout S] [--max-output BYTES] [--quiet]
```

| Option | Purpose |
|--------|---------|
| instruction as arg, `-` (stdin), or `--prompt-file` | Exactly one source, as in `run` |
| `-o` / `--bg` / `--timeout` / `--max-output` / `--quiet` | As in `continue` — same machinery, same semantics |

Interrupt the turn a session is running and redirect it, as one verb: `session/cancel`, wait for the `cancelled` ack, then start the next turn carrying the instruction. A caller watching a callee drift off task would otherwise have to `stop` it, notice that it stopped, and `continue` it by hand — three calls with a race in the middle.

- **Interrupt-based, because ACP has no mid-turn channel.** Turns are sequential and only `session/cancel` reaches a running one; `session/prompt` mid-turn is protocol-undefined. Vendor engines do support mid-task input internally, but adapters cannot expose it over ACP. Should ACP grow such a channel, `steer` keeps its name and swaps the composite for injection.
- **The instruction is wrapped in a fixed preamble** naming the interruption, so the callee reads a redirect as a redirect and not as a fresh unrelated task:

  ```
  Your previous turn was interrupted by the operator; this instruction takes precedence:

  <instruction>
  ```

  The wrapped text is what `prompt.md` stores, verbatim — what was sent is what is on disk.
- **Nothing is lost**: the transcript keeps everything, and the interrupted turn's partial answer is parked as that turn's `answer.<n>.md` by the normal rotation. The session's stored resolution is reused, exactly as `continue` reuses it.
- **A finished session is a usage error** naming `continue` — there is no turn to interrupt.
- **Race with a natural finish**: a turn that ends on its own before the cancel lands degrades to a plain `continue` — no preamble, because nothing was interrupted — and says so on stderr.

**Checkpoint is a recipe, not a verb.** "Tell me where you are" needs no new surface:

```
acpc steer x7k2 "Summarize: done / hypothesis / next step / blockers — then stop"
```

A dedicated verb would be speculative API, and the name would oversell: "checkpoint" sounds free while the mechanics still cancel work in flight. The zero-cost, read-only alternative is `log <id> --prose`, which asks the callee for nothing at all.

```
acpc steer x7k2 "Stop editing; diagnose only and report what you found"
```

### `stop`

Stop a running session. Graceful (ACP `session/cancel`) with a bounded wait for the ack (10s) — if the callee doesn't wind down in time, the connection is torn down anyway. Transcript, meta and partial answer stay on disk for post-mortem. A hard variant, if ever needed, is `stop --force`, not a new verb.

```
acpc stop x7k2
```

### `wait`

```
wait <id> [--timeout S] [-o FILE] [--max-output BYTES] [--quiet]
```

| Option | Purpose |
|--------|---------|
| `--timeout <s>` | Stops *waiting* only (exit 124): the session keeps running, unlike `run --timeout`, which cancels it — and the exit says so on stderr (`-- still running (gave up waiting after Ns) — session continues; acpc stop <id> to cancel`). Seconds or a suffixed duration, as in `run`; absent, it blocks indefinitely |
| `-o` / `--max-output` / `--quiet` | As in `run` — `wait` prints an answer, so it shapes it the same way |

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
env_passthrough = ["OPENROUTER_API_KEY"]   # names read from the caller's env at call time

[env]                                      # literal values declared in the entry
MODEL_PROVIDER = "openrouter"

$ acpc run builder "implement the parser per SPEC.md"
# ≡ acpc run codex … --model gpt-5.6-luna --effort xhigh --permissions write --home ~/.codex-openrouter
```

The `home` field is also the provider dimension: OpenAI vs OpenRouter vs a local endpoint is just a different vendor home (own config, own credentials). A variant is the named, permanent form; `--home` on `run` the one-off form.

**`description`** is optional on any entry, adapter or variant, and takes any string the operator writes — any length, newlines included. A roster reading `builder`, `explorer`, `planner` says nothing about what any of them is *for*; that is the operator's knowledge, not inferable from `--help`, and it belongs in the entry rather than in external documentation that goes stale. **The config never rejects it and never truncates it on disk**: a purely informational field must not be able to break a working dispatch, so context protection lives in the view rather than in the parser. The `agents` list normalizes whitespace and cuts at a word boundary within an 80-character budget, the same cut the condensed `log` view uses; `agents <name>` shows the description verbatim and in full, and `--json` carries the whole value — truncation shapes the text list and nothing else. It is **not inherited through `extends`** — a variant's purpose is its own, and rendering the parent's text under a child's name would be a confident lie about what the child does. Absence renders as absence everywhere: nothing in the list row, no line in the detail view, `null` in `--json`.

Presets are adapter-level: each adapter definition ships its `fast`/`standard`/`max` table — what `--model <tier>` resolves through and `agents <name> --models` prints. `model` is required; **`effort` is optional, because effort is a property of the model rather than of the tier.** A vendor may expose no effort setting for a given model — claude CLI ≥2.1.224 offers none for Haiku 4.5 while keeping it for Sonnet and Opus — and a tier that pins such a model omits `effort` entirely, meaning the model runs at its own built-in level. Where an effort is present it is validated like any other. Overriding what a tier means uses the same mechanism as everything else — a `[presets]` table in a file under `agents/` for that adapter — never `config.toml`, so resolution stays inspectable with provenance like every other field. Tiers left out keep the adapter's shipped pair.

A model with no effort setting is listed exactly that way: `agents <name> --models` and the cross-agent overview render `·` in the effort column and `--json` carries `null`, absence as absence. Asking for one anyway stays a loud failure rather than a silent downgrade — an explicit `--effort` the adapter rejects fails the turn, and the error names the resolved model as the likely reason it has no such setting. A preset effort the adapter rejects fails the same way: it means the entry TOML has gone stale against the vendor, and a one-line fix to a file beats a runtime capability probe that adapts silently.

```toml
# ~/.acpc/agents/codex.toml — same override mechanism, aimed at the base adapter
[presets]
fast = { model = "gpt-5.6-luna", effort = "high" }
max  = { model = "gpt-5.6-sol",  effort = "xhigh" }
# effort omitted: this model has no effort setting to give it
turbo = { model = "gpt-5.6-nova" }
```

Environment is part of the entry, in two fields. An `[env]` table holds literal values declared in the entry (e.g. the vendor home path). `env_passthrough` lists variable *names* read from the caller's environment at call time — values are never stored on disk, which is how API keys travel. Both feed the daemon target key ("declared env"): `[env]` by name and value, `env_passthrough` by name *and the value read at call time* — hashed into the key, still never stored — so two entries with different env, or two callers holding different credentials, are separate targets that cannot serve each other's traffic (nobody silently rides on the first caller's API key).

The adapter's environment is constructed, not inherited — but not paranoid-empty either. Three layers reach it: a base system set (`HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL`), capability variables passed through from the caller (`SSH_AUTH_SOCK`; `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` in both cases; `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`) so tools on PATH, proxies and ssh keep working, and the entry's declared env on top. The rest of the ambient environment never reaches the adapter. Capability variables are passed but not part of the target key — a long-lived daemon may hold the first caller's proxy or agent socket; stop the daemon when that must change.

Entry TOMLs are trusted at the level of shell config: an adapter definition names the command acpc executes and the env delivered to it. Only place files you trust in `agents/`.

## Bundled skills

`acpc skills` lists the skills bundled in the package; `acpc skills <name>` prints one skill's body. The source is bundled-only: readable `SKILL.md` files directly under `data/skills/*` are served, and the directory name wins over any frontmatter `name` — it is what the filesystem can be trusted about, and it is what `skills <name>` takes. In the detail view the body is verbatim on stdout, byte-identical to the file below the frontmatter, and the skill directory rides on stderr as `-- skill <name> | dir <path>` — which is how a caller finds `references/` without a flag for it. Both views accept `--json`; the list emits `{"skills": [...]}` like every other list view, and the detail view adds `body`. An unknown name is a usage error (exit 2) pointing at `acpc skills`.

## Output contract

- **stdout carries exactly one thing, chosen by flags**: the answer (default), a short confirmation (`-o`), a JSON envelope (`--json`), a session ID + dir path (`--bg`). Never spinners, ANSI, logs or diagnostics — those go to stderr or the session log file.
- **Every column is labeled, once — in a header or on the value, never nowhere.** A multi-row positional view prints one uppercase header line above its rows, on stdout with them (the `docker ps` prior — uppercase is what makes the label row readable as a label row at a glance; block labels like `presets` and inline-labeled values stay lowercase): `status`'s list, the `agents` list's variant rows, the preset table in `agents <name> --models`. A view that labels each value inline instead — `daemon status`'s `pid 728419 · up 36m53s · idle 8m39s` — is already labeled and gets no header. What is never acceptable is a column whose meaning lives nowhere: a bare `·` in an unlabeled position is unreadable on first contact, and this tool's first-contact reader is usually an agent that cannot ask. Detail views are exempt (one labeled field per line already), as is the `log` stream (each event is self-describing).
- **Column widths are computed from the rendered rows, never fixed.** Each view measures what it is about to print and pads to the widest value, header included. A hardcoded width is aligned only for the values that existed when it was written: a 26-character model id or a 14-character entry name shears every column after it, and the row that most needs reading — the unusual one — is the row that breaks. One long value widens the table; it never misaligns it.
- **A *message boundary* is where one agent message ends and the next begins**: any non-message update — a tool call, a thought chunk, a usage report, anything that is not an `agent_message_chunk` — arriving between two message chunks. Chunks with nothing between them are one message being streamed, however long the pause. The rule reads the update stream and never the clock, so a slow adapter cannot invent a boundary and a fast one cannot lose a real one. Two views consume this one definition: the answer separates messages at a boundary (below), and the condensed `log` view marks the events that continue across one (see `log`).
- **"The answer" is defined**: the turn's ACP agent-message content, chunks concatenated in stream order; thought chunks and tool output excluded; markdown passed through verbatim. Narration interleaved between tool calls is part of it — never silently dropped, and never silently glued to what follows: at every message boundary the answer carries a blank line. stdout and `answer.md` carry identical bytes; for a single-turn session, `log --prose --since 0` renders the same content.
- **Fixed exit codes** (Unix conventions; finer-grained ACP `stop_reason` lives in `meta.json` and the `--json` envelope):

  | Code | Meaning |
  |------|---------|
  | 0 | success (`end_turn`) |
  | 1 | agent error — crash, `refusal`, `max_tokens`, missing auth |
  | 2 | usage error — bad flags, unknown session, rejected mode/permissions combination |
  | 4 | output budget exhausted — `log --follow` stopped because `--max-output` ran out before the session ended; the footer's cursor covers what was printed and `--since` resumes from it |
  | 124 | timeout (`run`: session cancelled; `wait`/`log --wait-new`: nothing new within the window — a running session keeps running and the exit says so on stderr; a finished session returns at once) |
  | 130 | cancelled — SIGINT or `stop`. Answer-printing commands mirror the session result, so `wait` on a cancelled session also exits 130, whoever cancelled it and whenever; the finer distinction lives in `stop_reason` |
  | 141 / 143 | SIGPIPE / SIGTERM (SIGTERM detaches — see below) |

- **Client death ≠ session death.** SIGINT (a human's Ctrl-C) cancels the session (`session/cancel`, state `cancelled`). SIGTERM (a harness killing the tool call on its own timeout — the *normal* case for an agent caller) detaches: the session keeps running under the daemon, and on the way out the client prints exactly `-- detached, still RUNNING: <id> — answer: acpc wait <id> · cancel: acpc stop <id>` to stderr, so the caller that killed the tool still learns both the id and its options. When the adapter ran as a direct child because the daemon couldn't start (see `daemon`), detach is impossible — SIGTERM cancels there too.
- **`--json` means "this command's output as JSON"**, uniformly. Three shapes:
  - **Answer-printing commands** (`run`, `continue`, `wait`): a result envelope — `state`, `session_id`, `stop_reason`, `paths`, `cost`, `answer`. Two flags reshape it: `--bg` leaves only what exists at dispatch time (`session_id`, `state`, `paths`); `-o` names the output file and omits `answer`.
  - **Everything else** (`status`, `agents`, `daemon status`, `stop`, `rm`, `prune`, `install`, `--dry-run`): the same data the text view shows, as JSON.
  - **The one exception**: `log --json` emits raw transcript events (see `log`), not an envelope.
- **End-of-run summary, one line, on stderr, prefixed `--`**: duration, tokens/cost, exit status, session ID, session dir. Harnesses merge stderr into the same blob as the answer — the fixed prefix keeps it mechanically separable. The prefix only separates at a line boundary, and answers need not end with a newline, so when stdout's last line is unterminated the stderr metadata that follows leads with a newline of its own — on stderr, never appended to stdout, which stays byte-identical to `answer.md`. When permission denials occurred under a **defaulted** policy — the caller passed no `--permissions` and the entry had none, so the TTY rule chose it — the summary adds a segment: `denied: 3 write (default read policy — pass --permissions write)`. The silent read-only trap, made visible where it bit. An explicitly chosen policy denies quietly — that is the policy doing its job. The tally is per turn. `--quiet` suppresses it. A `--bg` dispatch prints none — nothing has finished; the finished `log` footer carries the same data. `log` footers follow the same rule — stderr, `--` prefix — the general principle being: when stdout carries agent content, acpc's own metadata goes to stderr; when stdout is acpc's own view (`status`, `agents`), the footer is part of the view and stays there.
- **Early session line, on blocking `run`/`continue`**: at dispatch — before the turn has produced anything — one stderr line, `-- session <id> | dir <path>`. Its segments are identical in form to the end-of-run summary's own `session <id>` and `dir <path>` segments; harnesses merge both streams into one blob, so one spelling has to serve whether it is read at the start or at the end. It is what makes a blocking call self-sufficient: the id is in the captured output from the first moment, so `log` and `stop` work mid-run and a call the harness kills on its own timeout leaves a session the caller can still find rather than an orphan. The client prints it before the turn starts, so it is the same on the daemon path and on the direct-child fallback. `--bg` does not print it — stdout already carries the id and the dir — and `--quiet` suppresses it exactly as it suppresses the summary. stdout is untouched and stays byte-identical to `answer.md`.
- **Errors are one line and actionable**: not a stack trace, but `codex: not authenticated, run 'codex login'`. Damaged state gets the same treatment — an unparseable `meta.json` or transcript produces one line naming the file and a non-zero exit, never a traceback. Known spellings from neighboring tools get the same treatment instead of a bare "no such option": `-d`/`--detach` → `--bg`, `-C` → `--cwd`, and the command `logs` → `log` — each a usage error naming the acpc spelling. `-f`/`--follow` is a real flag on `log`; on any other command it gets the same hint, pointing at `log --follow`. The `daemon` group answers the docker/systemctl vocabulary the same way: `daemon list`/`ls`/`ps` name `daemon status`; `daemon stop --all` says that bare `daemon stop` already addresses every daemon; `daemon start`/`restart` give the recipe instead — daemons start on first use, so `daemon stop <agent>` plus the next run is the restart. Hints, never working aliases: a second spelling that works is a second name for one operation, and the point of answering a wrong guess is to teach the right one.
- **Never prompt interactively on stdin.** If something is missing, fail with instructions.

## TTY vs non-TTY

Behavior differs between a human at a terminal and an agent behind a shell tool in exactly these places. "TTY" means `isatty` on stdout. Redirection flips it: a human running `acpc run … > out.md` is non-TTY and gets the `read` default.

| | TTY (human) | non-TTY (agent) |
|---|-------------|-----------------|
| `--permissions` default | `prompt` | `read` |
| Permission prompting | asks on `/dev/tty` | never; out-of-policy → denied. A `prompt` policy — from the explicit flag or an entry's `permissions` — is a usage error (exit 2), not a silent downgrade |
| `last` selector | works | rejected — a stale "last" misleads an agent; name sessions explicitly |

`--bg` counts as non-TTY for permissions regardless of the terminal: once the client has returned, a prompt could never be answered — so the default is `read`, and explicit `--permissions prompt --bg` is the same usage error.

## State on disk

File-based state is a feature: the agent can grep it, read fragments selectively, and doesn't depend on the tool's own commands to inspect anything.

```
~/.acpc/                     # root; ACPC_HOME overrides it — the only env var that configures acpc itself
  config.toml                # global knobs — the complete file just below
  agents/<name>.toml         # variants, adapter overrides, new adapters — hand-editable; `agents init` is just a scaffold
  cache/<agent>/             # advertised models, modes, commands
  daemon/<entry>~<hash>.log  # adapter stderr per concrete target; daemon sockets and locks live here too
  sessions/<id>/
    meta.json                # full resolved invocation (everything --dry-run shows) + state, timing, tokens/cost, exit code, stop_reason, prompt snippet, adapter session id
    prompt.md                # the prompt as sent, latest turn; earlier turns: prompt.<n>.md
    transcript.ndjson        # full event stream (this is where "streaming" lives)
    answer.md                # final answer, latest turn; earlier turns: answer.<n>.md
```

```toml
# ~/.acpc/config.toml — the complete configuration surface, deliberately
retention = "90d"           # auto-prune finished sessions older than this
daemon_ttl = "30m"          # idle daemon lifetime
daemon_max_concurrent = 8   # concurrent turns per daemon target
```

That is the whole file. Anything that changes a call's behavior lives in flags or agent entries (see *Anti-features*) — in particular, the adapter env pass-through list is not configurable here: extensions go through an entry's `env_passthrough`.

- **Adapter definitions are TOMLs shipped in the package**, one per adapter — the full contract: `command`, `install_command`, default `home`, `home_env` (the vendor variable the resolved home is exported as, e.g. `CODEX_HOME`), the bypass-mode list, `[presets]`, supported effort levels, `env_passthrough`. A user file in `agents/` with `extends` is a variant; under an adapter's own name it overrides that adapter's fields (e.g. `[presets]`); with a `command` and no `extends` it defines a new adapter. All at the trust level *Agent variants* states.
- **`ACPC_HOME` ≠ `--home`**: the state root vs the vendor config dir a callee runs against — they share a word, nothing else.
- **Owner-only**: 0700 dirs, 0600 files — prompts and transcripts routinely carry sensitive material.
- **No torn reads**: `meta.json` is replaced atomically, `transcript.ndjson` grows by whole lines only, `cache/` files and `-o` targets are written atomically too — a mid-write reader never sees garbage. A per-session lock serializes turns, so `run`, `continue` and `stop` on one session never interleave.
- **`answer.md` is written whatever the final state**: for `failed`/`timeout`/`cancelled` it holds the partial answer; for `orphaned`, where the dead process wrote nothing, detection writes a one-line placeholder naming what died — the advertised path always exists and explains itself.
- **Turn rotation happens at the *start* of the next turn**: `continue` renames the previous `prompt.md`/`answer.md` to their `.<n>` names, then writes the new `prompt.md` — one rename per file, ever (turn numbers are fixed, no logrotate-style cascade), so a mid-turn session has no `answer.md` until the turn produces one.
- **The transcript is a public, versioned format**: a header line names the schema version (`acpc.transcript/1`); every event line carries a global 1-based index `i` (continuous across turns — this is the `log` cursor), a timestamp, and a `type` from `msg | thought | tool | permission | error | state | usage` plus type-specific fields; consumers ignore unknown fields. An event is a readable unit, not a wire chunk: adapters stream word-sized message fragments, and consecutive same-type fragments coalesce into one `msg`/`thought` event, cut by whatever comes first — a different event type, a pause in the stream, a bounded age (so a long uninterrupted message still surfaces while running), a size bound, or the end of the turn. It is the programmatic layer, not the reading path — for reading, `log`, `log --prose` and `answer.md` are markdown; raw JSON costs several times more tokens than the content it carries. The markdown views are rendered on demand from the transcript, never materialized as a second on-disk copy: the only per-turn artifacts are the answers (`acpc log <id> --prose > file.md` if a file is wanted).
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
- **Who accepts what**: `stop` acts on `starting`/`running`, is a no-op on finished states, errors on unknown IDs. `continue` accepts any finished state, errors on `running`. `rm` errors on `starting`/`running`; `prune` never touches them. `wait`/`log`/`status` accept everything.

## `--help` as first-contact documentation

The recommended primary channel for usage docs is a short snippet in the caller's own context (AGENTS.md or a skill) — but the tool cannot assume it's there, so `--help` is the self-contained fallback. Two levels, one source:

- **`acpc --help`** — the cheat sheet, ≤100 lines, complete for the 90% path on its own. Grouped by the decision the caller is actually making, in the order they make it: short task (blocking) · long or uncertain task (`--bg` + `wait`) · checking on a run · supervising one they intend to steer or stop (`--follow`) · steering · continuing · heredoc prompt · context care · maintenance and setup. Write-task examples carry `--permissions write`. Each group names the cost or the failure it prevents, not just the syntax — the sheet is where an agent learns that `wait` already prints the answer and the file is the fallback for a truncated or huge one, that `--follow` is for one case, and that killing `acpc` does not stop the session. Ends with the command list and a flag → ACP mapping table, 3-4 lines (`--mode` → `session/set_mode`, `--permissions` → `request_permission`, …).
- **`acpc <cmd> --help`** — progressive disclosure: that command's full reference — synopsis, options table, semantics, one example.
- **Every command has a real page**: the short verbs too — `stop`, `rm` and `install` document their synopsis, the state rules that govern them, and one example; the root page keeps their one-liners so first contact never dead-ends.
- **Every option documents itself**: a help string, always — no option is ever a bare metavar — plus its default. An option with a real default value shows it (`[default: 131072]`); an option whose *absence* means a behavior names that behavior instead (`--timeout` absent blocks indefinitely; `log` with no `--since`/`--tail` shows the last 20 events; `--permissions` absent applies the TTY/non-TTY rule). The bar this sets: a caller can price a call's context cost and predict its no-flag behavior from `-h` alone, without reading this document.
- `--help`/`-h` and `--version`/`-V` both accepted.

## Anti-features

Out of scope — none of these deliver value to an agent caller:

- **TUI, colors, spinners.** The caller never sees them.
- **Built-in orchestration** (pipelines, DAGs, agent teams). The caller *is* the orchestrator; loops, retries and fan-out happen in its shell.
- **Terminal streaming as a primary mode.** Append to the transcript file; the terminal shows the final answer.
- **Rich configuration system.** Anything important is a flag — flags are visible in `--help`, config state is not. `config.toml` holds housekeeping knobs only (retention, daemon TTL and capacity), never anything that changes a call's behavior.
- **MCP server wrapping.** A plain CLI via shell is cheaper in context, standard, composable.
- **No user skills directory, no install, no scopes, no marketplace.** `acpc` serves what it ships; a skill of the operator's own belongs in their harness's own skills directory. Adding a second source is a SPEC change, not an implementation detail.
- **A bundled skill is not meant to be installed into a harness's global skills directory either.** It would then sit in every session's roster in every project, paying its description in context each time, for a task run a couple of times a year. This command exists so that trade never has to be made.
