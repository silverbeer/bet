"""``POST /similar``: your settled record on bets like a proposed one.

A POST because the proposal is a nested document -- legs and promotions -- that
a query string carries badly. Nothing is written.

The proposal arrives typed, in the inputs ``bet add`` and ``bet similar``
already build from ``--leg``/``--promo`` text (``LegInput``,
``PromotionInput``), and is judged by the same ``bet.services.tickets`` rules
in the same order: legs, wager kind, price, promotions. The sample-size
thresholds are the server's configuration, as they are the CLI's.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.config import Thresholds
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import WagerKind
from bet.services.odds import require_american
from bet.services.similar import SimilarQuery, similar_bets
from bet.services.tickets import (
    LegInput,
    PromotionInput,
    build_legs,
    build_promotions,
    ticket_odds,
    wager_kind_for,
)

router = APIRouter(tags=["similar"])


class SimilarRequest(BaseModel):
    """A proposed ticket, and the filters narrowing the history it is compared to.

    ``wager_kind`` defaults by leg count, as ``bet similar --wager-kind`` does.
    ``odds_american`` is the ticket price; without it the price is derived
    from the legs, and a ticket that cannot be priced is still compared on
    every other dimension. ``since``/``until`` bound the history's placement,
    as UTC days.
    """

    model_config = ConfigDict(extra="forbid")

    legs: Annotated[list[LegInput], Field(min_length=1)]
    wager_kind: WagerKind | None = None
    odds_american: int | None = None
    promotions: list[PromotionInput] = Field(default_factory=list)
    sportsbook: str | None = None
    sport: str | None = None
    since: date | None = None
    until: date | None = None
    include_void: bool = False


@router.post("/similar", responses=UNAUTHORIZED | {400: {"description": "Invalid proposal"}})
def similar(
    body: SimilarRequest, request: Request, scope: CurrentScope, cursor: Cursor
) -> list[dict[str, Any]]:
    """Comparison rows for the proposal, by dimension, against settled history."""
    thresholds: Thresholds = request.app.state.settings.bet.thresholds
    ids = {"tenant_id": scope.tenant_id, "user_id": scope.user_id, "bet_id": uuid4()}
    legs = build_legs(body.legs, **ids)
    kind = wager_kind_for(body.wager_kind, leg_count=len(legs))
    explicit = require_american(body.odds_american) if body.odds_american is not None else None
    try:
        odds_american: int | None = ticket_odds(legs, explicit)[0]
    except UsageError:
        odds_american = None
    promo_types = [p.promotion_type for p in build_promotions(body.promotions, legs=legs, **ids)]
    return similar_bets(
        Warehouse(cursor, scope),
        SimilarQuery(
            legs=legs,
            wager_kind=kind,
            odds_american=odds_american,
            promo_types=promo_types,
            sportsbook=body.sportsbook,
            sport=body.sport,
            since=body.since,
            until=body.until,
            include_void=body.include_void,
            min_settled=thresholds.min_settled_bets,
            exploratory_below=thresholds.exploratory_below,
        ),
    )
