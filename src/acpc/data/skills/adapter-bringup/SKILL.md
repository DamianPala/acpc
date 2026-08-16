---
name: adapter-bringup
description: >-
  Bring up a new base adapter for acpc: any ACP-speaking process registered with
  its own command (no extends). Use when adding a vendor agent that is not
  already shipped (or a local ACP binary), writing ~/.acpc/agents/<name>.toml
  with command/home/modes, or when run/dry-run refuse because the modes table is
  empty. Not for pointing an existing adapter at a new provider (provider-bringup)
  or retargeting presets when the vendor catalogue moved (refresh-adapter-models).
---

# Bringing up an adapter

An **adapter** is any process that speaks ACP on stdio. acpc spawns it, builds
its environment, and maps `--permissions` onto that process's session modes.
This skill is for a **new base entry**: `command` set, no `extends` — shipping
product, local binary, or wrapper, same ladder.

If the agent is already shipped as a base (claude, codex, …) and you only need
another provider or home, stop — use `provider-bringup`. Same adapter, vendor
catalogue moved? `refresh-adapter-models` (overlay only). This path does not
inherit `[modes]`.

Every failure is in one place: the binary is not ACP, auth/env/home is wrong, or
the entry's modes/presets/env lie about what this process accepts. Climb the
ladder. Do not invent mode names or config option ids from another entry's TOML.

## Already done

Shipped adapters: package `data/agents/`. Operator overlays: `~/.acpc/agents/`,
keyed by **filename**:

| Filename shape | Meaning |
|---|---|
| `<name>.toml` with `extends = "…"` | variant — wrong skill; use `provider-bringup` |
| `<name>.toml` with `command`, no `extends` | **this** skill: new base adapter |
| same name as a shipped adapter | field overrides; catalogue refresh is `refresh-adapter-models` |

Read a shipped TOML only as a **field catalogue** (`acpc agents claude`), never
as a modes or presets table to copy.

## Ladder

The method is a **ladder**: ordered steps. A **rung** is one numbered step — a
separate check that proves one layer before the next (English for a step on a
physical ladder; same word as in `provider-bringup`). Fail on a rung → fix that
layer; do not skip ahead.

Values every rung reuses (fill from the product you are wiring):

```bash
NAME=myagent                      # entry stem → acpc run $NAME
# Full argv acpc will spawn (shlex-split). Often multi-word, not bare TUI.
CMD="myagent acp"                 # or: vendor-cli agent stdio, path/to/bin, …
HOME_DIR=~/.myagent               # vendor home (credentials + config)
HOME_ENV=MYAGENT_HOME             # env var that points at that home, if any
# KEY_ENV=MYAGENT_API_KEY         # ambient secret name(s), if any — passthrough later
```

### 1. The binary is the ACP process

Interactive UI, headless one-shot, and ACP agent are often different flags or
packages. You need the process that:

- stays up on **stdio**,
- speaks JSON-RPC ACP (not a chat REPL),
- is what editors / SDKs / the product docs start for agent integration.

```bash
command -v ${CMD%% *}
$CMD --help 2>&1 | head -80
# Product docs: "agent mode", "ACP", "stdio", "JSON-RPC" — not the TUI alone.
```

Record a one-line installer as `install_command` only if it is as trusted as the
entry itself. Otherwise set `install_docs` to the vendor's install page so
missing-binary errors do not promise `acpc install`.

### 2. ACP works without acpc

Spawn the same argv with a clean env and the vendor home, open a session,
release it — zero turns. Use a tiny ACP client, the vendor's self-check, or
(after rung 3) `acpc probe` on a modes-less stub.

Failure here is not an acpc bug: fix command, auth, or home first.

Auth usually lives under the vendor home (tokens, credential files) and/or a
key in the ambient env. Name any required key in `env_passthrough` later; never
put the secret **value** in the entry.

### 3. Stub entry (command only)

`~/.acpc/agents/$NAME.toml` — filename is the entry name:

```toml
name = "Display name"
author = "Vendor"
description = "One line: what this adapter is for."
command = "myagent acp"                   # shlex-split; args allowed
# install_command = "…"                   # only a trusted one-liner
# install_docs = "https://vendor.example/install"
home = "~/.myagent"
home_env = "MYAGENT_HOME"                 # omit only if the vendor has no home var
env_passthrough = [
    "MYAGENT_HOME",
    # "MYAGENT_API_KEY",                  # names only — values from the caller
]

# No [modes] yet — intentional.
# No [presets] yet — only after set_config_option (or equivalent) is proven.
```

```bash
acpc agents "$NAME"
acpc run "$NAME" "x" --dry-run   # must refuse: empty [modes] — that is correct
acpc probe "$NAME" --discover    # works without [modes]; never edits the entry
```

### 4. Fill `[modes]` (assumed, not measured)

acpc cannot select a mode over an empty table: `run` and `--dry-run` refuse.
Mode **ids** come from this binary, not from another adapter's table.

**Path A — discovery non-empty.** Write every advertised id. Facts
(`grants` / `delegates` / `escalates`) are **assumed** until measured on disk:

```toml
[modes]
# Assumed from discovery labels / product docs — not measured ceilings.
default = { grants = "read", delegates = true }
```

**Path B — discovery empty (0 advertised).** Some agents omit ACP `modes` on
`session/new` but still accept `session/set_mode` (or only control permissions
via CLI / config / session `_meta`). Then:

1. Take candidate ids from **this product's** docs (permission / session modes),
   not from another adapter.
2. Verify each id with a raw `set_session_mode` (or first turn under
   `--permissions all --mode <id>`). Note: a JSON-RPC success is **not** proof
   the ceiling changed — only that the call was accepted.
3. Write the table with comments: source = docs, assumed, discovery empty.

Rules that do not move:

- `grants`: `none` | `read` | `edit` | `execute` | `all`.
- `delegates = true`: work above the ceiling can still hit acpc as
  `request_permission`; `false`: the vendor handles (or ignores) it alone.
- `escalates = true`: in-vendor auto-approver can raise the effective ceiling
  with nothing reaching acpc — so `grants` is a measurement, not a bound.
- A mode id missing from the table is selectable only with `--mode <id>` and
  only under `--permissions all`; otherwise selection never leaves the table.
- Bridge without a full table:
  `acpc run "$NAME" "…" --permissions all --mode <id>`
  then fill the table and drop the bridge.
- Hyphenated mode ids may be bare TOML keys (`read-only = { … }`) or quoted;
  quote if the id is not a bare key (spaces, etc.).
- Do not put free keys under `[modes]` — only mode → table values.

```bash
acpc probe "$NAME" --discover   # report only; empty catalogue stays empty
```

### 5. Model and effort — prove the wire before presets

Default path: `session/set_config_option` for model and effort
(`effort_config_id`, default `reasoning_effort`), after `session/set_mode`.

When that fails (Method not found / unknown option), use entry overrides:

| Field | Values | Effect |
|---|---|---|
| `model_via` | `config_option` (default), `set_model` | ACP `session/set_model` + `modelId` |
| `effort_via` | `config_option` (default), `cli` | inject flag into spawn argv |
| `effort_cli_flag` | e.g. `--reasoning-effort` | used when `effort_via = "cli"` |

**Before** `[presets]`:

1. One turn with no model/effort pin (adapter default).
2. Try `--model` / `--effort` or a preset; on wire failure, set `model_via` /
   `effort_via` after verifying the alternate path, or leave presets empty.
3. Effort / "thinking" labels are not permission modes — keep them out of
   `[modes]`. Per-model allowlists go in `[effort_by_model]`; omit the table
   (or leave it empty) to use the global scale. An empty row means that model
   has no effort setting. Quote dotted model ids (`"grok-4.5"`).
4. `effort_config_id` only on the config-option path when non-default.

```toml
# When set_config_option is missing but set_model + CLI effort work:
# model_via = "set_model"
# effort_via = "cli"
# effort_cli_flag = "--reasoning-effort"
# [effort_by_model]
# "vendor-model-a" = ["low", "medium", "high"]
# "vendor-model-b" = ["low", "medium", "high", "xhigh"]
# [presets]
# standard = { model = "…", effort = "high" }
```

### 6. Dry-run, then a trivial turn

```bash
acpc agents "$NAME"
acpc run "$NAME" "x" --dry-run
acpc run "$NAME" "Reply with exactly: OK" --timeout 180
```

Pass: exact answer (or acceptable short reply) and exit 0. Auth failures should
name login / missing passthrough — fix env, do not widen permissions.

### 7. Isolation and where the entry lives

```bash
acpc daemon status "$NAME"    # own target when home/env differ
acpc agents --check "$NAME"   # live apply of resolved options; zero turns
# (skip --check model/effort asserts if those options are unsupported)
```

- Own `home` ⇒ own daemon target.
- Secrets only as `env_passthrough` **names**, never `[env]` values.
- On a base adapter, `[env]` is the full declared set (no parent merge).
- Operator: `~/.acpc/agents/$NAME.toml`. Ship with acpc:
  `src/acpc/data/agents/$NAME.toml` (product change: tests, SPEC/README as
  needed).

## The entry (base adapter fields)

| Field | Role |
|---|---|
| `command` | Process acpc spawns (ACP on stdio). Required. |
| `install_command` | Trusted shell line for `acpc install <name>`. Optional. |
| `install_docs` | Vendor install URL when there is no trusted installer. Optional. |
| `home` / `home_env` | Vendor config+credentials dir; env var that points at it. |
| `env_passthrough` | Caller env **names** forwarded at call time. |
| `[env]` | Literal non-secret values. |
| `[modes]` | Mode ids + assumed/measured permission facts. Required for run. |
| `[presets]` | After model/effort apply path works (`*_via` or config options). |
| `[effort_by_model]` / `effort_config_id` | Optional per-model allowlists (empty/omitted = global scale); config option id if non-default. |
| `model_via` / `effort_via` / `effort_cli_flag` | Wire workarounds when config options are missing. |
| `description` | Roster purpose. |

Shape references (not values to copy): package `data/agents/claude.toml`,
`data/agents/codex.toml`, `data/agents/grok.toml`.

## Traps

- **UI ≠ adapter.** Wrong process, silent non-ACP.
- **Empty `[modes]` blocks run/dry-run.** Expected until rung 4.
- **Empty discovery ≠ "no modes exist".** May mean the agent does not advertise
  ACP modes; use Path B.
- **`set_session_mode` OK on every string.** Acceptance ≠ measured ceiling.
- **Copying another entry's `[modes]`.** Clean table, wrong ids/ceilings.
- **Presets before wire proof.** Method not found on every run.
- **Effort / "thinking" labels in `[modes]`.** Pollutes permission selection.
- **Secrets in `[env]`.** On disk in the entry.
- **Ambient env leakage.** Undeclared keys never reach the adapter.
- **Wrong `home_env`.** Vendor stays on default home; wrong credentials, quiet.

## Known lies

| Symptom | Actually means |
|---|---|
| `run` / `--dry-run` refuse; message about modes/policy | Empty or unusable `[modes]`, not a missing model. Rung 4. |
| `probe --discover` → 0 advertised | Catalogue missing on the wire — not proof the binary has no permission modes. Path B. |
| Mode name from another adapter "should work" | Mode ids are vendor-local. |
| `the adapter rejected model|effort '…'` with Method not found, Unknown config option, or similar | Config-option path missing — set `model_via` / `effort_via` after proving the alternate wire, or drop pins. |
| `missing → acpc install …` | argv[0] not on PATH and the entry has `install_command`. |
| `missing → https://…` | argv[0] not on PATH; entry has `install_docs`, no `install_command`. Install the vendor CLI. |
| Auth fails only under acpc | Passthrough or `home`/`home_env` wrong; re-run bare with the same env (rung 2). |
| Daemon shares traffic with another entry | Same target key (agent + home + declared env + policy). Own `home`. |
| `0 tok` / odd cost in the summary | Prefer ACP `usage_update`; acpc also reads PromptResponse `_meta` totals. Still not a failed turn if exit 0 and answer present. |

## When you are done

```bash
acpc agents "$NAME"
acpc probe "$NAME" --discover
acpc run "$NAME" "Reply with exactly: OK" --timeout 180
```

On Path B, `probe --discover` stays one-sided dirty (entry has modes, catalogue
empty). That is expected — do not delete table rows to "clean" the diff.

Leave comments in the entry: command argv + version if known, how modes were
sourced (discovery vs docs), assumed vs measured, whether model/effort config
options work. The entry is the record; this skill is the method.
