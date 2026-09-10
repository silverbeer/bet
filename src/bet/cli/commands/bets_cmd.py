"""``bet bets`` — record-level inspection: one bet in full, open bets, search.

``bet bets correct`` stays a stub here (SB-703); everything else in this
group is real. ``bet list`` (the root command, SB-812) is the general
listing -- ``bets open`` is the same query with ``--open`` fixed on, kept as
its own command because the planned tree names it separately.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer

from bet.cli.commands.list_cmd import _bounds, _row
from bet.cli.context import options_from
from bet.cli.output import render
from bet.cli.tree import NotImplementedYetError
from bet.config import resolve
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError

app = typer.Typer(name="bets", help="Query individual bets.", no_args_is_help=True)

LIST_COLUMNS = ["id", "placed", "sportsbook", "selection", "odds", "stake", "status", "profit_loss"]


@app.command("show")
def show(ctx: typer.Context, bet_id: Annotated[str, typer.Argument(help="Bet id.")]) -> None:
    """Show one bet in full: legs, promotions, settlement, and provenance."""
    try:
        bet_uuid = UUID(bet_id)
    except ValueError as exc:
        raise UsageError(f"{bet_id!r} is not a valid bet id.") from exc

    resolved = resolve()
    fmt = options_from(ctx).fmt

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        bet = w.bets.get(bet_uuid)
        account = w.accounts.find(bet.sportsbook_account_id)
        legs = w.legs.for_bet(bet.id)
        promotions = w.bet_promotions.for_bet(bet.id)
        provenance = w.bets.provenance(bet.id)

    rows = [
        {
            "bet_id": str(bet.id),
            "sportsbook": account.label if account else "",
            "placed_at": bet.placed_at,
            "status": bet.status,
            "result": bet.result,
            "cash_staked": bet.cash_staked,
            "bonus_staked": bet.bonus_staked,
            "cash_returned": bet.cash_returned,
            "net_profit": bet.net_profit if bet.status == "settled" else None,
            "leg_order": leg.leg_order,
            "sport": leg.sport,
            "market": leg.market_name,
            "selection": leg.selection_name,
            "odds": leg.odds_american,
            "leg_result": leg.result,
            "promotions": len(promotions),
            "capture_method": bet.capture_method,
            "import_run_id": provenance["import_run_id"],
            "source_record_id": provenance["source_record_id"],
            "source_file_id": provenance["source_file_id"],
            "profile_version": provenance["profile_version"],
            "version": bet.version,
        }
        for leg in legs
    ]
    columns = [
        "bet_id",
        "sportsbook",
        "placed_at",
        "status",
        "result",
        "cash_staked",
        "bonus_staked",
        "cash_returned",
        "net_profit",
        "leg_order",
        "sport",
        "market",
        "selection",
        "odds",
        "leg_result",
        "promotions",
        "capture_method",
        "import_run_id",
        "source_record_id",
        "source_file_id",
        "profile_version",
        "version",
    ]
    render(rows, columns=columns, fmt=fmt, title=f"bet {bet.id}")


@app.command("open")
def open_bets(ctx: typer.Context) -> None:
    """List unsettled bets. Equivalent to ``bet list --open``."""
    resolved = resolve()
    options = options_from(ctx)
    since, until = _bounds(options.since, options.until)

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        bets = w.bets.search(
            is_open=True,
            sportsbook_code=options.sportsbook,
            sport=options.sport,
            since=since,
            until=until,
        )
        rows = []
        for bet in bets:
            account = w.accounts.find(bet.sportsbook_account_id)
            legs = w.legs.for_bet(bet.id)
            rows.append(_row(bet, account.label if account else None, legs))

    render(rows, columns=LIST_COLUMNS, fmt=options.fmt, title="bet bets open")


@app.command("search")
def search(
    ctx: typer.Context,
    query: Annotated[
        str, typer.Argument(help="Text to match in selection, market, or team/player.")
    ],
) -> None:
    """Free-text search over selections and team/player/market labels.

    A plain substring match over what was actually typed or extracted, not
    entity resolution -- there's no controlled vocabulary to resolve against
    yet (SB-747, SB-768).
    """
    resolved = resolve()
    options = options_from(ctx)
    since, until = _bounds(options.since, options.until)

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        bets = w.bets.search(
            text=query,
            sportsbook_code=options.sportsbook,
            sport=options.sport,
            since=since,
            until=until,
        )
        rows = []
        for bet in bets:
            account = w.accounts.find(bet.sportsbook_account_id)
            legs = w.legs.for_bet(bet.id)
            rows.append(_row(bet, account.label if account else None, legs))

    render(rows, columns=LIST_COLUMNS, fmt=options.fmt, title=f"bet bets search {query!r}")


@app.command("correct", help="Record a correction to a bet.  [not yet implemented — SB-703]")
def correct(bet_id: str) -> None:
    raise NotImplementedYetError(
        "`bets correct` is not implemented yet.",
        remediation="Tracked by SB-703.",
    )


__all__ = ["app"]
