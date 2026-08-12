---
name: provider-bringup
description: >-
  Existing shipped harness + new provider/model for acpc (variant with extends:
  OpenRouter, gateway, local endpoint). Also for a new model on plumbing that
  already works, or when a variant fails with "There's an issue with the
  selected model". Not for a new base command entry — that is adapter-bringup.
---

# Bringing up a provider

A harness (Claude Code, codex, gemini) talks to a provider over the provider's
API. acpc only builds the environment the harness runs in. Every failure is in
exactly one of those two places, and the whole method is proving which one
before changing anything.

New base adapter (`command`, no `extends`)? Stop — use `adapter-bringup`.

Vendor errors lie about which one it is. Climb the ladder instead of reading
them, except for the recognized strings in *Known lies*, which name their own
rung.

## Already done

Check here first, then branch. This table is keyed by harness and provider,
never by model: a hit means the plumbing is solved, not that your model works.

| Combination | Entry | Provider switch |
|---|---|---|
| Claude Code + OpenRouter (2026-08-07) | `~/.acpc/agents/builder-deepseek.toml` | `~/.claude-openrouter/settings.json` |
| codex + OpenRouter | `~/.acpc/agents/general.toml` | `~/.codex-openrouter/config.toml` |

**On a hit**, read that entry first: a provider variant records its traps as
comments, so the file is the record and this table is only the index. Then copy
it and change every field that names a model or sizes its context, leaving the
provider switch (`home`, base URL, key mechanism) untouched. Run **rungs 1, 2
and 5**, nothing else: the plumbing is proven, the model is not.

**On a miss**, climb all seven rungs. `references/openrouter.md`, in this skill's
directory, carries both pairs anonymized plus what differs between the two
harnesses; `acpc skills <name>` prints that directory on stderr.

Keep the convention when you add a combination: the next agent should find
comments in your entry, not a longer version of this table.

## Ladder

Each rung is a separate process and proves one thing. Set the two values every
rung uses, and note the baseline rung 6 compares against:

```bash
MODEL=deepseek/deepseek-v4-pro
KEY=$OPENROUTER_API_KEY
stat -c '%y' ~/.claude/settings.json   # the real home of whatever harness
```

### 1. The model exists and can call tools

```bash
curl -s https://openrouter.ai/api/v1/models \
  | jq -r --arg m "$MODEL" '.data[] | select(.id==$m)
           | {id, context_length, supported_parameters}'
```

No `tools` in `supported_parameters` means the harness is dead weight: stop.
Keep `context_length`, the entry needs it.

### 2. The endpoint speaks the harness's protocol

Claude Code speaks the Anthropic Messages API, codex speaks OpenAI Responses.
Hit the provider's compatible endpoint raw, with a tool and `stream: true`:

```bash
jq -n --arg m "$MODEL" '{model:$m, max_tokens:256, stream:true,
   tools:[{name:"t", description:"d",
           input_schema:{type:"object", properties:{}}}],
   messages:[{role:"user", content:"call t"}]}' \
| curl -sN -X POST https://openrouter.ai/api/v1/messages \
    -H "Authorization: Bearer $KEY" -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" --data-binary @- | rg tool_use
```

You need a `tool_use` block and `"stop_reason":"tool_use"`. A chat reply proves
nothing; a harness that cannot get tool calls is useless.

### 3. The harness reaches it, without acpc

The rung that pays for the whole ladder. Run the harness bare, env on the
command line, so a failure here is the provider's and a failure only under acpc
is acpc's.

```bash
env -i HOME="$HOME" PATH="$PATH" USER="$USER" \
  CLAUDE_CONFIG_DIR="$HOME/.claude-throwaway" \
  ANTHROPIC_BASE_URL="https://openrouter.ai/api" \
  ANTHROPIC_AUTH_TOKEN="$KEY" ANTHROPIC_MODEL="$MODEL" \
  claude -p "Reply with exactly: PONG"
```

Pass is the exact string and nothing else. Always a throwaway vendor home: the
real one holds subscription credentials that quietly win over the env, plus
settings that then follow the callee everywhere.

### 4. When the harness lies, read the wire

```bash
nc -l 127.0.0.1 8899
```

Re-run rung 3 with the base URL pointed at `http://127.0.0.1:8899`. nc prints
the request line and headers and the harness then errors out, which is enough:
the **path** is the diagnosis. Harnesses append their own suffix to the base
URL, and a doubled segment comes back as a 404 dressed up as a model error.

Only if you need to watch a whole exchange rather than one request is a
forwarding proxy worth writing.

### 5. Write the entry, dry-run before you run it

Entry format and its traps are in *The entry* below. This skill assumes a
variant (`extends`); a base adapter with `command` and no `extends` is
`adapter-bringup` (empty `[modes]`, discovery, first mode table).

```bash
acpc agents <name>              # what the entry resolves to, with provenance
acpc run <name> "x" --dry-run   # what this call resolves to, incl. env
acpc run <name> "Reply with exactly: OK" --timeout 180
```

A variant inherits its parent's `[modes]` and the above just works once the
provider env is right.

### 6. Prove the isolation

```bash
acpc daemon status <name>                # the entry got its own target
stat -c '%y' ~/.claude/settings.json     # unchanged since the baseline
```

Never `daemon stop` to fix something here: other sessions may be running.

### 7. Check the advertised modes against the entry

```bash
acpc probe <name> --discover
```

Zero turns, zero cost: it opens a session, reads the mode catalogue the adapter
advertises and releases it. The report is a two-sided diff against the entry's
resolved `[modes]` — modes the adapter advertises that the entry does not list,
and entry modes the adapter no longer advertises. It reports and never edits;
applying anything it shows is your own explicit change to the entry.

What it catches on a bringup: a wrong `extends` (the entry inherited another
adapter's mode table), a typo'd mode name, and a harness build that renamed or
dropped a mode since the parent's table was written. What it cannot tell you is
what a mode *permits* — `grants` and `delegates` are measurements, and the
measuring probe is not in this release. A clean diff means the names line up,
not that the ceilings are right; a mode you add from this report still needs its
facts filled in by hand, stated as what they are: copied or assumed, not
measured.

If discovery is **empty by design** (parent is Path B in `adapter-bringup`: no
ACP modes on the wire, table filled from docs), "entry modes absent from
catalogue" is expected — do **not** strip `[modes]` to clear the diff.

Modes are adapter-level, so on the rungs-1-2-5 path (new model, proven
plumbing) this rung moves nothing. Run it when the harness or its version is
new — and it is worth re-running after a harness upgrade for the same reason
the command exists at all: a `[modes]` table records an observation, and
observations age. Building the first `[modes]` table for a new base adapter is
`adapter-bringup`, not this rung.

## The entry

```toml
extends = "claude"
description = "..."
permissions = "execute"
model = "deepseek/deepseek-v4-flash"
effort = "high"
home = "~/.claude-openrouter"
env_passthrough = ["OPENROUTER_API_KEY"]

[env]
ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
ANTHROPIC_DEFAULT_HAIKU_MODEL = "deepseek/deepseek-v4-flash"
CLAUDE_CODE_MAX_CONTEXT_TOKENS = "1000000"
```

The background model needs its own line or it stays a `claude-*` id the endpoint
will not serve, and the context budget comes from rung 1's `context_length`.

Five traps, all silent:

- **Pin `model`.** Unset, it inherits the adapter's `standard` preset, a
  `claude-*` id no third party serves. acpc sends it as an ACP session config
  option, which is enough on its own: the vendor's own model variable
  (`ANTHROPIC_MODEL`) is then only needed for running the harness bare at rung
  3. Verified 2026-08-07.
- **Pin `effort`** for the same reason: unpinned it moves when the preset moves.
- **`[env]` merges with the parent, `env_passthrough` replaces it.** Your
  passthrough list must be complete on its own.
- **The key never goes in `[env]`**, that is the entry on disk. Pass it through
  instead. Passthrough keeps a variable's own name and acpc cannot rename one,
  so when the harness expects a different name, bridge it in the harness's own
  config (a key-reading helper, a provider field), never in the entry.
- **Own `home` means own daemon target**, which is what keeps a new provider
  from disturbing running sessions.

## Known lies

| Symptom | Actually means |
|---|---|
| `There's an issue with the selected model (X). It may not exist or you may not have access to it.` (Claude Code) | Usually the base URL path, not the model. Skip to rung 4. |
| A warning that the model is unrecognized and the session will be kept within 200k | Cosmetic until it compacts. Set `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. |
| `cost $x.xx` in acpc's summary | The harness priced the turn at its own vendor's rates. Off by ~50x on cheap third-party models. Read tokens; get the real figure from the provider. |
