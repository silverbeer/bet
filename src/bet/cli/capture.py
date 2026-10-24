"""Input handling shared by every command that captures a figure off a slip.

``bet add`` was the only capture command until SB-1333 added balances,
deposits and withdrawals. Those are transcribed the same way a bet is -- typed
at the counter or read off a screenshot -- so they obey the same rules: money
parses to cents half-up, ``capture_method`` says how
the figure was obtained, and a ``--source-file`` is archived and cited. One
implementation, so the rules cannot drift between commands.

The rules themselves are ``bet.services.capture`` and ``bet.services.sources``
since SB-1395; this is the text and prompt side of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import CaptureMethod
from bet.models.ownership import SportsbookAccount
from bet.services.capture import (
    CAPTURE_METHODS,
    UnknownLabelError,
    new_account,
    parse_capture_method,
    require_not_future,
    require_seeded,
)
from bet.services.sources import refuse_manual_source, require_file
from bet.services.specs import parse_money

console = Console()
# Warnings go to stderr so they never share a stream with --format json/csv.
notices = Console(stderr=True)


# ------------------------------------------------------------------ parsing


def parse_timestamp(raw: str | None, *, option: str) -> datetime:
    """Parse an ISO 8601 ``option`` value. Absent is now; naive is UTC."""
    if raw is None:
        return datetime.now(UTC)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise UsageError(
            f"{raw!r} is not a valid {option} timestamp.",
            remediation="Use ISO 8601, e.g. 2026-09-08T19:30:00-04:00.",
        ) from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def parse_local_timestamp(raw: str | None, *, option: str) -> datetime:
    """Parse an ISO 8601 ``option`` value. Absent is now; naive is local time.

    For balances, deposits and withdrawals, which are typed off a screen in the
    user's own time zone. A date-only ``2026-10-01`` read as UTC is the previous
    evening in the Americas, which moves the row into the wrong interval. A
    moment more than ``FUTURE_TOLERANCE`` ahead of the clock is refused.

    ``bet add --placed-at`` still reads naive as UTC (``parse_timestamp``).
    """
    if raw is None:
        return datetime.now(UTC)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise UsageError(
            f"{raw!r} is not a valid {option} timestamp.",
            remediation="Use ISO 8601, e.g. 2026-09-08T19:30:00-04:00.",
        ) from exc
    if value.tzinfo is None:
        value = value.astimezone()  # naive -> the machine's local time zone
    return require_not_future(value, field_name=option, shown=raw)


def check_provenance(
    capture_method: CaptureMethod, source_file: Path | None, *, what: str = "bet"
) -> None:
    """Refuse the incoherent combination; warn about the merely incomplete one.

    The refusals are the services' own (``bet.services.sources``), checked here
    too so they fail before the warehouse is opened and before any prompting.

    The reverse — a transcribed figure with nothing archived — is a real loss
    but not a contradiction: the screenshot may genuinely be gone. It warns, so
    the row is still recorded and the gap is visible, because refusing it would
    cost the record as well as the evidence.
    """
    if source_file is not None:
        refuse_manual_source(capture_method, what=what)
        require_file(source_file)
    elif capture_method != "manual":
        notices.print(
            f"[yellow]Warning:[/yellow] recording a {capture_method} {what} with no "
            "--source-file. Nothing will be archived, so these figures cannot be checked "
            "against their source."
        )


# ------------------------------------------------------------ account lookup


@contextmanager
def markup_safe_labels() -> Iterator[None]:
    """Escape the account labels in an unknown-label error.

    Labels are user text; the error printer reads Rich markup.
    """
    try:
        yield
    except UnknownLabelError as exc:
        raise UsageError(
            f"no {exc.sportsbook_code} account is labelled {escape(repr(exc.label))}.",
            remediation="Known labels: " + escape(", ".join(exc.known_labels)),
        ) from exc


def choose_account(w: Warehouse, *, sportsbook_code: str) -> SportsbookAccount:
    """Pick, or name, the account an interactively entered bet attaches to.

    Prompts only where the flag-driven path would refuse or default: several
    accounts to choose between, or none yet and a label to give the new one.
    The returned account's label then resolves to it through the same service
    every front end uses; nothing is written here.
    """
    sportsbook_code = sportsbook_code.strip().lower()
    require_seeded(w, sportsbook_code)
    accounts = w.accounts.for_sportsbook(sportsbook_code)
    if not accounts:
        label = Prompt.ask("Account label", default=sportsbook_code)
        return new_account(w, sportsbook_code, label)
    if len(accounts) == 1:
        return accounts[0]
    chosen = Prompt.ask(f"Account for {sportsbook_code}", choices=[a.label for a in accounts])
    return next(a for a in accounts if a.label == chosen)


__all__ = [
    "CAPTURE_METHODS",
    "check_provenance",
    "choose_account",
    "markup_safe_labels",
    "new_account",
    "parse_capture_method",
    "parse_local_timestamp",
    "parse_money",
    "parse_timestamp",
]
