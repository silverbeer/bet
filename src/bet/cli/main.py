"""Root Typer application, global options, and error handling.

Every command shares the option set from BET_IMPLEMENTATION_PLAN.md section 5,
and every deliberate failure exits with a stable code and actionable text. An
unexpected exception is reported as a bug rather than dressed up as a user
error — telling someone to fix their input when the fault is ours wastes their
time.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console
from rich.markup import escape

from bet import __version__, log
from bet.cli import tree
from bet.cli.commands import balance_cmd, bets_cmd, config_cmd, user_cmd
from bet.cli.commands.add_cmd import add as add_command
from bet.cli.commands.card_cmd import card as card_command
from bet.cli.commands.cashflow_cmd import deposit as deposit_command
from bet.cli.commands.cashflow_cmd import withdraw as withdraw_command
from bet.cli.commands.doctor import doctor as doctor_command
from bet.cli.commands.init_cmd import init as init_command
from bet.cli.commands.list_cmd import list_bets as list_command
from bet.cli.commands.settle_cmd import settle as settle_command
from bet.cli.commands.similar_cmd import similar as similar_command
from bet.cli.context import AppContext, GlobalOptions
from bet.config import OutputFormat, resolve
from bet.errors import BetError

# Typer 0.27 vendors click privately (typer._click); BadParameter's base is
# click's UsageError, which also covers unknown commands/options and missing
# required options. Reached through the public class to avoid the private path.
ClickUsageError = cast("type[Exception]", typer.BadParameter.__base__)

app = typer.Typer(
    name="bet",
    help="Personal betting intelligence platform.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

DATE_FORMAT = "%Y-%m-%d"

app.add_typer(config_cmd.app, name="config")
app.command("init", help="Create the local warehouse and apply migrations.")(init_command)
app.command("doctor", help="Check that this installation is healthy.")(doctor_command)
app.command("add", help="Record a bet the moment it is placed.")(add_command)
app.command("list", help="List bets, newest first.")(list_command)
app.command("settle", help="Record the outcome of a bet.")(settle_command)
app.command("similar", help="Your settled record on bets comparable to a proposed one.")(
    similar_command
)
app.command("card", help="Shareable summary of recent betting.")(card_command)
app.add_typer(bets_cmd.app, name="bets")
app.add_typer(balance_cmd.app, name="balance")
app.command("deposit", help="Record cash deposited into a sportsbook account.")(deposit_command)
app.command("withdraw", help="Record cash withdrawn from a sportsbook account.")(withdraw_command)
app.add_typer(user_cmd.app, name="user")
tree.register(app)


def _as_date(value: datetime | None) -> date | None:
    """Typer parses dates as datetimes; the domain only ever wants the date."""
    return value.date() if value is not None else None


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Path | None, typer.Option("--db", help="Path to the warehouse.")] = None,
    fmt: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.")
    ] = OutputFormat.TABLE,
    since: Annotated[
        datetime | None,
        typer.Option("--since", formats=[DATE_FORMAT], help="Only bets placed on or after."),
    ] = None,
    until: Annotated[
        datetime | None,
        typer.Option("--until", formats=[DATE_FORMAT], help="Only bets placed on or before."),
    ] = None,
    sport: Annotated[str | None, typer.Option("--sport", help="Restrict to one sport.")] = None,
    sportsbook: Annotated[
        str | None, typer.Option("--sportsbook", help="Restrict to one sportsbook.")
    ] = None,
    include_void: Annotated[
        bool, typer.Option("--include-void", help="Include voided bets, excluded by default.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Personal betting intelligence platform."""
    options = GlobalOptions(
        db=db,
        fmt=fmt,
        since=_as_date(since),
        until=_as_date(until),
        sport=sport,
        sportsbook=sportsbook,
        include_void=include_void,
        verbose=verbose,
    )

    # Machine-readable output must never share a stream with logs.
    log.configure(verbose=verbose, json_logs=None if fmt is OutputFormat.TABLE else True)
    log.bind(command=ctx.invoked_subcommand)

    ctx.obj = AppContext(
        config=resolve(cli_overrides={"db_path": db} if db else None),
        options=options,
    )


def run() -> None:
    """Console-script entry point with the shared error contract."""
    console = Console(stderr=True)
    try:
        app(standalone_mode=False)
    except BetError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc.message}")
        if exc.remediation:
            console.print(exc.remediation)
        raise SystemExit(exc.exit_code) from exc
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from exc
    except ClickUsageError as exc:
        # Unknown command/option and missing required options, as well as
        # BadParameter, are the user's input -- never a bug in BET.
        console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
        console.print("Run `bet --help` for the available commands and options.")
        raise SystemExit(64) from exc  # sysexits.h EX_USAGE
    except typer.Abort as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(64) from exc  # sysexits.h EX_USAGE
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow]")
        raise SystemExit(130) from None
    except Exception as exc:
        console.print(f"[bold red]Internal error:[/bold red] {exc}")
        console.print("This is a bug in BET, not a problem with your input.")
        raise SystemExit(70) from exc  # sysexits.h EX_SOFTWARE


if __name__ == "__main__":  # pragma: no cover
    run()
