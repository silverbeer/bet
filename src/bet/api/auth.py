"""Logging in, refreshing and logging out (ADR 0002 section 4).

There is no signup route: users are created by the operator with
``bet user create`` in local mode (ADR 0002 section 5).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from bet.api.deps import Cursor
from bet.services import auth
from bet.services.auth import MAX_PASSWORD_LENGTH, TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])

UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"description": "Wrong credentials, or an invalid, expired or revoked token"}
}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=200)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[SecretStr, Field(min_length=1)]


def _settings(request: Request) -> auth.TokenSettings:
    settings: auth.TokenSettings = request.app.state.settings.auth
    return settings


@router.post("/token", responses=UNAUTHORIZED)
def token(body: LoginRequest, request: Request, cursor: Cursor) -> TokenPair:
    """Exchange a user name and password for an access and a refresh token."""
    return auth.login(cursor, body.username, body.password.get_secret_value(), _settings(request))


@router.post("/refresh", responses=UNAUTHORIZED)
def refresh(body: RefreshRequest, request: Request, cursor: Cursor) -> TokenPair:
    """Exchange a refresh token for a new pair. The presented token stops working."""
    return auth.refresh(cursor, body.refresh_token.get_secret_value(), _settings(request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, responses=UNAUTHORIZED)
def logout(body: RefreshRequest, request: Request, cursor: Cursor) -> Response:
    """Revoke a refresh token. Its access tokens lapse within their lifetime."""
    auth.logout(cursor, body.refresh_token.get_secret_value(), _settings(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
