"""Cumulative adapter-reported token usage and context compaction accounting."""

from collections.abc import Mapping, Sequence
from typing import Any, Final

_PROFILES: Final = {
    "claude_model_usage": "_meta.quota.model_usage",
    "codex_usage_updates": "usage_update.used",
    "grok_meta_usage": "_meta.usage",
}
_MODEL_FIELDS: Final = (
    "total_tokens",
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
)
_SPLIT_FIELDS: Final = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
)


def accumulate_turn(
    previous: Mapping[str, Any] | None,
    *,
    used: Sequence[int],
    prompt_response: Mapping[str, Any] | None,
    prompt: str,
    previous_context: Mapping[str, Any] | None,
    profile: str,
    adapter_name: str | None,
    adapter_version: str | None,
    resolved_model: str | None,
    billing: str | None,
    limit_gaps: int = 0,
    missing_response_is_gap: bool = True,
) -> dict[str, Any] | None:
    """Return cumulative usage after one ended turn, or `None` for profile none."""
    if profile == "none":
        return None
    result = _copy_previous(previous, billing)
    drops = _context_drops(used, prompt, previous_context)
    observed = _apply_profile(result, profile, used, prompt_response, drops, resolved_model)
    turn_gap = _turn_has_gap(profile, used, observed)
    if prompt_response is None and not missing_response_is_gap:
        turn_gap = False
    result["gaps"] += limit_gaps + int(turn_gap)
    _add_compactions(result, drops, profile, gap=turn_gap or limit_gaps > 0)
    if previous is None and not observed and not drops and result["gaps"] == 0:
        return None
    result["source"] = _source(adapter_name, adapter_version, profile)
    result["billing"] = billing
    result["quality"] = quality(result)
    return result


def mark_unknown(
    previous: Mapping[str, Any] | None,
    *,
    profile: str,
    adapter_name: str | None,
    adapter_version: str | None,
    billing: str | None,
) -> dict[str, Any] | None:
    """Mark one liveness-detected turn whose adapter outcome was lost."""
    if profile == "none":
        return None
    result = _copy_previous(previous, billing)
    result["gaps"] += 1
    result["source"] = _source(adapter_name, adapter_version, profile)
    result["billing"] = billing
    result["quality"] = quality(result)
    return result


def quality(value: Mapping[str, Any], *, drift_detected: bool = False) -> str:
    """Calculate quality in one place; later checks can add a drift condition."""
    compactions = value["compactions"]
    models = value["models"]
    complete_models = bool(models) and all(
        all(model.get(field) is not None for field in _SPLIT_FIELDS) for model in models.values()
    )
    if (
        value["gaps"] == 0
        and compactions["unaccounted"] == 0
        and complete_models
        and not drift_detected
    ):
        return "exact"
    return "estimate"


def _copy_previous(previous: Mapping[str, Any] | None, billing: str | None) -> dict[str, Any]:
    if previous is None:
        return {
            "quality": "estimate",
            "gaps": 0,
            "calls": None,
            "models": {},
            "compactions": {
                "count": 0,
                "unaccounted": 0,
                "context_before": 0,
                "context_after": 0,
            },
            "source": "",
            "billing": billing,
        }
    return {
        "quality": previous["quality"],
        "gaps": previous["gaps"],
        "calls": previous["calls"],
        "models": {key: dict(item) for key, item in previous["models"].items()},
        "compactions": dict(previous["compactions"]),
        "source": previous["source"],
        "billing": billing,
    }


def _context_drops(
    used: Sequence[int], prompt: str, previous_context: Mapping[str, Any] | None
) -> list[tuple[int, int, int]]:
    if prompt.startswith("/clear"):
        return []
    before = previous_context.get("used") if previous_context is not None else None
    drops: list[tuple[int, int, int]] = []
    for index, after in enumerate(used):
        # A live context is never empty: a zero reading (seen on notifications that
        # carry only rate-limit metadata) says nothing about occupancy.
        if after <= 0:
            continue
        if (
            isinstance(before, int)
            and before > 0
            and before - after >= 0
            and (before - after) * 100 >= before * 5
        ):
            drops.append((index, before, after))
        before = after
    return drops


def _add_compactions(
    result: dict[str, Any],
    drops: Sequence[tuple[int, int, int]],
    profile: str,
    *,
    gap: bool,
) -> None:
    compactions = result["compactions"]
    compactions["count"] += len(drops)
    if profile in {"codex_usage_updates", "grok_meta_usage"} or (
        profile == "claude_model_usage" and gap
    ):
        compactions["unaccounted"] += len(drops)
    compactions["context_before"] += sum(before for _, before, _ in drops)
    compactions["context_after"] += sum(after for _, _, after in drops)


def _apply_profile(
    result: dict[str, Any],
    profile: str,
    used: Sequence[int],
    response: Mapping[str, Any] | None,
    drops: Sequence[tuple[int, int, int]],
    resolved_model: str | None,
) -> bool:
    if profile == "claude_model_usage":
        return _apply_claude(result, response)
    if profile == "codex_usage_updates":
        return _apply_codex(result, used, response, drops, resolved_model)
    if profile == "grok_meta_usage":
        return _apply_grok(result, response, resolved_model)
    return False


def _apply_claude(result: dict[str, Any], response: Mapping[str, Any] | None) -> bool:
    if response is None or response.get("usage") is None:
        return False
    quota = _nested_mapping(response, "_meta", "quota")
    entries = quota.get("model_usage")
    if not isinstance(entries, list):
        # Usage without the per-model tally leaves this turn out of `models`.
        return False
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("model"), str):
            continue
        token_count = entry.get("token_count")
        if isinstance(token_count, Mapping):
            _add_model(result, entry["model"], _claude_fields(token_count))
    return True


def _apply_codex(
    result: dict[str, Any],
    used: Sequence[int],
    response: Mapping[str, Any] | None,
    drops: Sequence[tuple[int, int, int]],
    resolved_model: str | None,
) -> bool:
    if not used:
        return False
    dropped = {index for index, _, _ in drops}
    estimate_total = _codex_zero_category_total(response)
    if estimate_total is not None:
        matching = next(
            (index for index in range(len(used) - 1, -1, -1) if used[index] == estimate_total),
            None,
        )
        if matching is not None:
            dropped.add(matching)
    counted = [value for index, value in enumerate(used) if index not in dropped]
    result["calls"] = (result["calls"] or 0) + len(counted)
    if counted:
        model = _codex_model(response) or resolved_model or "unknown"
        _add_model(result, model, _empty_fields(total_tokens=sum(counted)))
    return True


def _codex_zero_category_total(response: Mapping[str, Any] | None) -> int | None:
    candidates = (
        _nested_mapping(response, "usage"),
        _nested_mapping(response, "_meta", "quota", "token_count"),
    )
    category_names = (
        ("inputTokens", "input_tokens"),
        ("cachedReadTokens", "cachedInputTokens", "cache_read_tokens"),
        ("outputTokens", "output_tokens"),
    )
    for counts in candidates:
        total = _first_int(counts, "totalTokens", "total_tokens")
        categories = [_first_int(counts, *names) for names in category_names]
        if total is not None and total > 0 and all(value == 0 for value in categories):
            return total
    return None


def _apply_grok(
    result: dict[str, Any], response: Mapping[str, Any] | None, resolved_model: str | None
) -> bool:
    raw = _nested_mapping(response, "_meta", "usage") if response is not None else {}
    if not raw or not _has_grok_numbers(raw):
        return False
    calls = _integer(raw.get("modelCalls"))
    if calls is not None:
        result["calls"] = (result["calls"] or 0) + calls
    model_usage = raw.get("modelUsage")
    deltas = _grok_models(model_usage) if isinstance(model_usage, Mapping) else {}
    if not deltas:
        model = resolved_model or "unknown"
        deltas = {model: _grok_fields(raw)}
    for model, values in deltas.items():
        _add_model(result, model, values)
    return True


def _turn_has_gap(profile: str, used: Sequence[int], observed: bool) -> bool:
    if profile in {"claude_model_usage", "grok_meta_usage"}:
        return not observed
    return not used and not observed


def _claude_fields(token_count: Mapping[str, Any]) -> dict[str, int | None]:
    return _empty_fields(
        total_tokens=_first_int(token_count, "totalTokens", "total_tokens"),
        input_tokens=_first_int(token_count, "inputTokens", "input_tokens"),
        cache_read_tokens=_first_int(token_count, "cachedInputTokens", "cachedReadTokens"),
        cache_write_tokens=_first_int(token_count, "cachedWriteTokens", "cacheCreationTokens"),
        output_tokens=_first_int(token_count, "outputTokens", "output_tokens"),
    )


def _grok_models(raw: Mapping[str, Any]) -> dict[str, dict[str, int | None]]:
    result: dict[str, dict[str, int | None]] = {}
    for model, values in raw.items():
        if isinstance(model, str) and isinstance(values, Mapping) and _has_grok_numbers(values):
            result[model] = _grok_fields(values)
    return result


def _grok_fields(raw: Mapping[str, Any]) -> dict[str, int | None]:
    cached = _first_int(raw, "cachedReadTokens", "cached_read_tokens")
    input_tokens = _first_int(raw, "inputTokens", "input_tokens")
    uncached = input_tokens - cached if input_tokens is not None and cached is not None else None
    return _empty_fields(
        total_tokens=_first_int(raw, "totalTokens", "total_tokens"),
        input_tokens=uncached,
        cache_read_tokens=cached,
        cache_write_tokens=_first_int(raw, "cacheCreationTokens", "cache_write_tokens"),
        output_tokens=_first_int(raw, "outputTokens", "output_tokens"),
    )


def _has_grok_numbers(raw: Mapping[str, Any]) -> bool:
    names = (
        "totalTokens",
        "total_tokens",
        "inputTokens",
        "cachedReadTokens",
        "cacheCreationTokens",
        "outputTokens",
        "modelCalls",
    )
    return any(_integer(raw.get(name)) is not None for name in names)


def _codex_model(response: Mapping[str, Any] | None) -> str | None:
    quota = _nested_mapping(response, "_meta", "quota")
    entries = quota.get("model_usage")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, Mapping) and isinstance(entry.get("model"), str):
                return entry["model"]
    return None


def _nested_mapping(value: Mapping[str, Any] | None, *keys: str) -> Mapping[str, Any]:
    current: Any = value
    for key in keys:
        current = current.get(key) if isinstance(current, Mapping) else None
    return current if isinstance(current, Mapping) else {}


def _first_int(value: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        number = _integer(value.get(key))
        if number is not None:
            return number
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _empty_fields(**values: int | None) -> dict[str, int | None]:
    return {field: values.get(field) for field in _MODEL_FIELDS}


def _add_model(result: dict[str, Any], model: str, delta: Mapping[str, int | None]) -> None:
    models = result["models"]
    if model not in models:
        models[model] = dict(delta)
        return
    current = models[model]
    for field in _MODEL_FIELDS:
        old, new = current[field], delta[field]
        current[field] = old + new if old is not None and new is not None else None


def _source(adapter_name: str | None, version: str | None, profile: str) -> str:
    field = _PROFILES[profile]
    parts = [adapter_name or "unknown"]
    if version:
        parts.append(version)
    parts.append(field)
    return " ".join(parts)
