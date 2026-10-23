"""Reading a slip out of text: ``--leg``/``--promo`` specs, odds and amounts.

How a front end that takes text -- ``bet add`` and ``bet similar`` today --
turns ``key=value,key=value`` into the typed inputs of
``bet.services.tickets``. One parser, so a slip described once can be reviewed
with ``similar`` and recorded with ``add`` without translating anything. A
front end that already has structured data skips this module entirely.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from bet.errors import UsageError
from bet.services.capture import check_money
from bet.services.odds import require_american
from bet.services.tickets import LegInput, PromotionInput

LEG_KEYS = {"sport", "league", "market", "selection", "odds", "line", "side", "team", "player"}
PROMO_KEYS = {"type", "generosity_pct", "label", "scope", "leg", "triggered", "value_delivered"}


# ------------------------------------------------------------------ scalars


def parse_money(raw: str, *, field_name: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise UsageError(f"{raw!r} is not a valid amount for {field_name}.") from exc
    return check_money(value, field_name=field_name, shown=raw)


def parse_odds(raw: str) -> int:
    text = raw.strip()
    try:
        value = int(text)
    except ValueError as exc:
        raise UsageError(f"{raw!r} is not a valid American odds value.") from exc
    return require_american(value)


def parse_triggered(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    raise UsageError(
        f"{raw!r} is not a valid value for 'triggered'.", remediation="Use true|false."
    )


# --------------------------------------------------------------------- legs


def parse_leg_spec(spec: str) -> dict[str, str]:
    """Parse a ``key=value,key=value`` ``--leg`` spec.

    Keys: sport, league, market, selection, odds, line, side, team, player.
    Matches the fields a single leg prompt collects interactively.

    ``odds`` is optional: a leg with no price is a faithful transcription (see
    ``LegInput``). The ticket price then has to arrive via ``--odds``;
    ``tickets.ticket_odds`` is what refuses the combination that leaves a bet
    with no price at all.
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


def leg_input(fields: dict[str, str]) -> LegInput:
    """The typed leg a parsed ``--leg`` spec describes."""
    raw_odds = fields.get("odds")
    return LegInput(
        sport=fields.get("sport") or None,
        league=fields.get("league") or None,
        market_name=fields.get("market") or None,
        selection_name=fields.get("selection") or None,
        side=fields.get("side") or None,
        line_value=Decimal(fields["line"]) if fields.get("line") else None,
        target_team=fields.get("team") or None,
        target_player=fields.get("player") or None,
        odds_american=parse_odds(raw_odds) if raw_odds else None,
    )


# --------------------------------------------------------------- promotions


def parse_promo_spec(spec: str) -> dict[str, str]:
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


def promotion_input(fields: dict[str, str]) -> PromotionInput:
    """The typed promotion a parsed ``--promo`` spec describes.

    Only the text is read here. Whether the type, scope and leg make sense is
    ``tickets.build_promotion``'s to decide, for every front end alike.
    """
    raw_leg = fields.get("leg")
    leg = None
    if raw_leg:
        try:
            leg = int(raw_leg)
        except ValueError as exc:
            raise UsageError(f"{raw_leg!r} is not a valid leg number.") from exc
    generosity = fields.get("generosity_pct")
    return PromotionInput(
        promotion_type=fields["type"],
        scope=fields.get("scope") or None,
        leg=leg,
        label=fields.get("label") or None,
        generosity_pct=Decimal(generosity) if generosity else None,
        triggered=parse_triggered(fields["triggered"]) if fields.get("triggered") else None,
        value_delivered=(
            parse_money(fields["value_delivered"], field_name="value_delivered")
            if fields.get("value_delivered")
            else None
        ),
    )


__all__ = [
    "LEG_KEYS",
    "PROMO_KEYS",
    "leg_input",
    "parse_leg_spec",
    "parse_money",
    "parse_odds",
    "parse_promo_spec",
    "parse_triggered",
    "promotion_input",
]
