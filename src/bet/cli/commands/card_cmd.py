"""``bet card`` -- a shareable summary of recent betting (SB-1363).

Record, net and ROI over the last ``--days`` local days by settle date, day by
day and by sport, plus the streak, the best hit, pending exposure and account
balances. ``--share`` hides every dollar amount so the card can be sent to
friends without saying what rode on it.

The global ``--sportsbook`` and ``--sport`` narrow the bets, and
``--sportsbook`` the balances too. ``--since``/``--until`` are refused: they
bound placement dates, and the card's window is ``--days`` by settle date --
honouring both would describe neither. ``--include-void`` adds a void count;
voids never enter the record.

Days are local: the user's configured time zone when one has been set,
otherwise this machine's. Read-only.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, tzinfo
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer

from bet.analytics.card import build_card, card_to_dict, window_bounds
from bet.cli.card_render import render_card
from bet.cli.commands.balance_cmd import account_name
from bet.cli.context import options_from
from bet.cli.output import _plain
from bet.config import OutputFormat, resolve
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet, BetLeg


def _named(key: str) -> tzinfo | None:
    """``key`` as an IANA zone; a path is read from its ``zoneinfo/`` suffix."""
    if "zoneinfo/" in key:
        key = key.split("zoneinfo/", 1)[1]
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError, ValueError:
        return None


def _machine_zone() -> tzinfo:
    """This machine's zone, by IANA name where it can be found.

    A named zone keeps each day of the window on its own UTC offset across a
    DST change; ``astimezone()`` alone yields today's fixed offset. A set
    ``TZ`` decides alone: when it names no zone (a POSIX rule like ``JST-9``)
    the offset it gives wins over ``/etc/localtime``, which it overrides.
    """
    tz = os.environ.get("TZ")
    zone = _named(tz.lstrip(":")) if tz else _named(os.path.realpath("/etc/localtime"))
    if zone is not None:
        return zone
    local = datetime.now().astimezone().tzinfo
    assert local is not None  # astimezone() always attaches a zone
    return local


def _zone(stored: str | None) -> tzinfo:
    """The user's zone if one was ever set; ``UTC`` is the unset default, so it defers."""
    if stored and stored != "UTC":
        try:
            return ZoneInfo(stored)
        except ZoneInfoNotFoundError, ValueError:
            pass
    return _machine_zone()


def card(
    ctx: typer.Context,
    days: Annotated[
        int,
        typer.Option("--days", "-d", min=1, max=60, help="Local days to cover, ending today."),
    ] = 7,
    share: Annotated[
        bool,
        typer.Option("--share", help="Hide every dollar amount, for sending to friends."),
    ] = False,
) -> None:
    """Shareable summary of recent betting."""
    options = options_from(ctx)
    if options.since is not None or options.until is not None:
        raise UsageError(
            "card window is --days, by settle date; --since/--until do not apply.",
            remediation="Use `bet card --days N` to choose how many days to cover.",
        )
    if options.fmt is OutputFormat.CSV:
        raise UsageError(
            "bet card has no CSV form: it is one summary, not rows.",
            remediation="Use --format json for a machine-readable card.",
        )

    resolved = resolve()
    code = (options.sportsbook or "").strip().lower() or None

    with connect(resolved.settings) as conn:
        scope = identity.resolve_scope(conn, resolved)
        w = Warehouse(conn, scope)
        tz = _zone(identity.user_timezone(conn, scope))
        today = datetime.now(tz).date()
        start, end = window_bounds(today, days, tz)

        # Bound by settle time in SQL so legs are fetched for the window only.
        in_window = w.bets.search(
            status="settled",
            sportsbook_code=code,
            sport=options.sport,
            settled_since=start,
            settled_until=end,
        )
        settled: list[tuple[Bet, list[BetLeg]]] = [
            (bet, w.legs.for_bet(bet.id)) for bet in in_window
        ]
        pending = w.bets.search(is_open=True, sportsbook_code=code, sport=options.sport)
        accounts = [
            a
            for a in w.accounts.fetch_all(order_by="sportsbook_code, label")
            if code is None or a.sportsbook_code == code
        ]
        snapshots: dict[str, list[BalanceSnapshot]] = {
            account_name(a): w.balances.for_account(a.id) for a in accounts
        }
        flows: dict[str, list[CashFlow]] = {
            account_name(a): w.cash_flows.for_account(a.id) for a in accounts
        }

    result = build_card(
        settled,
        pending,
        snapshots,
        cash_flows=flows,
        days=days,
        today=today,
        tz=tz,
        include_void=options.include_void,
    )
    if options.fmt is OutputFormat.JSON:
        json.dump(card_to_dict(result, share=share), sys.stdout, indent=2, default=_plain)
        sys.stdout.write("\n")
        return
    render_card(result, share=share)


__all__ = ["card"]
