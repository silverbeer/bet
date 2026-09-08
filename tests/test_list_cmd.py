"""``bet list`` — filters and pending/settled rendering.

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

import pytest
from typer.testing import CliRunner

from bet.cli.main import app
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg
from bet.models.ownership import SportsbookAccount

runner = CliRunner()

PLACED = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)


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


def _seed_two_bets(w: Warehouse) -> tuple[Bet, Bet]:
    account = w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code="fanduel",
            label="main",
        )
    )
    pending = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            placed_at=PLACED,
            cash_staked=Decimal("10.00"),
        )
    )
    w.legs.add(
        BetLeg(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=pending.id,
            leg_order=1,
            sport="MLB",
            selection_name="Red Sox",
            odds_american=-145,
        )
    )
    settled = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            placed_at=PLACED,
            cash_staked=Decimal("10.00"),
            status="settled",
            result="won",
            settled_at=PLACED,
            cash_returned=Decimal("16.67"),
        )
    )
    w.legs.add(
        BetLeg(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=settled.id,
            leg_order=1,
            sport="NFL",
            selection_name="Chiefs",
            odds_american=-150,
        )
    )
    return pending, settled


def test_list_shows_both_pending_and_settled(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _seed_two_bets(w)

    result = runner.invoke(app, ["--format", "json", "list"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 2


def test_list_open_shows_only_pending(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        pending, _settled = _seed_two_bets(w)

    result = runner.invoke(app, ["--format", "json", "list", "--open"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["id"] == str(pending.id)
    assert rows[0]["status"] == "pending"
    assert rows[0]["profit_loss"] is None


def test_list_settled_status_shows_result_and_profit(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _pending, settled = _seed_two_bets(w)

    result = runner.invoke(app, ["--format", "json", "list", "--status", "settled"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["id"] == str(settled.id)
    assert rows[0]["status"] == "won"
    assert rows[0]["profit_loss"] == "6.67"


def test_list_filters_by_sport(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _seed_two_bets(w)

    result = runner.invoke(app, ["--format", "json", "--sport", "NFL", "list"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["selection"] == "Chiefs"


def test_list_open_and_status_settled_conflict() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["list", "--open", "--status", "settled"])
    assert isinstance(result.exception, UsageError)


def test_list_rejects_an_unknown_status() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["list", "--status", "nonsense"])
    assert isinstance(result.exception, UsageError)
