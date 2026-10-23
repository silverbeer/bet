"""Reading the ``bet card`` (SB-1363): recent record, balances and exposure.

The zone is an input. Which zone a user means -- their stored one, or the
machine's when none was set -- is the caller's decision; a service on a
server has no machine zone worth meaning.
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo

from pydantic import ConfigDict, Field, field_validator

from bet.analytics.card import Card, build_card, window_bounds
from bet.database.repository import Warehouse
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet, BetLeg
from bet.services._query import Query, sportsbook_code
from bet.services.balance import account_name


class CardQuery(Query):
    """The card's window and filters.

    ``days`` local days end ``today`` in ``tz``; ``today`` defaults to the
    current date there. ``sportsbook`` narrows bets and balances alike.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tz: tzinfo
    days: int = Field(default=7, ge=1, le=60)
    today: date | None = None
    sportsbook: str | None = None
    sport: str | None = None
    include_void: bool = False

    @field_validator("sportsbook", mode="after")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return sportsbook_code(value)


def card_summary(w: Warehouse, query: CardQuery) -> Card:
    """The card over ``query``'s window, by settle date."""
    tz = query.tz
    today = query.today or datetime.now(tz).date()
    code = query.sportsbook
    start, end = window_bounds(today, query.days, tz)

    # Bound by settle time in SQL so legs are fetched for the window only.
    in_window = w.bets.search(
        status="settled",
        sportsbook_code=code,
        sport=query.sport,
        settled_since=start,
        settled_until=end,
    )
    settled: list[tuple[Bet, list[BetLeg]]] = [(bet, w.legs.for_bet(bet.id)) for bet in in_window]
    pending = w.bets.search(is_open=True, sportsbook_code=code, sport=query.sport)
    accounts = [
        a
        for a in w.accounts.fetch_all(order_by="sportsbook_code, label")
        if code is None or a.sportsbook_code == code
    ]
    snapshots: dict[str, list[BalanceSnapshot]] = {
        account_name(a): w.balances.for_account(a.id) for a in accounts
    }
    flows: dict[str, list[CashFlow]] = {
        account_name(a): w.cash_flows.for_account(a.id) for a in accounts
    }

    return build_card(
        settled,
        pending,
        snapshots,
        cash_flows=flows,
        days=query.days,
        today=today,
        tz=tz,
        include_void=query.include_void,
    )


__all__ = ["CardQuery", "card_summary"]
