"""Per-invocation state shared by every command."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bet.config import OutputFormat, ResolvedConfig


@dataclass(slots=True)
class GlobalOptions:
    """The options every command accepts, per plan section 5."""

    db: Path | None = None
    fmt: OutputFormat = OutputFormat.TABLE
    since: date | None = None
    until: date | None = None
    sport: str | None = None
    sportsbook: str | None = None
    include_void: bool = False
    verbose: bool = False


@dataclass(slots=True)
class AppContext:
    """Resolved configuration plus the options this invocation was given."""

    config: ResolvedConfig
    options: GlobalOptions = field(default_factory=GlobalOptions)


def options_from(ctx: object) -> GlobalOptions:
    """Return the global options attached to a Typer context.

    Falls back to defaults so a command remains callable in tests and in-process
    without a full Typer invocation.
    """
    obj = getattr(ctx, "obj", None)
    if isinstance(obj, AppContext):
        return obj.options
    return GlobalOptions()
