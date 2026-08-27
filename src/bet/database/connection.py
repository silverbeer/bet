"""Opening the warehouse.

Two things happen here that must happen everywhere, which is why nothing else
in BET calls ``duckdb.connect`` directly:

* the on-disk storage format is pinned explicitly (ADR 0001), and
* the database path is re-checked against the git rule.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from bet.config import Settings
from bet.errors import DatabaseError
from bet.paths import assert_outside_git

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


@contextmanager
def connect(
    settings: Settings, *, read_only: bool = False, create: bool = True
) -> Iterator[DuckDBPyConnection]:
    """Open the warehouse with the configured storage version pinned.

    ``storage_compatibility_version`` only takes effect when the file is
    created; an existing database keeps the format it was made with. That is
    intended — silently rewriting someone's warehouse on connect would be a far
    worse surprise than an inconsistent pin, which `bet doctor` reports.
    """
    db_path = settings.db_path
    if db_path is None:  # pragma: no cover - the validator always fills this in
        raise DatabaseError("no database path is configured.")

    assert_outside_git(db_path, field="db_path")

    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    elif not db_path.exists():
        raise DatabaseError(
            f"no warehouse at {db_path}.",
            remediation="Run `bet init` to create it.",
        )

    try:
        conn = duckdb.connect(
            str(db_path),
            read_only=read_only,
            config={"storage_compatibility_version": settings.storage_version},
        )
    except duckdb.Error as exc:
        raise DatabaseError(
            f"cannot open {db_path}: {exc}",
            remediation=(
                "Check the file is a DuckDB database and is not locked by another process."
            ),
        ) from exc

    try:
        yield conn
    finally:
        conn.close()


def on_disk_storage_version(db_path: Path) -> int | None:
    """Read the storage format version from a DuckDB file header.

    Lets `bet doctor` report the format actually on disk rather than the one
    configuration merely asked for. Returns None if the file is not a DuckDB
    database.
    """
    try:
        head = db_path.read_bytes()[:4096]
    except OSError:
        return None
    magic = head.find(b"DUCK")
    if magic == -1 or len(head) < magic + 12:
        return None
    return int(struct.unpack("<Q", head[magic + 4 : magic + 12])[0])
