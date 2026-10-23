"""Mapping ``BetError`` onto HTTP responses.

Every error body has the same shape, so a client branches on ``type`` rather
than parsing prose:

    {"error": {"type": "UsageError", "message": "...", "remediation": "..."}}

A ``BetError`` is raised deliberately and its message is written for the user,
so it is returned as-is. Anything else is a bug: it is logged here and the
client sees only that something failed -- no exception text, no traceback.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bet.errors import BetError, DatabaseError, NotFoundError, UsageError
from bet.log import get_logger

log = get_logger(__name__)

# Most specific first; the first isinstance match wins.
STATUS_BY_ERROR: tuple[tuple[type[BetError], int], ...] = (
    (UsageError, 400),
    (NotFoundError, 404),
    # Unreachable, locked or out of date: the request may succeed once the
    # warehouse is fixed, which is what 503 says.
    (DatabaseError, 503),
)


def status_for(exc: BetError) -> int:
    for error_type, status in STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return 500


def error_body(error_type: str, message: str, remediation: str | None) -> dict[str, object]:
    return {"error": {"type": error_type, "message": message, "remediation": remediation}}


async def _bet_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BetError)
    return JSONResponse(
        status_code=status_for(exc),
        content=error_body(type(exc).__name__, exc.message, exc.remediation),
    )


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.error(
        "unhandled exception",
        method=request.method,
        path=request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content=error_body("InternalError", "internal server error", None),
    )


def install(app: FastAPI) -> None:
    app.add_exception_handler(BetError, _bet_error)
    app.add_exception_handler(Exception, _unexpected)
