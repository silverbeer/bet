"""``bet add`` — manual entry: odds math, non-interactive scripting, and abort.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from rich.prompt import Confirm, Prompt
from typer.testing import CliRunner

from bet.cli.commands import add_cmd
from bet.cli.main import app
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for name in list(dict(os.environ)):
        if name.startswith("BET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@contextmanager
def _open_warehouse(data: Path) -> Iterator[Warehouse]:
    """Open the warehouse directly, bypassing the CLI, to assert on what it wrote.

    Closed on exit: DuckDB allows only one read-write connection to a file at a
    time, so a test that both inspects the warehouse and invokes the CLI again
    must not leave this connection open.
    """
    settings = Settings(data_dir=data)
    config = type("_Config", (), {"settings": settings})()
    with connect(settings) as conn:
        scope = identity.resolve_scope(conn, config)
        yield Warehouse(conn, scope)


# ------------------------------------------------------------------ odds math


@pytest.mark.parametrize(
    ("american", "decimal"),
    [
        (-100, Decimal("2.0000")),
        (100, Decimal("2.0000")),
        (-110, Decimal("1.9091")),
        (2500, Decimal("26.0000")),
    ],
)
def test_american_to_decimal_boundaries(american: int, decimal: Decimal) -> None:
    assert add_cmd.american_to_decimal(american) == decimal


def test_decimal_to_american_round_trips_at_the_boundary() -> None:
    assert add_cmd.decimal_to_american(Decimal("2.0000")) == 100


def test_combined_decimal_odds_multiplies_legs() -> None:
    legs = [add_cmd.american_to_decimal(-110), add_cmd.american_to_decimal(150)]
    assert add_cmd.combined_decimal_odds(legs) == (legs[0] * legs[1]).quantize(Decimal("0.0001"))


def test_parse_odds_rejects_the_dead_zone() -> None:
    with pytest.raises(UsageError):
        add_cmd._parse_odds("50")


def test_parse_odds_rejects_zero() -> None:
    with pytest.raises(UsageError):
        add_cmd._parse_odds("0")


def test_parse_money_quantizes_to_cents() -> None:
    assert add_cmd._parse_money("25", field_name="stake") == Decimal("25.00")


def test_parse_money_rejects_negative() -> None:
    with pytest.raises(UsageError):
        add_cmd._parse_money("-5.00", field_name="stake")


def test_parse_leg_spec_allows_a_leg_with_no_odds() -> None:
    """A leg price is optional -- some operators publish none (SB-1078).

    FanDuel renders no per-leg price on a Same Game Parlay and DraftKings
    publishes none at all, so refusing the leg would lose the selection as
    well as the price it never had.
    """
    fields = add_cmd._parse_leg_spec("sport=NFL,selection=Chiefs")
    assert fields == {"sport": "NFL", "selection": "Chiefs"}
    assert "odds" not in fields


def test_parse_leg_spec_rejects_unknown_field() -> None:
    with pytest.raises(UsageError):
        add_cmd._parse_leg_spec("odds=-110,nonsense=1")


def test_parse_placed_at_assumes_utc_when_naive() -> None:
    value = add_cmd._parse_placed_at("2026-09-08T19:30:00")
    assert value.tzinfo is UTC


def test_parse_placed_at_keeps_an_explicit_offset() -> None:
    value = add_cmd._parse_placed_at("2026-09-08T19:30:00-04:00")
    offset = value.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == -4 * 3600


# ------------------------------------------------------------- non-interactive


def test_straight_bet_round_trips(data_dir: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app,
        [
            "--format",
            "json",
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--leg",
            "sport=MLB,market=moneyline,selection=Red Sox,odds=-145",
            "--stake",
            "25.00",
        ],
    )
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bets = w.bets.current()
        assert len(bets) == 1
        bet = bets[0]
        assert bet.wager_kind == "straight"
        assert bet.capture_method == "manual"
        assert bet.status == "pending"
        assert bet.cash_staked == Decimal("25.00")
        assert bet.bonus_staked == Decimal("0.00")

        legs = w.legs.for_bet(bet.id)
        assert len(legs) == 1
        assert legs[0].selection_name == "Red Sox"
        assert legs[0].odds_american == -145


def test_parlay_round_trips(data_dir: Path) -> None:
    runner.invoke(app, ["init"])

    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "draftkings",
            "--leg",
            "sport=NFL,market=spread,selection=Chiefs -3.5,odds=-110",
            "--leg",
            "sport=NFL,market=moneyline,selection=Bills,odds=145",
            "--leg",
            "sport=NFL,market=total,selection=Over 47.5,odds=-105",
            "--stake",
            "10.00",
        ],
    )
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.wager_kind == "parlay"
        legs = w.legs.for_bet(bet.id)
        assert [leg.leg_order for leg in legs] == [1, 2, 3]
        assert {leg.selection_name for leg in legs} == {"Chiefs -3.5", "Bills", "Over 47.5"}


def test_free_bet_stake_is_distinct_from_cash_stake(data_dir: Path) -> None:
    runner.invoke(app, ["init"])

    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "betmgm",
            "--leg",
            "sport=NBA,market=moneyline,selection=Celtics,odds=-120",
            "--bonus-stake",
            "20.00",
        ],
    )
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.cash_staked == Decimal("0.00")
        assert bet.bonus_staked == Decimal("20.00")
        assert bet.is_free_bet is True


def test_neither_stake_is_rejected() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--leg",
            "odds=-110",
        ],
    )
    assert isinstance(result.exception, UsageError)


def test_unknown_sportsbook_is_rejected() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "notabook",
            "--leg",
            "odds=-110",
            "--stake",
            "5.00",
        ],
    )
    assert isinstance(result.exception, UsageError)


def test_ambiguous_account_requires_a_label(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        for label in ("main", "alt"):
            w.accounts.add(add_cmd._new_account(w, "fanduel", label))

    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--leg",
            "odds=-110",
            "--stake",
            "5.00",
        ],
    )
    assert isinstance(result.exception, UsageError)

    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--account-label",
            "alt",
            "--leg",
            "odds=-110",
            "--stake",
            "5.00",
        ],
    )
    assert result.exit_code == 0, result.output


# ----------------------------------------------------------------- interactive


def test_aborting_at_confirm_writes_nothing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = iter(["fanduel", "main", "MLB", "moneyline", "Red Sox", "-145", "25.00", "0.00"])
    confirms = iter([False, False])  # "add another leg?" then "confirm?"

    monkeypatch.setattr(Prompt, "ask", lambda *a, **k: next(prompts))
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: next(confirms))

    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["add"])
    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output

    with _open_warehouse(data_dir) as w:
        assert w.bets.current() == []


def test_interactive_straight_bet_round_trips(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = iter(["fanduel", "main", "MLB", "moneyline", "Red Sox", "-145", "25.00", "0.00"])
    confirms = iter([False, True])  # "add another leg?" then "confirm?"

    monkeypatch.setattr(Prompt, "ask", lambda *a, **k: next(prompts))
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: next(confirms))

    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["add"])
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bets = w.bets.current()
        assert len(bets) == 1
        assert bets[0].cash_staked == Decimal("25.00")


# ---------------------------------------------------- ticket-level odds (SB-1078)


def test_ticket_odds_prefers_the_stated_price_over_the_product() -> None:
    """``--odds`` wins even when every leg is priced.

    A Same Game Parlay is repriced as a group, so the product of its legs is
    the wrong number whether or not it can be computed.
    """
    legs = [
        add_cmd._leg_from_fields(
            {"odds": "-110"}, leg_order=1, tenant_id=uuid4(), user_id=uuid4(), bet_id=uuid4()
        ),
        add_cmd._leg_from_fields(
            {"odds": "-110"}, leg_order=2, tenant_id=uuid4(), user_id=uuid4(), bet_id=uuid4()
        ),
    ]
    american, decimal_odds = add_cmd._ticket_odds(legs, "-138")
    assert american == -138
    assert decimal_odds == add_cmd.american_to_decimal(-138)


def test_ticket_odds_derives_the_product_when_no_price_is_stated() -> None:
    legs = [
        add_cmd._leg_from_fields(
            {"odds": "100"}, leg_order=1, tenant_id=uuid4(), user_id=uuid4(), bet_id=uuid4()
        ),
        add_cmd._leg_from_fields(
            {"odds": "100"}, leg_order=2, tenant_id=uuid4(), user_id=uuid4(), bet_id=uuid4()
        ),
    ]
    american, _ = add_cmd._ticket_odds(legs, None)
    assert american == 300


def test_ticket_odds_refuses_a_partially_priced_ticket() -> None:
    """A product over the priced subset is a number with no meaning.

    Without this guard the two-leg ticket below would be recorded at the price
    of its one priced leg -- a plausible, wrong, and undetectable figure.
    """
    legs = [
        add_cmd._leg_from_fields(
            {"odds": "-110"}, leg_order=1, tenant_id=uuid4(), user_id=uuid4(), bet_id=uuid4()
        ),
        add_cmd._leg_from_fields(
            {}, leg_order=2, tenant_id=uuid4(), user_id=uuid4(), bet_id=uuid4()
        ),
    ]
    with pytest.raises(UsageError):
        add_cmd._ticket_odds(legs, None)


def test_ticket_odds_refuses_an_entirely_unpriced_ticket() -> None:
    """Guards ``decimal_to_american(1)``, which divides by zero."""
    legs = [
        add_cmd._leg_from_fields(
            {}, leg_order=1, tenant_id=uuid4(), user_id=uuid4(), bet_id=uuid4()
        )
    ]
    with pytest.raises(UsageError):
        add_cmd._ticket_odds(legs, None)


def test_parse_wager_kind_rejects_an_unknown_kind() -> None:
    with pytest.raises(UsageError):
        add_cmd._parse_wager_kind("accumulator", leg_count=2)


def test_parse_wager_kind_rejects_a_multi_leg_kind_on_one_leg() -> None:
    """A dropped leg is the likely cause, so this is worth catching at entry."""
    with pytest.raises(UsageError):
        add_cmd._parse_wager_kind("same_game_parlay", leg_count=1)


def test_parse_wager_kind_rejects_straight_on_several_legs() -> None:
    with pytest.raises(UsageError):
        add_cmd._parse_wager_kind("straight", leg_count=3)


def test_sgp_with_unpriced_legs_round_trips(data_dir: Path) -> None:
    """The FanDuel screenshot case: a ticket price and legs carrying none."""
    runner.invoke(app, ["init"])

    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--wager-kind",
            "same_game_parlay",
            "--odds",
            "-138",
            "--leg",
            "sport=NFL,market=alt_receiving_yds,selection=A.J. Brown 50+ Yards,player=A.J. Brown",
            "--leg",
            "sport=NFL,market=alt_receiving_yds,selection=Jaxon Smith-Njigba 50+ Yards,"
            "player=Jaxon Smith-Njigba",
            "--stake",
            "5.00",
        ],
    )
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.wager_kind == "same_game_parlay"
        assert bet.odds_american_placed == -138
        legs = w.legs.for_bet(bet.id)
        assert len(legs) == 2
        assert all(leg.odds_american is None for leg in legs)
        assert {leg.target_player for leg in legs} == {"A.J. Brown", "Jaxon Smith-Njigba"}


def test_unpriced_legs_without_a_ticket_price_are_refused(data_dir: Path) -> None:
    runner.invoke(app, ["init"])

    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--leg",
            "sport=NFL,selection=A.J. Brown 50+ Yards",
            "--stake",
            "5.00",
        ],
    )
    assert result.exit_code != 0
    assert "cannot be derived" in str(result.exception)


def test_priced_legs_still_derive_the_product(data_dir: Path) -> None:
    """The pre-SB-1078 path is unchanged when no --odds is passed."""
    runner.invoke(app, ["init"])

    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "draftkings",
            "--leg",
            "sport=NFL,market=moneyline,selection=Bills,odds=100",
            "--leg",
            "sport=NFL,market=moneyline,selection=Chiefs,odds=100",
            "--stake",
            "10.00",
        ],
    )
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.wager_kind == "parlay"
        assert bet.odds_american_placed == 300
