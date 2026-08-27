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
probe <entry> --discover [--json]     # the adapter's advertised modes, diffed against the entry
skills [name] [--json]                # bundled skills; with name: skill body and directory
install <agent>                       # one-step fix for "not installed"
daemon status|stop [target] [--force]   # plumbing escape hatch — never needed in the happy path
```

`<id>` everywhere accepts a session id or a `--name` alias; `last` works too, but only on a TTY (see *TTY vs non-TTY*). Session ids are 4 characters from a 32-glyph alphabet — lowercase letters and digits minus the ambiguous `0`/`o` and `1`/`l` — re-rolled on collision — a local handle; the adapter's own long session id stays internal, mapped in `meta.json`.

90% of usage is `run codex "do X" --cwd /path/to/repo --permissions execute` and reading stdout. That path must be trivial; everything else is optional.

Command reference below is alphabetical. Sections open with their synopsis; `--json` applies uniformly (see *Output contract*) and is listed only where its semantics differ (`log`).

### `agents`

```
agents [name] [--models | --commands | --check]
agents init <name> --extends <agent> [--model M] [--effort E] [--mode MODE] [--permissions P] [--home DIR]
```

| Option | Purpose |
|--------|---------|
| `--models` / `--commands` | Dump the full advertised list. Accepted on any name — a variant delegates to its parent |
| `--check` | Live probe: launch + auth + apply the resolved options (mode/model/effort), so a config the adapter would reject fails the check rather than the next run; no prompt is sent, so model access itself still surfaces at `run` time. With name one adapter, without every installed one; one line per adapter, any failure → exit 1 |
| `init --extends <agent>` | Scaffold a variant; the flags mirror the entry's fields |

Without name: one aligned row per adapter and variant. Variants (indented) show only their delta, under a header naming its columns — model, effort, permissions, home, description (`·` = unset; home `~`-abbreviated, copy-able into `--home`); widths are computed from the rows, per *Output contract*. Adapter rows are a different shape — entry, display name, install status — and carry no header of their own, since one header cannot describe both. Status is `installed` or `missing`; a missing adapter that defines `install_command` appends `→ acpc install <name>`, one that instead names `install_docs` appends that URL, and one with neither stays `missing`. Never `acpc install` for an entry that cannot run it. Auth is not shown — cached auth state rots; the truth surfaces at `run` time as an actionable error. `agents --check` is the opt-in live probe.

With name: the resolved definition, field by field with provenance — the entry in general; one concrete call's resolution, with call-site flags applied, is `run --dry-run`.

Advertised data — modes, models, slash commands — is adapter-level (variants inherit their parent's):

- It appears in the adapter's detail view only; a variant's view ends with a pointer instead of repeating the catalogs.
- Model and command lists are capped in the adapter view (first 3 + count); modes always print in full, each with its `grants`, whether it delegates and whether it escalates — the list is short, and this view is the only place a caller can see which modes a given policy admits. Model lists are short and curated, so `--models` prints them in full. Commands can be 50+ with paragraph-length descriptions, so each truncates to its first sentence; complete text lives in the cache file the footer names.
- `agents --models` without a name: cross-agent overview, variants collapsed to one line each.
- All of it is cached and refreshed on every real run (ACP announces it only after session creation); a run whose merged catalogs are unchanged leaves the cache file and its age untouched. On a cache miss — `agents <name>` before the first ever run — the live probe runs automatically instead of printing empty fields.
- Every view that prints advertised data ends with one cache-age footer; views built from live state alone (the bare list, a variant's resolution) have none.

```
$ acpc agents
claude  Claude Code (Anthropic)  installed
codex   Codex CLI (OpenAI)       installed
  ENTRY     MODEL         EFFORT  PERMISSIONS  HOME                 DESCRIPTION
  builder   gpt-5.6-luna  xhigh   execute      ~/.codex-openrouter  Implements a task against a plan; writes the code and runs the commands the...
  explorer  gpt-5.6-luna  low     read         ~/.codex-openrouter  Answers a question, reading only.
  planner   gpt-5.6-sol   xhigh   execute      ~/.codex-openrouter  Decomposes a problem into a plan.
  reviewer  gpt-5.6-sol   xhigh   read         ~/.codex-openrouter  Hunts defects in a change.

$ acpc agents builder           # what this entry resolves to
extends      codex
description  Implements a task against a plan; writes the code and runs the commands the plan calls for.
model        gpt-5.6-luna (entry)
effort       xhigh (entry)
mode         · (unset)
permissions  execute (entry)
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
permissions  ask on TTY, read otherwise (unset)
home         ~/.claude (default)
modes        6 · default (read · delegates) · acceptEdits (execute · delegates) · plan (edit · delegates)
             auto (all · escalates) · dontAsk (none) · bypassPermissions (all)
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
continue <id> (prompt | - | --prompt-file F) [--permissions P] [-o FILE] [--bg] [--timeout S] [--max-output BYTES] [--quiet]
```

| Option | Purpose |
|--------|---------|
| prompt as arg, `-` (stdin), or `--prompt-file` | Exactly one source, as in `run` |
| `--permissions none\|read\|edit\|execute\|all\|ask` | New policy for this and later turns. Without it, the stored policy is reused; with it, mode selection runs against the adapter's current `[modes]`. The run default is `ask` on a TTY and `read` otherwise; `--bg` counts as non-TTY. `write` and `prompt` are deprecated aliases |
| `-o` / `--bg` / `--timeout` / `--max-output` / `--quiet` | As in `run` — same machinery, same semantics |

Follow-up turn in an existing session, full context preserved. The caller can send feedback on the callee's own work instead of restarting from scratch.

- **Sessions are durable on the adapter side**: a cold resume prefers ACP `session/resume` where the adapter advertises it and falls back to `session/load`, so `continue` works after the daemon expired or the machine rebooted — the daemon only makes the next turn start warm. Observable behavior is identical either way; the preference exists because ACP v2 folds `session/load` into `session/resume`, and an adapter offering neither fails with an actionable error.
- **Replay is consumed silently.** `session/load` requires the adapter to replay the whole conversation, and that replay updates nothing user-visible: not `answer.md`, not the transcript, not the stderr stream, not the token or cost tally. The history is already in the transcript from when it happened, and re-appending it would double every earlier turn in the one file a caller reads for the answer. The rule holds on both routes — the warm daemon and the direct child — so a cold `continue` and a warm one produce the same `answer.md`. `session/resume` replays nothing at all, which is the same promise reached more cheaply.
- **A cold resume is verified before the new prompt is sent.** Reattaching by id is a claim, not a fact: an adapter may have rotated its store, or the id may name a conversation that is no longer the one acpc recorded, and a turn dispatched on the wrong conversation is worse than no turn at all — the callee answers confidently out of someone else's context. So acpc checks, always, with no flag to ask for it. Where the adapter can list its sessions, the stored adapter session id must be present with a matching `cwd`. Where the restore replays the conversation, the replayed *user* messages must contain the session's stored prompts as an ordered subsequence — ordered because turns happen in order, a subsequence rather than an exact match because the replay legitimately carries messages acpc never stored: a rolled-back turn comes back in the replay anyway (codex-acp#355), and a session can be prompted out of band. Extra messages are therefore not evidence of a mixup; a stored prompt that is missing, or one that arrives out of order, is. Only the user side is compared: agent text, thoughts, tool calls, ids and event counts all differ between adapters, across versions and between runs, and comparing them would fail honest resumes. Prompt text is compared exactly, because a prompt's own whitespace is part of it — indentation carries meaning in code, YAML and markdown, and a comparison that folds it away would accept a prompt that says something else.
- **Only prompts acpc knows it delivered are compared, and each check stands alone.** A turn can store its prompt and then die before the adapter ever receives it; requiring that prompt afterwards would make the session permanently unresumable, which is a worse failure than the one the check exists to prevent. So acpc records which prompts crossed the boundary and verifies against that record, never against prompts it merely wrote down. The two checks are independent: each runs when it can and neither implies the other, so an adapter that lists but does not replay is verified by the listing alone rather than failed for the replay it never promised. A check that cannot run is not a failure — an adapter that neither lists nor replays is resumed unverified, because refusing would make `continue` unusable against conforming adapters, and today, with `session/resume` replaying nothing, that is the ordinary case rather than an exotic one.
- **A delivery record that does not account for every prompt cannot verify a resume.** acpc compares a resume against the prompts it recorded as delivered, so that record has to be complete or known not to be. It can fall short two ways: the write can fail, and the connection can offer no way to observe the outgoing prompt at all — in which case nothing is recorded and nothing fails, which is the more dangerous of the two, because the comparison then passes against an empty record instead of failing. A record missing an entry cannot be detected by the check itself either: the replay carries a prompt the record does not, and extra replayed messages are deliberately not evidence of a mixup. So acpc retries a failed marker write before the turn finalizes, and where a prompt crossed the wire but could not be recorded — whether the write failed or the delivery was never observable — it records that the session's delivery record is incomplete, and the turn still finishes: failing a turn the adapter has already received discards a real answer to protect a bookkeeping entry. A cold resume of such a session reports `resume: unverified — delivery record incomplete`, in the summary and in `--json`, even where the subsequence check itself passed, and a resume that compared no prompts at all is never reported as verified on the strength of that comparison.
- **Unverified is reported, never silently equated with verified.** A cold resume says which of the two checks ran: the end-of-run summary carries a `resume: verified` segment when at least one check passed, and `resume: unverified — <what was unavailable>` when neither could run. The same value appears in `--json`. A caller that cares whether its session was confirmed can read it; one that does not is unaffected, since the turn runs either way. A mismatch, by contrast, fails the `continue` with an actionable error naming what did not line up — a stored prompt that is missing, or one that arrived out of order, and which of the two — *before* the prompt is dispatched, so a failed verification costs nothing and changes nothing.
- **`continue` on a `running` session is an error**, not a queue.
- **The turn runs with the session's stored resolution**: model, effort, mode, permissions and home come from `meta.json`, not from re-resolving the agent entry — editing an entry never changes a session mid-conversation. The mode is stored with the two facts it was selected on, `grants` and `delegates`, so a turn never re-reads `[modes]` and an edited adapter definition cannot move a running session's ceiling. A session stored without a mode runs selection once from its stored policy, records the result, and sends that mode. How model and effort are applied — `model_via`, `effort_via`, `effort_cli_flag`, and `effort_config_id` — is stored on the adapter block with those facts (see *Agent variants*) and reused on the direct-child path; a missing via means `config_option`. A daemon-hosted turn re-resolves those apply-path fields from the live entry.
- **`--permissions` is the one `run` *resolution* flag `continue` accepts** (the output-shaping flags above ride along unchanged), because a turn can end by refusing something the caller would have allowed — a denied category, or a mode switch above the ceiling. The new policy applies to this turn and every turn after it, re-runs mode selection against the adapter's current `[modes]` — the one case where a turn reads it, because the caller asked for a new mode — and writes the policy and the resulting mode triple back to `meta.json`, so the session carries one policy at a time rather than a history of them. It moves in either direction: lowering it is how a caller hands a session on with less authority than it had. Rewriting that adapter block copies the vias from the live selection (see *Agent variants*) rather than dropping them: stripping them would send the next turn down `config_option` and, for `effort_via = "cli"`, collapse distinct efforts onto one daemon target.
- **The early session line applies here too**: a blocking `continue` prints `-- session <id> | dir <path>` at dispatch, exactly as `run` does (see *Output contract*).
- **`continue` is also the recovery path after an adapter failure.** A turn killed mid-stream — an idle timeout, a torn connection, a crashed adapter — leaves the session `failed` with its transcript intact, and `continue` resumes on that context instead of restarting the work from zero; the interrupted turn's partial answer is parked as `answer.<n>.md` by the normal rotation, so nothing the turn already produced is lost.

A separate verb only because `run` takes an agent and `continue` takes a session. Each turn's prompt and answer are kept on disk (see *State on disk*). A `run`-only flag here is a usage error that names the rule — `continue reuses the session's model — drop --model` — never a bare "unrecognized argument".

```
acpc continue researcher "expand section 3, it's too thin"
acpc continue last "now apply the same fix to the v2 API"   # TTY only
```

### `daemon`

Plumbing, deliberately minimal. The daemon is a performance cache — it keeps adapters warm, nothing more.

- **Auto-managed**: starts on first use, expires after an idle TTL (default `30m`, `daemon_ttl` in the global config; idle = no active sessions, so a detached session keeps its daemon alive). No `start`/`restart` verbs — `daemon stop <target>` plus the next run *is* the restart.
- **Keyed per target** (agent + home + declared env — see *Agent variants*): one daemon serves any number of sessions on its target; concurrent *turns* run up to `daemon_max_concurrent` (default 8; further turns queue and start when a slot opens, noted on stderr) — an idle or detached session holds no slot. Different homes/providers are separate targets, so fan-out never serializes. When the entry applies effort on the CLI (`effort_via = "cli"`), the resolved effort and flag name join that key too — the flag is process-level and cannot be changed after spawn. A turn routed to a warm daemon whose adapter was spawned with a different argv is failed with an error naming both, never silently served; `daemon stop <target>` is the remedy the error names.
- **The `[target]` argument** to `daemon status`/`stop` is an agent or variant name and addresses every target under it; `daemon status` lists each concrete target with its log path.
- **Two uses**: a wedged or stale daemon (`daemon stop`), and debugging (`daemon status` prints the acpc version the daemon runs, PID, uptime, idle age and the per-target log path — the only place adapter stderr goes in daemon mode; the version is there because a caller reporting a wedged daemon should not need a second command to say which build wedged). The **idle age** is the TTL's own clock: time since the target last had an active session, rendered `idle <age>` in the vocabulary session `status` uses, and `·` while the target is currently serving one. Uptime cannot answer what the view is usually opened for — a daemon reporting `up 1h48m` under a 30 m TTL looks leaked and may simply have been busy until a moment ago. The TTL measures idle time, so idle time is what says how close the daemon is to being reaped.
- **`daemon stop` refuses a target with active sessions.** A target serving sessions in state `running` or `starting` is not stopped: one error line naming the count and the ids, exit 2, nothing signalled — stopping a daemon under a live dispatch is nearly always a mistake, and the ids are exactly what the caller needs in order to `wait` or `stop` them first. Liveness is verified as everywhere else, so a session whose process is already gone reads `orphaned` and does not block the stop. When the argument addresses several targets, the guard is evaluated across all of them before anything is stopped: one blocking session refuses the whole command, because a partial stop would leave the caller guessing which half happened. `--force` stops anyway, and the sessions it takes down transition to `failed` with the reason recorded in meta — never orphaned.
- **Version skew self-heals**: a daemon that doesn't match the client version restarts itself on connect.
- **Fallback**: if the daemon cannot start at all (restricted sandboxes), `run` spawns the adapter as a direct child — visibly: the stderr summary says so, and SIGTERM then cancels instead of detaching.

```
acpc daemon status
acpc daemon stop codex          # controlled nuke; beats pkill, which kills mid-task dispatches
acpc daemon stop codex --force  # ... and take its running sessions down with it
```

### `install`

Run the entry's trusted `install_command`. An entry without one — the shipped
`grok` adapter is the first — refuses `acpc install` and names the vendor
docs (`install_docs`) instead: acpc already registers the adapter; the binary
is the vendor CLI.

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
- **Finished session**: same output, footer becomes `-- done exit 0 | 3m12s | 41k tok | answer: <path> | events 26–45 of 45 | cursor: 45` — duration and tokens/cost included, because a `--bg` caller never sees the stderr summary; the cursor stays, so a poller's final call needs no special casing. When the state is `failed`, `timeout` or `orphaned`, the last agent message prints in full — it usually contains the reason. When the adapter died without saying anything it contains nothing useful, and the reason is instead the `error` event a `failed` session always records (see *Session states*) — which needs no special rule here, since error events render in any `log` view like every other event.
- **The transcript records what the adapter reports**, which is not always everything the callee attempted. A vendor that blocks an action inside its own process need emit nothing over ACP: codex's sandbox denials arrive as neither tool call, error nor permission event — measured on codex-acp 1.1.9, where the client received 3 tool calls for a session whose own rollout recorded 8. So `log` is the record of the session as ACP reported it, and an audit that has to be exhaustive cannot end there.
- **`--follow` ends exactly three ways**, and the exit code says which: the session finished (**0**, finished footer — following a stopped stream ends, the `logs -f` convention, so a session already finished at the call returns its replay at once); `--timeout` expired (**124**, still-running footer plus the `--wait-new` timeout note — the session is untouched and keeps running); `--max-output` ran out before either (**4**, the truncation marker on stdout and, on stderr, `-- stopped: --max-output <N> exhausted — resume with: acpc log <id> --follow --since <cursor>`). The third code exists because a cut stream is not a completed follow: with 0 or 124 alone a caller cannot tell "the run is still going" from "I stopped reading it". Every ending prints the footer, whose cursor covers exactly what stdout carried, so `--since <cursor>` resumes without a gap or a repeat.
- **`--follow` is for one case**: supervising a run you intend to steer or stop mid-flight. It is not a live view — a foreground tool call returns its output when it exits, so what a caller gets is a bounded digest of what happened while it blocked. Checking in on a run is a plain `log` snapshot; waiting for a result is `wait`. Follow costs a blocked call and puts every event it collects into the caller's context, which is the expensive way to ask a question the other two answer for free.

### `probe`

```
probe <entry> --discover [--json]
```

| Option | Purpose |
|--------|---------|
| `--discover` | Read the advertised mode catalogue. Zero turns |
| `--json` | Emit the report as JSON |

Report the modes an adapter advertises, and how they differ from the entry's recorded `[modes]`
table, without editing anything.

- **`probe` re-reads the adapter, because a `[modes]` table goes stale.** A `[modes]` entry records
what an adapter was observed to allow, and observation ages: vendors change defaults, ship new modes,
and rename old ones between releases. Nothing in the entry notices when that happens, so a table that
was accurate when it was written keeps being trusted after it stops being true. `probe` asks the
adapter directly.
- **`--discover` costs nothing.** It opens a session, reads the advertised mode catalogue and
releases it, running zero turns: enough to see a mode the adapter advertises that the entry does not
list, and an entry mode the adapter no longer advertises, each shown with the description the adapter
gives.
- **`probe` reports; it does not edit the registry.** Output is the advertised catalogue and a diff
against the entry's current table, stated from both sides — what the adapter advertises that the
entry lacks, and what the entry records that the adapter no longer advertises. Applying any of it is
a separate, explicit act.
- **Measuring what a mode actually permits is not in this release.** What an adapter *advertises* and
what it *allows* are different questions, and the second can only be answered by evidence read off
disk after a real turn. `probe` invoked without `--discover` says so and is a usage error naming the
flag, exit 2 — not an empty report, and not a silent success. A discovery report handed to a caller
who expected a measurement would be the exact failure this command exists to prevent: an answer to a
question nobody asked, presented as though it settled the one they did.

```
acpc probe claude --discover
acpc probe codex --discover --json
```

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
acpc run codex "fix the failing test in tests/test_auth.py" --cwd ~/repo --permissions execute
acpc run claude - --bg --name researcher --effort high <<'EOF'
Research X. Write findings to ./findings.md.
EOF
```

```
$ acpc run codex "probe" --permissions edit --cwd ~/repo --dry-run
entry        codex (codex)
command      codex-acp
model        gpt-5.6-terra (adapter default)
effort       xhigh (adapter default)
mode         read-only (selected for permissions edit) · vendor-decided · escalates
permissions  edit (call flag)
home         ~/.codex (adapter default)
cwd          ~/repo
passthrough  CODEX_HOME · CODEX_PATH · CODEX_CONFIG · MODEL_PROVIDER · CODEX_API_KEY · OPENAI_API_KEY · INITIAL_AGENT_MODE

$ acpc run codex "probe" --permissions edit --cwd ~/repo --dry-run --json
{"entry": "codex", "base_adapter": "codex", "command": "codex-acp", "cwd": "~/repo", "env": {}, "env_passthrough": ["CODEX_HOME", "CODEX_PATH", "CODEX_CONFIG", "MODEL_PROVIDER", "CODEX_API_KEY", "OPENAI_API_KEY", "INITIAL_AGENT_MODE"], "resolved": {"model": {"value": "gpt-5.6-terra", "source": "adapter default"}, "effort": {"value": "xhigh", "source": "adapter default"}, "mode": {"value": "read-only", "source": "selected", "grants": "edit", "delegates": false, "escalates": true}, "permissions": {"value": "edit", "source": "call flag"}, "home": {"value": "~/.codex", "source": "adapter default"}}}
```

| Option | Purpose |
|--------|---------|
| prompt as arg, `-` (stdin), or `--prompt-file` | Heredoc/stdin for long prompts with quotes and backticks. Exactly one source — zero or two is a usage error naming the options; stdin is never read implicitly |
| `--cwd <dir>` | Working directory of the callee. Long flag on purpose: `-C`/`-c` invites confusion with `continue` |
| `--model <tier\|id>` | A tier (`fast`/`standard`/`max`, resolved through the adapter's preset table — see *Agent variants*) or a raw model ID from `agents <name> --models`. Explicit `--effort` overrides the preset's effort, and supplies one where the preset has none |
| `--effort <level>` | Reasoning effort, orthogonal to `--model`. Two layers: a global scale (none/minimal/low/medium/high/xhigh/max/ultra), and an optional per-model `[effort_by_model]` table on the adapter. A level outside the global scale is a usage error listing that scale. When the resolved model has a row, the value must be in that row — an empty row means the model has no effort setting. When the model is unset or has no row and the table is non-empty, the value must be in the derived union of the table's non-empty rows (unique, global-scale order), and a model with no row prints a warning rather than failing. An empty table, or a table whose rows are all empty, is the global scale alone. Never a silent fallback |
| `--permissions none\|read\|edit\|execute\|all\|ask` | Approval policy (defined below). Default: agent entry if set, else `ask` on a TTY and `read` otherwise; `--bg` counts as non-TTY (see *TTY vs non-TTY*). `write` and `prompt` are accepted as deprecated aliases for `execute` and `ask` |
| `--mode <name>` | Vendor mode override; normally unnecessary, since `--permissions` selects the mode. Refused when the mode grants more than the policy, whichever of entry or flag set it, and when the adapter's `[modes]` omits it. Values via `agents` |
| `--home <dir>` | Vendor home override (the dir with the vendor's config + credentials). The provider switch (see *Agent variants*); ad-hoc counterpart of a variant's `home` field |
| `-o <file>` | Write the answer to the given path; stdout then carries only a short confirmation (path, size, session id). `answer.md` in the session dir is always written regardless |
| `--bg` | Return immediately with session ID + session dir path. With `-o`, the file is written when the session finishes |
| `--timeout <s>` | Cancels the session on expiry (state `timeout`, exit 124). No default — wall-clock limits belong to the calling harness. A bare number is seconds; a suffixed value is a duration (`90s`, `5m`, `1h`, `1h30m`), the vocabulary the config file already uses. Every `--timeout` in the CLI reads the same way |
| `--name <alias>` | Human-typeable handle for `continue`/`status`/`log`. Reusing a name rebinds it to the new session with a warning — hard error while the old session is `running`. `last` is reserved |
| `--dry-run` | Print what this call would resolve to (model, effort, mode, permissions, home, declared env, cwd — and where each value came from), then exit. The mode line names why that mode was selected, whether it delegates to acpc, and whether it escalates in-vendor |
| `--max-output <bytes>` | Cap on stdout bytes (default 128 KiB, 0 disables). Truncation keeps the head, cuts on a UTF-8 boundary, and ends with a marker line naming the full answer path. The marker sits at the tail, which some harness previews clip — the stderr summary repeats the session dir, so the path always survives. Shapes stdout only: `-o` files and `answer.md` are always complete. With `--json`, truncation applies to the `answer` field and sets `truncated: true`; the envelope is always valid JSON |
| `--quiet` | Suppress acpc's own stderr lines for this call — the early session line and the end-of-run summary (see *Output contract*) |

**Early session line.** A blocking `run` prints `-- session <id> | dir <path>` to stderr at
dispatch, before the turn has produced anything, so the id is reachable mid-run — `log`,
`stop` — from output the caller has already captured. See *Output contract*.

**Permissions.** `--permissions` names a ceiling on one scale:

| Rung | Admits categories |
|------|-------------------|
| `none` | — |
| `read` | read |
| `edit` | read, edit |
| `execute` | read, edit, execute |
| `all` | everything, unknown included |

`ask` is not a point on the scale: read is auto-allowed, every other category asks the
human on `/dev/tty`.

Categories come from the ACP tool-call `kind`: `read`/`search`/`fetch`/`think` → read,
`edit` → edit, `execute`/`delete`/`move` → execute, everything else — `other` and any kind
outside the ACP enum — → unknown. `switch_mode` is not a category; it re-runs mode
selection (below). Allowing answers `allow_once`; `allow_always` only under `all`;
`reject_always` is never sent. Every decision lands in the transcript as a `permission`
event.

**Modes carry three facts**, none declared by ACP, all from probing the adapter.
`grants` is the ceiling of what a mode permits with no request reaching acpc, by effect;
`delegates` says whether anything above it arrives at all; `escalates` says whether an
in-vendor auto-approver can raise that ceiling with no request reaching acpc either.

`escalates` is optional and defaults to false where a mode is declared fresh; on a mode
inherited through `extends`, omitting it keeps the parent's value rather than resetting it,
so a variant cannot quietly drop the flag. It is informational: mode selection
reads `grants` and `delegates` exactly as it always has, and an escalating mode is neither
preferred nor discarded for it. What it records is that `grants` for that mode is a
measurement and not a bound — codex's `read-only` runs writes that its own in-vendor
reviewer approves, and claude's `auto` approves silently with no marker at all — so a caller reading
`agents <name>` or `--dry-run` can see where the recorded ceiling is least trustworthy.
It is a label on a measurement, not a second scale: a mode that escalates is one whose
`grants` the next adapter release is most likely to move.

```toml
# claude.toml
[modes]
default           = { grants = "read",    delegates = true }
plan              = { grants = "edit",    delegates = true }
acceptEdits       = { grants = "execute", delegates = true }
auto              = { grants = "all",     delegates = false, escalates = true }
dontAsk           = { grants = "none",    delegates = false }
bypassPermissions = { grants = "all",     delegates = false }
```

**Mode selection.** acpc always sends `session/set_mode`: a vendor default never silently
overrides the policy. Two steps: discard every mode whose `grants` exceeds the policy,
then among the rest prefer `delegates = true`, then the highest `grants`. A policy no mode
satisfies is a usage error naming the adapter's modes, never a silent downgrade. A mode
the adapter advertises but `[modes]` omits is refused unless the policy is `all`. `--mode`
and an entry's `mode` override the second step, not the first. A policy that sits below every
declared mode's grant is refused with the floor named and the flag that clears it — `the lowest
policy grok runs under is execute; pass --permissions execute` — because the caller who hits this
is one flag away from a working call, and a declared-modes dump alone makes them compute the floor
by hand.

**A runtime switch is a re-selection, not a permission.** acpc resolves a `switch_mode`
target through `[modes]` under the same rules as dispatch: a target above the policy is
refused, and one absent from `[modes]` is refused unless the policy is `all`. The refusal
ends the turn, naming the requested mode and the policy that would admit it, so the caller
can raise the ceiling and resume with `continue`.

**Client methods carry the same policy.** ACP puts `fs/*` and `terminal/*` outside
`request_permission`: the callee calls them, acpc executes them. They are classified
like kinds — reads as read, `fs/write_text_file` as edit, terminal creation as execute; the
creation gate ships, but `create_terminal` currently raises `NotImplementedError` and returns
no id. Existing-terminal operations are not shipped yet, so no decision is stored; should
terminal support ever ship, the inheritance rule for those operations is defined with it.
Without the creation gate, an adapter routing edits through the filesystem callback would
write to disk at any rung, `none` included.

Where the model's edges are:

- **The non-TTY default is a silent read-only trap**: absent `--permissions` and an entry default, writes are denied without an error and the turn ends at exit 0 having changed nothing. Pass `edit` for file work, `execute` for commands.
- **Denials are always reported**, by category, in the end-of-run summary and the `--json` envelope, with the lowest policy that would have admitted them — defaulted or not. The exit code stays 0: a denial is a result, not a failure.
- **`delegates` is not completeness, and the route sets the rung, not the effect**: a delegating mode asks only what the *vendor* thinks worth asking — Claude Code runs commands its own classifier calls read-only without emitting a request. `echo x > file` is `execute` where the edit tool is `edit`; deletes and renames are `execute` too, since adapters shell out and the `delete`/`move` kinds go unemitted in practice. `edit` therefore bounds what may change, not whether a shell ran: a callee under it cannot run tests or linters.
- **`fetch` is network egress and it sits in `read`**: the lowest useful rung reaches the internet, which is why a research task needs nothing above it and where a callee processing untrusted input exfiltrates from. `none` is the only policy that closes it.
- **An approval policy, not a sandbox**: with no delegating mode the policy only picks which vendor mode runs — nothing is asked, so acpc answers nothing and the boundary is the vendor's. A real boundary means confining the adapter: a container, a dedicated user, or the vendor's own sandbox.
- **The ceiling is inherited across re-dispatch**: a callee permitted `execute` can run `acpc` itself, so its environment carries `ACPC_CEILING`, the parent's resolved rung (`read` when the parent was on `ask`). A nested call resolves to the lower of the two and reports the clamp. The environment is fixed at spawn while the policy varies per turn, so the resolved policy joins the daemon target key (see *Daemon*) — equal policies share a target, which is what keeps the inherited rung correct. Ancestry beyond the rung is not carried: one warm adapter serves many sessions, so a per-session value in that environment would be the first session's for every session after it. A guardrail against an orchestrator that has not noticed its child reaches higher, not a boundary: a callee with a shell can unset it.
- **`ask` excludes the daemon**: it needs the calling terminal, which a daemon has not, so the call is a direct child and pays a cold adapter start (see *Daemon*).

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

With id: one session's vitals — state (exit code once finished), runtime, idle age while active, tokens/cost so far, the entry with its base adapter and resolved model, name, session dir and answer path. Cost accumulates across the session's turns: for adapters that report usage on the prompt response the per-turn charges are summed, and for adapters that stream cumulative usage the largest reported tally wins, so a cold resume never forgets earlier turns. Tokens are the latest reported figure — context occupancy for streaming adapters, the turn's replayed-context total for prompt-response reporters — and a turn that reports no usage at all leaves both untouched. A `failed` session's vitals also say why: a `failure` line carries the error event's `observation` — one line, ANSI-free, bounded, without the log tail — followed by the follow-up as `continue: acpc continue <id>`, the same vocabulary the end-of-run summary uses, because every finished state is resumable and a failure view that does not say so sends the reader off to discover what should be the next keystroke. The line exists only while the failure is current: rotation clears it with the rest of the per-turn state, so a session that failed and was then resumed shows the resumed turn's vitals, not a stale post-mortem.

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
- **A turn is registered before its preparation begins, not after.** Routing, the session reservation, adapter startup and the restore all happen before a prompt is sent, and the restore is both the slowest phase of a `continue` and the one most likely to hang — so it is precisely when a caller reaches for `stop`. On the daemon path the daemon records the turn in memory as soon as it accepts it and before preparation starts, so `status` can say the session is preparing, `stop` can reach it, and `steer` has something to address. The record is in memory and nowhere else: a daemon that dies takes it with it, the session is then read as `orphaned` by the same liveness rule as everywhere else, and no crash can leave a durable preparation marker behind to be cleaned up. That is the whole reason it is not written down — durable preparation state would reintroduce the residue problem the ephemeral reservation exists to prevent.
- **Cancelling during preparation cancels the preparation, and no turn ever runs against a partially restored session.** `stop` and Ctrl-C answer the same way in every phase before the prompt is sent as they do during the turn: the session ends `cancelled`, not `failed` — nothing failed, a caller changed their mind — and `answer.md` holds the placeholder rather than a diagnosis. SIGTERM keeps its own meaning throughout rather than borrowing Ctrl-C's: it detaches once a daemon owns the turn and otherwise ends the preparation, exiting 143 either way. What differs is what has to be undone: the session reservation is released, and the adapter session being restored is released along with the replay context collected for it, so nothing half-applied stays bound to the turn. What acpc cannot do is unsend the restore. ACP defines no request cancellation for `session/load` or `session/resume` — `session/cancel` ends prompts, not restores — so a restore acpc has cancelled may still run to completion inside the adapter, and acpc states that rather than implying it stopped it. The guarantee is therefore about what a turn runs against, not about what the adapter does: a later `continue` on that session waits for an in-flight restore to settle before preparing its own, so it begins against a whole state rather than racing a half-applied one. An external `stop` or `steer` aimed at a session whose turn no daemon has accepted yet finds nothing to address and says so; the Ctrl-C in the continuing process itself is not that case — it owns the turn it is cancelling, so it records one rather than pretending nothing happened.
- **`steer` before the prompt is sent redirects rather than interrupts.** `steer` is `stop` plus `continue` with a preamble naming the interruption, and during preparation there is no turn to interrupt: the preamble would describe something that never happened. So a `steer` that lands before the prompt goes out cancels the preparation and dispatches its instruction as the turn's prompt, with no preamble, and says on stderr that nothing was interrupted. This is the same degradation the race with a natural finish already takes, for the same reason — the preamble is a description of an event, and acpc does not assert events that did not occur.

**Checkpoint is a recipe, not a verb.** "Tell me where you are" needs no new surface:

```
acpc steer x7k2 "Summarize: done / hypothesis / next step / blockers — then stop"
```

A dedicated verb would be speculative API, and the name would oversell: "checkpoint" sounds free while the mechanics still cancel work in flight. The zero-cost, read-only alternative is `log <id> --prose`, which asks the callee for nothing at all.

```
acpc steer x7k2 "Stop editing; diagnose only and report what you found"
```

### `stop`

Stop a running session. Graceful (ACP `session/cancel`) with a bounded wait for the ack (10s) — if the callee doesn't wind down in time, the connection is torn down anyway. Transcript, meta and partial answer stay on disk for post-mortem. A stopped session is finished and resumable with `continue`, with the adapter context preserved. A hard variant, if ever needed, is `stop --force`, not a new verb.

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

Block until a background session finishes, then print its answer; exit code mirrors the session result. On an already-finished session it returns immediately — the free way to reprint an answer. When that session failed, the stderr summary carries a `failure:` segment with the recorded cause (see *Session states*), so a poller learns why without a second command.

```
acpc wait x7k2 --timeout 600
```

## Agent variants

A named agent entry can bundle model, effort, mode, permissions and environment, so `run builder "task"` replaces five flags. The one acceptable form of configuration, under one condition: resolution stays fully inspectable — `agents <name>` shows what an entry resolves to, `--dry-run` what a specific call resolves to and why.

```
# ~/.acpc/agents/builder.toml — hand-editable; `agents init` scaffolds this
extends = "codex"
description = "Implements a task against a plan; writes code and runs commands."
model = "gpt-5.6-luna"
effort = "xhigh"
permissions = "execute"
home = "~/.codex-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]   # names read from the caller's env at call time

[env]                                      # literal values declared in the entry
MODEL_PROVIDER = "openrouter"

$ acpc run builder "implement the parser per SPEC.md"
# ≡ acpc run codex … --model gpt-5.6-luna --effort xhigh --permissions execute --home ~/.codex-openrouter
```

A variant sets `permissions`, not `mode`: the policy selects the mode (see *Permissions*). An entry may still pin `mode` as an override, subject to the same ceiling as `--mode` — an entry cannot become the way around the policy.

An adapter definition declares its modes in a `[modes]` table: for each vendor mode, what it permits with no request reaching acpc (`grants`), whether anything above that arrives at all (`delegates`), and optionally whether an in-vendor auto-approver can raise that ceiling unasked (`escalates`, default false — informational only, see *Permissions*). These are adapter facts established by probing the adapter; ACP declares none of them. A mode the running adapter advertises but the table omits is refused unless the policy is `all`, so a vendor that adds a mode cannot quietly widen what acpc allows.

The `home` field is also the provider dimension: OpenAI vs OpenRouter vs a local endpoint is just a different vendor home (own config, own credentials). A variant is the named, permanent form; `--home` on `run` the one-off form.

**`description`** is optional on any entry, adapter or variant, and takes any string the operator writes — any length, newlines included. A roster reading `builder`, `explorer`, `planner` says nothing about what any of them is *for*; that is the operator's knowledge, not inferable from `--help`, and it belongs in the entry rather than in external documentation that goes stale. **The config never rejects it and never truncates it on disk**: a purely informational field must not be able to break a working dispatch, so context protection lives in the view rather than in the parser. The `agents` list normalizes whitespace and cuts at a word boundary within an 80-character budget, the same cut the condensed `log` view uses; `agents <name>` shows the description verbatim and in full, and `--json` carries the whole value — truncation shapes the text list and nothing else. It is **not inherited through `extends`** — a variant's purpose is its own, and rendering the parent's text under a child's name would be a confident lie about what the child does. Absence renders as absence everywhere: nothing in the list row, no line in the detail view, `null` in `--json`.

Presets are adapter-level: each adapter definition ships its `fast`/`standard`/`max` table — what `--model <tier>` resolves through and `agents <name> --models` prints. `model` is required; **`effort` is optional, because effort is a property of the model rather than of the tier.** A vendor may expose no effort setting for a given model — claude CLI ≥2.1.224 offers none for Haiku 4.5 while keeping it for Sonnet and Opus — and a tier that pins such a model omits `effort` entirely, meaning the model runs at its own built-in level. Where an effort is present it is validated against that model's `[effort_by_model]` row: Haiku's empty `[]` refuses any effort, grok-4.5's subset refuses `xhigh` while grok-4.6 accepts it. Overriding what a tier means uses the same mechanism as everything else — a `[presets]` table in a file under `agents/` for that adapter — never `config.toml`, so resolution stays inspectable with provenance like every other field. Tiers left out keep the adapter's shipped pair.

A model with no effort setting is listed exactly that way: `agents <name> --models` and the cross-agent overview render `·` in the effort column and `--json` carries `null`, absence as absence. Asking for one anyway stays a loud failure rather than a silent downgrade — an explicit `--effort` the adapter rejects fails the turn, and the error names the resolved model as the likely reason it has no such setting. A preset effort the table rejects fails the same way: it means the entry TOML has gone stale against the vendor, and a one-line fix to a file beats a runtime capability probe that adapts silently.

```toml
# ~/.acpc/agents/codex.toml — same override mechanism, aimed at the base adapter
[presets]
fast = { model = "gpt-5.6-luna", effort = "high" }
max  = { model = "gpt-5.6-sol",  effort = "xhigh" }
# effort omitted: this model has no effort setting to give it
turbo = { model = "gpt-5.6-nova" }
```

Resolved model and effort are applied on a path the entry names for that
field — not a via table for arbitrary settings. Session RPCs run after
`session/set_mode`. The default is ACP `session/set_config_option`: model
under id `model`, effort under `effort_config_id` when the entry sets one
(claude's is `effort`) and `reasoning_effort` otherwise. That is what shipped
`claude` and `codex` do, and what a missing field means, including sessions
stored before these fields existed.

When a vendor does not implement that RPC, the entry names an alternate for
**that field**:

- `model_via`: `config_option` (default) or `set_model` — ACP
  `session/set_model` with `modelId`.
- `effort_via`: `config_option` (default) or `cli` — two spawn-argv tokens,
  `effort_cli_flag` then the effort value (`--reasoning-effort` when the flag
  is omitted), never `--flag=value`. Injected once, before a trailing
  transport subcommand (`stdio`, `serve`, `headless`, `leader`) when the
  command has one. CLI effort is not reapplied after `session/set_mode`.

These are closed enums on two fields. `set_model` is not a road for effort;
`cli` is not a road for model. An unknown value is a registry error. A new
combination is a new enum member and the code that implements it, not an
implied third column.

`cli` effort is process-level: it cannot be changed on a live adapter, so the
resolved effort and the flag name join the daemon target key. `--dry-run`
shows the injected argv; the stored session keeps the base `command` string
so `continue` re-injects once rather than stacking flags. The vias themselves
are stored on the session's adapter block. A continue that does not re-select
a mode reuses them on the direct-child path. A continue that rewrites that
block (`--permissions`, a missing stored mode, a ceiling clamp) copies the
vias from the live selection rather than dropping them back to
`config_option`. A daemon-hosted turn re-resolves apply-path fields from the
live entry — it does not ship the stored vias over the socket — so editing
them can take effect on the next daemon turn.

Shipped `grok` is the first adapter on the alternate pair
(`model_via = "set_model"`, `effort_via = "cli"`). Variants inherit the vias.

Environment is part of the entry, in two fields. An `[env]` table holds literal values declared in the entry (e.g. the vendor home path). `env_passthrough` lists variable *names* read from the caller's environment at call time — values are never stored on disk, which is how API keys travel. Both feed the daemon target key ("declared env"): `[env]` by name and value, `env_passthrough` by name *and the value read at call time* — hashed into the key, still never stored — so two entries with different env, or two callers holding different credentials, are separate targets that cannot serve each other's traffic (nobody silently rides on the first caller's API key). The resolved permission policy is part of the key too: an adapter's environment is fixed when it is spawned while the policy varies per turn, so one warm adapter cannot serve two policies without handing the later turn a stale `ACPC_CEILING`. When effort is applied on the CLI it joins the key for the same reason — it is fixed at spawn. That costs the fixed per-target overhead only — the adapter's per-session memory is paid however targets are keyed — and idle targets expire.

The adapter's environment is constructed, not inherited — but not paranoid-empty either. Three layers reach it: a base system set (`HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL`), capability variables passed through from the caller (`SSH_AUTH_SOCK`; `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` in both cases; `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`) so tools on PATH, proxies and ssh keep working, and the entry's declared env on top. The rest of the ambient environment never reaches the adapter. Capability variables are passed but not part of the target key — a long-lived daemon may hold the first caller's proxy or agent socket; stop the daemon when that must change.

Entry TOMLs are trusted at the level of shell config: an adapter definition names the command acpc executes and the env delivered to it. Only place files you trust in `agents/`.

## Bundled skills

`acpc skills` lists the skills bundled in the package; `acpc skills <name>` prints one skill's body. The source is bundled-only: readable `SKILL.md` files directly under `data/skills/*` are served, and the directory name wins over any frontmatter `name` — it is what the filesystem can be trusted about, and it is what `skills <name>` takes. In the detail view the body is verbatim on stdout, byte-identical to the file below the frontmatter, and the skill directory rides on stderr as `-- skill <name> | dir <path>` — which is how a caller finds `references/` without a flag for it. Both views accept `--json`; the list emits `{"skills": [...]}` like every other list view, and the detail view adds `body`. An unknown name is a usage error (exit 2) pointing at `acpc skills`.

## Output contract

- **stdout carries exactly one thing, chosen by flags**: the answer (default), a short confirmation (`-o`), a JSON envelope (`--json`), a session ID + dir path (`--bg`). Never spinners, ANSI, logs or diagnostics — those go to stderr or the session log file.
- **Every column is labeled, once — in a header or on the value, never nowhere.** A multi-row positional view prints one uppercase header line above its rows, on stdout with them (the `docker ps` prior — uppercase is what makes the label row readable as a label row at a glance; block labels like `presets` and inline-labeled values stay lowercase): `status`'s list, the `agents` list's variant rows, the preset table in `agents <name> --models`. A view that labels each value inline instead — `daemon status`'s `acpc 0.6.0  pid 728419  · up 36m53s  · idle 8m39s` — is already labeled and gets no header. What is never acceptable is a column whose meaning lives nowhere: a bare `·` in an unlabeled position is unreadable on first contact, and this tool's first-contact reader is usually an agent that cannot ask. Detail views are exempt (one labeled field per line already), as is the `log` stream (each event is self-describing).
- **Column widths are computed from the rendered rows, never fixed.** Each view measures what it is about to print and pads to the widest value, header included. A hardcoded width is aligned only for the values that existed when it was written: a 26-character model id or a 14-character entry name shears every column after it, and the row that most needs reading — the unusual one — is the row that breaks. One long value widens the table; it never misaligns it.
- **A *message boundary* is where one agent message ends and the next begins**: any non-message update — a tool call, a thought chunk, a usage report, anything that is not an `agent_message_chunk` — arriving between two message chunks. Chunks with nothing between them are one message being streamed, however long the pause. The rule reads the update stream and never the clock, so a slow adapter cannot invent a boundary and a fast one cannot lose a real one. Two views consume this one definition: the answer separates messages at a boundary (below), and the condensed `log` view marks the events that continue across one (see `log`).
- **"The answer" is defined**: the turn's ACP agent-message content, chunks concatenated in stream order; thought chunks and tool output excluded; markdown passed through verbatim. Narration interleaved between tool calls is part of it — never silently dropped, and never silently glued to what follows: at every message boundary the answer carries a blank line. stdout and `answer.md` carry identical bytes; for a single-turn session, `log --prose --since 0` renders the same content.
- **Fixed exit codes** (Unix conventions; finer-grained ACP `stop_reason` lives in `meta.json` and the `--json` envelope):

  | Code | Meaning |
  |------|---------|
  | 0 | success (`end_turn`) |
  | 1 | agent error — crash, `refusal`, `max_tokens`, missing auth |
  | 2 | usage error — bad flags, unknown session, a mode that exceeds the policy, or a policy no mode satisfies |
  | 4 | output budget exhausted — `log --follow` stopped because `--max-output` ran out before the session ended; the footer's cursor covers what was printed and `--since` resumes from it |
  | 124 | timeout (`run`: session cancelled; `wait`/`log --wait-new`: nothing new within the window — a running session keeps running and the exit says so on stderr; a finished session returns at once) |
  | 130 | cancelled — SIGINT or `stop`. Answer-printing commands mirror the session result, so `wait` on a cancelled session also exits 130, whoever cancelled it and whenever; the finer distinction lives in `stop_reason` |
  | 141 / 143 | SIGPIPE / SIGTERM (SIGTERM detaches — see below) |

- **Client death ≠ session death.** SIGINT (a human's Ctrl-C) cancels the session (`session/cancel`, state `cancelled`). SIGTERM (a harness killing the tool call on its own timeout — the *normal* case for an agent caller) detaches: the session keeps running under the daemon, and on the way out the client prints exactly `-- detached, still RUNNING: <id> — answer: acpc wait <id> · cancel: acpc stop <id>` to stderr, so the caller that killed the tool still learns both the id and its options. When the adapter ran as a direct child because the daemon couldn't start (see `daemon`), detach is impossible — SIGTERM cancels there too.
- **`--json` means "this command's output as JSON"**, uniformly. Three shapes:
  - **Answer-printing commands** (`run`, `continue`, `wait`): a result envelope — `state`, `session_id`, `stop_reason`, `paths`, `cost`, `answer`. Two flags reshape it: `--bg` leaves only what exists at dispatch time (`session_id`, `state`, `paths`); `-o` names the output file and omits `answer`.
  - **Everything else** (`status`, `agents`, `daemon status`, `stop`, `rm`, `prune`, `install`, `probe`, `--dry-run`): the same data the text view shows, as JSON.
  - **The one exception**: `log --json` emits raw transcript events (see `log`), not an envelope.
- **End-of-run summary, one line, on stderr, prefixed `--`**: duration, tokens/cost, exit status, session ID, session dir, and the follow-up command as `continue: acpc continue <id>` — every finished state is resumable, and the caller reading this line is the one deciding whether to send another turn, so the id travels next to the verb that consumes it. Harnesses merge stderr into the same blob as the answer — the fixed prefix keeps it mechanically separable. The prefix only separates at a line boundary, and answers need not end with a newline, so when stdout's last line is unterminated the stderr metadata that follows leads with a newline of its own — on stderr, never appended to stdout, which stays byte-identical to `answer.md`. When permission denials occurred the summary adds a segment naming the count, the categories and the lowest policy that would have admitted them: `denied: 3 edit (pass --permissions edit)`. Reported whether the policy was defaulted or passed explicitly — an explicit policy set too low is the same mistake as an absent one, and the caller that passes flags is the one reading output mechanically; when it was defaulted the segment says so (`default read policy`), since that caller chose nothing. A refused mode switch is reported the same way, naming the mode. The tally is per turn and appears in `--json` as `denied`. `--quiet` suppresses it. A `--bg` dispatch prints none — nothing has finished; the finished `log` footer carries the same data. `log` footers follow the same rule — stderr, `--` prefix — the general principle being: when stdout carries agent content, acpc's own metadata goes to stderr; when stdout is acpc's own view (`status`, `agents`), the footer is part of the view and stays there.
- **Early session line, on blocking `run`/`continue`**: at dispatch — before the turn has produced anything — one stderr line, `-- session <id> | dir <path>`. Its segments are identical in form to the end-of-run summary's own `session <id>` and `dir <path>` segments; harnesses merge both streams into one blob, so one spelling has to serve whether it is read at the start or at the end. It is what makes a blocking call self-sufficient: the id is in the captured output from the first moment, so `log` and `stop` work mid-run and a call the harness kills on its own timeout leaves a session the caller can still find rather than an orphan. The client prints it before the turn starts, so it is the same on the daemon path and on the direct-child fallback. `--bg` does not print it — stdout already carries the id and the dir — and `--quiet` suppresses it exactly as it suppresses the summary. stdout is untouched and stays byte-identical to `answer.md`.
- **Errors are one line and actionable**: not a stack trace, but `codex: not authenticated, run 'codex login'`. Damaged state gets the same treatment — an unparseable `meta.json` or transcript produces one line naming the file and a non-zero exit, never a traceback. Known spellings from neighboring tools get the same treatment instead of a bare "no such option": `-d`/`--detach` → `--bg`, `-C` → `--cwd`, and the command `logs` → `log` — each a usage error naming the acpc spelling. `-f`/`--follow` is a real flag on `log`; on any other command it gets the same hint, pointing at `log --follow`. The `daemon` group answers the docker/systemctl vocabulary the same way: `daemon list`/`ls`/`ps` name `daemon status`; `daemon stop --all` says that bare `daemon stop` already addresses every daemon; `daemon start`/`restart` give the recipe instead — daemons start on first use, so `daemon stop <agent>` plus the next run is the restart. Hints, never working aliases: a second spelling that works is a second name for one operation, and the point of answering a wrong guess is to teach the right one.
- **Never prompt interactively on stdin.** If something is missing, fail with instructions.

## TTY vs non-TTY

Behavior differs between a human at a terminal and an agent behind a shell tool in exactly these places. "TTY" means `isatty` on stdout. Redirection flips it: a human running `acpc run … > out.md` is non-TTY and gets the `read` default.

| | TTY (human) | non-TTY (agent) |
|---|-------------|-----------------|
| `--permissions` default | `ask` | `read` |
| Permission prompting | asks on `/dev/tty` | never; out-of-policy → denied. An `ask` policy — from the explicit flag or an entry's `permissions` — is a usage error (exit 2), not a silent downgrade |
| `last` selector | works | rejected — a stale "last" misleads an agent; name sessions explicitly |

`--bg` counts as non-TTY for permissions regardless of the terminal: once the client has returned, a prompt could never be answered — so the default is `read`, and explicit `--permissions ask --bg` is the same usage error. `ask` also excludes the daemon, which has no terminal to ask on, so such a call is always a direct child and pays a cold adapter start.

## State on disk

File-based state is a feature: the agent can grep it, read fragments selectively, and doesn't depend on the tool's own commands to inspect anything.

```
~/.acpc/                     # root; ACPC_HOME overrides it — the only env var that configures acpc itself
  config.toml                # global knobs — the complete file just below
  agents/<name>.toml         # variants, adapter overrides, new adapters — hand-editable; `agents init` is just a scaffold
  cache/<agent>/             # advertised models, modes, commands
  daemon/<entry>~<hash>.log  # adapter stderr per concrete target; daemon sockets and locks live here too
  sessions/<id>/
    meta.json                # resolved invocation + adapter vias + state, timing, tokens/cost, exit code, stop_reason, failure (latest turn's observation, cleared on rotation), prompt snippet, adapter session id (stored command is the entry's base string; --dry-run shows the spawn argv)
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

- **Adapter definitions are TOMLs shipped in the package**, one per adapter — the full contract: `command`, `install_command` (trusted one-liner for `acpc install`, optional), `install_docs` (vendor URL when there is no trusted installer), default `home`, `home_env` (the vendor variable the resolved home is exported as, e.g. `CODEX_HOME`), the `[modes]` table, `[presets]`, optional `[effort_by_model]` (per-model allowlists; the adapter's supported set is the derived union of those lists), `env_passthrough`, and the per-field apply paths `model_via` / `effort_via` / `effort_cli_flag` / `effort_config_id` (see *Agent variants*). A user file in `agents/` with `extends` is a variant; under an adapter's own name it overrides that adapter's fields (e.g. `[presets]`, one `[effort_by_model]` row); with a `command` and no `extends` it defines a new adapter. All at the trust level *Agent variants* states.
- **`ACPC_HOME` ≠ `--home`**: the state root vs the vendor config dir a callee runs against — they share a word, nothing else.
- **Owner-only**: 0700 dirs, 0600 files — prompts and transcripts routinely carry sensitive material.
- **No torn reads**: `meta.json` is replaced atomically, `transcript.ndjson` grows by whole lines only, `cache/` files and `-o` targets are written atomically too — a mid-write reader never sees garbage. A per-session lock serializes turns, so `run`, `continue` and `stop` on one session never interleave.
- **`answer.md` is written whatever the final state**: for `failed`/`timeout`/`cancelled` it holds the partial answer, and prose the adapter streamed before it died is kept rather than replaced by the diagnosis; for `failed` with nothing streamed it holds the recorded cause instead (see *Session states*); for `orphaned`, where the dead process wrote nothing, detection writes a one-line placeholder naming what died — the advertised path always exists and explains itself.
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
- **`cancelled` counts as finished and resumable**: `stop` → `continue` is the pause/resume path, and the adapter preserves the cancelled turn's context instead of dispatching from scratch.
- **`orphaned` counts as finished**: `continue` resumes it on the cold path (see `continue`), `rm` and `prune` delete it, `wait` returns immediately with exit 1 and the reason.
- **A `failed` session says why.** Every transition to `failed` records an `error` event in the transcript, whatever killed the turn — an adapter that exited, a connection torn mid-stream, a refused authentication, a command that was never installed. The event carries `observation`, what acpc itself saw; `next_step`, a single actionable instruction — the login command when the vendor refused credentials, a smaller turn when the adapter hit its own token or turn limit, otherwise the log to read; and `message`, the three of them joined into one line. On a turn that ran under a daemon it also carries `adapter_log` and `adapter_log_tail`: the per-target log's path, and the bytes *this turn* appended to it, bounded so one runaway line cannot flood a caller's context. That log is where the adapter's stderr is drained, so the tail is usually the adapter's own last words, but it also carries acpc's daemon lifecycle notes — hence the neutral name, because presenting an acpc line as something the adapter said would be the same dishonesty the transcript rules forbid. It is scoped to the turn for the same reason: the log is shared and append-only across every session the target ever ran, and quoting an earlier session's stderr would be a confident, wrong diagnosis. The tail is stored and quoted with ANSI escape sequences stripped — adapters style their stderr for a terminal, and a caller reading a transcript or an error message never is one; the raw bytes stay in the log file itself. Both fields are absent when the turn produced no log output of its own, and on a directly spawned adapter, whose stderr comes back on acpc's own stderr and reaches no log at all. A denied permission is not a failure of this kind and records no such event: acpc refused it, and the summary already names the policy that would admit it. The `message` is what `answer.md` falls back to when the turn produced no prose, so the promise that `answer.md` always explains itself holds for `failed` and not only for `orphaned`. Callers do not have to parse the transcript to get it: `log` renders error events like any other, `wait` appends the message to its stderr summary as a `failure:` segment, and `status <id>` shows the observation on its `failure` line for as long as the failed turn is the session's latest — the observation is written to `meta.json` at finalization precisely so that `status` can honor its fixed-cost promise without touching the event stream.
- **Who accepts what**: `stop` acts on `starting`/`running`, is a no-op on finished states, errors on unknown IDs. `continue` accepts any finished state, errors on `running`. `rm` errors on `starting`/`running`; `prune` never touches them. `wait`/`log`/`status` accept everything.

## `--help` as first-contact documentation

The recommended primary channel for usage docs is a short snippet in the caller's own context (AGENTS.md or a skill) — but the tool cannot assume it's there, so `--help` is the self-contained fallback. Two levels, one source:

- **`acpc --help`** — the cheat sheet, ≤100 lines, complete for the 90% path on its own. Grouped by the decision the caller is actually making, in the order they make it: short task (blocking) · long or uncertain task (`--bg` + `wait`) · checking on a run · supervising one they intend to steer or stop (`--follow`) · steering · continuing · heredoc prompt · context care · maintenance and setup. Write-task examples carry `--permissions edit` or `--permissions execute`, and the sheet says which: `edit` writes files but runs nothing, so a callee under it cannot run the tests it just wrote. Each group names the cost or the failure it prevents, not just the syntax — the sheet is where an agent learns that `wait` already prints the answer and the file is the fallback for a truncated or huge one, that `--follow` is for one case, and that killing `acpc` does not stop the session. Ends with the command list and a flag → ACP mapping table, 3-4 lines (`--permissions` → `session/set_mode` *and* the `request_permission` answers, `--cwd` → `session/new`, …) — the sheet has to show that one flag drives both, or a reader will look for the second knob.
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
