"""``bet deposit`` / ``bet withdraw`` — cash moved into or out of an account.

SB-1333. The cash-flow half of balance reconciliation: without deposits and
withdrawals, every change in a reported balance would look like betting P&L,
and every deposit like a gap. Append-only, one row per movement, transcribed
under the same ``--capture-method`` / ``--source-file`` rules as a bet.

Bonus and free-bet credit is not a deposit. An operator awarding one moves no
cash, so it has no place here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bet.cli.capture import (
    check_provenance,
    markup_safe_labels,
    parse_capture_method,
    parse_local_timestamp,
    parse_money,
)
from bet.cli.commands.balance_cmd import AT_HELP, account_name, created_notice
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.models.balance import CashFlowKind
from bet.services.ledger import CashFlowRequest, record_cash_flow, require_moves_money
from bet.services.sources import SourceToArchive

COLUMNS = [
    "id",
    "account",
    "kind",
    "occurred_at",
    "amount",
    "capture_method",
    "source_file_id",
]

SportsbookOption = Annotated[
    str, typer.Option("--sportsbook", help="Sportsbook code, e.g. fanduel.")
]
AmountOption = Annotated[str, typer.Option("--amount", help="Amount moved. Always positive.")]
AccountLabelOption = Annotated[
    str | None,
    typer.Option(
        "--account-label",
        help="Which account at the sportsbook, ignoring case and spacing. Names a new "
        "account only when the sportsbook has none yet.",
    ),
]
AtOption = Annotated[
    str | None,
    typer.Option("--at", help="When the money moved. " + AT_HELP),
]
CaptureMethodOption = Annotated[
    str,
    typer.Option(
        "--capture-method",
        help="How the figure was obtained: manual, screenshot, pdf, statement, export or api.",
    ),
]
SourceFileOption = Annotated[
    Path | None,
    typer.Option(
        "--source-file",
        help="The screenshot or statement this was read from. Archived unchanged. "
        "Requires a non-manual --capture-method.",
    ),
]
NotesOption = Annotated[str | None, typer.Option("--notes", help="Free-text note.")]


def _record(
    ctx: typer.Context,
    kind: CashFlowKind,
    *,
    sportsbook: str,
    amount: str,
    account_label: str | None,
    at: str | None,
    capture_method: str,
    source_file: Path | None,
    notes: str | None,
) -> None:
    resolved = resolve()
    fmt = options_from(ctx).fmt

    method = parse_capture_method(capture_method)
    check_provenance(method, source_file, what=kind)
    # Checked again by the service; here so it is reported before --at is read.
    value = require_moves_money(kind, parse_money(amount, field_name="--amount"))
    occurred_at = parse_local_timestamp(at, option="--at")
    request = CashFlowRequest(
        kind=kind,
        sportsbook=sportsbook,
        amount=value,
        account_label=account_label,
        occurred_at=occurred_at,
        capture_method=method,
        notes=notes,
    )

    with connect(resolved.settings) as conn:
        migrator.require_current(conn)
        scope = identity.resolve_scope(conn, resolved)
        archive_dir = resolved.settings.source_archive_dir
        assert archive_dir is not None  # Settings derives it from data_dir
        source = (
            SourceToArchive(archive_dir=archive_dir, path=source_file)
            if source_file is not None
            else None
        )
        with markup_safe_labels():
            recorded = record_cash_flow(Warehouse(conn, scope), request, source=source)

    account, flow = recorded.account, recorded.cash_flow
    if recorded.account_is_new:
        created_notice(account)
    row = {
        "id": str(flow.id),
        "account": account_name(account),
        "kind": flow.kind,
        "occurred_at": flow.occurred_at,
        "amount": flow.amount,
        "capture_method": flow.capture_method,
        "source_file_id": str(flow.source_file_id) if flow.source_file_id else None,
    }
    render([row], columns=COLUMNS, fmt=fmt, title=f"bet {kind}", markup=False, fold=True)


def deposit(
    ctx: typer.Context,
    sportsbook: SportsbookOption,
    amount: AmountOption,
    account_label: AccountLabelOption = None,
    at: AtOption = None,
    capture_method: CaptureMethodOption = "manual",
    source_file: SourceFileOption = None,
    notes: NotesOption = None,
) -> None:
    """Record cash deposited into a sportsbook account."""
    _record(
        ctx,
        "deposit",
        sportsbook=sportsbook,
        amount=amount,
        account_label=account_label,
        at=at,
        capture_method=capture_method,
        source_file=source_file,
        notes=notes,
    )


def withdraw(
    ctx: typer.Context,
    sportsbook: SportsbookOption,
    amount: AmountOption,
    account_label: AccountLabelOption = None,
    at: AtOption = None,
    capture_method: CaptureMethodOption = "manual",
    source_file: SourceFileOption = None,
    notes: NotesOption = None,
) -> None:
    """Record cash withdrawn from a sportsbook account."""
    _record(
        ctx,
        "withdrawal",
        sportsbook=sportsbook,
        amount=amount,
        account_label=account_label,
        at=at,
        capture_method=capture_method,
        source_file=source_file,
        notes=notes,
    )


__all__ = ["deposit", "withdraw"]
