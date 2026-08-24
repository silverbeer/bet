"""Smoke tests for the package skeleton."""

import importlib

import bet

SUBPACKAGES = [
    "agents",
    "analytics",
    "cli",
    "database",
    "extraction",
    "importers",
    "models",
    "reports",
    "services",
    "settlement",
    "sports",
]


def test_version_is_exposed() -> None:
    assert bet.__version__ == "0.1.0"


def test_every_subpackage_imports() -> None:
    for name in SUBPACKAGES:
        assert importlib.import_module(f"bet.{name}") is not None


def test_cli_reports_version() -> None:
    from typer.testing import CliRunner

    from bet.cli.main import app

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == bet.__version__
