"""Permission-kind classification and approval policy.

Implements SPEC.md *Permissions* exactly:

- `read` allows the read-only kinds (`read`, `search`, `fetch`, `think`) and
  `switch_mode` — unless the target mode is on the adapter's bypass list, in
  which case the request is treated like an unknown kind.
- `write` adds `edit` and `execute`; never `delete`/`move`.
- `all` allows everything, including `allow_always` options.
- `none` denies every request.
- `prompt` auto-allows read kinds and asks the human for everything else.

Unknown kinds are denied under `read`/`write`/`none`, asked under `prompt`,
allowed under `all`. `read` and `write` answer with `allow_once` only —
`allow_always` is reserved to `all`.
"""

from enum import Enum

from acp.schema import PermissionOption

READ_KINDS: frozenset[str] = frozenset({"read", "search", "think", "fetch", "switch_mode"})
WRITE_KINDS: frozenset[str] = frozenset({"edit", "execute"})
DELETE_KINDS: frozenset[str] = frozenset({"delete", "move"})


class PermissionLevel(Enum):
    ALL = "all"
    WRITE = "write"
    READ = "read"
    NONE = "none"
    PROMPT = "prompt"


def classify_kind(kind: str | None, *, bypass_mode_switch: bool = False) -> str:
    """Return a permission category for a ToolKind value.

    A `switch_mode` whose target mode is on the adapter's bypass list is a
    permission-evasion door (SPEC.md: "the same door exists at runtime") and
    classifies as unknown — denied below `all`, asked under `prompt`.
    """
    if kind == "switch_mode" and bypass_mode_switch:
        return "unknown"
    if kind in READ_KINDS:
        return "read"
    if kind in WRITE_KINDS:
        return "write"
    if kind in DELETE_KINDS:
        return "delete"
    return "unknown"


def should_allow(level: PermissionLevel, category: str) -> bool | None:
    """Return True (allow), False (deny), or None (ask the human)."""
    if level is PermissionLevel.ALL:
        return True
    if level is PermissionLevel.NONE:
        return False
    if level is PermissionLevel.READ:
        return category == "read"
    if level is PermissionLevel.WRITE:
        return category not in {"delete", "unknown"}
    # PROMPT
    if category == "read":
        return True
    return None


def minimum_policy(category: str) -> str:
    """Return the least non-interactive policy that allows a category."""
    for level in (PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.ALL):
        if should_allow(level, category) is True:
            return level.value
    raise ValueError(f"no policy allows permission category {category!r}")


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
