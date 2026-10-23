"""What the read services share: the query base and its normalisations."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from pydantic import BaseModel, ConfigDict


class Query(BaseModel):
    """Base for a service's typed input. Immutable, and strict about its fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def placed_bounds(
    since: date | None, until: date | None
) -> tuple[datetime | None, datetime | None]:
    """Global ``--since``/``--until`` are dates; a bet's placed_at is a UTC
    instant, so a date bound expands to the start/end of that day in UTC."""
    start = datetime.combine(since, time.min, tzinfo=UTC) if since is not None else None
    end = datetime.combine(until, time.max, tzinfo=UTC) if until is not None else None
    return start, end


def sportsbook_code(value: str | None) -> str | None:
    """A sportsbook code as stored: trimmed and lower-case, or ``None`` when blank."""
    return (value or "").strip().lower() or None


__all__ = ["Query", "placed_bounds", "sportsbook_code"]
