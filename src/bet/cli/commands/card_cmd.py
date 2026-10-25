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

from bet.analytics.card import card_to_dict
from bet.cli.backend import open_backend
from bet.cli.card_render import render_card
from bet.cli.context import options_from
from bet.cli.output import _plain
from bet.config import OutputFormat, resolve
from bet.errors import UsageError
from bet.services.card import CardQuery


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

    with open_backend(resolved) as backend:
        # Remote mode sends this zone to the API, so its days are this CLI's.
        query = CardQuery(
            tz=_zone(backend.user_timezone()),
            days=days,
            sportsbook=options.sportsbook,
            sport=options.sport,
            include_void=options.include_void,
        )
        result = backend.card(query)

    if options.fmt is OutputFormat.JSON:
        json.dump(card_to_dict(result, share=share), sys.stdout, indent=2, default=_plain)
        sys.stdout.write("\n")
        return
    render_card(result, share=share)


__all__ = ["card"]
