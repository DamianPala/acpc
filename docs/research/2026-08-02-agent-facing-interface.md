# acpc: the agent-facing calling surface

Written from the seat of the caller: an orchestrating LLM (me) deciding whether and how to shell
out to `acpc run <agent> "..."`. Compares acpc's current CLI against my own built-in Agent tool,
which does the same job (spawn another agent, get a result back) one layer down, in-process.

Two facts already established (not re-derived here, see `docs/plans/2026-08-02-acpc-v0.3-polish.md`
section 2b): `--permissions` answers ACP permission requests, it is not a sandbox; `--mode` is a
second, adapter-specific vocabulary that acpc passes through unvalidated and codex-acp 1.1.9 does
not enforce (upstream PR #337 open).

## 1. My Agent tool as a design artifact

Everything below is read directly off the tool schema and description shown to me this session,
or from behavior I've observed. Marked `[inferred]` where I'm reading intent into a gap rather
than citing a stated fact. Marked `[unsure]` where I don't have enough to say.

### 1.1 Parameter set

| Param | Required | What it does |
|---|---|---|
| `description` | yes | 3-5 word label, shown to the user watching, not to the sub-agent |
| `prompt` | yes | the entire task. No separate structured-context channel — everything, including files to read and constraints, goes in this one string |
| `subagent_type` | no | selects a pre-declared agent persona (tools, model, reasoning effort fixed in its definition file) |
| `model` | no | overrides that persona's model for this one call only — the *only* per-call override that exists |
| `isolation` | no | `"worktree"` (isolated git worktree, auto-cleaned if no changes) or `"remote"` (cloud sandbox, always background) |
| `name` | no | makes the spawned agent addressable later via `SendMessage` — turns a fire-and-forget call into a resumable one |
| `run_in_background` | no | in most sessions, background by default; **this session it's disabled — only synchronous calls work**, per a system-reminder override |
| `mode` | no | **deprecated, ignored.** Docs say: subagents inherit the parent session's permission mode; agent-definition frontmatter *may* override it |
| `team_name` | no | deprecated, ignored |

What's conspicuously absent: no `cwd` (runs wherever the parent runs, no per-call override), no
`timeout`, no `env`, no explicit tool allow/deny list on the call itself. Tool access is a property
of `subagent_type`'s static definition, not a call-time argument — I select a *persona*, not a
capability set.

### 1.2 Permissions and modes — how inheritance actually works

Verified from my own instructions: a subagent **inherits the parent session's permission mode**.
The `mode` parameter that would let a caller pick a different one explicitly is deprecated and
ignored. The only lever left is the agent-definition frontmatter, which "may override" — direction
unstated. `[unsure]` whether frontmatter can widen past what the parent allows, or only narrow.
I have never seen it grant something my own session lacked, which is *consistent* with narrow-only,
but I can't confirm that as a rule from what I've been told.

Tool access is separate from permission mode and is fixed per `subagent_type` at definition time
(`Explore`: "All tools except Agent, Artifact, ExitPlanMode, Edit, Write, NotebookEdit" — a static
denylist baked into that type, not something I pass at call time).

This session also demonstrates that the capability surface itself can be stripped per calling
context: `run_in_background` and `name` are listed as present in the schema but disabled for me
right now, because I am myself a subagent, not the top-level session. That's a real, observed
mechanic: **the set of parameters a caller may use is not fixed by the schema alone — the hosting
context can narrow it further at dispatch time**, and it says so plainly rather than silently
no-op'ing.

### 1.3 What comes back

"When the agent is done, it will return a single message back to you." Unstructured prose. No
exit code, no machine-readable status field, no separation of "the subagent completed the task"
from "the subagent tool itself failed to spawn." If I want that distinction I have to infer it
from the wording of the message. There is explicit guidance to keep the report short when I want
a summary rather than a raw transcript ("report in under 200 words") — but that's a prompting
convention I impose, not something the tool enforces or returns structured.

### 1.4 What makes it usable without reading docs

- The description does real work: it states the decision rule ("known target → direct tool;
  open-ended → this tool") right where I'd otherwise guess.
- Two worked `<example>` blocks show a full call shape end to end, including how the result gets
  relayed back to the user afterward.
- The enum of valid `subagent_type` values is **not in this schema** — it lives in a separate
  system-reminder block listed alongside it. The tool is unusable correctly from its own schema in
  isolation; it depends on a sibling message being present in context. That's a split I'd flag as
  a wart if I were reviewing it as someone else's design.
- No output schema is given, so I can't validate or route on the return value — only read it.

## 2. When I actually reach for it

| Situation | What I pass | What I need back | When I skip it |
|---|---|---|---|
| Broad "what's the state of X" question spanning many files I don't want to read myself | task + explicit response-length cap | a short synthesized answer, not raw grep output | target file/symbol already known → read it directly, cheaper and I keep the full trace |
| Independent second opinion (adversarial review, "did I miss something") | facts and the artifact to review, deliberately *not* my own conclusion, so the sub-agent isn't anchored on it | a verdict plus reasoning I can cross-check, not agreement | I already have high confidence and just need to execute |
| Several independent research angles at once | one call per angle, dispatched in the same turn (parallel) | short reports per angle to synthesize myself | the angles aren't actually independent — sequential work with shared state serializes anyway |
| Task needs a different capability profile than mine (fast read-only search, a narrower tool set) | pick `subagent_type` for the profile, not just to delegate work | results shaped by that profile (e.g. Explore returns file locations, not edits) | my own tools already cover it — an extra hop adds latency and a context-summarization loss for no gain |
| Risky/exploratory change I don't want touching my working tree | `isolation: "worktree"` | a branch/path back if it made changes, silent cleanup if it didn't | change is small and reversible enough that I'd rather just make it and let git diff be the safety net |

Honest failure mode: spawning when the task is small enough to just do. The subagent starts cold,
re-derives context I already have, and the round trip costs more than doing it inline — this is
called out explicitly in my own tool guidance ("don't spawn agents unless asked... it's the
expensive path").

## 3. The acpc surface I'd want

### 3.1 Defaults when everything is omitted

Already close to right and I wouldn't touch the core of it: non-TTY caller (i.e., me) gets
`--permissions read` and the daemon path with zero flags. That's the correct default for "I don't
know yet whether I trust this call" — same instinct as my Agent tool defaulting to inherit rather
than to `bypassPermissions`.

**Gap:** nothing in that default path also picks a matching adapter *mode*. A caller who wants a
read-only default gets an ACP-answer policy of `read`, but the adapter itself may still be running
in a mode that permits writes it never happens to ask permission for (codex's default `agent`
mode, verified: network blocked, filesystem writes not gated by a permission request). Silent gap
between "acpc will refuse writes it's asked about" and "the adapter might not ask."

**Proposal 3.1:** on session creation, always request the adapter's most restrictive advertised
mode consistent with the chosen `--permissions` level, best-effort, and say on stderr whether the
adapter is known to enforce it.
Cost: one more adapter-specific mapping table to maintain (like model presets, item 2b.4 in the
polish plan), and it can't promise anything for codex-acp 1.1.9 today — has to ship as "requested,
not guaranteed" or it's a lie. Breaks nothing; it's additive to the current silent behavior.

### 3.2 One coherent story for permissions + mode

My Agent tool's answer to "two incompatible vocabularies" is to not expose the second one at
call time at all: permission posture is inherited, tool/capability profile is a static property
of the persona you pick, and the only per-call escape hatch is a single, narrow one (`model`).
It doesn't try to reconcile codex-shaped and claude-shaped mode enums into one dial — it just
never lets the caller touch that layer directly.

acpc can't fully copy this: it has no ambient parent session to inherit from (each invocation is
a fresh process), and unlike my tool, its whole reason to exist is exposing per-call control to
whatever orchestrator invokes it. But the *shape* of the fix applies:

**Proposal 3.2a — `--sandbox LEVEL`** (levels: `none`/`read`/`write`/`all`, same names as today's
`--permissions` so nothing new to learn) becomes the one flag a caller sets. It expands internally
to both the ACP permission-decision policy (today's `--permissions`, unchanged) *and* a best-effort
mode request via the same kind of per-agent mapping table `acpc models` already has for model
tiers. `--permissions` and `--mode` remain as raw, individually-settable escape hatches for a
caller that knows exactly what it wants and needs to bypass the mapping — but they stop being the
first thing documented or defaulted to.
Cost: a mapping table that will be wrong or incomplete for some future adapter, and a second name
(`--sandbox`) that means almost but not quite the same thing as `--permissions`, which is exactly
the kind of near-duplicate flag pair that caused this problem in the first place. Only worth it if
the mapping table is honest about adapters (like codex today) where the mode half is a no-op.
Breaks: nothing existing, `--permissions`/`--mode` keep working standalone.

**Proposal 3.2b — reject contradictory explicit pairs** (already item 2b.3 in the polish plan,
endorsing it here from the caller's side): `--permissions none --mode bypassPermissions` should
be a usage error (exit 2), not a silent accept. This is cheap and should ship regardless of 3.2a.
Cost: a small validation table per adapter, and it has to special-case "adapter doesn't enforce
mode anyway" (codex) or the rejection message overclaims a guarantee acpc can't back.

**On "narrow but never widen" for a CLI:** the inheritance model my Agent tool uses only works
because a subagent lives inside the same host process as its parent, which can read the parent's
mode. acpc has no such channel — it's a subprocess with no notion of "what permission mode is the
orchestrator running under." The nearest equivalent would be an environment variable a *supervising*
process sets, e.g. `ACPC_MAX_PERMISSIONS=read`, that acpc clamps every `--permissions` request
against regardless of what the leaf caller (a prompt-generated shell command, say) asks for.
`[inferred, not in current codebase]` — I did not find this in cli.py; it doesn't exist today.
Cost: one more env var to document, and it only helps if the *orchestrator* sets it — a call made
directly by a human or a script with no supervisor gets no benefit and no different default than
today. Worth proposing but not worth blocking 3.2a/b on.

### 3.3 What the tool returns

The exit code table (README, already thorough: 0/1/2/3/124/130/141/143) is a real advantage over
my Agent tool, which has no exit-code equivalent at all — I only get prose back and must infer
success from it. acpc should lean into that advantage rather than flatten it.

**Problem, verified in `cli.py`:** exit 1 is overloaded. `stderr_error(f"unexpected error: {e}")`
at the bottom of every command's `except Exception` catches both genuine agent-side failures
(`max_tokens`, ACP `RequestError`) *and* acpc's own bugs or adapter-spawn failures, all under the
same code. A caller cannot tell "the agent tried and failed" from "acpc itself broke" without
parsing the stderr message text — which is exactly the failure mode structured exit codes exist
to avoid.

**Proposal 3.3 — split exit 1.** Reserve 1 strictly for agent-side failures the adapter reported
(`max_tokens`, `max_turn_requests`, ACP error responses). Add a new code, e.g. 5, for acpc-internal
failures: adapter process wouldn't start, daemon protocol violation, uncaught exception in acpc's
own code. This is the one distinction that matters most for an orchestrator deciding whether to
retry (internal failure: maybe retry) versus give up or change the prompt (agent failure: retrying
identically won't help).
Cost: touches every `except Exception` handler in `cli.py` (roughly a dozen), and any existing
script keying off "exit 1 = something went wrong, don't care what" needs no change, but a script
that specifically expects agent-failure semantics on 1 today gets some of that traffic redirected
to 5. Breaking change for exit-code-sensitive callers; needs a version bump and changelog entry,
not a silent patch.

stdout/stderr split is already right (agent output only on stdout, `[acpc]`-prefixed diagnostics
on stderr) and needs no change.

### 3.4 Self-explanatory failure without a second call

**Proposal 3.4 — one categorized error line before every non-zero exit.**
`[acpc] error: <category>: <message>` where category is one of `usage`, `agent`, `permission`,
`timeout`, `adapter`, `daemon`, `internal`. A caller that only greps stderr for `^\[acpc\] error:`
gets a classification without needing `--json`, without a second `acpc daemon status` call, and
without string-matching the free-text message.
Cost: another compatibility surface (category names become semi-public API once a caller greps
for them), and every existing `stderr_error(...)` call site (there are ~15 in `cli.py`) needs a
category argument added. Doesn't break existing text-matching callers since it's a prefix addition,
not a reformat.

### 3.5 Discoverability: what `--help` must contain for zero-prior-knowledge use

Checked directly, not assumed: `acpc --help` carries the full `CHEAT_SHEET` epilog. `acpc run
--help` and `acpc prompt --help` — the two commands a model reaches for immediately once it knows
it wants to send a prompt — carry **no epilog at all**, just the flag list. A caller that skips
straight to `acpc run --help` (the more specific, more likely lookup) sees none of the cheat sheet,
including the one line that would save it from the single most likely silent failure for this
exact audience:

**Proposal 3.5a** — copy (or a trimmed version of) `CHEAT_SHEET` onto `run`/`prompt`'s own
`--help`, and add the line the current sheet is missing entirely: *"Piped/non-interactive callers
default to `--permissions read` — writes silently fail with nothing changed, not an error, unless
you pass `--permissions write` or `--permissions all`."* This is the TTY-default trap from polish
plan item 2.2, stated where an agent will actually see it.
Cost: near zero, text-only change; keeping two copies of a cheat sheet in sync if the top-level one
changes. Consider making `run --help`'s epilog a strict subset (flag-relevant lines only) rather
than a full duplicate, to reduce drift risk.

**Proposal 3.5b** — one line stating mode enforcement is adapter-dependent and not guaranteed
today, so an agent reading `--help` cold doesn't build a safety assumption `--mode read-only`
can't currently back for codex. Cheap, prevents a caller from shipping a false sense of sandboxing
based on `--help` text alone, which is worse than no claim at all.

## 4. Summary of proposals and their cost

| # | Proposal | Cost | Breaks |
|---|---|---|---|
| 3.1 | Request matching adapter mode from `--permissions` at session start, best-effort | one mapping table per adapter, can't promise codex compliance | nothing, additive |
| 3.2a | `--sandbox LEVEL` single flag, maps to permissions + mode | new near-duplicate flag name, mapping table maintenance | nothing, `--permissions`/`--mode` stay as escape hatches |
| 3.2b | Reject contradictory `--permissions`/`--mode` pairs | small per-adapter validation table | scripts currently relying on silent accept (unlikely, undocumented behavior) |
| 3.2 (env) | `ACPC_MAX_PERMISSIONS` clamp for supervising processes | one more env var to document | nothing; opt-in |
| 3.3 | Split exit 1 into agent-failure vs acpc-internal (new code 5) | touches ~12 exception handlers | yes — exit-code-sensitive callers expecting all failures on 1 |
| 3.4 | Categorized `[acpc] error: <category>:` prefix | touches ~15 call sites, category names become semi-API | nothing, prefix addition |
| 3.5a | Copy cheat sheet onto `run`/`prompt --help`, add TTY-default warning | text only, sync risk between two copies | nothing |
| 3.5b | State mode non-enforcement in `--help` | text only | nothing |

Ranked by value for the least cost: 3.5a and 3.5b first (near-free, close the exact trap this
audience hits first), then 3.4 and 3.2b (cheap, immediately actionable by a caller with no other
change required), then 3.1, then 3.3 (real value but a breaking change, save for a version bump),
then 3.2a last — it's the most expensive and the least urgent, since `--permissions`/`--mode` as
two separate documented flags is livable once 3.5b tells a caller not to trust the mode half.

## 5. Review notes

Added by the orchestrating session after checking sections 1-4 against the code.

### 5.1 What checked out, and what did not

| Claim | Verdict |
|---|---|
| `run --help` and `prompt --help` carry no epilog | Confirmed. Only top-level `acpc --help` has the cheat sheet |
| `--permissions` levels are `none`/`read`/`write`/`all` | Confirmed, plus `prompt`, which section 3.2a's level list silently drops |
| Exit 1 is overloaded across agent failures and acpc's own crashes | Confirmed in substance |
| "~15 `stderr_error` call sites" | Undercounted. 30 in `cli.py`, so proposal 3.4 costs roughly twice what it claims |
| "`except Exception` at the bottom of every command" | 8 handlers, not every command. The gap is real but narrower than stated |
| The exit code table is "already thorough" and an advantage worth leaning into | **Wrong, and the opposite is true.** See below |

### 5.2 The documented exit code that never happens

`EXIT_PERMISSION_DENIED = 3` is defined at `runner.py:40`, documented in the README table, and
**never returned from anywhere**. The only reference outside the definition is
`tests/test_runner.py:129`, `assert EXIT_PERMISSION_DENIED == 3`, which asserts a literal against
itself and would pass no matter what the program does.

So the surface an agent is most likely to trust, a documented exit code, is where acpc is least
truthful. A caller that denies a write and checks for exit 3 gets a code that cannot arrive, and
the tautological test guarantees nobody notices. This has to be fixed before any of 3.3's
refinements are worth building: adding exit code 5 to a table that already contains a fictional
entry makes the table less trustworthy, not more.

Fix is a fork, not a detail. Either return 3 when a permission request is denied and the turn
produced nothing, or delete the constant and the README row. The first is more useful to an
orchestrator and is the harder change, because "denied and therefore useless" and "denied but the
agent worked around it and answered anyway" are different outcomes and only the first deserves a
non-zero code.

### 5.3 Permissions are per-prompt, mode is per-session

This is the load-bearing objection to 3.2a and section 3 does not account for it.

Under the daemon, `--permissions` travels in each `prompt` frame and is evaluated for that prompt
alone, while `--mode` is set on the session and only when it differs from what the session record
already holds. Two callers sharing one warm session can therefore hold different permission
policies at the same time, but they cannot hold different modes: the second caller's mode either
silently applies to the first caller's session or is skipped as unchanged.

Collapsing both axes behind one `--sandbox` flag would hide that asymmetry rather than resolve it.
A caller would set one value and get one half honoured per-call and the other half shared with
whoever else is on that session. That is worse than two honest flags. Either `--sandbox` forces a
fresh session when the mode differs, which costs the warm-start saving that justifies the daemon,
or 3.2a stays unbuilt. My read: leave it unbuilt, and say plainly in `--help` that mode is a
session property while permissions are a call property.

### 5.4 acpc does inherit, just not through ACP

Section 3.2 says acpc has no ambient parent to inherit posture from. That is true at the protocol
layer and false at the process layer. When an orchestrator shells out to acpc, acpc is a child of
whatever sandbox the orchestrator's own shell tool runs under, and every adapter it spawns sits
inside that same boundary. The kernel already does the inheritance, and it is the only part of
this system that is actually enforced rather than requested.

This makes `ACPC_MAX_PERMISSIONS` less valuable than 3.2 suggests, not more. It would be an
advisory clamp inside a boundary that already holds, defending against a caller that asks for too
much rather than one that is malicious. Worth having for legibility, worth nobody's time to build
before 5.2 and 3.5a.

### 5.5 Where acpc already beats the tool it is being compared to

Section 1.4 flags that valid `subagent_type` values live outside the tool schema, so the tool
cannot be used correctly from its own definition. acpc does not have that problem: `acpc agents`
and `acpc models` are discovery commands, so the enum is queryable rather than ambient. Section
2b.1 of the polish plan proposes the same for modes. That is the right instinct and this document
should be read as reinforcing it, since the one place acpc's own design already diverges from the
in-process tool is the place the in-process tool is weakest.

### 5.6 One thing missing from the proposals

The Agent tool has a `description` field that exists only so a human watching can tell what a
spawned agent is doing. acpc has no equivalent, and under the daemon it needs one more than the
in-process tool does: `daemon status` lists sessions by id, cwd, and state, with nothing about
what any of them is for. A `--label` carried into the session record and printed by
`daemon status` would cost almost nothing and is the concrete form of polish plan item 1.3.
