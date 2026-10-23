"""``bet add`` — bet entry, typed by hand or read off a captured file.

The primary capture method until sportsbook exports land (SB-689): the only
way to record a bet the moment it is placed, with no export file, scrape or
screenshot to wait for. Writes the same ``core.bet``/``core.bet_leg`` rows an
import would, through the same repositories, so nothing downstream can tell
a bet was typed in rather than imported except ``capture_method``.

That last clause is the whole reason ``--capture-method`` exists (SB-1049).
The daily loop is now a photograph of a bet slip, read by an agent, written
through these same flags — and a transcribed bet must never claim the
precision of one a human typed while looking at the real numbers. Pass
``--capture-method screenshot --source-file`` and the bet is tagged as
transcribed and cites the archived image it came from. The default stays
``manual``, so a bet typed at the counter still says so.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from bet.cli.capture import (
    check_provenance,
    choose_account,
    new_account,
    parse_capture_method,
    parse_money,
    parse_timestamp,
)
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg, BetPromotion, CaptureMethod
from bet.services.add import AddBetRequest, add_bet
from bet.services.odds import (
    american_to_decimal,
    combined_decimal_odds,
    decimal_to_american,
)
from bet.services.sources import SourceToArchive
from bet.services.specs import (
    leg_input,
    parse_leg_spec,
    parse_odds,
    parse_promo_spec,
    promotion_input,
)
from bet.services.tickets import (
    LegGroupInput,
    LegInput,
    build_leg,
    build_promotion,
    ticket_odds,
    wager_kind_for,
)

console = Console()

TWO_PLACES = Decimal("0.01")

# The rules moved to bet.services in SB-1395 (odds, tickets, specs) and the
# shared capture helpers to bet.cli.capture in SB-1333; kept here under their
# original names for this module's tests.
_parse_money = parse_money
_new_account = new_account
_parse_odds = parse_odds
_parse_leg_spec = parse_leg_spec
_parse_promo_spec = parse_promo_spec
_parse_wager_kind = wager_kind_for


def _parse_placed_at(raw: str | None) -> datetime:
    return parse_timestamp(raw, option="--placed-at")


def _ticket_odds(legs: list[BetLeg], explicit: str | None) -> tuple[int, Decimal]:
    return ticket_odds(legs, parse_odds(explicit) if explicit is not None else None)


def _leg_from_fields(
    fields: dict[str, str], *, leg_order: int, tenant_id: UUID, user_id: UUID, bet_id: UUID
) -> BetLeg:
    return build_leg(
        leg_input(fields), leg_order=leg_order, tenant_id=tenant_id, user_id=user_id, bet_id=bet_id
    )


def _promotion_from_fields(
    fields: dict[str, str],
    *,
    apply_order: int,
    legs: list[BetLeg],
    tenant_id: UUID,
    user_id: UUID,
    bet_id: UUID,
) -> BetPromotion:
    return build_promotion(
        promotion_input(fields),
        apply_order=apply_order,
        legs=legs,
        tenant_id=tenant_id,
        user_id=user_id,
        bet_id=bet_id,
    )


# ------------------------------------------------------------ account lookup


def _recall_defaults(w: Warehouse) -> tuple[str | None, str | None]:
    """(sportsbook_code, sport) of the most recent bet, for prompt defaults."""
    recent = w.bets.current(limit=1)
    if not recent:
        return None, None
    last = recent[0]
    account = w.accounts.find(last.sportsbook_account_id)
    legs = w.legs.for_bet(last.id)
    sport = legs[0].sport if legs else None
    return (account.sportsbook_code if account else None), sport


# ------------------------------------------------------------------ request


def _request_from_flags(
    *,
    sportsbook: str | None,
    account_label: str | None,
    leg_specs: list[str],
    stake: str | None,
    bonus_stake: str | None,
    odds: str | None,
    wager_kind: str | None,
    promo_specs: list[str],
    placed_at: str | None,
    capture_method: CaptureMethod,
) -> AddBetRequest:
    """Read the flags into a request. Only the text is judged here."""
    if not sportsbook:
        raise UsageError("--sportsbook is required with --non-interactive.")
    if not leg_specs:
        raise UsageError("at least one --leg is required with --non-interactive.")

    cash_staked = _parse_money(stake, field_name="stake") if stake else Decimal("0.00")
    bonus_staked = (
        _parse_money(bonus_stake, field_name="bonus-stake") if bonus_stake else Decimal("0.00")
    )
    if cash_staked == 0 and bonus_staked == 0:
        raise UsageError("a bet must risk cash or bonus stake: pass --stake and/or --bonus-stake.")

    return AddBetRequest(
        sportsbook=sportsbook,
        account_label=account_label,
        legs=[leg_input(parse_leg_spec(spec)) for spec in leg_specs],
        odds_american=parse_odds(odds) if odds is not None else None,
        wager_kind=wager_kind,
        promotions=[promotion_input(parse_promo_spec(spec)) for spec in promo_specs],
        cash_staked=cash_staked,
        bonus_staked=bonus_staked,
        placed_at=_parse_placed_at(placed_at),
        capture_method=capture_method,
    )


def _request_interactively(
    w: Warehouse, *, placed_at: str | None, capture_method: CaptureMethod
) -> AddBetRequest | None:
    default_sportsbook, default_sport = _recall_defaults(w)

    raw_sportsbook = Prompt.ask("Sportsbook", default=default_sportsbook or "fanduel")
    sportsbook_code = raw_sportsbook.strip().lower()
    account = choose_account(w, sportsbook_code=sportsbook_code)

    legs: list[LegInput] = []
    while True:
        sport = Prompt.ask("Sport", default=default_sport or "") or None
        market = Prompt.ask("Wager type") or None
        selection = Prompt.ask("Selection") or None
        odds_american = _parse_odds(Prompt.ask("Odds (American)"))
        legs.append(
            LegInput(
                sport=sport,
                market_name=market,
                selection_name=selection,
                odds_american=odds_american,
            )
        )
        if not Confirm.ask("Add another leg?", default=False):
            break

    leg_group: LegGroupInput | None = None
    wager_kind: str | None = None
    product = combined_decimal_odds(
        [american_to_decimal(leg.odds_american) for leg in legs if leg.odds_american is not None]
    )
    combined = product

    if len(legs) > 1 and Confirm.ask("Same Game Parlay?", default=False):
        wager_kind = "same_game_parlay"
        group_american = _parse_odds(Prompt.ask("SGP price (American, as the book shows it)"))
        combined = american_to_decimal(group_american)
        leg_group = LegGroupInput(
            odds_american=group_american,
            category=Prompt.ask("Group label", default="") or None,
        )

    cash_staked = _parse_money(Prompt.ask("Stake", default="0.00"), field_name="stake")
    bonus_staked = _parse_money(
        Prompt.ask("Free-bet / bonus stake", default="0.00"), field_name="bonus stake"
    )
    if cash_staked == 0 and bonus_staked == 0:
        raise UsageError("a bet must risk cash or bonus stake.")

    payout = ((cash_staked + bonus_staked) * combined).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    console.print()
    for leg in legs:
        odds_text = f"{leg.odds_american:+d}" if leg.odds_american is not None else "?"
        console.print(f"  {leg.selection_name or '?'}  {leg.market_name or ''}  {odds_text}")
    stake_line = f"  Stake: ${cash_staked}"
    if bonus_staked:
        stake_line += f" + ${bonus_staked} bonus"
    console.print(stake_line)
    if leg_group is not None:
        # The whole reason to model groups: the book's group price against the
        # product of its legs is the correlation adjustment, and it is only
        # measurable when both numbers are recorded.
        console.print(
            f"  SGP price: {decimal_to_american(combined):+d}   "
            f"legs multiply to {decimal_to_american(product):+d}"
        )
    console.print(f"  -> potential payout: ${payout}")
    if not Confirm.ask("Confirm?", default=True):
        return None

    return AddBetRequest(
        sportsbook=sportsbook_code,
        account_label=account.label,
        legs=legs,
        leg_group=leg_group,
        wager_kind=wager_kind,
        cash_staked=cash_staked,
        bonus_staked=bonus_staked,
        placed_at=_parse_placed_at(placed_at),
        capture_method=capture_method,
    )


def _summary_row(bet: Bet, legs: list[BetLeg]) -> dict[str, object]:
    return {
        "id": str(bet.id),
        "sportsbook_account_id": str(bet.sportsbook_account_id),
        "wager_kind": bet.wager_kind,
        "legs": len(legs),
        "cash_staked": bet.cash_staked,
        "bonus_staked": bet.bonus_staked,
        "status": bet.status,
        "capture_method": bet.capture_method,
        "source_file_id": str(bet.source_file_id) if bet.source_file_id else None,
    }


# ------------------------------------------------------------------ command


def add(
    ctx: typer.Context,
    non_interactive: Annotated[
        bool,
        typer.Option("--non-interactive", help="Accept every field as a flag; prompt for nothing."),
    ] = False,
    sportsbook: Annotated[
        str | None, typer.Option("--sportsbook", help="Sportsbook code, e.g. fanduel.")
    ] = None,
    account_label: Annotated[
        str | None,
        typer.Option(
            "--account-label", help="Disambiguate an existing account, or name a new one."
        ),
    ] = None,
    leg: Annotated[
        list[str] | None,
        typer.Option(
            "--leg",
            help="One leg as key=value pairs (sport,league,market,selection,odds,line,side,team,"
            "player). Repeat for a parlay. odds is optional: pass --odds when the slip "
            "prices the ticket but not its legs.",
        ),
    ] = None,
    odds: Annotated[
        str | None,
        typer.Option(
            "--odds",
            help="Ticket-level American odds as shown on the slip. Authoritative when "
            "passed; required when any leg has no odds. A Same Game Parlay is priced as "
            "a group, so its ticket price is never the product of its legs.",
        ),
    ] = None,
    wager_kind: Annotated[
        str | None,
        typer.Option(
            "--wager-kind",
            help="Ticket shape, e.g. same_game_parlay. Default: straight for one leg, "
            "parlay for more.",
        ),
    ] = None,
    promo: Annotated[
        list[str] | None,
        typer.Option(
            "--promo",
            help="One promotion as key=value pairs (type,generosity_pct,label,scope,leg,"
            "triggered,value_delivered). Repeat to stack. Odds stay the BASE price: the "
            "boost is applied to profit at settlement, never folded into the price.",
        ),
    ] = None,
    stake: Annotated[str | None, typer.Option("--stake", help="Cash stake.")] = None,
    bonus_stake: Annotated[
        str | None, typer.Option("--bonus-stake", help="Free-bet / bonus stake.")
    ] = None,
    placed_at: Annotated[
        str | None,
        typer.Option("--placed-at", help="ISO 8601 timestamp the bet was placed. Default: now."),
    ] = None,
    capture_method: Annotated[
        str,
        typer.Option(
            "--capture-method",
            help="How these numbers were obtained: manual (typed by a human), screenshot, "
            "pdf, statement, export or api. Anything but manual marks the bet transcribed.",
        ),
    ] = "manual",
    source_file: Annotated[
        Path | None,
        typer.Option(
            "--source-file",
            help="The screenshot or statement this bet was read from. Archived unchanged "
            "and cited by the bet. Requires a non-manual --capture-method.",
        ),
    ] = None,
) -> None:
    """Record a bet the moment it is placed."""
    resolved = resolve()
    fmt = options_from(ctx).fmt

    method = parse_capture_method(capture_method)
    check_provenance(method, source_file)

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        if non_interactive:
            request: AddBetRequest | None = _request_from_flags(
                sportsbook=sportsbook,
                account_label=account_label,
                leg_specs=leg or [],
                stake=stake,
                bonus_stake=bonus_stake,
                odds=odds,
                wager_kind=wager_kind,
                promo_specs=promo or [],
                placed_at=placed_at,
                capture_method=method,
            )
        else:
            request = _request_interactively(w, placed_at=placed_at, capture_method=method)

        if request is None:
            console.print("[yellow]Aborted. Nothing was written.[/yellow]")
            return

        archive_dir = resolved.settings.source_archive_dir
        assert archive_dir is not None  # Settings derives it from data_dir
        source = (
            SourceToArchive(archive_dir=archive_dir, path=source_file)
            if source_file is not None
            else None
        )
        added = add_bet(w, request, source=source)

    columns = [
        "id",
        "sportsbook_account_id",
        "wager_kind",
        "legs",
        "cash_staked",
        "bonus_staked",
        "status",
        "capture_method",
        "source_file_id",
    ]
    render([_summary_row(added.bet, added.legs)], columns=columns, fmt=fmt, title="bet add")


__all__ = [
    "add",
    "american_to_decimal",
    "combined_decimal_odds",
    "decimal_to_american",
]
