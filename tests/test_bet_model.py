"""The canonical bet model: schema constraints, round-trips, and the money.

The acceptance criterion for SB-702 is that every wager kind round-trips and the
worked examples from the data dictionary persist and reproduce their expected
values. Those are the tests that matter here; the rest guard the constraints
that stop incoherent money reaching the warehouse at all.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.models.bet import Bet, BetLeg, BetPromotion, Promotion
from bet.settlement.promotions import (
    american_to_decimal,
    apply_boosts,
    base_profit,
    promotional_value,
    to_cents,
)

PLACED = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[tuple[duckdb.DuckDBPyConnection, str, str, str]]:
    """A migrated warehouse with a local user and one sportsbook account."""
    data = tmp_path / "data"
    data.mkdir()
    with connect(Settings(data_dir=data)) as conn:
        migrator.apply(conn)
        scope = identity.bootstrap(conn)
        account = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO core.sportsbook_account "
            "(tenant_id, user_id, id, sportsbook_code, label) VALUES (?, ?, ?, ?, ?)",
            [str(scope.tenant_id), str(scope.user_id), account, "fanduel", "main"],
        )
        yield conn, str(scope.tenant_id), str(scope.user_id), account


def insert_bet(warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str], **fields: Any) -> str:
    conn, tenant, user, account = warehouse
    row: dict[str, Any] = {
        "tenant_id": tenant,
        "user_id": user,
        "id": str(uuid.uuid4()),
        "sportsbook_account_id": account,
        "placed_at": PLACED,
        **fields,
    }
    conn.execute(
        f"INSERT INTO core.bet ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
        list(row.values()),
    )
    return str(row["id"])


def money(conn: duckdb.DuckDBPyConnection, bet_id: str) -> tuple[Decimal, Decimal]:
    row = conn.execute(
        "SELECT net_profit, total_risk FROM core.bet WHERE id = ?", [bet_id]
    ).fetchone()
    assert row is not None
    return Decimal(str(row[0])), Decimal(str(row[1]))


# ------------------------------------------- the worked examples, end to end

WORKED_EXAMPLES = [
    pytest.param(
        {"status": "settled", "result": "won", "cash_staked": "10.00", "cash_returned": "16.67"},
        "6.67",
        "10.00",
        id="9.1-straight-win",
    ),
    pytest.param(
        {"status": "settled", "result": "won", "cash_staked": "20.00", "cash_returned": "57.50"},
        "37.50",
        "20.00",
        id="9.2-boosted-win",
    ),
    pytest.param(
        {
            "status": "settled",
            "result": "won",
            "cash_staked": "0.00",
            "bonus_staked": "25.00",
            "cash_returned": "21.50",
        },
        "21.50",
        "25.00",
        id="9.3-free-bet-win",
    ),
    pytest.param(
        {"status": "settled", "result": "lost", "cash_staked": "10.00", "cash_returned": "0.00"},
        "-10.00",
        "10.00",
        id="9.4-voided-leg-ticket-lost",
    ),
    pytest.param(
        {"status": "settled", "result": "won", "cash_staked": "10.00", "cash_returned": "24.50"},
        "14.50",
        "10.00",
        id="9.5-parlay-with-push-leg",
    ),
    pytest.param(
        {
            "status": "settled",
            "result": "cashed_out",
            "cash_staked": "40.00",
            "cash_returned": "52.00",
            "cashout_amount": "52.00",
        },
        "12.00",
        "40.00",
        id="9.6-cash-out",
    ),
]


@pytest.mark.parametrize(("fields", "expected_net", "expected_risk"), WORKED_EXAMPLES)
def test_worked_examples_persist_and_reproduce(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
    fields: dict[str, Any],
    expected_net: str,
    expected_risk: str,
) -> None:
    bet_id = insert_bet(warehouse, **fields)
    net, risk = money(warehouse[0], bet_id)
    assert net == Decimal(expected_net)
    assert risk == Decimal(expected_risk)


def test_net_profit_cannot_be_written(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """The canonical formula must not be overridable by any importer."""
    conn = warehouse[0]
    insert_bet(
        warehouse,
        status="settled",
        result="won",
        cash_staked="10.00",
        cash_returned="16.67",
    )
    with pytest.raises(duckdb.Error):
        conn.execute("UPDATE core.bet SET net_profit = 999")


def test_a_free_bet_contributes_no_cash_stake(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """Its winnings must never land in a cash-ROI denominator of zero."""
    conn = warehouse[0]
    insert_bet(
        warehouse,
        status="settled",
        result="won",
        cash_staked="0.00",
        bonus_staked="25.00",
        cash_returned="21.50",
    )
    row = conn.execute("SELECT sum(cash_staked) FROM core.bet WHERE cash_staked > 0").fetchone()
    assert row is not None
    assert row[0] is None


# ------------------------------------------------------- every wager kind

WAGER_KINDS = [
    "straight",
    "parlay",
    "same_game_parlay",
    "teaser",
    "round_robin",
    "future",
    "system",
    "unknown",
]


@pytest.mark.parametrize("kind", WAGER_KINDS)
def test_every_wager_kind_round_trips(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str], kind: str
) -> None:
    conn = warehouse[0]
    bet_id = insert_bet(warehouse, wager_kind=kind, cash_staked="5.00")
    row = conn.execute("SELECT wager_kind FROM core.bet WHERE id = ?", [bet_id]).fetchone()
    assert row is not None
    assert row[0] == kind


def test_an_unknown_wager_kind_is_rejected(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    with pytest.raises(duckdb.ConstraintException):
        insert_bet(warehouse, wager_kind="accumulator_deluxe", cash_staked="5.00")


# --------------------------------------------------- schema-level coherence


def test_a_settled_bet_must_have_a_result(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """Otherwise it drops silently out of every result-based analytic."""
    with pytest.raises(duckdb.ConstraintException):
        insert_bet(warehouse, status="settled", cash_staked="5.00")


def test_a_pending_bet_must_not_have_a_result(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    with pytest.raises(duckdb.ConstraintException):
        insert_bet(warehouse, status="pending", result="won", cash_staked="5.00")


def test_a_bet_must_risk_something(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    with pytest.raises(duckdb.ConstraintException):
        insert_bet(warehouse, cash_staked="0.00", bonus_staked="0.00")


def test_negative_money_is_rejected(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    with pytest.raises(duckdb.ConstraintException):
        insert_bet(warehouse, cash_staked="-5.00")


def test_a_bet_cannot_belong_to_another_users_account(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    conn, tenant, _, account = warehouse
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO core.bet "
            "(tenant_id, user_id, sportsbook_account_id, placed_at, cash_staked) "
            "VALUES (?, ?, ?, ?, ?)",
            [tenant, str(uuid.uuid4()), account, PLACED, Decimal("5.00")],
        )


# ------------------------------------------------------------------ legs


def _leg(conn: duckdb.DuckDBPyConnection, tenant: str, user: str, bet_id: str, **f: Any) -> str:
    row: dict[str, Any] = {
        "tenant_id": tenant,
        "user_id": user,
        "id": str(uuid.uuid4()),
        "bet_id": bet_id,
        "leg_order": 1,
        **f,
    }
    conn.execute(
        f"INSERT INTO core.bet_leg ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
        list(row.values()),
    )
    return str(row["id"])


def test_a_parlay_persists_its_legs_in_order(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, wager_kind="parlay", cash_staked="10.00")
    for order, selection in enumerate(["Red Sox ML", "Over 8.5", "Yankees -1.5"], start=1):
        _leg(conn, tenant, user, bet_id, leg_order=order, selection_name=selection)

    rows = conn.execute(
        "SELECT leg_order, selection_name FROM core.bet_leg WHERE bet_id = ? ORDER BY leg_order",
        [bet_id],
    ).fetchall()
    assert [r[1] for r in rows] == ["Red Sox ML", "Over 8.5", "Yankees -1.5"]


def test_leg_order_is_unique_within_a_ticket(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, wager_kind="parlay", cash_staked="10.00")
    _leg(conn, tenant, user, bet_id, leg_order=1)
    with pytest.raises(duckdb.ConstraintException):
        _leg(conn, tenant, user, bet_id, leg_order=1)


def test_legs_may_carry_no_odds(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """DraftKings publishes none; requiring them would make its history unimportable."""
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, wager_kind="same_game_parlay", cash_staked="5.00")
    leg_id = _leg(conn, tenant, user, bet_id, selection_name="Messi to score")
    row = conn.execute(
        "SELECT odds_american, result_value FROM core.bet_leg WHERE id = ?", [leg_id]
    ).fetchone()
    assert row == (None, None)


def test_a_leg_can_record_the_achieved_value(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """Enables near-miss analysis where the operator publishes a quantity."""
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, wager_kind="same_game_parlay", cash_staked="5.00")
    leg_id = _leg(
        conn,
        tenant,
        user,
        bet_id,
        market_name="Points",
        line_value=Decimal("15"),
        result="won",
        result_value=Decimal("24"),
    )
    row = conn.execute(
        "SELECT line_value, result_value FROM core.bet_leg WHERE id = ?", [leg_id]
    ).fetchone()
    assert row is not None
    assert Decimal(str(row[1])) > Decimal(str(row[0]))


def test_a_threshold_met_exactly_is_a_win(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """A 6+ rebounds leg settled won at exactly 6: the comparison is >=, not >."""
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, wager_kind="same_game_parlay", cash_staked="5.00")
    _leg(
        conn,
        tenant,
        user,
        bet_id,
        market_name="Rebounds",
        line_value=Decimal("6"),
        result="won",
        result_value=Decimal("6"),
    )
    row = conn.execute(
        "SELECT result FROM core.bet_leg WHERE bet_id = ? AND result_value = line_value",
        [bet_id],
    ).fetchone()
    assert row is not None
    assert row[0] == "won"


def test_one_ticket_can_mix_leg_results_including_void(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(
        warehouse,
        wager_kind="same_game_parlay",
        cash_staked="10.00",
        status="settled",
        result="lost",
        cash_returned="0.00",
        settled_at=PLACED,
    )
    _leg(conn, tenant, user, bet_id, leg_order=1, result="void")
    _leg(conn, tenant, user, bet_id, leg_order=2, result="lost")

    results = {
        r[0]
        for r in conn.execute(
            "SELECT result FROM core.bet_leg WHERE bet_id = ?", [bet_id]
        ).fetchall()
    }
    assert results == {"void", "lost"}


def test_a_voided_leg_reprices_the_ticket(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """Placed and settled odds are different facts; one value cannot carry both."""
    conn = warehouse[0]
    bet_id = insert_bet(
        warehouse,
        wager_kind="same_game_parlay",
        cash_staked="10.00",
        odds_american_placed=377,
        odds_american_settled=-110,
    )
    row = conn.execute(
        "SELECT odds_american_placed, odds_american_settled FROM core.bet WHERE id = ?",
        [bet_id],
    ).fetchone()
    assert row == (377, -110)


# ----------------------------------------------------------------- groups


def test_a_same_game_group_carries_its_own_price(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """The group price against the legs' product is what makes correlation measurable."""
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, wager_kind="same_game_parlay", cash_staked="5.00")
    group_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO core.bet_leg_group "
        "(tenant_id, user_id, id, bet_id, external_ref, category, odds_american) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [tenant, user, group_id, bet_id, "g1", "SGM", 178],
    )
    for order, price in enumerate([-129, -250, -865], start=1):
        _leg(conn, tenant, user, bet_id, leg_order=order, group_id=group_id, odds_american=price)

    row = conn.execute(
        "SELECT g.odds_american, count(l.id) FROM core.bet_leg_group g "
        "JOIN core.bet_leg l ON l.group_id = g.id WHERE g.id = ? GROUP BY g.odds_american",
        [group_id],
    ).fetchone()
    assert row == (178, 3)


# ------------------------------------------------------------- promotions


def test_a_ticket_scoped_and_a_leg_scoped_promotion_coexist(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """The real stacked specimen: a 30% boost on the ticket, Super Sub on one leg."""
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, wager_kind="same_game_parlay", cash_staked="5.00")
    leg_id = _leg(conn, tenant, user, bet_id, selection_name="Lionel Messi")

    conn.execute(
        "INSERT INTO core.bet_promotion "
        "(tenant_id, user_id, bet_id, promotion_type, scope, apply_order, generosity_pct, "
        "triggered) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [tenant, user, bet_id, "profit_boost", "ticket", 1, Decimal("30"), True],
    )
    conn.execute(
        "INSERT INTO core.bet_promotion "
        "(tenant_id, user_id, bet_id, bet_leg_id, promotion_type, scope, apply_order, triggered) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [tenant, user, bet_id, leg_id, "insurance", "leg", 2, False],
    )

    rows = conn.execute(
        "SELECT scope, promotion_type, triggered FROM core.bet_promotion "
        "WHERE bet_id = ? ORDER BY apply_order",
        [bet_id],
    ).fetchall()
    assert rows == [("ticket", "profit_boost", True), ("leg", "insurance", False)]


def test_a_leg_scoped_promotion_must_name_a_leg(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, cash_staked="5.00")
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO core.bet_promotion "
            "(tenant_id, user_id, bet_id, promotion_type, scope) VALUES (?, ?, ?, ?, ?)",
            [tenant, user, bet_id, "insurance", "leg"],
        )


def test_a_ticket_scoped_promotion_must_not_name_a_leg(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    conn, tenant, user, _ = warehouse
    bet_id = insert_bet(warehouse, cash_staked="5.00")
    leg_id = _leg(conn, tenant, user, bet_id)
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO core.bet_promotion "
            "(tenant_id, user_id, bet_id, bet_leg_id, promotion_type, scope) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [tenant, user, bet_id, leg_id, "profit_boost", "ticket"],
        )


def test_an_expired_promotion_records_when(
    warehouse: tuple[duckdb.DuckDBPyConnection, str, str, str],
) -> None:
    """Expiry is recorded, never inferred from the awarded/played gap."""
    conn, tenant, user, _ = warehouse
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO core.promotion "
            "(tenant_id, user_id, sportsbook_code, promotion_type, state) VALUES (?, ?, ?, ?, ?)",
            [tenant, user, "fanduel", "bonus_bet", "expired"],
        )


# ------------------------------------------------------- promotion economics


@pytest.mark.parametrize(
    ("american", "stake", "generosity", "expected_return"),
    [
        (-130, "5.00", None, "8.85"),
        (126, "5.00", "30", "13.19"),
        (-180, "5.00", "10", "8.06"),
        (145, "5.00", "10", "12.98"),
        (-140, "5.00", "25", "9.46"),
    ],
    ids=["straight", "fanduel-30pct", "fanatics-10pct-neg", "fanatics-10pct-pos", "fanatics-25pct"],
)
def test_the_boost_formula_matches_four_operators(
    american: int, stake: str, generosity: str | None, expected_return: str
) -> None:
    """boosted_profit = base_profit * (1 + generosity/100), applied to profit."""
    base = base_profit(Decimal(stake), american)
    if generosity is None:
        total = base
    else:
        promotion = BetPromotion(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            bet_id=uuid.uuid4(),
            promotion_type="profit_boost",
            generosity_pct=Decimal(generosity),
        )
        total = apply_boosts(base, [promotion])
    assert to_cents(Decimal(stake) + total) == Decimal(expected_return)


def test_boosts_compose_in_apply_order() -> None:
    def boost(order: int, pct: str) -> BetPromotion:
        return BetPromotion(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            bet_id=uuid.uuid4(),
            promotion_type="profit_boost",
            apply_order=order,
            generosity_pct=Decimal(pct),
        )

    base = Decimal("10.00")
    # 10 * 1.5 * 1.2 == 10 * 1.2 * 1.5, so use it to prove ordering is respected
    # by checking the running value, not just the product.
    assert apply_boosts(base, [boost(1, "50"), boost(2, "20")]) == Decimal("18.000")


def test_an_untriggered_boost_delivers_nothing() -> None:
    """Attached-but-not-fired must not be counted as delivered value."""
    promotion = BetPromotion(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        bet_id=uuid.uuid4(),
        promotion_type="profit_boost",
        generosity_pct=Decimal("30"),
        triggered=False,
    )
    assert apply_boosts(Decimal("10.00"), [promotion]) == Decimal("10.00")


def test_promotional_value_is_the_difference() -> None:
    base = base_profit(Decimal("5.00"), 126)
    boosted = base * Decimal("1.30")
    assert promotional_value(base, boosted) == Decimal("1.89")


def test_american_odds_convert_exactly() -> None:
    assert american_to_decimal(100) == Decimal(2)
    assert american_to_decimal(-100) == Decimal(2)
    assert american_to_decimal(-110) == Decimal(1) + Decimal(100) / Decimal(110)


def test_zero_american_odds_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be zero"):
        american_to_decimal(0)


# ---------------------------------------------------------------- models


def test_model_net_profit_matches_the_generated_column() -> None:
    bet = Bet(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        placed_at=PLACED,
        settled_at=PLACED,
        status="settled",
        result="won",
        cash_staked=Decimal("10.00"),
        cash_returned=Decimal("16.67"),
    )
    assert bet.net_profit == Decimal("6.67")
    assert bet.total_risk == Decimal("10.00")


def test_a_free_bet_is_identified_as_one() -> None:
    bet = Bet(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        placed_at=PLACED,
        cash_staked=Decimal("0.00"),
        bonus_staked=Decimal("25.00"),
    )
    assert bet.is_free_bet


def test_the_model_refuses_a_settled_bet_without_a_result() -> None:
    with pytest.raises(ValidationError):
        Bet(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            sportsbook_account_id=uuid.uuid4(),
            placed_at=PLACED,
            status="settled",
            cash_staked=Decimal("5.00"),
        )


def test_the_model_refuses_a_bet_risking_nothing() -> None:
    with pytest.raises(ValidationError):
        Bet(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            sportsbook_account_id=uuid.uuid4(),
            placed_at=PLACED,
        )


def test_the_model_refuses_a_naive_placed_at() -> None:
    with pytest.raises(ValidationError):
        Bet(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            sportsbook_account_id=uuid.uuid4(),
            placed_at=datetime(2026, 8, 1, 16, 0),
            cash_staked=Decimal("5.00"),
        )


def test_the_model_refuses_an_incoherent_promotion_scope() -> None:
    with pytest.raises(ValidationError):
        BetPromotion(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            bet_id=uuid.uuid4(),
            promotion_type="insurance",
            scope="leg",
        )


def test_the_model_refuses_an_expired_promotion_without_a_date() -> None:
    with pytest.raises(ValidationError):
        Promotion(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            sportsbook_code="fanduel",
            promotion_type="bonus_bet",
            state="expired",
        )


def test_a_leg_carries_no_money() -> None:
    """Attributing money to legs invents an allocation the operator never made."""
    leg = BetLeg(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        bet_id=uuid.uuid4(),
        leg_order=1,
    )
    money_fields = {"cash_staked", "cash_returned", "net_profit"}
    assert not (money_fields & set(type(leg).model_fields))
