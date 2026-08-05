"""Tests for the permission classification and policy in acpc.permissions.

Assertions restate the SPEC.md *Permissions* table, tier by tier.
"""

import pytest
from acp.schema import PermissionOption

from acpc.permissions import (
    PermissionLevel,
    classify_kind,
    find_option,
    minimum_policy,
    should_allow,
)


class TestClassifyKind:
    @pytest.mark.parametrize("kind", ["read", "search", "fetch", "think", "switch_mode"])
    def test_read_kinds(self, kind: str) -> None:
        assert classify_kind(kind) == "read"

    @pytest.mark.parametrize("kind", ["edit", "execute"])
    def test_write_kinds(self, kind: str) -> None:
        assert classify_kind(kind) == "write"

    @pytest.mark.parametrize("kind", ["delete", "move"])
    def test_delete_kinds(self, kind: str) -> None:
        assert classify_kind(kind) == "delete"

    @pytest.mark.parametrize("kind", [None, "other", ""])
    def test_unknown_kinds(self, kind: str | None) -> None:
        assert classify_kind(kind) == "unknown"

    def test_switch_into_bypass_mode_is_unknown(self) -> None:
        assert classify_kind("switch_mode", bypass_mode_switch=True) == "unknown"

    def test_bypass_flag_leaves_other_kinds_alone(self) -> None:
        assert classify_kind("read", bypass_mode_switch=True) == "read"


class TestShouldAllow:
    def test_all_allows_everything(self) -> None:
        for category in ("read", "write", "delete", "unknown"):
            assert should_allow(PermissionLevel.ALL, category) is True

    def test_none_denies_everything(self) -> None:
        for category in ("read", "write", "delete", "unknown"):
            assert should_allow(PermissionLevel.NONE, category) is False

    def test_read_allows_only_read(self) -> None:
        assert should_allow(PermissionLevel.READ, "read") is True
        assert should_allow(PermissionLevel.READ, "write") is False
        assert should_allow(PermissionLevel.READ, "delete") is False
        assert should_allow(PermissionLevel.READ, "unknown") is False

    def test_write_allows_read_and_write_never_delete(self) -> None:
        assert should_allow(PermissionLevel.WRITE, "read") is True
        assert should_allow(PermissionLevel.WRITE, "write") is True
        assert should_allow(PermissionLevel.WRITE, "delete") is False
        assert should_allow(PermissionLevel.WRITE, "unknown") is False

    def test_prompt_auto_allows_read_and_asks_otherwise(self) -> None:
        assert should_allow(PermissionLevel.PROMPT, "read") is True
        assert should_allow(PermissionLevel.PROMPT, "write") is None
        assert should_allow(PermissionLevel.PROMPT, "delete") is None
        assert should_allow(PermissionLevel.PROMPT, "unknown") is None


class TestMinimumPolicy:
    def test_categories_map_to_least_permissive_policy(self) -> None:
        assert minimum_policy("read") == "read"
        assert minimum_policy("write") == "write"
        assert minimum_policy("delete") == "all"
        assert minimum_policy("unknown") == "all"


def _options() -> list[PermissionOption]:
    return [
        PermissionOption(option_id="always", name="Always", kind="allow_always"),
        PermissionOption(option_id="once", name="Once", kind="allow_once"),
        PermissionOption(option_id="no", name="No", kind="reject_once"),
    ]


class TestFindOption:
    def test_allow_prefers_allow_once(self) -> None:
        assert find_option(_options(), allow=True, permission_level=PermissionLevel.WRITE) == "once"

    def test_deny_answers_reject_once(self) -> None:
        assert find_option(_options(), allow=False, permission_level=PermissionLevel.ALL) == "no"

    def test_allow_always_reserved_to_all(self) -> None:
        only_always = [PermissionOption(option_id="always", name="A", kind="allow_always")]
        assert find_option(only_always, allow=True, permission_level=PermissionLevel.WRITE) is None
        assert (
            find_option(only_always, allow=True, permission_level=PermissionLevel.ALL) == "always"
        )

    def test_no_matching_option_returns_none(self) -> None:
        assert find_option([], allow=True, permission_level=PermissionLevel.ALL) is None
