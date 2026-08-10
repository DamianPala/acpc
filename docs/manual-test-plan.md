# Manual test plan

Checks against real adapters, to be run by an agent. The automated suite runs against
`tests/mock_agent.py`, so it cannot see what a vendor actually advertises, how long a cold start
takes, whether two vendor homes really stay apart, or whether a daemon survives a day of use.
Everything here is chosen because the suite structurally cannot answer it.

Run `uv run pytest` first. If it is red, stop.

## Rails

- **Isolate the state root.** `ACPC_HOME` is the *only* environment variable that configures acpc
  itself; there is no `ACPC_STATE_DIR`, `ACPC_CONFIG_DIR` or `ACPC_USER_AGENTS_DIR`. Set it, or you
  are testing against the operator's live daemons and sessions. (A 2026-08-10 run listed the
  operator's real variants before this was caught — the plan itself was the hazard.)
- **`ACPC_HOME` is not `--home`.** `ACPC_HOME` is acpc's own state root; `--home` is the vendor
  config dir the callee runs against. They share a word and nothing else.
- **Never kill by pattern.** `pkill -f acpc` and `pgrep -f "acpc.daemon"` are forbidden: the `.` is
  a wildcard and any process merely mentioning `src/acpc/daemon.py` matches. A dispatch died this
  way on 2026-08-04. Use `acpc daemon stop <target>`, or signal a PID read from
  `acpc daemon status`.
- **Do not modify any vendor home** (`~/.codex`, `~/.claude`) and do not commit anything.
- **Keep prompts trivial** (`Reply with exactly: OK`). These are live, billed calls.

```bash
cd "$(git rev-parse --show-toplevel)"   # the acpc checkout
export ACPC_HOME=$(mktemp -d /tmp/acpc-manual-XXXX)
mkdir -p "$ACPC_HOME/agents"
uv run acpc --version   # record it; no result below is portable across builds
```

Run the CLI under test as `uv run acpc` from the repo. A bare `acpc` is the *installed* snapshot,
which is a different build — mixing them invalidates every timing and version result here.

Clean up with `trash-put`, never `rm -rf`.

## Observing a run

When stdout carries agent content — `run`, `wait`, `log` — it carries exactly one thing (the
answer, or a confirmation, or JSON) and acpc's own `--`-prefixed lines go to stderr. Redirect them
separately or you cannot tell them apart. The exception is a view that *is* acpc's own output:
`status` and `agents` keep their `--` footer on stdout, because there it is part of the view.

- `acpc log <id>` — condensed snapshot, the first thing to read when a run looks stuck.
- `acpc log <id> --tail N` / `--since <cursor>` — narrower windows; the footer's cursor resumes.
- `acpc log <id> --prose` — what it is actually saying, as markdown.
- `acpc log <id> --json` — raw transcript events (NDJSON), for when a rendered line is ambiguous.
- `acpc status <id>` — vitals only, never the event stream.

State lives at `$ACPC_HOME/sessions/<id>/`: `answer.md`, `prompt.md`, `meta.json`,
`transcript.ndjson`. Adapter stderr in daemon mode goes nowhere else but the per-target log named
by `acpc daemon status`.

`$?` after a pipe is the exit code of the *last pipe stage*. Several readings in the 2026-08-10
run measured `head` instead of acpc. Capture the code before piping.

## A. Resolution

| # | Do | Expect |
|---|---|---|
| A1 | `acpc agents` | Every adapter with install status. On a fresh `ACPC_HOME` there are no variants yet, so no variant rows and no header — the header appears at B4, once a variant exists |
| A2 | `acpc agents codex` | Resolved model, effort, permissions, home, each with provenance; a `modes` line naming every mode's `grants`, with a ` · delegates` marker on any mode that delegates (on codex none do, so the marker is absent everywhere — that is the pass) |
| A3 | `acpc run codex "x" --dry-run` | Exit 2: the non-TTY default `read` fits no codex mode, and the error names the adapter's declared modes. This is the real first-contact experience, not a defect |
| A4 | `acpc run codex "x" --permissions edit --dry-run` | Full resolution, including the mode line naming why that mode was selected and whether it delegates |
| A5 | `acpc run codex "x" --permissions prompt --dry-run` | A one-time deprecation warning naming `ask`, then `ask` on non-TTY is a usage error listing the alternatives |
| A6 | `acpc run codex "x" --help` and `acpc --help` | Defaults stated as rules where they depend on the caller; the usage line for `--permissions` does not offer `write`/`prompt` as first-class choices |

## B. Cold start and reuse

| # | Do | Expect |
|---|---|---|
| B1 | Time a `run` from a clean `ACPC_HOME` | A daemon starts; record the wall time (2026-08-10 baseline: 6.0s) |
| B2 | Repeat it | Faster (baseline 4.7s); `acpc daemon status` shows the same PID |
| B3 | `acpc daemon status` | One row per concrete target with the acpc version, PID, uptime, idle age, session count and log path |
| B4 | Create a variant declaring its own `home`, run it | A **second** daemon; both alive with different PIDs and different target hashes |
| B5 | `acpc daemon stop <target>`, run again | Clean stop (`no daemons running`), then a fresh cold start on a new PID |

If B4 lands both calls on one daemon, stop and report it. That is credential crossing, not a
performance quirk. Note that a foreign vendor home may have no credentials — codex auth lives in
the system keyring and does not follow `--home` — so the variant's *turn* may fail
`Authentication required`. Two daemons is the pass condition; the turn succeeding is not.

## C. Sessions

| # | Do | Expect |
|---|---|---|
| C1 | `run` with a memorable codeword in the prompt | The early `-- session <id> \| dir <path>` line at dispatch, and an end-of-run summary naming the continue command |
| C2 | `acpc continue <id> "what codeword did I give you?"` | The codeword recalled; the prior transcript **not** reprinted into the answer |
| C3 | `acpc daemon stop <target>`, then `acpc continue <id> "..."` | A cold resume through a fresh daemon reattaches to the same conversation; `answer.md` holds only the new turn, never replayed history |
| C4 | `acpc continue last "..."` with stdout redirected to a file | Rejected, exit 2 — `last` is TTY-only, and a stale "last" misleads an agent |
| C5 | `acpc status`, then `acpc status <id>` | Listed with resolved model and idle age; then that session's vitals |
| C6 | `acpc stop <id>` on a running session | State `cancelled`, **still listed**, and resumable with `continue` — cancelled counts as finished, not deleted |
| C7 | `acpc steer <id> "Stop that; reply with exactly STEERED"` mid-turn | The turn is interrupted, the instruction lands under the fixed preamble, the interrupted turn's partial answer is kept as `answer.<n>.md` |
| C8 | `acpc steer <id> "..."` on a finished session | Usage error naming `continue` |

C3 is the one worth doing carefully: it is the only check that a session survives its daemon.
The *direct-spawn* path is not reachable from the CLI today — no flag forces it — so any check
that depends on the daemonless route is out of scope here.

## D. Environment and identity

| # | Do | Expect |
|---|---|---|
| D1 | Run an entry declaring `env_passthrough`, with the variable **set** | Works; note the target hash from `acpc daemon status` |
| D2 | The same with the variable **unset** | A **different** target hash and a separate daemon, so the call cannot spend another caller's credential |
| D3 | Prompt the callee to print its own environment (`--permissions execute`) | `ACPC_CEILING` at the resolved rung and `ACPC_HOME` present; no `ACPC_DAEMON_ENV_PAYLOAD`, no `ACPC_PARENT_SESSION`, and an unrelated exported variable does not leak through |
| D4 | Run `acpc` itself from inside a callee at `--permissions execute` | The nested call resolves to the lower of the two ceilings and reports the clamp |

D2 shipped broken once and was caught by review, not by tests. Measure it, do not reason about it.

## E. Permissions

Use **claude** for the delegating checks: on codex no mode delegates, so nothing is ever asked of
acpc and the boundary is entirely vendor-side. That is by design, not a defect.

| # | Do | Expect |
|---|---|---|
| E1 | claude, `--permissions read`, prompt asks to create a file | The file is not created; the answer says so honestly |
| E2 | `--json` on that run | `denied` as an array of self-describing records, each with category, count, `minimum_policy` and a `remedy` an orchestrator can retry on mechanically |
| E3 | `--permissions none` on codex | Exit 2 naming the modes and their grants — no codex mode grants at most `none` |
| E4 | `--permissions ask` on a **real TTY** (operator only) | A y/n question on `/dev/tty`; answering `y` lets the write through |
| E5 | codex, `--permissions edit`, prompt that shells out to write a file | Record what happens. A write landing with zero permission events is the known vendor behavior (`read-only` escalates through an in-vendor reviewer); report the transcript, not a verdict |

If a refusal appears only as prose on stderr, that is the defect: an orchestrator cannot retry
with the missing permission.

E4 needs a human terminal; everything above it is agent-runnable.

```bash
# E4 — for the operator, in a real terminal
cd "$(git rev-parse --show-toplevel)"   # the acpc checkout
export ACPC_HOME=$(mktemp -d /tmp/acpc-e4-XXXX)
uv run acpc run claude "Create a file called e4-proof.txt with content OK in $ACPC_HOME" --permissions ask
# expect a y/n question on your terminal; answer y; the file should exist afterwards
uv run acpc daemon stop claude
trash-put "$ACPC_HOME"
```

## F. Failure paths

| # | Do | Expect |
|---|---|---|
| F1 | Run an entry whose adapter binary is not installed | An actionable error naming the install command |
| F2 | `--model` the vendor rejects | The vendor's own refusal plus the adapter log path, not a generic failure |
| F3 | SIGKILL the adapter mid-turn, by PID from `acpc daemon status` | State `failed`; the cause is agent-visible — what acpc observed, the adapter's last stderr, and one next step |
| F4 | An `ACPC_HOME` spelling containing `..`, then run | The daemon is reachable with no stall, and no 15s hang followed by a direct-child fallback |
| F5 | `acpc status zzzz` (a session id that does not exist) | `unknown session 'zzzz'`, local state intact |
| F6 | Two concurrent runs on one target | Both complete correctly; the second may report queueing |
| F7 | `acpc wait <id> --timeout 1` on a running session | Exit 124, a stderr note saying the session continues, and the session still `running` afterwards |

F3 was measured as a silent death on 2026-08-10 (state `failed · exit 1` and nothing else); the
expectation above is the 0.6 contract. F4 is here because it broke once: client and daemon derived
different *socket paths* from two spellings of one directory, so a healthy daemon sat on a socket
nobody dialled. The target key is unaffected — it digests the entry, the vendor home, declared env
and the policy, never `ACPC_HOME` — so both spellings share one target and one lock file. Check
reachability, not the target hash.

## G. Teardown

Stop each daemon you started, confirm no survivors by the PIDs you recorded, and `trash-put` the
temporary directories.

```bash
uv run acpc daemon stop            # bare stop addresses every target
uv run acpc daemon status          # expect: no daemons running
trash-put "$ACPC_HOME"
```

A daemon that ignores `SIGTERM` is worth reporting with its age and how it was started. A healthy
one exits in about two seconds; one whose virtualenv was deleted underneath it is known to need
`SIGKILL`.

## Reporting

One section per group, per step: what you ran, what happened, pass or fail.

- **Quote the output.** "Worked as expected" is not a result.
- **State the acpc version, the vendor CLI and adapter versions, and the platform.** Vendor
  behavior drifts between adapter releases — a result without versions cannot be compared to the
  next run.
- **Separate a product failure from an environment one.** A sandbox refusing Unix socket binds
  looks exactly like a broken daemon path. Say which, and how you know.
- **Say what you skipped and why.**
- End with concrete friction you hit. You are the only one seeing the tool from outside.
