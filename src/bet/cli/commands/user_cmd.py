"""``bet user`` — create API users and set their passwords (ADR 0002 section 5).

Local mode only: these open the warehouse file directly, and are how the
operator invites someone, since the API has no signup endpoint. Both act on
the tenant ``bet init`` created. ``set-password`` works for the user ``bet
init`` created too, who owns every bet recorded before credentials existed.

A password is never an argument (it would land in shell history and ``ps``).
It is prompted for twice with hidden input, or read from standard input with
``--password-stdin`` for scripting; a single trailing newline is dropped.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.database import migrator
from bet.database.connection import connect
from bet.errors import UsageError
from bet.services.users import create_user, set_password_for

notices = Console(stderr=True)

app = typer.Typer(name="user", help="Manage API users (local mode only).", no_args_is_help=True)


@app.command("create")
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Display name, which is also the login name.")],
) -> None:
    """Add a user to the local tenant. Set their password with `bet user set-password`."""
    resolved = resolve()
    with connect(resolved.settings, create=False) as conn:
        migrator.require_current(conn)
        scope = create_user(conn, name)

    render(
        [{"user": name.strip(), "user_id": str(scope.user_id), "action": "created"}],
        columns=["user", "user_id", "action"],
        fmt=options_from(ctx).fmt,
        title="bet user create",
        markup=False,
    )
    notices.print(f"Set a password with: bet user set-password {escape(repr(name.strip()))}")


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        raw = sys.stdin.read()
        if raw.endswith("\n"):
            raw = raw[:-1].removesuffix("\r")
        if not raw:
            raise UsageError("no password was given on standard input.")
        return raw
    password: str = typer.prompt(
        "New password", hide_input=True, confirmation_prompt="Repeat the password"
    )
    return password


@app.command("set-password")
def set_password(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The user's display name.")],
    password_stdin: Annotated[
        bool,
        typer.Option("--password-stdin", help="Read the password from standard input."),
    ] = False,
) -> None:
    """Set or change a user's API password."""
    resolved = resolve()
    with connect(resolved.settings, create=False) as conn:
        migrator.require_current(conn)
        # After the checks above, so a wrong warehouse fails before prompting.
        password = _read_password(password_stdin)
        result = set_password_for(conn, name, password)

    render(
        [
            {
                "user": result.display_name,
                "user_id": str(result.scope.user_id),
                "action": "password changed" if result.rotated else "password set",
            }
        ],
        columns=["user", "user_id", "action"],
        fmt=options_from(ctx).fmt,
        title="bet user set-password",
        markup=False,
    )


__all__ = ["app"]
