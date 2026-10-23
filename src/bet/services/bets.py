"""Reading bets: the filtered listing and one bet in full.

``list_bets`` backs ``bet list``, ``bet bets open`` and ``bet bets search``
-- the same query with different filters fixed. ``bet_detail`` backs ``bet
bets show``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg, BetPromotion, BetStatus, WagerKind
from bet.services._query import Query, placed_bounds

STATUSES: tuple[BetStatus, ...] = ("pending", "settled")
KINDS: tuple[WagerKind, ...] = get_args(WagerKind)


class ListQuery(Query):
    """Filters for a bet listing. Every one is optional; none means all bets.

    ``since``/``until`` are dates bounding ``placed_at``, read as UTC days.
    ``status`` and ``kind`` are plain strings so an unknown value is refused
    with the known ones, rather than as a type error.
    """

    is_open: bool = False
    status: str | None = None
    kind: str | None = None
    sportsbook: str | None = None
    sport: str | None = None
    text: str | None = None
    since: date | None = None
    until: date | None = None
    limit: int | None = None

    @model_validator(mode="after")
    def _known_filters(self) -> Self:
        if self.status is not None and self.status not in STATUSES:
            raise UsageError(
                f"{self.status!r} is not a known status.",
                remediation=f"Known statuses: {', '.join(STATUSES)}.",
            )
        if self.kind is not None and self.kind not in KINDS:
            raise UsageError(
                f"{self.kind!r} is not a known wager kind.",
                remediation=f"Known kinds: {', '.join(KINDS)}.",
            )
        if self.is_open and self.status not in (None, "pending"):
            raise UsageError("--open and --status settled cannot both be given.")
        return self


class BetListing(BaseModel):
    """One listed bet with what a listing shows beside it."""

    model_config = ConfigDict(frozen=True)

    bet: Bet
    account_label: str | None
    legs: list[BetLeg]


class BetDetail(BaseModel):
    """One bet in full: legs, promotions and provenance."""

    model_config = ConfigDict(frozen=True)

    bet: Bet
    account_label: str | None
    legs: list[BetLeg]
    promotions: list[BetPromotion]
    provenance: dict[str, Any]


def list_bets(w: Warehouse, query: ListQuery) -> list[BetListing]:
    """Current bets matching ``query``, newest first."""
    since, until = placed_bounds(query.since, query.until)
    bets = w.bets.search(
        status=query.status,
        is_open=query.is_open,
        sportsbook_code=query.sportsbook,
        sport=query.sport,
        wager_kind=query.kind,
        text=query.text,
        since=since,
        until=until,
        limit=query.limit,
    )
    listings = []
    for bet in bets:
        account = w.accounts.find(bet.sportsbook_account_id)
        listings.append(
            BetListing(
                bet=bet,
                account_label=account.label if account else None,
                legs=w.legs.for_bet(bet.id),
            )
        )
    return listings


def bet_detail(w: Warehouse, bet_id: UUID) -> BetDetail:
    """One bet owned by this scope, or ``NotFoundError``."""
    bet = w.bets.get(bet_id)
    account = w.accounts.find(bet.sportsbook_account_id)
    return BetDetail(
        bet=bet,
        account_label=account.label if account else None,
        legs=w.legs.for_bet(bet.id),
        promotions=w.bet_promotions.for_bet(bet.id),
        provenance=w.bets.provenance(bet.id),
    )


__all__ = [
    "KINDS",
    "STATUSES",
    "BetDetail",
    "BetListing",
    "ListQuery",
    "bet_detail",
    "list_bets",
]
