"""Recording balances and cash flows: the ledger a balance reconciles against.

Backs ``bet balance add``, ``bet deposit`` and ``bet withdraw`` (SB-1333).
Both rows are append-only -- correcting a wrong one is SB-703 -- and are
transcribed exactly like a bet: ``capture_method`` says how the figure was
obtained, and a source is archived and cited.

Accounts are matched strictly (``capture.resolve_account``): a row filed
against a phantom account silently breaks two reconciliations.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.balance import BalanceSnapshot, CashFlow, CashFlowKind
from bet.models.bet import CaptureMethod
from bet.models.ownership import SportsbookAccount
from bet.services._query import Query
from bet.services.capture import check_money, now_if_absent, require_not_future, resolve_account
from bet.services.sources import SourceToArchive, check_provenance, cited_source, source_id


class BalanceSnapshotRequest(Query):
    """One account's cash balance as the sportsbook reported it.

    ``cash_balance`` excludes bonus credit, which goes in ``bonus_balance``:
    recorded, never reconciled. ``observed_at`` defaults to now and may not be
    in the future.
    """

    sportsbook: str
    cash_balance: Decimal
    account_label: str | None = None
    bonus_balance: Decimal | None = None
    observed_at: AwareDatetime | None = None
    capture_method: CaptureMethod = "manual"
    source_file_id: UUID | None = None
    notes: str | None = None


class CashFlowRequest(Query):
    """Cash moved into (``deposit``) or out of (``withdrawal``) an account.

    ``amount`` is always positive; ``kind`` carries the direction. Bonus and
    free-bet credit is not a deposit: an operator awarding one moves no cash.
    """

    kind: CashFlowKind
    sportsbook: str
    amount: Decimal
    account_label: str | None = None
    occurred_at: AwareDatetime | None = None
    capture_method: CaptureMethod = "manual"
    source_file_id: UUID | None = None
    notes: str | None = None


class RecordedSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: SportsbookAccount
    account_is_new: bool
    snapshot: BalanceSnapshot


class RecordedCashFlow(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: SportsbookAccount
    account_is_new: bool
    cash_flow: CashFlow


def add_balance_snapshot(
    w: Warehouse, request: BalanceSnapshotRequest, *, source: SourceToArchive | None = None
) -> RecordedSnapshot:
    """Append one dated balance snapshot, creating the account if it is the first."""
    check_provenance(
        request.capture_method,
        source=source,
        source_file_id=request.source_file_id,
        what="balance",
    )
    cited_source(w, request.source_file_id)
    cash_balance = check_money(request.cash_balance, field_name="cash_balance")
    bonus_balance = (
        check_money(request.bonus_balance, field_name="bonus_balance")
        if request.bonus_balance is not None
        else None
    )
    observed_at = require_not_future(now_if_absent(request.observed_at), field_name="observed_at")

    account, is_new = resolve_account(
        w,
        sportsbook_code=request.sportsbook,
        account_label=request.account_label,
        strict_label=True,
    )
    snapshot = BalanceSnapshot(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=uuid4(),
        sportsbook_account_id=account.id,
        observed_at=observed_at,
        cash_balance=cash_balance,
        bonus_balance=bonus_balance,
        capture_method=request.capture_method,
        source_file_id=request.source_file_id,
        notes=request.notes,
    )

    with w.transaction() as tx:
        snapshot.source_file_id = source_id(
            tx,
            source=source,
            source_file_id=request.source_file_id,
            capture_method=request.capture_method,
        )
        if is_new:
            tx.accounts.add(account)
        tx.balances.add(snapshot)

    return RecordedSnapshot(account=account, account_is_new=is_new, snapshot=snapshot)


def require_moves_money(kind: CashFlowKind, amount: Decimal) -> Decimal:
    """A cash flow of nothing is not a cash flow."""
    if amount == 0:
        raise UsageError(f"a {kind} must move money: --amount must be greater than zero.")
    return amount


def record_cash_flow(
    w: Warehouse, request: CashFlowRequest, *, source: SourceToArchive | None = None
) -> RecordedCashFlow:
    """Append one deposit or withdrawal, creating the account if it is the first."""
    kind = request.kind
    check_provenance(
        request.capture_method, source=source, source_file_id=request.source_file_id, what=kind
    )
    cited_source(w, request.source_file_id)
    amount = require_moves_money(kind, check_money(request.amount, field_name="amount"))
    occurred_at = require_not_future(now_if_absent(request.occurred_at), field_name="occurred_at")

    account, is_new = resolve_account(
        w,
        sportsbook_code=request.sportsbook,
        account_label=request.account_label,
        strict_label=True,
    )
    flow = CashFlow(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=uuid4(),
        sportsbook_account_id=account.id,
        kind=kind,
        occurred_at=occurred_at,
        amount=amount,
        capture_method=request.capture_method,
        source_file_id=request.source_file_id,
        notes=request.notes,
    )

    with w.transaction() as tx:
        flow.source_file_id = source_id(
            tx,
            source=source,
            source_file_id=request.source_file_id,
            capture_method=request.capture_method,
        )
        if is_new:
            tx.accounts.add(account)
        tx.cash_flows.add(flow)

    return RecordedCashFlow(account=account, account_is_new=is_new, cash_flow=flow)


__all__ = [
    "BalanceSnapshotRequest",
    "CashFlowRequest",
    "RecordedCashFlow",
    "RecordedSnapshot",
    "add_balance_snapshot",
    "record_cash_flow",
    "require_moves_money",
]
