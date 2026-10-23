"""Request dependencies.

The process holds one DuckDB connection (opened by the lifespan). Each request
gets its own ``cursor()`` -- DuckDB's per-thread handle onto that connection --
and the cursor is closed when the request finishes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import duckdb
from duckdb import DuckDBPyConnection
from fastapi import Depends, Request

from bet.errors import DatabaseError


def get_cursor(request: Request) -> Iterator[DuckDBPyConnection]:
    conn: DuckDBPyConnection = request.app.state.conn
    try:
        cursor = conn.cursor()
    except duckdb.Error as exc:
        raise DatabaseError(
            f"the warehouse connection is unusable: {exc}",
            remediation="Restart the API server.",
        ) from exc
    try:
        yield cursor
    finally:
        cursor.close()


Cursor = Annotated[DuckDBPyConnection, Depends(get_cursor)]
