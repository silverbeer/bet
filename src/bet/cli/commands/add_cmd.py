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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, get_args
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
from bet.models.bet import (
    MULTI_LEG,
    Bet,
    BetLeg,
    BetLegGroup,
    BetPromotion,
    CaptureMethod,
    PromotionScope,
    PromotionType,
    WagerKind,
)
from bet.models.ownership import SportsbookAccount
from bet.sources import archive as archive_source

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

console = Console()

FOUR_PLACES = Decimal("0.0001")
TWO_PLACES = Decimal("0.01")

CAPTURE_METHODS: tuple[str, ...] = get_args(CaptureMethod)
WAGER_KINDS: tuple[str, ...] = get_args(WagerKind)
PROMOTION_TYPES: tuple[str, ...] = get_args(PromotionType)
PROMOTION_SCOPES: tuple[str, ...] = get_args(PromotionScope)


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


def _parse_capture_method(raw: str) -> CaptureMethod:
    value = raw.strip().lower()
    if value not in CAPTURE_METHODS:
        raise UsageError(
            f"{raw!r} is not a known capture method.",
            remediation="Known methods: " + ", ".join(sorted(CAPTURE_METHODS)),
        )
    return value  # type: ignore[return-value]


def _parse_wager_kind(raw: str, *, leg_count: int) -> WagerKind:
    """Validate an explicit ``--wager-kind`` against the ticket it describes.

    A kind that contradicts the leg count is a transcription error worth
    catching at entry: ``same_game_parlay`` with one leg means a leg was
    dropped, and ``straight`` with three means they were merged.
    """
    value = raw.strip().lower()
    if value not in WAGER_KINDS:
        raise UsageError(
            f"{raw!r} is not a known wager kind.",
            remediation="Known kinds: " + ", ".join(sorted(WAGER_KINDS)),
        )
    if value in MULTI_LEG and leg_count < 2:
        raise UsageError(
            f"--wager-kind {value} describes a multi-leg ticket, but only "
            f"{leg_count} --leg was given.",
            remediation="Pass one --leg per selection on the slip.",
        )
    if value == "straight" and leg_count > 1:
        raise UsageError(
            f"--wager-kind straight describes a single selection, but {leg_count} --leg were given."
        )
    return value  # type: ignore[return-value]


def _ticket_odds(legs: list[BetLeg], explicit: str | None) -> tuple[int, Decimal]:
    """The ticket's price: stated outright, or derived from fully priced legs.

    ``--odds`` wins whenever it is passed. That is not merely a convenience:
    for a Same Game Parlay the product of the legs is the *wrong* answer even
    when every leg price is known, because correlated legs are repriced as a
    group. The book's own number is the only correct one.

    Deriving requires every leg to carry a price. A product over the subset
    that happens to have one is a number with no meaning, so a partially
    priced ticket is refused rather than silently under-priced.
    """
    if explicit is not None:
        american = _parse_odds(explicit)
        return american, american_to_decimal(american)

    priced = _leg_odds(legs)
    if len(priced) != len(legs):
        raise UsageError(
            f"{len(legs) - len(priced)} of {len(legs)} legs have no odds, so the ticket "
            "price cannot be derived.",
            remediation="Pass --odds with the ticket price shown on the slip.",
        )
    combined = combined_decimal_odds(priced)
    return decimal_to_american(combined), combined


def _check_provenance(capture_method: CaptureMethod, source_file: Path | None) -> None:
    """Refuse the incoherent combination; warn about the merely incomplete one.

    ``manual`` means a human typed the numbers. A file it was captured from is
    a contradiction, not extra detail, so it is an error rather than something
    to quietly accept.

    A path that is not a file fails here too, before the warehouse is opened
    and before any prompting, rather than from inside the write transaction
    where it would surface as a rollback rather than as bad input.

    The reverse — a transcribed bet with nothing archived — is a real loss but
    not a contradiction: the screenshot may genuinely be gone. It warns, so the
    bet is still recorded and the gap is visible, because refusing it would
    cost the bet as well as the evidence.
    """
    if source_file is not None and capture_method == "manual":
        raise UsageError(
            "--source-file contradicts --capture-method manual.",
            remediation="A manually typed bet was not captured from a file. Pass "
            "--capture-method screenshot (or pdf, statement, export) alongside it.",
        )
    if source_file is not None and not source_file.expanduser().is_file():
        raise UsageError(
            f"{source_file} is not a file.",
            remediation="Pass --source-file the path to the screenshot or statement.",
        )
    if source_file is None and capture_method != "manual":
        console.print(
            f"[yellow]Warning:[/yellow] recording a {capture_method} bet with no --source-file. "
            "Nothing will be archived, so these figures cannot be checked against their source."
        )


# --------------------------------------------------------------- leg parsing

LEG_KEYS = {"sport", "league", "market", "selection", "odds", "line", "side", "team", "player"}


def _parse_leg_spec(spec: str) -> dict[str, str]:
    """Parse a ``key=value,key=value`` ``--leg`` spec.

    Keys: sport, league, market, selection, odds, line, side, team, player.
    Matches the fields a single leg prompt collects interactively.

    ``odds`` is optional. ``BetLeg.odds_american`` is nullable by design --
    DraftKings publishes no per-leg price, and FanDuel renders none for a Same
    Game Parlay -- so a leg with no price is a faithful transcription rather
    than an incomplete one. The ticket price then has to arrive via ``--odds``;
    :func:`_ticket_odds` is what refuses the combination that leaves a bet with
    no price at all.
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
    return fields


def _leg_from_fields(
    fields: dict[str, str],
    *,
    leg_order: int,
    tenant_id: object,
    user_id: object,
    bet_id: object,
) -> BetLeg:
    raw_odds = fields.get("odds")
    odds_american = _parse_odds(raw_odds) if raw_odds else None
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
        odds_decimal=american_to_decimal(odds_american) if odds_american is not None else None,
    )


# --------------------------------------------------------- promotion parsing

PROMO_KEYS = {"type", "generosity_pct", "label", "scope", "leg", "triggered", "value_delivered"}


def _parse_promo_spec(spec: str) -> dict[str, str]:
    """Parse a ``key=value,key=value`` ``--promo`` spec."""
    fields: dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise UsageError(
                f"malformed --promo field {chunk!r}.",
                remediation="Use key=value pairs separated by commas, e.g. "
                "--promo 'type=profit_boost,generosity_pct=50'.",
            )
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key not in PROMO_KEYS:
            raise UsageError(
                f"unknown --promo field {key!r}.",
                remediation=f"Known fields: {', '.join(sorted(PROMO_KEYS))}.",
            )
        fields[key] = value.strip()
    if "type" not in fields:
        raise UsageError(f"--promo {spec!r} is missing required field 'type'.")
    return fields


def _parse_triggered(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    raise UsageError(
        f"{raw!r} is not a valid value for 'triggered'.", remediation="Use true|false."
    )


def _promotion_from_fields(
    fields: dict[str, str],
    *,
    apply_order: int,
    legs: list[BetLeg],
    tenant_id: object,
    user_id: object,
    bet_id: object,
) -> BetPromotion:
    """Build one ``BetPromotion``, resolving leg scope to a real leg id.

    ``generosity_pct`` is mandatory on a profit boost. ``apply_boosts`` skips a
    boost whose percentage is null, so one recorded without it would be stored,
    reported as a promotion, and contribute nothing -- worse than refusing it.
    """
    promotion_type = fields["type"].strip().lower()
    if promotion_type not in PROMOTION_TYPES:
        raise UsageError(
            f"{fields['type']!r} is not a known promotion type.",
            remediation="Known types: " + ", ".join(sorted(PROMOTION_TYPES)),
        )

    scope = (fields.get("scope") or "ticket").strip().lower()
    if scope not in PROMOTION_SCOPES:
        raise UsageError(
            f"{scope!r} is not a known promotion scope.",
            remediation="Known scopes: " + ", ".join(sorted(PROMOTION_SCOPES)),
        )

    raw_leg = fields.get("leg")
    if scope == "leg" and not raw_leg:
        raise UsageError(
            "--promo scope=leg needs the leg it applies to.",
            remediation="Add leg=N, the 1-based position of the leg on the slip.",
        )
    if scope == "ticket" and raw_leg:
        raise UsageError("--promo leg=N is only meaningful with scope=leg.")

    bet_leg_id = None
    if raw_leg:
        try:
            leg_order = int(raw_leg)
        except ValueError as exc:
            raise UsageError(f"{raw_leg!r} is not a valid leg number.") from exc
        match = next((leg for leg in legs if leg.leg_order == leg_order), None)
        if match is None:
            raise UsageError(
                f"--promo names leg {leg_order}, but this bet has {len(legs)} legs.",
            )
        bet_leg_id = match.id

    generosity = fields.get("generosity_pct")
    if promotion_type == "profit_boost" and not generosity:
        raise UsageError(
            "a profit boost needs generosity_pct.",
            remediation="Add generosity_pct=50 for a 50% boost. Without it the boost is "
            "recorded but contributes nothing to the economics.",
        )

    return BetPromotion(
        tenant_id=tenant_id,  # type: ignore[arg-type]
        user_id=user_id,  # type: ignore[arg-type]
        id=uuid4(),
        bet_id=bet_id,  # type: ignore[arg-type]
        bet_leg_id=bet_leg_id,
        promotion_type=promotion_type,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        label=fields.get("label") or None,
        apply_order=apply_order,
        generosity_pct=Decimal(generosity) if generosity else None,
        triggered=_parse_triggered(fields["triggered"]) if fields.get("triggered") else None,
        value_delivered=(
            _parse_money(fields["value_delivered"], field_name="value_delivered")
            if fields.get("value_delivered")
            else None
        ),
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
    promotions: list[BetPromotion] = field(default_factory=list)
    leg_groups: list[BetLegGroup] = field(default_factory=list)


def _build_from_flags(
    w: Warehouse,
    conn: DuckDBPyConnection,
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
    odds_american, odds_decimal = _ticket_odds(legs, odds)
    kind: WagerKind = (
        _parse_wager_kind(wager_kind, leg_count=len(legs))
        if wager_kind
        else ("parlay" if len(legs) > 1 else "straight")
    )

    bet = Bet(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=bet_id,
        sportsbook_account_id=account.id,
        capture_method=capture_method,
        placed_at=_parse_placed_at(placed_at),
        wager_kind=kind,
        cash_staked=cash_staked,
        bonus_staked=bonus_staked,
        odds_american_placed=odds_american,
        odds_decimal_placed=odds_decimal,
    )
    promotions = [
        _promotion_from_fields(
            _parse_promo_spec(spec),
            apply_order=order,
            legs=legs,
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            bet_id=bet_id,
        )
        for order, spec in enumerate(promo_specs, start=1)
    ]

    return _Draft(
        account=account,
        account_is_new=account_is_new,
        bet=bet,
        legs=legs,
        promotions=promotions,
    )


def _build_interactively(
    w: Warehouse,
    conn: DuckDBPyConnection,
    *,
    placed_at: str | None,
    capture_method: CaptureMethod,
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

    leg_groups: list[BetLegGroup] = []
    kind: WagerKind = "parlay" if len(legs) > 1 else "straight"
    product = combined_decimal_odds(_leg_odds(legs))
    combined = product

    if len(legs) > 1 and Confirm.ask("Same Game Parlay?", default=False):
        kind = "same_game_parlay"
        group_american = _parse_odds(Prompt.ask("SGP price (American, as the book shows it)"))
        combined = american_to_decimal(group_american)
        group = BetLegGroup(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid4(),
            bet_id=bet_id,
            category=Prompt.ask("Group label", default="") or None,
            odds_american=group_american,
            odds_decimal=combined,
        )
        leg_groups.append(group)
        for leg in legs:
            leg.group_id = group.id

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
    if leg_groups:
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

    bet = Bet(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=bet_id,
        sportsbook_account_id=account.id,
        capture_method=capture_method,
        placed_at=_parse_placed_at(placed_at),
        wager_kind=kind,
        cash_staked=cash_staked,
        bonus_staked=bonus_staked,
        odds_american_placed=decimal_to_american(combined),
        odds_decimal_placed=combined,
    )
    return _Draft(
        account=account,
        account_is_new=account_is_new,
        bet=bet,
        legs=legs,
        leg_groups=leg_groups,
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

    method = _parse_capture_method(capture_method)
    _check_provenance(method, source_file)

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
                odds=odds,
                wager_kind=wager_kind,
                promo_specs=promo or [],
                placed_at=placed_at,
                capture_method=method,
            )
        else:
            draft = _build_interactively(w, conn, placed_at=placed_at, capture_method=method)

        if draft is None:
            console.print("[yellow]Aborted. Nothing was written.[/yellow]")
            return

        archive_dir = resolved.settings.source_archive_dir
        assert archive_dir is not None  # Settings derives it from data_dir

        with w.transaction() as tx:
            # Archived inside the transaction so an abort leaves no row citing
            # a bet that was never written. The copied bytes survive a
            # rollback, which is harmless: the archive is content-addressed,
            # so the next attempt reuses them rather than duplicating them.
            if source_file is not None:
                archived = archive_source(
                    tx, source_file, archive_dir=archive_dir, capture_method=method
                )
                draft.bet.source_file_id = archived.id
            if draft.account_is_new:
                tx.accounts.add(draft.account)
            tx.bets.add(draft.bet)
            if draft.leg_groups:
                tx.leg_groups.add_all(draft.leg_groups)
            tx.legs.add_all(draft.legs)
            if draft.promotions:
                tx.bet_promotions.add_all(draft.promotions)

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
    render([_summary_row(draft.bet, draft.legs)], columns=columns, fmt=fmt, title="bet add")


__all__ = [
    "add",
    "american_to_decimal",
    "combined_decimal_odds",
    "decimal_to_american",
]
