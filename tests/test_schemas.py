"""The shipped migrations: namespaces, taxonomies, and their semantics.

These tests pin the reference vocabularies to what docs/DATA_DICTIONARY.md says.
If someone changes a taxonomy without changing the dictionary — or the reverse —
this suite fails, which is the point: the ROI rules live in the database now, so
the database is where drift has to be caught.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from bet.config import Settings
from bet.database import migrator
from bet.database.connection import connect

EXPECTED_SCHEMAS = {"analytics", "control", "core", "raw", "reference"}

# Namespaces belonging to epics that have not been designed. Creating them early
# would imply a schema nobody has agreed.
DEFERRED_SCHEMAS = {"sports", "market", "agent"}


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A fully migrated warehouse built from the real migration files."""
    data = tmp_path / "data"
    data.mkdir()
    settings = Settings(data_dir=data)
    with connect(settings) as conn:
        migrator.apply(conn)
        yield conn


def scalar(conn: duckdb.DuckDBPyConnection, sql: str) -> object:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def codes(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(f"SELECT code FROM reference.{table} ORDER BY sort_order").fetchall()
    ]


# ----------------------------------------------------------------- namespaces


def test_every_planned_schema_exists(warehouse: duckdb.DuckDBPyConnection) -> None:
    found = {
        str(r[0])
        for r in warehouse.execute(
            "SELECT schema_name FROM duckdb_schemas() WHERE NOT internal"
        ).fetchall()
    }
    assert found >= EXPECTED_SCHEMAS


def test_deferred_schemas_are_not_created(warehouse: duckdb.DuckDBPyConnection) -> None:
    found = {
        str(r[0])
        for r in warehouse.execute(
            "SELECT schema_name FROM duckdb_schemas() WHERE NOT internal"
        ).fetchall()
    }
    assert not (DEFERRED_SCHEMAS & found)


def test_migrations_are_idempotent(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert migrator.apply(warehouse) == []
    assert migrator.current_version(warehouse) == 2


def test_the_shipped_migrations_pass_their_own_integrity_check(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Guards against a shipped migration being edited after release."""
    migrator.verify(warehouse)


# ---------------------------------------------------------------- vocabularies


def test_bet_status_vocabulary(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert codes(warehouse, "bet_status") == ["pending", "settled"]


def test_bet_result_vocabulary(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert codes(warehouse, "bet_result") == [
        "won",
        "lost",
        "push",
        "void",
        "partial",
        "cashed_out",
    ]


def test_wager_kind_vocabulary(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert codes(warehouse, "wager_kind") == [
        "straight",
        "parlay",
        "same_game_parlay",
        "teaser",
        "round_robin",
        "future",
        "system",
        "unknown",
    ]


def test_promotion_state_vocabulary(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert codes(warehouse, "promotion_state") == ["awarded", "played", "expired"]


def test_settlement_source_vocabulary(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert codes(warehouse, "settlement_source") == ["amount", "status_word", "both_agree"]


def test_market_taxonomy_is_not_invented_here(warehouse: duckdb.DuckDBPyConnection) -> None:
    """Market families and selection types belong to SB-768, not to this migration."""
    tables = {
        str(r[0])
        for r in warehouse.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'reference'"
        ).fetchall()
    }
    assert "market_family" not in tables
    assert "selection_type" not in tables


# ------------------------------------------------------------------- semantics


def test_only_void_is_excluded_from_the_roi_denominator(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """DATA_DICTIONARY section 4.3, and the one rule most easily got backwards.

    A push committed capital and tied, so it belongs in the denominator. A void
    never put capital at risk, so it belongs in nothing. Reversing these inflates
    every reported figure.
    """
    excluded = [
        str(r[0])
        for r in warehouse.execute(
            "SELECT code FROM reference.bet_result WHERE NOT counts_in_roi_denominator"
        ).fetchall()
    ]
    assert excluded == ["void"]


def test_push_counts_in_the_denominator_but_not_the_win_rate(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    row = warehouse.execute(
        "SELECT counts_in_roi_denominator, win_rate_treatment "
        "FROM reference.bet_result WHERE code = 'push'"
    ).fetchone()
    assert row == (True, "excluded")


def test_a_profit_boost_applies_to_profit_not_stake(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    """Getting this wrong is a silent money bug."""
    assert (
        scalar(
            warehouse,
            "SELECT applies_to FROM reference.promotion_type WHERE code = 'profit_boost'",
        )
        == "profit"
    )


def test_a_bonus_bet_is_excluded_from_cash_roi_but_not_economic_roi(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    row = warehouse.execute(
        "SELECT in_cash_roi, in_economic_roi FROM reference.promotion_type WHERE code = 'bonus_bet'"
    ).fetchone()
    assert row == (False, True)


def test_rewards_currency_is_economic_only(warehouse: duckdb.DuckDBPyConnection) -> None:
    """It accrues on losing bets too, so it cannot touch cash ROI."""
    row = warehouse.execute(
        "SELECT applies_to, in_cash_roi, in_economic_roi "
        "FROM reference.promotion_type WHERE code = 'rewards_currency'"
    ).fetchone()
    assert row == ("none", False, True)


def test_screenshot_capture_is_the_least_trusted(warehouse: duckdb.DuckDBPyConnection) -> None:
    assert (
        scalar(
            warehouse,
            "SELECT trust_level FROM reference.capture_method WHERE code = 'screenshot'",
        )
        == "low"
    )


def test_manual_entry_is_a_known_capture_method(warehouse: duckdb.DuckDBPyConnection) -> None:
    """`bet add` (SB-811) records manual entries against this code."""
    assert "manual" in codes(warehouse, "capture_method")


def test_multi_leg_wager_kinds_are_marked(warehouse: duckdb.DuckDBPyConnection) -> None:
    multi = {
        str(r[0])
        for r in warehouse.execute(
            "SELECT code FROM reference.wager_kind WHERE is_multi_leg"
        ).fetchall()
    }
    assert multi == {"parlay", "same_game_parlay", "teaser", "round_robin", "system"}


# ------------------------------------------------------------------ constraints


def test_an_unknown_win_rate_treatment_is_rejected(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO reference.bet_result VALUES "
            "('nonsense', 'Nonsense', TRUE, 'maybe', 'invalid', 99)"
        )


def test_an_unknown_trust_level_is_rejected(warehouse: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO reference.capture_method VALUES "
            "('telepathy', 'Telepathy', 'absolute', 'invalid', 99)"
        )


def test_duplicate_taxonomy_codes_are_rejected(warehouse: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(duckdb.ConstraintException):
        warehouse.execute(
            "INSERT INTO reference.bet_status VALUES ('pending', 'Again', 'duplicate', 9)"
        )
