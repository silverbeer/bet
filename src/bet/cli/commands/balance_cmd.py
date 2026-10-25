"""``bet balance`` — reported balances, and whether the bet ledger explains them.

SB-1333. ``bet balance add`` appends one dated snapshot of an account's cash
balance, as the sportsbook reports it. ``bet balance`` on its own reconciles
each account's latest snapshot against the one before it: the previous
balance, plus deposits, less withdrawals, less cash staked, plus cash
returned, should equal the reported balance. Each account's status is decided
over the whole span since its first snapshot (``cumulative_gap``); the latest
interval's own gap is a diagnostic. A gap flags a missing or miscaptured bet,
deposit or withdrawal (see ``bet.analytics.balance``).

One row per account, then a ``TOTAL`` row across them: balances are held at
several books at once, and the question "where is my money" spans all of
them.

Snapshots are transcribed exactly like bets — ``--capture-method`` and
``--source-file`` behave as on ``bet add`` — and are append-only. Correcting
a wrong one is SB-703.

A gap does not change the exit status. It is a finding about the data, not a
failure of the command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from bet.analytics.balance import COLUMNS, DIAGNOSTIC_COLUMNS
from bet.cli.backend import open_backend
from bet.cli.capture import (
    check_provenance,
    markup_safe_labels,
    parse_capture_method,
    parse_local_timestamp,
    parse_money,
)
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import OutputFormat, resolve
from bet.errors import UsageError
from bet.models.ownership import SportsbookAccount
from bet.services.balance import BalanceQuery, account_name
from bet.services.ledger import BalanceSnapshotRequest

# Notices go to stderr so they never share a stream with --format json/csv.
notices = Console(stderr=True)

app = typer.Typer(
    name="balance",
    help="Account balances, and reconciliation against the bet ledger.",
    invoke_without_command=True,
)

# Shared with bet deposit / bet withdraw.
AT_HELP = (
    "ISO 8601 time. A time without an offset, or a bare date, is read in this "
    "machine's local time zone. At most 5 minutes in the future. Default: now."
)

ADD_COLUMNS = [
    "id",
    "account",
    "observed_at",
    "cash_balance",
    "bonus_balance",
    "capture_method",
    "source_file_id",
]


def created_notice(account: SportsbookAccount) -> None:
    """Announce an auto-created account. The label is user text, never markup."""
    notices.print(f"Created account {escape(account_name(account))}.")


# ------------------------------------------------------------------ report


@app.callback()
def report(
    ctx: typer.Context,
    sportsbook: Annotated[
        str | None, typer.Option("--sportsbook", help="Only accounts at this sportsbook.")
    ] = None,
    account_label: Annotated[
        str | None, typer.Option("--account-label", help="Only the account with this label.")
    ] = None,
    history: Annotated[
        bool,
        typer.Option("--history", help="Every snapshot interval, not just the latest."),
    ] = False,
) -> None:
    """Latest balance per account, its reconciliation, and a cross-account total.

    The table shows the interval diagnostics (previous balance, staked,
    returned, expected, interval gap) only with --history or --verbose;
    --format json and csv always carry every column.
    """
    if ctx.invoked_subcommand is not None:
        misplaced = [
            name
            for name, given in (
                ("--sportsbook", sportsbook is not None),
                ("--account-label", account_label is not None),
                ("--history", history),
            )
            if given
        ]
        if misplaced:
            sub = ctx.invoked_subcommand
            raise UsageError(
                f"{', '.join(misplaced)} given before `{sub}` would be ignored.",
                remediation=f"Options for `bet balance {sub}` go after it, e.g. "
                f"`bet balance {sub} --sportsbook fanduel`. --history belongs to the "
                "report alone: `bet balance --history`.",
            )
        return

    resolved = resolve()
    options = options_from(ctx)
    query = BalanceQuery(
        sportsbook=sportsbook or options.sportsbook,
        account_label=account_label,
        history=history,
    )

    # The sportsbook is checked before the local identity, as it always was.
    with open_backend(resolved, migrated=True, defer_scope=True) as backend:
        rows = backend.balances(query)

    columns = COLUMNS
    if options.fmt is OutputFormat.TABLE and not options.verbose:
        # --history judges each row by its own interval, so it keeps that gap.
        hidden = set(DIAGNOSTIC_COLUMNS) - ({"gap"} if history else set())
        columns = [c for c in COLUMNS if c not in hidden]
    render(rows, columns=columns, fmt=options.fmt, title="bet balance", markup=False, fold=True)


# --------------------------------------------------------------------- add


@app.command("add")
def add(
    ctx: typer.Context,
    sportsbook: Annotated[str, typer.Option("--sportsbook", help="Sportsbook code, e.g. fanduel.")],
    amount: Annotated[
        str, typer.Option("--amount", help="Cash balance as reported. Excludes bonus credit.")
    ],
    account_label: Annotated[
        str | None,
        typer.Option(
            "--account-label",
            help="Which account at the sportsbook, ignoring case and spacing. Names a new "
            "account only when the sportsbook has none yet.",
        ),
    ] = None,
    bonus_amount: Annotated[
        str | None,
        typer.Option(
            "--bonus-amount",
            help="Bonus / free-bet credit shown alongside. Recorded, never reconciled.",
        ),
    ] = None,
    at: Annotated[
        str | None,
        typer.Option("--at", help="When the balance was observed. " + AT_HELP),
    ] = None,
    capture_method: Annotated[
        str,
        typer.Option(
            "--capture-method",
            help="How the figure was obtained: manual, screenshot, pdf, statement, export or api.",
        ),
    ] = "manual",
    source_file: Annotated[
        Path | None,
        typer.Option(
            "--source-file",
            help="The screenshot or statement the balance was read from. Archived unchanged. "
            "Requires a non-manual --capture-method.",
        ),
    ] = None,
    notes: Annotated[str | None, typer.Option("--notes", help="Free-text note.")] = None,
) -> None:
    """Append a dated balance snapshot for one account."""
    resolved = resolve()
    fmt = options_from(ctx).fmt

    method = parse_capture_method(capture_method)
    check_provenance(method, source_file, what="balance")
    cash_balance = parse_money(amount, field_name="--amount")
    bonus_balance = parse_money(bonus_amount, field_name="--bonus-amount") if bonus_amount else None
    observed_at = parse_local_timestamp(at, option="--at")

    request = BalanceSnapshotRequest(
        sportsbook=sportsbook,
        account_label=account_label,
        cash_balance=cash_balance,
        bonus_balance=bonus_balance,
        observed_at=observed_at,
        capture_method=method,
        notes=notes,
    )

    with open_backend(resolved, migrated=True) as backend, markup_safe_labels():
        recorded = backend.add_balance(request, source_file)

    account, snapshot = recorded.account, recorded.snapshot
    if recorded.account_is_new:
        created_notice(account)
    row = {
        "id": str(snapshot.id),
        "account": account_name(account),
        "observed_at": snapshot.observed_at,
        "cash_balance": snapshot.cash_balance,
        "bonus_balance": snapshot.bonus_balance,
        "capture_method": snapshot.capture_method,
        "source_file_id": str(snapshot.source_file_id) if snapshot.source_file_id else None,
    }
    render([row], columns=ADD_COLUMNS, fmt=fmt, title="bet balance add", markup=False, fold=True)


__all__ = ["AT_HELP", "account_name", "add", "app", "created_notice", "report"]
