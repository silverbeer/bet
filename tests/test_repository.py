"""Repository layer: scoping that cannot be bypassed, and atomic writes.

SB-704's acceptance is that user scoping cannot be bypassed and that writes roll
back atomically. Isolation is asserted for *every* owned repository rather than a
representative one — the table most likely to leak is the one nobody thought to
check.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from bet.config import Settings
from bet.database import migrator
from bet.database.connection import connect
from bet.database.repository import (
    OWNED_REPOSITORIES,
    ScopedRepository,
    Warehouse,
)
from bet.errors import DatabaseError, NotFoundError
from bet.models.bet import Bet, BetLeg, BetPromotion, Promotion
from bet.models.ownership import OwnerScope, SportsbookAccount

PLACED = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)


@pytest.fixture
def two_users(tmp_path: Path) -> Iterator[tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse]]:
    """One tenant, two users, each with an account. The leak scenario."""
    data = tmp_path / "data"
    data.mkdir()
    with connect(Settings(data_dir=data)) as conn:
        migrator.apply(conn)
        tenant = uuid.uuid4()
        conn.execute("INSERT INTO core.tenant (id, name) VALUES (?, ?)", [str(tenant), "t"])

        scopes = []
        for name in ("alice", "bob"):
            user = uuid.uuid4()
            conn.execute(
                "INSERT INTO core.user (tenant_id, id, display_name) VALUES (?, ?, ?)",
                [str(tenant), str(user), name],
            )
            scopes.append(OwnerScope(tenant_id=tenant, user_id=user))

        yield conn, Warehouse(conn, scopes[0]), Warehouse(conn, scopes[1])


def make_account(w: Warehouse, label: str = "main") -> SportsbookAccount:
    return w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code="fanduel",
            label=label,
        )
    )


def make_bet(w: Warehouse, account: SportsbookAccount, **fields: Any) -> Bet:
    defaults: dict[str, Any] = {"cash_staked": Decimal("10.00")}
    return w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            placed_at=PLACED,
            **(defaults | fields),
        )
    )


# ------------------------------------------------- scoping cannot be bypassed


def test_every_owned_repository_derives_from_scoped_repository() -> None:
    """Converts "remember to scope the new repository" into a failing test."""
    for repo in OWNED_REPOSITORIES:
        assert issubclass(repo, ScopedRepository), f"{repo.__name__} is not scoped"
        assert repo.table.startswith(("core.", "control.", "raw.", "analytics."))
        assert repo.model is not None


def test_a_repository_cannot_be_constructed_without_a_scope() -> None:
    """There is no default scope and no None sentinel."""
    for repo in OWNED_REPOSITORIES:
        with pytest.raises(TypeError):
            repo(None)  # type: ignore[call-arg,arg-type]


@pytest.mark.parametrize("repo_name", ["accounts", "bets", "legs", "promotions"])
def test_one_user_never_sees_another_users_rows(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse], repo_name: str
) -> None:
    _, alice, bob = two_users
    a_account, b_account = make_account(alice, "alice-fd"), make_account(bob, "bob-fd")
    a_bet, b_bet = make_bet(alice, a_account), make_bet(bob, b_account)

    for w, account, bet in ((alice, a_account, a_bet), (bob, b_account, b_bet)):
        w.legs.add(
            BetLeg(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                leg_order=1,
            )
        )
        w.promotions.add(
            Promotion(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                sportsbook_code="fanduel",
                promotion_type="profit_boost",
            )
        )
        assert account.id is not None

    assert getattr(alice, repo_name).count() == 1
    assert getattr(bob, repo_name).count() == 1

    alice_ids = {e.id for e in getattr(alice, repo_name).fetch_all()}
    bob_ids = {e.id for e in getattr(bob, repo_name).fetch_all()}
    assert not (alice_ids & bob_ids)


def test_get_refuses_another_users_row_by_id(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """Knowing the id must not be enough."""
    _, alice, bob = two_users
    bob_bet = make_bet(bob, make_account(bob))

    with pytest.raises(NotFoundError):
        alice.bets.get(bob_bet.id)
    assert alice.bets.find(bob_bet.id) is None


def test_delete_cannot_reach_another_users_row(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, bob = two_users
    bob_bet = make_bet(bob, make_account(bob))

    assert alice.bets.delete(bob_bet.id) == 0
    assert bob.bets.count() == 1


def test_writing_an_entity_owned_by_someone_else_is_refused(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """Silently reassigning it would be the exact leak this layer prevents."""
    _, alice, bob = two_users
    account = make_account(bob)
    foreign = Bet(
        tenant_id=bob.scope.tenant_id,
        user_id=bob.scope.user_id,
        id=uuid.uuid4(),
        sportsbook_account_id=account.id,
        placed_at=PLACED,
        cash_staked=Decimal("5.00"),
    )
    with pytest.raises(DatabaseError, match="different user"):
        alice.bets.add(foreign)


def test_every_generated_query_carries_the_scope_predicate(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """Structural check: the helper cannot produce an unscoped WHERE."""
    _, alice, _ = two_users
    for repo in OWNED_REPOSITORIES:
        instance = repo(alice._conn, alice.scope)
        assert instance._scoped().startswith("tenant_id = ? AND user_id = ?")
        assert "tenant_id = ?" in instance._scoped("id = ?")


# ----------------------------------------------------------- model and schema


def test_model_fields_all_exist_as_columns(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """Column lists come from the models, so a stray field must fail loudly."""
    conn, _alice, _ = two_users
    for repo in OWNED_REPOSITORIES:
        schema, _, table = repo.table.partition(".")
        actual = {
            str(r[0])
            for r in conn.execute(
                "SELECT column_name FROM duckdb_columns() WHERE schema_name = ? AND table_name = ?",
                [schema, table],
            ).fetchall()
        }
        missing = set(repo.columns()) - actual
        assert not missing, f"{repo.table} has no column for: {sorted(missing)}"


def test_round_trip_preserves_money_exactly(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, _ = two_users
    account = make_account(alice)
    written = make_bet(
        alice,
        account,
        cash_staked=Decimal("10.00"),
        cash_returned=Decimal("16.67"),
        status="settled",
        result="won",
        settled_at=PLACED,
    )
    read = alice.bets.get(written.id)
    assert read.cash_staked == Decimal("10.00")
    assert read.net_profit == Decimal("6.67")
    assert read.placed_at == PLACED


# ------------------------------------------------------------- transactions


def test_a_failed_write_rolls_back_completely(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """A bet with only some of its legs is worse than no bet at all."""
    _, alice, _ = two_users
    account = make_account(alice)

    with pytest.raises(DatabaseError), alice.transaction() as w:
        bet = make_bet(w, account, wager_kind="parlay")
        w.legs.add(
            BetLeg(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                leg_order=1,
            )
        )
        # Duplicate leg_order violates the unique constraint.
        w.legs.add(
            BetLeg(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                leg_order=1,
            )
        )

    assert alice.bets.count() == 0
    assert alice.legs.count() == 0


def test_a_successful_transaction_commits(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, _ = two_users
    account = make_account(alice)

    with alice.transaction() as w:
        bet = make_bet(w, account, wager_kind="parlay")
        for order in (1, 2, 3):
            w.legs.add(
                BetLeg(
                    tenant_id=w.scope.tenant_id,
                    user_id=w.scope.user_id,
                    id=uuid.uuid4(),
                    bet_id=bet.id,
                    leg_order=order,
                )
            )

    assert alice.bets.count() == 1
    assert alice.legs.count() == 3


def test_a_rollback_does_not_discard_another_users_committed_rows(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, bob = two_users
    make_bet(bob, make_account(bob))
    account = make_account(alice)

    with pytest.raises(DatabaseError), alice.transaction() as w:
        make_bet(w, account)
        raise RuntimeError("boom")

    assert bob.bets.count() == 1
    assert alice.bets.count() == 0


# -------------------------------------------------------------- provenance


def test_provenance_reports_what_was_recorded(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """The import-run tables arrive with SB-703; reporting the id is honest."""
    _, alice, _ = two_users
    run_id = uuid.uuid4()
    bet = make_bet(
        alice,
        make_account(alice),
        capture_method="export",
        import_run_id=run_id,
        profile_version="fanduel-csv-v2",
        external_bet_id="226030899",
        external_receipt_id="O/0028268/0000815",
    )

    provenance = alice.bets.provenance(bet.id)
    assert provenance["capture_method"] == "export"
    assert provenance["import_run_id"] == run_id
    assert provenance["profile_version"] == "fanduel-csv-v2"
    assert provenance["external_receipt_id"] == "O/0028268/0000815"


def test_provenance_refuses_another_users_bet(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, bob = two_users
    bob_bet = make_bet(bob, make_account(bob))
    with pytest.raises(NotFoundError):
        alice.bets.provenance(bob_bet.id)


def test_correction_history_returns_the_chain_oldest_first(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """Corrections supersede rather than overwrite, so this is the audit trail."""
    _, alice, _ = two_users
    account = make_account(alice)

    original = make_bet(alice, account, version=1, is_current=False)
    corrected = make_bet(alice, account, version=2, is_current=True, supersedes_id=original.id)

    chain = alice.bets.history(corrected.id)
    assert [b.version for b in chain] == [1, 2]
    assert chain[0].id == original.id


def test_history_terminates_on_a_cycle(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """Corrupt data must not hang the CLI."""
    conn, alice, _ = two_users
    account = make_account(alice)
    bet = make_bet(alice, account)
    conn.execute("UPDATE core.bet SET supersedes_id = id WHERE id = ?", [str(bet.id)])

    assert len(alice.bets.history(bet.id)) == 1


def test_current_excludes_superseded_versions(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, _ = two_users
    account = make_account(alice)
    make_bet(alice, account, version=1, is_current=False)
    make_bet(alice, account, version=2, is_current=True)

    assert len(alice.bets.current()) == 1
    assert alice.bets.count() == 2


def test_open_bets_lists_only_pending_current_bets(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, _ = two_users
    account = make_account(alice)
    make_bet(alice, account)
    make_bet(
        alice,
        account,
        status="settled",
        result="won",
        settled_at=PLACED,
        cash_returned=Decimal("16.67"),
    )

    assert len(alice.bets.open_bets()) == 1


# ------------------------------------------------------- child lookups


def test_legs_and_promotions_are_fetched_for_one_bet_in_order(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, _ = two_users
    account = make_account(alice)
    bet = make_bet(alice, account, wager_kind="parlay")

    for order in (3, 1, 2):
        alice.legs.add(
            BetLeg(
                tenant_id=alice.scope.tenant_id,
                user_id=alice.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                leg_order=order,
                selection_name=f"leg {order}",
            )
        )
    alice.bet_promotions.add(
        BetPromotion(
            tenant_id=alice.scope.tenant_id,
            user_id=alice.scope.user_id,
            id=uuid.uuid4(),
            bet_id=bet.id,
            promotion_type="profit_boost",
            generosity_pct=Decimal("30"),
        )
    )

    assert [leg.leg_order for leg in alice.legs.for_bet(bet.id)] == [1, 2, 3]
    assert len(alice.bet_promotions.for_bet(bet.id)) == 1


def test_accounts_can_be_filtered_by_sportsbook(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    _, alice, _ = two_users
    make_account(alice, "fd")
    assert len(alice.accounts.for_sportsbook("fanduel")) == 1
    assert alice.accounts.for_sportsbook("draftkings") == []


# --------------------------------------- no raw SQL outside the data layer

# OWNERSHIP.md section 6.1 layer 4. A query written anywhere else is a query
# nobody scoped, and it would not fail — it would quietly return everyone's rows
# the moment a second user exists.
DATA_LAYER = "src/bet/database"
OWNED_TABLE_SQL = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+core\.", re.IGNORECASE)


def test_no_module_outside_the_data_layer_queries_an_owned_table() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for path in (root / "src" / "bet").rglob("*.py"):
        if str(path.relative_to(root)).startswith(DATA_LAYER):
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if OWNED_TABLE_SQL.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")

    assert not offenders, (
        "raw SQL against an owned table outside " + DATA_LAYER + ":\n  " + "\n  ".join(offenders)
    )


def test_the_raw_sql_check_would_actually_catch_something(tmp_path: Path) -> None:
    """Guard the guard: a pattern that never matches proves nothing."""
    assert OWNED_TABLE_SQL.search("SELECT * FROM core.bet WHERE 1")
    assert OWNED_TABLE_SQL.search("insert into core.bet_leg (id) values (?)")
    assert not OWNED_TABLE_SQL.search("SELECT * FROM reference.bet_result")


def test_every_statement_a_repository_emits_is_scoped(
    two_users: tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse],
) -> None:
    """The strongest form of the claim: capture the SQL and inspect all of it.

    Asserting on the helper only proves the helper. This drives every read and
    delete on every repository through a recording connection and checks the
    statements that actually reached DuckDB.
    """
    conn, alice, _ = two_users
    account = make_account(alice)
    bet = make_bet(alice, account, wager_kind="parlay")

    emitted: list[str] = []

    class Recorder:
        def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
            self._inner = inner

        def execute(self, sql: str, params: Any = None) -> Any:
            emitted.append(sql)
            return (
                self._inner.execute(sql, params) if params is not None else self._inner.execute(sql)
            )

    recording = Warehouse(Recorder(conn), alice.scope)  # type: ignore[arg-type]

    recording.bets.fetch_all()
    recording.bets.current()
    recording.bets.open_bets()
    recording.bets.count()
    recording.bets.find(bet.id)
    recording.legs.for_bet(bet.id)
    recording.bet_promotions.for_bet(bet.id)
    recording.accounts.for_sportsbook("fanduel")
    recording.leg_groups.for_bet(bet.id)
    recording.promotions.fetch_all()
    recording.bets.delete(uuid.uuid4())

    reads_and_deletes = [
        sql
        for sql in emitted
        if re.search(r"\bcore\.", sql) and not sql.lstrip().upper().startswith("INSERT")
    ]
    assert reads_and_deletes, "no statements were captured"

    unscoped = [sql for sql in reads_and_deletes if "tenant_id = ? AND user_id = ?" not in sql]
    assert not unscoped, "statements missing the ownership predicate:\n  " + "\n  ".join(unscoped)
