"""``bet list`` — list bets, newest first.

Filters split two ways: sportsbook/sport/since/until are the shared
``GlobalOptions`` every analytical command takes (``bet --sportsbook fanduel
list``), read from the Typer context rather than redeclared here. ``--open``,
``--status`` and ``--kind`` are specific to this command.
"""

from __future__ import annotations

from typing import Annotated

import typer

from bet.analytics.card import selection_summary
from bet.cli.backend import open_backend
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.services.bets import BetListing, ListQuery


def _row(listing: BetListing) -> dict[str, object]:
    bet, legs = listing.bet, listing.legs
    return {
        "id": str(bet.id),
        "placed": bet.placed_at.date().isoformat(),
        "sportsbook": listing.account_label or "",
        "kind": bet.wager_kind,
        "selection": selection_summary(legs),
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
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Restrict to one wager kind, e.g. future or same_game_parlay."),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum rows.")] = None,
) -> None:
    """List bets, newest first."""
    options = options_from(ctx)
    query = ListQuery(
        is_open=open_,
        status=status,
        kind=kind,
        sportsbook=options.sportsbook,
        sport=options.sport,
        since=options.since,
        until=options.until,
        limit=limit,
    )
    resolved = resolve()

    with open_backend(resolved) as backend:
        rows = [_row(listing) for listing in backend.list_bets(query)]

    columns = [
        "id",
        "placed",
        "sportsbook",
        "kind",
        "selection",
        "odds",
        "stake",
        "status",
        "profit_loss",
    ]
    render(rows, columns=columns, fmt=options.fmt, title="bet list")


__all__ = ["list_bets"]
