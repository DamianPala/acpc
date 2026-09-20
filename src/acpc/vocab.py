"""Closed vocabularies shared across the CLI, registry, and session state.

SPEC.md defines each of these as a single vocabulary used verbatim everywhere
it appears (flags, `status` output, `log` footers, `meta.json`). Keeping them
in one dependency-free module lets every layer import them without cycles.
"""

# Superset reasoning-effort scale, mapped per adapter (SPEC.md `run --effort`).
EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# Approval policies for ACP permission requests (SPEC.md `run --permissions`).
PERMISSION_VALUES = ("none", "read", "edit", "execute", "all", "ask")
PERMISSION_ALIASES = {"write": "execute", "prompt": "ask"}


def normalize_permission(value: str | None) -> str | None:
    """Return a canonical permission value, preserving unknown values."""
    if value is None:
        return None
    return PERMISSION_ALIASES.get(value, value)


# Session states (SPEC.md *Session states*): one vocabulary, used verbatim.
SESSION_STATES = (
    "starting",
    "running",
    "preparing",
    "waiting",
    "succeeded",
    "failed",
    "canceled",
    "unknown",
)

# States that count as finished: `continue` accepts them, `delete`/`prune` delete
# them, and `wait` returns immediately. `unknown` means liveness was observed
# to be lost before acpc could observe the operation's terminal result.
FINISHED_STATES = frozenset({"succeeded", "failed", "canceled", "unknown"})
# `waiting` is a turn holding for a usage limit acpc will resume by itself
# (SPEC.md *Session states*): active for liveness, listing and `cancel`.
ACTIVE_STATES = frozenset({"starting", "running", "preparing", "waiting"})

LEGACY_SESSION_STATES = {
    "done": "succeeded",
    "cancelled": "canceled",
    "orphaned": "unknown",
    "timeout": "failed",
}


def normalize_session_state(value: str) -> str:
    """Map pre-1.0 state spellings to the published vocabulary."""
    return LEGACY_SESSION_STATES.get(value, value)


# Steering modes (SPEC.md `steer`), in preference order. `in-place` needs the
# adapter's `_session/steering` extension; `cancel-then-start` needs only
# `session/cancel` and a fresh `session/prompt`, so it is always available.
STEER_IN_PLACE = "in-place"
STEER_CANCEL_THEN_START = "cancel-then-start"
STEER_MODES = (STEER_IN_PLACE, STEER_CANCEL_THEN_START)

# `--on-limit` (SPEC.md `run`): what to do when a usage limit blocks a turn.
ON_LIMIT_WAIT = "wait"
ON_LIMIT_FAIL = "fail"
ON_LIMIT = (ON_LIMIT_WAIT, ON_LIMIT_FAIL)

# Largest prompt acpc buffers from any single source: the prompt argument, `-`
# on stdin, or `--prompt-file`.  Counted in UTF-8 bytes and enforced before a
# session directory exists, so an oversized call leaves nothing behind.  Far
# above a real prompt and far below anything that would burden a session dir.
#
# Changing either value means editing `run`'s docstring too: Click renders a
# docstring verbatim, so that one help text spells the number out instead of
# reading it from here, and D1 asks the two to agree.
MAX_PROMPT_BYTES = 1_048_576
MAX_PROMPT_LABEL = "1 MiB"

# Fixed exit codes (SPEC.md *Output contract*).
EXIT_OK = 0
EXIT_AGENT_ERROR = 1
EXIT_USAGE = 2
EXIT_BUDGET = 4
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130
EXIT_SIGPIPE = 141
EXIT_SIGTERM = 143

# One stable meaning per code, keyed by its decimal spelling so the table can
# be published verbatim.  Finer-grained failure detail is the error envelope's
# `kind`, never a new exit code.
EXIT_DESCRIPTIONS: dict[str, str] = {
    "0": "Success, including an empty result: the turn ended normally, or the view rendered.",
    "1": "Generic failure: the agent errored — a crash, a refusal, exhausted context, "
    "missing auth — or the command could not do what was asked.",
    "2": "Usage error: the call cannot be accepted in this form — bad flags, a mode that "
    "exceeds the policy, or a policy no declared mode satisfies.",
    "4": "Output budget exhausted: `log --follow` stopped because `--max-output` ran out "
    "before the session ended; the footer's cursor covers what was printed.",
    "124": "Timeout: `run --timeout` stopped waiting and left the session running; `wait` "
    "and `log --wait-new` do the same.",
    "130": "Cancelled by SIGINT or `acpc cancel`. Answer-printing commands mirror the session "
    "result, so `wait` on a cancelled session also exits 130.",
    "141": "SIGPIPE: a downstream reader closed the pipe.",
    "143": "SIGTERM: the client detached from a daemon-owned session, or ended the turn.",
}
