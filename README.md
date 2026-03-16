# acpc

Thin Python CLI client for the Agent Client Protocol (ACP).

<!-- Badges: PyPI version, Python 3.12+, License MIT -->

## What is acpc?

acpc is the first headless Python CLI for ACP. It wraps the official
[agent-client-protocol](https://pypi.org/project/agent-client-protocol/) SDK
into a unix-friendly command-line interface. AI agents are the primary users,
so output is clean plaintext with no ANSI escape codes.

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

# Use a specific model
acpc prompt claude "analyze this repo" --model sonnet

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
| `--model MODEL` | Set model for the session | `session/set_model` |
| `--mode MODE` | Set mode for the session | `session/set_mode` |
| `--permissions LEVEL` | Permission policy (see below) | `request_permission` |
| `--cwd DIR` | Working directory for the agent | `session/new` (cwd) |
| `--json` | NDJSON output (ACP events) | local |
| `--quiet` | Final text only (no streaming) | local |
| `-o, --output FILE` | Write output to file | local |
| `--input-file FILE` | Read prompt from file | local |
| `--timeout SECS` | Timeout in seconds | local |

## Permissions

The `--permissions` flag controls how acpc responds to ACP permission requests.
Tool calls are classified by their `kind` field into three categories.

| Level | read/search/think/fetch | edit/execute | delete/move |
|-------|-------------------------|--------------|-------------|
| `all` | allow | allow | allow |
| `write` | allow | allow | deny |
| `read` | allow | deny | deny |
| `none` | deny | deny | deny |
| `prompt` | allow | ask user | ask user |

Default: `prompt` when stdin is a TTY, `read` when piped (non-TTY).

In non-interactive contexts, denied operations cause exit code 3.

## Output modes

stdout carries agent output only. stderr carries `[acpc]`-prefixed diagnostics.

**text** (default): Agent text streams to stdout as it arrives. Tool calls
appear on stderr as `[acpc] tool: edit:src/main.py`.

```bash
acpc prompt codex "fix the bug" > fix.md
```

**json**: Every ACP `session_update` event passes through as NDJSON. Three
meta-events wrap the stream: `session_started`, `session_ended`, `session_error`.

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

Agents are defined in TOML files. Three agents ship built-in:

| Identity | Name | Author | run_command |
|----------|------|--------|-------------|
| `codex` | Codex CLI | OpenAI | `npx @zed-industries/codex-acp` |
| `claude` | Claude Code | Anthropic | `npx @zed-industries/claude-agent-acp` |
| `gemini` | Gemini CLI | Google | `gemini --experimental-acp` |

### TOML format

```toml
identity = "codex"
name = "Codex CLI"
author = "OpenAI"
run_command = "npx @zed-industries/codex-acp"
install_command = "npm install -g @zed-industries/codex-acp"
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

Running sessions are tracked in the platform state directory
(`~/.local/state/acpc/` on Linux). Each entry stores PID, agent identity,
working directory, and start time.

```bash
acpc status                              # list running sessions
acpc stop codex                          # stop all sessions for codex
acpc stop -s 019cf2ca-b50f-7a13-ad67     # stop a specific session
```

Signal handling: Ctrl+C sends `session/cancel` via ACP, waits 5s for graceful
shutdown, then SIGTERM to the process group, waits 2s, then SIGKILL. Child
processes are spawned in their own process group to prevent orphans.

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

TOML agent files define `run_command` and `install_command`, which acpc
executes as shell commands. This is the same trust level as shell aliases or
PATH entries. Only place TOML files you trust in the config directory.

The spawned adapter process inherits your full environment (env vars, PATH,
credentials). Environment filtering (`--env`) is planned for v0.2. As a
workaround, use `env -i` to strip the environment:

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

## License

MIT
