"""Promotion economics.

The boost formula is confirmed on four operators and on both positive and
negative base prices (docs/DATA_DICTIONARY.md 5.2):

    boosted_profit = base_profit * (1 + generosity / 100)

It applies to **profit**, never to stake and never to the return. Applying it to
stake is a silent money bug: the number stays plausible and is wrong on every
boosted bet.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from bet.models.bet import BetPromotion

CENTS = Decimal("0.01")


def to_cents(value: Decimal) -> Decimal:
    """Round half-up to two places. Money leaves this module at cent precision."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def american_to_decimal(american: int) -> Decimal:
    """Convert American odds to decimal odds, exactly.

    Positive is profit on a 100 stake; negative is the stake needed to win 100.
    """
    if american == 0:
        raise ValueError("American odds cannot be zero")
    value = Decimal(american)
    if american > 0:
        return Decimal(1) + value / Decimal(100)
    return Decimal(1) + Decimal(100) / -value


def base_profit(stake: Decimal, american: int) -> Decimal:
    """Profit before any promotion, unrounded."""
    return stake * (american_to_decimal(american) - Decimal(1))


def apply_boosts(base: Decimal, promotions: Sequence[BetPromotion]) -> Decimal:
    """Compose profit boosts in ``apply_order``, each on the running profit.

    Order matters and is assigned by the importer profile, because that is the
    only layer that knows an operator's composition rules. Two boosts composed
    in the wrong order produce a different number that no downstream layer can
    detect as wrong.

    Only promotions that actually fired contribute. ``triggered is None`` means
    the source did not say, and is treated as having fired — a boost is applied
    at placement, unlike insurance, which only pays on a loss.
    """
    running = base
    for promotion in sorted(promotions, key=lambda p: p.apply_order):
        if promotion.promotion_type != "profit_boost":
            continue
        if promotion.triggered is False:
            continue
        if promotion.generosity_pct is None:
            continue
        running *= Decimal(1) + promotion.generosity_pct / Decimal(100)
    return running


def promotional_value(base: Decimal, boosted: Decimal) -> Decimal:
    """What the promotion actually delivered, at cent precision."""
    return to_cents(boosted) - to_cents(base)


__all__ = [
    "american_to_decimal",
    "apply_boosts",
    "base_profit",
    "promotional_value",
    "to_cents",
]
