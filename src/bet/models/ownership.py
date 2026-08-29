"""Domain models for the ownership chain.

``Tenant -> User -> SportsbookAccount``, per docs/OWNERSHIP.md.

Every timestamp is timezone-aware. A naive datetime is rejected rather than
assumed to be UTC: BET records when a bet was placed, and silently reinterpreting
a wall-clock time is how a bet lands on the wrong day.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

UserStatus = Literal["active", "disabled"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class OwnedModel(BaseModel):
    """Base for models that carry both ownership columns.

    Both are required. Nullable ownership is prohibited by OWNERSHIP.md section
    3, and making them optional here would let an unowned row be constructed in
    Python even though the database would refuse it.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    tenant_id: UUID
    user_id: UUID


class AwareTimestamps(BaseModel):
    """Mixin rejecting naive datetimes on the common audit columns."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value


class Tenant(AwareTimestamps):
    """The isolation boundary. Locally there is exactly one."""

    id: UUID
    name: str = Field(min_length=1)


class User(AwareTimestamps):
    """A person. Tenant-scoped; its own id is the user id."""

    tenant_id: UUID
    id: UUID
    display_name: str = Field(min_length=1)
    locale: str = "en-US"
    timezone: str = "UTC"
    status: UserStatus = "active"
    preferences: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone", mode="after")
    @classmethod
    def _must_be_a_real_timezone(cls, value: str) -> str:
        """Reject an unknown zone at the boundary.

        This is the display timezone for every stored UTC timestamp. An
        unresolvable name would surface much later, as bets rendered on the
        wrong day, with nothing pointing back to the cause.
        """
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value


class Sportsbook(AwareTimestamps):
    """Operator identity. Reference data, keyed by a stable natural code.

    The export capability flags are tri-state on purpose: ``None`` means "not
    yet established" rather than "no". Which formats each operator publishes is
    being determined against real specimens (SB-689), and the versioned importer
    profile (SB-710) is what will ultimately declare supported formats.
    """

    code: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1)
    is_supported: bool = True
    exports_csv: bool | None = None
    exports_xlsx: bool | None = None
    exports_pdf: bool | None = None
    has_api: bool | None = None


class SportsbookAccount(OwnedModel, AwareTimestamps):
    """One operator account belonging to one user."""

    id: UUID
    sportsbook_code: str = Field(min_length=1)
    label: str = Field(min_length=1)
    external_account_ref: str | None = None
    is_active: bool = True
    opened_at: datetime | None = None

    @field_validator("opened_at", mode="after")
    @classmethod
    def _opened_at_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        return value


class OwnerScope(BaseModel):
    """The identity every scoped query runs as.

    Resolved once at startup and passed down. Frozen so it cannot be mutated
    mid-command into someone else's scope.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    user_id: UUID


LocalIdentity = Annotated[
    OwnerScope,
    "The single local tenant and user, bootstrapped by `bet init`.",
]

__all__ = [
    "AwareTimestamps",
    "LocalIdentity",
    "OwnedModel",
    "OwnerScope",
    "Sportsbook",
    "SportsbookAccount",
    "Tenant",
    "User",
    "UserStatus",
    "utc_now",
]
