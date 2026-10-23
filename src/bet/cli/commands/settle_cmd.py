"""``bet settle`` — record the outcome of a bet.

Mutates the same ``core.bet``/``core.bet_leg`` rows `bet add` created — this
is not a correction (``bets correct``, SB-703, creates a new version) but the
first resolution of a bet that has always been pending.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetResult
from bet.services.settle import (
    RESULTS,
    STAKE_RETURNED,
    SettleRequest,
    cash_returned,
    needs_explicit_return,
    parse_result,
    resolve_result,
    settle_bet,
)

console = Console()

TWO_PLACES = Decimal("0.01")


def _parse_return(raw: str) -> Decimal:
    try:
        value = Decimal(raw).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise UsageError(f"{raw!r} is not a valid amount for --return.") from exc
    if value < 0:
        raise UsageError("--return cannot be negative.")
    return value


def _parse_timestamp(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise UsageError(
            f"{raw!r} is not a valid timestamp.",
            remediation="Use ISO 8601, e.g. 2026-09-08T19:30:00-04:00.",
        ) from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_leg_result_spec(spec: str) -> tuple[int, str]:
    """``2=lost`` as (2, "lost"). Whether ``lost`` is a result is the service's call."""
    if "=" not in spec:
        raise UsageError(
            f"malformed --leg-result {spec!r}.",
            remediation="Use leg_order=result, e.g. --leg-result '2=lost'.",
        )
    order_text, _, result_text = spec.partition("=")
    try:
        leg_order = int(order_text.strip())
    except ValueError as exc:
        raise UsageError(f"{order_text!r} is not a valid leg order.") from exc
    return leg_order, result_text


def _summary_row(bet: Bet) -> dict[str, object]:
    return {
        "id": str(bet.id),
        "result": bet.result,
        "cash_returned": bet.cash_returned,
        "net_profit": bet.net_profit,
        "settled_at": bet.settled_at,
    }


# -------------------------------------------------------------- interactive


def _settle_one_interactively(w: Warehouse, bet: Bet, account_label: str | None) -> bool:
    """Prompt for one bet's settlement. Returns True if it was settled."""
    legs = w.legs.for_bet(bet.id)

    console.print()
    stake_line = f"[bold]{account_label or '?'}[/bold]  staked ${bet.cash_staked}"
    if bet.bonus_staked:
        stake_line += f" + ${bet.bonus_staked} bonus"
    console.print(f"{stake_line}  placed {bet.placed_at.date()}")
    for leg in legs:
        odds_text = f"{leg.odds_american:+d}" if leg.odds_american is not None else "?"
        console.print(
            f"  [{leg.leg_order}] {leg.selection_name or '?'}  {leg.market_name or ''}  {odds_text}"
        )

    if not Confirm.ask("Settle this bet now?", default=True):
        return False

    leg_results: dict[int, BetResult] = {}
    for leg in legs:
        raw = Prompt.ask(f"  leg {leg.leg_order} result", choices=[*RESULTS, ""], default="")
        if raw:
            leg_results[leg.leg_order] = raw  # type: ignore[assignment]

    forced = "lost" if any(r == "lost" for r in leg_results.values()) else None
    if forced is not None:
        raw_result = Prompt.ask("Result", choices=list(RESULTS), default=forced)
    else:
        raw_result = Prompt.ask("Result", choices=list(RESULTS))
    result = resolve_result(parse_result(raw_result), leg_results)

    has_promotions = bool(w.bet_promotions.for_bet(bet.id))
    explicit_return: Decimal | None = None
    if needs_explicit_return(
        bet, result, has_promotions=has_promotions
    ) or result not in STAKE_RETURNED | {"lost"}:
        raw_return = Prompt.ask("Amount returned (blank = computed)", default="")
        if raw_return:
            explicit_return = _parse_return(raw_return)

    returned = cash_returned(bet, result, explicit_return, has_promotions=has_promotions)
    raw_settled_at = Prompt.ask("Settled at (blank = now)", default="")
    settled_at = _parse_timestamp(raw_settled_at or None)

    console.print(f"  -> cash returned: ${returned}  net profit: ${returned - bet.cash_staked}")
    if not Confirm.ask("Confirm?", default=True):
        return False

    settle_bet(
        w,
        SettleRequest(
            bet_id=bet.id,
            result=result,
            cash_returned=explicit_return,
            settled_at=settled_at,
            leg_results=dict(leg_results),
        ),
    )
    return True


def _settle_interactively(w: Warehouse) -> int:
    """Walk pending bets one at a time. Returns how many were settled."""
    settled = 0
    for bet in w.bets.open_bets():
        account = w.accounts.find(bet.sportsbook_account_id)
        if _settle_one_interactively(w, bet, account.label if account else None):
            settled += 1
    return settled


# ------------------------------------------------------------------ command


def settle(
    ctx: typer.Context,
    bet_id: Annotated[
        str | None,
        typer.Argument(help="Bet id to settle. Omit to walk open bets one at a time."),
    ] = None,
    result: Annotated[
        str | None, typer.Option("--result", help="won|lost|push|void|partial|cashed_out.")
    ] = None,
    return_: Annotated[
        str | None, typer.Option("--return", help="Override the computed amount returned.")
    ] = None,
    settled_at: Annotated[
        str | None, typer.Option("--settled-at", help="ISO 8601 timestamp. Default: now.")
    ] = None,
    leg_result: Annotated[
        list[str] | None,
        typer.Option("--leg-result", help="leg_order=result, repeatable."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-settle an already-settled bet.")
    ] = False,
) -> None:
    """Record the outcome of a bet."""
    resolved = resolve()
    fmt = options_from(ctx).fmt

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        if bet_id is None:
            count = _settle_interactively(w)
            console.print(f"[green]{count} bet(s) settled.[/green]")
            return

        try:
            bet_uuid = UUID(bet_id)
        except ValueError as exc:
            raise UsageError(f"{bet_id!r} is not a valid bet id.") from exc

        settled = settle_bet(
            w,
            SettleRequest(
                bet_id=bet_uuid,
                result=result,
                cash_returned=_parse_return(return_) if return_ else None,
                settled_at=_parse_timestamp(settled_at),
                leg_results=dict(_parse_leg_result_spec(spec) for spec in (leg_result or [])),
                force=force,
            ),
        )

    render([_summary_row(settled.bet)], fmt=fmt, title="bet settle")


__all__ = ["settle"]
