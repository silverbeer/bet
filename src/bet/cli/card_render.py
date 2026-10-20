"""Rich rendering for ``bet card`` (SB-1363).

One rounded panel, at most ``MAX_WIDTH`` cells wide so a phone screenshot of
it stays legible. ``share`` hides every dollar amount -- balances, stake,
exposure, daily and best-hit money -- leaving the record, ratios and odds,
which say how the picks went without saying how much rode on them.

Every user-entered string (account labels, selections) goes in as ``Text``,
never markup: a selection like ``[Over]`` must print as written.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from bet.analytics.card import Card, DayRow, Record

MAX_WIDTH = 72
# A 3/4-height block leaves a sliver of background between rows, so a run
# of days reads as separate bars rather than one solid shape.
BAR_WIDTH = 18
BAR = "▆"
DASH = "—"
EN_DASH = "\u2013"
FIRE = "🔥"

ONE_PLACE = Decimal("0.1")


def money(value: Decimal, *, signed: bool = False) -> str:
    """Dollars to the cent, thousands grouped; ``signed`` adds ``+`` to a gain."""
    body = f"${abs(value):,.2f}"
    if value < 0:
        return f"-{body}"
    return f"+{body}" if signed else body


def percent(ratio: Decimal | None) -> str:
    if ratio is None:
        return DASH
    pct = (ratio * 100).quantize(ONE_PLACE, ROUND_HALF_UP)
    return f"{'+' if pct > 0 else ''}{pct}%"


def odds(value: int | None) -> str:
    if value is None:
        return ""
    return f"+{value}" if value > 0 else str(value)


def _tone(value: Decimal | int) -> str:
    return "green" if value > 0 else "red" if value < 0 else ""


def _bar(value: Decimal | int, scale: Decimal | int) -> Text:
    if not scale or not value:
        return Text("")
    share = abs(Decimal(value)) / Decimal(scale)
    cells = int((share * BAR_WIDTH).quantize(Decimal(1), ROUND_HALF_UP))
    return Text(BAR * max(cells, 1), style=_tone(value))


ACCENT = "cyan"


def _section(title: str) -> Group:
    # The blank line above each section is what keeps the card from reading
    # as one dense block.
    rule = Rule(Text(f" {title.upper()} ", style=f"bold {ACCENT}"), align="left", style="dim")
    return Group(Text(""), rule)


def _header(card: Card) -> Text:
    span = "day" if card.days == 1 else "days"
    start, end = card.window_start, card.window_end
    dates = (
        f"{end:%b} {end.day}"
        if start == end
        else f"{start:%b} {start.day}{EN_DASH}{end:%b} {end.day}"
    )
    return Text(f" Last {card.days} {span} · {dates} ({card.tz_name})", style="dim")


def _summary(card: Card, *, share: bool, emoji: bool) -> Table:
    """Stat tiles: a dim label over a bold value, one column each."""
    total = card.total
    tiles: list[tuple[str, Text]] = [
        ("RECORD", Text(total.label if total.bets else DASH, style="bold")),
    ]
    if not share:
        tiles.append(("NET", Text(money(total.net, signed=True), style=f"bold {_tone(total.net)}")))
    tiles.append(("ROI", Text(percent(total.roi), style=f"bold {_tone(total.roi or 0)}")))
    if card.streak:
        hot = card.streak.startswith("W")
        mark = FIRE if hot and emoji else ""
        tiles.append(("STREAK", Text(mark + card.streak, style="bold")))

    grid = Table.grid(padding=(0, 3))
    for _ in tiles:
        grid.add_column(no_wrap=True)
    grid.add_row(*(Text(f" {label}", style="dim") for label, _ in tiles))
    grid.add_row(*(Text(" ").append_text(value) for _, value in tiles))
    return grid


def _detail(card: Card, *, share: bool) -> Text:
    parts: list[str] = []
    if not share:
        parts.append(f"Staked {money(card.total.staked)}")
    pending = f"Pending {card.pending_count}"
    if card.pending_count and not share:
        pending += f" ({money(card.pending_exposure)} at risk)"
    parts.append(pending)
    if card.voids is not None:
        parts.append(f"Voids {card.voids}")
    return Text(" " + " · ".join(parts), style="dim")


def _daily(rows: Sequence[DayRow], *, share: bool) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)  # day
    grid.add_column(no_wrap=True)  # record
    if not share:
        grid.add_column(justify="right", no_wrap=True)  # net
    grid.add_column(no_wrap=True)  # bar

    def swing(r: Record) -> int:
        return r.won - r.lost

    if share:
        scale: Decimal | int = max((abs(swing(d.record)) for d in rows), default=0)
    else:
        scale = max((abs(d.record.net) for d in rows), default=Decimal(0))

    for d in rows:
        day = f" {d.day:%a} {d.day.day:>2}"
        r = d.record
        if not r.bets:
            cells: list[RenderableType] = [Text(day, style="dim"), Text(DASH, style="dim")]
            cells += [Text("")] * (1 if share else 2)
            grid.add_row(*cells)
            continue
        label = Text(r.label, style=_tone(swing(r)))
        if share:
            grid.add_row(Text(day), label, _bar(swing(r), scale))
        else:
            grid.add_row(
                Text(day),
                label,
                Text(money(r.net, signed=True), style=_tone(r.net)),
                _bar(r.net, scale),
            )
    return grid


def _by_sport(card: Card, *, share: bool) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True)
    if not share:
        grid.add_column(justify="right", no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    for s in card.by_sport:
        r = s.record
        cells: list[RenderableType] = [
            Text(f" {s.sport}", style="bold"),
            Text(r.label, style=_tone(r.won - r.lost)),
        ]
        if not share:
            cells.append(Text(money(r.net, signed=True), style=_tone(r.net)))
        cells.append(Text(percent(r.roi), style=_tone(r.roi or 0)))
        grid.add_row(*cells)
    return grid


def _best(card: Card, *, share: bool) -> Group:
    """The pick on one line, the price (and payout) beneath it, so a long
    parlay label never has to be cut short to fit the numbers."""
    best = card.best
    assert best is not None
    price = Text("   ")
    price.append(odds(best.odds_american), style="bold")
    if not share:
        price.append("  →  ")
        price.append(money(best.net, signed=True), style="bold green")
    pick = Text(f" {best.sport} · ", style="dim")
    pick.append_text(Text(best.label, style="bold"))
    pick.no_wrap = True
    pick.overflow = "ellipsis"
    return Group(pick, price)


def _balances(card: Card) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True, overflow="ellipsis", max_width=28)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True)

    def change(value: Decimal | None) -> Text:
        if value is None:
            return Text("(new)", style="dim")
        return Text(f"({money(value, signed=True)})", style=_tone(value))

    for a in card.balances:
        grid.add_row(Text(f" {a.account}"), Text(money(a.balance)), change(a.change))
    if len(card.balances) > 1:
        grid.add_row(
            Text(" TOTAL", style="bold"),
            Text(money(card.balance_total), style="bold"),
            change(card.balance_change) if card.balance_change is not None else Text(""),
        )
    return grid


def card_panel(card: Card, *, share: bool = False, emoji: bool = True) -> Panel:
    parts: list[RenderableType] = [
        _header(card),
        Text(""),
        _summary(card, share=share, emoji=emoji),
        Text(""),
        _detail(card, share=share),
        _section("Daily"),
        _daily(card.by_day, share=share),
    ]
    if card.by_sport:
        parts += [_section("By sport"), _by_sport(card, share=share)]
    if card.best is not None:
        parts += [_section("Best hit"), _best(card, share=share)]
    if card.balances and not share:
        parts += [_section("Balances"), _balances(card)]
    return Panel(
        Group(*parts),
        box=box.ROUNDED,
        title=Text(" BET CARD ", style=f"bold black on {ACCENT}"),
        border_style=ACCENT,
        padding=(1, 2),
    )


def render_card(card: Card, *, share: bool = False, stream: TextIO | None = None) -> None:
    """Print the card, no wider than ``MAX_WIDTH`` whatever the terminal."""
    probe = Console(file=stream)
    console = Console(file=stream, width=min(probe.width, MAX_WIDTH))
    emoji = (console.encoding or "").lower().replace("-", "").startswith("utf")
    console.print(card_panel(card, share=share, emoji=emoji))


__all__ = ["BAR", "MAX_WIDTH", "card_panel", "money", "odds", "percent", "render_card"]
