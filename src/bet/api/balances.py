"""``/balances``: each account's reconciliation, and recording a balance.

``GET`` rows are ``balance_report``'s, every column included -- which of them a
table shows is the client's decision, as it is for ``bet balance``.

``POST`` appends one snapshot through ``bet.services.ledger``, as ``bet balance
add`` does, and answers with the snapshot and the account it was filed under
(``account_is_new`` when this snapshot created it). ``observed_at`` must carry
an offset: the server's zone is not the user's.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.database.repository import Warehouse
from bet.services.balance import BalanceQuery, balance_report, require_known_sportsbook
from bet.services.capture import parse_capture_method
from bet.services.ledger import BalanceSnapshotRequest, RecordedSnapshot, add_balance_snapshot

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


class BalanceSnapshotBody(BaseModel):
    """``BalanceSnapshotRequest``, with ``capture_method`` as text."""

    model_config = ConfigDict(extra="forbid")

    sportsbook: str
    cash_balance: Decimal
    account_label: str | None = None
    bonus_balance: Decimal | None = None
    observed_at: AwareDatetime | None = None
    capture_method: str = "manual"
    source_file_id: UUID | None = None
    notes: str | None = None


@router.post(
    "/balances",
    status_code=status.HTTP_201_CREATED,
    responses=UNAUTHORIZED
    | {
        400: {"description": "Invalid snapshot or provenance"},
        404: {"description": "No such source file"},
    },
)
def add_balance(body: BalanceSnapshotBody, scope: CurrentScope, cursor: Cursor) -> RecordedSnapshot:
    """Append one dated balance snapshot for one account."""
    request = BalanceSnapshotRequest(
        **body.model_dump(exclude={"capture_method"}),
        capture_method=parse_capture_method(body.capture_method),
    )
    return add_balance_snapshot(Warehouse(cursor, scope), request)
