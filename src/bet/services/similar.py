"""Reading your record on bets like a proposed one (SB-1133).

The proposal arrives already parsed into legs; how a front end reads a slip
is its own business. The history it is compared against is narrowed by the
same filters every analytical command takes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from pydantic import Field

from bet.analytics.similar import HistoricBet, Proposal, compare, eligible
from bet.database.repository import Warehouse
from bet.models.bet import BetLeg
from bet.services._query import Query, placed_bounds


class SimilarQuery(Query):
    """A proposed ticket, the history filters, and the sample-size thresholds.

    ``since``/``until`` bound the history's ``placed_at``, read as UTC days.
    """

    legs: list[BetLeg] = Field(min_length=1)
    wager_kind: str
    odds_american: int | None = None
    promo_types: Sequence[str] = ()
    sportsbook: str | None = None
    sport: str | None = None
    since: date | None = None
    until: date | None = None
    include_void: bool = False
    min_settled: int
    exploratory_below: int


def similar_bets(w: Warehouse, query: SimilarQuery) -> list[dict[str, Any]]:
    """Comparison rows for the proposal against settled, eligible history."""
    since, until = placed_bounds(query.since, query.until)
    history = [
        HistoricBet(
            bet=bet,
            legs=w.legs.for_bet(bet.id),
            promo_types=frozenset(p.promotion_type for p in w.bet_promotions.for_bet(bet.id)),
        )
        for bet in w.bets.search(
            status="settled",
            sportsbook_code=query.sportsbook,
            sport=query.sport,
            since=since,
            until=until,
        )
        if eligible(bet, include_void=query.include_void)
    ]
    return compare(
        Proposal(
            legs=query.legs,
            wager_kind=query.wager_kind,
            odds_american=query.odds_american,
            promo_types=query.promo_types,
        ),
        history,
        min_settled=query.min_settled,
        exploratory_below=query.exploratory_below,
    )


__all__ = ["SimilarQuery", "similar_bets"]
