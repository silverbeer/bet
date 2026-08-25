"""``bet doctor`` reports a healthy install and each individual failure mode."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bet.cli.commands.doctor import Status, run_checks
from bet.config import resolve


def _checks(env: dict[str, str], tmp_path: Path) -> dict[str, tuple[Status, str]]:
    resolved = resolve(config_path=tmp_path / "missing.toml", env=env)
    return {c.name: (c.status, c.detail) for c in run_checks(resolved).checks}


@pytest.fixture
def healthy(tmp_path: Path) -> dict[str, str]:
    data = tmp_path / "data"
    (data / "sources").mkdir(parents=True)
    (data / "backups").mkdir(parents=True)
    return {"BET_DATA_DIR": str(data)}


def test_reports_a_healthy_installation(healthy: dict[str, str], tmp_path: Path) -> None:
    checks = _checks(healthy, tmp_path)
    assert checks["python version"][0] is Status.PASS
    assert checks["data directory"][0] is Status.PASS
    assert checks["data directory outside git"][0] is Status.PASS
    assert checks["source archive"][0] is Status.PASS


def test_warns_when_the_database_does_not_exist(healthy: dict[str, str], tmp_path: Path) -> None:
    status, detail = _checks(healthy, tmp_path)["database"]
    assert status is Status.WARN
    assert "does not exist" in detail


def test_warns_when_the_data_directory_is_missing(tmp_path: Path) -> None:
    checks = _checks({"BET_DATA_DIR": str(tmp_path / "absent")}, tmp_path)
    assert checks["data directory"][0] is Status.WARN


def test_fails_when_the_data_directory_is_not_writable(
    healthy: dict[str, str], tmp_path: Path
) -> None:
    data = Path(healthy["BET_DATA_DIR"])
    data.chmod(0o500)
    try:
        status, _ = _checks(healthy, tmp_path)["data directory"]
    finally:
        data.chmod(0o700)
    assert status is Status.FAIL


def test_a_report_with_a_failure_is_marked_failed(healthy: dict[str, str], tmp_path: Path) -> None:
    data = Path(healthy["BET_DATA_DIR"])
    data.chmod(0o500)
    try:
        resolved = resolve(config_path=tmp_path / "missing.toml", env=healthy)
        report = run_checks(resolved)
    finally:
        data.chmod(0o700)
    assert report.failed


def test_a_healthy_report_is_not_marked_failed(healthy: dict[str, str], tmp_path: Path) -> None:
    resolved = resolve(config_path=tmp_path / "missing.toml", env=healthy)
    assert not run_checks(resolved).failed


def test_reports_a_real_database(healthy: dict[str, str], tmp_path: Path) -> None:
    import duckdb

    db = Path(healthy["BET_DATA_DIR"]) / "bet.duckdb"
    duckdb.connect(str(db)).close()
    status, detail = _checks(healthy, tmp_path)["database"]
    assert status is Status.PASS
    assert "v" in detail


def test_reports_a_corrupt_database_as_failed(healthy: dict[str, str], tmp_path: Path) -> None:
    db = Path(healthy["BET_DATA_DIR"]) / "bet.duckdb"
    db.write_text("this is not a duckdb file")
    status, _ = _checks(healthy, tmp_path)["database"]
    assert status is Status.FAIL


def test_detects_a_missing_pre_commit_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: dict[str, str]
) -> None:
    """A BET checkout without the guard installed is one commit from disclosure."""
    repo = tmp_path / "clone"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "check-no-personal-data.sh").write_text("#!/bin/sh\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / ".git" / "hooks" / "pre-commit").unlink(missing_ok=True)
    monkeypatch.chdir(repo)

    status, detail = _checks(healthy, tmp_path)["pre-commit guard"]
    assert status is Status.FAIL
    assert "not installed" in detail


def test_doctor_command_renders_and_succeeds_when_healthy(
    healthy: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from bet.cli.main import app

    for key, value in healthy.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    result = CliRunner().invoke(app, ["--format", "json", "doctor"])
    assert result.exit_code == 0, result.output
    import json as _json

    names = {row["check"] for row in _json.loads(result.stdout)}
    assert "data directory outside git" in names


def test_doctor_command_raises_when_a_check_fails(
    healthy: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from bet.cli.main import app
    from bet.errors import BetError

    data = Path(healthy["BET_DATA_DIR"])
    for key, value in healthy.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    data.chmod(0o500)
    try:
        result = CliRunner().invoke(app, ["--format", "json", "doctor"])
    finally:
        data.chmod(0o700)
    assert isinstance(result.exception, BetError)
