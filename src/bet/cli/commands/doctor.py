"""``bet doctor`` — check that this installation is healthy.

Each check reports pass, warn or fail with remediation. ``doctor`` exits
non-zero if anything failed, so it is usable in a script or a CI step, and it
never raises on a failing check — reporting every problem at once is more
useful than stopping at the first.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import typer

from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import ResolvedConfig, resolve
from bet.errors import BetError
from bet.paths import find_git_work_tree

MIN_PYTHON = (3, 14)


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str
    remediation: str = ""


def _python_version() -> Check:
    current = sys.version_info[:2]
    if current >= MIN_PYTHON:
        return Check("python version", Status.PASS, ".".join(map(str, current)))
    return Check(
        "python version",
        Status.FAIL,
        ".".join(map(str, current)),
        f"BET requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.",
    )


def _data_dir_outside_git(config: ResolvedConfig) -> Check:
    """Re-check the git rule at runtime.

    Configuration validation already enforces this, but a directory can be moved
    into a repository after the fact, or a parent can become one. This is the
    check that would notice.
    """
    data_dir = config.settings.data_dir
    work_tree = find_git_work_tree(data_dir)
    if work_tree is None:
        return Check("data directory outside git", Status.PASS, str(data_dir))
    return Check(
        "data directory outside git",
        Status.FAIL,
        f"{data_dir} is inside {work_tree}",
        "Move the data directory outside any git work tree immediately. "
        "If it has already been committed, treat the data as disclosed.",
    )


def _data_dir_writable(config: ResolvedConfig) -> Check:
    data_dir = config.settings.data_dir
    if not data_dir.exists():
        return Check(
            "data directory",
            Status.WARN,
            f"{data_dir} does not exist",
            "Run `bet init` to create it.",
        )
    probe = data_dir / ".bet-write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check(
            "data directory",
            Status.FAIL,
            f"{data_dir} is not writable: {exc}",
            "Fix the directory permissions.",
        )
    return Check("data directory", Status.PASS, f"{data_dir} is writable")


def _database(config: ResolvedConfig) -> Check:
    db_path = config.settings.db_path
    assert db_path is not None
    if not db_path.exists():
        return Check(
            "database",
            Status.WARN,
            f"{db_path} does not exist",
            "Run `bet init` to create the warehouse.",
        )

    try:
        import duckdb

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            version = conn.sql("SELECT version()").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return Check(
            "database",
            Status.FAIL,
            f"cannot open {db_path}: {exc}",
            "Check the file is a DuckDB database and is not locked.",
        )
    engine = version[0] if version else "unknown"
    return Check("database", Status.PASS, f"{db_path} ({engine})")


def _schema_version(config: ResolvedConfig) -> Check:
    """Report the applied schema version and whether migrations are pending.

    The migration harness is SB-699; until it exists this reports "unknown"
    rather than claiming a healthy schema it cannot actually verify.
    """
    db_path = config.settings.db_path
    assert db_path is not None
    if not db_path.exists():
        return Check("schema version", Status.WARN, "no database yet", "Run `bet init`.")
    return Check(
        "schema version", Status.WARN, "not yet tracked", "Migration tracking arrives with SB-699."
    )


def _source_archive(config: ResolvedConfig) -> Check:
    archive = config.settings.source_archive_dir
    assert archive is not None
    if not archive.exists():
        return Check("source archive", Status.WARN, f"{archive} does not exist", "Run `bet init`.")
    count = sum(1 for _ in archive.rglob("*") if _.is_file())
    return Check("source archive", Status.PASS, f"{count} archived file(s) in {archive}")


def _pre_commit_hook(cwd: Path) -> Check:
    """Report whether the personal-data guard is installed in this clone.

    Only meaningful when run from a checkout of BET itself. A developer machine
    without the hook is one `git commit` away from publishing betting data.

    Reported as a warning rather than a failure. It is a property of the
    development clone, not of the installation, and a check whose severity
    depends on the current working directory would make `bet doctor` exit
    non-zero in CI and in containers — which is how checks get ignored.
    """
    repo = find_git_work_tree(cwd)
    if repo is None or not (repo / "scripts" / "check-no-personal-data.sh").is_file():
        return Check("pre-commit guard", Status.PASS, "not a BET checkout")
    if not (repo / ".git" / "hooks" / "pre-commit").exists():
        return Check(
            "pre-commit guard",
            Status.WARN,
            "not installed in this clone",
            "Run `uv run pre-commit install`. Without it, nothing stops a commit "
            "of personal betting data, and git history is permanent.",
        )
    return Check("pre-commit guard", Status.PASS, "installed")


def _duckdb_available() -> Check:
    if shutil.which("duckdb"):
        return Check("duckdb cli", Status.PASS, "on PATH")
    return Check("duckdb cli", Status.PASS, "not on PATH (optional)")


@dataclass(slots=True)
class _Report:
    checks: list[Check]

    @property
    def failed(self) -> bool:
        return any(c.status is Status.FAIL for c in self.checks)


def run_checks(config: ResolvedConfig, *, cwd: Path | None = None) -> _Report:
    """Run every check and collect the results.

    ``cwd`` is injectable so the clone-specific check does not make the whole
    report depend on where the process happens to be running.
    """
    standalone: tuple[Callable[[], Check], ...] = (_python_version, _duckdb_available)
    with_config: tuple[Callable[[ResolvedConfig], Check], ...] = (
        _data_dir_outside_git,
        _data_dir_writable,
        _database,
        _schema_version,
        _source_archive,
    )
    checks = [check() for check in standalone]
    checks.append(_pre_commit_hook(cwd if cwd is not None else Path.cwd()))
    checks.extend(check(config) for check in with_config)
    return _Report(checks)


_MARK = {
    Status.PASS: "[green]pass[/green]",
    Status.WARN: "[yellow]warn[/yellow]",
    Status.FAIL: "[red]fail[/red]",
}


def doctor(ctx: typer.Context) -> None:
    """Check Python, configuration, data locations and the warehouse."""
    config = resolve()
    report = run_checks(config)
    fmt = options_from(ctx).fmt

    rows = [
        {
            "check": c.name,
            "status": _MARK[c.status] if fmt.value == "table" else c.status.value,
            "detail": c.detail,
            "remediation": c.remediation,
        }
        for c in report.checks
    ]
    render(rows, columns=["check", "status", "detail", "remediation"], fmt=fmt, title="bet doctor")

    if report.failed:
        raise BetError(
            "one or more checks failed.",
            remediation="Address the remediation column above and run `bet doctor` again.",
        )


__all__ = ["Check", "Status", "doctor", "run_checks"]
