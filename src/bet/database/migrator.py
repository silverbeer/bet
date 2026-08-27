"""Forward-only SQL migrations.

Numbered ``.sql`` files applied in order, each inside a transaction, each
recorded with a checksum. There is no down-migration: a rollback of schema is a
new forward migration, because an automated down-migration on a warehouse of
irreplaceable financial history is a way to lose it.

The integrity checks matter more than the runner. A migration file edited after
it was applied means the database no longer matches the code that supposedly
built it, and every later assumption is unverified. That is detected and
refused, not warned about.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bet.errors import DatabaseError

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _dir(directory: Path | None) -> Path:
    """Resolve the migrations directory at call time, not at import time.

    A ``directory: Path = MIGRATIONS_DIR`` default would bind the module value
    when the function is defined, making it impossible to override afterwards —
    including from a test.
    """
    return MIGRATIONS_DIR if directory is None else directory


FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS control;
CREATE TABLE IF NOT EXISTS control.migration (
    version     INTEGER      NOT NULL PRIMARY KEY,
    name        VARCHAR      NOT NULL,
    checksum    VARCHAR      NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL,
    duration_ms BIGINT       NOT NULL
);
"""


def checksum(sql: str) -> str:
    """Hash migration content, ignoring line-ending differences.

    Normalising newlines means a checkout on another platform does not read as
    tampering, while any real edit still does.
    """
    return hashlib.sha256(sql.replace("\r\n", "\n").encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text()

    @property
    def checksum(self) -> str:
        return checksum(self.sql)


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: datetime


def discover(directory: Path | None = None) -> list[Migration]:
    """Return every migration in the directory, ordered by version.

    A file that does not match the naming convention is an error rather than
    something to skip: a migration silently ignored because of a typo in its
    name is a schema change that never happened.
    """
    resolved = _dir(directory)
    if not resolved.is_dir():
        return []

    found: dict[int, Migration] = {}
    for path in sorted(resolved.iterdir()):
        if path.name.startswith(".") or path.suffix != ".sql":
            continue
        match = FILENAME.match(path.name)
        if match is None:
            raise DatabaseError(
                f"migration file has an unusable name: {path.name}",
                remediation="Migrations must be named NNNN_lower_snake_case.sql, "
                "for example 0001_control_schema.sql.",
            )
        version = int(match["version"])
        if version in found:
            raise DatabaseError(
                f"two migrations share version {version:04d}: "
                f"{found[version].path.name} and {path.name}",
                remediation="Renumber one of them. Version numbers must be unique.",
            )
        found[version] = Migration(version, match["name"], path)

    return [found[v] for v in sorted(found)]


def _bootstrap(conn: DuckDBPyConnection) -> None:
    conn.execute(BOOTSTRAP)


def applied(conn: DuckDBPyConnection) -> list[AppliedMigration]:
    """Return the migrations recorded as applied, in version order."""
    _bootstrap(conn)
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM control.migration ORDER BY version"
    ).fetchall()
    return [AppliedMigration(int(r[0]), str(r[1]), str(r[2]), r[3]) for r in rows]


def verify(conn: DuckDBPyConnection, directory: Path | None = None) -> None:
    """Raise if the database and the migration files disagree.

    Three ways they can disagree, all refused:

    * an applied migration's file was edited — the schema no longer matches the
      code that claims to have produced it;
    * an applied migration's file is gone — the history cannot be reconstructed;
    * a *new* migration was inserted below the highest applied version — it
      would be skipped forever, because the runner only moves forward.
    """
    resolved = _dir(directory)
    on_disk = {m.version: m for m in discover(resolved)}
    already = applied(conn)
    if not already:
        return

    highest = max(m.version for m in already)

    for record in already:
        migration = on_disk.get(record.version)
        if migration is None:
            raise DatabaseError(
                f"migration {record.version:04d}_{record.name} is applied to the "
                "database but its file is missing.",
                remediation="Restore the migration file. The applied schema cannot "
                "be verified without it.",
            )
        if migration.checksum != record.checksum:
            raise DatabaseError(
                f"migration {record.version:04d}_{record.name} was modified after it was applied.",
                remediation=(
                    "The database no longer matches the migration that produced it.\n"
                    f"  expected checksum {record.checksum[:16]}…\n"
                    f"  file checksum     {migration.checksum[:16]}…\n"
                    "Revert the file, or write a new forward migration instead of "
                    "editing an applied one."
                ),
            )

    unapplied_below = [v for v in on_disk if v < highest and v not in {m.version for m in already}]
    if unapplied_below:
        listed = ", ".join(f"{v:04d}" for v in sorted(unapplied_below))
        raise DatabaseError(
            f"migration(s) {listed} were added below the highest applied version "
            f"({highest:04d}) and would never run.",
            remediation="Renumber them above the highest applied version.",
        )


def pending(conn: DuckDBPyConnection, directory: Path | None = None) -> list[Migration]:
    """Return migrations not yet applied, in order. Verifies integrity first."""
    resolved = _dir(directory)
    verify(conn, resolved)
    done = {m.version for m in applied(conn)}
    return [m for m in discover(resolved) if m.version not in done]


def current_version(conn: DuckDBPyConnection) -> int | None:
    """The highest applied migration version, or None on an empty database."""
    records = applied(conn)
    return max((r.version for r in records), default=None)


def apply(conn: DuckDBPyConnection, directory: Path | None = None) -> list[Migration]:
    """Apply every pending migration in order. Returns those applied.

    Each migration runs in its own transaction, so a failure leaves the database
    at the last complete version rather than partway through a schema change.
    Re-running with nothing pending is a no-op.
    """
    to_apply = pending(conn, _dir(directory))

    for migration in to_apply:
        started = time.perf_counter()
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO control.migration "
                "(version, name, checksum, applied_at, duration_ms) VALUES (?, ?, ?, ?, ?)",
                [
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(UTC),
                    int((time.perf_counter() - started) * 1000),
                ],
            )
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise DatabaseError(
                f"migration {migration.version:04d}_{migration.name} failed: {exc}",
                remediation=(
                    "The database was rolled back to version "
                    f"{migration.version - 1:04d}. Fix the migration and retry."
                ),
            ) from exc

    return to_apply
