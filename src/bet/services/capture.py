"""Rules shared by every write that records a figure off a slip.

A bet, a balance snapshot and a cash flow are all transcribed the same way --
typed at the counter or read off a screenshot -- so they obey the same rules:
money is cents half-up and fits ``DECIMAL(12,2)``, a recorded moment is not in
the future, and the row attaches to an account resolved the same way. One
implementation, so the rules cannot drift between writes or front ends.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import get_args
from uuid import uuid4

from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import CaptureMethod
from bet.models.ownership import SportsbookAccount

TWO_PLACES = Decimal("0.01")
# DECIMAL(12,2): ten integer digits. Anything at or above this cannot be stored.
MONEY_LIMIT = Decimal("1e10")
# How far ahead of the clock a recorded moment may be. Enough for clock skew;
# far too little for a year typo, which would otherwise sort after every real
# snapshot and become the "latest" forever.
FUTURE_TOLERANCE = timedelta(minutes=5)

CAPTURE_METHODS: tuple[str, ...] = get_args(CaptureMethod)


def parse_capture_method(raw: str) -> CaptureMethod:
    """``raw`` as a capture method, or refused with the known ones."""
    value = raw.strip().lower()
    if value not in CAPTURE_METHODS:
        raise UsageError(
            f"{raw!r} is not a known capture method.",
            remediation="Known methods: " + ", ".join(sorted(CAPTURE_METHODS)),
        )
    return value  # type: ignore[return-value]


# -------------------------------------------------------------------- money


def check_money(value: Decimal, *, field_name: str, shown: str | None = None) -> Decimal:
    """``value`` as a storable amount: finite, cents half-up, non-negative.

    ``shown`` is how the amount is quoted in an error -- the text as typed,
    when there was any.
    """
    quoted = repr(shown if shown is not None else str(value))
    if not value.is_finite():  # NaN, sNaN, Infinity: Decimal accepts all three
        raise UsageError(f"{quoted} is not a valid amount for {field_name}.")
    if abs(value) < MONEY_LIMIT:
        value = value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if abs(value) >= MONEY_LIMIT:  # checked again: 9999999999.995 rounds up to it
        raise UsageError(
            f"{quoted} is too large for {field_name}.",
            remediation="Amounts are stored with at most ten digits before the decimal point.",
        )
    if value < 0:
        raise UsageError(f"{field_name} cannot be negative.")
    return value


# --------------------------------------------------------------------- time


def require_not_future(value: datetime, *, field_name: str, shown: str | None = None) -> datetime:
    """Refuse a moment more than ``FUTURE_TOLERANCE`` ahead of the clock."""
    if value > datetime.now(UTC) + FUTURE_TOLERANCE:
        raise UsageError(
            f"{field_name} {shown if shown is not None else value.isoformat()} is in the future.",
            remediation=f"Pass the moment it actually happened, or omit {field_name} for now.",
        )
    return value


def now_if_absent(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(UTC)


# ------------------------------------------------------------ account lookup


class UnknownLabelError(UsageError):
    """A label matching none of a sportsbook's several accounts.

    Its own class because the label is user text: a front end that renders
    markup has to escape it, and needs the parts to do so.
    """

    def __init__(self, sportsbook_code: str, label: str, known_labels: list[str]) -> None:
        self.sportsbook_code = sportsbook_code
        self.label = label
        self.known_labels = known_labels
        super().__init__(
            f"no {sportsbook_code} account is labelled {label!r}.",
            remediation="Known labels: " + ", ".join(known_labels),
        )


def resolve_account(
    w: Warehouse,
    *,
    sportsbook_code: str,
    account_label: str | None,
    strict_label: bool = False,
) -> tuple[SportsbookAccount, bool]:
    """Find the account a row attaches to, constructing one if none exists yet.

    ``sportsbook_code`` is trimmed and lowercased here, once, for every caller.

    ``strict_label`` is for balances and cash flows, where a row filed against
    a phantom account silently breaks two reconciliations. Labels then match
    ignoring case and whitespace, and an account is created only when the
    sportsbook has none at all; a label that matches none of several existing
    accounts is refused with the labels that do exist.

    Reads only -- a newly constructed account is written by the caller inside
    the same transaction as the row that needs it. There is no ``sportsbook
    accounts create`` command yet (SB-701), so this is the only way one gets
    made today.
    """
    sportsbook_code = sportsbook_code.strip().lower()
    require_seeded(w, sportsbook_code)

    accounts = w.accounts.for_sportsbook(sportsbook_code)

    if strict_label and account_label is not None:
        account_label = account_label.strip() or None
    if strict_label and account_label and accounts:
        wanted = _label_key(account_label)
        for account in accounts:
            if _label_key(account.label) == wanted:
                return account, False
        raise UnknownLabelError(sportsbook_code, account_label, [a.label for a in accounts])

    if account_label:
        for account in accounts:
            if account.label == account_label:
                return account, False
        return new_account(w, sportsbook_code, account_label), True
    if len(accounts) == 1:
        return accounts[0], False
    if len(accounts) > 1:
        raise UsageError(
            f"multiple {sportsbook_code} accounts exist.",
            remediation="Disambiguate with --account-label. Known labels: "
            + ", ".join(a.label for a in accounts),
        )
    return new_account(w, sportsbook_code, sportsbook_code), True


def require_seeded(w: Warehouse, sportsbook_code: str) -> None:
    """Refuse a sportsbook code that is not in ``core.sportsbook``."""
    known = w.sportsbook_codes()
    if sportsbook_code not in known:
        raise UsageError(
            f"{sportsbook_code!r} is not a known sportsbook.",
            remediation="Known sportsbooks: " + ", ".join(sorted(known)),
        )


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
    "FUTURE_TOLERANCE",
    "MONEY_LIMIT",
    "TWO_PLACES",
    "UnknownLabelError",
    "check_money",
    "new_account",
    "now_if_absent",
    "parse_capture_method",
    "require_not_future",
    "require_seeded",
    "resolve_account",
]
