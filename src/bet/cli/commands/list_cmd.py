"""``bet list`` — list bets, newest first.

Filters split two ways: sportsbook/sport/since/until are the shared
``GlobalOptions`` every analytical command takes (``bet --sportsbook fanduel
list``), read from the Typer context rather than redeclared here. ``--open``
and ``--status`` are specific to this command.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated

import typer

from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg, BetStatus

STATUSES: tuple[BetStatus, ...] = ("pending", "settled")


def _bounds(since: date | None, until: date | None) -> tuple[datetime | None, datetime | None]:
    """Global ``--since``/``--until`` are dates; a bet's placed_at is a UTC
    instant, so a date bound expands to the start/end of that day in UTC."""
    start = datetime.combine(since, time.min, tzinfo=UTC) if since is not None else None
    end = datetime.combine(until, time.max, tzinfo=UTC) if until is not None else None
    return start, end


def _selection_summary(legs: list[BetLeg]) -> str:
    if not legs:
        return ""
    first = legs[0].selection_name or "?"
    return first if len(legs) == 1 else f"{first} +{len(legs) - 1} more"


def _row(bet: Bet, account_label: str | None, legs: list[BetLeg]) -> dict[str, object]:
    return {
        "id": str(bet.id),
        "placed": bet.placed_at.date().isoformat(),
        "sportsbook": account_label or "",
        "selection": _selection_summary(legs),
        "odds": bet.odds_american_placed,
        "stake": bet.cash_staked + bet.bonus_staked,
        "status": bet.result if bet.status == "settled" else bet.status,
        "profit_loss": bet.net_profit if bet.status == "settled" else None,
    }


def list_bets(
    ctx: typer.Context,
    open_: Annotated[bool, typer.Option("--open", help="Only pending bets.")] = False,
    status: Annotated[
        str | None, typer.Option("--status", help="Restrict to pending or settled.")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum rows.")] = None,
) -> None:
    """List bets, newest first."""
    if status is not None and status not in STATUSES:
        raise UsageError(
            f"{status!r} is not a known status.",
            remediation=f"Known statuses: {', '.join(STATUSES)}.",
        )
    if open_ and status not in (None, "pending"):
        raise UsageError("--open and --status settled cannot both be given.")

    resolved = resolve()
    options = options_from(ctx)
    fmt = options.fmt
    since, until = _bounds(options.since, options.until)

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        bets = w.bets.search(
            status=status,
            is_open=open_,
            sportsbook_code=options.sportsbook,
            sport=options.sport,
            since=since,
            until=until,
            limit=limit,
        )

        rows = []
        for bet in bets:
            account = w.accounts.find(bet.sportsbook_account_id)
            legs = w.legs.for_bet(bet.id)
            rows.append(_row(bet, account.label if account else None, legs))

    columns = ["id", "placed", "sportsbook", "selection", "odds", "stake", "status", "profit_loss"]
    render(rows, columns=columns, fmt=fmt, title="bet list")


__all__ = ["list_bets"]
