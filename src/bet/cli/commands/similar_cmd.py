"""``bet similar`` -- your settled record on bets like a proposed one (SB-1133).

Takes the proposed ticket in exactly the flags ``bet add`` takes, parsed by the
same functions (``bet.services.specs``) and judged by the same rules
(``bet.services.tickets``), so a slip described once can be reviewed with
``similar`` and then recorded with ``add`` without translating anything.
Read-only: nothing about the proposal is written.

The shared ``--sportsbook``/``--sport``/``--since``/``--until``/
``--include-void`` options narrow the *history* being compared against, as
they do for every analytical command.
"""

from __future__ import annotations

from typing import Annotated

import typer

from bet.analytics.similar import COLUMNS
from bet.cli.backend import HistoryFilters, Proposal, open_backend
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.errors import UsageError


def similar(
    ctx: typer.Context,
    leg: Annotated[
        list[str] | None,
        typer.Option(
            "--leg",
            help="One proposed leg, same key=value syntax as `bet add --leg`. Repeat per leg.",
        ),
    ] = None,
    odds: Annotated[
        str | None,
        typer.Option("--odds", help="Ticket-level American odds, as on the slip (base price)."),
    ] = None,
    wager_kind: Annotated[
        str | None,
        typer.Option("--wager-kind", help="Ticket shape, e.g. same_game_parlay."),
    ] = None,
    promo: Annotated[
        list[str] | None,
        typer.Option("--promo", help="A promotion on the proposed ticket, as `bet add --promo`."),
    ] = None,
) -> None:
    """Settled record on bets comparable to a proposed one."""
    leg_specs = leg or []
    if not leg_specs:
        raise UsageError(
            "at least one --leg is required.",
            remediation="Describe the proposed bet the way `bet add` would record it.",
        )

    resolved = resolve()
    options = options_from(ctx)
    thresholds = resolved.settings.thresholds

    with open_backend(resolved) as backend:
        rows = backend.similar(
            Proposal(
                leg_specs=leg_specs, odds=odds, wager_kind=wager_kind, promo_specs=promo or []
            ),
            HistoryFilters(
                sportsbook=options.sportsbook,
                sport=options.sport,
                since=options.since,
                until=options.until,
                include_void=options.include_void,
            ),
            thresholds,
        )

    render(rows, columns=COLUMNS, fmt=options.fmt, title="bet similar")


__all__ = ["similar"]
