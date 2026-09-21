"""Recognizing and parsing a vendor usage limit from an ACP failure.

SPEC.md `run`: a limit that blocks `session/prompt` is recognized from the
adapter's JSON-RPC error (`data.errorKind`), from a preceding `usage_update`'s
`_meta["_claude/rateLimit"]`, or from the vendor's own text. Only
claude-agent-acp publishes anything structural; codex-acp's limits report
none of these signals and stay plain failures.

This module is pure: it takes what the runner already observed (the raised
error, the last-seen rate-limit metadata, and the current time) and returns a
classification, with no I/O and no knowledge of sessions, turns or the clock.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# claude-agent-acp's rate-limit message builder uses these prefixes for a
# limit that carries its own reset time. The rest ("You're out of usage
# credits", "Your org is out of usage ...", "Your seat type doesn't include
# usage ...") are account-level and never resolve to a return time acpc could
# wait for, so they are deliberately not matched here: an error with none of
# the three recognition signals below falls through to a plain failure, same
# as before this module existed.
_TEMPORARY_LIMIT_PREFIXES = ("You've hit your", "You've reached your")

_INTERNAL_ERROR_PREFIX = "Internal error: "

# Matches the vendor's "resets <time> (<zone>)" clause, optionally preceded by
# a month/day: "resets 11:10pm (Europe/Warsaw)", "resets 9am (America/New_York)",
# "resets Sep 24 at 11am (Europe/Warsaw)", "resets Sep 24, 11am (Europe/Warsaw)".
_RESETS_RE = re.compile(
    r"resets\s+"
    r"(?:(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})(?:\s+at|,)\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"\s*\((?P<zone>[^)]+)\)",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True, slots=True)
class LimitObservation:
    """One recognized usage limit, ready for the runner to act on.

    ``resume_at`` is ``None`` when a return time was never observed —
    unparseable text, or a structural signal with no time attached — which
    the runner treats as "cannot wait for this".
    """

    reason: str
    resume_at: datetime | None
    source: str
    detail: str


def _strip_internal_prefix(message: str) -> str:
    if message.startswith(_INTERNAL_ERROR_PREFIX):
        return message[len(_INTERNAL_ERROR_PREFIX) :]
    return message


def _is_temporary_limit_text(text: str) -> bool:
    return text.startswith(_TEMPORARY_LIMIT_PREFIXES)


def _parse_resets_clause(text: str, *, now: datetime) -> datetime | None:
    """Parse the vendor's "resets <time> (<zone>)" clause to an absolute instant.

    A bare time (no month/day) means "the next occurrence of this wall-clock
    time at or after `now`"; a month/day means that calendar date this year,
    or next year if it has already passed.
    """
    match = _RESETS_RE.search(text)
    if match is None:
        return None
    try:
        zone = ZoneInfo(match.group("zone").strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None

    hour = int(match.group("hour"))
    if not (1 <= hour <= 12):
        return None
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm").lower()
    hour = hour % 12
    if ampm == "pm":
        hour += 12

    now_local = now.astimezone(zone)
    month_name = match.group("month")
    day_text = match.group("day")
    if month_name is not None and day_text is not None:
        month = _MONTHS.get(month_name.lower()[:3])
        if month is None:
            return None
        try:
            candidate = now_local.replace(
                month=month, day=int(day_text), hour=hour, minute=minute, second=0, microsecond=0
            )
        except ValueError:
            return None
        if candidate < now_local:
            candidate = candidate.replace(year=candidate.year + 1)
        return candidate.astimezone(UTC)

    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def classify_limit(
    error: Exception | None,
    rate_limit_info: Mapping[str, Any] | None,
    now: datetime,
) -> LimitObservation | None:
    """Recognize a usage limit from a failed `session/prompt`, or return None.

    Recognition: ``data.errorKind == "rate_limit"``, or a preceding
    ``usage_update``'s ``rate_limit_info.status == "rejected"``, or the
    error's own text starting with a temporary-limit prefix. The return time
    prefers `rate_limit_info`'s `resetsAt` when it observed the rejection,
    then the text's own `resets` clause; a structural signal with neither is
    still recognized, just with an unknown return time.
    """
    data = getattr(error, "data", None) if error is not None else None
    error_kind = data.get("errorKind") if isinstance(data, Mapping) else None
    message = str(error) if error is not None else ""
    text = _strip_internal_prefix(message)

    info = rate_limit_info if isinstance(rate_limit_info, Mapping) else None
    info_rejected = info is not None and info.get("status") == "rejected"
    temporary_text = _is_temporary_limit_text(text)

    if not (error_kind == "rate_limit" or info_rejected or temporary_text):
        return None

    reset_at = info.get("resetsAt") if info is not None else None
    if info_rejected and isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        return LimitObservation(
            reason="rate_limit",
            resume_at=datetime.fromtimestamp(reset_at, tz=UTC),
            source="rate_limit_info",
            detail=text or message,
        )

    text_resume = _parse_resets_clause(text, now=now) if temporary_text else None
    if text_resume is not None:
        return LimitObservation(
            reason="rate_limit", resume_at=text_resume, source="text", detail=text or message
        )

    if error_kind == "rate_limit":
        return LimitObservation(
            reason="rate_limit", resume_at=None, source="error_kind", detail=text or message
        )
    if info_rejected:
        return LimitObservation(
            reason="rate_limit", resume_at=None, source="rate_limit_info", detail=text or message
        )
    return LimitObservation(
        reason="rate_limit", resume_at=None, source="text", detail=text or message
    )
