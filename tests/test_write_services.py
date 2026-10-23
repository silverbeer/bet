"""Write-path services (SB-1395): called directly, against a temp warehouse.

The CLI tests cover what each command accepts and prints; these cover what
each service writes and refuses with no Typer in the way, as a future HTTP
layer would call them -- including that one user can never write against, or
cite, another's rows.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import NotFoundError, UsageError
from bet.models.bet import Bet
from bet.models.ownership import OwnerScope, SportsbookAccount
from bet.services.add import AddBetRequest, add_bet
from bet.services.bootstrap import bootstrap_warehouse
from bet.services.capture import UnknownLabelError, check_money, require_not_future
from bet.services.ledger import (
    BalanceSnapshotRequest,
    CashFlowRequest,
    add_balance_snapshot,
    record_cash_flow,
)
from bet.services.settle import SettleRequest, settle_bet
from bet.services.sources import SourceToArchive, archive_source
from bet.services.specs import (
    leg_input,
    parse_leg_spec,
    parse_money,
    parse_triggered,
    promotion_input,
)
from bet.services.tickets import LegGroupInput, LegInput, PromotionInput

PLACED = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n synthetic slip"


# ------------------------------------------------------------- fixtures


@pytest.fixture
def two_users(tmp_path: Path) -> Iterator[tuple[duckdb.DuckDBPyConnection, Warehouse, Warehouse]]:
    with connect(Settings(data_dir=tmp_path)) as conn:
        migrator.apply(conn)
        scopes = []
        for name in ("alice", "bob"):
            tenant, user = uuid.uuid4(), uuid.uuid4()
            conn.execute("INSERT INTO core.tenant (id, name) VALUES (?, ?)", [str(tenant), name])
            conn.execute(
                "INSERT INTO core.user (tenant_id, id, display_name) VALUES (?, ?, ?)",
                [str(tenant), str(user), name],
            )
            scopes.append(OwnerScope(tenant_id=tenant, user_id=user))
        yield conn, Warehouse(conn, scopes[0]), Warehouse(conn, scopes[1])


@pytest.fixture
def w(two_users: Any) -> Warehouse:
    alice: Warehouse = two_users[1]
    return alice


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    return tmp_path / "sources"


@pytest.fixture
def slip(tmp_path: Path) -> Path:
    path = tmp_path / "IMG_0001.PNG"
    path.write_bytes(PNG)
    return path


def _account(w: Warehouse, code: str = "fanduel", label: str | None = None) -> SportsbookAccount:
    return w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code=code,
            label=label or code,
        )
    )


def _request(**fields: Any) -> AddBetRequest:
    defaults: dict[str, Any] = {
        "sportsbook": "fanduel",
        "legs": [LegInput(sport="NFL", selection_name="Chiefs ML", odds_american=150)],
        "cash_staked": Decimal("10.00"),
        "placed_at": PLACED,
    }
    return AddBetRequest(**(defaults | fields))


def _source(archive_dir: Path, **fields: Any) -> SourceToArchive:
    return SourceToArchive(archive_dir=archive_dir, **fields)


# ------------------------------------------------------------------- add


class TestAddBet:
    def test_writes_the_ticket_and_creates_the_account(self, w: Warehouse) -> None:
        added = add_bet(w, _request())
        assert added.account_is_new
        assert added.account.label == "fanduel"
        bet = w.bets.get(added.bet.id)
        assert (bet.wager_kind, bet.odds_american_placed) == ("straight", 150)
        assert bet.odds_decimal_placed == Decimal("2.5000")
        assert bet.capture_method == "manual" and bet.source_file_id is None
        assert [leg.selection_name for leg in w.legs.for_bet(bet.id)] == ["Chiefs ML"]

    def test_reuses_the_one_existing_account(self, w: Warehouse) -> None:
        account = _account(w)
        added = add_bet(w, _request(sportsbook="  FanDuel "))
        assert not added.account_is_new
        assert added.bet.sportsbook_account_id == account.id

    def test_placed_at_defaults_to_now(self, w: Warehouse) -> None:
        before = datetime.now(UTC)
        added = add_bet(w, _request(placed_at=None))
        assert added.bet.placed_at >= before

    def test_parlay_is_priced_by_its_legs(self, w: Warehouse) -> None:
        legs = [LegInput(odds_american=100), LegInput(odds_american=100)]
        added = add_bet(w, _request(legs=legs))
        assert (added.bet.wager_kind, added.bet.odds_american_placed) == ("parlay", 300)

    def test_an_explicit_price_wins(self, w: Warehouse) -> None:
        legs = [LegInput(), LegInput()]
        added = add_bet(w, _request(legs=legs, odds_american=-138, wager_kind="same_game_parlay"))
        assert added.bet.odds_american_placed == -138

    def test_a_leg_group_prices_the_ticket_and_links_its_legs(self, w: Warehouse) -> None:
        legs = [LegInput(odds_american=-110), LegInput(odds_american=-110)]
        added = add_bet(
            w,
            _request(
                legs=legs,
                leg_group=LegGroupInput(odds_american=200, category="SGP"),
                wager_kind="same_game_parlay",
            ),
        )
        (group,) = w.leg_groups.for_bet(added.bet.id)
        assert (group.odds_american, group.category) == (200, "SGP")
        assert added.bet.odds_decimal_placed == Decimal("3.0000")
        assert {leg.group_id for leg in w.legs.for_bet(added.bet.id)} == {group.id}

    def test_promotions_bind_to_the_named_leg(self, w: Warehouse) -> None:
        legs = [LegInput(odds_american=-110), LegInput(odds_american=-110)]
        promos = [
            PromotionInput(promotion_type="Profit_Boost", generosity_pct=Decimal(50)),
            PromotionInput(promotion_type="insurance", scope="leg", leg=2),
        ]
        added = add_bet(w, _request(legs=legs, promotions=promos))
        stored = w.bet_promotions.for_bet(added.bet.id)
        assert [p.promotion_type for p in stored] == ["profit_boost", "insurance"]
        assert [p.apply_order for p in stored] == [1, 2]
        assert stored[1].bet_leg_id == added.legs[1].id

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"cash_staked": Decimal("0")}, "must risk cash or bonus stake"),
            ({"cash_staked": Decimal("-1")}, "cannot be negative"),
            ({"sportsbook": "nosuchbook"}, "not a known sportsbook"),
            ({"legs": [LegInput(odds_american=50)]}, "not a valid American odds"),
            ({"legs": [LegInput(), LegInput(odds_american=100)]}, "1 of 2 legs have no odds"),
            ({"wager_kind": "parlay"}, "describes a multi-leg ticket"),
            ({"promotions": [PromotionInput(promotion_type="profit_boost")]}, "generosity_pct"),
            (
                {"promotions": [PromotionInput(promotion_type="insurance", scope="leg", leg=3)]},
                "names leg 3",
            ),
        ],
    )
    def test_refusals_write_nothing(
        self, w: Warehouse, fields: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(UsageError, match=message):
            add_bet(w, _request(**fields))
        assert w.bets.count() == 0 and w.accounts.count() == 0

    def test_several_accounts_need_a_label(self, w: Warehouse) -> None:
        _account(w, label="main")
        boosts = _account(w, label="boosts")
        with pytest.raises(UsageError, match="multiple fanduel accounts"):
            add_bet(w, _request())
        added = add_bet(w, _request(account_label="boosts"))
        assert added.bet.sportsbook_account_id == boosts.id

    def test_a_ticket_needs_a_leg(self) -> None:
        with pytest.raises(ValidationError):
            _request(legs=[])

    def test_placed_at_must_carry_a_zone(self) -> None:
        with pytest.raises(ValidationError):
            _request(placed_at=datetime(2026, 10, 18, 16, 0))


class TestAddBetProvenance:
    def test_a_path_is_archived_and_cited(
        self, w: Warehouse, slip: Path, archive_dir: Path
    ) -> None:
        added = add_bet(
            w, _request(capture_method="screenshot"), source=_source(archive_dir, path=slip)
        )
        source = w.sources.get(added.bet.source_file_id)  # type: ignore[arg-type]
        assert (source.original_name, source.media_type) == ("IMG_0001.PNG", "image/png")
        assert (archive_dir / source.archived_path).read_bytes() == PNG

    def test_uploaded_bytes_archive_to_the_same_row_as_the_path(
        self, w: Warehouse, slip: Path, archive_dir: Path
    ) -> None:
        by_path = add_bet(
            w, _request(capture_method="screenshot"), source=_source(archive_dir, path=slip)
        )
        by_upload = add_bet(
            w,
            _request(capture_method="screenshot"),
            source=_source(archive_dir, data=PNG, original_name="upload/slip.png"),
        )
        assert by_upload.bet.source_file_id == by_path.bet.source_file_id
        assert w.sources.count() == 1

    def test_an_upload_keeps_only_its_file_name(self, w: Warehouse, archive_dir: Path) -> None:
        source = archive_source(
            w,
            _source(archive_dir, data=PNG, original_name="../../etc/slip.PNG"),
            capture_method="screenshot",
        )
        assert source.original_name == "slip.PNG"
        assert (archive_dir / source.archived_path).read_bytes() == PNG

    def test_an_archived_source_can_be_cited_by_id(self, w: Warehouse, archive_dir: Path) -> None:
        source = archive_source(
            w, _source(archive_dir, data=PNG, original_name="slip.png"), capture_method="pdf"
        )
        added = add_bet(w, _request(capture_method="pdf", source_file_id=source.id))
        assert added.bet.source_file_id == source.id

    def test_another_users_source_cannot_be_cited(self, two_users: Any, archive_dir: Path) -> None:
        _, alice, bob = two_users
        source = archive_source(
            alice,
            _source(archive_dir, data=PNG, original_name="slip.png"),
            capture_method="screenshot",
        )
        with pytest.raises(NotFoundError):
            add_bet(bob, _request(capture_method="screenshot", source_file_id=source.id))
        assert bob.bets.count() == 0

    def test_a_manual_bet_cannot_cite_a_source(
        self, w: Warehouse, slip: Path, archive_dir: Path
    ) -> None:
        with pytest.raises(UsageError, match="contradicts --capture-method manual"):
            add_bet(w, _request(), source=_source(archive_dir, path=slip))
        with pytest.raises(UsageError, match="contradicts --capture-method manual"):
            add_bet(w, _request(source_file_id=uuid.uuid4()))
        assert w.sources.count() == 0

    def test_a_source_is_archived_or_cited_not_both(
        self, w: Warehouse, slip: Path, archive_dir: Path
    ) -> None:
        with pytest.raises(UsageError, match="not both"):
            add_bet(
                w,
                _request(capture_method="screenshot", source_file_id=uuid.uuid4()),
                source=_source(archive_dir, path=slip),
            )

    def test_a_missing_path_is_refused_before_any_write(
        self, w: Warehouse, tmp_path: Path, archive_dir: Path
    ) -> None:
        with pytest.raises(UsageError, match="is not a file"):
            add_bet(
                w,
                _request(capture_method="screenshot"),
                source=_source(archive_dir, path=tmp_path / "gone.png"),
            )
        assert w.bets.count() == 0

    @pytest.mark.parametrize(
        "fields",
        [{}, {"path": Path("a.png"), "data": PNG}, {"data": PNG}],
        ids=["neither", "both", "upload-without-name"],
    )
    def test_a_source_to_archive_is_one_thing(self, archive_dir: Path, fields: Any) -> None:
        with pytest.raises(ValidationError):
            _source(archive_dir, **fields)


# ---------------------------------------------------------------- settle


def _pending(w: Warehouse, *legs: LegInput, **fields: Any) -> Bet:
    return add_bet(w, _request(legs=list(legs) or [LegInput(odds_american=150)], **fields)).bet


class TestSettleBet:
    def test_a_won_cash_bet_is_computed_and_its_one_leg_follows(self, w: Warehouse) -> None:
        bet = _pending(w)
        settled = settle_bet(w, SettleRequest(bet_id=bet.id, result="Won", settled_at=PLACED))
        stored = w.bets.get(bet.id)
        assert (stored.status, stored.result, stored.cash_returned) == (
            "settled",
            "won",
            Decimal("25.00"),
        )
        assert stored.settled_at == PLACED
        assert [leg.result for leg in w.legs.for_bet(bet.id)] == ["won"]
        assert settled.bet == stored

    def test_a_lost_leg_forces_the_ticket_lost(self, w: Warehouse) -> None:
        bet = _pending(w, LegInput(odds_american=100), LegInput(odds_american=100))
        settled = settle_bet(w, SettleRequest(bet_id=bet.id, leg_results={2: "lost"}))
        assert settled.bet.result == "lost"
        assert [leg.result for leg in settled.legs] == [None, "lost"]
        assert [leg.result for leg in w.legs.for_bet(bet.id)] == [None, "lost"]

    def test_an_override_is_rounded_to_cents(self, w: Warehouse) -> None:
        bet = _pending(w)
        settled = settle_bet(
            w, SettleRequest(bet_id=bet.id, result="won", cash_returned=Decimal("24.995"))
        )
        assert settled.bet.cash_returned == Decimal("25.00")

    def test_re_settling_needs_force(self, w: Warehouse) -> None:
        bet = _pending(w)
        settle_bet(w, SettleRequest(bet_id=bet.id, result="won"))
        with pytest.raises(UsageError, match="already settled"):
            settle_bet(w, SettleRequest(bet_id=bet.id, result="lost"))
        settled = settle_bet(w, SettleRequest(bet_id=bet.id, result="lost", force=True))
        assert settled.bet.cash_returned == Decimal("0.00")

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"result": "cashed"}, "not a known result"),
            ({}, "--result is required"),
            ({"result": "won", "leg_results": {1: "nope"}}, "not a known result"),
            ({"result": "won", "leg_results": {4: "won"}}, r"unknown leg order\(s\)"),
            ({"result": "won", "leg_results": {1: "lost"}}, "cannot settle as anything else"),
            ({"result": "won", "cash_returned": Decimal("-1")}, "cannot be negative"),
            ({"result": "lost", "cash_returned": Decimal("1")}, "not used with a lost result"),
            ({"result": "partial"}, "--return is required"),
        ],
    )
    def test_refusals_leave_the_bet_pending(
        self, w: Warehouse, fields: dict[str, Any], message: str
    ) -> None:
        bet = _pending(w)
        with pytest.raises(UsageError, match=message):
            settle_bet(w, SettleRequest(bet_id=bet.id, **fields))
        assert w.bets.get(bet.id).status == "pending"

    def test_another_users_bet_is_not_found(self, two_users: Any) -> None:
        _, alice, bob = two_users
        bet = _pending(alice)
        with pytest.raises(NotFoundError):
            settle_bet(bob, SettleRequest(bet_id=bet.id, result="won"))
        assert alice.bets.get(bet.id).status == "pending"


# ---------------------------------------------------------------- ledger


class TestBalanceSnapshot:
    def test_appends_and_creates_the_first_account(self, w: Warehouse) -> None:
        recorded = add_balance_snapshot(
            w,
            BalanceSnapshotRequest(
                sportsbook="fanduel",
                cash_balance=Decimal("40.005"),
                bonus_balance=Decimal("5"),
                observed_at=PLACED,
                notes="synthetic",
            ),
        )
        assert recorded.account_is_new
        (stored,) = w.balances.for_account(recorded.account.id)
        assert (stored.cash_balance, stored.bonus_balance) == (Decimal("40.01"), Decimal("5.00"))
        assert (stored.observed_at, stored.notes) == (PLACED, "synthetic")

    def test_labels_match_strictly(self, w: Warehouse) -> None:
        main = _account(w, label="Main")
        _account(w, label="boosts")
        request: dict[str, Any] = {"sportsbook": "fanduel", "cash_balance": Decimal("1")}
        recorded = add_balance_snapshot(
            w, BalanceSnapshotRequest(account_label="  main ", **request)
        )
        assert recorded.account.id == main.id
        with pytest.raises(UnknownLabelError) as caught:
            add_balance_snapshot(w, BalanceSnapshotRequest(account_label="mian", **request))
        assert sorted(caught.value.known_labels) == ["Main", "boosts"]
        assert caught.value.label == "mian"

    def test_a_future_observation_is_refused(self, w: Warehouse) -> None:
        later = datetime.now(UTC) + timedelta(hours=1)
        with pytest.raises(UsageError, match=r"observed_at .* is in the future"):
            add_balance_snapshot(
                w,
                BalanceSnapshotRequest(
                    sportsbook="fanduel", cash_balance=Decimal("1"), observed_at=later
                ),
            )
        assert w.accounts.count() == 0

    def test_an_upload_is_archived_and_cited(self, w: Warehouse, archive_dir: Path) -> None:
        recorded = add_balance_snapshot(
            w,
            BalanceSnapshotRequest(
                sportsbook="fanduel", cash_balance=Decimal("1"), capture_method="screenshot"
            ),
            source=_source(archive_dir, data=PNG, original_name="balance.png"),
        )
        assert recorded.snapshot.source_file_id == w.sources.fetch_all()[0].id

    def test_users_never_share_an_account(self, two_users: Any) -> None:
        _, alice, bob = two_users
        mine = _account(alice)
        recorded = add_balance_snapshot(
            bob, BalanceSnapshotRequest(sportsbook="fanduel", cash_balance=Decimal("1"))
        )
        assert recorded.account_is_new and recorded.account.id != mine.id
        assert alice.balances.for_account(mine.id) == []


class TestCashFlow:
    def test_records_a_deposit(self, w: Warehouse) -> None:
        recorded = record_cash_flow(
            w,
            CashFlowRequest(
                kind="deposit", sportsbook="draftkings", amount=Decimal("20"), occurred_at=PLACED
            ),
        )
        (stored,) = w.cash_flows.for_account(recorded.account.id)
        assert (stored.kind, stored.amount, stored.occurred_at) == (
            "deposit",
            Decimal("20.00"),
            PLACED,
        )

    def test_a_flow_must_move_money(self, w: Warehouse) -> None:
        with pytest.raises(UsageError, match="a withdrawal must move money"):
            record_cash_flow(
                w, CashFlowRequest(kind="withdrawal", sportsbook="fanduel", amount=Decimal(0))
            )
        assert w.accounts.count() == 0

    def test_another_users_source_cannot_be_cited(self, two_users: Any, archive_dir: Path) -> None:
        _, alice, bob = two_users
        source = archive_source(
            alice,
            _source(archive_dir, data=PNG, original_name="slip.png"),
            capture_method="statement",
        )
        with pytest.raises(NotFoundError):
            record_cash_flow(
                bob,
                CashFlowRequest(
                    kind="deposit",
                    sportsbook="fanduel",
                    amount=Decimal("5"),
                    capture_method="statement",
                    source_file_id=source.id,
                ),
            )
        assert bob.cash_flows.count() == 0

    def test_the_kind_is_typed(self) -> None:
        with pytest.raises(ValidationError):
            CashFlowRequest(kind="bonus", sportsbook="fanduel", amount=Decimal(1))  # type: ignore[arg-type]


# -------------------------------------------------------------- bootstrap


def test_bootstrap_is_idempotent(tmp_path: Path) -> None:
    with connect(Settings(data_dir=tmp_path)) as conn:
        first = bootstrap_warehouse(conn, display_name="synthetic")
        second = bootstrap_warehouse(conn)
        assert first.scope_created and first.applied
        assert first.pending_before == len(first.applied)
        assert first.version == max(first.applied)
        assert not second.scope_created and second.applied == []
        assert second.scope == first.scope == identity.existing_scope(conn)


# ----------------------------------------------------------------- shared


class TestRules:
    def test_money_quotes_what_was_typed(self) -> None:
        with pytest.raises(UsageError, match="'1e99' is too large for stake"):
            parse_money("1e99", field_name="stake")
        with pytest.raises(UsageError, match="'NaN' is not a valid amount for amount"):
            check_money(Decimal("NaN"), field_name="amount")
        assert check_money(Decimal("1.005"), field_name="amount") == Decimal("1.01")

    def test_the_future_is_quoted_as_given(self) -> None:
        later = datetime.now(UTC) + timedelta(hours=1)
        with pytest.raises(UsageError, match="--at tomorrow is in the future"):
            require_not_future(later, field_name="--at", shown="tomorrow")
        assert require_not_future(PLACED, field_name="--at") == PLACED

    def test_a_leg_spec_reads_into_a_typed_leg(self) -> None:
        leg = leg_input(parse_leg_spec("sport=NFL, line=-3.5 ,odds=-110,team=Chiefs,market="))
        assert leg == LegInput(
            sport="NFL", line_value=Decimal("-3.5"), odds_american=-110, target_team="Chiefs"
        )

    def test_a_promo_spec_reads_only_text(self) -> None:
        promo = promotion_input(
            {"type": "bogus", "leg": "2", "triggered": "yes", "value_delivered": "1.5"}
        )
        assert (promo.promotion_type, promo.leg, promo.triggered) == ("bogus", 2, True)
        assert promo.value_delivered == Decimal("1.50")
        with pytest.raises(UsageError, match="not a valid leg number"):
            promotion_input({"type": "insurance", "leg": "two"})

    @pytest.mark.parametrize(("raw", "value"), [("No", False), (" 1", True)])
    def test_triggered(self, raw: str, value: bool) -> None:
        assert parse_triggered(raw) is value

    def test_triggered_refuses_anything_else(self) -> None:
        with pytest.raises(UsageError, match="not a valid value for 'triggered'") as caught:
            parse_triggered("maybe")
        assert caught.value.remediation == "Use true|false."
