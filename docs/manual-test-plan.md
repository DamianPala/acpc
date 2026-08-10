# Manual test plan

Checks against real adapters, to be run by an agent. The suite runs against `tests/mock_agent.py`, so it cannot see what a vendor actually advertises, how long a cold start takes, or whether a daemon survives a day of use.

Run `uv run pytest` first. If it is red, stop.

## Rails

- **Isolate the state directory.** Otherwise you stop the operator's live daemons and lose their sessions.
- **Never kill by pattern.** `pkill -f acpc` and `pgrep -f "acpc.daemon"` are forbidden: the `.` is a wildcard and any process merely mentioning `src/acpc/daemon.py` matches. A dispatch died this way on 2026-08-04. Use `acpc daemon stop TARGET`, or signal a PID read from `acpc daemon status`.
- **Do not modify `~/.config/acpc/`** or commit anything.
- **Keep prompts trivial** (`Reply with exactly: OK`). These are live calls.

```bash
export ACPC_STATE_DIR=$(mktemp -d /tmp/acpc-manual-state-XXXX)
export ACPC_CONFIG_DIR=$(mktemp -d /tmp/acpc-manual-config-XXXX)
export ACPC_USER_AGENTS_DIR="$ACPC_CONFIG_DIR/agents"
mkdir -p "$ACPC_USER_AGENTS_DIR"
acpc --version   # record it; no result below is portable across builds
```

Clean up with `trash-put`, never `rm -rf`.

## Observing a run

stdout is the response, stderr is diagnostics; redirect them separately. `acpc status -s <id> --tail N` gives the last milestones of a live session and is the first thing to read when a run looks stuck. After it ends the same stream persists under `$ACPC_STATE_DIR/output/<id>.md`, and `--tail` names that path for you. `--json` gives NDJSON events when a human-readable line is ambiguous.

The shell log of a backgrounded run holds raw tool invocations, not the milestone stream. It answers a different question.

## A. Resolution

| # | Do | Expect |
|---|---|---|
| A1 | `acpc agents` | Every agent with install status, variants under their base |
| A2 | `acpc agents codex` | Resolved model, effort, permissions, home, and where each came from |
| A3 | `acpc prompt codex "x" --dry-run` | What this call resolves to and why, with no adapter contacted |
| A4 | `acpc prompt codex "x" --dry-run --permissions prompt` | The routing reported is the routing taken, not `daemon: enabled` for a call that runs direct |
| A5 | `acpc --help` | Defaults stated as rules where they depend on the caller |

## B. Cold start and reuse

| # | Do | Expect |
|---|---|---|
| B1 | Time a prompt from a clean state directory | A daemon starts; record the wall time |
| B2 | Repeat it | Faster; `acpc daemon status` shows the same PID. Record this time too |
| B3 | `acpc daemon status` | One daemon per target, with socket, PID and version |
| B4 | Prompt a variant that declares its own vendor home | A **second** daemon, both alive |
| B5 | `acpc daemon stop TARGET`, prompt again | Clean stop, fresh cold start |

If B4 lands both calls on one daemon, stop and report it. That is credential crossing, not a performance quirk.

## C. Sessions

| # | Do | Expect |
|---|---|---|
| C1 | Prompt with something memorable | A resume line naming the exact command to continue |
| C2 | `acpc prompt codex -s <id> "what did I just tell you?"` | Context present, prior transcript **not** reprinted |
| C3 | `acpc prompt codex --last "..."` non-interactively | Refusal with exit 2, not a silent new session |
| C4 | `acpc status`, then `acpc status -s <id>` | Listed, then the full record |
| C5 | `acpc sessions codex` | See below |
| C6 | `acpc stop -s <id>` | Ends; no longer listed |

C5 was measured on 2026-08-04: `claude` returns thousands of sessions unscoped, including ones acpc never created, with their titles; `codex` returns **zero items with a non-null cursor** when cwd-scoped, so ignoring pagination reports nothing for an agent that has sessions; `gemini` advertises no session capabilities. An empty listing proves nothing until you know which case you are in.

## D. Environment and identity

| # | Do | Expect |
|---|---|---|
| D1 | Prompt an entry declaring `env_passthrough`, variable set | Works; the target carries an environment digest |
| D2 | The same, variable **unset** | A **different** target, so the call cannot spend another caller's credential |
| D3 | Have the adapter print its own environment | No unrelated caller variables; no daemon payload variable |
| D4 | The same with `--no-daemon` | Identical to D3 |

D2 and D4 both shipped broken and were caught by review, not by tests. Measure them.

## E. Permissions

| # | Do | Expect |
|---|---|---|
| E1 | `--permissions read` with a prompt that writes | Refused |
| E2 | `--json` on that run | A `permission` event per decision, and a terminal event naming what was refused and the least permissive policy that would have allowed it |
| E3 | `--permissions none` | Everything denied, turn still ends cleanly |
| E4 | `--permissions prompt` on a TTY | A human is actually asked |

If a refusal appears only as prose on stderr, that is the defect: an orchestrator cannot retry with the missing permission.

## F. Failure paths

| # | Do | Expect |
|---|---|---|
| F1 | Prompt an agent whose adapter is not installed | An actionable error naming the install command |
| F2 | `--model` the vendor rejects | The vendor's refusal, not a generic failure |
| F3 | Kill the adapter mid-turn, by PID from `acpc daemon status` | The loss reported with the adapter's final stderr |
| F4 | `ACPC_STATE_DIR` containing `..`, then prompt | Daemon reachable; no 15 s stall then `running direct` |
| F5 | Prompt with a corrupt session id | Clear refusal, local state intact |
| F6 | Two concurrent prompts on one target | Both complete; the second may report queueing |

F4 is here because it broke: client and daemon derived different socket paths from two spellings of one directory, so a healthy daemon sat on a socket nobody dialled.

## G. Teardown

Stop each daemon you started, confirm no survivors by the PIDs you recorded, and `trash-put` the temporary directories.

A daemon that ignores `SIGTERM` is worth reporting with its age and how it was started. A healthy one exits in about two seconds; one whose virtualenv was deleted underneath it is known to need `SIGKILL`.

## Reporting

One section per group, per step: what you ran, what happened, pass or fail.

- **Quote the output.** "Worked as expected" is not a result.
- **State the version and platform.**
- **Separate a product failure from an environment one.** A sandbox refusing Unix socket binds looks exactly like a broken daemon path. Say which, and how you know.
- **Say what you skipped and why.**
- End with concrete friction you hit. You are the only one seeing the tool from outside.
