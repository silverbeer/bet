"""Liveness and readiness.

``/healthz`` answers "is the process up" and never touches the database, so a
locked or migrating warehouse cannot get a live pod restarted. ``/readyz``
answers "can it serve": the connection works and no migration is pending.
"""

from __future__ import annotations

import duckdb
from fastapi import APIRouter

from bet.api.deps import Cursor
from bet.database import migrator
from bet.errors import DatabaseError

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/readyz",
    responses={503: {"description": "Warehouse unusable or schema out of date"}},
)
def readyz(cursor: Cursor) -> dict[str, str]:
    try:
        cursor.execute("SELECT 1").fetchone()
        # Raises DatabaseError, mapped to 503, when a migration is pending.
        migrator.require_current(cursor)
    except duckdb.Error as exc:
        raise DatabaseError(
            f"the warehouse connection is unusable: {exc}",
            remediation="Restart the API server.",
        ) from exc
    return {"status": "ready"}
