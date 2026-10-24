"""``GET /card``: the ``bet card`` as ``card_to_dict`` gives it.

The body is exactly ``card_to_dict(card, share=share)``, so ``share=true``
carries no money amount at all -- the server never sends what a shared card
hides. A client that renders a shared card itself can ask for the full one and
hide the amounts locally, as ``render_card`` does.

Days are local to ``tz``, an IANA name. Without one the user's stored zone is
used as stored -- ``UTC`` included. The CLI reads a stored ``UTC`` as "never
set" and falls back to the machine's zone, but a server's zone describes the
server, not the user; a client that wants its machine's zone sends it as
``tz``. A stored zone that no longer names one is read as ``UTC``.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query

from bet.analytics.card import card_to_dict
from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.database import identity
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.services.card import CardQuery, card_summary

router = APIRouter(tags=["card"])

DEFAULT_ZONE = "UTC"


def _zone(key: str) -> tzinfo | None:
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError, ValueError:
        return None


def resolve_zone(requested: str | None, stored: str | None) -> tzinfo:
    """``requested`` when given (or ``UsageError``), else the stored zone, else UTC."""
    if requested is not None:
        zone = _zone(requested)
        if zone is None:
            raise UsageError(
                f"{requested!r} is not a known time zone.",
                remediation="Pass an IANA name, e.g. tz=America/New_York.",
            )
        return zone
    return _zone(stored or DEFAULT_ZONE) or ZoneInfo(DEFAULT_ZONE)


@router.get("/card", responses=UNAUTHORIZED | {400: {"description": "Unknown time zone"}})
def card(
    scope: CurrentScope,
    cursor: Cursor,
    days: Annotated[int, Query(ge=1, le=60, description="Local days to cover, ending today.")] = 7,
    share: Annotated[bool, Query(description="Leave out every dollar amount.")] = False,
    sportsbook: str | None = None,
    sport: str | None = None,
    include_void: Annotated[bool, Query(description="Add a count of voids.")] = False,
    tz: Annotated[
        str | None, Query(description="IANA zone for the days. Default: the user's stored zone.")
    ] = None,
) -> dict[str, Any]:
    """Record, net and ROI over the last ``days`` local days by settle date."""
    query = CardQuery(
        tz=resolve_zone(tz, identity.user_timezone(cursor, scope)),
        days=days,
        sportsbook=sportsbook,
        sport=sport,
        include_void=include_void,
    )
    return card_to_dict(card_summary(Warehouse(cursor, scope), query), share=share)
