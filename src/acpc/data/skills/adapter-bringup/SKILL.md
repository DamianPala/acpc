---
name: adapter-bringup
description: >-
  Bring up a new base adapter for acpc: any ACP-speaking process registered with
  its own command (no extends). Use when adding a vendor agent that is not
  already shipped (or a local ACP binary), writing ~/.acpc/agents/<name>.toml
  with command/home/modes, or when run/dry-run refuse because the modes table is
  empty. Not for pointing an existing adapter at a new provider or model — that
  is provider-bringup.
---

# Bringing up an adapter

An **adapter** is any process that speaks ACP on stdio. acpc spawns it, builds
its environment, and maps `--permissions` onto that process's session modes.
This skill is for a **new base entry**: `command` set, no `extends` — shipping
product, local binary, or wrapper, same ladder.

If the agent is already shipped as a base (claude, codex, …) and you only need
another provider, home, or model, stop — use `provider-bringup`. That path
inherits `[modes]` and the command; this one does not.

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
| same name as a shipped adapter | field overrides on that adapter (e.g. `[presets]`) |

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
entry itself.

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
# install_command = "…"
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
- Do not put `efforts` / free keys under `[modes]` — only mode → table values.

```bash
acpc probe "$NAME" --discover   # report only; empty catalogue stays empty
```

### 5. Model and effort — prove the wire before presets

acpc applies call options with:

- `session/set_mode` for the selected mode,
- `session/set_config_option` with `config_id=model` and
  `config_id=<effort_config_id>` (default `reasoning_effort`).

**Before** `[presets]` or pinning `model` / `effort` on the entry:

1. Run one turn with **no** model/effort in the entry (adapter default).
2. If you need a pin, try dry-run + run with `--model` / `--effort`, or
   `acpc agents --check` after adding a preset.
3. If the adapter returns **Method not found** / unknown config option, stop
   pinning. Models may live only in vendor `_meta`, CLI flags on `command`, or
   home config — acpc's standard config-option path does not reach them yet.
   Leave presets empty; document the gap in the entry comments.
4. Effort (or "thinking") levels sometimes appear in a product UI as a "mode"
   category — that is **not** a permission mode. Keep them out of `[modes]`.
   Put accepted effort values in `efforts = […]` only after a successful set of
   that option.
5. Set `effort_config_id` only when the adapter's option id is not the default.

```toml
# Only after set_config_option accepts these values on this binary:
# efforts = ["low", "medium", "high"]
# effort_config_id = "effort"
#
# [presets]
# standard = { model = "…", effort = "high" }
```

A stale preset that fails loud is better than a silent wrong model — but a
preset that always hits Method not found is noise; omit it.

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
| `home` / `home_env` | Vendor config+credentials dir; env var that points at it. |
| `env_passthrough` | Caller env **names** forwarded at call time. |
| `[env]` | Literal non-secret values. |
| `[modes]` | Mode ids + assumed/measured permission facts. Required for run. |
| `[presets]` | Only after model/effort config options work on this binary. |
| `efforts` / `effort_config_id` | Only after those options work; non-default effort id if needed. |
| `description` | Roster purpose. |

Shape references (not values to copy): package `data/agents/claude.toml`,
`data/agents/codex.toml`.

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
| `the adapter rejected model|effort '…'` with Method not found, Unknown config option, or similar | No working ACP config-option path for that pin on this binary. Drop pin/preset; default model/effort only until product or acpc grows a path. |
| `missing → acpc install …` | argv[0] not on PATH; install binary or set `install_command`. |
| Auth fails only under acpc | Passthrough or `home`/`home_env` wrong; re-run bare with the same env (rung 2). |
| Daemon shares traffic with another entry | Same target key (agent + home + declared env + policy). Own `home`. |
| `0 tok` / odd cost in the summary | Usage mapping may not match this agent; not a failed turn if exit 0 and answer present. |

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
