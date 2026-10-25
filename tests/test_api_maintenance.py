"""The maintenance routes (SB-1404): POST /maintenance/backup and GET /maintenance/backups/latest.

Each runs through a TestClient over a temp warehouse. The token guard is
checked from every side -- disabled, missing, wrong, short, a user's access
token -- and the maintenance token is checked to open no user route. Every
token, key and password here is generated per test and thrown away.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from bet import log
from bet.api import ApiSettings, create_app
from bet.api.settings import (
    MAINTENANCE_TOKEN_ENV,
    SIGNING_KEY_ENV,
    maintenance_token,
    token_settings,
)
from bet.config import Settings, resolve
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import ConfigError
from bet.services import backup as backup_service
from bet.services.add import AddBetRequest, add_bet
from bet.services.backup import backup_name
from bet.services.tickets import LegInput
from bet.services.users import set_password_for

PLACED = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)


@pytest.fixture
def log_stream() -> io.StringIO:
    stream = io.StringIO()
    log.configure(json_logs=True, stream=stream)
    return stream


@pytest.fixture
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(dict(os.environ)):
        if name.startswith(("BET_", "XDG_")) or name == "WEB_CONCURRENCY":
            monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return data


@dataclass
class Api:
    app: FastAPI
    client: TestClient
    backups: Path
    token: str
    user_token: str

    @property
    def maintenance(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def user(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.user_token}"}

    def backup(self, **params: Any) -> httpx.Response:
        response: httpx.Response = self.client.post(
            "/maintenance/backup", headers=self.maintenance, params=params
        )
        return response

    def latest(self, headers: dict[str, str] | None = None) -> httpx.Response:
        response: httpx.Response = self.client.get(
            "/maintenance/backups/latest",
            headers=self.maintenance if headers is None else headers,
        )
        return response

    def counts(self) -> dict[str, int]:
        cursor: duckdb.DuckDBPyConnection = self.app.state.conn.cursor()
        tables = cursor.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            "WHERE database_name = current_database()"
        ).fetchall()
        counts = {}
        for schema, table in tables:
            row = cursor.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()
            assert row is not None
            counts[f"{schema}.{table}"] = int(row[0])
        return counts


def _serve(clean: Path, token: str | None) -> Iterator[tuple[FastAPI, TestClient, Settings, str]]:
    bet_settings = Settings(data_dir=clean)
    password = secrets.token_urlsafe(18)
    with connect(bet_settings) as conn:
        migrator.apply(conn)
        alice = identity.bootstrap(conn, display_name="alice")
        set_password_for(conn, "alice", password)
        add_bet(
            Warehouse(conn, alice),
            AddBetRequest(
                sportsbook="fanduel",
                legs=[LegInput(sport="NFL", selection_name="Synthetic ML", odds_american=150)],
                cash_staked=Decimal("5.00"),
                placed_at=PLACED,
            ),
        )
    settings = ApiSettings(
        bet=bet_settings,
        auth=token_settings({SIGNING_KEY_ENV: secrets.token_urlsafe(48)}),
        maintenance_token=None if token is None else SecretStr(token),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post("/auth/token", json={"username": "alice", "password": password})
        assert login.status_code == 200
        yield app, client, bet_settings, login.json()["access_token"]


@pytest.fixture
def api(clean: Path, log_stream: io.StringIO) -> Iterator[Api]:
    token = secrets.token_urlsafe(48)
    for app, client, bet_settings, user_token in _serve(clean, token):
        assert bet_settings.backup_dir is not None
        yield Api(app, client, bet_settings.backup_dir, token, user_token)


def _error(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error: dict[str, Any] = response.json()["error"]
    return error


ROUTES = [("POST", "/maintenance/backup"), ("GET", "/maintenance/backups/latest")]


# ---------------------------------------------------------------- the guard


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_unset_token_disables_the_routes_as_404(
    clean: Path, log_stream: io.StringIO, method: str, path: str
) -> None:
    for _, client, bet_settings, user_token in _serve(clean, None):
        for headers in (
            {},
            {"Authorization": "Bearer " + secrets.token_urlsafe(48)},
            {"Authorization": f"Bearer {user_token}"},
        ):
            error = _error(client.request(method, path, headers=headers), 404)
            # Indistinguishable from a route that does not exist.
            unknown = _error(client.request(method, "/maintenance/nothing"), 404)
            assert error["type"] == unknown["type"] == "NotFoundError"
            assert error["message"] == f"no route {path}."
            assert error["remediation"] == unknown["remediation"]
        assert bet_settings.backup_dir is not None
        assert not bet_settings.backup_dir.exists()


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_a_missing_token_is_401(api: Api, method: str, path: str) -> None:
    response = api.client.request(method, path)
    error = _error(response, 401)
    assert error["type"] == "AuthenticationError"
    assert error["message"] == "a maintenance token is required."
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("kind", ["wrong", "short", "prefix", "not-bearer", "user-jwt"])
def test_any_other_credential_is_401(api: Api, method: str, path: str, kind: str) -> None:
    headers = {
        "wrong": {"Authorization": "Bearer " + secrets.token_urlsafe(48)},
        "short": {"Authorization": "Bearer " + secrets.token_urlsafe(8)},
        "prefix": {"Authorization": "Bearer " + api.token[:-1]},
        "not-bearer": {"Authorization": "Basic " + api.token},
        "user-jwt": api.user,
    }[kind]
    response = api.client.request(method, path, headers=headers)
    error = _error(response, 401)
    assert error["type"] == "AuthenticationError"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert api.token not in response.text
    assert not api.backups.exists() or not any(api.backups.iterdir())


@pytest.mark.parametrize("path", ["/me", "/bets", "/balances"])
def test_the_maintenance_token_is_not_a_user_login(api: Api, path: str) -> None:
    _error(api.client.get(path, headers=api.maintenance), 401)


def test_the_token_is_never_logged_or_echoed(api: Api, log_stream: io.StringIO) -> None:
    made = api.backup()
    assert made.status_code == 201
    api.latest()
    api.client.post("/maintenance/backup", headers={"Authorization": "Bearer " + api.token[:-1]})
    for text in (made.text, log_stream.getvalue()):
        assert api.token not in text
        assert api.token[:-1] not in text
    assert "backup written" in log_stream.getvalue()


# ---------------------------------------------------------------- settings


def test_the_token_is_read_from_the_environment(clean: Path) -> None:
    token = secrets.token_urlsafe(48)
    env = {SIGNING_KEY_ENV: secrets.token_urlsafe(48), MAINTENANCE_TOKEN_ENV: f" {token}\n"}
    settings = ApiSettings.from_env(env=env)
    assert settings.maintenance_token is not None
    assert settings.maintenance_token.get_secret_value() == token
    assert token not in repr(settings) and token not in settings.model_dump_json()


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_unset_or_blank_token_disables_maintenance(raw: str | None) -> None:
    env = {} if raw is None else {MAINTENANCE_TOKEN_ENV: raw}
    assert maintenance_token(env) is None


def test_a_short_token_refuses_to_start_without_echoing_it() -> None:
    short = secrets.token_hex(15)  # 30 bytes
    with pytest.raises(ConfigError, match=MAINTENANCE_TOKEN_ENV) as raised:
        maintenance_token({MAINTENANCE_TOKEN_ENV: short})
    assert short not in raised.value.message
    assert short not in (raised.value.remediation or "")
    assert maintenance_token({MAINTENANCE_TOKEN_ENV: secrets.token_hex(16)}) is not None


def test_the_token_variable_does_not_reach_cli_configuration(
    clean: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings forbids unknown BET_* keys; BET_MAINTENANCE_* is skipped, not rejected."""
    token = secrets.token_urlsafe(48)
    monkeypatch.setenv(MAINTENANCE_TOKEN_ENV, token)
    resolved = resolve()
    assert token not in resolved.settings.model_dump_json()
    assert not any(k.startswith("maintenance") for k in resolved.sources)


# ---------------------------------------------------------------- backup


def test_a_backup_is_written_and_described(api: Api) -> None:
    before = api.counts()
    response = api.backup()
    assert response.status_code == 201, response.text
    body = response.json()

    path = api.backups / body["name"]
    assert backup_service.backup_time(body["name"]) is not None
    assert path.is_file()
    assert body["byte_size"] == path.stat().st_size
    assert body["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert body["tables"] == before
    assert body["total_rows"] == sum(before.values())
    assert body["tables"]["core.bet"] == 1
    assert body["schema_version"] == max(m.version for m in migrator.discover())
    assert body["pruned"] == []
    created = datetime.fromisoformat(body["created_at"])
    assert abs(datetime.now(UTC) - created) < timedelta(minutes=1)
    assert backup_service.backup_name(created) == body["name"]

    with duckdb.connect(str(path), read_only=True) as copy:
        assert copy.execute("SELECT count(*) FROM core.bet").fetchone() == (1,)


def test_a_backup_prunes_by_keep_days(api: Api) -> None:
    now = datetime.now(UTC)
    api.backups.mkdir(parents=True)
    stale = api.backups / backup_name(now - timedelta(days=20))
    kept = api.backups / backup_name(now - timedelta(days=5))
    other = api.backups / "notes.txt"
    for path in (stale, kept, other):
        path.write_bytes(b"synthetic")

    body = api.backup(keep_days=10).json()
    assert body["pruned"] == [stale.name]
    assert not stale.exists() and kept.exists() and other.exists()

    # The default keeps 14 days: the 5-day-old copy survives it too.
    api.backups.joinpath(body["name"]).unlink()  # the next would share its second
    assert api.backup().json()["pruned"] == []
    assert kept.exists()


@pytest.mark.parametrize("keep_days", ["0", "366", "-1", "soon"])
def test_keep_days_out_of_bounds_is_refused(api: Api, keep_days: str) -> None:
    error = _error(api.backup(keep_days=keep_days), 422)
    assert error["type"] == "ValidationError"
    assert error["details"][0]["loc"] == ["query", "keep_days"]
    assert not api.backups.exists() or not any(api.backups.iterdir())


@pytest.mark.parametrize("keep_days", [1, 365])
def test_keep_days_bounds_are_inclusive(api: Api, keep_days: int) -> None:
    assert api.backup(keep_days=keep_days).status_code == 201


def test_a_failed_backup_is_a_503_in_bet_format(api: Api) -> None:
    api.backups.parent.mkdir(parents=True, exist_ok=True)
    api.backups.write_bytes(b"not a directory")
    error = _error(api.backup(), 503)
    assert error["type"] == "DatabaseError"
    assert error["message"].startswith("the backup could not be written")


def test_a_second_backup_in_the_same_second_is_a_400(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 10, 24, 3, 30, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Frozen:
            return cls.fromtimestamp(fixed.timestamp(), tz)

    monkeypatch.setattr("bet.api.maintenance.datetime", Frozen)
    assert api.backup().status_code == 201
    assert _error(api.backup(), 400)["type"] == "UsageError"


# ---------------------------------------------------------------- download


def test_latest_without_a_backup_is_404(api: Api) -> None:
    error = _error(api.latest(), 404)
    assert error["message"] == "there is no backup yet."


def test_latest_returns_the_newest_with_its_sha256(api: Api) -> None:
    api.backups.mkdir(parents=True)
    older = api.backups / backup_name(datetime.now(UTC) - timedelta(days=1))
    older.write_bytes(b"synthetic older backup")
    (api.backups / "zzz-not-a-backup.duckdb").write_bytes(b"synthetic")
    made = api.backup().json()

    response = api.latest()
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert f'filename="{made["name"]}"' in response.headers["content-disposition"]
    assert response.headers["X-Backup-SHA256"] == made["sha256"]
    assert hashlib.sha256(response.content).hexdigest() == made["sha256"]
    assert len(response.content) == made["byte_size"]


def test_latest_needs_the_token(api: Api) -> None:
    api.backup()
    _error(api.latest(headers={}), 401)
    _error(api.latest(headers=api.user), 401)
