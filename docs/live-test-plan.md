# Live test plan — acpc 0.3

Checks against real adapters. The suite runs against `tests/mock_agent.py`, so it cannot see what a vendor actually advertises, how long a cold start takes, whether `session/load` really restores context, or whether a daemon survives real use. This plan covers that gap; run it after changes to the client, runner, daemon or adapter TOMLs, or when a vendor ships something new.

Adapted from the 0.2-era `docs/manual-test-plan.md` (donor repo); every step re-derived against SPEC.md. The measured 0.2 landmines are kept where they still apply — they were bought with real incidents. Last full run: 2026-08-06 (Stage 3), which found and fixed four defects; its findings are folded in below.

Run `uv run pytest && ./smoke.sh` first. If either is red, stop.

## Two tiers

- **Tier 1 (cheap, the bulk):** groups B, C, D, E, F on OpenRouter-backed entries (`codex-acp` + a fast model, Luna-class). Same adapter code path as subscription codex, negligible cost, so repeat runs are fine.
- **Tier 2 (vendor, short):** the checks that only real vendor surfaces can answer — group V, plus E4 (TTY prompt, human in the loop). One trivial prompt per check. **Codex only** (operator decision 2026-08-05): claude is not live-tested; its shipped TOML keeps the `TODO(stage3)` markers as an accepted risk until first real use, and the environment facts recorded below for it are informational. Codex's TOML facts were verified live 2026-08-06. (The gemini adapter was retired 2026-08-07 — the Gemini CLI no longer exists.)

## Rails

- **Run the build under test, not the installed tool.** The system `acpc` may be an older install that other agents are actively dispatching through. Always `uv run --project <this repo> acpc …`; record `acpc -V` first — no result below is portable across builds. A code change under an unchanged version number does not restart already-running daemons (version-skew restart keys on the version string); `daemon stop` between builds.
- **Isolate the state root.** One env var in 0.3:

  ```bash
  export ACPC_HOME=$(mktemp -d /tmp/acpc-live-XXXX)
  ```

  Never run against `~/.acpc` — you would stop the operator's live daemons and lose their sessions.
- **Never kill by pattern.** `pkill -f acpc` / `pgrep -f "acpc.daemon"` are forbidden: the `.` is a wildcard and any process merely mentioning the path matches; a dispatch died this way on 2026-08-04. Use `acpc daemon stop <target>`, or signal a PID read from `acpc daemon status`.
- **Secrets stay in env.** Tier 1 entries name the OpenRouter key via `env_passthrough`; source it (`set -a && . ~/.config/secrets/base.env && set +a`) before running. Values must never appear in a TOML, a prompt, or this report.
- **Do not run `acpc install`** unless a vendor adapter is genuinely missing and its `install_command` has been shown to the operator first — it modifies global tooling. F1 only checks the *error text*.
- **Keep prompts trivial** (`Reply with exactly: OK`). These are live calls.
- Clean up with `trash-put`, never `rm -rf`.

### Tier 1 setup

Write the test entries by hand into the isolated home (this also exercises the hand-editable contract):

```bash
mkdir -p "$ACPC_HOME/agents"
cat > "$ACPC_HOME/agents/lt.toml" <<'EOF'
extends = "codex"
description = "Live-test worker on the cheap path."
model = "gpt-5.6-luna"
effort = "low"
permissions = "read"
home = "~/.codex-openrouter"
env_passthrough = ["OPENROUTER_GENERAL_BUILDER_API_KEY"]

[env]
MODEL_PROVIDER = "openrouter"
EOF
```

The variable name matters: `~/.codex-openrouter/config.toml` names its key through `env_key = "OPENROUTER_GENERAL_BUILDER_API_KEY"` — `base.env` also holds a plain `OPENROUTER_API_KEY`, but that is not the one this home reads.

Plus a second entry `lt2.toml`, identical except `home = "~/.codex-openrouter-b"`, for B4/D — a distinct target by construction. Create that home at execution time as a *slim* copy (`config.toml`, `installation_id`, `version.json` — not the sqlite/session bulk):

```bash
mkdir -p ~/.codex-openrouter-b
cp ~/.codex-openrouter/{config.toml,installation_id,version.json} ~/.codex-openrouter-b/
```

`trash-put ~/.codex-openrouter-b` at teardown.

## Environment facts (verified 2026-08-05/06)

- Adapter binaries all on PATH: `codex-acp`, `claude-agent-acp` (plus vendor `codex`, `claude`) under `~/.local/bin`.
- Vendor homes exist: `~/.codex`, `~/.claude`, `~/.codex-openrouter`.
- **claude:** `~/.claude/.credentials.json` fresh — expected to just work.
- **codex (tier 2):** auth lives in the system **keyring** (`cli_auth_credentials_store = "keyring"` in `~/.codex/config.toml`), not `auth.json`. Measured 2026-08-06: vendor codex authenticated through acpc's constructed env **without** `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR` — the risk did not materialize; no passthrough additions needed. If a future run fails auth here, add `env_passthrough = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]` to a `codex.toml` override in the test home and record the outcome.
- A vendor login-status command (`codex login status` and friends) is **not** a credential check — it can report a logged-in state for a refresh token that has already been spent. Only a real call proves the credentials currently work.
- **codex mode semantics (measured 2026-08-06):** the default `agent` mode auto-allows workspace edits and commands without emitting `request_permission`, so `--permissions` below `all` only bites in `read-only` mode; `agent-full-access` never asks and is the declared bypass mode. A callee's shell commands re-source the operator's profile, so ambient secrets are visible to it even though acpc's adapter env is clean — the vendor sandbox is the boundary there.

## Observing a run

stdout is the answer, stderr is acpc metadata prefixed `--`; redirect them separately. `acpc status <id>` gives vitals; `acpc log <id> --tail N` the last events and is the first thing to read when a run looks stuck. After it ends the answer persists at `$ACPC_HOME/sessions/<id>/answer.md` and the finished `log` footer names it. `log --json` gives raw NDJSON events when a condensed line is ambiguous.

## A. Resolution (no live calls, run first)

| # | Do | Expect |
|---|---|---|
| A1 | `acpc agents` | Every adapter with install status; variants indented showing only their delta; `missing → acpc install <name>` for absent adapters |
| A2 | `acpc agents lt` | Field-by-field resolution with provenance (`(entry)`, `(adapter default)`, …); ends with a pointer to the parent's catalogs, no cache footer |
| A3 | `acpc run lt "x" --dry-run` | The call's full resolution incl. declared env and cwd, each value sourced; no adapter contacted, no session dir created |
| A4 | `acpc --help`, then `acpc log --help` | Cheat sheet ≤100 lines with grouped examples + flag→ACP table; per-command page is the full reference; `acpc stop --help` prints the root page (no stubs) |
| A5 | `acpc run lt "x" --effort ultra --dry-run` | Hard usage error listing the levels the resolved model supports — never a silent fallback |

Caveat for A2 on a *base adapter* name: on a fresh home a cache miss triggers the automatic live probe — that is a live call and belongs to tier 1/2, not here. `lt` (a variant of an uncached parent) should end with the pointer without probing; if it probes instead, record that as a finding.

## B. Cold start and reuse (tier 1)

| # | Do | Expect |
|---|---|---|
| B1 | Time `acpc run lt "Reply with exactly: OK"` from the clean home | A daemon starts; stderr summary (one `--` line: duration, tokens, exit, id, dir); record wall time |
| B2 | Repeat it | Faster; `acpc daemon status` shows the same PID; record the delta |
| B3 | `acpc daemon status` | One concrete target per entry used so far, each with PID, uptime, log path |
| B4 | `acpc run lt2 "Reply with exactly: OK"` | A **second** daemon; both alive in `daemon status` |
| B5 | `acpc daemon stop lt`, run `lt` again | Clean stop (~2s), fresh cold start; the `lt2` daemon untouched |

If B4 lands both calls on one daemon, stop and report it. That is credential crossing, not a performance quirk.

## C. Sessions, turns, detach (tier 1)

| # | Do | Expect |
|---|---|---|
| C1 | `acpc run lt "Remember the word: kalarepa. Reply: OK"` | Answer on stdout; summary names id + session dir; dir holds `meta.json`, `prompt.md`, `transcript.ndjson`, `answer.md` |
| C2 | `acpc continue <id> "What word did I ask you to remember?"` (warm) | Context present; prior transcript not re-appended; previous turn rotated to `prompt.1.md`/`answer.1.md` |
| C3 | `acpc daemon stop lt`, then `continue <id>` again | The cold path: `session/load` resume — context still present. This is the S09 warm-vs-cold contract measured live |
| C4 | `acpc continue last "x"` non-TTY | Rejected, exit 2 — `last` is TTY-only |
| C5 | `acpc run lt "Reply: OK" --bg`, then `acpc wait <id>` | `--bg` prints id + dir only, no summary; `wait` prints the answer, exit mirrors the result |
| C6 | Slow prompt `--bg`, then `acpc stop <id>` | Graceful cancel; state `cancelled`; `wait <id>` exits 130; partial transcript on disk |
| C7 | Slow prompt in foreground, SIGTERM the client (PID of the `acpc run` process, not the daemon) | Client exits 143 printing the id to stderr; session keeps running under the daemon; `wait <id>` collects the answer |
| C8 | Same, SIGINT instead | Session cancelled (state `cancelled`), exit 130 |

C7 is the single most load-bearing behavior for the primary consumer (a harness killing the tool call on its own timeout). If detach does not survive on a real adapter, that is a stop-everything finding.

## D. Environment and identity (tier 1)

| # | Do | Expect |
|---|---|---|
| D1 | Run `lt` with `OPENROUTER_API_KEY` set | Works; `daemon status` shows the target |
| D2 | Same entry, key **unset** | A **different** target (env digest differs) — the call fails auth-wise but must not ride the keyed daemon of D1 |
| D3 | Prompt the agent to print its environment (`--permissions write` for the shell call) | Base set + capability vars + declared env only; no unrelated caller variables; the key value nowhere in state files |

D2 shipped broken in 0.2 and was caught by review, not tests. Measure it.

## E. Permissions (tier 1 except E4)

| # | Do | Expect |
|---|---|---|
| E1 | `--permissions read` with a prompt that writes a file | Turn ends normally, exit 0, file **not** created — the documented silent read-only trap |
| E2 | `acpc log <id>` after E1 | A `permission` event per decision with the denial visible; never filtered from any view |
| E3 | `--permissions none` | Every request denied, turn still ends cleanly |
| E4 | `--permissions prompt` on a real TTY (tier 2, human present) | A human is actually asked on `/dev/tty`; same flag non-TTY → usage error exit 2, not a downgrade |
| E5 | `--mode <bypass mode>` without `--permissions all` | Rejected at parse time, exit 2, naming the rule |

## F. Failure paths (tier 1)

| # | Do | Expect |
|---|---|---|
| F1 | Run an agent whose adapter binary is absent from PATH | Exit 1, one actionable line naming `acpc install <agent>` |
| F2 | `--model definitely-not-a-model` | The vendor's refusal surfaced, not a generic failure |
| F3 | Kill the *adapter* mid-turn by PID from `acpc daemon status` | Loss reported; session `orphaned` or `failed`, never stale `running`; `answer.md` exists (placeholder if nothing was written) |
| F4 | `ACPC_HOME` containing `..`, then run | Daemon reachable — no stall-then-fallback (0.2: two spellings of one dir derived two socket paths) |
| F5 | `acpc status zzzz` / corrupt a copy of `meta.json` and read it | One line naming the file/id, non-zero exit, no traceback, other state intact |
| F6 | Two concurrent runs on one target | Both complete; queueing noted on stderr if slots exhausted |
| F7 | Make the daemon unable to start (e.g. unwritable `$ACPC_HOME/daemon/`) | Visible direct-child fallback in the stderr summary; SIGTERM then cancels instead of detaching |
| F8 | `acpc run lt "slow task" --timeout 5` | Session cancelled, state `timeout`, exit 124; `wait --timeout` on a running session exits 124 but leaves it running |

## V. Vendor facts (tier 2 — real codex only)

Verifies the shipped codex TOML against vendor reality, one trivial prompt per check. Last verified 2026-08-06: modes `read-only`/`agent`/`agent-full-access`, `bypass_modes = ["agent-full-access"]`, `efforts = ["low","medium","high","xhigh"]` (minimal and ultra rejected with `Invalid params`).

| # | Do | Expect / record |
|---|---|---|
| V1 | `acpc agents codex` after one real run | Advertised modes, models, commands as the vendor announces them; compare `bypass_modes` and `efforts` in the shipped TOML against reality; fix the TOML in the same change |
| V2 | `acpc agents codex --models` | Preset table resolves to model IDs the vendor actually accepts (`--model fast` must not 404) |
| V3 | `acpc continue` on a codex session after `daemon stop` | Does real codex support `loadSession` and restore context on the cold path; record either way |
| V4 | `acpc agents codex --check` | Launch+auth verdict, exit 0; a failure → exit 1. The unnamed `--check` live-probes every installed adapter — do not run it while claude is excluded from live testing |
| V5 | `--mode` values from `agents codex` | `session/set_mode` accepted and observably changes behavior (`read-only` makes codex emit permission requests) |

## G. Teardown

Stop each daemon you started (`acpc daemon stop <entry>`), confirm no survivors by the PIDs you recorded, and `trash-put` the temporary homes.

A daemon that ignores SIGTERM is worth reporting with its age and how it was started. A healthy one exits in about two seconds.

## Reporting

One section per group, per step: what you ran, what happened, pass or fail.

- **Quote the output.** "Worked as expected" is not a result.
- **State the version (`acpc -V`) and platform.**
- **Separate a product failure from an environment one.** A sandbox refusing Unix socket binds looks exactly like a broken daemon path. Say which, and how you know.
- **Say what you skipped and why.**
- End with concrete friction you hit. You are the only one seeing the tool from outside.
