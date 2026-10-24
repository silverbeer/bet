"""Migration 0010 on a warehouse that already holds a user and their bets (SB-1397).

The real warehouse reached 0009 with one user who owns every bet. 0010 must add
the credential tables and leave every existing row -- and every earlier
migration's recorded checksum -- exactly as it was.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.models.bet import Bet
from bet.models.ownership import SportsbookAccount

EXISTING = (
    "core.tenant",
    "core.user",
    "core.sportsbook_account",
    "core.bet",
    "control.migration",
)


def _snapshot(conn: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[Any, ...]]]:
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in EXISTING}


def _columns(conn: duckdb.DuckDBPyConnection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT schema_name, table_name, column_name, data_type, is_nullable "
        "FROM duckdb_columns() WHERE schema_name IN ('core', 'control') "
        "ORDER BY ALL"
    ).fetchall()


@pytest.fixture
def at_0009(tmp_path: Path) -> Path:
    """Migrations 0001-0009 only: the shape of the warehouse before this ticket."""
    directory = tmp_path / "migrations-0009"
    directory.mkdir()
    for migration in migrator.discover():
        if migration.version <= 9:
            shutil.copy(migration.path, directory / migration.path.name)
    return directory


def test_0010_applies_cleanly_over_existing_data(tmp_path: Path, at_0009: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    with connect(settings) as conn:
        migrator.apply(conn, at_0009)
        assert migrator.current_version(conn) == 9

        scope = identity.bootstrap(conn, display_name="tom")
        w = Warehouse(conn, scope)
        account = w.accounts.add(
            SportsbookAccount(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                id=uuid.uuid4(),
                sportsbook_code="fanduel",
                label="fanduel",
            )
        )
        w.bets.add(
            Bet(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                id=uuid.uuid4(),
                sportsbook_account_id=account.id,
                placed_at=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
                cash_staked=Decimal("5.00"),
            )
        )
        before, columns_before = _snapshot(conn), _columns(conn)

        applied = migrator.apply(conn)

        assert [m.version for m in applied] == [10]
        after = _snapshot(conn)
        # Every pre-existing row is untouched; the only new migration row is 0010.
        assert {t: after[t] for t in EXISTING if t != "control.migration"} == {
            t: before[t] for t in EXISTING if t != "control.migration"
        }
        assert after["control.migration"][:9] == before["control.migration"]
        assert [r[0] for r in after["control.migration"]] == list(range(1, 11))
        # No existing column was added, removed or retyped.
        new_tables = {"user_credential", "refresh_token"}
        assert [c for c in _columns(conn) if c[1] not in new_tables] == columns_before

        assert conn.execute("SELECT count(*) FROM core.user_credential").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM core.refresh_token").fetchone() == (0,)
        assert migrator.pending(conn) == []
        # The existing user is still the local identity, and still owns the bet.
        assert identity.existing_scope(conn) == scope
        assert Warehouse(conn, scope).bets.count() == 1


def test_credential_rows_cannot_claim_a_user_that_does_not_exist(tmp_path: Path) -> None:
    with connect(Settings(data_dir=tmp_path / "data")) as conn:
        migrator.apply(conn)
        scope = identity.bootstrap(conn, display_name="tom")
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                "INSERT INTO core.user_credential (tenant_id, user_id, password_hash) "
                "VALUES (?, ?, '$argon2id$v=19$x')",
                [str(scope.tenant_id), str(uuid.uuid4())],
            )
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                "INSERT INTO core.refresh_token "
                "(tenant_id, user_id, id, token_hash, issued_at, expires_at) "
                "VALUES (?, ?, uuidv7(), repeat('0', 64), now(), now() + INTERVAL 1 DAY)",
                [str(uuid.uuid4()), str(scope.user_id)],
            )
