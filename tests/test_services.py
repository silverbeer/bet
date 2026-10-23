"""Read-path services (SB-1394): called directly, against a temp warehouse.

The CLI tests cover what each command renders; these cover what each service
returns, with no Typer in the way, as a future HTTP layer would call them.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import ast
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest
from pydantic import ValidationError

import bet.services
from bet.config import Settings
from bet.database import migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import NotFoundError, UsageError
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet, BetLeg, BetPromotion
from bet.models.ownership import OwnerScope, SportsbookAccount
from bet.services._query import placed_bounds
from bet.services.balance import (
    BalanceQuery,
    account_name,
    balance_report,
    require_known_sportsbook,
)
from bet.services.bets import ListQuery, bet_detail, list_bets
from bet.services.card import CardQuery, card_summary
from bet.services.similar import SimilarQuery, similar_bets

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 20)
PLACED = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)


# ------------------------------------------------------------- fixtures


@pytest.fixture
def two_users(tmp_path: Path) -> Iterator[tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse]]:
    with connect(Settings(data_dir=tmp_path)) as conn:
        migrator.apply(conn)
        scopes = []
        for name in ("alice", "bob"):
            tenant, user = uuid.uuid4(), uuid.uuid4()
            conn.execute("INSERT INTO core.tenant (id, name) VALUES (?, ?)", [str(tenant), name])
            conn.execute(
                "INSERT INTO core.user (tenant_id, id, display_name) VALUES (?, ?, ?)",
                [str(tenant), str(user), name],
            )
            scopes.append(OwnerScope(tenant_id=tenant, user_id=user))
        yield conn, Warehouse(conn, scopes[0]), Warehouse(conn, scopes[1])


@pytest.fixture
def w(two_users: Any) -> Warehouse:
    alice: Warehouse = two_users[1]
    return alice


def _account(w: Warehouse, code: str = "fanduel", label: str | None = None) -> SportsbookAccount:
    return w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code=code,
            label=label or code,
        )
    )


def _bet(
    w: Warehouse,
    account: SportsbookAccount,
    *,
    sport: str = "NFL",
    selection: str = "Chiefs ML",
    **fields: Any,
) -> Bet:
    defaults: dict[str, Any] = {"placed_at": PLACED, "cash_staked": Decimal("10.00")}
    bet = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            **(defaults | fields),
        )
    )
    w.legs.add(
        BetLeg(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=bet.id,
            leg_order=1,
            sport=sport,
            selection_name=selection,
            odds_american=150,
        )
    )
    return bet


def _won(at: datetime = PLACED, **fields: Any) -> dict[str, Any]:
    return {
        "status": "settled",
        "result": "won",
        "settled_at": at,
        "cash_returned": Decimal("25.00"),
    } | fields


def _snapshot(w: Warehouse, account: SportsbookAccount, at: datetime, amount: str) -> None:
    w.balances.add(
        BalanceSnapshot(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            observed_at=at,
            cash_balance=Decimal(amount),
        )
    )


# ------------------------------------------------------------- boundary


FORBIDDEN = ("typer", "rich", "click", "bet.cli")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _forbidden(name: str) -> bool:
    return any(name == f or name.startswith(f"{f}.") for f in FORBIDDEN)


def test_services_import_no_cli_framework() -> None:
    package = Path(bet.services.__file__).parent
    modules = sorted(package.rglob("*.py"))
    assert len(modules) > 1
    offending = {
        str(path.relative_to(package)): sorted(n for n in _imports(path) if _forbidden(n))
        for path in modules
    }
    assert {k: v for k, v in offending.items() if v} == {}


def test_services_pull_in_no_cli_framework_transitively() -> None:
    modules = sorted(p.stem for p in Path(bet.services.__file__).parent.glob("*.py"))
    code = (
        "import importlib, sys\n"
        f"for m in {modules!r}: importlib.import_module('bet.services.' + m)\n"
        f"print(sorted(n for n in sys.modules if n.split('.')[0] in {FORBIDDEN[:3]!r} "
        "or n.startswith('bet.cli')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"


def test_forbidden_matches_submodules_only_by_dotted_prefix() -> None:
    assert _forbidden("rich.console") and _forbidden("bet.cli.output")
    assert not _forbidden("richer") and not _forbidden("bet.client")


# ---------------------------------------------------------------- bounds


def test_placed_bounds_cover_whole_utc_days() -> None:
    start, end = placed_bounds(date(2026, 10, 1), date(2026, 10, 2))
    assert start == datetime(2026, 10, 1, tzinfo=UTC)
    assert end is not None and end.date() == date(2026, 10, 2)
    assert end + timedelta(microseconds=1) == datetime(2026, 10, 3, tzinfo=UTC)
    assert placed_bounds(None, None) == (None, None)


# ------------------------------------------------------------------ list


class TestListQuery:
    def test_defaults_mean_everything(self) -> None:
        q = ListQuery()
        assert (q.is_open, q.status, q.kind, q.limit) == (False, None, None, None)

    def test_unknown_status_is_a_usage_error(self) -> None:
        with pytest.raises(UsageError, match="not a known status"):
            ListQuery(status="lost")

    def test_unknown_kind_is_a_usage_error(self) -> None:
        with pytest.raises(UsageError, match="not a known wager kind"):
            ListQuery(kind="teaser-ish")

    def test_open_and_settled_conflict(self) -> None:
        with pytest.raises(UsageError, match="cannot both be given"):
            ListQuery(is_open=True, status="settled")

    def test_open_and_pending_agree(self) -> None:
        assert ListQuery(is_open=True, status="pending").is_open

    def test_is_immutable_and_strict(self) -> None:
        with pytest.raises(ValidationError):
            ListQuery(nonsense=1)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            ListQuery().limit = 3


class TestListBets:
    def test_newest_first_with_label_and_legs(self, w: Warehouse) -> None:
        account = _account(w, label="main")
        older = _bet(w, account, placed_at=PLACED - timedelta(days=1))
        newer = _bet(w, account)
        listed = list_bets(w, ListQuery())
        assert [x.bet.id for x in listed] == [newer.id, older.id]
        assert listed[0].account_label == "main"
        assert [leg.selection_name for leg in listed[0].legs] == ["Chiefs ML"]

    def test_filters_reach_the_query(self, w: Warehouse) -> None:
        fd, dk = _account(w, "fanduel"), _account(w, "draftkings")
        pending = _bet(w, fd, sport="MLB", selection="Red Sox")
        settled = _bet(w, dk, **_won())
        future = _bet(w, fd, wager_kind="future", selection="Bills to win")
        early = _bet(w, fd, placed_at=PLACED - timedelta(days=10))

        def ids(**kw: Any) -> set[uuid.UUID]:
            return {x.bet.id for x in list_bets(w, ListQuery(**kw))}

        assert ids(status="settled") == {settled.id}
        assert ids(is_open=True) == {pending.id, future.id, early.id}
        assert ids(sportsbook="draftkings") == {settled.id}
        assert ids(sport="MLB") == {pending.id}
        assert ids(kind="future") == {future.id}
        assert ids(text="bills") == {future.id}
        assert ids(since=PLACED.date()) == {pending.id, settled.id, future.id}
        assert ids(until=PLACED.date() - timedelta(days=1)) == {early.id}
        assert len(list_bets(w, ListQuery(limit=2))) == 2

    def test_another_users_bets_never_list(self, two_users: Any) -> None:
        _, alice, bob = two_users
        _bet(alice, _account(alice))
        assert list_bets(bob, ListQuery()) == []


class TestBetDetail:
    def test_full_record(self, w: Warehouse) -> None:
        bet = _bet(w, _account(w, label="main"), **_won())
        w.bet_promotions.add(
            BetPromotion(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                promotion_type="profit_boost",
            )
        )
        detail = bet_detail(w, bet.id)
        assert detail.bet.id == bet.id
        assert detail.account_label == "main"
        assert len(detail.legs) == 1
        assert [p.promotion_type for p in detail.promotions] == ["profit_boost"]
        assert detail.provenance["bet_id"] == bet.id

    def test_another_users_bet_is_not_found(self, two_users: Any) -> None:
        _, alice, bob = two_users
        bet = _bet(alice, _account(alice))
        with pytest.raises(NotFoundError):
            bet_detail(bob, bet.id)


# ------------------------------------------------------------------ card


class TestCard:
    def test_window_record_pending_and_balances(self, w: Warehouse) -> None:
        account = _account(w, "fanduel", label="boosts")
        _bet(w, account, **_won(datetime(2026, 10, 19, 22, 0, tzinfo=UTC)))
        _bet(w, account, **_won(datetime(2026, 9, 1, 22, 0, tzinfo=UTC)))  # outside
        _bet(w, account)  # pending
        _snapshot(w, account, datetime(2026, 10, 19, 12, 0, tzinfo=UTC), "40.00")

        card = card_summary(w, CardQuery(tz=NY, today=TODAY))
        assert (card.window_end, card.days) == (TODAY, 7)
        assert card.total.label == "1-0"
        assert card.pending_count == 1
        assert [b.account for b in card.balances] == ["fanduel:boosts"]

    def test_sportsbook_is_normalised_and_narrows_balances(self, w: Warehouse) -> None:
        fd, dk = _account(w, "fanduel"), _account(w, "draftkings")
        _bet(w, fd, **_won(datetime(2026, 10, 19, 22, 0, tzinfo=UTC)))
        _bet(w, dk, **_won(datetime(2026, 10, 19, 22, 0, tzinfo=UTC)))
        _snapshot(w, fd, PLACED, "40.00")
        _snapshot(w, dk, PLACED, "40.00")

        query = CardQuery(tz=NY, today=TODAY, sportsbook="  FanDuel ")
        assert query.sportsbook == "fanduel"
        card = card_summary(w, query)
        assert card.total.label == "1-0"
        assert [b.account for b in card.balances] == ["fanduel"]

    def test_today_defaults_to_now_in_the_given_zone(self, w: Warehouse) -> None:
        card = card_summary(w, CardQuery(tz=NY, days=3))
        assert card.window_end == datetime.now(NY).date()
        assert card.days == 3

    @pytest.mark.parametrize("days", [0, 61])
    def test_days_out_of_range(self, days: int) -> None:
        with pytest.raises(ValidationError):
            CardQuery(tz=NY, days=days)

    def test_tz_is_required(self) -> None:
        with pytest.raises(ValidationError):
            CardQuery()  # type: ignore[call-arg]


# --------------------------------------------------------------- balance


def test_account_name_shows_a_label_only_when_it_adds_something(w: Warehouse) -> None:
    assert account_name(_account(w, "fanduel")) == "fanduel"
    assert account_name(_account(w, "fanduel", label="boosts")) == "fanduel:boosts"


class TestBalanceReport:
    def test_no_snapshots_is_empty(self, w: Warehouse) -> None:
        _account(w)
        assert balance_report(w, BalanceQuery()) == []

    def test_latest_per_account_then_total(self, w: Warehouse) -> None:
        fd, dk = _account(w, "fanduel"), _account(w, "draftkings")
        _snapshot(w, fd, PLACED - timedelta(days=2), "100.00")
        _snapshot(w, fd, PLACED, "100.00")
        _snapshot(w, dk, PLACED, "50.00")
        rows = balance_report(w, BalanceQuery())
        assert [r["account"] for r in rows] == ["draftkings", "fanduel", "TOTAL"]
        assert [r["status"] for r in rows] == ["baseline", "ok", "ok"]
        assert rows[-1]["balance"] == Decimal("150.00")

    def test_history_lists_every_snapshot(self, w: Warehouse) -> None:
        fd = _account(w)
        _snapshot(w, fd, PLACED - timedelta(days=2), "100.00")
        _snapshot(w, fd, PLACED, "90.00")
        w.cash_flows.add(
            CashFlow(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                sportsbook_account_id=fd.id,
                kind="withdrawal",
                occurred_at=PLACED - timedelta(days=1),
                amount=Decimal("10.00"),
            )
        )
        rows = balance_report(w, BalanceQuery(history=True))
        assert [r["status"] for r in rows] == ["baseline", "ok", "ok"]
        assert rows[1]["withdrawals"] == Decimal("10.00")

    def test_bet_activity_enters_the_reconciliation(self, w: Warehouse) -> None:
        fd = _account(w)
        _snapshot(w, fd, PLACED - timedelta(days=1), "100.00")
        _bet(w, fd, **_won(PLACED + timedelta(hours=1)))
        _snapshot(w, fd, PLACED + timedelta(days=1), "115.00")
        row = balance_report(w, BalanceQuery())[0]
        assert (row["staked"], row["returned"], row["gap"]) == (
            Decimal("10.00"),
            Decimal("25.00"),
            Decimal("0.00"),
        )

    def test_sportsbook_and_label_filters(self, w: Warehouse) -> None:
        main, boosts = _account(w, "fanduel", "main"), _account(w, "fanduel", "boosts")
        dk = _account(w, "draftkings")
        for account in (main, boosts, dk):
            _snapshot(w, account, PLACED, "10.00")
        by_book = balance_report(w, BalanceQuery(sportsbook=" FANDUEL"))
        assert [r["account"] for r in by_book] == ["fanduel:boosts", "fanduel:main", "TOTAL"]
        by_label = balance_report(w, BalanceQuery(account_label="main"))
        assert [r["account"] for r in by_label] == ["fanduel:main", "TOTAL"]

    def test_blank_sportsbook_means_every_book(self) -> None:
        assert BalanceQuery(sportsbook="  ").sportsbook is None

    def test_another_users_balances_never_report(self, two_users: Any) -> None:
        _, alice, bob = two_users
        _snapshot(alice, _account(alice), PLACED, "10.00")
        assert balance_report(bob, BalanceQuery()) == []


def test_require_known_sportsbook(two_users: Any) -> None:
    conn = two_users[0]
    require_known_sportsbook(conn, None)
    require_known_sportsbook(conn, "fanduel")
    with pytest.raises(UsageError, match="not a known sportsbook") as caught:
        require_known_sportsbook(conn, "nosuchbook")
    assert caught.value.remediation is not None
    assert "fanduel" in caught.value.remediation


# --------------------------------------------------------------- similar


def _proposal_leg(w: Warehouse, **fields: Any) -> BetLeg:
    return BetLeg(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=uuid.uuid4(),
        bet_id=uuid.uuid4(),
        leg_order=1,
        **fields,
    )


def _similar(w: Warehouse, **kw: Any) -> list[dict[str, Any]]:
    query: dict[str, Any] = {
        "legs": [_proposal_leg(w, sport="NFL", selection_name="Chiefs ML")],
        "wager_kind": "straight",
        "odds_american": 150,
        "min_settled": 1,
        "exploratory_below": 2,
    }
    return similar_bets(w, SimilarQuery(**(query | kw)))


def _shape(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return next(r for r in rows if r["dimension"] == "shape")


class TestSimilar:
    def test_compares_against_settled_history_only(self, w: Warehouse) -> None:
        fd = _account(w)
        _bet(w, fd, **_won())
        _bet(w, fd)  # pending: not history
        shape = _shape(_similar(w))
        assert (shape["n"], shape["won"]) == (1, 1)

    def test_history_filters_and_void(self, w: Warehouse) -> None:
        fd, dk = _account(w, "fanduel"), _account(w, "draftkings")
        _bet(w, fd, **_won())
        _bet(w, dk, **_won(placed_at=PLACED - timedelta(days=10)))
        _bet(w, fd, **_won(result="void", cash_returned=Decimal("10.00")))
        assert _shape(_similar(w))["n"] == 2
        assert _shape(_similar(w, include_void=True))["n"] == 3
        assert _shape(_similar(w, sportsbook="draftkings"))["n"] == 1
        assert _shape(_similar(w, since=PLACED.date()))["n"] == 1
        assert _shape(_similar(w, until=PLACED.date() - timedelta(days=1)))["n"] == 1
        assert _shape(_similar(w, sport="MLB"))["n"] == 0

    def test_promotions_are_compared(self, w: Warehouse) -> None:
        bet = _bet(w, _account(w), **_won())
        w.bet_promotions.add(
            BetPromotion(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                promotion_type="profit_boost",
            )
        )
        rows = _similar(w, promo_types=["profit_boost"])
        promo = next(r for r in rows if r["dimension"] == "promotion")
        assert promo["n"] == 1

    def test_a_proposal_needs_a_leg(self, w: Warehouse) -> None:
        with pytest.raises(ValidationError):
            _similar(w, legs=[])
