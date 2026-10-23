"""Reading balances: each account's reconciliation, and a total across them.

Backs ``bet balance`` (SB-1333). The reconciliation itself is
``bet.analytics.balance``; this gathers what it reads.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import field_validator

from bet.analytics.balance import BetActivity, latest, reconcile, total
from bet.database.repository import Warehouse, known_sportsbook_codes
from bet.errors import UsageError
from bet.models.ownership import SportsbookAccount
from bet.services._query import Query, sportsbook_code

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class BalanceQuery(Query):
    """Which accounts to report, and whether to show every interval.

    ``sportsbook`` is normalised to its stored code; blank means every book.
    """

    sportsbook: str | None = None
    account_label: str | None = None
    history: bool = False

    @field_validator("sportsbook", mode="after")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return sportsbook_code(value)


def account_name(account: SportsbookAccount) -> str:
    """``fanduel``, or ``fanduel:boosts`` when the label says something more."""
    if account.label == account.sportsbook_code:
        return account.sportsbook_code
    return f"{account.sportsbook_code}:{account.label}"


def require_known_sportsbook(conn: DuckDBPyConnection, code: str | None) -> None:
    """Refuse a sportsbook code that is not seeded. ``None`` means every book."""
    if code is not None and code not in (known := known_sportsbook_codes(conn)):
        raise UsageError(
            f"{code!r} is not a known sportsbook.",
            remediation="Known sportsbooks: " + ", ".join(sorted(known)),
        )


def _activity(w: Warehouse, account_id: UUID, after: datetime, through: datetime) -> BetActivity:
    staked, returned = w.bets.reconciliation_inputs(account_id, after, through)
    return BetActivity(staked=staked, returned=returned)


def balance_report(w: Warehouse, query: BalanceQuery) -> list[dict[str, Any]]:
    """Reconciliation rows, one per account (every interval with ``history``).

    Accounts with no snapshot are skipped. A ``TOTAL`` row across each
    account's latest row closes the list whenever there is at least one.
    """
    accounts = [
        a
        for a in w.accounts.fetch_all(order_by="sportsbook_code, label")
        if (query.sportsbook is None or a.sportsbook_code == query.sportsbook)
        and (query.account_label is None or a.label == query.account_label)
    ]

    rows: list[dict[str, Any]] = []
    last_rows: list[dict[str, Any]] = []
    for account in accounts:
        snapshots = w.balances.for_account(account.id)
        if not snapshots:
            continue
        args = (
            account_name(account),
            snapshots,
            w.cash_flows.for_account(account.id),
            partial(_activity, w, account.id),
        )
        if query.history:
            reconciled = reconcile(*args)
            rows.extend(reconciled)
            last_rows.append(reconciled[-1])
        else:
            row = latest(*args)
            assert row is not None  # snapshots is non-empty
            rows.append(row)
            last_rows.append(row)

    if last_rows:
        rows.append(total(last_rows))
    return rows


__all__ = ["BalanceQuery", "account_name", "balance_report", "require_known_sportsbook"]
