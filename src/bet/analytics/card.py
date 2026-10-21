"""A shareable summary of recent betting: the ``bet card`` (SB-1363).

Built to be screenshotted: the record over the last few days, day by day and
by sport, the current streak, the best hit, and where the money sits.

The window is local calendar days, by **settle** date: "how did this week
go" is answered by what was decided this week, not by what was placed. The
caller supplies the zone and today's date, so the bucketing is testable at a
midnight edge without touching the clock.

Outcomes follow DATA_DICTIONARY §4.2. ``won`` is a win, ``lost`` a loss and
``push`` a push; ``partial`` and ``cashed_out`` count by the sign of their net
(zero is a push). A void did not happen as a bet and is in nothing here but
its own count.

Money follows §6.1. ``staked``, ``net`` and ``roi`` are cash ROI: over bets
with cash at stake, push included in the denominator. A free bet still counts
in the win/loss record -- it was a pick -- but contributes no money, so
``net / staked`` is always the ROI printed beside it. For the same reason
only a cash bet can be the best hit: its net is money that is in NET.

A balance's change is what betting did to it: the balance delta since the
window opened, less the deposits and withdrawals made over that span. An
account first snapshotted inside the window is measured from that first
snapshot instead, and ``since`` says so; one with a single snapshot has no
change to show.

Pure: no database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from bet.analytics._common import ZERO, _money
from bet.analytics.balance import _flows
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet, BetLeg

Outcome = Literal["W", "L", "P"]

MULTI = "MULTI"
OTHER = "OTHER"

SettledBet = tuple[Bet, Sequence[BetLeg]]


@dataclass(frozen=True)
class Record:
    """Win/loss/push counts with the cash figures behind them."""

    won: int = 0
    lost: int = 0
    push: int = 0
    staked: Decimal = ZERO
    net: Decimal = ZERO
    roi: Decimal | None = None

    @property
    def bets(self) -> int:
        return self.won + self.lost + self.push

    @property
    def label(self) -> str:
        """``5-3``, or ``5-3-1`` once there is a push to show."""
        base = f"{self.won}-{self.lost}"
        return f"{base}-{self.push}" if self.push else base


@dataclass(frozen=True)
class DayRow:
    day: date
    record: Record


@dataclass(frozen=True)
class SportRow:
    sport: str
    record: Record


@dataclass(frozen=True)
class AccountBalance:
    account: str
    balance: Decimal
    observed_at: datetime
    # Latest balance less a baseline -- the latest snapshot at or before the
    # window opened, else the earliest one inside it -- net of deposits and
    # withdrawals between the two; ``None`` when the account has only one
    # snapshot to go on.
    change: Decimal | None
    # When the baseline was taken, in the card's zone; ``None`` exactly when
    # ``change`` is.
    since: datetime | None = None


@dataclass(frozen=True)
class BestHit:
    bet_id: UUID
    sport: str
    label: str
    odds_american: int | None
    net: Decimal


@dataclass(frozen=True)
class Card:
    window_start: date
    window_end: date
    tz_name: str
    days: int
    total: Record
    by_day: list[DayRow]
    by_sport: list[SportRow]
    streak: str | None
    balances: list[AccountBalance]
    balance_total: Decimal
    # Sum of every account's change; ``None`` unless every account has one,
    # so it never pairs a full total with a partial change.
    balance_change: Decimal | None
    best: BestHit | None
    pending_count: int
    pending_exposure: Decimal
    # Settled voids in the window. ``None`` unless asked for (--include-void).
    voids: int | None = None


# ------------------------------------------------------------- outcomes


def outcome(bet: Bet) -> Outcome | None:
    """W, L or P for a settled bet; ``None`` for a void or an unsettled bet."""
    if bet.result == "won":
        return "W"
    if bet.result == "lost":
        return "L"
    if bet.result == "push":
        return "P"
    if bet.result in ("partial", "cashed_out"):
        net = bet.net_profit
        return "W" if net > 0 else "L" if net < 0 else "P"
    return None


def record_of(bets: Sequence[Bet]) -> Record:
    """The record over ``bets``. Voids are ignored; free bets count but carry no money."""
    outcomes = [o for o in (outcome(b) for b in bets) if o is not None]
    cash = [b for b in bets if outcome(b) is not None and b.cash_staked > 0]
    _, staked, net, roi = _money(cash)
    return Record(
        won=outcomes.count("W"),
        lost=outcomes.count("L"),
        push=outcomes.count("P"),
        staked=staked,
        net=net,
        roi=roi,
    )


def streak(bets: Sequence[Bet]) -> str | None:
    """The current run, e.g. ``W3``, in settle order. A push neither extends nor breaks it."""
    decided = [o for o in (outcome(b) for b in _by_settle(bets)) if o in ("W", "L")]
    if not decided:
        return None
    last = decided[-1]
    run = 0
    for o in reversed(decided):
        if o != last:
            break
        run += 1
    return f"{last}{run}"


EPOCH = datetime.min.replace(tzinfo=UTC)


def _by_settle(bets: Sequence[Bet]) -> list[Bet]:
    # Ties on settle time fall back to placement, then id, so the order never
    # depends on the order the caller fetched them in.
    return sorted(bets, key=lambda b: (b.settled_at or EPOCH, b.placed_at, b.id))


# ---------------------------------------------------------------- sport


def _leg_sport(leg: BetLeg) -> str:
    for value in (leg.sport, leg.league):
        if value and value.strip():
            return value.strip().upper()
    return OTHER


def sport_of(legs: Sequence[BetLeg]) -> str:
    """The ticket's one sport, ``MULTI`` when its legs span several, ``OTHER`` when unknown."""
    sports = {_leg_sport(leg) for leg in legs}
    if not sports:
        return OTHER
    return sports.pop() if len(sports) == 1 else MULTI


def selection_summary(legs: Sequence[BetLeg]) -> str:
    """``Chiefs ML``, or ``Chiefs ML +2 more`` -- as ``bet list`` shows it."""
    if not legs:
        return ""
    first = legs[0].selection_name or "?"
    return first if len(legs) == 1 else f"{first} +{len(legs) - 1} more"


# --------------------------------------------------------------- window


def window_bounds(today: date, days: int, tz: tzinfo) -> tuple[datetime, datetime]:
    """``[start, end)`` in UTC covering local days ``today - days + 1`` through ``today``."""
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def _best(window: Sequence[SettledBet]) -> BestHit | None:
    # Cash bets only: a free bet's winnings are not in NET.
    winners = [
        (b, legs)
        for b, legs in window
        if outcome(b) is not None and b.cash_staked > 0 and b.net_profit > 0
    ]
    if not winners:
        return None
    # Largest net; on a tie, the more recent.
    bet, legs = max(
        winners,
        key=lambda item: (item[0].net_profit, item[0].settled_at or EPOCH),
    )
    odds = bet.odds_american_settled
    return BestHit(
        bet_id=bet.id,
        sport=sport_of(legs),
        label=selection_summary(legs),
        odds_american=odds if odds is not None else bet.odds_american_placed,
        net=bet.net_profit,
    )


def _change(
    last: BalanceSnapshot, base: BalanceSnapshot | None, flows: Sequence[CashFlow]
) -> Decimal | None:
    if base is None:
        return None
    after, through = base.observed_at, last.observed_at
    moved = _flows(flows, "deposit", after, through) - _flows(flows, "withdrawal", after, through)
    return last.cash_balance - base.cash_balance - moved


def _balances(
    snapshots: Mapping[str, Sequence[BalanceSnapshot]],
    flows: Mapping[str, Sequence[CashFlow]],
    opened: datetime,
    tz: tzinfo,
) -> list[AccountBalance]:
    rows: list[AccountBalance] = []
    for account, snaps in snapshots.items():
        if not snaps:
            continue
        last = snaps[-1]
        before = [s for s in snaps if s.observed_at <= opened]
        base = before[-1] if before else (snaps[0] if len(snaps) > 1 else None)
        rows.append(
            AccountBalance(
                account=account,
                balance=last.cash_balance,
                observed_at=last.observed_at,
                change=_change(last, base, flows.get(account, ())),
                since=base.observed_at.astimezone(tz) if base is not None else None,
            )
        )
    return rows


# ---------------------------------------------------------------- build


def build_card(
    settled: Sequence[SettledBet],
    pending: Sequence[Bet],
    snapshots: Mapping[str, Sequence[BalanceSnapshot]],
    *,
    cash_flows: Mapping[str, Sequence[CashFlow]] | None = None,
    days: int,
    today: date,
    tz: tzinfo,
    include_void: bool = False,
) -> Card:
    """Summarise ``settled`` bets decided in the window, with balances and open exposure.

    ``settled`` may hold bets from outside the window; they are dropped here,
    as are bets with no ``settled_at``. ``snapshots`` maps an account name to
    its snapshots, oldest first; ``cash_flows`` maps the same names to their
    deposits and withdrawals, which each account's change excludes.
    """
    start, end = window_bounds(today, days, tz)
    in_window = [
        (b, legs)
        for b, legs in settled
        if b.status == "settled" and b.settled_at is not None and start <= b.settled_at < end
    ]
    counted = [(b, legs) for b, legs in in_window if outcome(b) is not None]
    bets = [b for b, _ in counted]

    by_day: dict[date, list[Bet]] = {
        today - timedelta(days=offset): [] for offset in range(days - 1, -1, -1)
    }
    for b in bets:
        assert b.settled_at is not None  # filtered above
        by_day.setdefault(b.settled_at.astimezone(tz).date(), []).append(b)

    by_sport: dict[str, list[Bet]] = {}
    for b, legs in counted:
        by_sport.setdefault(sport_of(legs), []).append(b)
    sport_rows = [SportRow(sport, record_of(group)) for sport, group in by_sport.items()]
    sport_rows.sort(key=lambda r: (-r.record.net, r.sport))

    balances = _balances(snapshots, cash_flows or {}, start, tz)
    changes = [a.change for a in balances if a.change is not None]
    complete = bool(balances) and len(changes) == len(balances)

    return Card(
        window_start=min(by_day),
        window_end=today,
        tz_name=getattr(tz, "key", None) or str(tz),
        days=days,
        total=record_of(bets),
        by_day=[DayRow(day, record_of(group)) for day, group in sorted(by_day.items())],
        by_sport=sport_rows,
        streak=streak(bets),
        balances=balances,
        balance_total=sum((a.balance for a in balances), ZERO),
        balance_change=sum(changes, ZERO) if complete else None,
        best=_best(counted),
        pending_count=len(pending),
        pending_exposure=sum((b.cash_staked for b in pending), ZERO),
        voids=sum(1 for b, _ in in_window if b.result == "void") if include_void else None,
    )


# ------------------------------------------------------------------ JSON


def _record_dict(record: Record, *, share: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "won": record.won,
        "lost": record.lost,
        "push": record.push,
        "label": record.label,
        "roi": record.roi,
    }
    if not share:
        out["staked"] = record.staked
        out["net"] = record.net
    return out


def card_to_dict(card: Card, *, share: bool = False) -> dict[str, Any]:
    """The card as one object. ``share`` drops every money amount; ratios stay."""
    best: dict[str, Any] | None = None
    if card.best is not None:
        best = {
            "bet_id": str(card.best.bet_id),
            "sport": card.best.sport,
            "label": card.best.label,
            "odds_american": card.best.odds_american,
        }
        if not share:
            best["net"] = card.best.net
    out: dict[str, Any] = {
        "window_start": card.window_start,
        "window_end": card.window_end,
        "timezone": card.tz_name,
        "days": card.days,
        "record": _record_dict(card.total, share=share),
        "streak": card.streak,
        "by_day": [{"day": d.day, **_record_dict(d.record, share=share)} for d in card.by_day],
        "by_sport": [
            {"sport": s.sport, **_record_dict(s.record, share=share)} for s in card.by_sport
        ],
        "best": best,
        "pending": {"count": card.pending_count},
    }
    if card.voids is not None:
        out["voids"] = card.voids
    if not share:
        out["pending"]["exposure"] = card.pending_exposure
        out["balances"] = [
            {
                "account": a.account,
                "balance": a.balance,
                "observed_at": a.observed_at,
                "change": a.change,
                "since": a.since,
            }
            for a in card.balances
        ]
        out["balance_total"] = card.balance_total
        out["balance_change"] = card.balance_change
    return out


__all__ = [
    "MULTI",
    "OTHER",
    "AccountBalance",
    "BestHit",
    "Card",
    "DayRow",
    "Record",
    "SportRow",
    "build_card",
    "card_to_dict",
    "outcome",
    "record_of",
    "selection_summary",
    "sport_of",
    "streak",
    "window_bounds",
]
