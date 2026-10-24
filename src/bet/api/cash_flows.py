"""``POST /cash-flows``: a deposit or a withdrawal, as ``bet deposit`` and ``bet withdraw``.

One route for both: ``kind`` carries the direction and ``amount`` is always
positive, as in ``bet.services.ledger``. The answer is the cash flow and the
account it was filed under (``account_is_new`` when this one created it).
``occurred_at`` must carry an offset: the server's zone is not the user's.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import AwareDatetime, BaseModel, ConfigDict

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.database.repository import Warehouse
from bet.models.balance import CashFlowKind
from bet.services.capture import parse_capture_method
from bet.services.ledger import CashFlowRequest, RecordedCashFlow, record_cash_flow

router = APIRouter(tags=["balances"])


class CashFlowBody(BaseModel):
    """``CashFlowRequest``, with ``capture_method`` as text."""

    model_config = ConfigDict(extra="forbid")

    kind: CashFlowKind
    sportsbook: str
    amount: Decimal
    account_label: str | None = None
    occurred_at: AwareDatetime | None = None
    capture_method: str = "manual"
    source_file_id: UUID | None = None
    notes: str | None = None


@router.post(
    "/cash-flows",
    status_code=status.HTTP_201_CREATED,
    responses=UNAUTHORIZED
    | {
        400: {"description": "Invalid cash flow or provenance"},
        404: {"description": "No such source file"},
    },
)
def add_cash_flow(body: CashFlowBody, scope: CurrentScope, cursor: Cursor) -> RecordedCashFlow:
    """Append one deposit or withdrawal for one account."""
    request = CashFlowRequest(
        **body.model_dump(exclude={"capture_method"}),
        capture_method=parse_capture_method(body.capture_method),
    )
    return record_cash_flow(Warehouse(cursor, scope), request)
