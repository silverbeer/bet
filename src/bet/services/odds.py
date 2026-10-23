"""Odds arithmetic: American and decimal prices, and a ticket's combined price."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from bet.errors import UsageError

FOUR_PLACES = Decimal("0.0001")


def require_american(value: int) -> int:
    """Refuse a value no American price can take: they are >= 100 or <= -100."""
    if -100 < value < 100:
        raise UsageError(
            f"{value} is not a valid American odds value.",
            remediation="American odds are always >= 100 or <= -100.",
        )
    return value


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


__all__ = [
    "FOUR_PLACES",
    "american_to_decimal",
    "combined_decimal_odds",
    "decimal_to_american",
    "require_american",
]
