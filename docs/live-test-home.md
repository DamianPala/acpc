# Live-test authentication home

`tests/test_live.py` calls real ACP agents and is skipped by default. Its isolated authentication home prevents those calls from loading normal user configuration, skills, and agent state.

## Layout used by the harness

The harness resolves the test-home root from `AGENT_TEST_HOME`. If that variable is unset, it uses `~/.agent-test-home`.

When the root exists, the harness sets `HOME` to it. It then sets agent-specific configuration roots only if the corresponding directory exists:

| Agent | Required directory | Environment set by the harness |
|-------|--------------------|--------------------------------|
| Codex | `$AGENT_TEST_HOME/.codex` | `CODEX_HOME=$AGENT_TEST_HOME/.codex` |
| Claude | `$AGENT_TEST_HOME/.claude` | `CLAUDE_CONFIG_DIR=$AGENT_TEST_HOME/.claude` |

Create the root and both agent directories before running the suite. Otherwise the harness leaves `HOME` and the per-agent variables alone, which defeats the intended isolation.

```bash
test_home="${AGENT_TEST_HOME:-$HOME/.agent-test-home}"
mkdir -p "$test_home/.codex" "$test_home/.claude"
```

Keep this directory limited to the authentication material required by the adapters. One known exception is upstream: `claude-agent-acp` can still read the real `~/.claude/CLAUDE.md` despite the `HOME` override.

## Initial login and re-authentication

Run the login command for the agent whose credentials need replacing. These commands deliberately set the same environment that the test harness later sets.

```bash
test_home="${AGENT_TEST_HOME:-$HOME/.agent-test-home}"

HOME="$test_home" CODEX_HOME="$test_home/.codex" codex login
HOME="$test_home" CLAUDE_CONFIG_DIR="$test_home/.claude" claude auth login
```

The first command authenticates Codex. The second authenticates Claude using its default Claude subscription flow; use Claude's documented `auth login` options only when a different account flow is intended.

Re-authenticate in the same way when a token expires. Do not treat `codex login status` saying `Logged in using ChatGPT` as a successful credential check. It can report that state for a refresh token that has already been spent. Only a real agent call proves that the credentials currently work.

The live suite is that real check and spends subscription or API capacity:

```bash
uv run pytest tests/test_live.py -v -m live
```

Do not run it casually. It is intentionally skipped in normal test runs and each live test uses its own temporary `ACPC_STATE_DIR`.
