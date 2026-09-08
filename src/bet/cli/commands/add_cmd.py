"""``bet add`` — interactive manual bet entry.

The primary capture method until sportsbook exports land (SB-689): the only
way to record a bet the moment it is placed, with no export file, scrape or
screenshot to wait for. Writes the same ``core.bet``/``core.bet_leg`` rows an
import would, through the same repositories, so nothing downstream can tell
a bet was typed in rather than imported except ``capture_method``.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated
from uuid import uuid4

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse, known_sportsbook_codes
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg
from bet.models.ownership import SportsbookAccount

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

console = Console()

FOUR_PLACES = Decimal("0.0001")
TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------- odds math


def american_to_decimal(odds_american: int) -> Decimal:
    """Convert American odds to decimal odds, at 4 decimal places.

    +100 and -100 both land on 2.0000 — the boundary where a bet exactly
    doubles the stake sits between the two formats' sign conventions.
    """
    if odds_american > 0:
        value = 1 + (Decimal(odds_american) / 100)
    else:
        value = 1 + (Decimal(100) / abs(Decimal(odds_american)))
    return value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def decimal_to_american(odds_decimal: Decimal) -> int:
    """The American-odds equivalent of a decimal price.

    Gives combined parlay odds an American figure even though no sportsbook
    publishes one for a multi-leg ticket directly — the product of the legs'
    decimal prices, converted back the same way a single price would be.
    """
    value = (odds_decimal - 1) * 100 if odds_decimal >= 2 else Decimal(-100) / (odds_decimal - 1)
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def combined_decimal_odds(leg_odds: list[Decimal]) -> Decimal:
    """Ticket-level price: the product of its legs' decimal odds."""
    product = Decimal(1)
    for odds in leg_odds:
        product *= odds
    return product.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def _leg_odds(legs: list[BetLeg]) -> list[Decimal]:
    return [leg.odds_decimal for leg in legs if leg.odds_decimal is not None]


def _parse_odds(raw: str) -> int:
    text = raw.strip()
    try:
        value = int(text)
    except ValueError as exc:
        raise UsageError(f"{raw!r} is not a valid American odds value.") from exc
    if -100 < value < 100:
        raise UsageError(
            f"{value} is not a valid American odds value.",
            remediation="American odds are always >= 100 or <= -100.",
        )
    return value


def _parse_money(raw: str, *, field_name: str) -> Decimal:
    try:
        value = Decimal(raw).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise UsageError(f"{raw!r} is not a valid amount for {field_name}.") from exc
    if value < 0:
        raise UsageError(f"{field_name} cannot be negative.")
    return value


def _parse_placed_at(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise UsageError(
            f"{raw!r} is not a valid --placed-at timestamp.",
            remediation="Use ISO 8601, e.g. 2026-09-08T19:30:00-04:00.",
        ) from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------- leg parsing

LEG_KEYS = {"sport", "league", "market", "selection", "odds", "line", "side", "team", "player"}


def _parse_leg_spec(spec: str) -> dict[str, str]:
    """Parse a ``key=value,key=value`` ``--leg`` spec.

    Keys: sport, league, market, selection, odds (required), line, side, team,
    player. Matches the fields a single leg prompt collects interactively.
    """
    fields: dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise UsageError(
                f"malformed --leg field {chunk!r}.",
                remediation="Use key=value pairs separated by commas, e.g. "
                "--leg 'sport=NFL,market=spread,selection=Chiefs -3.5,odds=-110'.",
            )
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key not in LEG_KEYS:
            raise UsageError(
                f"unknown --leg field {key!r}.",
                remediation=f"Known fields: {', '.join(sorted(LEG_KEYS))}.",
            )
        fields[key] = value.strip()
    if "odds" not in fields:
        raise UsageError(f"--leg {spec!r} is missing required field 'odds'.")
    return fields


def _leg_from_fields(
    fields: dict[str, str],
    *,
    leg_order: int,
    tenant_id: object,
    user_id: object,
    bet_id: object,
) -> BetLeg:
    odds_american = _parse_odds(fields["odds"])
    return BetLeg(
        tenant_id=tenant_id,  # type: ignore[arg-type]
        user_id=user_id,  # type: ignore[arg-type]
        id=uuid4(),
        bet_id=bet_id,  # type: ignore[arg-type]
        leg_order=leg_order,
        sport=fields.get("sport") or None,
        league=fields.get("league") or None,
        market_name=fields.get("market") or None,
        selection_name=fields.get("selection") or None,
        side=fields.get("side") or None,
        line_value=Decimal(fields["line"]) if fields.get("line") else None,
        target_team=fields.get("team") or None,
        target_player=fields.get("player") or None,
        odds_american=odds_american,
        odds_decimal=american_to_decimal(odds_american),
    )


# ------------------------------------------------------------ account lookup


def _resolve_account(
    w: Warehouse,
    conn: DuckDBPyConnection,
    *,
    sportsbook_code: str,
    account_label: str | None,
    interactive: bool,
) -> tuple[SportsbookAccount, bool]:
    """Find the account a bet attaches to, creating one if none exists yet.

    Reads only — a newly constructed account is written by the caller inside
    the same transaction as the bet, so aborting at the confirm prompt leaves
    the warehouse untouched. There is no ``sportsbook accounts create``
    command yet (SB-701), so this is the only way one gets made today.
    """
    known = known_sportsbook_codes(conn)
    if sportsbook_code not in known:
        raise UsageError(
            f"{sportsbook_code!r} is not a known sportsbook.",
            remediation="Known sportsbooks: " + ", ".join(sorted(known)),
        )

    accounts = w.accounts.for_sportsbook(sportsbook_code)

    if account_label:
        for account in accounts:
            if account.label == account_label:
                return account, False
        if not interactive:
            return _new_account(w, sportsbook_code, account_label), True
    elif len(accounts) == 1:
        return accounts[0], False
    elif len(accounts) > 1:
        if not interactive:
            raise UsageError(
                f"multiple {sportsbook_code} accounts exist.",
                remediation="Disambiguate with --account-label. Known labels: "
                + ", ".join(a.label for a in accounts),
            )
        chosen = Prompt.ask(f"Account for {sportsbook_code}", choices=[a.label for a in accounts])
        return next(a for a in accounts if a.label == chosen), False

    if account_label:
        label = account_label
    elif interactive:
        label = Prompt.ask("Account label", default=sportsbook_code)
    else:
        label = sportsbook_code
    return _new_account(w, sportsbook_code, label), True


def _new_account(w: Warehouse, sportsbook_code: str, label: str) -> SportsbookAccount:
    return SportsbookAccount(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=uuid4(),
        sportsbook_code=sportsbook_code,
        label=label,
    )


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


# -------------------------------------------------------------------- draft


@dataclass(slots=True)
class _Draft:
    account: SportsbookAccount
    account_is_new: bool
    bet: Bet
    legs: list[BetLeg]


def _build_from_flags(
    w: Warehouse,
    conn: DuckDBPyConnection,
    *,
    sportsbook: str | None,
    account_label: str | None,
    leg_specs: list[str],
    stake: str | None,
    bonus_stake: str | None,
    placed_at: str | None,
) -> _Draft:
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

    account, account_is_new = _resolve_account(
        w, conn, sportsbook_code=sportsbook, account_label=account_label, interactive=False
    )

    bet_id = uuid4()
    legs = [
        _leg_from_fields(
            _parse_leg_spec(spec),
            leg_order=order,
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            bet_id=bet_id,
        )
        for order, spec in enumerate(leg_specs, start=1)
    ]
    combined = combined_decimal_odds(_leg_odds(legs))

    bet = Bet(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=bet_id,
        sportsbook_account_id=account.id,
        capture_method="manual",
        placed_at=_parse_placed_at(placed_at),
        wager_kind="parlay" if len(legs) > 1 else "straight",
        cash_staked=cash_staked,
        bonus_staked=bonus_staked,
        odds_american_placed=decimal_to_american(combined),
        odds_decimal_placed=combined,
    )
    return _Draft(account=account, account_is_new=account_is_new, bet=bet, legs=legs)


def _build_interactively(
    w: Warehouse, conn: DuckDBPyConnection, *, placed_at: str | None
) -> _Draft | None:
    default_sportsbook, default_sport = _recall_defaults(w)

    raw_sportsbook = Prompt.ask("Sportsbook", default=default_sportsbook or "fanduel")
    sportsbook_code = raw_sportsbook.strip().lower()
    account, account_is_new = _resolve_account(
        w, conn, sportsbook_code=sportsbook_code, account_label=None, interactive=True
    )

    bet_id = uuid4()
    legs: list[BetLeg] = []
    leg_order = 1
    while True:
        sport = Prompt.ask("Sport", default=default_sport or "") or None
        market = Prompt.ask("Wager type") or None
        selection = Prompt.ask("Selection") or None
        odds_american = _parse_odds(Prompt.ask("Odds (American)"))
        legs.append(
            BetLeg(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid4(),
                bet_id=bet_id,
                leg_order=leg_order,
                sport=sport,
                market_name=market,
                selection_name=selection,
                odds_american=odds_american,
                odds_decimal=american_to_decimal(odds_american),
            )
        )
        leg_order += 1
        if not Confirm.ask("Add another leg?", default=False):
            break

    cash_staked = _parse_money(Prompt.ask("Stake", default="0.00"), field_name="stake")
    bonus_staked = _parse_money(
        Prompt.ask("Free-bet / bonus stake", default="0.00"), field_name="bonus stake"
    )
    if cash_staked == 0 and bonus_staked == 0:
        raise UsageError("a bet must risk cash or bonus stake.")

    combined = combined_decimal_odds(_leg_odds(legs))
    payout = ((cash_staked + bonus_staked) * combined).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    console.print()
    for leg in legs:
        odds_text = f"{leg.odds_american:+d}" if leg.odds_american is not None else "?"
        console.print(f"  {leg.selection_name or '?'}  {leg.market_name or ''}  {odds_text}")
    stake_line = f"  Stake: ${cash_staked}"
    if bonus_staked:
        stake_line += f" + ${bonus_staked} bonus"
    console.print(stake_line)
    console.print(f"  -> potential payout: ${payout}")
    if not Confirm.ask("Confirm?", default=True):
        return None

    bet = Bet(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=bet_id,
        sportsbook_account_id=account.id,
        capture_method="manual",
        placed_at=_parse_placed_at(placed_at),
        wager_kind="parlay" if len(legs) > 1 else "straight",
        cash_staked=cash_staked,
        bonus_staked=bonus_staked,
        odds_american_placed=decimal_to_american(combined),
        odds_decimal_placed=combined,
    )
    return _Draft(account=account, account_is_new=account_is_new, bet=bet, legs=legs)


def _summary_row(bet: Bet, legs: list[BetLeg]) -> dict[str, object]:
    return {
        "id": str(bet.id),
        "sportsbook_account_id": str(bet.sportsbook_account_id),
        "wager_kind": bet.wager_kind,
        "legs": len(legs),
        "cash_staked": bet.cash_staked,
        "bonus_staked": bet.bonus_staked,
        "status": bet.status,
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
            "player). Repeat for a parlay. odds is required.",
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
) -> None:
    """Record a bet the moment it is placed."""
    resolved = resolve()
    fmt = options_from(ctx).fmt

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)

        if non_interactive:
            draft: _Draft | None = _build_from_flags(
                w,
                conn,
                sportsbook=sportsbook,
                account_label=account_label,
                leg_specs=leg or [],
                stake=stake,
                bonus_stake=bonus_stake,
                placed_at=placed_at,
            )
        else:
            draft = _build_interactively(w, conn, placed_at=placed_at)

        if draft is None:
            console.print("[yellow]Aborted. Nothing was written.[/yellow]")
            return

        with w.transaction() as tx:
            if draft.account_is_new:
                tx.accounts.add(draft.account)
            tx.bets.add(draft.bet)
            tx.legs.add_all(draft.legs)

    columns = [
        "id",
        "sportsbook_account_id",
        "wager_kind",
        "legs",
        "cash_staked",
        "bonus_staked",
        "status",
    ]
    render([_summary_row(draft.bet, draft.legs)], columns=columns, fmt=fmt, title="bet add")


__all__ = [
    "add",
    "american_to_decimal",
    "combined_decimal_odds",
    "decimal_to_american",
]
