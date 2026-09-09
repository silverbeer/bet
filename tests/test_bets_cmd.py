"""``bets show``/``open``/``search`` — record-level inspection (SB-745).

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
from bet.cli.tree import NotImplementedYetError
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import NotFoundError, UsageError
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


def _seed_bet(w: Warehouse, *, selection: str, status: str = "pending") -> Bet:
    account = w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code="fanduel",
            label="main",
        )
    )
    fields: dict[str, object] = {"cash_staked": Decimal("10.00")}
    if status == "settled":
        fields |= {
            "status": "settled",
            "result": "won",
            "settled_at": PLACED,
            "cash_returned": Decimal("16.67"),
        }
    bet = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            placed_at=PLACED,
            **fields,  # type: ignore[arg-type]
        )
    )
    w.legs.add(
        BetLeg(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=bet.id,
            leg_order=1,
            sport="NFL",
            selection_name=selection,
            odds_american=-150,
        )
    )
    return bet


# ------------------------------------------------------------------- show


def test_show_traces_a_bet_to_its_provenance(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        bet = _seed_bet(w, selection="Chiefs -3.5")

    result = runner.invoke(app, ["--format", "json", "bets", "show", str(bet.id)])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    row = rows[0]
    assert row["bet_id"] == str(bet.id)
    assert row["selection"] == "Chiefs -3.5"
    assert row["capture_method"] == "manual"
    assert row["import_run_id"] is None
    assert row["promotions"] == 0


def test_show_rejects_an_invalid_id() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["bets", "show", "not-a-uuid"])
    assert isinstance(result.exception, UsageError)


def test_show_unknown_bet_is_not_found() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["bets", "show", str(uuid.uuid4())])
    assert isinstance(result.exception, NotFoundError)


# ------------------------------------------------------------------- open


def test_open_shows_only_pending(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        pending = _seed_bet(w, selection="Chiefs -3.5")
        _seed_bet(w, selection="Bills", status="settled")

    result = runner.invoke(app, ["--format", "json", "bets", "open"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["id"] == str(pending.id)


# ----------------------------------------------------------------- search


def test_search_matches_selection_text(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        chiefs = _seed_bet(w, selection="Chiefs -3.5")
        _seed_bet(w, selection="Bills")

    result = runner.invoke(app, ["--format", "json", "bets", "search", "chief"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["id"] == str(chiefs.id)


def test_search_finds_nothing_for_an_unmatched_query(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _seed_bet(w, selection="Chiefs -3.5")

    result = runner.invoke(app, ["--format", "json", "bets", "search", "nonexistent-team"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


# ----------------------------------------------------------------- correct


def test_correct_remains_a_stub() -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["bets", "correct", str(uuid.uuid4())])
    assert isinstance(result.exception, NotImplementedYetError)
    assert "SB-703" in (result.exception.remediation or "")
