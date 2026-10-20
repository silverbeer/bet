"""``bet card`` -- shareable summary of recent betting (SB-1363).

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from bet.analytics.card import (
    MULTI,
    OTHER,
    Record,
    build_card,
    card_to_dict,
    outcome,
    record_of,
    selection_summary,
    sport_of,
    streak,
)
from bet.cli.card_render import money, odds, percent
from bet.cli.commands import list_cmd
from bet.cli.commands.card_cmd import _zone
from bet.cli.main import app
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import BetLegRepository, Warehouse
from bet.errors import UsageError
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet, BetLeg
from bet.models.ownership import OwnerScope, SportsbookAccount

runner = CliRunner()

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 20)
TENANT, USER = uuid.uuid4(), uuid.uuid4()


# --------------------------------------------------------------- builders


def _at(day: int, hour: int = 18) -> datetime:
    """A UTC instant on 2026-10-``day``."""
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


def _bet(
    result: str | None = "won",
    *,
    staked: str = "10.00",
    returned: str | None = None,
    bonus: str = "0.00",
    settled_at: datetime | None = None,
    odds_placed: int | None = 150,
    odds_settled: int | None = None,
) -> Bet:
    if returned is None:
        returned = {"won": "25.00", "push": staked, "void": staked}.get(result or "", "0.00")
    settled = result is not None
    return Bet(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        placed_at=_at(14, 12),
        status="settled" if settled else "pending",
        result=result,  # type: ignore[arg-type]
        settled_at=(settled_at or _at(19)) if settled else None,
        cash_staked=Decimal(staked),
        bonus_staked=Decimal(bonus),
        cash_returned=Decimal(returned),
        odds_american_placed=odds_placed,
        odds_american_settled=odds_settled,
    )


def _leg(order: int = 1, **fields: Any) -> BetLeg:
    return BetLeg(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        bet_id=uuid.uuid4(),
        leg_order=order,
        **fields,
    )


def _snap(observed: datetime, amount: str) -> BalanceSnapshot:
    return BalanceSnapshot(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        observed_at=observed,
        cash_balance=Decimal(amount),
    )


def _card(settled: list[tuple[Bet, list[BetLeg]]], **kw: Any) -> Any:
    args: dict[str, Any] = {"days": 7, "today": TODAY, "tz": NY}
    args.update(kw)
    pending = args.pop("pending", [])
    snapshots = args.pop("snapshots", {})
    return build_card(settled, pending, snapshots, **args)


def _nfl(bet: Bet) -> tuple[Bet, list[BetLeg]]:
    return bet, [_leg(sport="NFL", selection_name="Chiefs ML")]


# ---------------------------------------------------------------- outcome


@pytest.mark.parametrize(
    ("result", "returned", "expected"),
    [
        ("won", "25.00", "W"),
        ("lost", "0.00", "L"),
        ("push", "10.00", "P"),
        ("void", "10.00", None),
        ("partial", "15.00", "W"),
        ("partial", "4.00", "L"),
        ("partial", "10.00", "P"),
        ("cashed_out", "12.00", "W"),
        ("cashed_out", "3.00", "L"),
        ("cashed_out", "10.00", "P"),
    ],
)
def test_outcome_mapping(result: str, returned: str, expected: str | None) -> None:
    assert outcome(_bet(result, returned=returned)) == expected


def test_pending_bet_has_no_outcome() -> None:
    assert outcome(_bet(None)) is None


# ------------------------------------------------------------------- ROI


def test_record_roi_excludes_free_bets_and_voids_but_keeps_pushes() -> None:
    bets = [
        _bet("won", returned="25.00"),  # +15
        _bet("lost"),  # -10
        _bet("push"),  # 0, in the denominator
        _bet("void"),  # nowhere
        _bet("won", staked="0.00", bonus="10.00", returned="15.00"),  # free bet: W, no money
    ]
    r = record_of(bets)
    assert (r.won, r.lost, r.push) == (2, 1, 1)
    assert r.staked == Decimal("30.00")
    assert r.net == Decimal("5.00")
    assert r.roi == Decimal("0.1667")
    assert r.label == "2-1-1"


def test_record_label_omits_zero_pushes() -> None:
    assert Record(won=5, lost=3).label == "5-3"
    assert Record().roi is None


# -------------------------------------------------------------- bucketing


def test_settlement_at_0200_utc_belongs_to_the_previous_new_york_day() -> None:
    late = _bet("won", settled_at=datetime(2026, 10, 20, 2, 0, tzinfo=UTC))
    card = _card([_nfl(late)])
    by_day = {d.day: d.record for d in card.by_day}
    assert by_day[date(2026, 10, 19)].won == 1
    assert by_day[date(2026, 10, 20)].bets == 0


def test_window_edges_are_local_midnights() -> None:
    # 2026-10-14 00:00 New York is 04:00 UTC; one minute before is outside.
    inside = _bet("won", settled_at=datetime(2026, 10, 14, 4, 0, tzinfo=UTC))
    outside = _bet("lost", settled_at=datetime(2026, 10, 14, 3, 59, tzinfo=UTC))
    tomorrow = _bet("lost", settled_at=datetime(2026, 10, 21, 4, 0, tzinfo=UTC))
    card = _card([_nfl(inside), _nfl(outside), _nfl(tomorrow)])
    assert (card.total.won, card.total.lost) == (1, 0)
    assert card.window_start == date(2026, 10, 14)
    assert card.window_end == TODAY


def test_every_day_in_the_window_has_a_row() -> None:
    card = _card([_nfl(_bet("won", settled_at=_at(17)))])
    assert [d.day for d in card.by_day] == [TODAY - timedelta(days=n) for n in range(6, -1, -1)]
    assert sum(1 for d in card.by_day if d.record.bets == 0) == 6


def test_settled_bet_without_settled_at_is_skipped() -> None:
    bet = _bet("won")
    bet.__dict__["settled_at"] = None  # legacy row: settled with no date
    assert _card([_nfl(bet)]).total.bets == 0


# ----------------------------------------------------------------- streak


def test_streak_skips_pushes_without_breaking() -> None:
    bets = [
        _bet("lost", settled_at=_at(15)),
        _bet("won", settled_at=_at(16)),
        _bet("push", settled_at=_at(17)),
        _bet("won", settled_at=_at(18)),
        _bet("won", settled_at=_at(19)),
    ]
    assert streak(bets) == "W3"
    assert streak([_bet("won", settled_at=_at(15)), _bet("lost", settled_at=_at(16))]) == "L1"
    assert streak([_bet("push")]) is None


def test_streak_breaks_settle_ties_by_placement_then_id() -> None:
    at = _at(19)
    later = _bet("won", settled_at=at).model_copy(update={"placed_at": _at(14, 15)})
    earlier = _bet("lost", settled_at=at).model_copy(update={"placed_at": _at(14, 9)})
    # search returns placed_at DESC; the later-placed bet is still the latest.
    assert streak([later, earlier]) == "W1"
    assert streak([earlier, later]) == "W1"
    a, b = _bet("won", settled_at=at), _bet("lost", settled_at=at)
    last = max((a, b), key=lambda x: x.id)
    expected = "W1" if last is a else "L1"
    assert streak([a, b]) == streak([b, a]) == expected


# ------------------------------------------------------------------ sport


def test_sport_attribution() -> None:
    assert sport_of([_leg(sport=" nfl ")]) == "NFL"
    assert sport_of([_leg(1, sport="NFL"), _leg(2, sport="nfl")]) == "NFL"
    assert sport_of([_leg(1, sport="NFL"), _leg(2, sport="NBA")]) == MULTI
    assert sport_of([_leg(league="EPL")]) == "EPL"
    assert sport_of([_leg()]) == OTHER
    assert sport_of([]) == OTHER


def test_one_ticket_counts_once_and_sports_sort_by_net() -> None:
    sgp = (_bet("lost"), [_leg(1, sport="NFL"), _leg(2, sport="NBA")])
    nfl = _nfl(_bet("won", returned="30.00"))
    nba = (_bet("won", returned="15.00"), [_leg(sport="NBA")])
    card = _card([sgp, nfl, nba])
    assert [(s.sport, s.record.bets) for s in card.by_sport] == [
        ("NFL", 1),
        ("NBA", 1),
        (MULTI, 1),
    ]


# --------------------------------------------------------------- best hit


def test_best_hit_breaks_ties_by_later_settle_and_falls_back_to_placed_odds() -> None:
    early = _bet("won", returned="25.00", settled_at=_at(16), odds_settled=200)
    late = _bet("won", returned="25.00", settled_at=_at(18), odds_placed=-110)
    legs = [_leg(1, sport="NFL", selection_name="Chiefs ML"), _leg(2, sport="NFL")]
    card = _card([(early, [_leg(sport="NFL")]), (late, legs)])
    assert card.best is not None
    assert card.best.bet_id == late.id
    assert card.best.odds_american == -110
    assert card.best.label == "Chiefs ML +1 more"
    assert card.best.net == Decimal("15.00")


def test_best_hit_prefers_settled_odds() -> None:
    bet = _bet("won", odds_placed=150, odds_settled=175)
    card = _card([_nfl(bet)])
    assert card.best is not None and card.best.odds_american == 175


def test_no_winner_means_no_best_hit() -> None:
    assert _card([_nfl(_bet("lost"))]).best is None


def test_free_bet_is_never_the_best_hit() -> None:
    free = _bet("won", staked="0.00", bonus="50.00", returned="90.00")  # +90, not in NET
    cash = _bet("won", returned="25.00")  # +15
    card = _card([_nfl(free), _nfl(cash)])
    assert card.best is not None and card.best.bet_id == cash.id
    assert card.best.net == Decimal("15.00")
    assert _card([_nfl(free)]).best is None


def test_list_uses_the_shared_selection_summary() -> None:
    assert vars(list_cmd)["selection_summary"] is selection_summary
    assert not hasattr(list_cmd, "_selection_summary")
    assert selection_summary([]) == ""
    assert selection_summary([_leg()]) == "?"
    assert selection_summary([_leg(1, selection_name="A"), _leg(2), _leg(3)]) == "A +2 more"


# --------------------------------------------------------------- balances


def test_balance_change_against_snapshot_before_window() -> None:
    snapshots = {
        "fanduel": [_snap(_at(10), "100.00"), _snap(_at(18), "130.00")],
        "draftkings": [_snap(_at(17), "40.00")],
    }
    card = _card([], snapshots=snapshots)
    by_name = {a.account: a for a in card.balances}
    assert by_name["fanduel"].change == Decimal("30.00")
    assert by_name["draftkings"].change is None
    assert card.balance_total == Decimal("170.00")
    # draftkings has no baseline, so a TOTAL change would cover only fanduel.
    assert card.balance_change is None


def test_balance_total_change_needs_every_account() -> None:
    snapshots = {
        "fanduel": [_snap(_at(10), "100.00"), _snap(_at(18), "130.00")],
        "draftkings": [_snap(_at(11), "50.00"), _snap(_at(17), "40.00")],
    }
    card = _card([], snapshots=snapshots)
    assert card.balance_total == Decimal("170.00")
    assert card.balance_change == Decimal("20.00")


def _flow(kind: str, occurred: datetime, amount: str) -> CashFlow:
    return CashFlow(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        kind=kind,  # type: ignore[arg-type]
        occurred_at=occurred,
        amount=Decimal(amount),
    )


def test_balance_change_excludes_deposits_and_withdrawals() -> None:
    snapshots = {"fanduel": [_snap(_at(10), "100.00"), _snap(_at(18), "180.00")]}
    flows = {
        "fanduel": [
            _flow("deposit", _at(9), "500.00"),  # before the baseline: already in it
            _flow("deposit", _at(15), "100.00"),  # inside: not betting
            _flow("withdrawal", _at(16), "40.00"),  # inside: not betting
            _flow("deposit", _at(18), "10.00"),  # at the latest snapshot: inside
            _flow("deposit", _at(19), "999.00"),  # after the latest: not in it yet
        ]
    }
    card = _card([], snapshots=snapshots, cash_flows=flows)
    # 180 - 100 = +80 on the balance, of which 100 - 40 + 10 = +70 was moved in.
    assert card.balances[0].change == Decimal("10.00")
    assert card.balance_change == Decimal("10.00")
    assert card.balance_total == Decimal("180.00")


def test_pending_exposure_and_void_count() -> None:
    pending = [_bet(None, staked="5.00"), _bet(None, staked="10.00")]
    voided = _nfl(_bet("void"))
    card = _card([voided], pending=pending, include_void=True)
    assert (card.pending_count, card.pending_exposure) == (2, Decimal("15.00"))
    assert card.voids == 1
    assert card.total.bets == 0
    assert _card([voided]).voids is None


def test_empty_warehouse() -> None:
    card = _card([])
    assert card.total == Record()
    assert card.streak is None and card.best is None
    assert card.by_sport == [] and card.balances == []
    assert card.balance_change is None
    assert len(card.by_day) == 7


# ------------------------------------------------------------------ JSON


def test_card_to_dict_share_strips_money() -> None:
    card = _card(
        [_nfl(_bet("won"))],
        pending=[_bet(None)],
        snapshots={"fanduel": [_snap(_at(18), "50.00")]},
    )
    full = card_to_dict(card)
    assert full["record"]["net"] == Decimal("15.00")
    assert full["balances"][0]["account"] == "fanduel"
    shared = card_to_dict(card, share=True)
    assert "net" not in shared["record"] and "staked" not in shared["record"]
    assert "balances" not in shared and "balance_total" not in shared
    assert "exposure" not in shared["pending"]
    assert "net" not in shared["best"]
    assert all("net" not in d for d in shared["by_day"] + shared["by_sport"])


# -------------------------------------------------------------- formatting


def test_money_and_percent_formatting() -> None:
    assert money(Decimal("-8.00"), signed=True) == "-$8.00"
    assert money(Decimal("30.00"), signed=True) == "+$30.00"
    assert money(Decimal("1210.50")) == "$1,210.50"
    assert percent(Decimal("0.0812")) == "+8.1%"
    assert percent(Decimal("-0.0640")) == "-6.4%"
    assert percent(None) == "—"


def test_odds_formatting() -> None:
    assert odds(None) == ""
    assert odds(-110) == "-110"
    assert odds(150) == "+150"


# -------------------------------------------------------------------- CLI


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for name in list(dict(os.environ)):
        if name.startswith("BET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TZ", "America/New_York")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@contextmanager
def _open_warehouse(data: Path) -> Iterator[Warehouse]:
    settings = Settings(data_dir=data)
    config = type("_Config", (), {"settings": settings})()
    with connect(settings) as conn:
        scope = identity.resolve_scope(conn, config)
        yield Warehouse(conn, scope)


def _seed(w: Warehouse, code: str, *, result: str, returned: str, sport: str = "NFL") -> None:
    now = datetime.now(UTC)
    account = w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code=code,
            label=code,
        )
    )
    bet = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            placed_at=now - timedelta(hours=3),
            status="settled",
            result=result,  # type: ignore[arg-type]
            settled_at=now - timedelta(minutes=1),
            cash_staked=Decimal("10.00"),
            cash_returned=Decimal(returned),
            odds_american_placed=150,
        )
    )
    w.legs.add(
        BetLeg(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=bet.id,
            leg_order=1,
            sport=sport,
            selection_name="[Over] Chiefs",
        )
    )
    w.balances.add(
        BalanceSnapshot(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            observed_at=now - timedelta(minutes=1),
            cash_balance=Decimal("75.00"),
        )
    )


@pytest.fixture
def seeded(data_dir: Path) -> Path:
    runner.invoke(app, ["init"])
    with _open_warehouse(data_dir) as w:
        _seed(w, "fanduel", result="won", returned="25.00")
        _seed(w, "draftkings", result="lost", returned="0.00", sport="NBA")
    return data_dir


def _json(*args: str) -> dict[str, Any]:
    result = runner.invoke(app, ["--format", "json", *args])
    assert result.exit_code == 0, result.output
    out: dict[str, Any] = json.loads(result.stdout)
    return out


def test_cli_table_shows_record_and_sports(seeded: Path) -> None:
    result = runner.invoke(app, ["card"])
    assert result.exit_code == 0, result.output
    assert "RECORD" in result.stdout
    assert "1-1" in result.stdout
    assert "NFL" in result.stdout and "NBA" in result.stdout
    assert "[Over] Chiefs" in result.stdout  # user text is not markup
    assert "$" in result.stdout


def test_cli_share_shows_no_dollars(seeded: Path) -> None:
    result = runner.invoke(app, ["card", "--share"])
    assert result.exit_code == 0, result.output
    assert "RECORD" in result.stdout
    assert "$" not in result.stdout


def test_cli_json_keys_and_share(seeded: Path) -> None:
    full = _json("card")
    assert {"record", "by_day", "by_sport", "balances", "pending", "streak"} <= set(full)
    assert full["record"]["net"] == "5.00"
    assert full["balance_total"] == "150.00"
    shared = _json("card", "--share")
    assert "balances" not in shared and "net" not in shared["record"]


def test_cli_sportsbook_narrows_bets_and_balances(seeded: Path) -> None:
    out = _json("--sportsbook", "fanduel", "card")
    assert [a["account"] for a in out["balances"]] == ["fanduel"]
    assert out["record"]["label"] == "1-0"


def test_cli_rejects_since(seeded: Path) -> None:
    result = runner.invoke(app, ["--since", "2026-10-01", "card"])
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 64


def test_cli_rejects_csv(seeded: Path) -> None:
    result = runner.invoke(app, ["--format", "csv", "card"])
    assert isinstance(result.exception, UsageError)


def test_cli_rejects_days_out_of_range(seeded: Path) -> None:
    assert runner.invoke(app, ["card", "--days", "0"]).exit_code != 0
    assert runner.invoke(app, ["card", "--days", "61"]).exit_code != 0


@pytest.mark.parametrize("share", [False, True])
def test_cli_fits_a_narrow_terminal(
    seeded: Path, monkeypatch: pytest.MonkeyPatch, share: bool
) -> None:
    monkeypatch.setenv("COLUMNS", "64")
    result = runner.invoke(app, ["card", *(["--share"] if share else [])])
    assert result.exit_code == 0, result.output
    assert max(len(line) for line in result.stdout.splitlines()) <= 72


def test_cli_caps_width_on_a_wide_terminal(seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["card"])
    assert max(len(line) for line in result.stdout.splitlines()) <= 72


def test_zone_prefers_a_set_user_zone_and_treats_utc_as_unset() -> None:
    assert getattr(_zone("America/Chicago"), "key", None) == "America/Chicago"
    assert getattr(_zone("UTC"), "key", None) == "America/New_York"  # TZ from the fixture
    assert getattr(_zone(None), "key", None) == "America/New_York"
    assert getattr(_zone("Not/AZone"), "key", None) == "America/New_York"


def _account(w: Warehouse, code: str) -> SportsbookAccount:
    return w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code=code,
            label=code,
        )
    )


def _settled_bet(w: Warehouse, account: SportsbookAccount, settled_at: datetime) -> Bet:
    bet = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            placed_at=settled_at - timedelta(hours=3),
            status="settled",
            result="won",
            settled_at=settled_at,
            cash_staked=Decimal("10.00"),
            cash_returned=Decimal("25.00"),
        )
    )
    w.legs.add(
        BetLeg(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            bet_id=bet.id,
            leg_order=1,
            sport="NFL",
            selection_name="Chiefs ML",
        )
    )
    return bet


def test_search_bounds_settled_at_inclusively(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    t = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    with _open_warehouse(data_dir) as w:
        account = _account(w, "fanduel")
        before = _settled_bet(w, account, t - timedelta(seconds=1))
        first = _settled_bet(w, account, t)
        last = _settled_bet(w, account, t + timedelta(days=1))
        after = _settled_bet(w, account, t + timedelta(days=1, seconds=1))
        found = {b.id for b in w.bets.search(settled_since=t, settled_until=t + timedelta(days=1))}
        assert found == {first.id, last.id}
        assert {b.id for b in w.bets.search(settled_since=t)} == {first.id, last.id, after.id}
        assert {b.id for b in w.bets.search(settled_until=t)} == {before.id, first.id}
        assert len(w.bets.search()) == 4  # existing callers are unbounded


def test_cli_fetches_legs_for_the_window_only(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _open_warehouse(seeded) as w:
        old = _settled_bet(w, _account(w, "betmgm"), datetime.now(UTC) - timedelta(days=30))
    fetched: list[uuid.UUID] = []
    original = BetLegRepository.for_bet

    def spy(self: BetLegRepository, bet_id: uuid.UUID) -> list[BetLeg]:
        fetched.append(bet_id)
        return original(self, bet_id)

    monkeypatch.setattr(BetLegRepository, "for_bet", spy)
    out = _json("card")
    assert out["record"]["label"] == "1-1"
    assert len(fetched) == 2 and old.id not in fetched


def test_cli_balance_change_excludes_a_deposit(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    now = datetime.now(UTC)
    with _open_warehouse(data_dir) as w:
        account = _account(w, "fanduel")
        for observed, amount in ((now - timedelta(days=30), "100.00"), (now, "160.00")):
            w.balances.add(
                BalanceSnapshot(
                    tenant_id=w.scope.tenant_id,
                    user_id=w.scope.user_id,
                    id=uuid.uuid4(),
                    sportsbook_account_id=account.id,
                    observed_at=observed,
                    cash_balance=Decimal(amount),
                )
            )
        w.cash_flows.add(
            CashFlow(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                sportsbook_account_id=account.id,
                kind="deposit",
                occurred_at=now - timedelta(days=2),
                amount=Decimal("50.00"),
            )
        )
    out = _json("card")
    assert out["balances"][0]["change"] == "10.00"
    assert out["balance_change"] == "10.00"


def test_user_timezone_is_none_for_a_missing_user(data_dir: Path) -> None:
    runner.invoke(app, ["init"])
    settings = Settings(data_dir=data_dir)
    with connect(settings) as conn:
        stranger = OwnerScope(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
        assert identity.user_timezone(conn, stranger) is None
