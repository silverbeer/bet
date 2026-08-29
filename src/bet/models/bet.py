"""The canonical bet model.

``Bet`` is the ticket aggregate, ``BetLeg`` the atomic selection. Money follows
docs/DATA_DICTIONARY.md: cash and bonus stake never merge, ``net_profit`` is
always ``cash_returned - cash_staked``, and every amount is a ``Decimal``.

Several fields are nullable because they are **operator-dependent**, not because
they are unimportant. Leg odds and leg result values exist at some sportsbooks
and not others, so any analytic built on them covers part of the book and must
say so.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from bet.models.ownership import AwareTimestamps, OwnedModel

Money = Annotated[Decimal, Field(max_digits=12, decimal_places=2)]
FineMoney = Annotated[Decimal, Field(max_digits=12, decimal_places=4)]
Odds = Annotated[Decimal, Field(max_digits=12, decimal_places=4)]

BetStatus = Literal["pending", "settled"]
BetResult = Literal["won", "lost", "push", "void", "partial", "cashed_out"]
WagerKind = Literal[
    "straight",
    "parlay",
    "same_game_parlay",
    "teaser",
    "round_robin",
    "future",
    "system",
    "unknown",
]
SettlementSource = Literal["amount", "status_word", "both_agree"]
CaptureMethod = Literal["export", "api", "statement", "manual", "pdf", "screenshot"]
PromotionType = Literal["profit_boost", "bonus_bet", "insurance", "rewards_currency", "other"]
PromotionState = Literal["awarded", "played", "expired"]
PromotionScope = Literal["ticket", "leg"]

ZERO = Decimal("0.00")

# Multi-leg wager kinds, per reference.wager_kind.
MULTI_LEG: frozenset[str] = frozenset(
    {"parlay", "same_game_parlay", "teaser", "round_robin", "system"}
)


class Bet(OwnedModel, AwareTimestamps):
    """One ticket as accepted by a sportsbook."""

    id: UUID
    sportsbook_account_id: UUID

    external_bet_id: str | None = None
    external_receipt_id: str | None = None

    import_run_id: UUID | None = None
    source_record_id: UUID | None = None
    profile_version: str | None = None
    capture_method: CaptureMethod = "manual"

    placed_at: datetime
    accepted_at: datetime | None = None
    settled_at: datetime | None = None

    status: BetStatus = "pending"
    result: BetResult | None = None
    wager_kind: WagerKind = "straight"
    settlement_source: SettlementSource | None = None

    currency: str = Field(default="USD", min_length=3, max_length=3)
    cash_staked: Money = ZERO
    bonus_staked: Money = ZERO
    cash_returned: Money = ZERO
    cashout_amount: Money | None = None
    rewards_earned: FineMoney = Decimal("0.0000")

    odds_american_placed: int | None = None
    odds_decimal_placed: Odds | None = None
    odds_american_settled: int | None = None
    odds_decimal_settled: Odds | None = None

    version: int = Field(default=1, ge=1)
    is_current: bool = True
    supersedes_id: UUID | None = None
    notes: str | None = None

    @property
    def total_risk(self) -> Decimal:
        """Display convenience. Never an ROI denominator: bonus stake is not risk."""
        return self.cash_staked + self.bonus_staked

    @property
    def net_profit(self) -> Decimal:
        """The one canonical P&L figure. Always cash."""
        return self.cash_returned - self.cash_staked

    @property
    def is_free_bet(self) -> bool:
        """A bet with no cash at stake is excluded from cash ROI entirely."""
        return self.cash_staked == ZERO and self.bonus_staked > ZERO

    @model_validator(mode="after")
    def _settlement_is_coherent(self) -> Self:
        """Mirror the database's constraints so bad state cannot be constructed.

        A settled bet without a result would silently drop out of every
        result-based analytic rather than failing.
        """
        if (self.status == "settled") != (self.result is not None):
            raise ValueError("a settled bet has a result and a pending bet has none")
        if self.status != "settled" and self.settled_at is not None:
            raise ValueError("settled_at is set on a bet that is not settled")
        if self.cash_staked < ZERO or self.bonus_staked < ZERO or self.cash_returned < ZERO:
            raise ValueError("money amounts cannot be negative")
        if self.cash_staked == ZERO and self.bonus_staked == ZERO:
            raise ValueError("a bet must risk cash or bonus stake")
        for name in ("placed_at", "accepted_at", "settled_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        return self


class BetLegGroup(OwnedModel):
    """A same-game group, carrying its own price.

    Orthogonal to the leg hierarchy rather than a third level. The group price
    compared against the product of its legs' prices is what makes the SGP
    correlation adjustment measurable — the only reason to model groups at all.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID
    bet_id: UUID
    external_ref: str | None = None
    category: str | None = None
    odds_american: int | None = None
    odds_decimal: Odds | None = None


class BetLeg(OwnedModel, AwareTimestamps):
    """One selection within a ticket. Carries no money — that belongs to the ticket.

    Attributing money to legs would require inventing an allocation rule the
    operator never applied.
    """

    id: UUID
    bet_id: UUID
    leg_order: int = Field(ge=1)
    group_id: UUID | None = None

    sport: str | None = None
    league: str | None = None
    event_ref: str | None = None
    event_label: str | None = None
    event_starts_at: datetime | None = None

    market_family: str | None = None
    market_name: str | None = None
    selection_name: str | None = None
    side: str | None = None
    line_value: Decimal | None = None

    target_team: str | None = None
    target_player: str | None = None
    is_home: bool | None = None

    # Operator-dependent. Fanatics and the FanDuel API publish these; DraftKings
    # publishes none.
    odds_american: int | None = None
    odds_decimal: Odds | None = None

    result: BetResult | None = None
    # The achieved quantity where the operator publishes one, enabling near-miss
    # analysis. Null on lost and voided legs, and null for every operator that
    # renders only a tick.
    result_value: Decimal | None = None


class Promotion(OwnedModel, AwareTimestamps):
    """A promotion the user holds or was awarded."""

    id: UUID
    sportsbook_code: str
    promotion_type: PromotionType
    state: PromotionState = "awarded"
    external_ref: str | None = None
    label: str | None = None
    face_value: Money | None = None
    generosity_pct: Decimal | None = None
    awarded_at: datetime | None = None
    expires_at: datetime | None = None
    expired_at: datetime | None = None

    @model_validator(mode="after")
    def _expiry_is_recorded_not_inferred(self) -> Self:
        if (self.state == "expired") != (self.expired_at is not None):
            raise ValueError("an expired promotion records when it expired")
        return self


class BetPromotion(OwnedModel):
    """A promotion applied to a ticket, or to one leg of it."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID
    bet_id: UUID
    bet_leg_id: UUID | None = None
    promotion_id: UUID | None = None
    promotion_type: PromotionType
    scope: PromotionScope = "ticket"
    label: str | None = None
    apply_order: int = Field(default=1, ge=1)
    generosity_pct: Decimal | None = None
    # A promotion attached but never paid out is worth something different from
    # one that fired. Conflating them distorts promotion performance.
    triggered: bool | None = None
    value_delivered: Money | None = None

    @model_validator(mode="after")
    def _scope_cannot_lie(self) -> Self:
        if (self.scope == "leg") != (self.bet_leg_id is not None):
            raise ValueError("scope 'leg' requires bet_leg_id, and 'ticket' forbids it")
        return self


__all__ = [
    "MULTI_LEG",
    "Bet",
    "BetLeg",
    "BetLegGroup",
    "BetPromotion",
    "BetResult",
    "BetStatus",
    "CaptureMethod",
    "FineMoney",
    "Money",
    "Odds",
    "Promotion",
    "PromotionScope",
    "PromotionState",
    "PromotionType",
    "SettlementSource",
    "WagerKind",
]
