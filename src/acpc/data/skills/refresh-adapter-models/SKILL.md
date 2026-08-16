---
name: refresh-adapter-models
description: >-
  Refresh an existing acpc adapter overlay when the vendor model catalogue
  moved (new model id on claude, codex, grok, or any other registered adapter;
  stale fast/standard/max). Upgrade the vendor binary first — that is what
  advertises new ids — then write only $ACPC_HOME/agents/<name>.toml after
  the user confirms the proposed patch. Not a new base adapter
  (adapter-bringup) and not a new provider or third-party model
  (provider-bringup). Use when asked to update adapter toml models, retarget
  presets, add [effort_by_model] rows, or "the advertised models changed".
---

# Refresh adapter models

Vendor catalogue moved on a working adapter. New model ids come from a
newer vendor binary, not from the overlay. Upgrade that process first,
then refresh the operator overlay. Grok snippets are a shape, not the
only target. Investigate first; write only after step 5 yes.

## 1. Update the adapter

The advertised list is whatever `command` currently speaks. A stale
binary cannot grow new ids. Update that process **before** `--check`.

- Entry has `install_command` → propose `acpc install $NAME` (same
  trusted one-liner). Ask, then run it.
- No `install_command` → vendor's own upgrade from `install_docs` (grok:
  the `grok` CLI). Ask; do not invent `curl | bash`.
- Binary missing → same path, still ask. Do not silently install.

Then `acpc agents --check "$NAME"`. If check still fails, stop — the
catalogue is not trustworthy.

## 2. Which entry

```bash
NAME=<adapter>          # stem: acpc run $NAME
acpc agents             # roster
acpc agents "$NAME"     # resolved entry + base_adapter
OVERLAY="${ACPC_HOME:-$HOME/.acpc}/agents/${NAME}.toml"
```

If the entry has `extends`, it is a variant: advertised models belong to
its `base_adapter`. Tell the user and set `NAME` to that base so the
overlay is `grok.toml` / `claude.toml` / …, not the variant file.
Third-party model on a new home → `provider-bringup`. Empty `[modes]` or
no `command` → `adapter-bringup`. Do not invent an entry.

Same filename as the shipped adapter = overlay merge. Never write
`src/acpc/data/agents/`. A maintainer may copy the overlay
into the package later.

## 3. Catalogue

Read, do not edit: `acpc agents "$NAME"`, `$OVERLAY` if it exists. Record
current `fast` / `standard` / `max` and every `[effort_by_model]` key. Do
not copy rows from another adapter's TOML.

Stale cache is yesterday's list:

```bash
acpc agents --check "$NAME"      # live probe; refreshes cache
acpc agents "$NAME" --models     # advertised ids + current presets
acpc run "$NAME" "x" --dry-run   # today's default (no --model)
```

If resolve refuses because no mode grants the default policy (codex has
no `read` ceiling), retry with `--permissions edit` (live turns: `all`).
That is not an overlay change.

The advertised **models** list is the catalogue — not OpenRouter, a blog,
or another entry. The dry-run `model` line is today's default id.

| Bucket | Meaning |
|---|---|
| **new** | advertised, not in presets and not a table key |
| **still there** | advertised and already in the entry |
| **gone** | in the entry, not advertised — **keep** (callers still pin them) |

If nothing is **new** and the default id did not move, say so and stop.
If the default and every preset target are still advertised, **new** ids
alone are not a reason to write. Report them and stop unless the user
asked to pin those ids. Measuring them is optional and costs one turn
per level — wait for yes on that grid, or omit those rows.

## 4. Measure each new model

A new id does not inherit a sibling's effort list. Advertised is not
runnable: if this account / plan rejects the model itself, omit the row.

`--dry-run` only checks acpc's table. A live turn that exits 0 is not
enough: harnesses often fall back (unknown effort → default) and still
answer. `meta.json` / `acpc status` record what acpc **sent**, not what
the vendor applied.

For every **new** id, a level enters the row only from the **intersection**
of docs and a live apply that did not reject:

1. **This product's docs for this model** — official page / ACP or CLI
   reference for *this* vendor. Not OpenRouter, not another family's page,
   not "same as the last id". Docs say no effort control → row is `[]`
   and no preset may pin `effort` on it.
2. **Live apply.** Candidates = docs ∩ acpc's global scale
   (`none minimal low medium high xhigh max ultra`):

   ```bash
   acpc run "$NAME" --model "$NEW" --effort "$LEVEL" \
     "Reply with exactly: OK" --timeout 180
   ```

   Keep `$LEVEL` only when docs list it **and** the turn did not reject
   it (`unsupported` / `unknown option` / `Invalid params`). If you cannot
   tell whether the vendor stuck that level, omit it and say so.

User refuses the turn cost → **omit the row** (unlisted → warning + union
is legal). Do not invent a row from docs alone or from a silent 0-exit.

Leave **still there** / **gone** rows unless the user asked to re-measure.

If the live table is **empty** (global vocab), adding the first row changes
every unlisted model from the global scale to that row's union — and they
start warning. Do not propose a lone new row on an empty map. Either
measure the **still there** preset targets too, or write nothing.

## 5. Propose, confirm, write

Tiers are a product choice, not "newest string wins".

- **`standard`** — only if the no-`--model` default id moved. Point it
  there. Effort must be in the new row, or omit `effort` when the row is
  `[]`.
- **`fast` / `max`** — only with a reason (user ask, or that tier's
  current model is **gone**) and a measured row (or `[]`) for the target.
  Newest advertised id is not automatically `max`.
- Unsure → leave the tier.

Quote ids that are not bare TOML keys (`"grok-4.7"`, `"deepseek/…"`).
Never write `efforts = […]`. Never touch `command`, vias, `[modes]`,
`[env]`, `home`.

Show the proposal **before** creating or editing `$OVERLAY`. Wait for an
explicit yes. "Refresh models" is not a yes to this patch.

The proposal names:

- `$NAME` and `$OVERLAY`
- catalogue date and the advertised list
- **new** / **still there** / **gone** ids
- each new `[effort_by_model]` row, and for every level: docs URL + live
  reject-or-accept (or "omitted — fallback / unclear")
- preset lines you will change, old → new
- what you will not write (deletions, unmeasured levels)
- if the table is empty today: that the first row changes the unlisted
  fallback (global scale → this union) and will warn on preset models
  that still have no row

No or a different default → revise and ask again. Write only after yes.

New overlay — only the tables you need:

```toml
# Live catalogue 2026-08-16: grok-4.7 (default), grok-4.6, grok-4.5.
# Shape example — same tables on any $NAME.
[effort_by_model]
"grok-4.7" = ["low", "medium", "high", "xhigh"]

[presets]
fast = { model = "grok-4.5", effort = "low" }
standard = { model = "grok-4.7", effort = "high" }
max = { model = "grok-4.7", effort = "xhigh" }
```

Existing overlay: add or replace those keys in place. Keep every other
key and comment. Date the catalogue comment.

## 6. Prove the overlay

```bash
acpc agents "$NAME"
acpc run "$NAME" "x" --dry-run
acpc run "$NAME" --model fast --dry-run
acpc run "$NAME" --model standard --dry-run
acpc run "$NAME" --model max --dry-run
```

Each dry-run must resolve the models you proposed. A preset whose effort
the new row rejects means the overlay is wrong — fix the approved patch
(no new proposal). A different model or effort choice needs a new yes.
Report `$OVERLAY` and the diff.

## Traps

| Symptom | Actually means |
|---|---|
| `--models` without `--check` | Yesterday's catalogue. |
| Edit `src/acpc/data/agents/` | Wrong tree. Overlay only. |
| Live turn exit 0, docs omit the level | Silent fallback. Do not write that level. |
| First row on an empty map | Unlisted models leave the global scale. Name it. |
