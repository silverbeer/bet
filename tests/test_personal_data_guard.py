"""Tests for scripts/check-no-personal-data.sh.

The guard is the only thing standing between a public repository and an
irreversible disclosure of personal financial data, so it is tested in both
directions: it must block real figures, and it must not cry wolf on ordinary
prose, version numbers or explicitly marked examples.

bet-guard: synthetic-amounts
"""

import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "check-no-personal-data.sh"

BLOCKED = 1
ALLOWED = 0


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repository with the guard available."""
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "seed").write_text("seed\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def stage_and_check(repo: Path, name: str, content: str) -> subprocess.CompletedProcess[str]:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    _git(repo, "add", name)
    return subprocess.run(
        ["bash", str(GUARD)], cwd=repo, capture_output=True, text=True, check=False
    )


# --------------------------------------------------------------- must block


@pytest.mark.parametrize(
    ("name", "content"),
    [
        pytest.param(
            "notes.md",
            "cash_staked 489.00, cash_returned 352.64, net_profit -136.36\n",
            id="statement-totals-in-markdown",
        ),
        pytest.param(
            "notes.md",
            "Promotions awarded $182.03 against $3,685.16 played.\n",
            id="currency-amounts-in-markdown",
        ),
        pytest.param(
            "conf.py",
            "STAKE = 489.00  # actual handle\n",
            id="real-stake-in-source",
        ),
        pytest.param("export.csv", "a,b\n1,2\n", id="csv-by-filename"),
        pytest.param("statement.pdf", "%PDF\n", id="pdf-by-filename"),
        pytest.param("data/bets.txt", "anything\n", id="personal-data-directory"),
    ],
)
def test_blocks(repo: Path, name: str, content: str) -> None:
    result = stage_and_check(repo, name, content)
    assert result.returncode == BLOCKED, f"guard allowed {name}:\n{result.stderr}"


# --------------------------------------------------------------- must allow


@pytest.mark.parametrize(
    ("name", "content"),
    [
        pytest.param(
            "doc.md",
            "Ownership is enforced by composite foreign keys.\n",
            id="ordinary-prose",
        ),
        pytest.param(
            "versions.md",
            "duckdb 1.5.5, pandas 3.0.5, python 3.14.0\nline-length = 100\n",
            id="version-numbers-are-not-money",
        ),
        pytest.param(
            "worked.md",
            "<!-- bet-guard: synthetic-amounts -->\nstake $10.00 returns $16.67\n",
            id="marked-synthetic-examples",
        ),
        pytest.param("script.sh", "echo hello\n", id="shell-without-figures"),
    ],
)
def test_allows(repo: Path, name: str, content: str) -> None:
    result = stage_and_check(repo, name, content)
    assert result.returncode == ALLOWED, f"guard blocked {name}:\n{result.stderr}"


def test_synthetic_exemption_is_reported_not_silent(repo: Path) -> None:
    """A skipped file must be announced, so the exemption stays visible."""
    result = stage_and_check(
        repo,
        "worked.md",
        "<!-- bet-guard: synthetic-amounts -->\nstake $10.00 returns $16.67\n",
    )
    assert result.returncode == ALLOWED
    assert "skipped" in result.stderr
    assert "worked.md" in result.stderr


def test_range_mode_checks_a_commit_range(repo: Path) -> None:
    """CI uses --range, which cannot be bypassed with --no-verify."""
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "leak.md").write_text("net_profit -136.36 on cash_staked 489.00\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bypassed the hook", "--no-verify")

    result = subprocess.run(
        ["bash", str(GUARD), "--range", f"{base}..HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == BLOCKED
    assert "leak.md" in result.stderr
