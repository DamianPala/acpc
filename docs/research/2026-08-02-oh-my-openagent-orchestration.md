I’ll first verify network reachability, then inspect the repository directly and anchor each conclusion to code.The repository’s Senpi task subsystem is the relevant implementation; it has both live-progress and control paths, not just final-result delivery.Network verified: `git ls-remote` reached GitHub and returned commit [`47802a9`](https://github.com/code-yeongyu/oh-my-openagent/tree/47802a9bcfcdac27a043413985875d3456ddcf91).  
Evidence below is from that commit’s `packages/senpi-task` implementation.

## 1. Progress and observability

- Foreground `task` calls push compact live updates through the host tool callback, not raw events: [`execute.ts` · `runSpawn`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/task/execute.ts) subscribes to child events and calls `onUpdate(...)`, throttled to 250 ms.
- The update unit is structured status plus a short text fragment, with fields such as `currentTool`, `lastAssistantLine`, `turns`, `tokens`, and `tokensPerSecond`: [`progress.ts` · `ToolProgressDetails`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/progress.ts), quoted: `"↳ last: ${lastAssistantLine}"`.
- Background dispatch returns immediately, while `task_output` is an explicit pull API that “always returns immediately”: [`output.ts` · `DESCRIPTION`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/output.ts).
- Pull status returns lifecycle facts and final result when present, while `tail` and `full` return a persisted transcript: [`output.ts` · `outputForRecord`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/output.ts).
- This is not a raw event feed, because [`transcript-log.ts` · `toPersistedEvent`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/manager/transcript-log.ts) retains only assistant completions, tool-end markers, child errors, and fallback events, and says “Every other event is ignored.”
- The durable buffer is the task record plus `logs/<taskId>.jsonl`: [`event-log.ts` · `appendTaskEvent`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/store/event-log.ts).
- Transcript reads are capped at 30 000 characters: [`render.ts` · `TRANSCRIPT_MAX_CHARS`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/render.ts).
- Completion is pushed to the parent, with buffering during transient parent states: [`completion/routing.ts` · `routeCompletion`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/completion/routing.ts), quoted: `"idle" -> { kind: "wake" }` and `"streaming" -> { kind: "deliver_streaming" }`.
- The completion buffer itself is in-memory per parent session: [`completion/notifier.ts` · `createCompletionNotifier`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/completion/notifier.ts), `const buffered = new Map<string, BufferedEntry[]>()`.

## 2. Detecting stuck or degraded work

- Process-mode children have an internal 10-second liveness probe: [`rpc-process.ts`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/rpc-process.ts) sets `DEFAULT_HEARTBEAT_INTERVAL_MS = 10_000`.
- The probe sends `get_state` and records a local `lastSeenAt`: [`rpc/handle.ts` · `createRpcChildHandle`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/rpc/handle.ts), quoted: `lastSeenAt = now()`.
- I could not find `lastSeenAt` in the public `task_output` snapshot, whose fields are status, age, pid, session id, result/error, and terminal `run_stats`: [`output/types.ts` · `TaskSnapshot`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/types.ts).
- The public evidence of process liveness is therefore indirect, namely PID, age, and eventual `lost` status: [`snapshot.ts` · `buildTaskSnapshot`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/snapshot.ts).
- Reconciliation checks whether the recorded PID is alive and labels an orphan heartbeat “fresh” or “stale” from `updated_at`: [`lifecycle/reconcile.ts` · `heartbeatState`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/lifecycle/reconcile.ts).
- A vanished or unreachable child becomes `lost`, with a diagnostic and PID/session-directory breadcrumbs: [`state/types.ts` · `TaskStatus`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/state/types.ts) and [`snapshot.ts` · `LOST_EXPLANATION`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/snapshot.ts).
- Run statistics include turns, tool calls, total/output tokens, cost, cache-hit rate, and measured generation throughput: [`state/types.ts` · `TaskRunStats`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/state/types.ts).
- I found no public rule that distinguishes “thinking hard” from “wedged” before a process exit, reconciliation, or visible new event.

## 3. Trust in the result

- A terminal answer is explicitly distinguished from failure by `TaskRecord.status`, `final_response`, `error_message`, and optional `killed`: [`state/types.ts` · `TaskRecord`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/state/types.ts).
- Tool failures are retained in the pull transcript as `tool[error]`: [`render.ts` · `renderEntry`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/render.ts).
- Provider or child-turn failures are retained as transcript `error:` entries: [`transcript-log.ts` · `assistantFailure`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/manager/transcript-log.ts).
- Model fallback is persisted as an event and reflected in `fallback_attempts`: [`runtime-fallback-event.ts` · `applyRuntimeFallbackEvent`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/manager/runtime-fallback-event.ts).
- Completion details include the resolved model, fallback chain, run stats, terminal status, and final response: [`completion/notification.ts` · `buildCompletionDetails`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/completion/notification.ts).
- I found no explicit reliability verdict such as “answer incomplete because capability X was denied.”
- Permission/UI requests in process mode are auto-denied or cancelled rather than escalated: [`rpc/ui-auto-answer.ts` · `buildAutoUiResponse`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/rpc/ui-auto-answer.ts), quoted: `confirmed: false` and `cancelled: true`.
- Thus a successful prose answer can coexist with a prior tool error or auto-denied capability, and the orchestrator must inspect transcript evidence rather than trust a success status alone.

## 4. Intervention

- `task_send` steers plain text immediately into an ordinary running child: [`control/send.ts` · `runTaskSend`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/control/send.ts), quoted: `deliverAs: "steer"`.
- A message to a finished resident child revives the same session: [`control/send-results.ts` · `mapSendOutcome`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/control/send-results.ts), which returns `{ kind: "revived" }`.
- `task_cancel` is a terminal intervention: [`control/cancel.ts` · `DESCRIPTION`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/control/cancel.ts), quoted: “NOT resumable.”
- Process-mode steering and cancellation map to child RPC commands `steer` and `abort`: [`rpc/handle.ts`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/rpc/handle.ts).
- I found no parent-mediated question or approval relay, because headless extension UI requests are automatically denied as above.
- Team messages differ from ordinary child steering, because they are durable mailbox writes consumed by recipient polling later: [`team/messaging/send.ts`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/team/messaging/send.ts), quoted: “never reserves, reads, steers, revives, or notifies.”

## 5. Capabilities and permissions

- Agent types declare `tools`, `executionMode`, `allowedSubagents`, and depth limits: [`agents/types.ts` · `AgentDefinition`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/agents/types.ts).
- Per-agent allow rules become a child `toolAllowlist`: [`agents/resolve-agent.ts` · `agentPersona`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/agents/resolve-agent.ts).
- In-process children reuse parent auth, model registry, model runtime, and parent tool closures: [`runners/in-process.ts` · `InProcessRunner.start`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/in-process.ts).
- In-process children cannot receive `task` or `team_*` tools from that inherited set: [`shared-tool-filter.ts` · `filterSharedParentTools`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/in-process/shared-tool-filter.ts).
- Curated research agents replace `bash` with a read-only `curl`/`gh` wrapper: [`curated-readonly-bash.ts`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/in-process/curated-readonly-bash.ts).
- Process children inherit the parent environment and cwd, but launch with `--no-extensions` plus only explicitly forwarded extensions: [`rpc/spawn.ts` · `buildRpcSpawn`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/rpc/spawn.ts).
- I could not find process-mode propagation of `toolAllowlist` into [`RpcRunnerSpec`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/types.ts) or `buildChildArgs`.
- Therefore the project has clear in-process narrowing mechanisms, but I could not establish a system-wide “children may be narrowed but never widened” sandbox guarantee.

## 6. Return contract

- A synchronous dispatch returns prose plus structured tool details containing `task_id`, terminal `status`, model, resolved model, fallback attempts, and run stats: [`task/result-details.ts` · `recordDetails`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/task/result-details.ts).
- A background dispatch returns a started task id/status immediately: [`task/execute.ts` · `runSpawn`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/task/execute.ts).
- The manager maps normal completion to `completed` with `final_response`, turn failure to `error` with `error_message`, and cancellation to `cancelled`: [`manager.ts` · `#trackOutcome`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/manager/manager.ts).
- Dispatch failure is distinct from child failure, because `task` returns a `start_failed` result before a child run exists: [`task/execute.ts` · `started.kind === "start_failed"`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/task/execute.ts).
- Process exit is further classified as clean, killed, crashed, or spawn error: [`rpc/exit-mapping.ts` · `classifyChildExit`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/rpc/exit-mapping.ts).
- Large completion prose is capped at 32 000 characters and spilled to a local result file: [`completion/notification.ts` · `FINAL_RESPONSE_TRANSPORT_LIMIT`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/completion/notification.ts).

## Three ideas worth taking for acpc

1. **A pull-first, bounded task snapshot plus transcript tail.**  
   This transfers well because the daemon can reduce ACP notifications into `{status, last_event_at, current_tool, last_assistant_line, counters, trust_flags}` and return it only when the caller asks.  
   Borrow [`task_output`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/tools/output/output.ts), but not its foreground push UI, because ACP cannot return mid-request.

2. **A durable evidence ledger separate from final prose.**  
   This transfers well because ACP already supplies tool status, message chunks, plan updates, and counters, while [`transcript-log.ts`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/manager/transcript-log.ts) shows how to retain only decision-relevant facts.  
   Improve it by emitting explicit flags such as `tool_failed`, `permission_denied`, `capability_missing`, `stream_silent`, and `fallback_used`, which this project notably lacks.

3. **Liveness as evidence, not a verdict.**  
   The internal `get_state` heartbeat in [`rpc/handle.ts`](https://github.com/code-yeongyu/oh-my-openagent/blob/47802a9bcfcdac27a043413985875d3456ddcf91/packages/senpi-task/src/runners/rpc/handle.ts) is useful, but active polling does not directly transfer if ACP cannot accept another request during a turn.  
   acpc can still expose `last_notification_at` and “silent for N seconds,” but must not label that “wedged” without a protocol-supported heartbeat or termination signal.