"""Migration harness: ordering, idempotence, rollback, and tamper detection.

The runner is the easy part. What is tested hardest here is refusal — the cases
where the database and the migration files disagree and continuing would leave
a warehouse whose schema nobody can account for.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from bet.config import Settings
from bet.database import migrator
from bet.database.connection import connect, on_disk_storage_version
from bet.errors import DatabaseError


@pytest.fixture
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(str(tmp_path / "test.duckdb"))
    yield conn
    conn.close()


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    return directory


def write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql)
    return path


# ------------------------------------------------------------- applying


def test_applies_from_empty(db: duckdb.DuckDBPyConnection, migrations: Path) -> None:
    write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    write(migrations, "0002_second.sql", "CREATE TABLE core.bet(id INTEGER);")

    applied = migrator.apply(db, migrations)

    assert [m.version for m in applied] == [1, 2]
    assert migrator.current_version(db) == 2


def test_is_idempotent_on_re_run(db: duckdb.DuckDBPyConnection, migrations: Path) -> None:
    write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    migrator.apply(db, migrations)

    assert migrator.apply(db, migrations) == []
    assert migrator.current_version(db) == 1


def test_applies_in_version_order_not_filesystem_order(
    db: duckdb.DuckDBPyConnection, migrations: Path
) -> None:
    """0002 depends on 0001, so an out-of-order run would fail outright."""
    write(migrations, "0002_add_table.sql", "CREATE TABLE core.bet(id INTEGER);")
    write(migrations, "0001_add_schema.sql", "CREATE SCHEMA core;")

    assert [m.version for m in migrator.apply(db, migrations)] == [1, 2]


def test_records_checksum_and_timestamp(db: duckdb.DuckDBPyConnection, migrations: Path) -> None:
    path = write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    migrator.apply(db, migrations)

    (record,) = migrator.applied(db)
    assert record.version == 1
    assert record.name == "first"
    assert record.checksum == migrator.checksum(path.read_text())
    assert record.applied_at is not None


def test_only_pending_migrations_are_applied(
    db: duckdb.DuckDBPyConnection, migrations: Path
) -> None:
    write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    migrator.apply(db, migrations)

    write(migrations, "0002_second.sql", "CREATE TABLE core.bet(id INTEGER);")
    assert [m.version for m in migrator.pending(db, migrations)] == [2]
    assert [m.version for m in migrator.apply(db, migrations)] == [2]


def test_empty_directory_is_not_an_error(db: duckdb.DuckDBPyConnection, migrations: Path) -> None:
    assert migrator.apply(db, migrations) == []
    assert migrator.current_version(db) is None


# ------------------------------------------------------------- refusing


def test_detects_an_edited_migration(db: duckdb.DuckDBPyConnection, migrations: Path) -> None:
    """The database no longer matches the code that claims to have built it."""
    path = write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    migrator.apply(db, migrations)

    path.write_text("CREATE SCHEMA core; -- an innocent looking edit")

    with pytest.raises(DatabaseError) as caught:
        migrator.verify(db, migrations)
    assert "modified after it was applied" in caught.value.message
    assert "checksum" in (caught.value.remediation or "")


def test_an_edited_migration_blocks_further_migration(
    db: duckdb.DuckDBPyConnection, migrations: Path
) -> None:
    path = write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    migrator.apply(db, migrations)
    path.write_text("CREATE SCHEMA core;\nCREATE TABLE core.x(i INTEGER);")
    write(migrations, "0002_second.sql", "CREATE TABLE core.bet(id INTEGER);")

    with pytest.raises(DatabaseError):
        migrator.apply(db, migrations)
    assert migrator.current_version(db) == 1


def test_detects_a_deleted_migration(db: duckdb.DuckDBPyConnection, migrations: Path) -> None:
    path = write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    migrator.apply(db, migrations)
    path.unlink()

    with pytest.raises(DatabaseError) as caught:
        migrator.verify(db, migrations)
    assert "file is missing" in caught.value.message


def test_detects_a_migration_inserted_below_the_applied_high_water_mark(
    db: duckdb.DuckDBPyConnection, migrations: Path
) -> None:
    """A forward-only runner would skip it forever, so it is refused."""
    write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    write(migrations, "0003_third.sql", "CREATE TABLE core.a(i INTEGER);")
    migrator.apply(db, migrations)

    write(migrations, "0002_sneaked_in.sql", "CREATE TABLE core.b(i INTEGER);")

    with pytest.raises(DatabaseError) as caught:
        migrator.verify(db, migrations)
    assert "would never run" in caught.value.message


def test_rejects_a_badly_named_file(migrations: Path) -> None:
    """A migration skipped for a typo is a schema change that never happened."""
    write(migrations, "001_too_short.sql", "SELECT 1;")
    with pytest.raises(DatabaseError) as caught:
        migrator.discover(migrations)
    assert "unusable name" in caught.value.message


def test_rejects_duplicate_version_numbers(migrations: Path) -> None:
    write(migrations, "0001_one.sql", "SELECT 1;")
    write(migrations, "0001_other.sql", "SELECT 1;")
    with pytest.raises(DatabaseError) as caught:
        migrator.discover(migrations)
    assert "share version" in caught.value.message


def test_non_sql_files_are_ignored(db: duckdb.DuckDBPyConnection, migrations: Path) -> None:
    write(migrations, "README.md", "not a migration")
    write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    assert [m.version for m in migrator.discover(migrations)] == [1]


# ------------------------------------------------------------ rollback


def test_a_failing_migration_rolls_back_completely(
    db: duckdb.DuckDBPyConnection, migrations: Path
) -> None:
    """A partial schema change is worse than none: it cannot be reasoned about."""
    write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    write(
        migrations,
        "0002_broken.sql",
        "CREATE TABLE core.good(i INTEGER); THIS IS NOT SQL;",
    )

    with pytest.raises(DatabaseError) as caught:
        migrator.apply(db, migrations)
    assert "0002_broken failed" in caught.value.message

    assert migrator.current_version(db) == 1
    remaining = db.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'good'"
    ).fetchone()
    assert remaining is not None
    assert remaining[0] == 0


def test_a_failure_leaves_earlier_migrations_applied(
    db: duckdb.DuckDBPyConnection, migrations: Path
) -> None:
    write(migrations, "0001_first.sql", "CREATE SCHEMA core;")
    write(migrations, "0002_broken.sql", "NOT SQL;")
    with pytest.raises(DatabaseError):
        migrator.apply(db, migrations)

    schemas = db.execute(
        "SELECT count(*) FROM duckdb_schemas() WHERE schema_name = 'core'"
    ).fetchone()
    assert schemas is not None
    assert schemas[0] == 1


# ------------------------------------------------------ storage version


def test_new_databases_are_created_with_the_pinned_storage_version(
    tmp_path: Path,
) -> None:
    """DuckDB's default is *not* its own native format, so the pin is load-bearing."""
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data, storage_version="v1.5.0")

    with connect(settings) as conn:
        conn.execute("CREATE TABLE t(i INTEGER)")

    assert settings.db_path is not None
    assert on_disk_storage_version(settings.db_path) == 68


def test_an_older_pin_produces_an_older_on_disk_format(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data, storage_version="v1.2.0")

    with connect(settings) as conn:
        conn.execute("CREATE TABLE t(i INTEGER)")

    assert settings.db_path is not None
    assert on_disk_storage_version(settings.db_path) == 65


def test_a_non_duckdb_file_reports_no_storage_version(tmp_path: Path) -> None:
    impostor = tmp_path / "not.duckdb"
    impostor.write_text("hello")
    assert on_disk_storage_version(impostor) is None


def test_connect_refuses_a_database_inside_a_git_work_tree(tmp_path: Path) -> None:
    import subprocess

    from bet.errors import DataLocationError

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

    settings = Settings.model_construct(
        data_dir=repo / "data",
        db_path=repo / "data" / "bet.duckdb",
        source_archive_dir=repo / "data" / "sources",
        backup_dir=repo / "data" / "backups",
    )
    with pytest.raises(DataLocationError), connect(settings) as _:
        pass


def test_connect_without_create_refuses_a_missing_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    with pytest.raises(DatabaseError) as caught, connect(settings, create=False) as _:
        pass
    assert "bet init" in (caught.value.remediation or "")
