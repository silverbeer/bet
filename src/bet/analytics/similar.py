"""Settled record on bets comparable to a proposed one (SB-1133).

A proposed bet is compared against history along several independent
dimensions -- shape, price band, league, promotion, and per leg the player or
team and the market. Each dimension is its own row rather than one blended
"similarity score": a score hides which part of the comparison carries the
sample, and the reader needs to see that Bowers-anytime-TD has 3 bets behind
it while singles have 60.

Two units, never mixed:

* Ticket rows count **bets**. Win rate, stake, net and ROI are the ticket's.
* Leg rows count **legs**. A parlay's profit cannot be attributed to one of
  its legs, so a leg row's hit rate is over every decided leg that matched,
  and its money columns come only from the *singles* among them -- the one
  case where the leg and the ticket are the same thing.

Only cash bets are considered. A bonus-stake bet has no cash at risk, so it
has no cash ROI to contribute (DATA_DICTIONARY), and folding its winnings
into ``net`` would flatter every row it touched.

Sample size is labelled, not hidden (SB-688): below ``min_settled_bets`` a
row is ``insufficient``, below ``exploratory_below`` it is ``exploratory``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bet.models.bet import Bet, BetLeg

RATIO_PLACES = Decimal("0.0001")
TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")

COLUMNS = [
    "dimension",
    "match",
    "unit",
    "n",
    "won",
    "rate",
    "bets",
    "staked",
    "net",
    "roi",
    "flag",
]


@dataclass(frozen=True)
class HistoricBet:
    """One settled ticket with everything a comparison reads."""

    bet: Bet
    legs: Sequence[BetLeg]
    promo_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Proposal:
    """The bet under consideration. Nothing about it is persisted."""

    legs: Sequence[BetLeg]
    wager_kind: str
    odds_american: int | None = None
    promo_types: Sequence[str] = field(default_factory=tuple)


# ------------------------------------------------------------------ keys


def _norm(value: str | None) -> str | None:
    return value.strip().casefold() if value and value.strip() else None


def shape_of(wager_kind: str, leg_count: int) -> str:
    """Single / 2-leg / 3+-leg, split by SGP vs cross-game parlay.

    The split is the one the user's own policy is written in (singles and
    2-leg only from 2026-09-18), so it is the one history must be cut by.
    """
    if leg_count <= 1:
        return "single"
    size = "2-leg" if leg_count == 2 else "3+-leg"
    style = "SGP" if wager_kind == "same_game_parlay" else "parlay"
    return f"{size} {style}"


def odds_band(odds_american: int) -> str:
    if odds_american <= -200:
        return "-200 or shorter"
    if odds_american < 0:
        return "-199 to -100"
    if odds_american < 200:
        return "+100 to +199"
    if odds_american < 500:
        return "+200 to +499"
    return "+500 or longer"


# ----------------------------------------------------------- aggregation


def _flag(n: int, *, min_settled: int, exploratory_below: int) -> str:
    if n == 0:
        return "no history"
    if n < min_settled:
        return "insufficient"
    if n < exploratory_below:
        return "exploratory"
    return ""


def _ratio(numerator: Decimal | int, denominator: Decimal | int) -> Decimal | None:
    if not denominator:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(RATIO_PLACES, ROUND_HALF_UP)


def _money(bets: Iterable[Bet]) -> tuple[int, Decimal, Decimal, Decimal | None]:
    chosen = list(bets)
    staked = sum((b.cash_staked for b in chosen), ZERO)
    net = sum((b.net_profit or ZERO for b in chosen), ZERO)
    return len(chosen), staked, net, _ratio(net, staked)


def _ticket_row(
    dimension: str,
    match: str,
    history: Sequence[HistoricBet],
    predicate: Callable[[HistoricBet], bool],
    **thresholds: int,
) -> dict[str, Any]:
    hits = [h.bet for h in history if predicate(h)]
    won = sum(1 for b in hits if b.result == "won")
    bets, staked, net, roi = _money(hits)
    return {
        "dimension": dimension,
        "match": match,
        "unit": "bets",
        "n": bets,
        "won": won,
        "rate": _ratio(won, bets),
        "bets": bets,
        "staked": staked,
        "net": net,
        "roi": roi,
        "flag": _flag(bets, **thresholds),
    }


def _leg_row(
    dimension: str,
    match: str,
    history: Sequence[HistoricBet],
    predicate: Callable[[BetLeg], bool],
    **thresholds: int,
) -> dict[str, Any]:
    decided = [
        leg for h in history for leg in h.legs if predicate(leg) and leg.result in ("won", "lost")
    ]
    won = sum(1 for leg in decided if leg.result == "won")
    singles = [h.bet for h in history if len(h.legs) == 1 and predicate(h.legs[0])]
    bets, staked, net, roi = _money(singles)
    return {
        "dimension": dimension,
        "match": match,
        "unit": "legs",
        "n": len(decided),
        "won": won,
        "rate": _ratio(won, len(decided)),
        "bets": bets,
        "staked": staked,
        "net": net,
        "roi": roi,
        "flag": _flag(len(decided), **thresholds),
    }


def _in_league(key: str) -> Callable[[HistoricBet], bool]:
    return lambda h: any(_norm(leg.league or leg.sport) == key for leg in h.legs)


def _has_promo(promo: str) -> Callable[[HistoricBet], bool]:
    return lambda h: promo in h.promo_types


def _leg_matches(
    *, player: str | None = None, team: str | None = None, market: str | None = None
) -> Callable[[BetLeg], bool]:
    """Every given field must match, normalised; unspecified fields are ignored."""

    def match(leg: BetLeg) -> bool:
        return (
            (player is None or _norm(leg.target_player) == player)
            and (team is None or _norm(leg.target_team) == team)
            and (market is None or _norm(leg.market_name) == market)
        )

    return match


def eligible(bet: Bet, *, include_void: bool = False) -> bool:
    """Settled, cash, and (unless asked) not void."""
    if bet.status != "settled" or bet.cash_staked <= 0:
        return False
    return include_void or bet.result != "void"


def compare(
    proposal: Proposal,
    history: Sequence[HistoricBet],
    *,
    min_settled: int,
    exploratory_below: int,
) -> list[dict[str, Any]]:
    """One row per comparison dimension. ``history`` must already be eligible."""
    t = {"min_settled": min_settled, "exploratory_below": exploratory_below}
    rows: list[dict[str, Any]] = []

    shape = shape_of(proposal.wager_kind, len(proposal.legs))
    rows.append(
        _ticket_row(
            "shape",
            shape,
            history,
            lambda h: shape_of(h.bet.wager_kind, len(h.legs)) == shape,
            **t,
        )
    )

    if proposal.odds_american is not None:
        band = odds_band(proposal.odds_american)
        rows.append(
            _ticket_row(
                "odds band",
                band,
                history,
                lambda h: (
                    h.bet.odds_american_placed is not None
                    and odds_band(h.bet.odds_american_placed) == band
                ),
                **t,
            )
        )

    leagues: dict[str, str] = {}
    for leg in proposal.legs:
        label = leg.league or leg.sport
        key = _norm(label)
        if key and label:
            leagues.setdefault(key, label)
    for key, label in leagues.items():
        rows.append(_ticket_row("league", label, history, _in_league(key), **t))

    for promo in dict.fromkeys(proposal.promo_types):
        rows.append(_ticket_row("promotion", promo, history, _has_promo(promo), **t))

    for leg in proposal.legs:
        prefix = f"leg {leg.leg_order}"
        player, team, market = (
            _norm(leg.target_player),
            _norm(leg.target_team),
            _norm(leg.market_name),
        )
        # A player prop is about the player; the team only stands in when
        # there is no player (moneyline, spread).
        if player:
            by_player = _leg_matches(player=player)
            rows.append(
                _leg_row(f"{prefix} player", leg.target_player or "", history, by_player, **t)
            )
        elif team:
            by_team = _leg_matches(team=team)
            rows.append(_leg_row(f"{prefix} team", leg.target_team or "", history, by_team, **t))
        if market:
            by_market = _leg_matches(market=market)
            rows.append(
                _leg_row(f"{prefix} market", leg.market_name or "", history, by_market, **t)
            )
        if player and market:
            rows.append(
                _leg_row(
                    f"{prefix} player+market",
                    f"{leg.target_player} · {leg.market_name}",
                    history,
                    _leg_matches(player=player, market=market),
                    **t,
                )
            )

    return rows


__all__ = [
    "COLUMNS",
    "HistoricBet",
    "Proposal",
    "compare",
    "eligible",
    "odds_band",
    "shape_of",
]
