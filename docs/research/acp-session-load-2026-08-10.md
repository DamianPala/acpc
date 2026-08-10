# ACP `session/load` semantics: research report (Opus, 2026-08-10)

## Verdict

The transcript-comparison verifier is **viable with modifications**, and the modifications change
the design. Comparing replayed history against `transcript.ndjson` produces false mismatches on
healthy sessions — four independent reasons, three in shipped code. What survives: compare the
**user messages** in the replay against acpc's stored `prompt.md` / `prompt.N.md`, as an ordered
subsequence. There is also a cheaper primary check acpc isn't using: `session/list` (codex-acp
advertises it) confirms a session id exists with a matching `cwd` without replaying anything.

## 1. What the ACP spec guarantees

Source: `docs/protocol/v1/session-setup.mdx` in agentclientprotocol/agent-client-protocol.

Replay is **mandatory**: "The Agent MUST replay the entire conversation to the Client in the form
of `session/update` notifications", and the Agent MUST respond to `session/load` only after all
entries streamed. Ordering guaranteed: every replayed update precedes the response.

Unspecified: the *form* (only `user_message_chunk`/`agent_message_chunk` are exemplified; tool
calls/thoughts/plans neither required nor forbidden); message ids ("*if* provided", opaque);
unknown/stale id behavior (no error code specified; `error.mdx` is a stub). Convention:
`-32002 "Resource not found"` — named constructor `RequestError.resource_not_found` in the SDK
(`acp/exceptions.py:41`), and what codex-acp returns in practice.

Protocol v1 already has `session/resume` (restore without replay, gated on
`sessionCapabilities.resume`; codex-acp advertises it). Protocol v2 **removes `session/load`**,
folding it into `session/resume` with optional `replayFrom` cursor (`{"type":"start"}` = full
history) — replay becomes opt-in per call, which suits a verifier. v2 states replay framing is the
agent's choice ("full `content` arrays or as chunks"): never depend on chunk boundaries.

## 2. What codex-acp actually does

Installed: `@agentclientprotocol/codex-acp@1.1.9` (readable bundle at
`~/.local/lib/node_modules/@agentclientprotocol/codex-acp/dist/index.js`); backing CLI
`@openai/codex@0.147.0`. Development moved zed-industries → agentclientprotocol org; the new
adapter talks to the codex **app-server**, not rollout files directly.

Load path: `loadSession` → `threadResume({threadId: request.sessionId, ...})` →
`threadRead({threadId: response.thread.id, includeTurns: true})`. It echoes back
`request.sessionId` while history comes from `response.thread.id` — **assumed equal, never
compared**. The one place a wrong-conversation attach could hide; unaudited assumption, no
evidence of an actual divergence.

**Unknown id fails loudly**: `-32002 Resource not found` propagates (zed-industries/codex-acp#203);
nothing silently creates a new session.

**Storage / ids / restart survival**: one rollout per thread at
`~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl`, first line `session_meta` with
`session_id`, `cwd`, `originator`, `cli_version`. Ids are UUIDv7, globally unique, survive
restarts. Verified against acpc session `x7bv`: resume **appends to the same rollout under the
same id** (two `user_message` events, one per acpc turn). `session_index.jsonl` is stale (last
write 2026-07-31 vs 245 August rollouts) and `state_5.sqlite` is the desktop app's store
(openai/codex#16385) — neither is trustworthy. Rollout `originator` distinguishes provenance:
`@agentclientprotocol/codex-acp` vs `codex-tui`.

## 3. Replay fidelity — why transcript comparison fails

- **(a)** acpc's transcript chunking is wall-clock dependent (`client.py:62-64`:
  `_CHUNK_GAP_SECONDS=1.0`, `_CHUNK_MAX_AGE_SECONDS=2.0`, `_CHUNK_MAX_CHARS=4096`) — identical
  text yields different event counts across runs.
- **(b)** Reasoning replays as `item.summary`, not raw deltas — thoughts must be excluded.
- **(c)** Replay can contain events the live turn never emitted (codex-acp#222 multi-agent tool
  calls replay-only; zed#191 shell calls replay as generic `exec_command`).
- **(d)** Rollout-file fallback (`createResponseItemHistoryFallbackUpdates` +
  `mergeHistoryUpdates`, deduped on `historyUpdateKey`) makes replay composition
  non-deterministic; fallback-sourced updates carry **no messageId**; and the merge is
  known-buggy — codex-acp#355 (open, against 1.1.9): replay includes **rolled-back turns**.

Not found: compaction loss — `contextCompaction` is a first-class thread item; history survives
compaction (strongly implied by source, not measured).

**Stable enough to compare**: ordered sequence of replayed **user message texts** (both code paths
produce them from text acpc authored verbatim; content-based dedupe prevents doubling). Note
`transcript.ndjson` has no user-message event type at all (`transcript.py:25`) — the reference is
the prompt files.

## 4. Prior art: nobody verifies

`openclaw/acpx` (closest peer): documented reconnect ladder "falls back to `session/new` if
reconnecting fails, transparently updating the saved record" — a failed resume silently becomes a
fresh context, exactly the failure mode acpc wants to rule out. Zed trusts the id by construction.

Issues (resume-is-lossy family, none reporting wrong-conversation attach):
codex-acp#355 (open, rolled-back turns in replay), **codex-acp#343 (open: `session/load` resets
model/effort to config.toml defaults** — acpc is probably immune because `apply_call_options`
re-applies both after load, but that immunity is accidental; add an explicit test), codex-acp#206,
#222, zed#203, #186, #191, openai/codex#16385.

## 5. Warm-adapter hazard

**codex-acp side: no cross-session binding path.** `this.sessions` Map keyed by id;
`getSessionState` throws on miss; concurrent opens of the same id fenced by a generation counter.
Only the unchecked `request.sessionId` vs `response.thread.id` from §2 remains.

**acpc side: real asymmetry, probably a bug.** `daemon.py:663-681`: `mux.bind` happens *after*
`load_session` returns, and the multiplexer drops updates for unbound sessions — so on the daemon
path the entire replay is silently discarded. On the direct-spawn path (`runner.py:239-248`) the
client is attached at spawn, so replayed chunks flow into `_answer_parts` and the transcript — a
**cold `acpc continue` without the daemon should prepend the whole previous conversation to
`answer.md`**. Predicted from source, not observed (the one on-disk resume went through the warm
daemon). Cheap to confirm: cold continue with the daemon disabled.

Either way 0.6 needs an explicit replay mode on `AcpcClient`: updates between `session/load` and
its response go to a verifier sink, never to answer/transcript. Required to fix the contamination
anyway; exactly the hook the verifier needs.

## Recommended shape for 0.6

1. **Cheap primary check, no replay**: `session/list` when advertised (codex-acp 1.1.9 advertises
   `resume`, `list`, `close`, `delete`, `additionalDirectories`); assert stored
   `adapter_session_id` present with matching `cwd`; `title`/`updatedAt` as corroboration.
2. **Identity check via replay, narrowed**: replay sink before `session/load`; collect only
   `user_message_chunk`; concatenate per messageId (or contiguous run when absent); require stored
   prompts to appear **in order as a subsequence**, not exact prefix (extra user messages are
   legitimate: #355 rolled-back turns, out-of-band prompts). Missing/reordered prompts =
   hard error.
3. **Never compare** agent text, thoughts, tool calls, message ids, or event counts.
4. **Prefer `session/resume` for ordinary continue** (`resume_session` exists in
   agent-client-protocol 0.11.1, `interfaces.py:230`; codex-acp implements without streaming).
   Pay for `session/load` only when verifying; plan for v2.
5. **Codex-specific belt and braces**: rollout file existence-and-cwd check
   (`$CODEX_HOME/sessions/**/rollout-*-<uuid>.jsonl`, first line `session_meta`) — more definitive
   than replay comparison but adapter-specific; belongs behind the base_adapter abstraction.

## Research friction

- **Bash tool output passed through a silent string-rewriting filter**: `rg` output had the
  adapter's package name replaced by a bare `n` (e.g. `command = "n"`); Read showed the real
  content. Every quote re-checked with Read; bundle extraction via python3 heredocs.
- WebFetch on raw.githubusercontent 404'd for spec docs (paths differ from docs-site URLs); spec
  text came via `gh api … | base64 -d`. WebFetch on the rendered docs site returns summaries, not
  quotable source.
