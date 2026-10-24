"""A user's password and the refresh tokens issued to them (ADR 0002 section 4).

Both are owned rows: a password and a session belong to exactly one user.
Neither model holds anything replayable -- ``password_hash`` is an argon2id PHC
string and ``token_hash`` a SHA-256 digest -- and both are still kept out of
``repr`` so a logged model never prints them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from bet.models.ownership import OwnedModel, utc_now


def _require_aware(model: OwnedModel, *names: str) -> None:
    for name in names:
        value = getattr(model, name)
        if value is not None and value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")


class UserCredential(OwnedModel):
    """One user's password, as an argon2id hash. ``rotated_at`` is the last change."""

    password_hash: str = Field(min_length=1, repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    rotated_at: datetime | None = None

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        _require_aware(self, "created_at", "rotated_at")
        return self


class RefreshToken(OwnedModel):
    """One issued refresh token. ``id`` is its ``jti``; the token itself is not kept."""

    id: UUID
    token_hash: str = Field(min_length=64, max_length=64, repr=False)
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by: UUID | None = None

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        _require_aware(self, "issued_at", "expires_at", "revoked_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self


__all__ = ["RefreshToken", "UserCredential"]
