"""Balances, deposits and withdrawals, and reconciliation against bets (SB-1333).

The property under test: given two reported balances, the cash flows and the
bets between them, the second balance is predicted to the cent, and anything
the ledger cannot explain surfaces as a gap rather than disappearing.

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
from pydantic import ValidationError
from typer.testing import CliRunner, Result

from bet.analytics.balance import BetActivity, reconcile, total
from bet.cli.main import app
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.balance import BalanceSnapshot, CashFlow

runner = CliRunner()

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = datetime(2026, 10, 5, tzinfo=UTC)
TENANT, USER, ACCOUNT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8cfc0f01f00050001ff89993d1d"
    "0000000049454e44ae426082"
)


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


def _bet(*args: str) -> Result:
    return runner.invoke(app, list(args))


def _json(*args: str) -> list[dict[str, Any]]:
    result = runner.invoke(app, ["--format", "json", *args])
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    return rows


def _snap(balance: str, at: datetime) -> BalanceSnapshot:
    return BalanceSnapshot(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=ACCOUNT,
        observed_at=at,
        cash_balance=Decimal(balance),
    )


def _flow(kind: str, amount: str, at: datetime) -> CashFlow:
    return CashFlow(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=ACCOUNT,
        kind=kind,  # type: ignore[arg-type]
        occurred_at=at,
        amount=Decimal(amount),
    )


# ------------------------------------------------------------------- models


def test_a_snapshot_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _snap("10.00", datetime(2026, 10, 1))


def test_a_snapshot_rejects_negative_money() -> None:
    with pytest.raises(ValidationError):
        _snap("-1.00", T0)


def test_a_cash_flow_must_move_money() -> None:
    with pytest.raises(ValidationError, match="positive"):
        _flow("deposit", "0.00", T0)


# ------------------------------------------------------------ reconciliation


def test_the_first_snapshot_is_a_baseline_with_no_gap() -> None:
    [row] = reconcile("fanduel", [_snap("100.00", T0)], [], lambda a, b: BetActivity())
    assert row["status"] == "baseline"
    assert row["gap"] is None


def test_a_fully_explained_interval_reconciles_to_zero() -> None:
    flows = [_flow("deposit", "50.00", T0.replace(day=2)), _flow("withdrawal", "20.00", T1)]
    activity = BetActivity(staked=Decimal("10.00"), returned=Decimal("25.00"))
    rows = reconcile(
        "fanduel", [_snap("100.00", T0), _snap("145.00", T1)], flows, lambda a, b: activity
    )
    last = rows[-1]
    assert last["expected"] == Decimal("145.00")
    assert last["gap"] == Decimal("0.00")
    assert last["status"] == "ok"
    assert last["change"] == Decimal("45.00")
    assert last["net_deposited"] == Decimal("30.00")


def test_an_unexplained_shortfall_is_a_gap() -> None:
    rows = reconcile(
        "fanduel", [_snap("100.00", T0), _snap("95.00", T1)], [], lambda a, b: BetActivity()
    )
    assert rows[-1]["gap"] == Decimal("-5.00")
    assert rows[-1]["status"] == "GAP"


def test_a_flow_at_the_opening_snapshot_belongs_to_the_earlier_interval() -> None:
    """Intervals are (t0, t1]: a deposit stamped exactly at t0 is already in B0."""
    rows = reconcile(
        "fanduel",
        [_snap("150.00", T0), _snap("150.00", T1)],
        [_flow("deposit", "50.00", T0)],
        lambda a, b: BetActivity(),
    )
    assert rows[-1]["deposits"] == Decimal("0.00")
    assert rows[-1]["status"] == "ok"


def test_total_sums_balances_and_only_the_gaps_that_exist() -> None:
    baseline = reconcile("a", [_snap("30.00", T1)], [], lambda a, b: BetActivity())[-1]
    gapped = reconcile(
        "b", [_snap("100.00", T0), _snap("90.00", T1)], [], lambda a, b: BetActivity()
    )[-1]
    row = total([baseline, gapped])
    assert row["account"] == "TOTAL"
    assert row["balance"] == Decimal("120.00")
    assert row["cumulative_gap"] == Decimal("-10.00")
    assert row["status"] == "GAP"


def test_total_with_only_baselines_has_no_gap() -> None:
    baseline = reconcile("a", [_snap("30.00", T1)], [], lambda a, b: BetActivity())[-1]
    assert total([baseline])["cumulative_gap"] is None
    assert total([baseline])["status"] == "baseline"


# ------------------------------------------------------------------- the CLI


def test_balance_reconciles_against_flows_and_bets(data_dir: Path) -> None:
    assert _bet("init").exit_code == 0
    steps = [
        ("balance", "add", "--sportsbook", "fanduel", "--amount", "100", "--at", "2026-10-01"),
        ("deposit", "--sportsbook", "fanduel", "--amount", "50", "--at", "2026-10-02"),
        (
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--leg",
            "sport=NFL,selection=A,odds=+100",
            "--stake",
            "10",
            "--placed-at",
            "2026-10-03",
        ),
        ("withdraw", "--sportsbook", "fanduel", "--amount", "20", "--at", "2026-10-04"),
        ("balance", "add", "--sportsbook", "fanduel", "--amount", "120", "--at", "2026-10-05"),
    ]
    for step in steps:
        result = _bet(*step)
        assert result.exit_code == 0, result.output

    rows = _json("balance")
    fanduel, tot = rows
    assert fanduel["account"] == "fanduel"
    assert fanduel["staked"] == "10.00"
    assert fanduel["expected"] == "120.00"
    assert fanduel["gap"] == "0.00"
    assert fanduel["status"] == "ok"
    assert tot["account"] == "TOTAL"
    assert tot["net_deposited"] == "30.00"


def test_bonus_stake_and_bonus_balance_stay_out_of_cash(data_dir: Path) -> None:
    assert _bet("init").exit_code == 0
    assert (
        _bet("balance", "add", "--sportsbook", "fanduel", "--amount", "100", "--at", "2026-10-01")
    ).exit_code == 0
    result = _bet(
        "add",
        "--non-interactive",
        "--sportsbook",
        "fanduel",
        "--leg",
        "sport=NFL,selection=A,odds=+100",
        "--bonus-stake",
        "10",
        "--placed-at",
        "2026-10-02",
    )
    assert result.exit_code == 0, result.output
    assert (
        _bet(
            "balance",
            "add",
            "--sportsbook",
            "fanduel",
            "--amount",
            "100",
            "--bonus-amount",
            "40",
            "--at",
            "2026-10-03",
        )
    ).exit_code == 0

    fanduel = _json("balance")[0]
    assert fanduel["staked"] == "0.00"
    assert fanduel["status"] == "ok"


def test_total_spans_every_account_and_history_shows_every_interval(data_dir: Path) -> None:
    assert _bet("init").exit_code == 0
    for args in (
        ("--sportsbook", "fanduel", "--amount", "100", "--at", "2026-10-01"),
        ("--sportsbook", "fanduel", "--amount", "90", "--at", "2026-10-02"),
        ("--sportsbook", "draftkings", "--account-label", "boosts", "--amount", "30"),
    ):
        assert _bet("balance", "add", *args).exit_code == 0

    rows = _json("balance")
    assert [r["account"] for r in rows] == ["draftkings:boosts", "fanduel", "TOTAL"]
    assert rows[-1]["balance"] == "120.00"
    assert rows[-1]["cumulative_gap"] == "-10.00"

    assert len(_json("balance", "--history")) == 4
    assert [r["account"] for r in _json("balance", "--sportsbook", "fanduel")] == [
        "fanduel",
        "TOTAL",
    ]


def test_balance_refuses_an_unknown_sportsbook(data_dir: Path) -> None:
    assert _bet("init").exit_code == 0
    assert isinstance(_bet("balance", "--sportsbook", "nope").exception, UsageError)


def test_a_zero_deposit_is_refused(data_dir: Path) -> None:
    assert _bet("init").exit_code == 0
    result = _bet("deposit", "--sportsbook", "fanduel", "--amount", "0")
    assert isinstance(result.exception, UsageError)


def test_a_manual_balance_cannot_cite_a_source_file(data_dir: Path, tmp_path: Path) -> None:
    slip = tmp_path / "IMG_0001.PNG"
    slip.write_bytes(ONE_PIXEL_PNG)
    assert _bet("init").exit_code == 0
    result = _bet(
        "balance", "add", "--sportsbook", "fanduel", "--amount", "10", "--source-file", str(slip)
    )
    assert isinstance(result.exception, UsageError)


def test_a_screenshot_balance_and_withdrawal_cite_the_archived_file(
    data_dir: Path, tmp_path: Path
) -> None:
    slip = tmp_path / "IMG_0002.PNG"
    slip.write_bytes(ONE_PIXEL_PNG)
    assert _bet("init").exit_code == 0
    common = ("--sportsbook", "fanduel", "--capture-method", "screenshot", "--source-file")
    assert _bet("balance", "add", "--amount", "10", *common, str(slip)).exit_code == 0
    assert _bet("withdraw", "--amount", "5", *common, str(slip)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        [snapshot] = w.balances.fetch_all()
        [flow] = w.cash_flows.fetch_all()
        [source] = w.sources.fetch_all()
    assert snapshot.capture_method == "screenshot"
    assert snapshot.source_file_id == source.id
    assert flow.kind == "withdrawal"
    assert flow.source_file_id == source.id


def test_new_queries_carry_the_ownership_predicate(data_dir: Path) -> None:
    assert _bet("init").exit_code == 0
    emitted: list[str] = []
    with _open_warehouse(data_dir) as w:

        class Recorder:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def execute(self, sql: str, params: Any = None) -> Any:
                emitted.append(sql)
                return self._inner.execute(sql, params)

        recording = Warehouse(Recorder(w._conn), w.scope)  # type: ignore[arg-type]
        account = uuid.uuid4()
        recording.balances.for_account(account)
        recording.cash_flows.for_account(account)
        recording.bets.reconciliation_inputs(account, T0, T1)

    assert len(emitted) == 3
    assert all("tenant_id = ? AND user_id = ?" in sql for sql in emitted)
