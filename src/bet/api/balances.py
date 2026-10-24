"""``GET /balances``: each account's reconciliation and the ``TOTAL`` row.

The rows are ``balance_report``'s, every column included -- which of them a
table shows is the client's decision, as it is for ``bet balance``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.database.repository import Warehouse
from bet.services.balance import BalanceQuery, balance_report, require_known_sportsbook

router = APIRouter(tags=["balances"])


@router.get("/balances", responses=UNAUTHORIZED | {400: {"description": "Unknown sportsbook"}})
def balances(
    scope: CurrentScope,
    cursor: Cursor,
    sportsbook: Annotated[str | None, Query(description="Only accounts at this book.")] = None,
    account_label: Annotated[
        str | None, Query(description="Only the account with this label.")
    ] = None,
    history: Annotated[
        bool, Query(description="Every snapshot interval, not just the latest.")
    ] = False,
) -> list[dict[str, Any]]:
    """Latest balance per account (every interval with ``history``), then ``TOTAL``."""
    query = BalanceQuery(sportsbook=sportsbook, account_label=account_label, history=history)
    require_known_sportsbook(cursor, query.sportsbook)
    return balance_report(Warehouse(cursor, scope), query)
