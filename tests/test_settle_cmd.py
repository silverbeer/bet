"""``bet settle`` — result computation, --force, and leg-result cascade.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from rich.prompt import Confirm, Prompt
from typer.testing import CliRunner, Result

from bet.cli.main import app
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import NotFoundError, UsageError
from bet.models.bet import Bet, BetLeg
from bet.models.ownership import SportsbookAccount

runner = CliRunner()

# Recent, so --settled-at may default to now (SB-1521).
PLACED = datetime.now(UTC) - timedelta(hours=1)
STALE = datetime.now(UTC) - timedelta(days=3)


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
    settings = Settings(data_dir=data)
    config = type("_Config", (), {"settings": settings})()
    with connect(settings) as conn:
        scope = identity.resolve_scope(conn, config)
        yield Warehouse(conn, scope)


def _make_account(w: Warehouse) -> SportsbookAccount:
    return w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code="fanduel",
            label="main",
        )
    )


def _make_bet(w: Warehouse, account: SportsbookAccount, num_legs: int = 1, **fields: object) -> Bet:
    defaults: dict[str, object] = {
        "cash_staked": Decimal("10.00"),
        "odds_american_placed": -150,
        "odds_decimal_placed": Decimal("1.6667"),
        "placed_at": PLACED,
    }
    bet = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            **(defaults | fields),  # type: ignore[arg-type]
        )
    )
    for order in range(1, num_legs + 1):
        w.legs.add(
            BetLeg(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                leg_order=order,
                selection_name=f"Leg {order}",
                odds_american=-150,
                odds_decimal=Decimal("1.6667"),
            )
        )
    return bet


def _settle(*args: str) -> Result:
    return runner.invoke(app, ["settle", *args])


# --------------------------------------------------------------- result math


def test_won_computes_return_from_stored_odds(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", "won")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled.status == "settled"
        assert settled.result == "won"
        assert settled.cash_returned == Decimal("16.67")
        assert settled.net_profit == Decimal("6.67")
        assert settled.settled_at is not None


def test_lost_returns_nothing(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", "lost")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled.cash_returned == Decimal("0.00")
        assert settled.net_profit == Decimal("-10.00")


@pytest.mark.parametrize("outcome", ["push", "void"])
def test_push_and_void_return_the_stake(data_dir: Path, outcome: str) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", outcome)
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled.cash_returned == Decimal("10.00")
        assert settled.net_profit == Decimal("0.00")


def test_cashed_out_requires_return(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w), cash_staked=Decimal("40.00"))

    missing = _settle(str(bet.id), "--result", "cashed_out")
    assert isinstance(missing.exception, UsageError)

    result = _settle(str(bet.id), "--result", "cashed_out", "--return", "52.00")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled.cash_returned == Decimal("52.00")
        assert settled.cashout_amount == Decimal("52.00")
        assert settled.net_profit == Decimal("12.00")


def test_partial_requires_return(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", "partial")
    assert isinstance(result.exception, UsageError)


def test_won_with_bonus_stake_requires_return(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(
            w, _make_account(w), cash_staked=Decimal("0.00"), bonus_staked=Decimal("25.00")
        )

    result = _settle(str(bet.id), "--result", "won")
    assert isinstance(result.exception, UsageError)

    result = _settle(str(bet.id), "--result", "won", "--return", "21.50")
    assert result.exit_code == 0, result.output


def test_return_is_rejected_for_lost_push_void(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", "lost", "--return", "5.00")
    assert isinstance(result.exception, UsageError)


# ------------------------------------------------------------------- --force


def test_settling_twice_requires_force(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    first = _settle(str(bet.id), "--result", "won")
    assert first.exit_code == 0, first.output

    again = _settle(str(bet.id), "--result", "lost")
    assert isinstance(again.exception, UsageError)

    forced = _settle(str(bet.id), "--result", "lost", "--force")
    assert forced.exit_code == 0, forced.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled.result == "lost"
        assert settled.cash_returned == Decimal("0.00")


# ------------------------------------------------------------ leg cascade


def test_a_lost_leg_forces_the_ticket_lost(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w), num_legs=2, cash_staked=Decimal("10.00"))

    result = _settle(str(bet.id), "--leg-result", "2=lost")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled.result == "lost"
        legs = w.legs.for_bet(bet.id)
        assert next(leg for leg in legs if leg.leg_order == 2).result == "lost"


def test_a_lost_leg_conflicting_with_explicit_result_is_rejected(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w), num_legs=2)

    result = _settle(str(bet.id), "--leg-result", "2=lost", "--result", "won")
    assert isinstance(result.exception, UsageError)


def test_unknown_leg_order_is_rejected(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--leg-result", "9=lost")
    assert isinstance(result.exception, UsageError)


def test_result_is_required_without_a_forcing_leg(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id))
    assert isinstance(result.exception, UsageError)


def test_unknown_bet_id_is_a_not_found_error(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    result = _settle(str(uuid.uuid4()), "--result", "won")
    assert isinstance(result.exception, NotFoundError)


# ----------------------------------------------------------------- interactive


def test_interactive_walk_settles_an_open_bet(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _make_bet(w, _make_account(w))

    prompts = iter(["won", "won", "", ""])  # leg result, ticket result, amount, settled-at
    confirms = iter([True, True])  # "settle now?" then "confirm?"
    monkeypatch.setattr(Prompt, "ask", lambda *a, **k: next(prompts))
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: next(confirms))

    result = runner.invoke(app, ["settle"])
    assert result.exit_code == 0, result.output
    assert "1 bet(s) settled" in result.output

    with _open_warehouse(data_dir) as w:
        assert w.bets.open_bets() == []


# ------------------------------------------- promoted bets (SB-1084)


def _attach_boost(w: Warehouse, bet: Bet, pct: str = "50") -> None:
    from bet.models.bet import BetPromotion

    w.bet_promotions.add(
        BetPromotion(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=bet.id,
            promotion_type="profit_boost",
            scope="ticket",
            apply_order=1,
            generosity_pct=Decimal(pct),
        )
    )


def test_a_won_promoted_bet_refuses_to_compute_its_own_payout(data_dir: Path) -> None:
    """Placed odds are the BASE price, so computing from them drops the boost.

    Without this guard the bet below settles at the unboosted payout and calls
    it correct -- smaller than reality, plausible, and undetectable afterwards.
    """
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))
        _attach_boost(w, bet)

    result = _settle(str(bet.id), "--result", "won")
    assert result.exit_code != 0
    assert "--return" in str(result.exception)


def test_a_won_promoted_bet_settles_on_the_stated_amount(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))
        _attach_boost(w, bet)

    result = _settle(str(bet.id), "--result", "won", "--return", "20.00")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled is not None
        assert settled.cash_returned == Decimal("20.00")


def test_an_unpromoted_bet_still_computes_its_payout(data_dir: Path) -> None:
    """The guard must not widen to bets that never carried a promotion."""
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", "won")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled is not None
        assert settled.cash_returned == Decimal("16.67")


def test_a_lost_promoted_bet_needs_no_stated_amount(data_dir: Path) -> None:
    """A loss returns nothing whether or not a boost was attached."""
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))
        _attach_boost(w, bet)

    result = _settle(str(bet.id), "--result", "lost")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled = w.bets.get(bet.id)
        assert settled is not None
        assert settled.cash_returned == Decimal("0.00")


# --------------------------------------- single-leg cascade (SB-1140)


@pytest.mark.parametrize("outcome", ["won", "lost", "push", "void"])
def test_settling_a_single_records_its_leg_result(data_dir: Path, outcome: str) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", outcome)
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        assert [leg.result for leg in w.legs.for_bet(bet.id)] == [outcome]


def test_a_cashed_out_single_leaves_its_leg_undecided(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", "cashed_out", "--return", "8.00")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        assert [leg.result for leg in w.legs.for_bet(bet.id)] == [None]


def test_an_explicit_leg_result_on_a_single_wins(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w))

    result = _settle(str(bet.id), "--result", "lost", "--leg-result", "1=lost")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        assert [leg.result for leg in w.legs.for_bet(bet.id)] == ["lost"]


def test_a_parlay_ticket_result_does_not_cascade_to_its_legs(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w), num_legs=2)

    result = _settle(str(bet.id), "--result", "won")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        assert [leg.result for leg in w.legs.for_bet(bet.id)] == [None, None]


# ------------------------------------------------- stale default (SB-1521)


def test_stale_bet_refuses_default_settled_at(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w), placed_at=STALE)

    result = _settle(str(bet.id), "--result", "won")
    assert isinstance(result.exception, UsageError)
    assert "--settled-at" in (result.exception.remediation or "")

    with _open_warehouse(data_dir) as w:
        assert w.bets.get(bet.id).status != "settled"


def test_stale_bet_accepts_explicit_settled_at(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w), placed_at=STALE)

    at = (STALE + timedelta(hours=3)).isoformat()
    result = _settle(str(bet.id), "--result", "won", "--settled-at", at)
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        assert w.bets.get(bet.id).settled_at == datetime.fromisoformat(at)


def test_stale_bet_accepts_settled_at_now(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _make_bet(w, _make_account(w), placed_at=STALE)

    before = datetime.now(UTC)
    result = _settle(str(bet.id), "--result", "lost", "--settled-at", "now")
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        settled_at = w.bets.get(bet.id).settled_at
        assert settled_at is not None and settled_at >= before
