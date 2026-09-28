"""``bet similar`` -- your settled record on bets like a proposed one (SB-1133).

Takes the proposed ticket in exactly the flags ``bet add`` takes, parsed by the
same functions, so a slip described once can be reviewed with ``similar`` and
then recorded with ``add`` without translating anything. Read-only: nothing
about the proposal is written.

The shared ``--sportsbook``/``--sport``/``--since``/``--until``/
``--include-void`` options narrow the *history* being compared against, as
they do for every analytical command.
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

import typer

from bet.analytics.similar import COLUMNS, HistoricBet, Proposal, compare, eligible
from bet.cli.commands.add_cmd import (
    _leg_from_fields,
    _parse_leg_spec,
    _parse_promo_spec,
    _parse_wager_kind,
    _promotion_from_fields,
    _ticket_odds,
)
from bet.cli.commands.list_cmd import _bounds
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
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
    since, until = _bounds(options.since, options.until)

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        bet_id = uuid4()
        legs = [
            _leg_from_fields(
                _parse_leg_spec(spec),
                leg_order=order,
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                bet_id=bet_id,
            )
            for order, spec in enumerate(leg_specs, start=1)
        ]
        kind = (
            _parse_wager_kind(wager_kind, leg_count=len(legs))
            if wager_kind
            else ("parlay" if len(legs) > 1 else "straight")
        )
        # An unpriced SGP with no --odds is still worth comparing on every
        # other dimension; it simply has no price band.
        try:
            odds_american: int | None = _ticket_odds(legs, odds)[0]
        except UsageError:
            if odds is not None:
                raise
            odds_american = None
        promo_types = [
            _promotion_from_fields(
                _parse_promo_spec(spec),
                apply_order=order,
                legs=legs,
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                bet_id=bet_id,
            ).promotion_type
            for order, spec in enumerate(promo or [], start=1)
        ]

        history = [
            HistoricBet(
                bet=bet,
                legs=w.legs.for_bet(bet.id),
                promo_types=frozenset(p.promotion_type for p in w.bet_promotions.for_bet(bet.id)),
            )
            for bet in w.bets.search(
                status="settled",
                sportsbook_code=options.sportsbook,
                sport=options.sport,
                since=since,
                until=until,
            )
            if eligible(bet, include_void=options.include_void)
        ]

    rows = compare(
        Proposal(legs=legs, wager_kind=kind, odds_american=odds_american, promo_types=promo_types),
        history,
        min_settled=thresholds.min_settled_bets,
        exploratory_below=thresholds.exploratory_below,
    )
    render(rows, columns=COLUMNS, fmt=options.fmt, title="bet similar")


__all__ = ["similar"]
