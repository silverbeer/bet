"""``bet init`` and the migration state `bet doctor` reports afterwards."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bet.cli.main import app

runner = CliRunner()


@pytest.fixture
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pristine environment pointing at throwaway directories outside git."""
    import os

    for name in list(dict(os.environ)):
        if name.startswith("BET_") or name.startswith("XDG_"):
            monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return data


def rows(result_stdout: str) -> dict[str, dict[str, str]]:
    return {r["item"]: r for r in json.loads(result_stdout)}


def test_init_creates_everything_from_scratch(clean: Path) -> None:
    result = runner.invoke(app, ["--format", "json", "init"])
    assert result.exit_code == 0, result.output

    reported = rows(result.stdout)
    assert reported["data_dir"]["action"] == "created"
    assert reported["warehouse"]["action"] == "created"

    assert clean.is_dir()
    assert (clean / "sources").is_dir()
    assert (clean / "backups").is_dir()
    assert (clean / "bet.duckdb").is_file()


def test_init_writes_a_config_file(clean: Path) -> None:
    runner.invoke(app, ["init"])
    path = Path(runner.invoke(app, ["config", "path"]).stdout.strip())
    assert path.is_file()


def test_init_pins_the_storage_format(clean: Path) -> None:
    """DuckDB's default is version 64; the pin must actually take effect."""
    result = runner.invoke(app, ["--format", "json", "init"])
    assert rows(result.stdout)["storage format"]["path"] == "68"


def test_init_is_idempotent(clean: Path) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0

    second = runner.invoke(app, ["--format", "json", "init"])
    assert second.exit_code == 0
    reported = rows(second.stdout)
    assert reported["data_dir"]["action"] == "already present"
    assert reported["warehouse"]["action"] == "already present"
    assert reported["config"]["action"] == "already present"


def test_init_creates_the_migration_table(clean: Path) -> None:
    runner.invoke(app, ["init"])

    import duckdb

    conn = duckdb.connect(str(clean / "bet.duckdb"), read_only=True)
    try:
        found = conn.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE schema_name = 'control' AND table_name = 'migration'"
        ).fetchone()
    finally:
        conn.close()
    assert found is not None
    assert found[0] == 1


def test_init_refuses_a_data_dir_inside_a_git_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from bet.errors import DataLocationError

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    monkeypatch.setenv("BET_DATA_DIR", str(repo / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    result = runner.invoke(app, ["init"])
    assert isinstance(result.exception, DataLocationError)
    assert not (repo / "data" / "bet.duckdb").exists()


# ------------------------------------------------- doctor after init


def test_doctor_reports_a_healthy_warehouse_after_init(clean: Path) -> None:
    runner.invoke(app, ["init"])

    result = runner.invoke(app, ["--format", "json", "doctor"])
    assert result.exit_code == 0, result.output

    checks = {c["check"]: c for c in json.loads(result.stdout)}
    assert checks["database"]["status"] == "pass"
    assert checks["schema version"]["status"] == "pass"
    assert checks["storage format"]["status"] == "pass"
    assert "68" in checks["storage format"]["detail"]


def test_doctor_warns_before_init(clean: Path) -> None:
    result = runner.invoke(app, ["--format", "json", "doctor"])
    checks = {c["check"]: c for c in json.loads(result.stdout)}
    assert checks["schema version"]["status"] == "warn"
    assert checks["storage format"]["status"] == "warn"


def test_doctor_reports_tampering_as_a_failure_rather_than_crashing(clean: Path) -> None:
    """An edited migration must be reported alongside the other checks.

    Simulated by corrupting the stored checksum rather than editing a shipped
    migration file, which would break every other test in the suite. The runner
    compares the two, so either side changing produces the same failure.
    """
    runner.invoke(app, ["init"])

    import duckdb

    conn = duckdb.connect(str(clean / "bet.duckdb"))
    conn.execute(
        "UPDATE control.migration SET checksum = 'a-checksum-that-will-not-match' WHERE version = 1"
    )
    conn.close()

    result = runner.invoke(app, ["--format", "json", "doctor"])
    checks = {c["check"]: c for c in json.loads(result.stdout)}
    assert checks["schema version"]["status"] == "fail"
    assert "modified after it was applied" in checks["schema version"]["detail"]
    # Other checks still ran: doctor reports everything, not just the first fault.
    assert checks["python version"]["status"] == "pass"


def test_doctor_reports_a_corrupt_file_as_a_storage_format_failure(clean: Path) -> None:
    clean.mkdir(parents=True, exist_ok=True)
    (clean / "bet.duckdb").write_text("definitely not a database")

    result = runner.invoke(app, ["--format", "json", "doctor"])
    checks = {c["check"]: c for c in json.loads(result.stdout)}
    assert checks["storage format"]["status"] == "fail"
