"""Arithmetic shared by the analytics modules.

Ratios are rounded half-up to four places, so a ratio reads the same whichever
analytic produced it. Money is summed from the canonical ``net_profit`` and
``cash_staked`` (DATA_DICTIONARY): cash only, never bonus.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from bet.models.bet import Bet

RATIO_PLACES = Decimal("0.0001")
ZERO = Decimal("0.00")


def _ratio(numerator: Decimal | int, denominator: Decimal | int) -> Decimal | None:
    if not denominator:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(RATIO_PLACES, ROUND_HALF_UP)


def _money(bets: Iterable[Bet]) -> tuple[int, Decimal, Decimal, Decimal | None]:
    """(count, cash staked, net profit, cash ROI) over ``bets``."""
    chosen = list(bets)
    staked = sum((b.cash_staked for b in chosen), ZERO)
    net = sum((b.net_profit or ZERO for b in chosen), ZERO)
    return len(chosen), staked, net, _ratio(net, staked)
