"""``bet login`` / ``bet logout`` -- sign this CLI in to the BET API (ADR 0002 section 3).

Remote mode only: they need ``api_url``. ``login`` exchanges a user name and
password for an access and a refresh token and keeps both in the macOS
Keychain (``bet.client.tokens``); every remote command then sends the access
token and refreshes it when it lapses. ``logout`` revokes the refresh token on
the server and deletes both from the Keychain -- and still deletes them when
the server cannot be reached, since a session nobody can find is the goal.

The password is prompted for with hidden input, or read from standard input
with ``--password-stdin`` for scripting (a single trailing newline dropped).
It is never an argument, and neither it nor a token is ever printed.
"""

from __future__ import annotations

import getpass
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from bet.cli.backend import api_session, require_remote
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve
from bet.errors import ApiUnreachableError, BetError, UsageError

notices = Console(stderr=True)

COLUMNS = ["user", "api_url", "action"]


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        raw = sys.stdin.read()
        if raw.endswith("\n"):
            raw = raw[:-1].removesuffix("\r")
    else:
        raw = getpass.getpass("Password: ")
    if not raw:
        raise UsageError("no password was given.")
    return raw


def login(
    ctx: typer.Context,
    username: Annotated[
        str | None, typer.Option("--username", "-u", help="Your BET user name.")
    ] = None,
    password_stdin: Annotated[
        bool,
        typer.Option("--password-stdin", help="Read the password from standard input."),
    ] = False,
) -> None:
    """Sign in to the BET API at api_url and keep the tokens in the Keychain."""
    url = require_remote(resolve(), "bet login")
    name = username if username is not None else typer.prompt("Username", err=True)
    name = name.strip()
    if not name:
        raise UsageError("no user name was given.")
    password = _read_password(password_stdin)

    with api_session(url) as client:
        client.login(name, password)

    render(
        [{"user": name, "api_url": url, "action": "logged in"}],
        columns=COLUMNS,
        fmt=options_from(ctx).fmt,
        title="bet login",
        markup=False,
    )


def logout(ctx: typer.Context) -> None:
    """Revoke this CLI's session at api_url and delete its tokens from the Keychain."""
    url = require_remote(resolve(), "bet logout")
    with api_session(url) as client:
        session = client.tokens.load()
        action = "not logged in"
        if session is not None:
            action = "logged out"
            try:
                client.revoke(session)
            except ApiUnreachableError:
                action = "logged out locally"
                notices.print(
                    f"[yellow]Warning:[/yellow] could not reach {escape(url)}; the session "
                    "was not revoked there and lapses when its refresh token expires."
                )
            except BetError as exc:
                action = "logged out locally"
                notices.print(
                    f"[yellow]Warning:[/yellow] the server refused to revoke the session: "
                    f"{escape(exc.message)}"
                )
            finally:
                client.tokens.clear()

    render(
        [{"user": session.username if session else "", "api_url": url, "action": action}],
        columns=COLUMNS,
        fmt=options_from(ctx).fmt,
        title="bet logout",
        markup=False,
    )


__all__ = ["login", "logout"]
