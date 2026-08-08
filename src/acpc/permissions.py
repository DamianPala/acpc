"""Classify ACP permission requests against the ordered policy scale.

The non-interactive policies are ordered as ``none < read < edit < execute <
all``. ``ask`` is separate from that scale: read requests are allowed
automatically, while every other category is left for the human to answer.
Unknown kinds require ``all``. The deprecated ``write`` and ``prompt`` input
aliases are normalized before a ``PermissionLevel`` is constructed.
"""

from enum import Enum
from functools import total_ordering

from acp.schema import PermissionOption

from acpc.vocab import PERMISSION_ALIASES

READ_KINDS: frozenset[str] = frozenset({"read", "search", "think", "fetch"})
EDIT_KINDS: frozenset[str] = frozenset({"edit"})
EXECUTE_KINDS: frozenset[str] = frozenset({"execute", "delete", "move"})


@total_ordering
class PermissionLevel(Enum):
    NONE = "none"
    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"
    ALL = "all"
    ASK = "ask"

    @classmethod
    def _missing_(cls, value: object) -> "PermissionLevel | None":
        # Compatibility net for pre-0.5 meta: aliases become canonical before downstream code.
        if isinstance(value, str):
            canonical = PERMISSION_ALIASES.get(value)
            if canonical is not None:
                return cls(canonical)
        return None

    @property
    def rank(self) -> int:
        """Return this level's scale rank; ``ask`` has no rank."""
        if self is PermissionLevel.ASK:
            raise ValueError("ask is not ordered with the permission scale")
        return (
            PermissionLevel.NONE,
            PermissionLevel.READ,
            PermissionLevel.EDIT,
            PermissionLevel.EXECUTE,
            PermissionLevel.ALL,
        ).index(self)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank < other.rank


def classify_kind(kind: str | None) -> str:
    """Return the policy category for an ACP tool-call kind."""
    if kind in READ_KINDS:
        return "read"
    if kind in EDIT_KINDS:
        return "edit"
    if kind in EXECUTE_KINDS:
        return "execute"
    return "unknown"


def should_allow(level: PermissionLevel, category: str) -> bool | None:
    """Return True (allow), False (deny), or None (ask the human)."""
    if level is PermissionLevel.ASK:
        return True if category == "read" else None
    if category not in {"read", "edit", "execute"}:
        return level is PermissionLevel.ALL
    required = {
        "read": PermissionLevel.READ,
        "edit": PermissionLevel.EDIT,
        "execute": PermissionLevel.EXECUTE,
    }[category]
    return level >= required


def minimum_policy(category: str) -> str:
    """Return the least non-interactive policy that allows a category."""
    minimums = {
        "read": PermissionLevel.READ.value,
        "edit": PermissionLevel.EDIT.value,
        "execute": PermissionLevel.EXECUTE.value,
        "unknown": PermissionLevel.ALL.value,
    }
    # Unexpected categories are treated like unknown requests and need `all`.
    return minimums.get(category, PermissionLevel.ALL.value)


def find_option(
    options: list[PermissionOption],
    allow: bool,
    permission_level: PermissionLevel,
) -> str | None:
    """Find the option_id answering a request under the policy.

    Allowing prefers `allow_once`; `allow_always` is offered only under `all`.
    Denying always answers `reject_once`.
    """
    target_kind = "allow_once" if allow else "reject_once"
    for opt in options:
        if opt.kind == target_kind:
            return opt.option_id
    if allow and permission_level is PermissionLevel.ALL:
        for opt in options:
            if opt.kind == "allow_always":
                return opt.option_id
    return None
