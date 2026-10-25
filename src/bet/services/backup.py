"""A consistent copy of the whole warehouse, made by the process that holds it open.

The API is the warehouse's only process (ADR 0002), so nothing else can open
the file while it runs, and a file copy taken mid-write is not a backup. The
copy is made from inside, on a cursor of its own: ``EXPORT DATABASE`` writes
every schema, table, constraint, default and view -- migration bookkeeping
included -- and every row, as Parquet, in one statement and so from one
snapshot. Writes in flight on other cursors are neither blocked nor included;
only what was committed when the export began is in it. A fresh DuckDB file is
then attached and the export imported into it with ``IMPORT DATABASE``.

Why not ``COPY FROM DATABASE``, which would skip the intermediate files: on
DuckDB 1.5 its data step loads tables in name order, so
``core.sportsbook_account`` arrives before the ``core.user`` it references and
the copy fails its own foreign keys; and copying the rows by hand instead
trips over ``core.bet``'s generated columns, which no catalog view flags. The
export orders tables by their foreign keys and leaves generated columns out.

The copy is written under a hidden temporary name, flushed, and renamed into
place, so a ``bet-*.duckdb`` file is always a complete one. Its storage format
is pinned to the configured version (ADR 0001), the same as the warehouse.

Pruning deletes only files whose name is exactly a backup's
(``bet-YYYYMMDDTHHMMSSZ.duckdb``), judged by the time in that name -- never by
modification time, and never anything else in the directory.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from bet.errors import DatabaseError, UsageError

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

BACKUP_NAME = re.compile(r"^bet-(?P<stamp>\d{8}T\d{6}Z)\.duckdb$")
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
WRITE_REMEDIATION = "Check the backup directory is writable and has space, then retry."

# One backup at a time: two in the same second would want the same name, and
# a second concurrent copy only doubles the I/O for an identical result.
_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class BackupResult:
    """The file written, how to check it, and what it holds."""

    path: Path
    byte_size: int
    sha256: str
    created_at: datetime
    # The highest applied migration recorded in the copy.
    schema_version: int | None
    # Rows per table, keyed ``schema.table``, counted in the copy itself.
    row_counts: Mapping[str, int]
    # Names of the older backups deleted after this one was written.
    pruned: tuple[str, ...] = field(default=())

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


def backup_name(now: datetime) -> str:
    """``bet-YYYYMMDDTHHMMSSZ.duckdb`` for ``now``, in UTC."""
    return f"bet-{_utc(now).strftime(STAMP_FORMAT)}.duckdb"


def backup_time(name: str) -> datetime | None:
    """The UTC time in a backup's file name, or None if it is not one."""
    match = BACKUP_NAME.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match["stamp"], STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:  # the right shape but no such time, e.g. month 13
        return None


def create_backup(
    conn: DuckDBPyConnection,
    backup_dir: Path,
    *,
    now: datetime,
    storage_version: str,
    keep_days: int | None = None,
) -> BackupResult:
    """Copy the warehouse ``conn`` is open on into ``backup_dir``.

    A cursor of its own is taken from ``conn``, so a caller's open transaction
    is not touched. With ``keep_days``, older backups are pruned once the new
    one is in place (see ``prune_backups``).
    """
    created_at = _utc(now)
    final = backup_dir / backup_name(created_at)
    partial = backup_dir / f".{final.name}.partial"

    with _lock:
        if final.exists():
            raise UsageError(
                f"a backup named {final.name} already exists.",
                remediation="Backups are named to the second; wait a second and retry.",
            )
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            _remove(partial)
            schema_version, counts = _copy(conn, partial, storage_version)
            byte_size, sha256 = _flush_and_digest(partial)
            os.replace(partial, final)
        except OSError as exc:
            with suppress(OSError):
                _remove(partial)
            raise DatabaseError(
                f"the backup could not be written: {exc}",
                remediation=WRITE_REMEDIATION,
            ) from exc

    pruned = prune_backups(backup_dir, keep_days, created_at) if keep_days is not None else ()
    return BackupResult(
        path=final,
        byte_size=byte_size,
        sha256=sha256,
        created_at=created_at,
        schema_version=schema_version,
        row_counts=counts,
        pruned=pruned,
    )


def prune_backups(backup_dir: Path, keep_days: int, now: datetime) -> tuple[str, ...]:
    """Delete backups named for a time more than ``keep_days`` before ``now``.

    Only regular files whose name is exactly a backup's are candidates; a
    symlink, a directory or any other name is never touched. Returns the names
    deleted, oldest first.
    """
    if keep_days < 1:
        raise UsageError(
            f"keep_days must be at least 1, not {keep_days}.",
            remediation="Keep at least a day of backups.",
        )
    if not backup_dir.is_dir():
        return ()
    cutoff = _utc(now) - timedelta(days=keep_days)
    pruned: list[str] = []
    for path, stamp in _backups(backup_dir):
        if stamp < cutoff:
            path.unlink()
            pruned.append(path.name)
    return tuple(pruned)


def latest_backup(backup_dir: Path) -> Path | None:
    """The newest backup in ``backup_dir`` by the time in its name, if any."""
    found = _backups(backup_dir) if backup_dir.is_dir() else []
    return found[-1][0] if found else None


def file_digest(path: Path) -> tuple[int, str]:
    """A file's byte size and sha256 hex digest."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


# ------------------------------------------------------------------ internals


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a backup time must be timezone-aware")
    return moment.astimezone(UTC)


def _backups(backup_dir: Path) -> list[tuple[Path, datetime]]:
    """Every backup file in ``backup_dir``, oldest first."""
    found = []
    for path in backup_dir.iterdir():
        stamp = backup_time(path.name)
        if stamp is not None and not path.is_symlink() and path.is_file():
            found.append((path, stamp))
    return sorted(found, key=lambda item: item[1])


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _remove(partial: Path) -> None:
    """Remove a partial copy and its write-ahead log, if any are left."""
    partial.unlink(missing_ok=True)
    partial.with_name(partial.name + ".wal").unlink(missing_ok=True)


def _copy(
    conn: DuckDBPyConnection, partial: Path, storage_version: str
) -> tuple[int | None, dict[str, int]]:
    """Write the copy to ``partial``; return its schema version and row counts."""
    export_dir = partial.with_name(partial.name + ".export")
    # Attached databases are visible to every cursor on the connection; a
    # random alias cannot collide with a real catalog name.
    alias = f"bet_backup_{secrets.token_hex(4)}"
    cursor = conn.cursor()
    source = "memory"
    attached = False
    shutil.rmtree(export_dir, ignore_errors=True)
    try:
        row = cursor.execute("SELECT current_database()").fetchone()
        assert row is not None
        source = str(row[0])
        # One statement, so one transaction and one snapshot of every table.
        cursor.execute(f"EXPORT DATABASE {_literal(str(export_dir))} (FORMAT parquet)")
        cursor.execute(
            f"ATTACH {_literal(str(partial))} AS {alias} "
            f"(STORAGE_VERSION {_literal(storage_version)})"
        )
        attached = True
        # IMPORT writes into the current database; USE changes this cursor's only.
        cursor.execute(f"USE {alias}")
        cursor.execute(f"IMPORT DATABASE {_literal(str(export_dir))}")
        counts = _row_counts(cursor, alias)
        schema_version = _schema_version(cursor, alias, counts)
        cursor.execute(f"USE {_quote(source)}")
        # Detaching checkpoints the copy into its file and closes it.
        cursor.execute(f"DETACH {alias}")
        attached = False
    except duckdb.Error as exc:
        # Best effort: the original error is the one worth reporting.
        with suppress(duckdb.Error):
            if attached:
                cursor.execute(f"USE {_quote(source)}")
                cursor.execute(f"DETACH {alias}")
        _remove(partial)
        raise DatabaseError(
            f"the backup could not be written: {exc}",
            remediation=WRITE_REMEDIATION,
        ) from exc
    finally:
        cursor.close()
        shutil.rmtree(export_dir, ignore_errors=True)
    return schema_version, counts


def _row_counts(cursor: DuckDBPyConnection, alias: str) -> dict[str, int]:
    tables = cursor.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = ? ORDER BY schema_name, table_name",
        [alias],
    ).fetchall()
    counts: dict[str, int] = {}
    for schema, table in tables:
        row = cursor.execute(
            f"SELECT count(*) FROM {alias}.{_quote(schema)}.{_quote(table)}"
        ).fetchone()
        assert row is not None
        counts[f"{schema}.{table}"] = int(row[0])
    return counts


def _schema_version(
    cursor: DuckDBPyConnection, alias: str, counts: Mapping[str, int]
) -> int | None:
    if "control.migration" not in counts:
        return None
    row = cursor.execute(f"SELECT max(version) FROM {alias}.control.migration").fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _flush_and_digest(partial: Path) -> tuple[int, str]:
    """Size and sha256 of the finished copy, after forcing it to disk."""
    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
    return file_digest(partial)


__all__ = [
    "BACKUP_NAME",
    "BackupResult",
    "backup_name",
    "backup_time",
    "create_backup",
    "file_digest",
    "latest_backup",
    "prune_backups",
]
