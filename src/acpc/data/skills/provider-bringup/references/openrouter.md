# OpenRouter, both harnesses

A provider switch is always a pair: the acpc entry (what acpc sends, and the
environment it builds) and the harness's own config inside the vendor home (how
the harness reaches the provider and finds its key). Neither half works alone,
and the second half is the one no `--help` will teach you.

No key appears in either half. Both harnesses read it from a variable acpc
passes through by name.

## Claude Code

`~/.acpc/agents/<name>.toml`

```toml
extends = "claude"
model = "deepseek/deepseek-v4-flash"   # ACP session option; enough on its own
effort = "high"
permissions = "write"
home = "~/.claude-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]

[env]
ANTHROPIC_BASE_URL = "https://openrouter.ai/api"   # no /v1, the harness adds it
ANTHROPIC_DEFAULT_HAIKU_MODEL = "deepseek/deepseek-v4-flash"
CLAUDE_CODE_MAX_CONTEXT_TOKENS = "1000000"
```

`~/.claude-openrouter/settings.json`

```json
{ "apiKeyHelper": "printenv OPENROUTER_API_KEY" }
```

## codex

`~/.acpc/agents/<name>.toml`

```toml
extends = "codex"
model = "gpt-5.6-luna"
effort = "xhigh"
permissions = "write"
home = "~/.codex-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]
```

`~/.codex-openrouter/config.toml`

```toml
model = "gpt-5.6-luna"
model_provider = "openrouter"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"
```

## Where they differ

Same provider, same key, three different answers. Assume nothing carries over
to a third harness.

| | Claude Code | codex |
|---|---|---|
| base URL | `.../api`, the harness appends `/v1/messages` | `.../api/v1`, used verbatim |
| key | `apiKeyHelper` in `settings.json` | `env_key` in the provider block |
| provider selection | environment variables only | a named provider block plus `model_provider` |
| model | the acpc entry decides | the entry decides, the home's `model` is the fallback |
