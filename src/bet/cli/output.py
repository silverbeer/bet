"""Result rendering for the three output formats.

``table`` is for humans and uses Rich. ``json`` and ``csv`` are contracts with
other programs: they carry no colour, no borders, no progress output, and
nothing but the result itself.
"""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

from rich.console import Console
from rich.table import Table
from rich.text import Text

from bet.config import OutputFormat


def _plain(value: Any) -> Any:
    """Render a value for machine-readable output.

    Decimal becomes a string, not a float: money must not acquire binary
    floating-point error on the way out of the process.

    UUID is here because ``bets show`` renders provenance ids straight off the
    model. Every one of them was null until a bet could cite a source file, so
    `--format json` raised ``TypeError`` on the first bet that carried one.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def render(
    rows: Sequence[dict[str, Any]],
    *,
    columns: Sequence[str] | None = None,
    fmt: OutputFormat = OutputFormat.TABLE,
    title: str | None = None,
    stream: TextIO | None = None,
    markup: bool = True,
    fold: bool = False,
) -> None:
    """Write ``rows`` to stdout in the requested format.

    ``markup=False`` renders table cells as literal text, for rows carrying
    user-entered strings: an account label like ``[old]`` must print as
    written, not be read as a Rich style tag (or fail to parse as one).

    ``fold=True`` wraps an over-wide cell onto further lines instead of
    cutting it short with an ellipsis -- for wide tables of money, where a
    truncated figure is worse than none.
    """
    out = stream if stream is not None else sys.stdout
    keys = list(columns) if columns else sorted({k for row in rows for k in row})

    if fmt is OutputFormat.JSON:
        json.dump([{k: _plain(r.get(k)) for k in keys} for r in rows], out, indent=2)
        out.write("\n")
        return

    if fmt is OutputFormat.CSV:
        writer = csv.DictWriter(out, fieldnames=keys, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _plain(row.get(k)) for k in keys})
        return

    console = Console(file=out)
    if not rows:
        console.print("[dim]No results.[/dim]")
        return
    table = Table(title=title, header_style="bold")
    for key in keys:
        table.add_column(key, overflow="fold" if fold else "ellipsis")
    for row in rows:
        cells = ["" if row.get(k) is None else str(_plain(row.get(k))) for k in keys]
        table.add_row(*(cells if markup else [Text(c) for c in cells]))
    console.print(table)
