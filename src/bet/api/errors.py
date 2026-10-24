"""Mapping ``BetError`` onto HTTP responses.

Every error body has the same shape, so a client branches on ``type`` rather
than parsing prose:

    {"error": {"type": "UsageError", "message": "...", "remediation": "..."}}

A ``BetError`` is raised deliberately and its message is written for the user,
so it is returned as-is. Anything else is a bug: it is logged here and the
client sees only that something failed -- no exception text, no traceback.

FastAPI's own refusals use the same body: an unknown route or method
(Starlette's ``HTTPException``) and a request that fails validation (422,
which adds a ``details`` list of ``loc``/``msg``/``type``). Validation details
never carry the rejected input: it may be a password.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from bet.errors import AuthenticationError, BetError, DatabaseError, NotFoundError, UsageError
from bet.log import get_logger

log = get_logger(__name__)

# Most specific first; the first isinstance match wins.
STATUS_BY_ERROR: tuple[tuple[type[BetError], int], ...] = (
    (UsageError, 400),
    (AuthenticationError, 401),
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


# Starlette's refusals by status, named the way a BetError type would be.
HTTP_ERROR_TYPES: dict[int, str] = {
    404: "NotFoundError",
    405: "MethodNotAllowedError",
}

ROUTES_REMEDIATION = "See /docs for the available routes."


def error_body(
    error_type: str,
    message: str,
    remediation: str | None,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "type": error_type,
        "message": message,
        "remediation": remediation,
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


async def _bet_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BetError)
    status = status_for(exc)
    return JSONResponse(
        status_code=status,
        content=error_body(type(exc).__name__, exc.message, exc.remediation),
        # RFC 6750 section 3: a 401 names the scheme the client must use.
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    if exc.status_code == 404:
        message = f"no route {request.url.path}."
    elif exc.status_code == 405:
        message = f"{request.method} is not allowed on {request.url.path}."
    else:
        message = str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(
            HTTP_ERROR_TYPES.get(exc.status_code, "HTTPError"),
            message,
            ROUTES_REMEDIATION if exc.status_code in (404, 405) else None,
        ),
        # 405 carries Allow, which names the methods the route does take.
        headers=exc.headers,
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": e.get("type")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=error_body(
            "ValidationError",
            "the request is invalid.",
            "See `details`, and /docs for the request schema.",
            details,
        ),
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
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected)
