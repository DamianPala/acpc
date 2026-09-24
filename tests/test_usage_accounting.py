"""Behavioral tests for cumulative adapter-reported consumption."""

from __future__ import annotations

from typing import Any

import pytest

from acpc import usage


def update(
    profile: str,
    *,
    previous: dict[str, Any] | None = None,
    used: tuple[int, ...] = (),
    response: dict[str, Any] | None = None,
    prompt: str = "work",
    previous_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return usage.accumulate_turn(
        previous,
        used=used,
        prompt_response=response,
        prompt=prompt,
        previous_context=previous_context,
        profile=profile,
        adapter_name="mock-acp",
        adapter_version="1.2.3",
        resolved_model="resolved-model",
        billing=None,
    )


def claude_response(*entries: tuple[str, dict[str, int]]) -> dict[str, Any]:
    return {
        "usage": {"totalTokens": 1},
        "_meta": {
            "quota": {
                "model_usage": [
                    {"model": model, "token_count": counts} for model, counts in entries
                ]
            }
        },
    }


def claude_counts(total: int) -> dict[str, int]:
    return {
        "totalTokens": total,
        "inputTokens": total - 30,
        "cachedInputTokens": 20,
        "cachedWriteTokens": 10,
        "outputTokens": 30,
    }


def test_claude_multicall_turns_accumulate_per_verbatim_model_key() -> None:
    first = update(
        "claude_model_usage",
        response=claude_response(("claude-opus-5[1m]", claude_counts(100))),
        used=(1000, 1200, 1400),
    )
    second = update(
        "claude_model_usage",
        previous=first,
        response=claude_response(
            ("claude-opus-5[1m]", claude_counts(200)),
            ("claude-haiku-4-5-20251001", claude_counts(50)),
        ),
        used=(1500, 1700),
        previous_context={"used": 1400},
    )

    assert second is not None
    assert second["models"] == {
        "claude-opus-5[1m]": {
            "total_tokens": 300,
            "input_tokens": 240,
            "cache_read_tokens": 40,
            "cache_write_tokens": 20,
            "output_tokens": 60,
        },
        "claude-haiku-4-5-20251001": {
            "total_tokens": 50,
            "input_tokens": 20,
            "cache_read_tokens": 20,
            "cache_write_tokens": 10,
            "output_tokens": 30,
        },
    }
    assert second["calls"] is None
    assert second["quality"] == "exact"


def test_claude_failed_turn_is_a_permanent_gap() -> None:
    failed = update("claude_model_usage", response=None, used=(300,))
    recovered = update(
        "claude_model_usage",
        previous=failed,
        response=claude_response(("claude-opus-5", claude_counts(100))),
        used=(500,),
        previous_context={"used": 300},
    )

    assert failed is not None and failed["gaps"] == 1
    assert recovered is not None
    assert recovered["gaps"] == 1
    assert recovered["quality"] == "estimate"
    assert recovered["models"]["claude-opus-5"]["total_tokens"] == 100


@pytest.mark.parametrize(
    "response",
    [{"usage": None, "_meta": {}}, {"usage": {"totalTokens": 5}, "_meta": {}}],
    ids=["cancelled-without-usage", "usage-without-model-tally"],
)
def test_claude_turn_without_model_tally_is_a_gap(response: dict[str, Any]) -> None:
    clean = update("claude_model_usage", response=claude_response(("m", claude_counts(10))))
    result = update("claude_model_usage", previous=clean, response=response)

    assert result is not None
    assert result["gaps"] == 1
    assert result["quality"] == "estimate"


def test_claude_compaction_stays_accounted_and_exact() -> None:
    result = update(
        "claude_model_usage",
        response=claude_response(("m", claude_counts(10))),
        used=(4018,),
        previous_context={"used": 33931},
        prompt="/compact",
    )

    assert result is not None
    assert result["compactions"] == {
        "count": 1,
        "unaccounted": 0,
        "context_before": 33931,
        "context_after": 4018,
    }
    assert result["quality"] == "exact"


def test_claude_compaction_in_a_gap_turn_is_unaccounted() -> None:
    result = update(
        "claude_model_usage",
        response=None,
        used=(4018,),
        previous_context={"used": 33931},
    )

    assert result is not None
    assert result["gaps"] == 1
    assert result["compactions"] == {
        "count": 1,
        "unaccounted": 1,
        "context_before": 33931,
        "context_after": 4018,
    }
    assert result["quality"] == "estimate"


def test_unknown_split_field_stays_null_after_later_known_figures() -> None:
    partial = update("grok_meta_usage", response={"_meta": {"usage": {"totalTokens": 10}}})
    later = update(
        "grok_meta_usage",
        previous=partial,
        response={"_meta": {"usage": {"totalTokens": 5, "inputTokens": 4, "cachedReadTokens": 1}}},
    )

    assert later is not None
    model = later["models"]["resolved-model"]
    assert model["total_tokens"] == 15
    assert model["input_tokens"] is None
    assert model["cache_read_tokens"] is None


def test_codex_auto_compactions_count_only_nine_call_updates() -> None:
    used = (
        21429,
        31373,
        15232,
        21746,
        31771,
        15177,
        21726,
        31704,
        15141,
        21682,
        31657,
        15048,
        21554,
    )
    result = update("codex_usage_updates", used=used)

    assert result is not None
    model = result["models"]["resolved-model"]
    assert model["total_tokens"] == 234642
    assert model["input_tokens"] is None
    assert result["calls"] == 9
    assert result["compactions"] == {
        "count": 4,
        "unaccounted": 4,
        "context_before": 126505,
        "context_after": 60598,
    }
    assert result["quality"] == "estimate"


def test_codex_compaction_zero_category_response_adds_no_tokens() -> None:
    response = {
        "usage": {"totalTokens": 4998, "inputTokens": 0, "outputTokens": 0},
        "_meta": {
            "quota": {
                "token_count": {
                    "totalTokens": 4998,
                    "inputTokens": 0,
                    "cachedInputTokens": 0,
                    "outputTokens": 0,
                    "reasoningOutputTokens": 0,
                },
                "model_usage": [
                    {
                        "model": "gpt-6-sol",
                        "token_count": {
                            "totalTokens": 4998,
                            "inputTokens": 0,
                            "cachedInputTokens": 0,
                            "outputTokens": 0,
                        },
                    }
                ],
            }
        },
    }
    result = update(
        "codex_usage_updates",
        used=(4998,),
        response=response,
        previous_context={"used": 5000},
        prompt="/compact",
    )

    assert result is not None
    assert result["models"] == {}
    assert result["calls"] == 0
    assert result["compactions"]["count"] == 0


def test_grok_uses_meta_usage_uncaches_input_and_preserves_model_id() -> None:
    result = update(
        "grok_meta_usage",
        response={
            "usage": None,
            "_meta": {
                "inputTokens": 25172,
                "usage": {
                    "inputTokens": 99736,
                    "outputTokens": 414,
                    "totalTokens": 100150,
                    "cachedReadTokens": 76032,
                    "cacheCreationTokens": 0,
                    "modelCalls": 4,
                    "modelUsage": {
                        "grok-4.7-build": {
                            "inputTokens": 99736,
                            "outputTokens": 414,
                            "totalTokens": 100150,
                            "cachedReadTokens": 76032,
                            "cacheCreationTokens": 0,
                            "modelCalls": 4,
                        }
                    },
                },
            },
        },
    )

    assert result is not None
    assert result["models"]["grok-4.7-build"] == {
        "total_tokens": 100150,
        "input_tokens": 23704,
        "cache_read_tokens": 76032,
        "cache_write_tokens": 0,
        "output_tokens": 414,
    }
    assert result["calls"] == 4
    assert result["quality"] == "exact"


def test_grok_compact_turn_without_meta_usage_increments_gap() -> None:
    previous = update(
        "grok_meta_usage",
        response={"_meta": {"usage": {"inputTokens": 10, "cachedReadTokens": 0}}},
    )
    compact = update(
        "grok_meta_usage",
        previous=previous,
        response={"_meta": {"totalTokens": 0, "inputTokens": 24943}},
        prompt="/compact",
    )

    assert compact is not None
    assert compact["gaps"] == 1
    assert compact["quality"] == "estimate"
    assert compact["models"]["resolved-model"]["input_tokens"] == 10


def test_a_zero_context_reading_is_not_a_compaction() -> None:
    result = update(
        "claude_model_usage",
        response=claude_response(("m", claude_counts(10))),
        used=(27093, 0, 27500),
        previous_context={"used": 26894},
    )

    assert result is not None
    assert result["compactions"]["count"] == 0


@pytest.mark.parametrize(
    ("prompt", "previous_context", "used", "expected"),
    [
        ("/clear", {"used": 1000}, (100,), 0),
        ("work", {"used": 1000}, (960,), 0),
        ("work", {"used": 1000}, (950,), 1),
    ],
)
def test_compaction_threshold_and_clear_exception(
    prompt: str, previous_context: dict[str, Any], used: tuple[int, ...], expected: int
) -> None:
    result = update(
        "codex_usage_updates",
        used=used,
        prompt=prompt,
        previous_context=previous_context,
    )

    assert result is not None
    assert result["compactions"]["count"] == expected


def test_none_profile_stays_null() -> None:
    assert (
        update(
            "none",
            used=(1000, 100),
            response=claude_response(("model", claude_counts(10))),
            previous_context={"used": 1000},
        )
        is None
    )


def test_quality_is_exact_only_for_complete_models_without_gaps_or_unaccounted_compactions() -> (
    None
):
    clean = update("claude_model_usage", response=claude_response(("model", claude_counts(10))))
    assert clean is not None and clean["quality"] == "exact"
    assert usage.quality(clean, drift_detected=True) == "estimate"

    gap = update("claude_model_usage", response=None)
    assert gap is not None and gap["quality"] == "estimate"

    compact = update("codex_usage_updates", used=(1000, 500), previous_context={"used": 1000})
    assert compact is not None and compact["quality"] == "estimate"

    partial = update("grok_meta_usage", response={"_meta": {"usage": {"totalTokens": 10}}})
    assert partial is not None and partial["quality"] == "estimate"


def _codex_response(total: int) -> dict[str, Any]:
    return {"usage": {"totalTokens": total, "inputTokens": total - 1, "outputTokens": 1}}


def _claude_response(response_total: int, model_total: int) -> dict[str, Any]:
    return {
        "usage": {"totalTokens": response_total},
        "_meta": {
            "quota": {
                "model_usage": [{"model": "claude-opus", "token_count": claude_counts(model_total)}]
            }
        },
    }


def test_codex_turn_total_distinguishes_last_turn_and_cumulative_reports() -> None:
    previous = update("codex_usage_updates", used=(500,))
    used = (10, 20, 30, 40)
    sizes = (200_000,) * len(used)

    assert (
        usage.check_turn_invariants(
            previous=previous,
            used=used,
            sizes=sizes,
            prompt_response=_codex_response(40),
            profile="codex_usage_updates",
        )
        is None
    )
    assert usage.check_turn_invariants(
        previous=previous,
        used=used,
        sizes=sizes,
        prompt_response=_codex_response(100),
        profile="codex_usage_updates",
    ) == {"check": "codex_usage_updates", "declared": "last", "observed": "turn"}
    assert usage.check_turn_invariants(
        previous=previous,
        used=used,
        sizes=sizes,
        prompt_response=_codex_response(600),
        profile="codex_usage_updates",
    ) == {"check": "codex_usage_updates", "declared": "last", "observed": "cumulative"}
    assert usage.check_turn_invariants(
        previous=previous,
        used=used,
        sizes=sizes,
        prompt_response=_codex_response(777),
        profile="codex_usage_updates",
    ) == {"check": "codex_usage_updates", "declared": "last", "observed": "unknown"}


def test_codex_single_call_has_no_verdict_and_zero_category_is_ignored() -> None:
    single = usage.check_turn_invariants(
        previous=None,
        used=(40,),
        sizes=(200_000,),
        prompt_response=_codex_response(100),
        profile="codex_usage_updates",
    )
    zero_category = usage.check_turn_invariants(
        previous=None,
        used=(10, 20, 30, 40),
        sizes=(200_000,) * 4,
        prompt_response={
            "usage": {
                "totalTokens": 4998,
                "inputTokens": 0,
                "cachedReadTokens": 0,
                "outputTokens": 0,
            }
        },
        profile="codex_usage_updates",
    )

    assert single is None
    assert zero_category is None


def test_codex_check_uses_the_accumulator_count_after_compactions() -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=(100, 200, 50, 70),
        sizes=(200_000,) * 4,
        prompt_response=_codex_response(270),
        profile="codex_usage_updates",
        previous_context={"used": 200},
    )

    assert observation == {"check": "codex_usage_updates", "declared": "last", "observed": "turn"}


def test_claude_response_total_cannot_exceed_this_turn_model_usage() -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=(),
        sizes=(),
        prompt_response=_claude_response(response_total=120, model_total=100),
        profile="claude_model_usage",
    )

    assert observation == {
        "check": "response_le_model_usage",
        "declared": "turn",
        "observed": "cumulative",
    }


def test_claude_cold_restore_detects_history_delta() -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=(200, 200),
        sizes=(200_000, 200_000),
        prompt_response=_claude_response(response_total=100, model_total=1000),
        profile="claude_model_usage",
        cold_resume=True,
    )

    assert observation == {
        "check": "restore_delta",
        "declared": "turn",
        "observed": "cumulative",
    }


@pytest.mark.parametrize(
    ("prompt", "previous_context", "used", "response_total", "model_total"),
    [
        ("/compact", {"used": 33_931}, (4_018,), 0, 39_430),
        ("work", {"used": 42_201}, (26_967, 4_710), 26_967, 90_056),
    ],
    ids=["compact-1-turn-2", "auto-claude-turn-2"],
)
def test_claude_cold_restore_with_context_drop_has_no_restore_verdict(
    prompt: str,
    previous_context: dict[str, int],
    used: tuple[int, ...],
    response_total: int,
    model_total: int,
) -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=used,
        sizes=(200_000,) * len(used),
        prompt_response=_claude_response(response_total, model_total),
        profile="claude_model_usage",
        cold_resume=True,
        prompt=prompt,
        previous_context=previous_context,
    )

    assert observation is None


def test_restore_and_size_checks_do_not_drift_at_their_boundaries() -> None:
    restore_boundary = usage.check_turn_invariants(
        previous=None,
        used=(200,),
        sizes=(200_000,),
        prompt_response=_claude_response(response_total=100, model_total=300),
        profile="claude_model_usage",
        cold_resume=True,
    )
    size_boundary = usage.check_turn_invariants(
        previous=None,
        used=(200,),
        sizes=(200,),
        prompt_response=None,
        profile="claude_model_usage",
    )

    assert restore_boundary is None
    assert size_boundary is None


@pytest.mark.parametrize(
    "profile", ["claude_model_usage", "codex_usage_updates", "grok_meta_usage"]
)
def test_usage_update_cannot_report_more_than_its_context_size(profile: str) -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=(201,),
        sizes=(200,),
        prompt_response=None,
        profile=profile,
    )

    assert observation == {
        "check": "used_le_size",
        "declared": {
            "claude_model_usage": "turn",
            "codex_usage_updates": "last",
            "grok_meta_usage": "turn",
        }[profile],
        "observed": "cumulative",
    }
    unknown_size = usage.check_turn_invariants(
        previous=None,
        used=(201,),
        sizes=(None,),
        prompt_response=None,
        profile=profile,
    )
    assert unknown_size is None


def test_usage_size_check_uses_the_largest_window_reported_during_the_turn() -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=(250_000, 250_000),
        sizes=(200_000, 1_000_000),
        prompt_response=None,
        profile="claude_model_usage",
    )

    assert observation is None


def test_usage_size_check_uses_the_previous_context_window_after_cold_resume() -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=(250_000,),
        sizes=(200_000,),
        prompt_response=None,
        profile="claude_model_usage",
        cold_resume=True,
        previous_context={"used": 250_000, "size": 1_000_000},
    )

    assert observation is None


def test_usage_above_every_known_window_still_drifts() -> None:
    observation = usage.check_turn_invariants(
        previous=None,
        used=(1_000_001,),
        sizes=(200_000,),
        prompt_response=None,
        profile="claude_model_usage",
        previous_context={"used": 900_000, "size": 1_000_000},
    )

    assert observation == {
        "check": "used_le_size",
        "declared": "turn",
        "observed": "cumulative",
    }


def test_first_drift_survives_later_drift_and_clean_turns() -> None:
    drifted = usage.accumulate_turn(
        None,
        used=(),
        prompt_response=_claude_response(response_total=120, model_total=100),
        prompt="work",
        previous_context=None,
        profile="claude_model_usage",
        adapter_name="mock-agent",
        adapter_version="0.1.0",
        resolved_model="claude-opus",
        billing=None,
        turn_number=1,
    )
    second_drift = usage.accumulate_turn(
        drifted,
        used=(),
        prompt_response=_claude_response(response_total=100, model_total=1000),
        prompt="work",
        previous_context=None,
        profile="claude_model_usage",
        adapter_name="mock-agent",
        adapter_version="0.1.0",
        resolved_model="claude-opus",
        billing=None,
        turn_number=2,
        cold_resume=True,
    )
    clean = usage.accumulate_turn(
        second_drift,
        used=(),
        prompt_response=_claude_response(response_total=100, model_total=100),
        prompt="work",
        previous_context=None,
        profile="claude_model_usage",
        adapter_name="mock-agent",
        adapter_version="0.1.0",
        resolved_model="claude-opus",
        billing=None,
        turn_number=3,
    )

    assert drifted is not None and second_drift is not None and clean is not None
    assert drifted["drift"] == {
        "check": "response_le_model_usage",
        "declared": "turn",
        "observed": "cumulative",
        "adapter": "mock-agent",
        "version": "0.1.0",
        "turn": 1,
    }
    assert second_drift["drift"] == drifted["drift"] == clean["drift"]
    assert clean["quality"] == "estimate"
