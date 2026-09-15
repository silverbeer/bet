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
from bet.models.bet import Bet, BetLeg, BetResult

console = Console()

TWO_PLACES = Decimal("0.01")
RESULTS: tuple[BetResult, ...] = ("won", "lost", "push", "void", "partial", "cashed_out")

# push/void return the stake by a fixed rule (DATA_DICTIONARY.md 4.2); a
# --return for either would just contradict that rule, so it's rejected.
STAKE_RETURNED = {"push", "void"}
# No formula exists for these -- an operator's own settlement is the only
# source of truth for what they actually paid.
RETURN_REQUIRED = {"partial", "cashed_out"}


def _parse_result(raw: str) -> BetResult:
    value = raw.strip().lower()
    if value not in RESULTS:
        raise UsageError(
            f"{raw!r} is not a known result.",
            remediation=f"Known results: {', '.join(RESULTS)}.",
        )
    return value


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


def _parse_leg_result_spec(spec: str) -> tuple[int, BetResult]:
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
    return leg_order, _parse_result(result_text)


def _resolve_result(result: BetResult | None, leg_results: dict[int, BetResult]) -> BetResult:
    """A lost leg forces the ticket lost; otherwise --result must be given.

    Anything richer -- a parlay that wins with one pushed leg, repriced odds
    from a voided leg -- has no formula here and stays out of scope: it's
    normalization work (SB-711), not manual settlement. --return covers it in
    the meantime.
    """
    if any(r == "lost" for r in leg_results.values()):
        if result is not None and result != "lost":
            raise UsageError(
                "a leg was recorded lost, so the ticket cannot settle as anything else.",
                remediation="Pass --result lost, or omit --result and let it default.",
            )
        return "lost"
    if result is None:
        raise UsageError("--result is required (no leg forced a result).")
    return result


def _needs_explicit_return(bet: Bet, result: BetResult, *, has_promotions: bool = False) -> bool:
    """Whether the payout has to be supplied rather than computed.

    A promoted bet is one of those cases. ``odds_*_placed`` holds the **base**
    price (DATA_DICTIONARY 9.2) and a profit boost multiplies profit, not the
    price, so ``stake * placed_odds`` computes the *unboosted* payout. That is
    a plausible, smaller, and undetectably wrong number on every boosted win --
    exactly the silent money bug ``settlement.promotions`` opens by warning
    about. Until ``apply_boosts`` is wired into settlement, the slip's own
    figure is the only trustworthy source.
    """
    if result in RETURN_REQUIRED:
        return True
    if result == "won":
        return bet.bonus_staked > 0 or bet.odds_decimal_placed is None or has_promotions
    return False


def _cash_returned(
    bet: Bet,
    result: BetResult,
    explicit_return: Decimal | None,
    *,
    has_promotions: bool = False,
) -> Decimal:
    """Compute cash_returned for a result, or use the explicit override.

    won: auto-computed only for a plain cash bet with stored placed odds --
    stake * combined decimal odds. A boosted price or a free bet's
    stake-excluded payout has no single formula here; --return is required.
    A bet carrying promotions is required too -- placed odds are the base
    price, so computing from them would drop the boost (SB-1084). Wiring
    ``settlement.promotions.apply_boosts`` in here is what would make it
    automatic.
    lost: nothing is returned.
    push / void: the stake, per DATA_DICTIONARY.md 4.2.
    partial / cashed_out: no formula exists; --return is mandatory.
    """
    if result == "lost":
        if explicit_return is not None:
            raise UsageError("--return is not used with a lost result.")
        return Decimal("0.00")
    if result in STAKE_RETURNED:
        if explicit_return is not None:
            raise UsageError(f"--return is not used with a {result} result.")
        return bet.cash_staked
    if _needs_explicit_return(bet, result, has_promotions=has_promotions):
        if explicit_return is None:
            raise UsageError(
                f"--return is required to settle this {result} result.",
                remediation="There is no formula for it -- supply the amount actually paid.",
            )
        return explicit_return
    if explicit_return is not None:
        return explicit_return
    assert bet.odds_decimal_placed is not None  # guarded by _needs_explicit_return above
    return (bet.cash_staked * bet.odds_decimal_placed).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _settled_bet(
    bet: Bet, *, result: BetResult, cash_returned: Decimal, settled_at: datetime
) -> Bet:
    """A fresh ``Bet`` built from the existing row's fields plus the outcome.

    Goes through ``Bet(**...)`` rather than mutating in place, so the
    settlement-coherence validator runs on the result exactly as it would for
    a freshly imported row.
    """
    return Bet(
        **{
            **bet.model_dump(),
            "status": "settled",
            "result": result,
            "settled_at": settled_at,
            "cash_returned": cash_returned,
            "cashout_amount": cash_returned if result == "cashed_out" else bet.cashout_amount,
            "updated_at": datetime.now(UTC),
        }
    )


def _settled_leg(leg: BetLeg, result: BetResult) -> BetLeg:
    return BetLeg(**{**leg.model_dump(), "result": result, "updated_at": datetime.now(UTC)})


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
    result = _resolve_result(_parse_result(raw_result), leg_results)

    has_promotions = bool(w.bet_promotions.for_bet(bet.id))
    explicit_return: Decimal | None = None
    if _needs_explicit_return(
        bet, result, has_promotions=has_promotions
    ) or result not in STAKE_RETURNED | {"lost"}:
        raw_return = Prompt.ask("Amount returned (blank = computed)", default="")
        if raw_return:
            explicit_return = _parse_return(raw_return)

    cash_returned = _cash_returned(bet, result, explicit_return, has_promotions=has_promotions)
    raw_settled_at = Prompt.ask("Settled at (blank = now)", default="")
    settled_at = _parse_timestamp(raw_settled_at or None)

    console.print(
        f"  -> cash returned: ${cash_returned}  net profit: ${cash_returned - bet.cash_staked}"
    )
    if not Confirm.ask("Confirm?", default=True):
        return False

    updated = _settled_bet(bet, result=result, cash_returned=cash_returned, settled_at=settled_at)
    with w.transaction() as tx:
        tx.bets.update(updated)
        for leg in legs:
            if leg.leg_order in leg_results:
                tx.legs.update(_settled_leg(leg, leg_results[leg.leg_order]))
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

        bet = w.bets.get(bet_uuid)
        if bet.status == "settled" and not force:
            raise UsageError(
                f"bet {bet.id} is already settled.",
                remediation="Pass --force to re-settle it.",
            )

        leg_results = dict(_parse_leg_result_spec(spec) for spec in (leg_result or []))
        legs = w.legs.for_bet(bet.id)
        unknown = set(leg_results) - {leg.leg_order for leg in legs}
        if unknown:
            raise UsageError(f"unknown leg order(s) for this bet: {sorted(unknown)}.")

        resolved_result = _resolve_result(_parse_result(result) if result else None, leg_results)
        explicit_return = _parse_return(return_) if return_ else None
        cash_returned = _cash_returned(
            bet,
            resolved_result,
            explicit_return,
            has_promotions=bool(w.bet_promotions.for_bet(bet.id)),
        )

        updated = _settled_bet(
            bet,
            result=resolved_result,
            cash_returned=cash_returned,
            settled_at=_parse_timestamp(settled_at),
        )
        with w.transaction() as tx:
            tx.bets.update(updated)
            for leg in legs:
                if leg.leg_order in leg_results:
                    tx.legs.update(_settled_leg(leg, leg_results[leg.leg_order]))

    render([_summary_row(updated)], fmt=fmt, title="bet settle")


__all__ = ["settle"]
