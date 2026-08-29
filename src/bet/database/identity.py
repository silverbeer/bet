"""Bootstrapping and resolving the local tenant and user.

`bet init` creates exactly one of each and records their ids in the config file.
Every later command resolves that pair once into an :class:`OwnerScope`.

Ids are generated, never well-known constants. A nil UUID would make fixtures
prettier and would collide the first time two BET databases were merged — which
is the one operation the ownership columns exist to permit (OWNERSHIP.md 5.3).
"""

from __future__ import annotations

import getpass
import tomllib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from bet.config import ResolvedConfig, write_config
from bet.errors import DatabaseError
from bet.models.ownership import OwnerScope, Tenant, User

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

DEFAULT_TENANT_NAME = "local"


def _generate_id(conn: DuckDBPyConnection) -> UUID:
    """Mint a uuidv7 using the database, so ids are time-ordered like the rest.

    Falls back to uuid4 if the function is unavailable, which keeps uniqueness
    while losing only insertion locality.
    """
    row = conn.execute("SELECT uuidv7()").fetchone()
    return UUID(str(row[0])) if row else uuid4()


def existing_scope(conn: DuckDBPyConnection) -> OwnerScope | None:
    """Return the local tenant and user if they have been created."""
    row = conn.execute(
        "SELECT u.tenant_id, u.id FROM core.user u ORDER BY u.created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return OwnerScope(tenant_id=UUID(str(row[0])), user_id=UUID(str(row[1])))


def bootstrap(conn: DuckDBPyConnection, *, display_name: str | None = None) -> OwnerScope:
    """Create the single local tenant and user if they do not already exist.

    Idempotent: a second call returns the identity created by the first, so
    `bet init` can be re-run without producing a second user whose bets would be
    invisible to the first.
    """
    already = existing_scope(conn)
    if already is not None:
        return already

    now = datetime.now(UTC)
    tenant = Tenant(id=_generate_id(conn), name=DEFAULT_TENANT_NAME, created_at=now, updated_at=now)
    user = User(
        tenant_id=tenant.id,
        id=_generate_id(conn),
        display_name=display_name or _local_display_name(),
        created_at=now,
        updated_at=now,
    )

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "INSERT INTO core.tenant (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            [str(tenant.id), tenant.name, tenant.created_at, tenant.updated_at],
        )
        conn.execute(
            "INSERT INTO core.user "
            "(tenant_id, id, display_name, locale, timezone, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(user.tenant_id),
                str(user.id),
                user.display_name,
                user.locale,
                user.timezone,
                user.status,
                user.created_at,
                user.updated_at,
            ],
        )
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        raise DatabaseError(
            f"could not create the local tenant and user: {exc}",
            remediation="The database was left unchanged. Run `bet doctor` for details.",
        ) from exc

    return OwnerScope(tenant_id=tenant.id, user_id=user.id)


def _local_display_name() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getuser is reliable on supported platforms
        return "local"


def record_in_config(config: ResolvedConfig, scope: OwnerScope) -> None:
    """Persist the resolved identity so later commands run as the same user."""
    existing: dict[str, object] = {}
    if config.config_path.is_file():
        with config.config_path.open("rb") as handle:
            existing = dict(tomllib.load(handle))

    existing["tenant_id"] = str(scope.tenant_id)
    existing["user_id"] = str(scope.user_id)
    write_config(config.config_path, existing)


def resolve_scope(conn: DuckDBPyConnection, config: ResolvedConfig) -> OwnerScope:
    """Resolve the identity commands run as, and refuse a mismatch.

    Configuration and database can disagree — a restored backup against a stale
    config is the obvious way. Continuing would silently operate as a user that
    does not exist, producing an empty warehouse rather than an error, so this
    refuses instead.
    """
    in_database = existing_scope(conn)
    if in_database is None:
        raise DatabaseError(
            "the warehouse has no local user.",
            remediation="Run `bet init` to create the local tenant and user.",
        )

    configured_tenant = config.settings.tenant_id
    configured_user = config.settings.user_id
    if configured_user is None or configured_tenant is None:
        return in_database

    configured = OwnerScope(tenant_id=configured_tenant, user_id=configured_user)
    if configured != in_database:
        raise DatabaseError(
            "the configured identity does not exist in this warehouse.",
            remediation=(
                f"Config names user {configured.user_id}, but the warehouse holds "
                f"{in_database.user_id}.\n"
                "This usually means a restored backup against a stale config file.\n"
                f"Remove tenant_id and user_id from {config.config_path} and run "
                "`bet init` to re-record them."
            ),
        )
    return in_database
