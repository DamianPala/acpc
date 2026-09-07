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
    "succeeded",
    "failed",
    "canceled",
    "timeout",
    "orphaned",
)

# States that count as finished: `continue` accepts them, `rm`/`prune` delete
# them, `wait` returns immediately. `orphaned` is finished by definition.
FINISHED_STATES = frozenset({"succeeded", "failed", "canceled", "timeout", "orphaned"})
ACTIVE_STATES = frozenset({"starting", "running"})

LEGACY_SESSION_STATES = {"done": "succeeded", "cancelled": "canceled"}


def normalize_session_state(value: str) -> str:
    """Map the two pre-1.0 state spellings to the published vocabulary."""
    return LEGACY_SESSION_STATES.get(value, value)


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
    "124": "Timeout: `run` cancelled the session; `wait` and `log --wait-new` stopped waiting "
    "and left it running.",
    "130": "Cancelled by SIGINT or `acpc stop`. Answer-printing commands mirror the session "
    "result, so `wait` on a cancelled session also exits 130.",
    "141": "SIGPIPE: a downstream reader closed the pipe.",
    "143": "SIGTERM: the client detached from a daemon-owned session, or ended the turn.",
}
