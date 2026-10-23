"""A ticket's shape: its legs, its price, its wager kind and its promotions.

Shared by recording a bet (``bet.services.add``) and reviewing a proposed one
(``bet similar``), so a slip is judged by the same rules whichever it is for.
The inputs are typed; reading them out of ``--leg``/``--promo`` text is
``bet.services.specs``.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import get_args
from uuid import UUID, uuid4

from bet.errors import UsageError
from bet.models.bet import (
    MULTI_LEG,
    BetLeg,
    BetPromotion,
    PromotionScope,
    PromotionType,
    WagerKind,
)
from bet.services._query import Query
from bet.services.capture import check_money
from bet.services.odds import (
    american_to_decimal,
    combined_decimal_odds,
    decimal_to_american,
    require_american,
)

WAGER_KINDS: tuple[str, ...] = get_args(WagerKind)
PROMOTION_TYPES: tuple[str, ...] = get_args(PromotionType)
PROMOTION_SCOPES: tuple[str, ...] = get_args(PromotionScope)


class LegInput(Query):
    """One selection on the slip. Every field is optional, the price included.

    ``BetLeg.odds_american`` is nullable by design -- DraftKings publishes no
    per-leg price, and FanDuel renders none for a Same Game Parlay -- so a leg
    with no price is a faithful transcription rather than an incomplete one.
    """

    sport: str | None = None
    league: str | None = None
    market_name: str | None = None
    selection_name: str | None = None
    side: str | None = None
    line_value: Decimal | None = None
    target_team: str | None = None
    target_player: str | None = None
    odds_american: int | None = None


class LegGroupInput(Query):
    """A price the book quotes for every leg together: a Same Game Parlay."""

    odds_american: int
    category: str | None = None


class PromotionInput(Query):
    """One promotion on the ticket, as transcribed.

    ``promotion_type`` and ``scope`` are plain strings so an unknown value is
    refused with the known ones. ``leg`` is the 1-based position of the leg a
    ``scope="leg"`` promotion applies to.
    """

    promotion_type: str
    scope: str | None = None
    leg: int | None = None
    label: str | None = None
    generosity_pct: Decimal | None = None
    triggered: bool | None = None
    value_delivered: Decimal | None = None


# --------------------------------------------------------------------- legs


def build_leg(
    leg: LegInput,
    *,
    leg_order: int,
    tenant_id: UUID,
    user_id: UUID,
    bet_id: UUID,
) -> BetLeg:
    odds_american = require_american(leg.odds_american) if leg.odds_american is not None else None
    return BetLeg(
        tenant_id=tenant_id,
        user_id=user_id,
        id=uuid4(),
        bet_id=bet_id,
        leg_order=leg_order,
        sport=leg.sport,
        league=leg.league,
        market_name=leg.market_name,
        selection_name=leg.selection_name,
        side=leg.side,
        line_value=leg.line_value,
        target_team=leg.target_team,
        target_player=leg.target_player,
        odds_american=odds_american,
        odds_decimal=american_to_decimal(odds_american) if odds_american is not None else None,
    )


def build_legs(
    legs: Sequence[LegInput], *, tenant_id: UUID, user_id: UUID, bet_id: UUID
) -> list[BetLeg]:
    """The ticket's legs, in slip order from 1."""
    return [
        build_leg(leg, leg_order=order, tenant_id=tenant_id, user_id=user_id, bet_id=bet_id)
        for order, leg in enumerate(legs, start=1)
    ]


# ---------------------------------------------------------------- the price


def leg_odds(legs: Sequence[BetLeg]) -> list[Decimal]:
    return [leg.odds_decimal for leg in legs if leg.odds_decimal is not None]


def ticket_odds(
    legs: Sequence[BetLeg], explicit: int | None, *, group_odds: int | None = None
) -> tuple[int, Decimal]:
    """The ticket's price: stated outright, quoted for its leg group, or derived.

    An explicit price wins whenever it is given. That is not merely a
    convenience: for a Same Game Parlay the product of the legs is the *wrong*
    answer even when every leg price is known, because correlated legs are
    repriced as a group. The book's own number is the only correct one. A
    leg group's price is that number too, stored as its decimal equivalent.

    Deriving requires every leg to carry a price. A product over the subset
    that happens to have one is a number with no meaning, so a partially
    priced ticket is refused rather than silently under-priced.
    """
    if explicit is not None:
        american = require_american(explicit)
        return american, american_to_decimal(american)
    if group_odds is not None:
        combined = american_to_decimal(require_american(group_odds))
        return decimal_to_american(combined), combined

    priced = leg_odds(legs)
    if len(priced) != len(legs):
        raise UsageError(
            f"{len(legs) - len(priced)} of {len(legs)} legs have no odds, so the ticket "
            "price cannot be derived.",
            remediation="Pass --odds with the ticket price shown on the slip.",
        )
    combined = combined_decimal_odds(priced)
    return decimal_to_american(combined), combined


# ----------------------------------------------------------- the wager kind


def wager_kind_for(raw: str | None, *, leg_count: int) -> WagerKind:
    """The ticket's kind: as stated, checked against its legs, or by leg count.

    A kind that contradicts the leg count is a transcription error worth
    catching at entry: ``same_game_parlay`` with one leg means a leg was
    dropped, and ``straight`` with three means they were merged.
    """
    if not raw:
        return "parlay" if leg_count > 1 else "straight"
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


# --------------------------------------------------------------- promotions


def build_promotion(
    promotion: PromotionInput,
    *,
    apply_order: int,
    legs: Sequence[BetLeg],
    tenant_id: UUID,
    user_id: UUID,
    bet_id: UUID,
) -> BetPromotion:
    """Build one ``BetPromotion``, resolving leg scope to a real leg id.

    ``generosity_pct`` is mandatory on a profit boost. ``apply_boosts`` skips a
    boost whose percentage is null, so one recorded without it would be stored,
    reported as a promotion, and contribute nothing -- worse than refusing it.
    """
    promotion_type = promotion.promotion_type.strip().lower()
    if promotion_type not in PROMOTION_TYPES:
        raise UsageError(
            f"{promotion.promotion_type!r} is not a known promotion type.",
            remediation="Known types: " + ", ".join(sorted(PROMOTION_TYPES)),
        )

    scope = (promotion.scope or "ticket").strip().lower()
    if scope not in PROMOTION_SCOPES:
        raise UsageError(
            f"{scope!r} is not a known promotion scope.",
            remediation="Known scopes: " + ", ".join(sorted(PROMOTION_SCOPES)),
        )

    if scope == "leg" and promotion.leg is None:
        raise UsageError(
            "--promo scope=leg needs the leg it applies to.",
            remediation="Add leg=N, the 1-based position of the leg on the slip.",
        )
    if scope == "ticket" and promotion.leg is not None:
        raise UsageError("--promo leg=N is only meaningful with scope=leg.")

    bet_leg_id = None
    if promotion.leg is not None:
        match = next((leg for leg in legs if leg.leg_order == promotion.leg), None)
        if match is None:
            raise UsageError(
                f"--promo names leg {promotion.leg}, but this bet has {len(legs)} legs.",
            )
        bet_leg_id = match.id

    if promotion_type == "profit_boost" and promotion.generosity_pct is None:
        raise UsageError(
            "a profit boost needs generosity_pct.",
            remediation="Add generosity_pct=50 for a 50% boost. Without it the boost is "
            "recorded but contributes nothing to the economics.",
        )

    return BetPromotion(
        tenant_id=tenant_id,
        user_id=user_id,
        id=uuid4(),
        bet_id=bet_id,
        bet_leg_id=bet_leg_id,
        promotion_type=promotion_type,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        label=promotion.label,
        apply_order=apply_order,
        generosity_pct=promotion.generosity_pct,
        triggered=promotion.triggered,
        value_delivered=(
            check_money(promotion.value_delivered, field_name="value_delivered")
            if promotion.value_delivered is not None
            else None
        ),
    )


def build_promotions(
    promotions: Sequence[PromotionInput],
    *,
    legs: Sequence[BetLeg],
    tenant_id: UUID,
    user_id: UUID,
    bet_id: UUID,
) -> list[BetPromotion]:
    """The ticket's promotions, applied in the order given from 1."""
    return [
        build_promotion(
            promotion,
            apply_order=order,
            legs=legs,
            tenant_id=tenant_id,
            user_id=user_id,
            bet_id=bet_id,
        )
        for order, promotion in enumerate(promotions, start=1)
    ]


__all__ = [
    "PROMOTION_SCOPES",
    "PROMOTION_TYPES",
    "WAGER_KINDS",
    "LegGroupInput",
    "LegInput",
    "PromotionInput",
    "build_leg",
    "build_legs",
    "build_promotion",
    "build_promotions",
    "leg_odds",
    "ticket_odds",
    "wager_kind_for",
]
