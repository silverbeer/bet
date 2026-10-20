"""Edge-case coverage for ``bet card`` (SB-1363) beyond ``test_card.py``.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import io
import os
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from bet.analytics.card import (
    Card,
    build_card,
    card_to_dict,
    window_bounds,
)
from bet.cli.card_render import BAR, FIRE, card_panel, render_card
from bet.cli.commands import card_cmd
from bet.models.balance import BalanceSnapshot
from bet.models.bet import Bet, BetLeg

NY = ZoneInfo("America/New_York")
TENANT, USER = uuid.uuid4(), uuid.uuid4()
TODAY = date(2026, 10, 20)


def _bet(
    result: str | None,
    settled_at: datetime | None,
    *,
    staked: str = "10.00",
    bonus: str = "0.00",
    returned: str | None = None,
) -> Bet:
    if returned is None:
        returned = {"won": "25.00", "push": staked, "void": staked}.get(result or "", "0.00")
    settled = result is not None
    return Bet(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        placed_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        status="settled" if settled else "pending",
        result=result,  # type: ignore[arg-type]
        settled_at=settled_at if settled else None,
        cash_staked=Decimal(staked),
        bonus_staked=Decimal(bonus),
        cash_returned=Decimal(returned),
        odds_american_placed=150,
    )


def _row(bet: Bet) -> tuple[Bet, list[BetLeg]]:
    leg = BetLeg(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        bet_id=bet.id,
        leg_order=1,
        sport="NFL",
        selection_name="Chiefs ML",
    )
    return bet, [leg]


def _snap(observed: datetime, amount: str) -> BalanceSnapshot:
    return BalanceSnapshot(
        tenant_id=TENANT,
        user_id=USER,
        id=uuid.uuid4(),
        sportsbook_account_id=uuid.uuid4(),
        observed_at=observed,
        cash_balance=Decimal(amount),
    )


def _card(settled: list[tuple[Bet, list[BetLeg]]], **kw: Any) -> Card:
    args: dict[str, Any] = {"days": 7, "today": TODAY, "tz": NY}
    args.update(kw)
    pending = args.pop("pending", [])
    snapshots = args.pop("snapshots", {})
    return build_card(settled, pending, snapshots, **args)


class TestDstWindow:
    today = date(2026, 11, 3)  # window Oct 28 .. Nov 3 spans the fall-back on Nov 1

    def test_window_has_seven_rows_and_spans_the_extra_hour(self) -> None:
        start, end = window_bounds(self.today, 7, NY)
        assert start == datetime(2026, 10, 28, 4, 0, tzinfo=UTC)  # EDT
        assert end == datetime(2026, 11, 4, 5, 0, tzinfo=UTC)  # EST
        assert end - start == timedelta(days=7, hours=1)
        card = _card([], today=self.today)
        assert [d.day for d in card.by_day] == [
            date(2026, 10, 28) + timedelta(days=n) for n in range(7)
        ]

    @pytest.mark.parametrize(
        ("settled_at", "local_day"),
        [
            (datetime(2026, 11, 1, 4, 0, tzinfo=UTC), date(2026, 11, 1)),  # 00:00 EDT
            (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), date(2026, 11, 1)),  # 01:30 EDT
            (
                datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
                date(2026, 11, 1),
            ),  # 01:30 EST, repeated hour
            (datetime(2026, 11, 2, 4, 59, tzinfo=UTC), date(2026, 11, 1)),  # 23:59 EST
            (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), date(2026, 11, 2)),  # 00:00 EST
        ],
    )
    def test_days_bucket_by_local_date_across_the_transition(
        self, settled_at: datetime, local_day: date
    ) -> None:
        card = _card([_row(_bet("won", settled_at))], today=self.today)
        hit = [d.day for d in card.by_day if d.record.bets]
        assert hit == [local_day]


class TestBoundaries:
    def test_settled_at_local_midnight_belongs_to_the_new_day(self) -> None:
        midnight = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)  # 00:00 EDT
        before = midnight - timedelta(seconds=1)
        card = _card([_row(_bet("won", midnight)), _row(_bet("lost", before))])
        by_day = {d.day: d.record for d in card.by_day}
        assert (by_day[date(2026, 10, 20)].won, by_day[date(2026, 10, 20)].lost) == (1, 0)
        assert (by_day[date(2026, 10, 19)].won, by_day[date(2026, 10, 19)].lost) == (0, 1)

    def test_settled_exactly_at_window_end_is_excluded(self) -> None:
        end = window_bounds(TODAY, 7, NY)[1]
        last = end - timedelta(microseconds=1)
        card = _card([_row(_bet("won", end)), _row(_bet("lost", last))])
        assert (card.total.won, card.total.lost) == (0, 1)

    def test_single_day_window(self) -> None:
        today_bet = _row(_bet("won", datetime(2026, 10, 20, 15, 0, tzinfo=UTC)))
        yesterday = _row(_bet("lost", datetime(2026, 10, 19, 15, 0, tzinfo=UTC)))
        card = _card([today_bet, yesterday], days=1)
        assert len(card.by_day) == 1
        assert card.window_start == card.window_end == TODAY
        assert (card.total.won, card.total.lost) == (1, 0)

    def test_single_day_header_is_singular(self) -> None:
        card = _card([], days=1)
        buf = io.StringIO()
        render_card(card, stream=buf)
        assert "Last 1 day " in buf.getvalue()


class TestMoneyRules:
    def test_voids_never_touch_the_record_even_when_counted(self) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        rows = [_row(_bet("won", at)), _row(_bet("void", at, staked="50.00"))]
        plain = _card(rows)
        counted = _card(rows, include_void=True)
        assert counted.voids == 1 and plain.voids is None
        assert counted.total == plain.total
        assert counted.total.staked == Decimal("10.00")
        assert counted.by_day == plain.by_day
        assert counted.streak == plain.streak == "W1"

    def test_void_only_window_has_empty_record_and_no_roi(self) -> None:
        card = _card(
            [_row(_bet("void", datetime(2026, 10, 19, 15, 0, tzinfo=UTC)))], include_void=True
        )
        assert card.total.bets == 0 and card.total.roi is None
        assert card.voids == 1 and card.by_sport == []

    def test_bonus_only_bet_counts_in_record_but_not_money(self) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        free = _row(_bet("won", at, staked="0.00", bonus="10.00", returned="15.00"))
        card = _card([free])
        assert card.total.won == 1
        assert card.total.staked == Decimal("0")
        assert card.total.net == Decimal("0")
        assert card.total.roi is None

    def test_bonus_bet_does_not_dilute_cash_roi(self) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        free = _row(_bet("won", at, staked="0.00", bonus="10.00", returned="15.00"))
        cash = _row(_bet("lost", at))
        card = _card([free, cash])
        assert (card.total.won, card.total.lost) == (1, 1)
        assert card.total.staked == Decimal("10.00")
        assert card.total.roi == Decimal("-1")

    def test_unsettled_bets_stay_out_of_the_record(self) -> None:
        pending = _bet(None, None, staked="20.00")
        card = _card([_row(pending)], pending=[pending])
        assert card.total.bets == 0 and card.total.staked == Decimal("0")
        assert card.pending_count == 1
        assert card.pending_exposure == Decimal("20.00")
        assert all(d.record.bets == 0 for d in card.by_day)


class TestBalanceBaseline:
    def test_snapshot_exactly_at_window_start_is_the_baseline(self) -> None:
        start = window_bounds(TODAY, 7, NY)[0]
        snaps = {"fanduel": [_snap(start, "100.00"), _snap(start + timedelta(days=1), "125.00")]}
        card = _card([], snapshots=snaps)
        assert card.balances[0].change == Decimal("25.00")

    def test_snapshot_just_after_window_start_is_not_a_baseline(self) -> None:
        start = window_bounds(TODAY, 7, NY)[0]
        snaps = {"fanduel": [_snap(start + timedelta(microseconds=1), "100.00")]}
        card = _card([], snapshots=snaps)
        assert card.balances[0].change is None
        assert card.balance_change is None

    def test_account_with_no_snapshots_is_omitted(self) -> None:
        card = _card([], snapshots={"fanduel": []})
        assert card.balances == [] and card.balance_total == Decimal("0")


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set(value)
        for v in value.values():
            found |= _keys(v)
        return found
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


class TestSingleAccountBalance:
    def test_new_account_renders_new_and_no_total(self) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        card = _card([], snapshots={"fanduel": [_snap(at, "75.00")]})
        assert card.balances[0].change is None and card.balance_change is None
        buf = io.StringIO()
        render_card(card, stream=buf)
        text = buf.getvalue()
        assert "fanduel" in text and "$75.00" in text and "(new)" in text
        assert "TOTAL" not in text


class TestShareJson:
    def test_no_money_key_at_any_depth(self) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        pending = _bet(None, None)
        card = _card(
            [_row(_bet("won", at)), _row(_bet("lost", at)), _row(_bet("void", at))],
            pending=[pending],
            snapshots={"fanduel": [_snap(at, "75.00")]},
            include_void=True,
        )
        shared = card_to_dict(card, share=True)
        keys = _keys(shared)
        assert not keys & {
            "net",
            "staked",
            "exposure",
            "balance",
            "balances",
            "change",
            "balance_total",
            "balance_change",
            "observed_at",
        }
        assert not [k for k in keys if any(s in k for s in ("balance", "stake", "net", "exposure"))]
        assert shared["voids"] == 1
        # the full form does carry them, so the scan above is meaningful
        assert {"net", "staked", "exposure", "balances"} <= _keys(card_to_dict(card))


class TestRender:
    def _streaked(self) -> Card:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        return _card([_row(_bet("won", at))])

    def test_emoji_false_has_no_fire(self) -> None:
        out = io.StringIO()
        from rich.console import Console

        Console(file=out, width=72).print(card_panel(self._streaked(), emoji=False))
        assert FIRE not in out.getvalue() and "W1" in out.getvalue()

    def test_ascii_stream_drops_emoji(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="replace")
        render_card(self._streaked(), stream=stream)
        stream.flush()
        text = raw.getvalue().decode("ascii")
        assert FIRE not in text and "W1" in text

    def test_utf8_stream_keeps_emoji(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        render_card(self._streaked(), stream=stream)
        stream.flush()
        assert FIRE in raw.getvalue().decode("utf-8")

    @pytest.mark.parametrize("share", [False, True])
    def test_all_days_net_zero_renders_without_bars(self, share: bool) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        card = _card([_row(_bet("push", at)), _row(_bet("push", at - timedelta(days=1)))])
        buf = io.StringIO()
        render_card(card, share=share, stream=buf)
        text = buf.getvalue()
        assert BAR not in text and "0-0-1" in text

    def test_loss_only_week_draws_bars_and_no_streak_fire(self) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        buf = io.StringIO()
        render_card(_card([_row(_bet("lost", at))]), stream=buf)
        assert BAR in buf.getvalue() and FIRE not in buf.getvalue()

    def test_share_render_has_no_dollars(self) -> None:
        at = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
        card = _card(
            [_row(_bet("won", at))],
            pending=[_bet(None, None, staked="20.00")],
            snapshots={"fanduel": [_snap(at, "75.00")]},
        )
        buf = io.StringIO()
        render_card(card, share=True, stream=buf)
        assert "$" not in buf.getvalue()


class TestMachineZone:
    @pytest.fixture(autouse=True)
    def _no_tz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TZ", raising=False)

    def test_tz_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "Europe/London")
        assert getattr(card_cmd._machine_zone(), "key", None) == "Europe/London"

    def test_tz_env_leading_colon_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", ":Asia/Tokyo")
        assert getattr(card_cmd._machine_zone(), "key", None) == "Asia/Tokyo"

    def test_tz_env_absolute_path_reads_the_zoneinfo_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TZ", ":/usr/share/zoneinfo/Asia/Tokyo")
        assert getattr(card_cmd._machine_zone(), "key", None) == "Asia/Tokyo"

    def test_unset_tz_reads_etc_localtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os.path, "realpath", lambda _p: "/usr/share/zoneinfo/America/Chicago")
        assert getattr(card_cmd._machine_zone(), "key", None) == "America/Chicago"

    @pytest.mark.parametrize("posix", ["JST-9", "Not/AZone"])
    def test_unnamed_tz_uses_its_offset_not_etc_localtime(
        self, monkeypatch: pytest.MonkeyPatch, posix: str
    ) -> None:
        monkeypatch.setenv("TZ", posix)
        monkeypatch.setattr(os.path, "realpath", lambda _p: "/usr/share/zoneinfo/America/Chicago")
        time.tzset()  # the C library reads TZ at startup; re-read the patched one
        try:
            zone = card_cmd._machine_zone()
            assert not hasattr(zone, "key")  # not Chicago from /etc/localtime
            expected = timedelta(hours=9) if posix == "JST-9" else timedelta(0)
            assert datetime.now(zone).utcoffset() == expected
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_unset_tz_and_unnamed_localtime_uses_the_system_offset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os.path, "realpath", lambda _p: "/etc/localtime")
        zone = card_cmd._machine_zone()
        assert zone is not None and not hasattr(zone, "key")

    def test_stored_zone_other_than_utc_beats_the_machine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TZ", "Europe/London")
        assert getattr(card_cmd._zone("Asia/Tokyo"), "key", None) == "Asia/Tokyo"

    def test_stored_utc_defers_to_the_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "Europe/London")
        assert getattr(card_cmd._zone("UTC"), "key", None) == "Europe/London"
