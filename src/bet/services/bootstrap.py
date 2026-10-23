"""Bringing a warehouse up to date: migrations, then the local tenant and user.

Backs ``bet init``. Directories and the config file are the CLI's own
business -- a server is configured differently -- so this starts from an open
connection. Idempotent: run twice, it applies nothing and creates no one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from bet.database import identity, migrator
from bet.models.ownership import OwnerScope

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class Bootstrapped(BaseModel):
    """What bootstrapping found and did."""

    model_config = ConfigDict(frozen=True)

    pending_before: int
    applied: list[int]
    version: int | None
    scope: OwnerScope
    scope_created: bool


def bootstrap_warehouse(
    conn: DuckDBPyConnection, *, display_name: str | None = None
) -> Bootstrapped:
    """Apply pending migrations, then create the local tenant and user if missing."""
    pending_before = len(migrator.pending(conn))
    applied = migrator.apply(conn)
    version = migrator.current_version(conn)
    scope_existed = identity.existing_scope(conn) is not None
    scope = identity.bootstrap(conn, display_name=display_name)
    return Bootstrapped(
        pending_before=pending_before,
        applied=[m.version for m in applied],
        version=version,
        scope=scope,
        scope_created=not scope_existed,
    )


__all__ = ["Bootstrapped", "bootstrap_warehouse"]
