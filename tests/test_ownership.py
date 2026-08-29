"""The ownership chain: models, schema constraints, and local bootstrap.

The database enforcement tests matter most. OWNERSHIP.md claims cross-owner rows
are structurally impossible rather than merely discouraged; these are what make
that claim true, and what would notice if a future migration dropped a
constraint.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from bet.config import Settings, resolve
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.errors import DatabaseError
from bet.models.ownership import (
    OwnerScope,
    Sportsbook,
    SportsbookAccount,
    Tenant,
    User,
)

SPORTSBOOKS = {
    "fanduel",
    "draftkings",
    "betmgm",
    "fanatics",
    "caesars",
    "bally_bet",
    "thescore_bet",
}


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    data = tmp_path / "data"
    data.mkdir()
    with connect(Settings(data_dir=data)) as conn:
        migrator.apply(conn)
        yield conn


@pytest.fixture
def seeded(warehouse: duckdb.DuckDBPyConnection) -> tuple[str, str]:
    tenant, user = str(uuid.uuid4()), str(uuid.uuid4())
    warehouse.execute("INSERT INTO core.tenant (id, name) VALUES (?, ?)", [tenant, "t"])
    warehouse.execute(
        "INSERT INTO core.user (tenant_id, id, display_name) VALUES (?, ?, ?)",
        [tenant, user, "tom"],
    )
    return tenant, user


# --------------------------------------------------------------- schema shape


def test_every_supported_sportsbook_is_seeded(warehouse: duckdb.DuckDBPyConnection) -> None:
    found = {str(r[0]) for r in warehouse.execute("SELECT code FROM core.sportsbook").fetchall()}
    assert found == SPORTSBOOKS


def test_export_capabilities_are_unknown_not_false(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """NULL means 'not yet established'. Recording a guess would look like a fact."""
    row = warehouse.execute(
        "SELECT exports_csv, exports_pdf, has_api FROM core.sportsbook WHERE code = 'fanduel'"
    ).fetchone()
    assert row == (None, None, None)


def test_ids_default_to_uuidv7(warehouse: duckdb.DuckDBPyConnection) -> None:
    warehouse.execute("INSERT INTO core.tenant (name) VALUES ('generated')")
    row = warehouse.execute("SELECT id FROM core.tenant WHERE name = 'generated'").fetchone()
    assert row is not None
    # uuidv7 encodes its version in the third group's leading nibble.
    assert str(row[0])[14] == "7"


# ------------------------------------------------- database-enforced ownership


def test_an_account_cannot_reference_a_nonexistent_user(
    warehouse: duckdb.DuckDBPyConnection, seeded: tuple[str, str]
) -> None:
    tenant, _ = seeded
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO core.sportsbook_account "
            "(tenant_id, user_id, sportsbook_code, label) VALUES (?, ?, ?, ?)",
            [tenant, str(uuid.uuid4()), "fanduel", "ghost"],
        )


def test_an_account_cannot_cross_tenants(
    warehouse: duckdb.DuckDBPyConnection, seeded: tuple[str, str]
) -> None:
    """A real user id, but quoted under someone else's tenant."""
    _, user = seeded
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO core.sportsbook_account "
            "(tenant_id, user_id, sportsbook_code, label) VALUES (?, ?, ?, ?)",
            [str(uuid.uuid4()), user, "fanduel", "crosstenant"],
        )


def test_an_account_cannot_name_an_unknown_sportsbook(
    warehouse: duckdb.DuckDBPyConnection, seeded: tuple[str, str]
) -> None:
    tenant, user = seeded
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO core.sportsbook_account "
            "(tenant_id, user_id, sportsbook_code, label) VALUES (?, ?, ?, ?)",
            [tenant, user, "bookie_bob", "x"],
        )


def test_a_user_cannot_belong_to_a_nonexistent_tenant(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO core.user (tenant_id, id, display_name) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), str(uuid.uuid4()), "orphan"],
        )


def test_an_unknown_user_status_is_rejected(
    warehouse: duckdb.DuckDBPyConnection, seeded: tuple[str, str]
) -> None:
    tenant, _ = seeded
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO core.user (tenant_id, id, display_name, status) VALUES (?, ?, ?, ?)",
            [tenant, str(uuid.uuid4()), "x", "banana"],
        )


def test_a_valid_account_is_accepted(
    warehouse: duckdb.DuckDBPyConnection, seeded: tuple[str, str]
) -> None:
    tenant, user = seeded
    warehouse.execute(
        "INSERT INTO core.sportsbook_account "
        "(tenant_id, user_id, sportsbook_code, label) VALUES (?, ?, ?, ?)",
        [tenant, user, "fanduel", "main"],
    )
    row = warehouse.execute("SELECT count(*) FROM core.sportsbook_account").fetchone()
    assert row is not None
    assert row[0] == 1


# --------------------------------------------------------------------- models


def test_owned_models_require_both_ownership_columns() -> None:
    """Nullable ownership is prohibited; the model must not permit it either."""
    with pytest.raises(ValidationError):
        SportsbookAccount(  # type: ignore[call-arg]
            id=uuid.uuid4(), sportsbook_code="fanduel", label="main"
        )


def test_naive_timestamps_are_rejected() -> None:
    """Silently assuming UTC is how a bet lands on the wrong day."""
    with pytest.raises(ValidationError):
        Tenant(id=uuid.uuid4(), name="t", created_at=datetime(2026, 1, 1))


def test_aware_timestamps_are_accepted() -> None:
    tenant = Tenant(id=uuid.uuid4(), name="t", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert tenant.created_at.tzinfo is not None


def test_an_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        User(tenant_id=uuid.uuid4(), id=uuid.uuid4(), display_name="t", timezone="Mars/Olympus")


def test_a_real_timezone_is_accepted() -> None:
    user = User(
        tenant_id=uuid.uuid4(), id=uuid.uuid4(), display_name="t", timezone="America/New_York"
    )
    assert user.timezone == "America/New_York"


def test_sportsbook_codes_are_constrained_to_a_slug() -> None:
    with pytest.raises(ValidationError):
        Sportsbook(code="FanDuel!", name="FanDuel")


def test_owner_scope_is_frozen() -> None:
    """It must not be mutated mid-command into someone else's scope."""
    scope = OwnerScope(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        scope.user_id = uuid.uuid4()


# ------------------------------------------------------------------ bootstrap


def test_bootstrap_creates_one_tenant_and_one_user(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    scope = identity.bootstrap(warehouse)
    counts = warehouse.execute(
        "SELECT (SELECT count(*) FROM core.tenant), (SELECT count(*) FROM core.user)"
    ).fetchone()
    assert counts == (1, 1)
    assert scope.tenant_id is not None


def test_bootstrap_is_idempotent(warehouse: duckdb.DuckDBPyConnection) -> None:
    """A second user would own bets invisible to the first."""
    first = identity.bootstrap(warehouse)
    second = identity.bootstrap(warehouse)
    assert first == second

    counts = warehouse.execute("SELECT count(*) FROM core.user").fetchone()
    assert counts is not None
    assert counts[0] == 1


def test_bootstrap_generates_ids_rather_than_using_constants(
    tmp_path: Path,
) -> None:
    """Two installs must not collide, since merging is what these columns permit."""
    scopes = []
    for name in ("a", "b"):
        data = tmp_path / name
        data.mkdir()
        with connect(Settings(data_dir=data)) as conn:
            migrator.apply(conn)
            scopes.append(identity.bootstrap(conn))
    assert scopes[0].tenant_id != scopes[1].tenant_id
    assert scopes[0].user_id != scopes[1].user_id


def test_resolve_scope_refuses_an_unbootstrapped_warehouse(
    warehouse: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    config = resolve(config_path=tmp_path / "missing.toml", env={"BET_DATA_DIR": str(tmp_path)})
    with pytest.raises(DatabaseError) as caught:
        identity.resolve_scope(warehouse, config)
    assert "bet init" in (caught.value.remediation or "")


def test_resolve_scope_refuses_a_config_naming_a_different_user(
    warehouse: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A restored backup against a stale config would otherwise return nothing."""
    identity.bootstrap(warehouse)
    stranger = uuid.uuid4()
    config = resolve(
        config_path=tmp_path / "missing.toml",
        env={
            "BET_DATA_DIR": str(tmp_path),
            "BET_TENANT_ID": str(uuid.uuid4()),
            "BET_USER_ID": str(stranger),
        },
    )
    with pytest.raises(DatabaseError) as caught:
        identity.resolve_scope(warehouse, config)
    assert "does not exist in this warehouse" in caught.value.message


def test_resolve_scope_accepts_a_matching_config(
    warehouse: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    scope = identity.bootstrap(warehouse)
    config = resolve(
        config_path=tmp_path / "missing.toml",
        env={
            "BET_DATA_DIR": str(tmp_path),
            "BET_TENANT_ID": str(scope.tenant_id),
            "BET_USER_ID": str(scope.user_id),
        },
    )
    assert identity.resolve_scope(warehouse, config) == scope


def test_resolve_scope_falls_back_to_the_database_when_config_is_silent(
    warehouse: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    scope = identity.bootstrap(warehouse)
    config = resolve(config_path=tmp_path / "missing.toml", env={"BET_DATA_DIR": str(tmp_path)})
    assert identity.resolve_scope(warehouse, config) == scope
