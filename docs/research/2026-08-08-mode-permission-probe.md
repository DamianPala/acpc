# Mode and permission probe, 2026-08-08

Measured against `claude-agent-acp` (claude CLI 2.1.224) and `codex-acp` (codex-cli
0.147.0) on acpc 0.4.1. This is the evidence behind the `[modes]` tables and the 0.5
permission redesign; several entries contradict comments the adapter TOMLs shipped with.

## Method

One fixed task per run: read a file, append a line to a file with the edit tool, run
`printf 'SHELLOK\n' > probe-shell.txt`, delete a file. Each run in a fresh workspace under
`--permissions all`, so acpc denies nothing and the mode is the only variable. Two
observations per run: what changed on disk, and which `request_permission` kinds reached
acpc.

## Mode truth table

| Adapter | Mode | edit | shell | delete | requests reaching acpc |
|---------|------|------|-------|--------|------------------------|
| claude | `default` | yes | yes | yes | `edit`, `execute`×2 |
| claude | `plan` | yes | yes | yes | `switch_mode`, then as `default` |
| claude | `acceptEdits` | yes | yes | yes | `execute` only (the delete) |
| claude | `dontAsk` | no | no | no | none |
| claude | `auto` | yes | yes | yes | `edit`, `execute`×2 |
| claude | `bypassPermissions` | yes | yes | yes | none |
| codex | `read-only` | **yes** | no | no | none |
| codex | `agent` | yes | yes | yes | none |
| codex | `agent-full-access` | yes | yes | yes | none |

## Findings

**`auto` is not a bypass mode.** It emits permission requests exactly like `default`.
`claude.toml` shipped it in `bypass_modes` on the assumption that a vendor classifier
answers in acpc's place, which the probe contradicts. Consequence in 0.4.1: `--mode auto`
demands `--permissions all` for no reason.

**`acceptEdits` auto-allows shell writes, not only edits.** `printf > file` ran unasked;
only the delete produced a request. It is not an "edits, no shell" tier.

**The `delete` kind is never emitted.** Deleting a file arrived as `execute` in every run —
the callee shells out (`rm`, `trash-put`). Neither adapter's kind map contains `delete` or
`move`, so a policy that admits `execute` cannot withhold deletion.

**`fs/write_text_file` bypasses the policy entirely.** codex routes edits through the ACP
client callback, which `client.py` executes unconditionally. Verified separately:
`--mode read-only --permissions none` still wrote `EDITED` to disk. ACP does not gate
`fs/*` or `terminal/*` behind `request_permission` at all.

**A delegating mode does not ask about everything.** Under `--mode default --permissions
read`, `test -f X && echo` executed with no request. `~/.claude/settings.json` carries an
empty allow list, so this is Claude Code's own read-only command classification. `edit`
therefore bounds what a callee may change, not whether a shell ran.

**The vendor config wins when acpc sends no mode.** `runner.py:270` only sends
`session/set_mode` when a mode was explicitly resolved. With `defaultMode: "dontAsk"` in
the vendor home, `acpc run claude --permissions all` produced a callee that could do
nothing: every step blocked, zero requests, exit 0.

**Subagents need no special case.** A subagent spawned by claude's Task tool surfaces its
tool calls as ordinary `request_permission` in the same session — allowed under `all`,
denied twice under `read` with the file left untouched.

**Network sits in `read` on both adapters.** `--permissions read` fetched `example.com`
successfully: on claude as a `fetch` request acpc allowed, on codex with no request at all.

**Denials are only reported when the policy was defaulted.** With no `--permissions`, the
summary reads `denied: 2 write (default read policy — pass --permissions write)`. With
`--permissions read` passed explicitly, the same two denials produce no summary line at
all. The `--json` envelope carries no denial data in either case, and the exit code is 0.

**`other` was not observed.** Across all 48 sessions on this machine, 266 permission
requests: `execute` 250, `edit` 14, `switch_mode` 1, `fetch` 1. No `other`, `delete`,
`move`, `read`, `search` or `think`. Reviewer feedback had claimed `other` was common; on
this evidence it is not, so 0.5 leaves it classified as unknown rather than widening the
`execute` rung to admit it.

**codex advertises no `auto` mode over ACP.** Fresh probe after clearing the cache: three
modes only — `read-only`, `agent`, `agent-full-access`. The codex TUI names its middle
tier differently; the ACP name is `agent`.

## Cost of a warm target

Measured with two concurrent sessions on one target: one shared `claude-agent-acp` wrapper
(110 MB) and one shared acpc daemon (46 MB), plus roughly 300 MB of adapter engine **per
session**. The per-session cost is paid however targets are keyed; keying an extra target
adds only the ~156 MB fixed part, and idle targets expire.
