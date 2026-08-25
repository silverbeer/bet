"""``bet config`` — inspect and edit resolved configuration."""

from __future__ import annotations

import tomllib
from typing import Annotated, Any

import typer

from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve, write_config
from bet.errors import ConfigError

app = typer.Typer(name="config", help="Inspect and edit configuration.", no_args_is_help=True)

SETTABLE = (
    "data_dir",
    "db_path",
    "source_archive_dir",
    "backup_dir",
    "state_dir",
    "default_format",
)


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key, child in value.items():
            out.extend(_flatten(f"{prefix}.{key}" if prefix else key, child))
        return out
    return [(prefix, value)]


@app.command("show")
def show(ctx: typer.Context) -> None:
    """Show resolved configuration and where each value came from."""
    resolved = resolve()
    fmt = options_from(ctx).fmt

    rows = [
        {"setting": key, "value": value, "source": resolved.source_of(key).value}
        for key, value in _flatten("", resolved.settings.model_dump(mode="json"))
    ]
    render(rows, columns=["setting", "value", "source"], fmt=fmt, title="bet config")


@app.command("path")
def path() -> None:
    """Print the path of the configuration file."""
    typer.echo(str(resolve().config_path))


@app.command("set")
def set_value(
    key: Annotated[str, typer.Argument(help=f"One of: {', '.join(SETTABLE)}")],
    value: Annotated[str, typer.Argument(help="The value to store.")],
) -> None:
    """Write a value to the configuration file.

    The new configuration is validated before it is written, so an invalid
    value — a data directory inside a git work tree, for instance — is rejected
    rather than persisted for the next command to trip over.
    """
    if key not in SETTABLE:
        raise ConfigError(
            f"{key!r} is not a settable configuration key.",
            remediation="Settable keys are:\n    " + "\n    ".join(SETTABLE),
        )

    config_path = resolve().config_path
    existing: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("rb") as handle:
            existing = tomllib.load(handle)

    candidate = {**existing, key: value}

    # Validate before writing. Persisting a bad value would make every
    # subsequent command fail until someone hand-edited the file.
    resolve(config_path=None, cli_overrides=candidate)

    write_config(config_path, candidate)
    typer.echo(f"{key} = {value}   ->  {config_path}")


__all__ = ["app"]
