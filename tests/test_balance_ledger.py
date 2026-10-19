"""Boundaries, isolation and schema guarantees of the balance ledger (SB-1333).

Complements ``test_balance.py``: exact interval edges, bet-timing buckets,
superseded versions, tenant isolation, CHECK constraints and rendering.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import csv
import io
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from bet.analytics.balance import BetActivity, reconcile
from bet.cli.main import app
from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet
from bet.models.ownership import OwnerScope, SportsbookAccount

runner = CliRunner()

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
EPS = timedelta(seconds=1)

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
def two_users(tmp_path: Path) -> Iterator[tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse]]:
    data = tmp_path / "wh"
    data.mkdir()
    with connect(Settings(data_dir=data)) as conn:
        migrator.apply(conn)
        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        scopes = []
        for tenant, name in ((tenant_a, "alice"), (tenant_b, "bob")):
            user = uuid.uuid4()
            conn.execute("INSERT INTO core.tenant (id, name) VALUES (?, ?)", [str(tenant), name])
            conn.execute(
                "INSERT INTO core.user (tenant_id, id, display_name) VALUES (?, ?, ?)",
                [str(tenant), str(user), name],
            )
            scopes.append(OwnerScope(tenant_id=tenant, user_id=user))
        yield conn, Warehouse(conn, scopes[0]), Warehouse(conn, scopes[1])


def _account(w: Warehouse, code: str = "fanduel") -> SportsbookAccount:
    return w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code=code,
            label=code,
        )
    )


def _bet(w: Warehouse, account: SportsbookAccount, **fields: Any) -> Bet:
    defaults: dict[str, Any] = {"cash_staked": Decimal("10.00"), "placed_at": T0 + EPS}
    return w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            **(defaults | fields),
        )
    )


def _settle(**fields: Any) -> dict[str, Any]:
    return {"status": "settled", "result": "won", "settled_at": T0 + 2 * EPS} | fields


def _inputs(w: Warehouse, account: SportsbookAccount) -> tuple[Decimal, Decimal]:
    return w.bets.reconciliation_inputs(account.id, T0, T1)


# ------------------------------------------------- reconciliation_inputs


@pytest.fixture
def wh(two_users: Any) -> tuple[Warehouse, SportsbookAccount]:
    _, alice, _ = two_users
    return alice, _account(alice)


class TestReconciliationInputs:
    def test_empty_account_is_zero(self, wh: Any) -> None:
        w, acct = wh
        assert _inputs(w, acct) == (Decimal(0), Decimal(0))

    def test_stake_placed_exactly_at_t0_is_excluded(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, placed_at=T0)
        assert _inputs(w, acct)[0] == Decimal(0)

    def test_stake_placed_exactly_at_t1_is_included(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, placed_at=T1)
        assert _inputs(w, acct)[0] == Decimal("10.00")

    def test_stake_just_after_t1_is_excluded(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, placed_at=T1 + EPS)
        assert _inputs(w, acct)[0] == Decimal(0)

    def test_return_settled_exactly_at_t0_is_excluded(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, placed_at=T0 - EPS, **_settle(settled_at=T0, cash_returned=Decimal("20.00")))
        assert _inputs(w, acct)[1] == Decimal(0)

    def test_return_settled_exactly_at_t1_is_included(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, **_settle(settled_at=T1, cash_returned=Decimal("20.00")))
        assert _inputs(w, acct)[1] == Decimal("20.00")

    def test_placed_before_t0_settled_inside_counts_return_only(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, placed_at=T0 - timedelta(days=1), **_settle(cash_returned=Decimal("20.00")))
        assert _inputs(w, acct) == (Decimal(0), Decimal("20.00"))

    def test_placed_inside_still_open_counts_stake_only(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct)
        assert _inputs(w, acct) == (Decimal("10.00"), Decimal(0))

    def test_placed_inside_settled_after_t1_counts_stake_only(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, **_settle(settled_at=T1 + EPS, cash_returned=Decimal("20.00")))
        assert _inputs(w, acct) == (Decimal("10.00"), Decimal(0))

    @pytest.mark.parametrize("result", ["void", "push"])
    def test_void_and_push_cancel_exactly(self, wh: Any, result: str) -> None:
        w, acct = wh
        _bet(w, acct, **_settle(result=result, cash_returned=Decimal("10.00")))
        assert _inputs(w, acct) == (Decimal("10.00"), Decimal("10.00"))

    def test_lost_bet_is_a_pure_outflow(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, **_settle(result="lost"))
        assert _inputs(w, acct) == (Decimal("10.00"), Decimal(0))

    def test_bonus_bet_win_counts_only_cash_returned(self, wh: Any) -> None:
        w, acct = wh
        _bet(
            w,
            acct,
            cash_staked=Decimal("0.00"),
            bonus_staked=Decimal("10.00"),
            **_settle(cash_returned=Decimal("15.00")),
        )
        assert _inputs(w, acct) == (Decimal(0), Decimal("15.00"))

    def test_superseded_versions_are_ignored(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct, is_current=False, cash_staked=Decimal("99.00"))
        _bet(
            w,
            acct,
            is_current=False,
            **_settle(cash_returned=Decimal("77.00")),
        )
        current = _bet(w, acct, version=2, cash_staked=Decimal("4.00"))
        assert current.is_current
        assert _inputs(w, acct) == (Decimal("4.00"), Decimal(0))

    def test_other_accounts_bets_are_excluded(self, wh: Any) -> None:
        w, acct = wh
        other = _account(w, "draftkings")
        _bet(w, other)
        assert _inputs(w, acct) == (Decimal(0), Decimal(0))

    def test_multiple_bets_sum(self, wh: Any) -> None:
        w, acct = wh
        _bet(w, acct)
        _bet(w, acct, cash_staked=Decimal("2.50"))
        assert _inputs(w, acct)[0] == Decimal("12.50")

    def test_another_tenants_bets_never_leak_even_with_the_same_account_id(
        self, two_users: Any
    ) -> None:
        _, alice, bob = two_users
        bob_acct = _account(bob)
        _bet(bob, bob_acct, **_settle(cash_returned=Decimal("20.00")))
        assert alice.bets.reconciliation_inputs(bob_acct.id, T0, T1) == (Decimal(0), Decimal(0))


class TestLedgerIsolation:
    def test_for_account_never_returns_another_tenants_rows(self, two_users: Any) -> None:
        _, alice, bob = two_users
        a_acct, b_acct = _account(alice), _account(bob)
        for w, acct in ((alice, a_acct), (bob, b_acct)):
            w.balances.add(
                BalanceSnapshot(
                    tenant_id=w.scope.tenant_id,
                    user_id=w.scope.user_id,
                    id=uuid.uuid4(),
                    sportsbook_account_id=acct.id,
                    observed_at=T0,
                    cash_balance=Decimal("10.00"),
                )
            )
            w.cash_flows.add(
                CashFlow(
                    tenant_id=w.scope.tenant_id,
                    user_id=w.scope.user_id,
                    id=uuid.uuid4(),
                    sportsbook_account_id=acct.id,
                    kind="deposit",
                    occurred_at=T0,
                    amount=Decimal("5.00"),
                )
            )
        assert alice.balances.for_account(b_acct.id) == []
        assert alice.cash_flows.for_account(b_acct.id) == []
        assert len(bob.balances.for_account(b_acct.id)) == 1
        assert len(bob.cash_flows.for_account(b_acct.id)) == 1

    def test_snapshots_with_equal_observed_at_order_by_created_at_then_id(self, wh: Any) -> None:
        """Inserted out of order on purpose, so only the ORDER BY can produce the order."""
        w, acct = wh
        low, high = sorted((uuid.uuid4(), uuid.uuid4()))
        for amount, created, ident in (
            ("3.00", T0 + 3 * EPS, uuid.uuid4()),
            ("2.00", T0 + 2 * EPS, high),
            ("1.00", T0 + 2 * EPS, low),
            ("0.00", T0 + EPS, uuid.uuid4()),
        ):
            w.balances.add(
                BalanceSnapshot(
                    tenant_id=w.scope.tenant_id,
                    user_id=w.scope.user_id,
                    id=ident,
                    sportsbook_account_id=acct.id,
                    observed_at=T0,
                    cash_balance=Decimal(amount),
                    created_at=created,
                )
            )
        assert [s.cash_balance for s in w.balances.for_account(acct.id)] == [
            Decimal("0.00"),
            Decimal("1.00"),
            Decimal("2.00"),
            Decimal("3.00"),
        ]


# ------------------------------------------------------------ schema CHECKs


def _raw_snapshot(conn: duckdb.DuckDBPyConnection, w: Warehouse, acct: Any, **over: Any) -> None:
    row: dict[str, Any] = {
        "tenant_id": str(w.scope.tenant_id),
        "user_id": str(w.scope.user_id),
        "sportsbook_account_id": str(acct.id),
        "observed_at": T0,
        "cash_balance": Decimal("1.00"),
    } | over
    conn.execute(
        f"INSERT INTO core.balance_snapshot ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        list(row.values()),
    )


def _raw_flow(conn: duckdb.DuckDBPyConnection, w: Warehouse, acct: Any, **over: Any) -> None:
    row: dict[str, Any] = {
        "tenant_id": str(w.scope.tenant_id),
        "user_id": str(w.scope.user_id),
        "sportsbook_account_id": str(acct.id),
        "kind": "deposit",
        "occurred_at": T0,
        "amount": Decimal("1.00"),
    } | over
    conn.execute(
        f"INSERT INTO core.cash_flow ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        list(row.values()),
    )


class TestSchemaChecks:
    def test_valid_rows_insert(self, two_users: Any) -> None:
        conn, alice, _ = two_users
        acct = _account(alice)
        _raw_snapshot(conn, alice, acct, cash_balance=Decimal("0.00"))
        _raw_flow(conn, alice, acct)
        assert alice.balances.count() == 1
        assert alice.cash_flows.count() == 1

    @pytest.mark.parametrize("kind", ["transfer", "Deposit", "", "bonus"])
    def test_cash_flow_kind_vocabulary(self, two_users: Any, kind: str) -> None:
        conn, alice, _ = two_users
        with pytest.raises(duckdb.ConstraintException):
            _raw_flow(conn, alice, _account(alice), kind=kind)

    @pytest.mark.parametrize("amount", [Decimal("0.00"), Decimal("-1.00")])
    def test_cash_flow_amount_must_be_positive(self, two_users: Any, amount: Decimal) -> None:
        conn, alice, _ = two_users
        with pytest.raises(duckdb.ConstraintException):
            _raw_flow(conn, alice, _account(alice), amount=amount)

    def test_cash_flow_smallest_amount_is_allowed(self, two_users: Any) -> None:
        conn, alice, _ = two_users
        _raw_flow(conn, alice, _account(alice), amount=Decimal("0.01"))
        assert alice.cash_flows.count() == 1

    def test_cash_balance_cannot_be_negative(self, two_users: Any) -> None:
        conn, alice, _ = two_users
        with pytest.raises(duckdb.ConstraintException):
            _raw_snapshot(conn, alice, _account(alice), cash_balance=Decimal("-0.01"))

    def test_bonus_balance_cannot_be_negative(self, two_users: Any) -> None:
        conn, alice, _ = two_users
        with pytest.raises(duckdb.ConstraintException):
            _raw_snapshot(conn, alice, _account(alice), bonus_balance=Decimal("-0.01"))

    @pytest.mark.parametrize("method", ["telepathy", "SCREENSHOT", ""])
    def test_capture_method_vocabulary(self, two_users: Any, method: str) -> None:
        conn, alice, _ = two_users
        acct = _account(alice)
        with pytest.raises(duckdb.ConstraintException):
            _raw_snapshot(conn, alice, acct, capture_method=method)
        with pytest.raises(duckdb.ConstraintException):
            _raw_flow(conn, alice, acct, capture_method=method)

    @pytest.mark.parametrize(
        "method", ["export", "api", "statement", "manual", "pdf", "screenshot"]
    )
    def test_every_known_capture_method_is_accepted(self, two_users: Any, method: str) -> None:
        conn, alice, _ = two_users
        acct = _account(alice)
        _raw_snapshot(conn, alice, acct, capture_method=method)
        _raw_flow(conn, alice, acct, capture_method=method)

    def test_snapshot_requires_an_owned_account(self, two_users: Any) -> None:
        conn, alice, bob = two_users
        bobs = _account(bob)
        with pytest.raises(duckdb.ConstraintException):
            _raw_snapshot(conn, alice, bobs)
        with pytest.raises(duckdb.ConstraintException):
            _raw_flow(conn, alice, bobs)


# ---------------------------------------------------- reconcile, pure edges


def _snap(balance: str, at: datetime) -> BalanceSnapshot:
    return BalanceSnapshot(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        observed_at=at,
        cash_balance=Decimal(balance),
    )


def _flow(kind: Any, amount: str, at: datetime) -> CashFlow:
    return CashFlow(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        kind=kind,
        occurred_at=at,
        amount=Decimal(amount),
    )


class TestReconcileBoundaries:
    def test_flow_exactly_at_t1_is_included(self) -> None:
        rows = reconcile(
            "a",
            [_snap("100.00", T0), _snap("130.00", T1)],
            [_flow("deposit", "30.00", T1)],
            lambda a, b: BetActivity(),
        )
        assert rows[-1]["deposits"] == Decimal("30.00")
        assert rows[-1]["status"] == "ok"

    def test_flow_just_after_t1_is_excluded_and_surfaces_as_gap_only_if_balance_moved(
        self,
    ) -> None:
        rows = reconcile(
            "a",
            [_snap("100.00", T0), _snap("130.00", T1)],
            [_flow("deposit", "30.00", T1 + EPS)],
            lambda a, b: BetActivity(),
        )
        assert rows[-1]["gap"] == Decimal("30.00")
        assert rows[-1]["status"] == "GAP"

    def test_flow_just_after_t0_is_included(self) -> None:
        rows = reconcile(
            "a",
            [_snap("100.00", T0), _snap("90.00", T1)],
            [_flow("withdrawal", "10.00", T0 + EPS)],
            lambda a, b: BetActivity(),
        )
        assert rows[-1]["withdrawals"] == Decimal("10.00")
        assert rows[-1]["status"] == "ok"

    def test_activity_receives_the_exact_interval(self) -> None:
        seen: list[tuple[datetime, datetime]] = []

        def activity(a: datetime, b: datetime) -> BetActivity:
            seen.append((a, b))
            return BetActivity()

        reconcile("a", [_snap("1.00", T0), _snap("1.00", T1)], [], activity)
        assert seen == [(T0, T1)]

    def test_net_deposited_is_lifetime_as_of_each_row(self) -> None:
        mid = T0 + timedelta(days=2)
        rows = reconcile(
            "a",
            [_snap("10.00", T0), _snap("10.00", mid), _snap("10.00", T1)],
            [_flow("deposit", "50.00", T0), _flow("withdrawal", "5.00", T1)],
            lambda a, b: BetActivity(),
        )
        assert [r["net_deposited"] for r in rows] == [
            Decimal("50.00"),
            Decimal("50.00"),
            Decimal("45.00"),
        ]

    def test_no_snapshots_gives_no_rows(self) -> None:
        assert reconcile("a", [], [], lambda a, b: BetActivity()) == []


# ------------------------------------------------------------------- the CLI


def _run(*args: str, fmt: str | None = None) -> Any:
    argv = ["--format", fmt, *args] if fmt else list(args)
    return runner.invoke(app, argv)


def _ok(*args: str) -> None:
    result = _run(*args)
    assert result.exit_code == 0, result.output


def _json(*args: str) -> list[dict[str, Any]]:
    result = _run(*args, fmt="json")
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    return rows


def _seed_account(data: Path, code: str, label: str) -> None:
    settings = Settings(data_dir=data)
    with connect(settings) as conn:
        scope = identity.resolve_scope(conn, type("_Config", (), {"settings": settings})())
        w = Warehouse(conn, scope)
        w.accounts.add(
            SportsbookAccount(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                id=uuid.uuid4(),
                sportsbook_code=code,
                label=label,
            )
        )


def _two_books() -> None:
    _ok("init")
    _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "100", "--at", "2026-10-01")
    _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "100", "--at", "2026-10-03")
    _ok("balance", "add", "--sportsbook", "draftkings", "--amount", "40", "--at", "2026-10-01")
    _ok("balance", "add", "--sportsbook", "draftkings", "--amount", "55", "--at", "2026-10-03")


class TestBalanceCli:
    def test_two_accounts_get_per_account_rows_and_a_total(self) -> None:
        _two_books()
        rows = _json("balance")
        assert [r["account"] for r in rows] == ["draftkings", "fanduel", "TOTAL"]
        dk, fd, tot = rows
        assert (dk["status"], dk["gap"]) == ("GAP", "15.00")
        assert (fd["status"], fd["gap"]) == ("ok", "0.00")
        assert tot["balance"] == "155.00"
        assert tot["cumulative_gap"] == "15.00"
        assert tot["status"] == "GAP"

    def test_history_shows_every_interval_per_account_then_one_total(self) -> None:
        _two_books()
        rows = _json("balance", "--history")
        assert [(r["account"], r["status"]) for r in rows] == [
            ("draftkings", "baseline"),
            ("draftkings", "GAP"),
            ("fanduel", "baseline"),
            ("fanduel", "ok"),
            ("TOTAL", "GAP"),
        ]

    def test_total_is_ok_when_no_account_has_a_gap(self) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "10", "--at", "2026-10-01")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "10", "--at", "2026-10-02")
        assert _json("balance")[-1]["status"] == "ok"

    def test_no_snapshots_means_no_total_row(self) -> None:
        _ok("init")
        assert _json("balance") == []

    def test_json_renders_decimals_as_strings(self) -> None:
        _two_books()
        for row in _json("balance"):
            for key in ("balance", "net_deposited", "deposits", "withdrawals"):
                assert isinstance(row[key], str)
            assert row["gap"] is None or isinstance(row["gap"], str)

    def test_csv_renders_decimals_as_plain_text(self) -> None:
        _two_books()
        result = _run("balance", fmt="csv")
        assert result.exit_code == 0, result.output
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        assert rows[0]["balance"] == "55.00"
        assert rows[0]["gap"] == "15.00"
        assert rows[-1]["account"] == "TOTAL"

    def test_account_label_filters(self, tmp_path: Path) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--account-label", "main",
            "--amount", "10", "--at", "2026-10-01")  # fmt: skip
        # balance add creates only a sportsbook's first account; seed the second.
        _seed_account(tmp_path / "data", "fanduel", "boosts")
        _ok("balance", "add", "--sportsbook", "fanduel", "--account-label", "boosts",
            "--amount", "20", "--at", "2026-10-01")  # fmt: skip
        rows = _json("balance", "--account-label", "boosts")
        assert [r["account"] for r in rows] == ["fanduel:boosts", "TOTAL"]

    def test_balance_add_json_has_string_amounts(self) -> None:
        _ok("init")
        [row] = _json("balance", "add", "--sportsbook", "fanduel", "--amount", "12.5",
                      "--bonus-amount", "3", "--at", "2026-10-01")  # fmt: skip
        assert row["cash_balance"] == "12.50"
        assert row["bonus_balance"] == "3.00"
        assert row["capture_method"] == "manual"

    @pytest.mark.parametrize("amount", ["-1", "abc"])
    def test_balance_add_rejects_bad_amounts(self, amount: str) -> None:
        _ok("init")
        result = _run("balance", "add", "--sportsbook", "fanduel", "--amount", amount)
        assert isinstance(result.exception, UsageError)

    def test_balance_add_accepts_zero(self) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "0")

    def test_a_manual_deposit_cannot_cite_a_source_file(self, tmp_path: Path) -> None:
        slip = tmp_path / "IMG_9.PNG"
        slip.write_bytes(ONE_PIXEL_PNG)
        _ok("init")
        result = _run("deposit", "--sportsbook", "fanduel", "--amount", "5",
                      "--source-file", str(slip))  # fmt: skip
        assert isinstance(result.exception, UsageError)

    def test_the_same_screenshot_is_archived_once_across_two_rows(self, tmp_path: Path) -> None:
        slip = tmp_path / "IMG_7.PNG"
        slip.write_bytes(ONE_PIXEL_PNG)
        _ok("init")
        common = ("--sportsbook", "fanduel", "--capture-method", "screenshot",
                  "--source-file", str(slip))  # fmt: skip
        _ok("balance", "add", "--amount", "10", *common)
        _ok("deposit", "--amount", "5", *common)
        with connect(Settings(data_dir=tmp_path / "data")) as conn:
            sources = conn.execute("SELECT count(*) FROM control.source_file").fetchone()
            ids = conn.execute(
                "SELECT source_file_id FROM core.balance_snapshot "
                "UNION SELECT source_file_id FROM core.cash_flow"
            ).fetchall()
        assert sources is not None and sources[0] == 1
        assert len(ids) == 1 and ids[0][0] is not None


class TestCashFlowCli:
    @pytest.mark.parametrize("command", ["deposit", "withdraw"])
    @pytest.mark.parametrize("amount", ["0", "0.00", "0.004", "-5"])
    def test_zero_and_negative_amounts_are_refused_and_nothing_is_written(
        self, tmp_path: Path, command: str, amount: str
    ) -> None:
        _ok("init")
        result = _run(command, "--sportsbook", "fanduel", "--amount", amount)
        assert isinstance(result.exception, UsageError)
        with connect(Settings(data_dir=tmp_path / "data")) as conn:
            flows = conn.execute("SELECT count(*) FROM core.cash_flow").fetchone()
            accounts = conn.execute("SELECT count(*) FROM core.sportsbook_account").fetchone()
        assert flows is not None and flows[0] == 0
        assert accounts is not None and accounts[0] == 0

    @pytest.mark.parametrize(
        ("command", "kind"), [("deposit", "deposit"), ("withdraw", "withdrawal")]
    )
    def test_smallest_amount_is_accepted(self, command: str, kind: str) -> None:
        _ok("init")
        [row] = _json(command, "--sportsbook", "fanduel", "--amount", "0.01")
        assert row["kind"] == kind
        assert row["amount"] == "0.01"

    def test_withdrawal_reduces_expected_balance(self) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "50", "--at", "2026-10-01")
        _ok("withdraw", "--sportsbook", "fanduel", "--amount", "20", "--at", "2026-10-02")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "30", "--at", "2026-10-03")
        assert _json("balance")[0]["gap"] == "0.00"

    def test_a_flow_stamped_exactly_at_a_snapshot_belongs_to_the_earlier_interval(self) -> None:
        _ok("init")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "50", "--at", "2026-10-01")
        _ok("deposit", "--sportsbook", "fanduel", "--amount", "20", "--at", "2026-10-02")
        _ok("balance", "add", "--sportsbook", "fanduel", "--amount", "70", "--at", "2026-10-02")
        assert _json("balance")[0]["gap"] == "0.00"
