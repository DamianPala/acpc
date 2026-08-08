"""Tests for the permission classification and policy in acpc.permissions.

Assertions restate the SPEC.md *Permissions* table, tier by tier.
"""

from itertools import pairwise

import pytest
from acp.schema import PermissionOption

from acpc.permissions import (
    PermissionLevel,
    classify_kind,
    find_option,
    minimum_policy,
    should_allow,
)
from acpc.vocab import normalize_permission


class TestClassifyKind:
    @pytest.mark.parametrize("kind", ["read", "search", "fetch", "think"])
    def test_read_kinds(self, kind: str) -> None:
        assert classify_kind(kind) == "read"

    def test_edit_kind(self) -> None:
        assert classify_kind("edit") == "edit"

    @pytest.mark.parametrize("kind", ["execute", "delete", "move"])
    def test_execute_kinds(self, kind: str) -> None:
        assert classify_kind(kind) == "execute"

    @pytest.mark.parametrize("kind", [None, "other", "", "switch_mode"])
    def test_unknown_kinds(self, kind: str | None) -> None:
        assert classify_kind(kind) == "unknown"


class TestShouldAllow:
    def test_all_allows_everything(self) -> None:
        for category in ("read", "edit", "execute", "unknown"):
            assert should_allow(PermissionLevel.ALL, category) is True

    def test_none_denies_everything(self) -> None:
        for category in ("read", "edit", "execute", "unknown"):
            assert should_allow(PermissionLevel.NONE, category) is False

    def test_read_allows_only_read(self) -> None:
        assert should_allow(PermissionLevel.READ, "read") is True
        assert should_allow(PermissionLevel.READ, "edit") is False
        assert should_allow(PermissionLevel.READ, "execute") is False
        assert should_allow(PermissionLevel.READ, "unknown") is False

    def test_edit_allows_read_and_edit_but_not_execute(self) -> None:
        assert should_allow(PermissionLevel.EDIT, "read") is True
        assert should_allow(PermissionLevel.EDIT, "edit") is True
        assert should_allow(PermissionLevel.EDIT, "execute") is False
        assert should_allow(PermissionLevel.EDIT, "unknown") is False

    def test_execute_allows_read_edit_and_execute_but_not_unknown(self) -> None:
        assert should_allow(PermissionLevel.EXECUTE, "read") is True
        assert should_allow(PermissionLevel.EXECUTE, "edit") is True
        assert should_allow(PermissionLevel.EXECUTE, "execute") is True
        assert should_allow(PermissionLevel.EXECUTE, "unknown") is False

    def test_ask_auto_allows_read_and_asks_otherwise(self) -> None:
        assert should_allow(PermissionLevel.ASK, "read") is True
        assert should_allow(PermissionLevel.ASK, "edit") is None
        assert should_allow(PermissionLevel.ASK, "execute") is None
        assert should_allow(PermissionLevel.ASK, "unknown") is None


def test_permission_scale_is_ordered_and_ask_has_no_rank() -> None:
    levels = [
        PermissionLevel.NONE,
        PermissionLevel.READ,
        PermissionLevel.EDIT,
        PermissionLevel.EXECUTE,
        PermissionLevel.ALL,
    ]

    assert [level.rank for level in levels] == list(range(5))
    assert all(left < right for left, right in pairwise(levels))
    with pytest.raises(ValueError, match="not ordered"):
        _ = PermissionLevel.ASK.rank


def test_permission_aliases_normalize_and_construct_levels() -> None:
    assert normalize_permission("write") == "execute"
    assert normalize_permission("prompt") == "ask"
    assert normalize_permission("unsupported") == "unsupported"
    assert PermissionLevel("write") is PermissionLevel.EXECUTE
    assert PermissionLevel("prompt") is PermissionLevel.ASK


class TestMinimumPolicy:
    def test_categories_map_to_least_permissive_policy(self) -> None:
        assert minimum_policy("read") == "read"
        assert minimum_policy("edit") == "edit"
        assert minimum_policy("execute") == "execute"
        assert minimum_policy("unknown") == "all"


def _options() -> list[PermissionOption]:
    return [
        PermissionOption(option_id="always", name="Always", kind="allow_always"),
        PermissionOption(option_id="once", name="Once", kind="allow_once"),
        PermissionOption(option_id="no", name="No", kind="reject_once"),
    ]


class TestFindOption:
    def test_allow_prefers_allow_once(self) -> None:
        assert (
            find_option(_options(), allow=True, permission_level=PermissionLevel.EXECUTE) == "once"
        )

    def test_deny_answers_reject_once(self) -> None:
        assert find_option(_options(), allow=False, permission_level=PermissionLevel.ALL) == "no"

    def test_allow_always_reserved_to_all(self) -> None:
        only_always = [PermissionOption(option_id="always", name="A", kind="allow_always")]
        assert (
            find_option(only_always, allow=True, permission_level=PermissionLevel.EXECUTE) is None
        )
        assert (
            find_option(only_always, allow=True, permission_level=PermissionLevel.ALL) == "always"
        )

    def test_no_matching_option_returns_none(self) -> None:
        assert find_option([], allow=True, permission_level=PermissionLevel.ALL) is None
