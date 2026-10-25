"""The warehouse backup service (SB-1404), called directly against a temp warehouse.

A backup must reopen as the same warehouse -- every table and row, the same
schema version, ``bet doctor`` clean -- must contain only what was committed
when it began, and pruning must only ever delete files named exactly as a
backup. Every warehouse, user and password here is synthetic and temporary.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from bet.cli.commands.doctor import Status, run_checks
from bet.config import Settings, resolve
from bet.database import identity, migrator
from bet.database.connection import connect, on_disk_storage_version
from bet.database.repository import Warehouse
from bet.errors import DatabaseError, UsageError
from bet.models.ownership import OwnerScope
from bet.services import backup as backup_service
from bet.services.add import AddBetRequest, add_bet
from bet.services.backup import (
    backup_name,
    backup_time,
    create_backup,
    file_digest,
    latest_backup,
    prune_backups,
)
from bet.services.ledger import (
    BalanceSnapshotRequest,
    CashFlowRequest,
    add_balance_snapshot,
    record_cash_flow,
)
from bet.services.settle import SettleRequest, settle_bet
from bet.services.sources import SourceToArchive
from bet.services.tickets import LegInput
from bet.services.users import create_user, set_password_for

NOW = datetime(2026, 10, 24, 3, 30, 5, tzinfo=UTC)
PLACED = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n synthetic backup slip"


@dataclass(frozen=True)
class Populated:
    settings: Settings
    conn: duckdb.DuckDBPyConnection
    alice: OwnerScope
    backups: Path


def table_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Rows per ``schema.table`` in the connection's own database."""
    tables = conn.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = current_database() ORDER BY 1, 2"
    ).fetchall()
    counts = {}
    for schema, table in tables:
        row = conn.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()
        assert row is not None
        counts[f"{schema}.{table}"] = int(row[0])
    return counts


def _populate(conn: duckdb.DuckDBPyConnection, archive_dir: Path, slip: Path) -> OwnerScope:
    """Users with passwords, bets (one settled, one from a screenshot) and ledger rows."""
    alice = identity.bootstrap(conn, display_name="alice")
    create_user(conn, "bob")
    for name in ("alice", "bob"):
        set_password_for(conn, name, secrets.token_urlsafe(18))

    w = Warehouse(conn, alice)
    legs = [LegInput(sport="NFL", selection_name="Synthetic ML", odds_american=150)]
    first = add_bet(
        w,
        AddBetRequest(
            sportsbook="fanduel", legs=legs, cash_staked=Decimal("5.00"), placed_at=PLACED
        ),
    )
    settle_bet(w, SettleRequest(bet_id=first.bet.id, result="won"))
    add_bet(
        w,
        AddBetRequest(
            sportsbook="fanduel",
            legs=legs,
            cash_staked=Decimal("10.00"),
            placed_at=PLACED + timedelta(hours=1),
            capture_method="screenshot",
        ),
        source=SourceToArchive(archive_dir=archive_dir, path=slip),
    )
    add_balance_snapshot(
        w, BalanceSnapshotRequest(sportsbook="fanduel", cash_balance=Decimal("42.00"))
    )
    record_cash_flow(
        w, CashFlowRequest(kind="deposit", sportsbook="fanduel", amount=Decimal("20.00"))
    )
    return alice


@pytest.fixture
def populated(tmp_path: Path) -> Iterator[Populated]:
    settings = Settings(data_dir=tmp_path / "data")
    slip = tmp_path / "IMG_0001.PNG"
    slip.write_bytes(PNG)
    assert settings.source_archive_dir is not None and settings.backup_dir is not None
    with connect(settings) as conn:
        migrator.apply(conn)
        alice = _populate(conn, settings.source_archive_dir, slip)
        yield Populated(settings, conn, alice, settings.backup_dir)


def _backup(p: Populated, now: datetime = NOW, **kwargs: int) -> backup_service.BackupResult:
    return create_backup(
        p.conn, p.backups, now=now, storage_version=p.settings.storage_version, **kwargs
    )


def _touch(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"synthetic")
    return path


# ------------------------------------------------------------- the copy


def test_a_backup_reopens_with_every_table_and_row(populated: Populated) -> None:
    before = table_counts(populated.conn)
    # A populated warehouse, not an empty schema: the test proves nothing otherwise.
    for table in (
        "control.migration",
        "core.bet",
        "core.bet_leg",
        "core.balance_snapshot",
        "core.cash_flow",
        "core.user_credential",
        "control.source_file",
        "reference.bet_result",
    ):
        assert before[table] > 0, table
    assert before["core.user"] == 2

    made = _backup(populated)

    assert made.path == populated.backups / "bet-20261024T033005Z.duckdb"
    assert made.name == made.path.name
    assert made.created_at == NOW
    assert dict(made.row_counts) == before
    assert made.total_rows == sum(before.values())
    assert made.schema_version == migrator.current_version(populated.conn)
    assert made.byte_size == made.path.stat().st_size
    assert made.sha256 == hashlib.sha256(made.path.read_bytes()).hexdigest()
    assert made.pruned == ()
    # Nothing but the backup is left behind: no partial file, no WAL.
    assert sorted(p.name for p in populated.backups.iterdir()) == [made.name]

    with duckdb.connect(str(made.path), read_only=True) as copy:
        assert table_counts(copy) == before
        assert copy.execute("SELECT max(version) FROM control.migration").fetchone() == (
            made.schema_version,
        )
        assert copy.execute("SELECT count(*) FROM analytics.v_settled_bets").fetchone() == (1,)


def test_a_backup_holds_the_same_values_row_for_row(populated: Populated) -> None:
    """Counts could match with mangled values: types must survive the copy too."""
    made = _backup(populated)
    cursor = populated.conn.cursor()
    cursor.execute(f"ATTACH '{made.path}' AS copy (READ_ONLY)")
    try:
        for table in made.row_counts:
            for left, right in (("bet", "copy"), ("copy", "bet")):
                row = cursor.execute(
                    f"SELECT count(*) FROM (FROM {left}.{table} EXCEPT ALL FROM {right}.{table})"
                ).fetchone()
                assert row == (0,), f"{table}: rows in {left} missing from {right}"
            types = [
                cursor.execute(
                    "SELECT column_name, data_type, column_default FROM duckdb_columns() "
                    "WHERE database_name = ? AND schema_name || '.' || table_name = ? "
                    "ORDER BY column_index",
                    [db, table],
                ).fetchall()
                for db in ("bet", "copy")
            ]
            assert types[0] == types[1], table
    finally:
        cursor.execute("DETACH copy")


def test_a_backup_keeps_constraints_defaults_and_views(populated: Populated) -> None:
    made = _backup(populated)

    def shape(conn: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[object, ...]]]:
        where = "WHERE database_name = current_database()"
        return {
            "constraints": conn.execute(
                "SELECT schema_name, table_name, constraint_type, constraint_text "
                f"FROM duckdb_constraints() {where} ORDER BY ALL"
            ).fetchall(),
            "defaults": conn.execute(
                "SELECT schema_name, table_name, column_name, column_default "
                f"FROM duckdb_columns() {where} AND column_default IS NOT NULL ORDER BY ALL"
            ).fetchall(),
            "views": conn.execute(
                f"SELECT schema_name, view_name FROM duckdb_views() {where} "
                "AND NOT internal ORDER BY ALL"
            ).fetchall(),
        }

    original = shape(populated.conn)
    assert original["constraints"] and original["defaults"] and original["views"]
    with duckdb.connect(str(made.path), read_only=True) as copy:
        assert shape(copy) == original


def test_a_backup_is_doctor_clean_and_in_the_pinned_format(
    populated: Populated, tmp_path: Path
) -> None:
    made = _backup(populated)
    assert on_disk_storage_version(made.path) == on_disk_storage_version(
        populated.settings.db_path  # type: ignore[arg-type]
    )

    restored = tmp_path / "restored"
    for sub in ("sources", "backups"):
        (restored / sub).mkdir(parents=True)
    target = restored / "bet.duckdb"
    target.write_bytes(made.path.read_bytes())
    env = {
        "BET_DATA_DIR": str(restored),
        "BET_TENANT_ID": str(populated.alice.tenant_id),
        "BET_USER_ID": str(populated.alice.user_id),
    }
    report = run_checks(resolve(config_path=tmp_path / "missing.toml", env=env), cwd=tmp_path)
    checks = {c.name: c for c in report.checks}
    for name in ("database", "schema version", "storage format", "local identity"):
        assert checks[name].status is Status.PASS, (name, checks[name].detail)
    assert "no pending migrations" in checks["schema version"].detail
    assert not report.failed


def test_an_uncommitted_write_is_not_in_the_backup(populated: Populated) -> None:
    before = table_counts(populated.conn)
    writer = populated.conn.cursor()
    writer.execute("BEGIN TRANSACTION")
    writer.execute("INSERT INTO core.tenant (id, name) VALUES (?, ?)", [str(uuid.uuid4()), "late"])
    try:
        made = _backup(populated)
    finally:
        writer.execute("COMMIT")
        writer.close()

    assert made.row_counts["core.tenant"] == before["core.tenant"]
    with duckdb.connect(str(made.path), read_only=True) as copy:
        assert copy.execute("SELECT count(*) FROM core.tenant WHERE name = 'late'").fetchone() == (
            0,
        )
    # The write itself was not blocked or lost.
    assert table_counts(populated.conn)["core.tenant"] == before["core.tenant"] + 1


def test_the_callers_own_cursor_is_left_alone(populated: Populated) -> None:
    """The copy runs on a cursor of its own, so a caller mid-transaction is not disturbed."""
    cursor = populated.conn.cursor()
    cursor.execute("BEGIN TRANSACTION")
    _backup(populated, now=NOW)
    cursor.execute("ROLLBACK")
    assert "bet_backup_" not in str(populated.conn.execute("SHOW DATABASES").fetchall())


def test_a_second_backup_in_the_same_second_is_refused(populated: Populated) -> None:
    _backup(populated)
    with pytest.raises(UsageError, match="already exists"):
        _backup(populated)


def test_a_failed_copy_leaves_nothing_behind(populated: Populated) -> None:
    with pytest.raises(DatabaseError, match="could not be written"):
        create_backup(populated.conn, populated.backups, now=NOW, storage_version="v0.0.1")
    assert list(populated.backups.iterdir()) == []


def test_a_failure_inside_the_copy_is_rolled_back_and_detached(
    populated: Populated, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(cursor: duckdb.DuckDBPyConnection, alias: str) -> dict[str, int]:
        raise duckdb.IOException("synthetic disk full")

    monkeypatch.setattr(backup_service, "_row_counts", broken)
    with pytest.raises(DatabaseError, match="synthetic disk full"):
        _backup(populated)
    assert list(populated.backups.iterdir()) == []
    assert "bet_backup_" not in str(populated.conn.execute("SHOW DATABASES").fetchall())


def test_a_leftover_partial_file_is_replaced(populated: Populated) -> None:
    leftover = _touch(populated.backups, f".{backup_name(NOW)}.partial")
    made = _backup(populated)
    assert not leftover.exists()
    assert made.path.exists()


def test_a_backup_time_must_be_timezone_aware(populated: Populated) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _backup(populated, now=NOW.replace(tzinfo=None))


def test_without_migration_bookkeeping_there_is_no_schema_version(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    assert settings.backup_dir is not None
    with connect(settings) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        made = create_backup(conn, settings.backup_dir, now=NOW, storage_version="v1.5.0")
    assert made.schema_version is None
    assert dict(made.row_counts) == {"main.t": 0}


# ------------------------------------------------------------- names


def test_backup_names_round_trip_in_utc() -> None:
    eastern = NOW.astimezone(ZoneInfo("America/New_York"))
    assert eastern.hour != NOW.hour
    assert backup_name(eastern) == "bet-20261024T033005Z.duckdb"
    assert backup_time("bet-20261024T033005Z.duckdb") == NOW


@pytest.mark.parametrize(
    "name",
    [
        "bet-20261024T033005Z.duckdb.wal",
        "bet-20261024T033005Z.duckdb.sha256",
        ".bet-20261024T033005Z.duckdb.partial",
        "bet.duckdb",
        "bet-20261024.duckdb",
        "bet-20261324T033005Z.duckdb",  # month 13: the right shape, no such time
        "xbet-20261024T033005Z.duckdb",
    ],
)
def test_only_exact_backup_names_are_backups(name: str) -> None:
    assert backup_time(name) is None


# ------------------------------------------------------------- pruning


def test_pruning_removes_only_old_backups(tmp_path: Path) -> None:
    d = tmp_path / "backups"
    # Exactly 14 days old is not older than 14 days; a second more is.
    old = [
        _touch(d, backup_name(NOW - age))
        for age in (timedelta(days=40), timedelta(days=15), timedelta(days=14, seconds=1))
    ]
    recent = [
        _touch(d, backup_name(NOW - age))
        for age in (timedelta(days=14), timedelta(days=13), timedelta(days=1), timedelta(0))
    ]
    # Everything else, however old and however similar its name.
    bystanders = [
        _touch(d, name)
        for name in (
            "bet-20200101T000000Z.duckdb.wal",
            "bet-20200101T000000Z.duckdb.sha256",
            ".bet-20200101T000000Z.duckdb.partial",
            "bet.duckdb",
            "notes.txt",
            "bet-20201301T000000Z.duckdb",
        )
    ]
    (d / "bet-20200102T000000Z.duckdb").mkdir()  # a directory with a backup's name
    (d / "bet-20200103T000000Z.duckdb").symlink_to(bystanders[-2])

    pruned = prune_backups(d, 14, NOW)

    assert list(pruned) == [p.name for p in old]  # oldest first
    assert not any(p.exists() for p in old)
    for path in [*recent, *bystanders]:
        assert path.exists(), path.name
    assert (d / "bet-20200102T000000Z.duckdb").is_dir()
    assert (d / "bet-20200103T000000Z.duckdb").is_symlink()


def test_pruning_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    assert prune_backups(tmp_path / "absent", 14, NOW) == ()


def test_pruning_refuses_to_keep_nothing(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="at least 1"):
        prune_backups(tmp_path, 0, NOW)


def test_a_backup_with_keep_days_prunes_after_writing(populated: Populated) -> None:
    stale = _touch(populated.backups, backup_name(NOW - timedelta(days=30)))
    kept = _touch(populated.backups, backup_name(NOW - timedelta(days=3)))
    made = _backup(populated, keep_days=14)
    assert made.pruned == (stale.name,)
    assert not stale.exists()
    assert kept.exists() and made.path.exists()


# ------------------------------------------------------------- latest


def test_latest_is_the_newest_by_name(tmp_path: Path) -> None:
    d = tmp_path / "backups"
    assert latest_backup(d) is None
    _touch(d, "notes.txt")
    assert latest_backup(d) is None
    _touch(d, backup_name(NOW - timedelta(days=1)))
    newest = _touch(d, backup_name(NOW))
    _touch(d, backup_name(NOW - timedelta(days=2)))
    assert latest_backup(d) == newest


def test_file_digest_reads_in_chunks(tmp_path: Path) -> None:
    path = tmp_path / "blob"
    data = secrets.token_bytes(3 * 1024 * 1024 + 7)
    path.write_bytes(data)
    assert file_digest(path) == (len(data), hashlib.sha256(data).hexdigest())
