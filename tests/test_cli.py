"""CLI contract: help tree, output formats, stdout purity, and exit codes.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bet.cli.main import app, run
from bet.cli.output import render
from bet.cli.tree import ROOT_STUBS, TREE, NotImplementedYetError
from bet.config import OutputFormat
from bet.errors import BetError, ConfigError, DataLocationError, UsageError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every invocation at a throwaway data directory outside git."""
    data = tmp_path / "data"
    data.mkdir()
    for name in list(dict(__import__("os").environ)):
        if name.startswith("BET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


# ------------------------------------------------------------------- help


def test_help_lists_every_planned_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in TREE:
        assert group in result.stdout, f"{group} missing from --help"


def test_help_lists_every_root_command() -> None:
    result = runner.invoke(app, ["--help"])
    for name in ROOT_STUBS:
        assert name in result.stdout


def test_version_is_reported() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


# ------------------------------------------------------- output formats

ROWS = [{"team": "BOS", "roi": "0.14"}, {"team": "NYY", "roi": "-0.08"}]


def test_json_output_is_parseable() -> None:
    buf = io.StringIO()
    render(ROWS, columns=["team", "roi"], fmt=OutputFormat.JSON, stream=buf)
    assert json.loads(buf.getvalue()) == ROWS


def test_csv_output_is_parseable() -> None:
    buf = io.StringIO()
    render(ROWS, columns=["team", "roi"], fmt=OutputFormat.CSV, stream=buf)
    assert list(csv.DictReader(io.StringIO(buf.getvalue()))) == ROWS


def test_table_output_contains_the_values() -> None:
    buf = io.StringIO()
    render(ROWS, columns=["team", "roi"], fmt=OutputFormat.TABLE, stream=buf)
    assert "BOS" in buf.getvalue()


def test_decimal_is_rendered_as_string_not_float() -> None:
    """Money must not acquire binary floating-point error on the way out."""
    from decimal import Decimal

    buf = io.StringIO()
    render([{"stake": Decimal("10.10")}], fmt=OutputFormat.JSON, stream=buf)
    assert json.loads(buf.getvalue()) == [{"stake": "10.10"}]


# -------------------------------------------------- stdout stays clean


def test_stdout_is_pure_json_under_format_json() -> None:
    """SB-696: logs go to stderr so machine-readable stdout is never corrupted."""
    result = runner.invoke(app, ["--format", "json", "config", "show"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert any(row["setting"] == "data_dir" for row in parsed)


def test_stdout_stays_pure_json_even_with_verbose_logging() -> None:
    result = runner.invoke(app, ["--format", "json", "--verbose", "config", "show"])
    assert result.exit_code == 0
    json.loads(result.stdout)


def test_config_show_reports_the_source_of_each_value() -> None:
    result = runner.invoke(app, ["--format", "json", "config", "show"])
    rows = {r["setting"]: r["source"] for r in json.loads(result.stdout)}
    assert rows["data_dir"] == "environment"


# ------------------------------------------------------------ exit codes


def exit_code_of(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """Run the real console-script entry point and return its exit code.

    CliRunner invokes the Typer app directly and never reaches ``run()``, which
    is where BetError is mapped onto an exit code. Exercising ``run()`` is the
    only way to test the contract users actually get.
    """
    monkeypatch.setattr("sys.argv", ["bet", *argv])
    try:
        run()
    except SystemExit as exit_call:
        return int(exit_call.code or 0)
    return 0  # returning normally is success; the console script exits 0


def test_unimplemented_command_exits_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert exit_code_of(["roi"], monkeypatch) == NotImplementedYetError.exit_code


def test_unimplemented_subcommand_names_its_ticket() -> None:
    result = runner.invoke(app, ["bets", "show"])
    assert isinstance(result.exception, NotImplementedYetError)
    assert "SB-" in (result.exception.remediation or "")


def test_data_location_error_exit_code_reaches_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety failure must be distinguishable by a script, not just by eye."""
    import subprocess

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    monkeypatch.setenv("BET_DATA_DIR", str(repo / "data"))

    assert exit_code_of(["config", "show"], monkeypatch) == DataLocationError.exit_code


def test_usage_error_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    assert exit_code_of(["--format", "nope", "config", "path"], monkeypatch) == 64


def test_successful_command_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    assert exit_code_of(["config", "path"], monkeypatch) == 0


def test_error_classes_have_distinct_stable_exit_codes() -> None:
    codes = {
        BetError: 1,
        ConfigError: 2,
        DataLocationError: 3,
        UsageError: 64,
        NotImplementedYetError: 69,
    }
    for cls, code in codes.items():
        assert cls.exit_code == code


def test_config_path_is_printed() -> None:
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert result.stdout.strip().endswith("config.toml")


def test_config_set_rejects_an_unknown_key() -> None:
    result = runner.invoke(app, ["config", "set", "nonsense", "1"])
    assert isinstance(result.exception, ConfigError)


def test_config_set_refuses_a_path_inside_a_git_work_tree(tmp_path: Path) -> None:
    """A bad value must be rejected before it is persisted."""
    import subprocess

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

    result = runner.invoke(app, ["config", "set", "data_dir", str(repo / "data")])
    assert isinstance(result.exception, DataLocationError)


def test_config_set_writes_and_the_value_is_read_back(tmp_path: Path) -> None:
    """A write must survive into the next invocation, not just the current one."""
    target = tmp_path / "elsewhere"
    target.mkdir()

    written = runner.invoke(app, ["config", "set", "default_format", "csv"])
    assert written.exit_code == 0, written.output

    shown = runner.invoke(app, ["--format", "json", "config", "show"])
    rows = {r["setting"]: (r["value"], r["source"]) for r in json.loads(shown.stdout)}
    assert rows["default_format"] == ("csv", "config file")


def test_config_path_directory_is_created_on_write() -> None:
    runner.invoke(app, ["config", "set", "default_format", "json"])
    path = Path(runner.invoke(app, ["config", "path"]).stdout.strip())
    assert path.is_file()
