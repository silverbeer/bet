"""Request dependencies.

The process holds one DuckDB connection (opened by the lifespan). Each request
gets its own ``cursor()`` -- DuckDB's per-thread handle onto that connection --
and the cursor is closed when the request finishes.

``CurrentScope`` is the identity a request runs as. It comes only from a
verified bearer access token's ``tid`` and ``sub`` claims -- never from the
local "first user" shortcut the CLI uses -- and every route except the health
checks depends on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import duckdb
from duckdb import DuckDBPyConnection
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bet.errors import AuthenticationError, DatabaseError
from bet.models.ownership import OwnerScope
from bet.services import auth


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


# auto_error=False: a missing header is reported in the BET error body below,
# not as FastAPI's own 403.
bearer = HTTPBearer(auto_error=False, description="An access token from POST /auth/token.")


def current_scope(
    request: Request,
    cursor: Cursor,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> OwnerScope:
    if credentials is None:
        raise AuthenticationError(
            "an access token is required.",
            remediation="Send `Authorization: Bearer <token>`; get one at POST /auth/token.",
        )
    return auth.authenticate(cursor, credentials.credentials, request.app.state.settings.auth)


CurrentScope = Annotated[OwnerScope, Depends(current_scope)]
