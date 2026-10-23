"""Recording a bet: one ticket, its legs, its promotions and its provenance.

Backs ``bet add`` in both its forms -- every field as a flag, or prompted for
-- and is what an HTTP route will call. Writes the same ``core.bet`` /
``core.bet_leg`` rows an import would, through the same repositories, so
nothing downstream can tell a bet was typed in rather than imported except
``capture_method``.

A transcribed bet must never claim the precision of one a human typed while
looking at the real numbers (SB-1049): anything but ``manual`` marks the bet
transcribed, and it cites the archived file it was read from.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg, BetLegGroup, BetPromotion, CaptureMethod
from bet.models.ownership import SportsbookAccount
from bet.services._query import Query
from bet.services.capture import check_money, now_if_absent, resolve_account
from bet.services.odds import american_to_decimal
from bet.services.sources import SourceToArchive, check_provenance, cited_source, source_id
from bet.services.tickets import (
    LegGroupInput,
    LegInput,
    PromotionInput,
    build_legs,
    build_promotions,
    ticket_odds,
    wager_kind_for,
)

ZERO = Decimal("0.00")


class AddBetRequest(Query):
    """One ticket as placed.

    ``odds_american`` is the ticket price as the slip shows it -- the base
    price, never a boosted one: a boost is applied to profit at settlement.
    Without it the price is ``leg_group``'s, or else the product of the legs,
    which then must all be priced.

    ``wager_kind`` defaults to ``straight`` for one leg and ``parlay`` for
    more; a stated kind must agree with the leg count. ``placed_at`` defaults
    to now. ``source_file_id`` cites an already-archived source; a file still
    to be archived is handed to :func:`add_bet` instead.
    """

    sportsbook: str
    account_label: str | None = None
    legs: list[LegInput] = Field(min_length=1)
    odds_american: int | None = None
    leg_group: LegGroupInput | None = None
    wager_kind: str | None = None
    promotions: list[PromotionInput] = Field(default_factory=list)
    cash_staked: Decimal = ZERO
    bonus_staked: Decimal = ZERO
    placed_at: AwareDatetime | None = None
    capture_method: CaptureMethod = "manual"
    source_file_id: UUID | None = None


class AddedBet(BaseModel):
    """What was written, and whether the account had to be created for it."""

    model_config = ConfigDict(frozen=True)

    account: SportsbookAccount
    account_is_new: bool
    bet: Bet
    legs: list[BetLeg]
    leg_groups: list[BetLegGroup]
    promotions: list[BetPromotion]


def add_bet(
    w: Warehouse, request: AddBetRequest, *, source: SourceToArchive | None = None
) -> AddedBet:
    """Validate ``request`` and write it as one ticket, all or nothing.

    Every check runs before the write transaction opens, so a refused bet is
    a ``UsageError`` rather than a rollback.
    """
    check_provenance(request.capture_method, source=source, source_file_id=request.source_file_id)
    cited_source(w, request.source_file_id)

    cash_staked = check_money(request.cash_staked, field_name="cash_staked")
    bonus_staked = check_money(request.bonus_staked, field_name="bonus_staked")
    if cash_staked == 0 and bonus_staked == 0:
        raise UsageError("a bet must risk cash or bonus stake.")

    account, account_is_new = resolve_account(
        w, sportsbook_code=request.sportsbook, account_label=request.account_label
    )

    tenant_id, user_id = w.scope.tenant_id, w.scope.user_id
    bet_id = uuid4()
    legs = build_legs(request.legs, tenant_id=tenant_id, user_id=user_id, bet_id=bet_id)
    group = request.leg_group
    odds_american, odds_decimal = ticket_odds(
        legs, request.odds_american, group_odds=group.odds_american if group else None
    )
    kind = wager_kind_for(request.wager_kind, leg_count=len(legs))

    leg_groups: list[BetLegGroup] = []
    if group is not None:
        leg_group = BetLegGroup(
            tenant_id=tenant_id,
            user_id=user_id,
            id=uuid4(),
            bet_id=bet_id,
            category=group.category,
            odds_american=group.odds_american,
            odds_decimal=american_to_decimal(group.odds_american),
        )
        leg_groups.append(leg_group)
        for leg in legs:
            leg.group_id = leg_group.id

    bet = Bet(
        tenant_id=tenant_id,
        user_id=user_id,
        id=bet_id,
        sportsbook_account_id=account.id,
        capture_method=request.capture_method,
        placed_at=now_if_absent(request.placed_at),
        wager_kind=kind,
        cash_staked=cash_staked,
        bonus_staked=bonus_staked,
        odds_american_placed=odds_american,
        odds_decimal_placed=odds_decimal,
        source_file_id=request.source_file_id,
    )
    promotions = build_promotions(
        request.promotions, legs=legs, tenant_id=tenant_id, user_id=user_id, bet_id=bet_id
    )

    with w.transaction() as tx:
        # Archived inside the transaction so an abort leaves no row citing a
        # bet that was never written. The copied bytes survive a rollback,
        # which is harmless: the archive is content-addressed, so the next
        # attempt reuses them rather than duplicating them.
        bet.source_file_id = source_id(
            tx,
            source=source,
            source_file_id=request.source_file_id,
            capture_method=request.capture_method,
        )
        if account_is_new:
            tx.accounts.add(account)
        tx.bets.add(bet)
        if leg_groups:
            tx.leg_groups.add_all(leg_groups)
        tx.legs.add_all(legs)
        if promotions:
            tx.bet_promotions.add_all(promotions)

    return AddedBet(
        account=account,
        account_is_new=account_is_new,
        bet=bet,
        legs=legs,
        leg_groups=leg_groups,
        promotions=promotions,
    )


__all__ = ["AddBetRequest", "AddedBet", "add_bet"]
