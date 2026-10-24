"""``GET /me``: who the bearer token says you are -- display name and time zone only."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.database import identity
from bet.errors import AuthenticationError

router = APIRouter(tags=["users"])


class Me(BaseModel):
    display_name: str
    timezone: str


@router.get("/me", responses=UNAUTHORIZED)
def me(scope: CurrentScope, cursor: Cursor) -> Me:
    user = identity.find_user(cursor, scope)
    if user is None:  # pragma: no cover - CurrentScope has just checked the user exists
        raise AuthenticationError("the token's user no longer exists.")
    return Me(display_name=user.display_name, timezone=user.timezone)
