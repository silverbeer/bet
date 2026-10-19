"""Input handling shared by every command that captures a figure off a slip.

``bet add`` was the only capture command until SB-1333 added balances,
deposits and withdrawals. Those are transcribed the same way a bet is -- typed
at the counter or read off a screenshot -- so they obey the same rules: money
parses to cents half-up, ``capture_method`` says how
the figure was obtained, and a ``--source-file`` is archived and cited. One
implementation, so the rules cannot drift between commands.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, get_args
from uuid import uuid4

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from bet.database.repository import Warehouse, known_sportsbook_codes
from bet.errors import UsageError
from bet.models.bet import CaptureMethod
from bet.models.ownership import SportsbookAccount

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

console = Console()
# Warnings go to stderr so they never share a stream with --format json/csv.
notices = Console(stderr=True)

TWO_PLACES = Decimal("0.01")
# DECIMAL(12,2): ten integer digits. Anything at or above this cannot be stored.
MONEY_LIMIT = Decimal("1e10")
# How far ahead of the clock a recorded moment may be. Enough for clock skew;
# far too little for a year typo, which would otherwise sort after every real
# snapshot and become the "latest" forever.
FUTURE_TOLERANCE = timedelta(minutes=5)

CAPTURE_METHODS: tuple[str, ...] = get_args(CaptureMethod)


# ------------------------------------------------------------------ parsing


def parse_money(raw: str, *, field_name: str) -> Decimal:
    invalid = UsageError(f"{raw!r} is not a valid amount for {field_name}.")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise invalid from exc
    if not value.is_finite():  # NaN, sNaN, Infinity: Decimal accepts all three
        raise invalid
    if abs(value) < MONEY_LIMIT:
        value = value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if abs(value) >= MONEY_LIMIT:  # checked again: 9999999999.995 rounds up to it
        raise UsageError(
            f"{raw!r} is too large for {field_name}.",
            remediation="Amounts are stored with at most ten digits before the decimal point.",
        )
    if value < 0:
        raise UsageError(f"{field_name} cannot be negative.")
    return value


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
    if value > datetime.now(UTC) + FUTURE_TOLERANCE:
        raise UsageError(
            f"{option} {raw} is in the future.",
            remediation=f"Pass the moment it actually happened, or omit {option} for now.",
        )
    return value


def parse_capture_method(raw: str) -> CaptureMethod:
    value = raw.strip().lower()
    if value not in CAPTURE_METHODS:
        raise UsageError(
            f"{raw!r} is not a known capture method.",
            remediation="Known methods: " + ", ".join(sorted(CAPTURE_METHODS)),
        )
    return value  # type: ignore[return-value]


def check_provenance(
    capture_method: CaptureMethod, source_file: Path | None, *, what: str = "bet"
) -> None:
    """Refuse the incoherent combination; warn about the merely incomplete one.

    ``manual`` means a human typed the numbers. A file it was captured from is
    a contradiction, not extra detail, so it is an error rather than something
    to quietly accept.

    A path that is not a file fails here too, before the warehouse is opened
    and before any prompting, rather than from inside the write transaction
    where it would surface as a rollback rather than as bad input.

    The reverse — a transcribed figure with nothing archived — is a real loss
    but not a contradiction: the screenshot may genuinely be gone. It warns, so
    the row is still recorded and the gap is visible, because refusing it would
    cost the record as well as the evidence.
    """
    if source_file is not None and capture_method == "manual":
        raise UsageError(
            "--source-file contradicts --capture-method manual.",
            remediation=f"A manually typed {what} was not captured from a file. Pass "
            "--capture-method screenshot (or pdf, statement, export) alongside it.",
        )
    if source_file is not None and not source_file.expanduser().is_file():
        raise UsageError(
            f"{source_file} is not a file.",
            remediation="Pass --source-file the path to the screenshot or statement.",
        )
    if source_file is None and capture_method != "manual":
        notices.print(
            f"[yellow]Warning:[/yellow] recording a {capture_method} {what} with no "
            "--source-file. Nothing will be archived, so these figures cannot be checked "
            "against their source."
        )


# ------------------------------------------------------------ account lookup


def resolve_account(
    w: Warehouse,
    conn: DuckDBPyConnection,
    *,
    sportsbook_code: str,
    account_label: str | None,
    interactive: bool,
    strict_label: bool = False,
) -> tuple[SportsbookAccount, bool]:
    """Find the account a row attaches to, creating one if none exists yet.

    ``sportsbook_code`` is trimmed and lowercased here, once, for every caller.

    ``strict_label`` is for balances and cash flows, where a row filed against
    a phantom account silently breaks two reconciliations. Labels then match
    ignoring case and whitespace, and an account is created only when the
    sportsbook has none at all; a label that matches none of several existing
    accounts is refused with the labels that do exist.

    Reads only — a newly constructed account is written by the caller inside
    the same transaction as the row that needs it, so aborting at a confirm
    prompt leaves the warehouse untouched. There is no ``sportsbook accounts
    create`` command yet (SB-701), so this is the only way one gets made today.
    """
    sportsbook_code = sportsbook_code.strip().lower()
    known = known_sportsbook_codes(conn)
    if sportsbook_code not in known:
        raise UsageError(
            f"{sportsbook_code!r} is not a known sportsbook.",
            remediation="Known sportsbooks: " + ", ".join(sorted(known)),
        )

    accounts = w.accounts.for_sportsbook(sportsbook_code)

    if strict_label and account_label is not None:
        account_label = account_label.strip() or None
    if strict_label and account_label and accounts:
        wanted = _label_key(account_label)
        for account in accounts:
            if _label_key(account.label) == wanted:
                return account, False
        # Labels are user text; the error printer reads Rich markup.
        raise UsageError(
            f"no {sportsbook_code} account is labelled {escape(repr(account_label))}.",
            remediation="Known labels: " + escape(", ".join(a.label for a in accounts)),
        )
    if strict_label and account_label:
        return new_account(w, sportsbook_code, account_label), True

    if account_label:
        for account in accounts:
            if account.label == account_label:
                return account, False
        if not interactive:
            return new_account(w, sportsbook_code, account_label), True
    elif len(accounts) == 1:
        return accounts[0], False
    elif len(accounts) > 1:
        if not interactive:
            raise UsageError(
                f"multiple {sportsbook_code} accounts exist.",
                remediation="Disambiguate with --account-label. Known labels: "
                + ", ".join(a.label for a in accounts),
            )
        chosen = Prompt.ask(f"Account for {sportsbook_code}", choices=[a.label for a in accounts])
        return next(a for a in accounts if a.label == chosen), False

    if account_label:
        label = account_label
    elif interactive:
        label = Prompt.ask("Account label", default=sportsbook_code)
    else:
        label = sportsbook_code
    return new_account(w, sportsbook_code, label), True


def _label_key(label: str) -> str:
    return " ".join(label.split()).casefold()


def new_account(w: Warehouse, sportsbook_code: str, label: str) -> SportsbookAccount:
    return SportsbookAccount(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=uuid4(),
        sportsbook_code=sportsbook_code,
        label=label,
    )


__all__ = [
    "CAPTURE_METHODS",
    "check_provenance",
    "new_account",
    "parse_capture_method",
    "parse_local_timestamp",
    "parse_money",
    "parse_timestamp",
    "resolve_account",
]
