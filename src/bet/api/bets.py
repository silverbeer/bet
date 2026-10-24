"""``/bets``: the listing, one bet in full, recording a bet and settling one.

Every route returns the service's own models (``BetListing``, ``BetDetail``),
so a client validates the JSON back into them and renders exactly what ``bet
list`` and ``bet bets show`` do locally. ``net_profit`` and the other ``Bet``
properties are derived, not sent. ``POST /bets`` and ``POST
/bets/{bet_id}/settle`` answer with the bet as ``GET /bets/{bet_id}`` would.

``status`` and ``kind`` are ``Literal`` here so the OpenAPI schema lists the
known values; ``ListQuery`` keeps them as strings so the CLI's messages are
unchanged. The write bodies keep ``capture_method``, ``wager_kind`` and the
results as strings for the same reason: ``bet.services.add`` and
``bet.services.settle`` refuse an unknown one with the text the CLI shows.

A bet cites its screenshot by the ``source_file_id`` ``POST /sources``
returned. Another user's id is 404, exactly like an unknown one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.database.repository import Warehouse
from bet.models.bet import BetStatus, WagerKind
from bet.services.add import ZERO, AddBetRequest, add_bet
from bet.services.bets import BetDetail, BetListing, ListQuery, bet_detail, list_bets
from bet.services.capture import parse_capture_method
from bet.services.settle import SettleRequest, settle_bet
from bet.services.tickets import LegGroupInput, LegInput, PromotionInput

router = APIRouter(prefix="/bets", tags=["bets"])


@router.get("", responses=UNAUTHORIZED | {400: {"description": "Contradictory filters"}})
def bets(
    scope: CurrentScope,
    cursor: Cursor,
    is_open: Annotated[bool, Query(alias="open", description="Only pending bets.")] = False,
    status: BetStatus | None = None,
    kind: WagerKind | None = None,
    sportsbook: str | None = None,
    sport: str | None = None,
    text: Annotated[
        str | None, Query(description="Substring of a selection, market, team or player.")
    ] = None,
    since: Annotated[date | None, Query(description="Placed on or after, a UTC day.")] = None,
    until: Annotated[date | None, Query(description="Placed on or before, a UTC day.")] = None,
    limit: Annotated[int | None, Query(ge=1, description="Maximum rows.")] = None,
) -> list[BetListing]:
    """Current bets matching every filter given, newest first."""
    query = ListQuery(
        is_open=is_open,
        status=status,
        kind=kind,
        sportsbook=sportsbook,
        sport=sport,
        text=text,
        since=since,
        until=until,
        limit=limit,
    )
    return list_bets(Warehouse(cursor, scope), query)


@router.get("/{bet_id}", responses=UNAUTHORIZED | {404: {"description": "No such bet"}})
def bet(bet_id: UUID, scope: CurrentScope, cursor: Cursor) -> BetDetail:
    """One bet in full: legs, promotions and provenance. Another user's id is 404."""
    return bet_detail(Warehouse(cursor, scope), bet_id)


class AddBetBody(BaseModel):
    """One ticket as placed: ``AddBetRequest``, with ``capture_method`` as text.

    ``odds_american`` is the base price the slip shows, never a boosted one.
    ``placed_at`` defaults to now. ``source_file_id`` is an id from ``POST
    /sources``, and needs a ``capture_method`` other than ``manual``.
    """

    model_config = ConfigDict(extra="forbid")

    sportsbook: str
    account_label: str | None = None
    legs: Annotated[list[LegInput], Field(min_length=1)]
    odds_american: int | None = None
    leg_group: LegGroupInput | None = None
    wager_kind: str | None = None
    promotions: list[PromotionInput] = Field(default_factory=list)
    cash_staked: Decimal = ZERO
    bonus_staked: Decimal = ZERO
    placed_at: AwareDatetime | None = None
    capture_method: str = "manual"
    source_file_id: UUID | None = None


class SettleBody(BaseModel):
    """The outcome: ``SettleRequest`` without the id, which is the path's.

    ``leg_results`` is keyed by leg order. ``cash_returned`` overrides the
    computed return; ``settled_at`` defaults to now; ``force`` re-settles.
    """

    model_config = ConfigDict(extra="forbid")

    result: str | None = None
    cash_returned: Decimal | None = None
    settled_at: AwareDatetime | None = None
    leg_results: dict[int, str] = Field(default_factory=dict)
    force: bool = False


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=UNAUTHORIZED
    | {
        400: {"description": "Invalid ticket or provenance"},
        404: {"description": "No such source file"},
    },
)
def add(body: AddBetBody, scope: CurrentScope, cursor: Cursor) -> BetDetail:
    """Record one bet, legs and promotions, as ``bet add`` does."""
    request = AddBetRequest(
        **body.model_dump(exclude={"capture_method"}),
        capture_method=parse_capture_method(body.capture_method),
    )
    w = Warehouse(cursor, scope)
    return bet_detail(w, add_bet(w, request).bet.id)


@router.post(
    "/{bet_id}/settle",
    responses=UNAUTHORIZED
    | {400: {"description": "Invalid outcome"}, 404: {"description": "No such bet"}},
)
def settle(bet_id: UUID, body: SettleBody, scope: CurrentScope, cursor: Cursor) -> BetDetail:
    """Settle one bet, as ``bet settle`` does. Another user's id is 404."""
    w = Warehouse(cursor, scope)
    settle_bet(w, SettleRequest(bet_id=bet_id, **body.model_dump()))
    return bet_detail(w, bet_id)
