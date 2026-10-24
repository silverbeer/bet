"""``bet user create`` and ``bet user set-password`` against a temp warehouse (SB-1397).

Passwords are generated per test. None may appear in command output.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner, Result

from bet.cli.main import app
from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.errors import DatabaseError, NotFoundError, UsageError
from bet.services import auth

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(dict(os.environ)):
        if name.startswith(("BET_", "XDG_")):
            monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return data


@pytest.fixture
def initialised(data_dir: Path) -> str:
    """An initialised warehouse; returns the local user's display name."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    with connect(Settings(data_dir=data_dir)) as conn:
        scope = identity.existing_scope(conn)
        assert scope is not None
        user = identity.find_user(conn, scope)
        assert user is not None
        return user.display_name


def _password() -> str:
    return secrets.token_urlsafe(18)


def _json(*args: str, stdin: str | None = None) -> Result:
    return runner.invoke(app, ["--format", "json", *args], input=stdin)


def _can_log_in(data_dir: Path, name: str, password: str) -> bool:
    tokens = auth.TokenSettings(signing_key=SecretStr(secrets.token_urlsafe(48)))
    with connect(Settings(data_dir=data_dir)) as conn:
        try:
            auth.login(conn, name, password, tokens)
        except Exception:
            return False
        return True


# ------------------------------------------------------------------ create


def test_create_adds_a_user_to_the_local_tenant(data_dir: Path, initialised: str) -> None:
    result = _json("user", "create", "guest")
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)
    assert row["user"] == "guest" and row["action"] == "created"

    with connect(Settings(data_dir=data_dir)) as conn:
        local = identity.existing_scope(conn)
        assert local is not None
        (guest,) = identity.users_named(conn, "guest")
        assert guest.tenant_id == local.tenant_id
        assert str(guest.id) == row["user_id"]
        # The CLI keeps running as the original local user.
        assert local.user_id != guest.id
    assert "set-password" in result.stderr


def test_create_refuses_a_name_already_taken(initialised: str) -> None:
    result = runner.invoke(app, ["user", "create", initialised.upper()])
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 64


def test_create_refuses_a_blank_name(initialised: str) -> None:
    result = runner.invoke(app, ["user", "create", "   "])
    assert isinstance(result.exception, UsageError)


def test_create_needs_an_initialised_warehouse(data_dir: Path) -> None:
    result = runner.invoke(app, ["user", "create", "guest"])
    assert isinstance(result.exception, DatabaseError)
    assert not (data_dir / "bet.duckdb").exists()


def test_create_needs_a_local_user(data_dir: Path) -> None:
    with connect(Settings(data_dir=data_dir)) as conn:
        migrator.apply(conn)  # migrated, but `bet init` never created the local user
    result = runner.invoke(app, ["user", "create", "guest"])
    assert isinstance(result.exception, DatabaseError)
    assert "bet init" in (result.exception.remediation or "")


# ------------------------------------------------------------ set-password


def test_set_password_from_stdin_for_the_pre_existing_user(
    data_dir: Path, initialised: str
) -> None:
    password = _password()
    result = _json("user", "set-password", initialised, "--password-stdin", stdin=password + "\n")
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)
    assert row["action"] == "password set"
    assert password not in result.output
    assert _can_log_in(data_dir, initialised, password)


def test_set_password_again_changes_it(data_dir: Path, initialised: str) -> None:
    old, new = _password(), _password()
    _json("user", "set-password", initialised, "--password-stdin", stdin=old)
    result = _json("user", "set-password", initialised, "--password-stdin", stdin=new + "\r\n")
    assert json.loads(result.stdout)[0]["action"] == "password changed"
    assert _can_log_in(data_dir, initialised, new)
    assert not _can_log_in(data_dir, initialised, old)


def test_set_password_prompts_twice_with_hidden_input(data_dir: Path, initialised: str) -> None:
    password = _password()
    result = runner.invoke(
        app, ["user", "set-password", initialised], input=f"{password}\n{password}\n"
    )
    assert result.exit_code == 0, result.output
    assert "Repeat the password" in result.output
    assert password not in result.output
    assert _can_log_in(data_dir, initialised, password)


def test_set_password_for_a_newly_created_user(data_dir: Path, initialised: str) -> None:
    password = _password()
    runner.invoke(app, ["user", "create", "guest"])
    result = runner.invoke(app, ["user", "set-password", "guest", "--password-stdin"], password)
    assert result.exit_code == 0, result.output
    assert _can_log_in(data_dir, "guest", password)


def test_set_password_for_an_unknown_user(initialised: str) -> None:
    result = runner.invoke(app, ["user", "set-password", "nobody", "--password-stdin"], _password())
    assert isinstance(result.exception, NotFoundError)
    assert result.exception.exit_code == 5


def test_set_password_refuses_an_ambiguous_name(data_dir: Path, initialised: str) -> None:
    with connect(Settings(data_dir=data_dir)) as conn:
        local = identity.existing_scope(conn)
        assert local is not None
        identity.create_user(conn, local.tenant_id, initialised.upper())
    result = runner.invoke(
        app, ["user", "set-password", initialised, "--password-stdin"], _password()
    )
    assert isinstance(result.exception, UsageError)
    assert "more than one" in result.exception.message


@pytest.mark.parametrize("stdin", ["", "\n"])
def test_set_password_refuses_empty_stdin(initialised: str, stdin: str) -> None:
    result = runner.invoke(app, ["user", "set-password", initialised, "--password-stdin"], stdin)
    assert isinstance(result.exception, UsageError)
    assert "standard input" in result.exception.message


def test_set_password_refuses_a_short_password(data_dir: Path, initialised: str) -> None:
    result = runner.invoke(app, ["user", "set-password", initialised, "--password-stdin"], "short")
    assert isinstance(result.exception, UsageError)
    assert result.exception.exit_code == 64
    with connect(Settings(data_dir=data_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM core.user_credential").fetchone() == (0,)


def test_the_signing_key_in_the_environment_does_not_disturb_the_cli(
    initialised: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = secrets.token_urlsafe(48)
    monkeypatch.setenv("BET_JWT_SIGNING_KEY", key)
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert key not in result.output
