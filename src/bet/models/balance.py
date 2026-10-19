"""Account balances and cash flows: the independent check on the bet ledger.

A sportsbook's reported balance is the one figure BET does not derive from its
own bets. Two snapshots, the deposits and withdrawals between them, and the
bets placed and settled between them determine what the second balance should
be; a difference means a bet is missing or was captured wrong.

Both models are append-only facts. A balance is observed at a moment, a
deposit happened at a moment, and neither is edited afterwards -- so neither
carries ``updated_at``.

Bonus and free-bet credit is not cash. ``bonus_balance`` is recorded because
operators show it beside the cash figure, but it is never reconciled.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bet.models.bet import ZERO, CaptureMethod, Money
from bet.models.ownership import OwnedModel, utc_now

CashFlowKind = Literal["deposit", "withdrawal"]


class _AppendOnly(BaseModel):
    """``created_at`` without ``updated_at``: these rows are never updated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    created_at: datetime = Field(default_factory=utc_now)


def _require_aware(model: BaseModel, *names: str) -> None:
    for name in names:
        value = getattr(model, name)
        if value is not None and value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")


class BalanceSnapshot(OwnedModel, _AppendOnly):
    """One account's balance as the operator reported it at one moment."""

    id: UUID
    sportsbook_account_id: UUID

    observed_at: datetime
    currency: str = Field(default="USD", min_length=3, max_length=3)
    cash_balance: Money
    bonus_balance: Money | None = None

    capture_method: CaptureMethod = "manual"
    source_file_id: UUID | None = None

    supersedes_id: UUID | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        if self.cash_balance < ZERO:
            raise ValueError("cash_balance cannot be negative")
        if self.bonus_balance is not None and self.bonus_balance < ZERO:
            raise ValueError("bonus_balance cannot be negative")
        _require_aware(self, "observed_at", "created_at")
        return self


class CashFlow(OwnedModel, _AppendOnly):
    """Cash moved into an account (deposit) or out of it (withdrawal)."""

    id: UUID
    sportsbook_account_id: UUID

    kind: CashFlowKind
    occurred_at: datetime
    currency: str = Field(default="USD", min_length=3, max_length=3)
    # Always positive; ``kind`` carries the direction.
    amount: Money

    capture_method: CaptureMethod = "manual"
    source_file_id: UUID | None = None

    supersedes_id: UUID | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        if self.amount <= ZERO:
            raise ValueError("a cash flow amount must be positive")
        _require_aware(self, "occurred_at", "created_at")
        return self


__all__ = ["BalanceSnapshot", "CashFlow", "CashFlowKind"]
