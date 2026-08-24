"""Root Typer application.

Deliberately minimal. The full command surface, Rich output conventions and
error handling are SB-697; this exists so the ``bet`` console script declared
in ``pyproject.toml`` resolves to something that runs.
"""

from typing import Annotated

import typer

from bet import __version__

app = typer.Typer(
    name="bet",
    help="Personal betting intelligence platform.",
    no_args_is_help=True,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version,
            is_eager=True,
            help="Show the installed BET version and exit.",
        ),
    ] = False,
) -> None:
    """Personal betting intelligence platform."""


if __name__ == "__main__":  # pragma: no cover
    app()
