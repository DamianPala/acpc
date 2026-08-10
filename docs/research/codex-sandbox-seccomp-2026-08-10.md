# codex-acp AF_UNIX bind EPERM: research report (Opus, 2026-08-10)

## Headline

The Landlock hypothesis is wrong. The blocker is **seccomp**: `deny_syscall(&mut rules, libc::SYS_bind)`
in codex's Linux sandbox — when network access is disabled, `bind()` is denied unconditionally for
**every** address family, including AF_UNIX. That is why the path never mattered.

The CLI succeeds and codex-acp fails not because of a stricter sandbox: codex-acp hardcodes
`networkAccess: false` and ships a fully-materialized policy object per turn, which bypasses the
`network_access = true` in config.toml.

## Q1: Root cause (confirmed from source)

`codex-rs/linux-sandbox/src/landlock.rs` (name historical; filesystem is bubblewrap, this is seccomp):

```rust
NetworkSeccompMode::Restricted => {
    deny_syscall(&mut rules, libc::SYS_connect);
    deny_syscall(&mut rules, libc::SYS_accept);
    deny_syscall(&mut rules, libc::SYS_bind);      // line 191
    deny_syscall(&mut rules, libc::SYS_listen);
    deny_syscall(&mut rules, libc::SYS_getsockname);
    deny_syscall(&mut rules, libc::SYS_getsockopt);
    deny_syscall(&mut rules, libc::SYS_setsockopt);
    // socket() and socketpair() allowed ONLY for AF_UNIX
}
```

Match action is `SeccompAction::Errno(libc::EPERM)` — the measured errno 1. `socket(AF_UNIX)` is
explicitly allowed, then `bind()` fails: hence "socket creation works but bind doesn't".

Worse than measured:

- `connect()` is denied too — a sandboxed test *client* cannot reach a Unix socket even if the daemon
  runs outside the sandbox. Moving the daemon out does not rescue the suite.
- `getsockname`/`getsockopt`/`setsockopt` denied — most socket libraries call these during setup.

Landlock is doubly a dead end: `install_filesystem_landlock_rules_on_current_thread` is in-source
"currently unused" (bubblewrap does FS), and it grants `AccessFs::from_all(abi)` which on ABI V5
*includes* `MakeSock`.

**Why the CLI differs.** `codex-rs/core/src/config/permissions.rs` resolves the built-in workspace
profile from config: `network_access = true` → `NetworkSandboxPolicy::Enabled` →
`network_seccomp_mode()` returns `None` → **no seccomp filter at all**. Bind works.

codex-acp never takes that path. Installed bundle
`/home/haz/.local/lib/node_modules/@agentclientprotocol/codex-acp/dist/index.js:26056`:

```js
static Agent = new _AgentMode("agent", "Agent", "...", "on-request",
  { type: "workspaceWrite", writableRoots: [], networkAccess: false,
    excludeTmpdirEnvVar: false, excludeSlashTmp: false },
  "workspace-write");
```

and line 26773 passes that object straight into `runTurn({ sandboxPolicy: ... })`. The policy arrives
fully specified over the app-server protocol; codex never consults config.toml.

**Implication (supervisor's note):** the same hardcoded object carries `writableRoots: []`, so the
`writable_roots = ["~/.cache/uv"]` added to both codex homes during 0.5 likely never reached agent
sessions either. Verify with one write test from inside a sandboxed agent.

## Q2: Configurability

**No knob** grants Unix-socket bind while network stays restricted on Linux. `NetworkSandboxPolicy`
is `Restricted | Enabled`, nothing else keys the seccomp decision.

`allow_local_binding` and `permissions.<id>.network.unix_sockets` are real but belong to the
network-proxy subsystem (`codex-rs/network-proxy/`, `permissions_toml.rs:343`); they engage only in
managed-proxy sessions, which install `ProxyRouted` mode — worse: `socket()` denied for every family
except AF_INET/AF_INET6; only `socketpair(AF_UNIX)` survives.

codex-acp env vars: `INITIAL_AGENT_MODE` (preselect ACP mode, bundle:26115) and `CODEX_CONFIG`
(model/provider JSON, not sandbox, bundle:31488). codex-acp spawns `codex app-server` with no `-c`
args (bundle:22072); a wrapper injecting `-c` would not help since the per-turn policy overrides
config regardless.

## Q3: GitHub intel

| Issue | State | Relevance |
|---|---|---|
| openai/codex#24943 | open (2026-05-28) | Closest match: Py3.14 forkserver `listener.bind()` PermissionError in sandbox. No maintainer response. |
| openai/codex#10797 | closed (2026-02) | AF_UNIX *connect* blocked with `network_access=false`. Closed without code fix; workaround = `network_access=true`. |
| openai/codex#25076 | open | Same class: Codex App denies Docker Unix socket while CLI allows it, identical config. |
| openai/codex#12702 | merged 2026-03-05 | macOS Seatbelt only: explicit AF_UNIX bind permissions. **Linux got no equivalent.** |
| anthropics/claude-code#44180 | open | Feature request for Linux bwrap/seccomp Unix-socket allowlist, citing codex. Nobody has solved this on Linux. |

No issue found on codex-acp's hardcoded `networkAccess: false` — genuinely unreported;
`github.com/agentclientprotocol/codex-acp` is where it would go.

## Q4: `auto` mode — ruled out conclusively

`codex-rs/utils/approval-presets/src/lib.rs`:

```rust
ApprovalPreset {
    id: "auto",
    label: "Default",
    description: "... (Identical to Agent mode)",
    approval: AskForApproval::OnRequest,
    permission_profile: PermissionProfile::workspace_write(),
}
```

`auto` = `workspace_write` + `OnRequest`, exactly what codex-acp's `agent` mode sends. Its absence
from ACP is cosmetic naming, not a missing capability.

Same preset explains why codex never emits `request_permission`: `OnRequest` IS set for `read-only`
and `agent`, but the escalation path is not wired through the adapter — a separate defect from the
socket one.

## Q5: Workarounds

- **Abstract namespace sockets: dead.** Denial is at the `bind()` syscall, before address parsing.
- **TCP loopback: dead in restricted mode.** `socket(AF_INET)` denied outright. Works only with
  network enabled, at which point AF_UNIX bind already works.
- **`socketpair(AF_UNIX)`: works** (explicitly permitted, line 216). A test transport on inherited
  socketpair FDs runs under the sandbox unchanged. Constraint: no rendezvous by path — daemon must be
  spawned as a child of the test process; no reconnect, no out-of-band attach.

## Ranked paths to "sandboxed builder runs the full suite"

1. **Patch one line in the codex-acp bundle** — `networkAccess: false` → `true` at `dist/index.js:26064`.
   No seccomp filter installed; bubblewrap keeps FS confinement. Confidence: high (full causal chain
   source-confirmed). Cost: agent gains real internet (Linux cannot separate the two). Caveat: any
   npm reinstall silently reverts — re-apply and verify after every codex-acp upgrade.
2. **`--mode agent-full-access`** or `INITIAL_AGENT_MODE=agent-full-access`. No patching, survives
   upgrades. Confidence: high. Cost materially worse: `dangerFullAccess` removes the FS sandbox
   entirely and sets approval to `never`.
3. **socketpair test transport in the daemon.** Only path keeping the sandbox fully intact.
   Confidence: high on mechanism; real work, imposes parent-spawns-child topology on ~45 tests.
4. **File upstream.** Two reports: codex-acp should honor `sandbox_workspace_write.network_access`
   (or expose a network toggle per mode); openai/codex should allow AF_UNIX bind under Restricted as
   #12702 did for macOS. Slow; macOS precedent is a strong argument.

Recommendation: option 1 now, option 3 as the durable fix if sandboxed builders should have no
network. **Verify locally first:** whether the account uses a managed network proxy — if
`allow_network_for_proxy` is true, enabling network flips into `ProxyRouted` mode (worse). A single
bind test after the patch settles it.

## Research friction

- WebFetch on raw.githubusercontent.com returns model-summarized renderings; first pass on
  landlock.rs missed the `SYS_bind` denial. `curl` + direct read cracked it — skip WebFetch for
  source files.
- developers.openai.com 308-redirects to learn.chatgpt.com; WebFetch refuses cross-host redirects.
  `gh api search/code` was the better tool for this question shape.

Sources: landlock.rs, protocol/permissions.rs, core/config/permissions.rs, approval-presets/lib.rs,
config/permissions_toml.rs in openai/codex; issues #24943, #10797, #25076; PR #12702;
anthropics/claude-code#44180.
