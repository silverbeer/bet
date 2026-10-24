"""``GET /bets`` and ``GET /bets/{bet_id}``: the listing and one bet in full.

Both return the service's own models (``BetListing``, ``BetDetail``), so a
client validates the JSON back into them and renders exactly what ``bet list``
and ``bet bets show`` do locally. ``net_profit`` and the other ``Bet``
properties are derived, not sent.

``status`` and ``kind`` are ``Literal`` here so the OpenAPI schema lists the
known values; ``ListQuery`` keeps them as strings so the CLI's messages are
unchanged.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.database.repository import Warehouse
from bet.models.bet import BetStatus, WagerKind
from bet.services.bets import BetDetail, BetListing, ListQuery, bet_detail, list_bets

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
