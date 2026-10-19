"""Balance reconciliation: does the bet ledger explain the reported balance? (SB-1333)

Between two snapshots of one account, at ``t0 < t1``, cash moves for exactly
four reasons, so the second balance is predictable:

    expected = balance(t0)
             + deposits(t0, t1]  - withdrawals(t0, t1]
             - cash staked on bets placed in (t0, t1]
             + cash returned by bets settled in (t0, t1]

    gap      = balance(t1) - expected

This is the ticket's "previous balance + deposits - withdrawals + settled P&L
- open stakes", bucketed by when each cash movement actually happened: a stake
leaves the account when the bet is placed and its return arrives when it
settles, so a bet still open at ``t1`` counts its stake only, and one placed
before ``t0`` but settled after it counts its return only.

A non-zero gap means the warehouse is missing a bet, holds a wrong stake or
return, or is missing a deposit or withdrawal. It is reported, never
corrected: guessing which would be inventing data.

An account is judged on its *cumulative* gap: the same equation applied once
from its first snapshot (the baseline) to its latest. The per-interval gap is
kept as a diagnostic, but it cannot carry the verdict on its own. A bet whose
``settled_at`` is the moment it was entered rather than when the book paid it
shifts its return into the next interval, giving a +X gap and then a -X gap
on a complete ledger; and a real gap in one interval vanishes from view as
soon as a routine snapshot follows it. Over the whole span the first cancels
and the second persists.

Cash only. Bonus stake never left the cash balance and bonus balance is not
withdrawable, so neither enters the equation. A free bet's winnings *are*
cash, and arrive through ``cash_returned`` like any other return.

Pure: no database. The caller supplies snapshots, cash flows, and a function
returning bet activity for an interval.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from bet.models.balance import BalanceSnapshot, CashFlow

ZERO = Decimal("0.00")

Status = Literal["baseline", "ok", "GAP"]

COLUMNS = [
    "account",
    "observed_at",
    "balance",
    "prev_balance",
    "change",
    "deposits",
    "withdrawals",
    "net_deposited",
    "staked",
    "returned",
    "expected",
    "gap",
    "cumulative_gap",
    "status",
]

# The interval diagnostics. Machine formats always carry them; the table drops
# them unless asked, so the account and money columns fit a terminal.
DIAGNOSTIC_COLUMNS = ["prev_balance", "staked", "returned", "expected", "gap"]

TOTAL = "TOTAL"


@dataclass(frozen=True)
class BetActivity:
    """Cash staked and cash returned on one account over one interval."""

    staked: Decimal = ZERO
    returned: Decimal = ZERO


ActivityFn = Callable[[datetime, datetime], BetActivity]


def _flows(
    flows: Sequence[CashFlow], kind: str, after: datetime | None, through: datetime
) -> Decimal:
    return sum(
        (
            f.amount
            for f in flows
            if f.kind == kind
            and f.occurred_at <= through
            and (after is None or f.occurred_at > after)
        ),
        ZERO,
    )


def _status(gap: Decimal) -> Status:
    return "ok" if gap == ZERO else "GAP"


def _net_deposited(flows: Sequence[CashFlow], through: datetime) -> Decimal:
    return _flows(flows, "deposit", None, through) - _flows(flows, "withdrawal", None, through)


def _expected(
    opening: BalanceSnapshot, closing: BalanceSnapshot, flows: Sequence[CashFlow], bets: BetActivity
) -> tuple[Decimal, Decimal, Decimal]:
    """(deposits, withdrawals, expected closing balance) over ``(opening, closing]``."""
    t0, t1 = opening.observed_at, closing.observed_at
    deposits = _flows(flows, "deposit", t0, t1)
    withdrawals = _flows(flows, "withdrawal", t0, t1)
    expected = opening.cash_balance + deposits - withdrawals - bets.staked + bets.returned
    return deposits, withdrawals, expected


def _baseline_row(account: str, snap: BalanceSnapshot, flows: Sequence[CashFlow]) -> dict[str, Any]:
    """The first snapshot: nothing to reconcile against, so no interval figures at all.

    Deposits and withdrawals are blank rather than lifetime-to-date: summed
    into ``TOTAL`` beside other accounts' interval figures, a lifetime figure
    would describe no real period.
    """
    return {
        "account": account,
        "observed_at": snap.observed_at,
        "balance": snap.cash_balance,
        "prev_balance": None,
        "change": None,
        "deposits": None,
        "withdrawals": None,
        "net_deposited": _net_deposited(flows, snap.observed_at),
        "staked": None,
        "returned": None,
        "expected": None,
        "gap": None,
        "cumulative_gap": None,
        "status": "baseline",
    }


def _interval_row(
    account: str,
    previous: BalanceSnapshot,
    snap: BalanceSnapshot,
    flows: Sequence[CashFlow],
    bets: BetActivity,
) -> dict[str, Any]:
    deposits, withdrawals, expected = _expected(previous, snap, flows, bets)
    gap = snap.cash_balance - expected
    return {
        "account": account,
        "observed_at": snap.observed_at,
        "balance": snap.cash_balance,
        "prev_balance": previous.cash_balance,
        "change": snap.cash_balance - previous.cash_balance,
        "deposits": deposits,
        "withdrawals": withdrawals,
        "net_deposited": _net_deposited(flows, snap.observed_at),
        "staked": bets.staked,
        "returned": bets.returned,
        "expected": expected,
        "gap": gap,
        "cumulative_gap": None,
        "status": _status(gap),
    }


def reconcile(
    account: str,
    snapshots: Sequence[BalanceSnapshot],
    flows: Sequence[CashFlow],
    activity: ActivityFn,
) -> list[dict[str, Any]]:
    """One row per snapshot of one account, oldest first: the ``--history`` view.

    ``snapshots`` must already be in observation order. The first snapshot has
    nothing to reconcile against, so it is a ``baseline`` with no gap.

    Each later row's ``status`` judges its own interval. ``cumulative_gap`` is
    the running sum of interval gaps, which telescopes to the gap over the
    whole span from the baseline to that row (see ``latest``).

    ``net_deposited`` is lifetime deposits less withdrawals as of each row's
    ``observed_at``, so it describes the same moment as the balance beside it.
    """
    rows: list[dict[str, Any]] = []
    previous: BalanceSnapshot | None = None
    cumulative = ZERO
    for snap in snapshots:
        if previous is None:
            rows.append(_baseline_row(account, snap, flows))
        else:
            row = _interval_row(
                account, previous, snap, flows, activity(previous.observed_at, snap.observed_at)
            )
            cumulative += row["gap"]
            row["cumulative_gap"] = cumulative
            rows.append(row)
        previous = snap
    return rows


def latest(
    account: str,
    snapshots: Sequence[BalanceSnapshot],
    flows: Sequence[CashFlow],
    activity: ActivityFn,
) -> dict[str, Any] | None:
    """The account's latest snapshot, judged over everything since its baseline.

    The interval figures (``staked`` to ``gap``) describe the last interval
    only, as a diagnostic. ``status`` is decided by ``cumulative_gap``: the
    same equation applied once across the whole span from the first snapshot
    to the last. A bet settled or captured late lands in a different interval
    from the one it belongs to, producing a +X gap in one and a -X gap in the
    next; across the span the two cancel. A real gap persists in the span no
    matter how many routine snapshots follow it.

    Costs two activity lookups (the last interval, and the span) however many
    snapshots the account has; with exactly two snapshots they coincide.
    """
    if not snapshots:
        return None
    baseline, last = snapshots[0], snapshots[-1]
    if len(snapshots) == 1:
        return _baseline_row(account, last, flows)

    previous = snapshots[-2]
    row = _interval_row(
        account, previous, last, flows, activity(previous.observed_at, last.observed_at)
    )
    if previous is baseline:
        cumulative: Decimal = row["gap"]
    else:
        _, _, expected = _expected(
            baseline, last, flows, activity(baseline.observed_at, last.observed_at)
        )
        cumulative = last.cash_balance - expected
    row["cumulative_gap"] = cumulative
    row["status"] = _status(cumulative)
    return row


def _sum_present(rows: Sequence[dict[str, Any]], key: str) -> Decimal | None:
    present = [r[key] for r in rows if r[key] is not None]
    return sum(present, ZERO) if present else None


def total(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The cross-account row over each account's latest row.

    Balances and lifetime net deposited are summed. Deposits and withdrawals
    are summed over the accounts that have an interval; a baseline has none.
    ``cumulative_gap`` is the sum of the account cumulative gaps, and the
    status is ``GAP`` if any account's cumulative gap is non-zero, so
    offsetting gaps cannot hide -- whatever per-interval status the rows
    themselves carry (``--history``). Other interval-specific figures span a
    different interval per account and are left blank rather than summed into
    something that describes no real period.
    """
    gaps = [r["cumulative_gap"] for r in rows if r["cumulative_gap"] is not None]
    status: Status = "GAP" if any(g != ZERO for g in gaps) else "ok" if gaps else "baseline"
    return {
        "account": TOTAL,
        "observed_at": None,
        "balance": sum((r["balance"] for r in rows), ZERO),
        "prev_balance": None,
        "change": None,
        "deposits": _sum_present(rows, "deposits"),
        "withdrawals": _sum_present(rows, "withdrawals"),
        "net_deposited": sum((r["net_deposited"] for r in rows), ZERO),
        "staked": None,
        "returned": None,
        "expected": None,
        "gap": None,
        "cumulative_gap": sum(gaps, ZERO) if gaps else None,
        "status": status,
    }


__all__ = [
    "COLUMNS",
    "DIAGNOSTIC_COLUMNS",
    "TOTAL",
    "ActivityFn",
    "BetActivity",
    "latest",
    "reconcile",
    "total",
]
