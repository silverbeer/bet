"""Operator user management: backs ``bet user create`` and ``bet user set-password``.

Local mode only (ADR 0002 section 5). Users are created by whoever holds the
warehouse file; there is no signup endpoint. Every user goes into the tenant
``bet init`` created, and the display name is the login name, so it must be
unique within that tenant, ignoring case.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from bet.database import identity
from bet.database.repository import Warehouse
from bet.errors import DatabaseError, NotFoundError, UsageError
from bet.models.ownership import OwnerScope
from bet.services.auth import set_password

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class PasswordSet(BaseModel):
    """Whose password was set, and whether it replaced an earlier one."""

    model_config = ConfigDict(frozen=True)

    display_name: str
    scope: OwnerScope
    rotated: bool


def _local_tenant(conn: DuckDBPyConnection) -> OwnerScope:
    local = identity.existing_scope(conn)
    if local is None:
        raise DatabaseError(
            "the warehouse has no local user.",
            remediation="Run `bet init` to create the local tenant and user.",
        )
    return local


def _name(display_name: str) -> str:
    name = display_name.strip()
    if not name:
        raise UsageError("a user name cannot be blank.")
    return name


def create_user(conn: DuckDBPyConnection, display_name: str) -> OwnerScope:
    """Add a user to the local tenant. Refuses a name already taken there."""
    name = _name(display_name)
    tenant_id = _local_tenant(conn).tenant_id
    if identity.users_named(conn, name, tenant_id=tenant_id):
        raise UsageError(
            f"a user named {name!r} already exists.",
            remediation="Names are login names and must be unique, ignoring case.",
        )
    return identity.create_user(conn, tenant_id, name)


def set_password_for(
    conn: DuckDBPyConnection, display_name: str, password: str, *, now: datetime | None = None
) -> PasswordSet:
    """Set or rotate the password of the local-tenant user with this name.

    Works for the user ``bet init`` created, who has owned every recorded bet
    since before credentials existed.
    """
    name = _name(display_name)
    tenant_id = _local_tenant(conn).tenant_id
    matches = identity.users_named(conn, name, tenant_id=tenant_id)
    if not matches:
        raise NotFoundError(
            f"no user named {name!r}.",
            remediation="Create one with `bet user create <name>`.",
        )
    if len(matches) > 1:
        raise UsageError(
            f"more than one user is named {name!r}, ignoring case.",
            remediation="Login names must be unique; neither can log in until they are.",
        )
    user = matches[0]
    w = Warehouse(conn, OwnerScope(tenant_id=user.tenant_id, user_id=user.id))
    rotated = w.credentials.current() is not None
    set_password(w, password, now=now)
    return PasswordSet(display_name=user.display_name, scope=w.scope, rotated=rotated)


__all__ = ["PasswordSet", "create_user", "set_password_for"]
