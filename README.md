# acpc

Thin Python CLI client for the Agent Client Protocol (ACP).

<!-- Badges: PyPI version, Python 3.12+, License MIT -->

> **Status: alpha (v0.1).** No persistent daemon yet, so every call pays a 3-10s cold start while the adapter boots. Fine for scripts and CI, painful for interactive loops. v0.2 will add a daemon. See [Known limitations](#known-limitations).

## What is acpc?

acpc is a headless Python CLI for ACP, built on top of the official [agent-client-protocol](https://pypi.org/project/agent-client-protocol/) SDK.
Output is plain text with no ANSI escapes, so it composes cleanly with pipes, files, and other agents.

## Install

```bash
pip install acpc
# or
uv tool install acpc
```

Requires Python 3.12+.

## Quick start

```bash
# Send a prompt to Codex
acpc prompt codex "fix the tests"

# Use a model preset
acpc prompt claude "analyze this repo" --model fast

# Pipe a prompt from stdin
echo "explain the architecture" | acpc prompt codex -

# Read prompt from file, write output to file
acpc prompt codex --input-file task.md -o result.md
```

## Commands

| Command | Description |
|---------|-------------|
| `prompt <agent> [text]` | Send a prompt to an ACP agent |
| `run <agent> [text]` | Alias for `prompt` |
| `models [agent]` | Show available models and presets |
| `agents` | List registered agents and install status |
| `sessions <agent>` | List agent sessions (via ACP) |
| `install <agent>` | Run the agent's install command |
| `stop <agent>` | Stop running sessions for an agent |
| `stop -s <id>` | Stop a specific session by ID |
| `status` | Show all running sessions |
| `generate-completion` | Generate or install shell completions |

## Options reference

All options for the `prompt` command:

| Flag | Description | ACP mapping |
|------|-------------|-------------|
| `--last` | Resume the last session | `session/load` |
| `-s, --session ID` | Resume session by ID | `session/load` |
| `--model MODEL` | Model ID or preset (fast/standard/max) | `session/set_model` |
| `--mode MODE` | Set mode for the session | `session/set_mode` |
| `--permissions LEVEL` | Permission policy (see below) | `request_permission` |
| `--cwd DIR` | Working directory for the agent | `session/new` (cwd) |
| `--json` | NDJSON output (ACP events) | local |
| `--quiet` | Final text only (no streaming) | local |
| `-o, --output FILE` | Write output to file | local |
| `--input-file FILE` | Read prompt from file | local |
| `--timeout SECS` | Timeout in seconds | local |
| `--dry-run` | Resolve config and exit without running | local |

## Model presets

Instead of memorizing vendor-specific model IDs, use one of three tier names:

```bash
acpc prompt claude --model fast       # cheapest, fastest
acpc prompt claude --model standard   # default
acpc prompt codex --model max         # most capable
```

Run `acpc models` to see what each preset resolves to for a given agent.

Presets live in `~/.agents/config.toml`. If the file doesn't exist, built-in defaults are used:

```toml
[models.claude]
fast = "haiku"
standard = "sonnet"
max = "opus"

[models.codex]
fast = "..."
standard = "..."
max = "..."
```

Use `--dry-run` to verify what model will be used without running:

```bash
acpc prompt codex --model standard "task" --dry-run
```

### Discovering models

```bash
acpc models           # show presets and available models for all agents
acpc models claude    # show for a specific agent
```

Available models are cached from ACP responses (TTL 7 days).
Each `acpc prompt` call refreshes the cache as a side effect, so it stays fresh without an extra round-trip.
If the cache is stale, `acpc models <agent>` fetches live from the adapter.

## Permissions

The `--permissions` flag controls how acpc responds to ACP permission requests.
Tool calls are classified by their `kind` field into three categories.


| Level | read-like | edit/execute | delete/move |
|-------|-----------|--------------|-------------|
| `all` | allow | allow | allow |
| `write` | allow | allow | deny |
| `read` | allow | deny | deny |
| `none` | deny | deny | deny |
| `prompt` | allow | ask user | ask user |

Read-like tool kinds: `read`, `search`, `think`, `fetch`, `switch_mode`, `other`.

Default: `prompt` when stdin is a TTY, `read` when piped (non-TTY).

In non-interactive contexts, denied operations cause exit code 3.

## Output modes

stdout carries agent output only. stderr carries `[acpc]`-prefixed diagnostics.

**text** (default): Agent text streams to stdout as it arrives.
Tool calls appear on stderr as `[acpc] tool: edit:src/main.py`.

```bash
acpc prompt codex "fix the bug" > fix.md
```

**json**: Every ACP `session_update` event passes through as NDJSON.
The stream is wrapped by `session_started`, `session_ended`, and `session_error` meta-events.

```bash
acpc prompt codex "fix the bug" --json | jq 'select(.sessionUpdate == "agent_message_chunk")'
```

**quiet**: Collects all text, emits only the final result when the session ends.
The `-o` flag writes to a file (not written on crash).


```bash
acpc prompt codex "summarize" --quiet -o summary.md
```

## Multi-turn sessions

Every completed session saves its ID per agent. Use `--last` to resume:

```bash
acpc prompt codex "remember: the password is hunter2"
acpc prompt codex --last "what was the password?"
```

Resume a specific session by ID:

```bash
acpc prompt codex -s 019cf2ca-b50f-7a13-ad67-14fe4db0e0ac "follow up"
```

Session IDs and resume commands appear on stderr:

```
[acpc] session: 019cf2ca-b50f-7a13-ad67-14fe4db0e0ac
[acpc] resume: acpc prompt codex -s 019cf2ca-b50f-7a13-ad67-14fe4db0e0ac "follow up"
```

Last-session tracking is scoped per PPID to avoid race conditions in concurrent use.

## Agent registry

Agents are defined in TOML files. Three agents ship built-in (codex, claude, gemini).
Run `acpc agents` to see the current list and install status.

### TOML format

```toml
identity = "my-agent"
name = "My Agent"
author = "Me"
run_command = "my-agent-acp"
install_command = "npm install -g my-agent-acp"
```

### User overrides

Place `.toml` files in the platform config directory to add or override agents:

- Linux: `~/.config/acpc/agents/`
- macOS: `~/Library/Application Support/acpc/agents/`
- Windows: `%APPDATA%\acpc\agents\`

User overrides take priority over built-in agents with the same identity.

### Installing adapters

```bash
acpc install codex    # runs the install_command from the TOML file
acpc agents           # shows install status for all agents
```

## Process management

Running sessions are tracked in the platform state directory (`~/.local/state/acpc/` on Linux).
Each entry stores PID, agent identity, working directory, and start time.

```bash
acpc status                              # list running sessions
acpc stop codex                          # stop all sessions for codex
acpc stop -s 019cf2ca-b50f-7a13-ad67     # stop a specific session
```

Signal handling: Ctrl+C sends `session/cancel` via ACP, waits 2s for the agent to exit cleanly, then sends SIGKILL to the entire process group.
Child processes are spawned in their own process group to prevent orphans.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (`end_turn`) |
| 1 | Agent error (`max_tokens`, `max_turn_requests`, ACP error) |
| 2 | Usage error (unknown agent, missing prompt) |
| 3 | Permission denied (non-interactive) |
| 124 | Timeout (`--timeout` exceeded) |
| 130 | SIGINT (Ctrl+C) |
| 141 | SIGPIPE |
| 143 | SIGTERM |

## Trust model

TOML agent files define `run_command` and `install_command`, which acpc executes as shell commands.
This is the same trust level as shell aliases or PATH entries.
Only place TOML files you trust in the config directory.

The spawned adapter process inherits your full environment (env vars, PATH, credentials).
Environment filtering (`--env`) is planned for v0.2.
As a workaround, use `env -i` to strip the environment:

```bash
env -i HOME="$HOME" PATH="$PATH" acpc prompt codex "task"
```

## CI usage

In non-TTY environments (CI, scripts, pipes), permissions default to `read`.
Use `--permissions all` to allow writes, or `--permissions none` for dry runs.

```bash
# CI: run agent, capture JSON output, check exit code
acpc prompt codex "run the test suite and fix failures" \
    --permissions all \
    --json \
    --timeout 300 \
    > agent-output.ndjson

if [ $? -eq 0 ]; then
    echo "Agent completed successfully"
elif [ $? -eq 124 ]; then
    echo "Agent timed out"
else
    echo "Agent failed with exit code $?"
fi
```

```bash
# Parse agent text from NDJSON
acpc prompt codex "summarize changes" --json \
    | jq -r 'select(.sessionUpdate == "agent_message_chunk") | .content.text' \
    | tr -d '\n'
```

## Known limitations

v0.1 covers the basics. A few rough edges to be aware of:

- **Cold start per call (~3-10s).** Every `acpc prompt` spawns a fresh adapter process. No persistent daemon yet, so interactive multi-turn loops feel slow. Scripts and CI are fine. The daemon lands in v0.2.
- **Unix-only.** Linux and macOS work. Windows is planned for v0.3 (needs named pipes instead of Unix sockets, and `taskkill` instead of `killpg`).
- **`--last` is not orchestration-safe.** Last-session tracking is scoped per parent PID. That works for a human in a terminal, but breaks when one orchestrator agent spawns several `acpc` calls. For programmatic use, grab the session ID from stderr and pass it back with `-s SESSION_ID`. acpc warns when `--last` is used.
- **Gemini needs `GEMINI_API_KEY` in env.** OAuth reuse in ACP subprocess mode is broken upstream (gemini-cli [#7549](https://github.com/google-gemini/gemini-cli/issues/7549), [#12042](https://github.com/google-gemini/gemini-cli/issues/12042)). API key is the only reliable auth path right now.
- **No env filtering.** The spawned adapter inherits your full environment (see [Trust model](#trust-model)). An `--env` flag is planned for v0.2.

## License

MIT
