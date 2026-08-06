"""Closed vocabularies shared across the CLI, registry, and session state.

SPEC.md defines each of these as a single vocabulary used verbatim everywhere
it appears (flags, `status` output, `log` footers, `meta.json`). Keeping them
in one dependency-free module lets every layer import them without cycles.
"""

# Superset reasoning-effort scale, mapped per adapter (SPEC.md `run --effort`).
EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# Approval policies for ACP permission requests (SPEC.md `run --permissions`).
PERMISSION_VALUES = ("all", "write", "read", "none", "prompt")

# Session states (SPEC.md *Session states*): one vocabulary, used verbatim.
SESSION_STATES = (
    "starting",
    "running",
    "done",
    "failed",
    "cancelled",
    "timeout",
    "orphaned",
)

# States that count as finished: `continue` accepts them, `rm`/`prune` delete
# them, `wait` returns immediately. `orphaned` is finished by definition.
FINISHED_STATES = frozenset({"done", "failed", "cancelled", "timeout", "orphaned"})
ACTIVE_STATES = frozenset({"starting", "running"})

# Fixed exit codes (SPEC.md *Output contract*).
EXIT_OK = 0
EXIT_AGENT_ERROR = 1
EXIT_USAGE = 2
EXIT_BUDGET = 4
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130
EXIT_SIGPIPE = 141
EXIT_SIGTERM = 143
