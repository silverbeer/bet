"""``bet similar`` -- settled record on bets comparable to a proposed one (SB-1133).

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bet.analytics.similar import HistoricBet, Proposal, compare, eligible, odds_band, shape_of
from bet.cli.main import app
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg, BetPromotion
from bet.models.ownership import SportsbookAccount

runner = CliRunner()

PLACED = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)
TENANT, USER = uuid.uuid4(), uuid.uuid4()
THRESHOLDS = {"min_settled": 3, "exploratory_below": 5}

BOWERS_TD: dict[str, Any] = {
    "sport": "NFL",
    "league": "NFL",
    "market_name": "anytime_touchdown_scorer",
    "target_player": "Brock Bowers",
    "target_team": "Las Vegas Raiders",
}


# --------------------------------------------------------------- builders


def _bet(
    *,
    result: str | None = "won",
    staked: str = "10.00",
    returned: str = "0.00",
    kind: str = "straight",
    odds: int | None = 150,
    bonus: str = "0.00",
) -> Bet:
    settled = result is not None
    return Bet(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        placed_at=PLACED,
        status="settled" if settled else "pending",
        result=result,  # type: ignore[arg-type]
        settled_at=PLACED if settled else None,
        wager_kind=kind,  # type: ignore[arg-type]
        cash_staked=Decimal(staked),
        bonus_staked=Decimal(bonus),
        cash_returned=Decimal(returned),
        odds_american_placed=odds,
    )


def _leg(order: int = 1, *, result: str | None = None, **fields: Any) -> BetLeg:
    return BetLeg(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        bet_id=uuid.uuid4(),
        leg_order=order,
        result=result,  # type: ignore[arg-type]
        **fields,
    )


def _single(
    result: str, returned: str, promos: frozenset[str] = frozenset(), **leg: Any
) -> HistoricBet:
    return HistoricBet(
        bet=_bet(result=result, returned=returned),
        legs=[_leg(result=result, **leg)],
        promo_types=promos,
    )


def _rows(proposal: Proposal, history: list[HistoricBet]) -> dict[str, dict[str, Any]]:
    return {r["dimension"]: r for r in compare(proposal, history, **THRESHOLDS)}


def _bowers_proposal(**kw: Any) -> Proposal:
    return Proposal(legs=[_leg(**BOWERS_TD)], wager_kind="straight", odds_american=175, **kw)


@pytest.fixture
def history() -> list[HistoricBet]:
    return [
        _single("won", "27.50", frozenset({"profit_boost"}), **BOWERS_TD),
        _single("lost", "0.00", **BOWERS_TD),
        # A 2-leg SGP whose Bowers leg hit but whose ticket lost.
        HistoricBet(
            bet=_bet(result="lost", kind="same_game_parlay", odds=250),
            legs=[
                _leg(1, result="won", **BOWERS_TD),
                _leg(
                    2,
                    result="lost",
                    sport="NFL",
                    league="NFL",
                    market_name="alt_receiving_yds",
                    target_player="Jakobi Meyers",
                ),
            ],
        ),
        _single(
            "won",
            "19.00",
            sport="NFL",
            league="NFL",
            market_name="receiving_yds",
            target_player="Travis Kelce",
            odds_american=-110,
        ),
        _single(
            "won",
            "15.00",
            sport="WNBA",
            league="WNBA",
            market_name="alt_points",
            target_player="A'ja Wilson",
        ),
    ]


# ------------------------------------------------------------------ keys


@pytest.mark.parametrize(
    ("kind", "legs", "expected"),
    [
        ("straight", 1, "single"),
        ("parlay", 2, "2-leg parlay"),
        ("same_game_parlay", 2, "2-leg SGP"),
        ("same_game_parlay", 3, "3+-leg SGP"),
        ("parlay", 5, "3+-leg parlay"),
    ],
)
def test_shape_splits_by_size_and_style(kind: str, legs: int, expected: str) -> None:
    assert shape_of(kind, legs) == expected


@pytest.mark.parametrize(
    ("odds", "band"),
    [
        (-450, "-200 or shorter"),
        (-200, "-200 or shorter"),
        (-199, "-199 to -100"),
        (-110, "-199 to -100"),
        (100, "+100 to +199"),
        (199, "+100 to +199"),
        (200, "+200 to +499"),
        (500, "+500 or longer"),
    ],
)
def test_odds_band_edges(odds: int, band: str) -> None:
    assert odds_band(odds) == band


# ------------------------------------------------------------ ticket rows


def test_shape_row_counts_only_bets_of_the_same_shape(history: list[HistoricBet]) -> None:
    row = _rows(_bowers_proposal(), history)["shape"]
    assert row["match"] == "single"
    assert row["unit"] == "bets"
    assert (row["n"], row["won"]) == (4, 3)
    assert row["staked"] == Decimal("40.00")
    # 17.50 - 10.00 + 9.00 + 5.00
    assert row["net"] == Decimal("21.50")
    assert row["roi"] == Decimal("0.5375")


def test_league_row_matches_any_leg_of_that_league(history: list[HistoricBet]) -> None:
    row = _rows(_bowers_proposal(), history)["league"]
    assert row["match"] == "NFL"
    assert row["n"] == 4  # the WNBA single is excluded; the NFL SGP is included


def test_odds_band_row_uses_the_ticket_price(history: list[HistoricBet]) -> None:
    row = _rows(_bowers_proposal(), history)["odds band"]
    assert row["match"] == "+100 to +199"
    assert row["n"] == 4  # every +150 bet; not the +250 SGP


def test_unpriced_proposal_has_no_odds_band_row(history: list[HistoricBet]) -> None:
    proposal = Proposal(legs=[_leg(**BOWERS_TD)], wager_kind="straight", odds_american=None)
    assert "odds band" not in _rows(proposal, history)


def test_promotion_row_counts_bets_carrying_that_promotion(history: list[HistoricBet]) -> None:
    row = _rows(_bowers_proposal(promo_types=["profit_boost"]), history)["promotion"]
    assert (row["match"], row["n"], row["won"]) == ("profit_boost", 1, 1)


# --------------------------------------------------------------- leg rows


def test_leg_row_hit_rate_counts_parlay_legs_but_money_only_singles(
    history: list[HistoricBet],
) -> None:
    row = _rows(_bowers_proposal(), history)["leg 1 player"]
    assert row["unit"] == "legs"
    # Three Bowers legs: two singles and the SGP leg that hit.
    assert (row["n"], row["won"]) == (3, 2)
    assert row["rate"] == Decimal("0.6667")
    # Money from the two singles only -- the lost SGP is not his to carry.
    assert (row["bets"], row["staked"], row["net"]) == (2, Decimal("20.00"), Decimal("7.50"))


def test_player_and_market_rows_are_separate_and_combined(history: list[HistoricBet]) -> None:
    rows = _rows(_bowers_proposal(), history)
    assert rows["leg 1 market"]["n"] == 3
    assert rows["leg 1 player+market"]["match"] == "Brock Bowers · anytime_touchdown_scorer"
    assert rows["leg 1 player+market"]["n"] == 3


def test_matching_ignores_case_and_whitespace(history: list[HistoricBet]) -> None:
    proposal = Proposal(
        legs=[_leg(target_player="  brock BOWERS ", market_name="Anytime_Touchdown_Scorer")],
        wager_kind="straight",
    )
    assert _rows(proposal, history)["leg 1 player"]["n"] == 3


def test_team_stands_in_only_when_there_is_no_player(history: list[HistoricBet]) -> None:
    moneyline = Proposal(
        legs=[_leg(target_team="Las Vegas Raiders", market_name="moneyline")],
        wager_kind="straight",
    )
    rows = _rows(moneyline, history)
    assert rows["leg 1 team"]["n"] == 3
    assert "leg 1 team" not in _rows(_bowers_proposal(), history)


def test_undecided_legs_are_not_in_the_hit_rate() -> None:
    history = [
        HistoricBet(
            bet=_bet(result="void", returned="10.00"),
            legs=[_leg(result="void", **BOWERS_TD)],
        ),
    ]
    row = _rows(_bowers_proposal(), history)["leg 1 player"]
    assert (row["n"], row["rate"]) == (0, None)


# ----------------------------------------------------------------- flags


@pytest.mark.parametrize(
    ("count", "flag"),
    [(0, "no history"), (2, "insufficient"), (3, "exploratory"), (4, "exploratory"), (5, "")],
)
def test_sample_size_is_labelled(count: int, flag: str) -> None:
    history = [_single("won", "20.00", **BOWERS_TD) for _ in range(count)]
    assert _rows(_bowers_proposal(), history)["shape"]["flag"] == flag


def test_empty_history_reports_every_dimension_as_no_history() -> None:
    rows = compare(_bowers_proposal(promo_types=["profit_boost"]), [], **THRESHOLDS)
    assert rows
    assert {r["flag"] for r in rows} == {"no history"}
    assert all(r["roi"] is None and r["rate"] is None for r in rows)


# -------------------------------------------------------------- eligible


def test_eligible_keeps_only_settled_cash_non_void_bets() -> None:
    assert eligible(_bet(result="won"))
    assert not eligible(_bet(result=None))
    assert not eligible(_bet(result="void", returned="10.00"))
    assert eligible(_bet(result="void", returned="10.00"), include_void=True)
    assert not eligible(_bet(result="won", staked="0.00", bonus="10.00"))


# -------------------------------------------------------------------- CLI


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for name in list(dict(os.environ)):
        if name.startswith("BET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("BET_THRESHOLDS__MIN_SETTLED_BETS", "2")


@contextmanager
def _open_warehouse(data: Path) -> Iterator[Warehouse]:
    settings = Settings(data_dir=data)
    config = type("_Config", (), {"settings": settings})()
    with connect(settings) as conn:
        scope = identity.resolve_scope(conn, config)
        yield Warehouse(conn, scope)


def _seed(w: Warehouse, *, result: str, returned: str, promo: str | None = None) -> None:
    account = w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code="fanduel",
            label=f"acct-{uuid.uuid4().hex[:6]}",
        )
    )
    bet = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            placed_at=PLACED,
            status="settled",
            result=result,  # type: ignore[arg-type]
            settled_at=PLACED,
            cash_staked=Decimal("10.00"),
            cash_returned=Decimal(returned),
            odds_american_placed=175,
        )
    )
    w.legs.add(
        BetLeg(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=bet.id,
            leg_order=1,
            result=result,  # type: ignore[arg-type]
            odds_american=175,
            selection_name="Brock Bowers Any Time Touchdown Scorer",
            **BOWERS_TD,
        )
    )
    if promo:
        w.bet_promotions.add(
            BetPromotion(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                promotion_type=promo,  # type: ignore[arg-type]
                generosity_pct=Decimal("30"),
            )
        )


BOWERS_LEG = (
    "sport=NFL,league=NFL,market=anytime_touchdown_scorer,"
    "selection=Brock Bowers Any Time Touchdown Scorer,team=Las Vegas Raiders,"
    "player=Brock Bowers,odds=175"
)


def _invoke(*args: str) -> list[dict[str, Any]]:
    result = runner.invoke(app, ["--format", "json", "similar", *args])
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    return rows


def test_cli_reports_history_for_a_proposed_bet(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _seed(w, result="won", returned="27.50", promo="profit_boost")
        _seed(w, result="lost", returned="0.00")

    rows = {
        r["dimension"]: r
        for r in _invoke("--leg", BOWERS_LEG, "--promo", "type=profit_boost,generosity_pct=30")
    }
    assert rows["shape"]["n"] == 2
    assert rows["shape"]["net"] == "7.50"
    # min_settled_bets is 2 here, so two bets are exploratory, not insufficient.
    assert rows["shape"]["flag"] == "exploratory"
    assert rows["promotion"]["n"] == 1
    assert rows["promotion"]["flag"] == "insufficient"
    assert rows["leg 1 player+market"]["won"] == 1


def test_cli_writes_nothing(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _seed(w, result="won", returned="27.50")
        before = w.bets.count(), w.legs.count(), w.bet_promotions.count()

    _invoke("--leg", BOWERS_LEG, "--promo", "type=profit_boost,generosity_pct=30")

    with _open_warehouse(data_dir) as w:
        assert (w.bets.count(), w.legs.count(), w.bet_promotions.count()) == before


def test_cli_accepts_an_unpriced_sgp_without_odds(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    unpriced = BOWERS_LEG.replace(",odds=175", "")
    rows = _invoke("--leg", unpriced, "--leg", unpriced, "--wager-kind", "same_game_parlay")
    dims = {r["dimension"] for r in rows}
    assert "odds band" not in dims
    assert next(r for r in rows if r["dimension"] == "shape")["match"] == "2-leg SGP"


def test_cli_filters_history_with_global_options(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _seed(w, result="won", returned="27.50")
    result = runner.invoke(
        app, ["--format", "json", "--since", "2026-09-01", "similar", "--leg", BOWERS_LEG]
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert next(r for r in rows if r["dimension"] == "shape")["n"] == 0


def test_cli_requires_a_leg() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["similar"])
    assert isinstance(result.exception, UsageError)


def test_cli_rejects_a_bad_leg_the_way_add_does() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["similar", "--leg", "sport=NFL,colour=blue"])
    assert isinstance(result.exception, UsageError)


def test_cli_rejects_a_boost_without_generosity() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["similar", "--leg", BOWERS_LEG, "--promo", "type=profit_boost"])
    assert isinstance(result.exception, UsageError)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"
