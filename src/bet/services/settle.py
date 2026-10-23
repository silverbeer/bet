"""Settling a bet: its result, what it returned, and its legs' results.

Backs ``bet settle``. Mutates the same ``core.bet``/``core.bet_leg`` rows
``bet add`` created -- this is not a correction (``bets correct``, SB-703,
creates a new version) but the first resolution of a bet that has always been
pending.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg, BetResult
from bet.services._query import Query
from bet.services.capture import now_if_absent

TWO_PLACES = Decimal("0.01")
RESULTS: tuple[BetResult, ...] = ("won", "lost", "push", "void", "partial", "cashed_out")

# push/void return the stake by a fixed rule (DATA_DICTIONARY.md 4.2); a
# --return for either would just contradict that rule, so it's rejected.
STAKE_RETURNED = {"push", "void"}
# No formula exists for these -- an operator's own settlement is the only
# source of truth for what they actually paid.
RETURN_REQUIRED = {"partial", "cashed_out"}
# Ticket outcomes that are also a leg's outcome. A cash-out or partial settles
# the ticket without deciding the selection, so it says nothing about the leg.
LEG_DECIDING: frozenset[str] = frozenset({"won", "lost", "push", "void"})


class SettleRequest(Query):
    """The outcome of one bet.

    ``result`` and the values of ``leg_results`` (keyed by leg order) are
    plain strings so an unknown one is refused with the known ones. A lost leg
    forces the ticket lost, so ``result`` may then be omitted. ``cash_returned``
    overrides the computed return; ``settled_at`` defaults to now. ``force``
    re-settles a bet that is already settled.
    """

    bet_id: UUID
    result: str | None = None
    cash_returned: Decimal | None = None
    settled_at: AwareDatetime | None = None
    leg_results: dict[int, str] = Field(default_factory=dict)
    force: bool = False


class SettledBet(BaseModel):
    """The bet as settled, and every one of its legs after it."""

    model_config = ConfigDict(frozen=True)

    bet: Bet
    legs: list[BetLeg]


# -------------------------------------------------------------------- rules


def parse_result(raw: str) -> BetResult:
    value = raw.strip().lower()
    if value not in RESULTS:
        raise UsageError(
            f"{raw!r} is not a known result.",
            remediation=f"Known results: {', '.join(RESULTS)}.",
        )
    return value


def resolve_result(result: BetResult | None, leg_results: dict[int, BetResult]) -> BetResult:
    """A lost leg forces the ticket lost; otherwise --result must be given.

    Anything richer -- a parlay that wins with one pushed leg, repriced odds
    from a voided leg -- has no formula here and stays out of scope: it's
    normalization work (SB-711), not manual settlement. --return covers it in
    the meantime.
    """
    if any(r == "lost" for r in leg_results.values()):
        if result is not None and result != "lost":
            raise UsageError(
                "a leg was recorded lost, so the ticket cannot settle as anything else.",
                remediation="Pass --result lost, or omit --result and let it default.",
            )
        return "lost"
    if result is None:
        raise UsageError("--result is required (no leg forced a result).")
    return result


def needs_explicit_return(bet: Bet, result: BetResult, *, has_promotions: bool = False) -> bool:
    """Whether the payout has to be supplied rather than computed.

    A promoted bet is one of those cases. ``odds_*_placed`` holds the **base**
    price (DATA_DICTIONARY 9.2) and a profit boost multiplies profit, not the
    price, so ``stake * placed_odds`` computes the *unboosted* payout. That is
    a plausible, smaller, and undetectably wrong number on every boosted win --
    exactly the silent money bug ``settlement.promotions`` opens by warning
    about. Until ``apply_boosts`` is wired into settlement, the slip's own
    figure is the only trustworthy source.
    """
    if result in RETURN_REQUIRED:
        return True
    if result == "won":
        return bet.bonus_staked > 0 or bet.odds_decimal_placed is None or has_promotions
    return False


def cash_returned(
    bet: Bet,
    result: BetResult,
    explicit_return: Decimal | None,
    *,
    has_promotions: bool = False,
) -> Decimal:
    """Compute cash_returned for a result, or use the explicit override.

    won: auto-computed only for a plain cash bet with stored placed odds --
    stake * combined decimal odds. A boosted price or a free bet's
    stake-excluded payout has no single formula here; --return is required.
    A bet carrying promotions is required too -- placed odds are the base
    price, so computing from them would drop the boost (SB-1084). Wiring
    ``settlement.promotions.apply_boosts`` in here is what would make it
    automatic.
    lost: nothing is returned.
    push / void: the stake, per DATA_DICTIONARY.md 4.2.
    partial / cashed_out: no formula exists; --return is mandatory.
    """
    if result == "lost":
        if explicit_return is not None:
            raise UsageError("--return is not used with a lost result.")
        return Decimal("0.00")
    if result in STAKE_RETURNED:
        if explicit_return is not None:
            raise UsageError(f"--return is not used with a {result} result.")
        return bet.cash_staked
    if needs_explicit_return(bet, result, has_promotions=has_promotions):
        if explicit_return is None:
            raise UsageError(
                f"--return is required to settle this {result} result.",
                remediation="There is no formula for it -- supply the amount actually paid.",
            )
        return explicit_return
    if explicit_return is not None:
        return explicit_return
    assert bet.odds_decimal_placed is not None  # guarded by needs_explicit_return above
    return (bet.cash_staked * bet.odds_decimal_placed).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def with_single_leg(
    legs: list[BetLeg], leg_results: dict[int, BetResult], result: BetResult
) -> dict[int, BetResult]:
    """On a single, the ticket result *is* the leg result (SB-1140).

    Settling a straight bet with only ``--result`` used to leave its one leg
    null, and every leg-level analytic then quietly dropped it: ``bet similar``
    counted 8 receiving-yards singles but 5 decided legs. An explicit
    ``--leg-result`` still wins.
    """
    if len(legs) != 1 or legs[0].leg_order in leg_results or result not in LEG_DECIDING:
        return leg_results
    return {**leg_results, legs[0].leg_order: result}


def _settled_bet(
    bet: Bet, *, result: BetResult, cash_returned: Decimal, settled_at: datetime
) -> Bet:
    """A fresh ``Bet`` built from the existing row's fields plus the outcome.

    Goes through ``Bet(**...)`` rather than mutating in place, so the
    settlement-coherence validator runs on the result exactly as it would for
    a freshly imported row.
    """
    return Bet(
        **{
            **bet.model_dump(),
            "status": "settled",
            "result": result,
            "settled_at": settled_at,
            "cash_returned": cash_returned,
            "cashout_amount": cash_returned if result == "cashed_out" else bet.cashout_amount,
            "updated_at": datetime.now(UTC),
        }
    )


def _settled_leg(leg: BetLeg, result: BetResult) -> BetLeg:
    return BetLeg(**{**leg.model_dump(), "result": result, "updated_at": datetime.now(UTC)})


# ------------------------------------------------------------------ service


def settle_bet(w: Warehouse, request: SettleRequest) -> SettledBet:
    """Settle one bet this scope owns, legs and all, in one transaction.

    Another user's bet is ``NotFoundError``. Every check runs before the
    write, in the order the CLI has always reported them.
    """
    bet = w.bets.get(request.bet_id)
    if bet.status == "settled" and not request.force:
        raise UsageError(
            f"bet {bet.id} is already settled.",
            remediation="Pass --force to re-settle it.",
        )

    leg_results = {order: parse_result(raw) for order, raw in request.leg_results.items()}
    legs = w.legs.for_bet(bet.id)
    unknown = set(leg_results) - {leg.leg_order for leg in legs}
    if unknown:
        raise UsageError(f"unknown leg order(s) for this bet: {sorted(unknown)}.")

    result = resolve_result(parse_result(request.result) if request.result else None, leg_results)
    explicit_return = request.cash_returned
    if explicit_return is not None:
        explicit_return = explicit_return.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if explicit_return < 0:
            raise UsageError("--return cannot be negative.")
    returned = cash_returned(
        bet,
        result,
        explicit_return,
        has_promotions=bool(w.bet_promotions.for_bet(bet.id)),
    )

    updated = _settled_bet(
        bet, result=result, cash_returned=returned, settled_at=now_if_absent(request.settled_at)
    )
    leg_results = with_single_leg(legs, leg_results, result)
    settled_legs = [
        _settled_leg(leg, leg_results[leg.leg_order]) if leg.leg_order in leg_results else leg
        for leg in legs
    ]
    with w.transaction() as tx:
        tx.bets.update(updated)
        for before, after in zip(legs, settled_legs, strict=True):
            if after is not before:
                tx.legs.update(after)

    return SettledBet(bet=updated, legs=settled_legs)


__all__ = [
    "LEG_DECIDING",
    "RESULTS",
    "RETURN_REQUIRED",
    "STAKE_RETURNED",
    "SettleRequest",
    "SettledBet",
    "cash_returned",
    "needs_explicit_return",
    "parse_result",
    "resolve_result",
    "settle_bet",
    "with_single_leg",
]
