"""Regressions from the SB-1333 code review: one test group per finding.

Cumulative-gap status, local-time --at, misplaced group options, strict
account labels, stderr warnings, markup-safe labels, baseline rows, money
bounds, pending migrations, table width, query count.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from bet.analytics.balance import BetActivity, latest, reconcile, total
from bet.cli.capture import parse_money, parse_timestamp
from bet.cli.main import app
from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import BetRepository, Warehouse
from bet.errors import DatabaseError, UsageError
from bet.models.balance import BalanceSnapshot, CashFlow

runner = CliRunner()

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = datetime(2026, 10, 2, tzinfo=UTC)
T2 = datetime(2026, 10, 3, tzinfo=UTC)

ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8cfc0f01f00050001ff89993d1d"
    "0000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "data").mkdir()
    for name in list(dict(os.environ)):
        if name.startswith("BET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _run(*args: str) -> Result:
    return runner.invoke(app, list(args))


def _ok(*args: str) -> Result:
    result = _run(*args)
    assert result.exit_code == 0, result.output
    return result


def _json(*args: str) -> list[dict[str, Any]]:
    result = _ok("--format", "json", *args)
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    return rows


def _warehouse_rows(data: Path, sql: str) -> list[tuple[Any, ...]]:
    with connect(Settings(data_dir=data)) as conn:
        return conn.execute(sql).fetchall()


def _snap(balance: str, at: datetime) -> BalanceSnapshot:
    return BalanceSnapshot(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        observed_at=at,
        cash_balance=Decimal(balance),
    )


def _deposit(amount: str, at: datetime) -> CashFlow:
    return CashFlow(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        kind="deposit",
        occurred_at=at,
        amount=Decimal(amount),
    )


def _late_settlement(after: datetime, through: datetime) -> BetActivity:
    """A return the book paid in (T0, T1] but recorded as settled in (T1, T2]."""
    return BetActivity(returned=Decimal("10.00") if through == T2 else Decimal("0.00"))


# ---------------------------------------------------------- A. cumulative gap


class TestCumulativeGap:
    def test_a_late_settlement_cancels_across_the_span(self) -> None:
        snaps = [_snap("100.00", T0), _snap("110.00", T1), _snap("110.00", T2)]
        history = reconcile("a", snaps, [], _late_settlement)
        assert [r["gap"] for r in history[1:]] == [Decimal("10.00"), Decimal("-10.00")]
        assert [r["status"] for r in history] == ["baseline", "GAP", "GAP"]
        assert history[-1]["cumulative_gap"] == Decimal("0.00")

        row = latest("a", snaps, [], _late_settlement)
        assert row is not None
        assert row["gap"] == Decimal("-10.00")
        assert row["cumulative_gap"] == Decimal("0.00")
        assert row["status"] == "ok"

    def test_a_real_gap_survives_a_routine_snapshot(self) -> None:
        snaps = [_snap("100.00", T0), _snap("95.00", T1), _snap("95.00", T2)]
        row = latest("a", snaps, [], lambda a, b: BetActivity())
        assert row is not None
        assert row["gap"] == Decimal("0.00")
        assert row["cumulative_gap"] == Decimal("-5.00")
        assert row["status"] == "GAP"
        tot = total([row])
        assert (tot["cumulative_gap"], tot["status"]) == (Decimal("-5.00"), "GAP")

    def test_history_total_uses_cumulative_not_the_last_interval(self) -> None:
        snaps = [_snap("100.00", T0), _snap("95.00", T1), _snap("95.00", T2)]
        rows = reconcile("a", snaps, [], lambda a, b: BetActivity())
        assert rows[-1]["status"] == "ok"
        assert total(rows[-1:])["status"] == "GAP"

    def test_offsetting_cumulative_gaps_across_accounts_still_read_gap(self) -> None:
        up = latest("a", [_snap("10.00", T0), _snap("15.00", T1)], [], lambda a, b: BetActivity())
        down = latest("b", [_snap("10.00", T0), _snap("5.00", T1)], [], lambda a, b: BetActivity())
        assert up is not None and down is not None
        tot = total([up, down])
        assert (tot["cumulative_gap"], tot["status"]) == (Decimal("0.00"), "GAP")

    def test_latest_looks_up_only_the_last_interval_and_the_span(self) -> None:
        seen: list[tuple[datetime, datetime]] = []

        def activity(a: datetime, b: datetime) -> BetActivity:
            seen.append((a, b))
            return BetActivity()

        t3 = T2 + timedelta(days=1)
        snaps = [_snap("1.00", t) for t in (T0, T1, T2, t3)]
        latest("a", snaps, [], activity)
        assert seen == [(T2, t3), (T0, t3)]

        seen.clear()
        latest("a", snaps[:2], [], activity)
        assert seen == [(T0, T1)]

    def test_cli_status_follows_the_cumulative_gap(self) -> None:
        _ok("init")
        for amount, day in (("100", "01"), ("95", "02"), ("95", "03")):
            _ok("balance", "add", "--sportsbook", "fanduel", "--amount", amount,
                "--at", f"2026-10-{day}T12:00:00+00:00")  # fmt: skip
        row, tot = _json("balance")
        assert (row["gap"], row["cumulative_gap"], row["status"]) == ("0.00", "-5.00", "GAP")
        assert (tot["cumulative_gap"], tot["status"]) == ("-5.00", "GAP")

        history = _json("balance", "--history")
        assert [r["status"] for r in history] == ["baseline", "GAP", "ok", "GAP"]
        assert history[-1]["account"] == "TOTAL"


# ---------------------------------------------------------- B. --at handling


class TestLocalAt:
    def test_a_date_only_at_is_local_midnight(self, data_dir: Path, new_york: None) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "10", "--at", "2026-10-01")
        _ok("deposit", "--sportsbook", "fanduel", "--amount", "5", "--at", "2026-10-01T09:30")
        [(observed,)] = _warehouse_rows(data_dir, "SELECT observed_at FROM core.balance_snapshot")
        [(occurred,)] = _warehouse_rows(data_dir, "SELECT occurred_at FROM core.cash_flow")
        assert observed == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
        assert occurred == datetime(2026, 10, 1, 13, 30, tzinfo=UTC)

    def test_bet_add_placed_at_still_reads_naive_as_utc(self, new_york: None) -> None:
        assert parse_timestamp("2026-10-01", option="--placed-at") == T0

    @pytest.mark.parametrize("command", [("balance", "add"), ("deposit",), ("withdraw",)])
    def test_far_future_at_is_refused(self, data_dir: Path, command: tuple[str, ...]) -> None:
        _ok("init")
        result = _run(*command, "--sportsbook", "fanduel", "--amount", "5", "--at", "2062-10-01")
        assert isinstance(result.exception, UsageError)
        assert "future" in result.exception.message

    def test_a_moment_within_the_tolerance_is_accepted(self) -> None:
        _ok("init")
        soon = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "5", "--at", soon)

    @pytest.mark.parametrize("command", [("balance", "add"), ("deposit",), ("withdraw",)])
    def test_help_states_the_local_time_zone(self, command: tuple[str, ...]) -> None:
        result = _ok(*command, "--help")
        text = " ".join(result.stdout.replace("│", " ").split())
        assert "local time zone" in text


# ------------------------------------------------- C. misplaced group options


class TestMisplacedGroupOptions:
    @pytest.mark.parametrize(
        ("before", "named"),
        [
            (("--sportsbook", "fanduel"), "--sportsbook"),
            (("--account-label", "main"), "--account-label"),
            (("--history",), "--history"),
        ],
    )
    def test_group_options_before_a_subcommand_are_refused(
        self, data_dir: Path, before: tuple[str, ...], named: str
    ) -> None:
        _ok("init")
        result = _run("balance", *before, "add", "--sportsbook", "fanduel", "--amount", "5")
        assert isinstance(result.exception, UsageError)
        assert result.exception.exit_code == 64
        assert named in result.exception.message
        assert _warehouse_rows(data_dir, "SELECT count(*) FROM core.balance_snapshot") == [(0,)]


# ------------------------------------------------------- D. account labels


class TestStrictAccountLabels:
    def test_labels_match_ignoring_case_and_whitespace(self, data_dir: Path) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--account-label", "Main",
            "--amount", "10")  # fmt: skip
        _ok("deposit", "--sportsbook", "fanduel", "--account-label", "  main ", "--amount", "5")
        assert _warehouse_rows(data_dir, "SELECT label FROM core.sportsbook_account") == [("Main",)]

    def test_an_unmatched_label_lists_the_existing_ones_and_writes_nothing(
        self, data_dir: Path
    ) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--account-label", "main",
            "--amount", "10")  # fmt: skip
        for command in (("balance", "add"), ("deposit",), ("withdraw",)):
            result = _run(*command, "--sportsbook", "fanduel", "--account-label", "mian",
                          "--amount", "5")  # fmt: skip
            assert isinstance(result.exception, UsageError)
            assert "main" in (result.exception.remediation or "")
        assert _warehouse_rows(data_dir, "SELECT count(*) FROM core.sportsbook_account") == [(1,)]
        assert _warehouse_rows(data_dir, "SELECT count(*) FROM core.cash_flow") == [(0,)]

    def test_the_first_account_at_a_book_is_created_with_a_notice(self) -> None:
        _ok("init")
        result = _ok("deposit", "--sportsbook", "fanduel", "--account-label", "main",
                     "--amount", "5")  # fmt: skip
        assert "Created account fanduel:main." in result.stderr
        assert "Created account" not in result.stdout

    def test_the_sportsbook_code_is_normalised_for_every_caller(self, data_dir: Path) -> None:
        _ok("init")
        _ok("deposit", "--sportsbook", " FanDuel ", "--amount", "5")
        _ok("add", "--non-interactive", "--sportsbook", "FANDUEL",
            "--leg", "sport=NFL,selection=A,odds=+100", "--stake", "1")  # fmt: skip
        assert _warehouse_rows(data_dir, "SELECT sportsbook_code FROM core.sportsbook_account") == [
            ("fanduel",)
        ]

    def test_bet_add_still_creates_a_new_labelled_account(self, data_dir: Path) -> None:
        _ok("init")
        _ok("deposit", "--sportsbook", "fanduel", "--amount", "5")
        _ok("add", "--non-interactive", "--sportsbook", "fanduel", "--account-label", "boosts",
            "--leg", "sport=NFL,selection=A,odds=+100", "--stake", "1")  # fmt: skip
        assert _warehouse_rows(data_dir, "SELECT count(*) FROM core.sportsbook_account") == [(2,)]


# ------------------------------------------------- E. warnings stay on stderr


SCREENSHOT = ("--sportsbook", "fanduel", "--capture-method", "screenshot")


@pytest.mark.parametrize(
    "command",
    [
        ("balance", "add", "--amount", "5", *SCREENSHOT),
        ("deposit", "--amount", "5", *SCREENSHOT),
        ("add", "--non-interactive", "--leg", "sport=NFL,selection=A,odds=+100",
         "--stake", "5", *SCREENSHOT),
    ],
)  # fmt: skip
def test_a_provenance_warning_never_corrupts_json(command: tuple[str, ...]) -> None:
    _ok("init")
    result = _ok("--format", "json", *command)
    assert isinstance(json.loads(result.stdout), list)
    assert "no --source-file" in result.stderr


# ------------------------------------------------------ F. markup in labels


@pytest.mark.parametrize("label", ["[/old]", "[old]", "[bold]x"])
def test_a_label_that_looks_like_markup_prints_as_written(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setenv("COLUMNS", "250")
    _ok("init")
    result = _ok("balance", "add", "--sportsbook", "fanduel", "--account-label", label,
                 "--amount", "10", "--at", "2026-10-01")  # fmt: skip
    assert f"Created account fanduel:{label}." in result.stderr
    assert f"fanduel:{label}" in result.stdout
    assert f"fanduel:{label}" in _ok("balance").stdout
    result = _ok("deposit", "--sportsbook", "fanduel", "--amount", "5")
    assert f"fanduel:{label}" in result.stdout
    assert _warehouse_rows(data_dir, "SELECT count(*) FROM core.balance_snapshot") == [(1,)]


# ---------------------------------------------------------- G. baseline rows


class TestBaselineRows:
    def test_a_baseline_has_no_interval_figures(self) -> None:
        [row] = reconcile("a", [_snap("50.00", T1)], [_deposit("50.00", T0)], lambda a, b: None)  # type: ignore[arg-type,return-value]
        for key in ("deposits", "withdrawals", "staked", "returned", "expected", "gap"):
            assert row[key] is None, key
        assert row["cumulative_gap"] is None
        assert row["net_deposited"] == Decimal("50.00")

    def test_total_sums_only_interval_deposits(self) -> None:
        base = latest(
            "a", [_snap("50.00", T1)], [_deposit("50.00", T0)], lambda a, b: BetActivity()
        )
        moved = latest(
            "b",
            [_snap("10.00", T0), _snap("13.00", T2)],
            [_deposit("3.00", T1)],
            lambda a, b: BetActivity(),
        )
        assert base is not None and moved is not None
        tot = total([base, moved])
        assert tot["deposits"] == Decimal("3.00")
        assert tot["withdrawals"] == Decimal("0.00")
        assert tot["net_deposited"] == Decimal("53.00")

    def test_total_over_baselines_only_has_no_interval_deposits(self) -> None:
        base = latest(
            "a", [_snap("50.00", T1)], [_deposit("50.00", T0)], lambda a, b: BetActivity()
        )
        assert base is not None
        assert total([base])["deposits"] is None


# ------------------------------------------------------------- H. money bounds


@pytest.mark.parametrize(
    "raw", ["NaN", "nan", "sNaN", "Infinity", "-Infinity", "1e10", "10000000000", "9999999999.995"]
)
def test_parse_money_refuses_non_finite_and_oversized(raw: str) -> None:
    with pytest.raises(UsageError):
        parse_money(raw, field_name="--amount")


def test_parse_money_accepts_the_largest_storable_amount() -> None:
    assert parse_money("9999999999.99", field_name="--amount") == Decimal("9999999999.99")


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "1e10"])
def test_balance_add_refuses_unstorable_amounts_as_usage(data_dir: Path, raw: str) -> None:
    _ok("init")
    result = _run("balance", "add", "--sportsbook", "fanduel", "--amount", raw)
    assert isinstance(result.exception, UsageError)
    assert _warehouse_rows(data_dir, "SELECT count(*) FROM core.balance_snapshot") == [(0,)]


# -------------------------------------------------------- I. pending migration


@pytest.fixture
def warehouse_at_0008(data_dir: Path, tmp_path: Path) -> None:
    older = tmp_path / "migrations_0008"
    older.mkdir()
    for path in sorted(migrator._dir(None).glob("*.sql")):
        if int(path.name[:4]) <= 8:
            shutil.copy(path, older / path.name)
    with connect(Settings(data_dir=data_dir)) as conn:
        migrator.apply(conn, older)


@pytest.mark.parametrize(
    "command",
    [
        ("balance",),
        ("balance", "add", "--sportsbook", "fanduel", "--amount", "5"),
        ("deposit", "--sportsbook", "fanduel", "--amount", "5"),
        ("withdraw", "--sportsbook", "fanduel", "--amount", "5"),
    ],
)
def test_a_pending_migration_says_run_bet_init(
    warehouse_at_0008: None, command: tuple[str, ...]
) -> None:
    result = _run(*command)
    assert isinstance(result.exception, DatabaseError)
    assert "0009_balance_ledger" in result.exception.message
    assert "bet init" in (result.exception.remediation or "")


# ------------------------------------------------------------ J. table width


@pytest.mark.parametrize("width", ["80", "120"])
def test_the_table_never_ellipsizes(monkeypatch: pytest.MonkeyPatch, width: str) -> None:
    monkeypatch.setenv("COLUMNS", width)
    _ok("init")
    for amount, day in (("1234567.89", "01"), ("1234500.01", "02")):
        _ok("balance", "add", "--sportsbook", "draftkings", "--account-label", "longlabel",
            "--amount", amount, "--at", f"2026-10-{day}T12:00:00+00:00")  # fmt: skip
    for args in (("balance",), ("balance", "--history"), ("--verbose", "balance")):
        assert "…" not in _ok(*args).stdout, args


def test_at_120_columns_the_default_table_shows_names_and_money_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "120")
    _ok("init")
    for amount, day in (("1234567.89", "01"), ("1234500.01", "02")):
        _ok("balance", "add", "--sportsbook", "draftkings", "--amount", amount,
            "--at", f"2026-10-{day}T12:00:00+00:00")  # fmt: skip
    out = _ok("balance").stdout
    for text in ("draftkings", "1234500.01", "-67.88"):
        assert text in out


def test_the_table_hides_diagnostics_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok("init")
    _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "5", "--at", "2026-10-01")
    monkeypatch.setenv("COLUMNS", "250")
    assert "expected" not in _ok("balance").stdout
    assert "expected" in _ok("--verbose", "balance").stdout
    assert {"expected", "staked", "returned", "prev_balance", "gap"} <= set(_json("balance")[0])


# ----------------------------------------------------------- K. query count


def test_the_default_view_looks_up_two_intervals_per_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ok("init")
    for day in ("01", "02", "03", "04", "05"):
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "5",
            "--at", f"2026-10-{day}T12:00:00+00:00")  # fmt: skip
    calls: list[tuple[datetime, datetime]] = []
    original = BetRepository.reconciliation_inputs

    def counting(self: BetRepository, account_id: Any, after: datetime, through: datetime) -> Any:
        calls.append((after, through))
        return original(self, account_id, after, through)

    monkeypatch.setattr(BetRepository, "reconciliation_inputs", counting)
    _ok("balance")
    assert len(calls) == 2
    calls.clear()
    _ok("balance", "--history")
    assert len(calls) == 4


def test_reconciliation_inputs_is_one_statement(data_dir: Path) -> None:
    _ok("init")
    settings = Settings(data_dir=data_dir)
    emitted: list[str] = []
    with connect(settings) as conn:
        scope = identity.resolve_scope(conn, type("_Config", (), {"settings": settings})())

        class Recorder:
            def execute(self, sql: str, params: Any = None) -> Any:
                emitted.append(sql)
                return conn.execute(sql, params)

        Warehouse(Recorder(), scope).bets.reconciliation_inputs(uuid.uuid4(), T0, T1)  # type: ignore[arg-type]
    assert len(emitted) == 1
