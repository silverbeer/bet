"""The API skeleton: lifecycle, single-worker rule, error mapping and health."""

from __future__ import annotations

import io
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import bet
from bet import log
from bet.api import ApiSettings, create_app
from bet.api import server as api_server
from bet.api.deps import Cursor, get_cursor
from bet.config import Settings
from bet.database import migrator
from bet.database.connection import connect
from bet.errors import (
    BetError,
    ConfigError,
    DatabaseError,
    NotFoundError,
    UsageError,
)


@pytest.fixture(autouse=True)
def server_log() -> io.StringIO:
    """Route structlog to a fresh stream; earlier CLI tests leave it on a closed one."""
    stream = io.StringIO()
    log.configure(json_logs=True, stream=stream)
    return stream


@pytest.fixture
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pristine environment pointing at throwaway directories outside git."""
    for name in list(dict(os.environ)):
        if name.startswith(("BET_", "XDG_")) or name == "WEB_CONCURRENCY":
            monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return data


@pytest.fixture
def settings(clean: Path) -> ApiSettings:
    """Settings for a migrated, empty warehouse."""
    bet_settings = Settings(data_dir=clean)
    with connect(bet_settings) as conn:
        migrator.apply(conn)
    return ApiSettings(bet=bet_settings)


@pytest.fixture
def client(settings: ApiSettings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ------------------------------------------------------------- health


def test_healthz_reports_the_process_alive(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_does_not_need_the_database(settings: ApiSettings) -> None:
    """Without entering the lifespan no connection exists; healthz still answers."""
    response = TestClient(create_app(settings)).get("/healthz")
    assert response.status_code == 200


def test_readyz_is_ready_on_a_current_warehouse(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_is_unavailable_with_a_pending_migration(
    settings: ApiSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ahead = tmp_path / "migrations"
    shutil.copytree(migrator.MIGRATIONS_DIR, ahead)
    (ahead / "9999_from_the_future.sql").write_text("CREATE SCHEMA future;")
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", ahead)

    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "DatabaseError"
    assert "9999_from_the_future" in error["message"]
    assert "bet init" in error["remediation"]


def test_readyz_is_unavailable_when_the_connection_is_closed(
    settings: ApiSettings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.conn.close()
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "DatabaseError"


def test_readyz_is_unavailable_when_the_cursor_fails(settings: ApiSettings) -> None:
    app = create_app(settings)

    def broken_cursor() -> Iterator[duckdb.DuckDBPyConnection]:
        conn = duckdb.connect(":memory:")
        cursor = conn.cursor()
        cursor.close()
        try:
            yield cursor
        finally:
            conn.close()

    app.dependency_overrides[get_cursor] = broken_cursor
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert "unusable" in response.json()["error"]["message"]


# ------------------------------------------------------------- lifecycle


def test_lifespan_opens_one_connection_and_closes_it(settings: ApiSettings) -> None:
    app = create_app(settings)
    with TestClient(app):
        conn = app.state.conn
        assert conn.execute("SELECT 1").fetchone() == (1,)

    assert not hasattr(app.state, "conn")
    with pytest.raises(duckdb.ConnectionException):
        conn.execute("SELECT 1")


def test_lifespan_uses_the_pinned_storage_version(settings: ApiSettings) -> None:
    app = create_app(settings)
    with TestClient(app):
        (setting,) = app.state.conn.execute(
            "SELECT value FROM duckdb_settings() WHERE name = 'storage_compatibility_version'"
        ).fetchone()
    assert setting == settings.bet.storage_version


def test_startup_refuses_a_missing_warehouse(clean: Path) -> None:
    app = create_app(ApiSettings(bet=Settings(data_dir=clean)))
    with pytest.raises(DatabaseError, match="no warehouse"), TestClient(app):
        pass  # pragma: no cover - startup raises first
    assert not (clean / "bet.duckdb").exists()


def test_each_request_gets_a_cursor_that_is_closed_afterwards(settings: ApiSettings) -> None:
    app = create_app(settings)
    seen: list[duckdb.DuckDBPyConnection] = []

    @app.get("/probe")
    def probe(cursor: Cursor) -> dict[str, int]:
        seen.append(cursor)
        (value,) = cursor.execute("SELECT 42").fetchone() or (0,)
        return {"value": value}

    with TestClient(app) as client:
        assert client.get("/probe").json() == {"value": 42}
        assert client.get("/probe").json() == {"value": 42}
        process_conn = app.state.conn

        assert len(seen) == 2
        assert seen[0] is not seen[1]
        assert all(cursor is not process_conn for cursor in seen)
        for cursor in seen:
            with pytest.raises(duckdb.ConnectionException):
                cursor.execute("SELECT 1")
        # Closing a cursor leaves the process connection open.
        assert process_conn.execute("SELECT 1").fetchone() == (1,)


# ------------------------------------------------------------- error mapping


def _raising_app(settings: ApiSettings, exc: Exception) -> TestClient:
    app = create_app(settings)

    @app.get("/boom")
    def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (UsageError("bad input", remediation="Pass a valid id."), 400),
        (NotFoundError("no such bet", remediation="Check the id."), 404),
        (DatabaseError("warehouse locked", remediation="Stop the other process."), 503),
        (ConfigError("config broken", remediation="Fix it."), 500),
        (BetError("plain failure", remediation="Try again."), 500),
    ],
)
def test_bet_errors_map_to_status_and_envelope(
    settings: ApiSettings, exc: BetError, status: int
) -> None:
    response = _raising_app(settings, exc).get("/boom")

    assert response.status_code == status
    assert response.json() == {
        "error": {
            "type": type(exc).__name__,
            "message": exc.message,
            "remediation": exc.remediation,
        }
    }


def test_a_bet_error_without_remediation_reports_null(settings: ApiSettings) -> None:
    response = _raising_app(settings, UsageError("bad input")).get("/boom")
    assert response.json()["error"]["remediation"] is None


def test_unexpected_errors_are_500_without_internal_detail(
    settings: ApiSettings, server_log: io.StringIO
) -> None:
    secret = "internal detail /private/path/bet.duckdb"
    response = _raising_app(settings, RuntimeError(secret)).get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "type": "InternalError",
            "message": "internal server error",
            "remediation": None,
        }
    }
    assert secret not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text
    # The detail is kept server-side, where it can be debugged.
    assert secret in server_log.getvalue()
    assert "/boom" in server_log.getvalue()


# ------------------------------------------------------------- single worker


def test_refuses_more_than_one_worker(settings: ApiSettings) -> None:
    with pytest.raises(ConfigError, match="exactly one worker") as raised:
        create_app(settings.model_copy(update={"workers": 2}))
    assert raised.value.remediation is not None
    assert "WEB_CONCURRENCY" in raised.value.remediation


def test_web_concurrency_sets_the_worker_count(
    clean: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    resolved = ApiSettings.from_env()
    assert resolved.workers == 4
    with pytest.raises(ConfigError, match="exactly one worker"):
        create_app(resolved)


def test_rejects_a_non_integer_web_concurrency(
    clean: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "lots")
    with pytest.raises(ConfigError, match="WEB_CONCURRENCY"):
        ApiSettings.from_env()


def test_from_env_reuses_cli_configuration(clean: Path) -> None:
    resolved = ApiSettings.from_env()
    assert resolved.workers == 1
    assert resolved.bet.db_path == (clean / "bet.duckdb").resolve()


def test_create_app_defaults_to_the_environment(settings: ApiSettings) -> None:
    """With no settings argument the factory resolves BET_DATA_DIR itself."""
    with TestClient(create_app()) as client:
        assert client.get("/readyz").status_code == 200


# ------------------------------------------------------------- openapi


def test_openapi_reports_title_and_package_version(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "BET API"
    assert schema["info"]["version"] == bet.__version__
    assert {"/healthz", "/readyz"} <= set(schema["paths"])


def test_docs_are_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200


# ------------------------------------------------------------- entry point


def test_entry_point_runs_uvicorn_with_one_worker(
    settings: ApiSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[FastAPI, dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))

    api_server.main(["--host", "0.0.0.0", "--port", "8123"])

    ((app, kwargs),) = calls
    assert app.title == "BET API"
    assert kwargs == {"host": "0.0.0.0", "port": 8123, "workers": 1}


def test_entry_point_refuses_multiple_workers_before_serving(
    settings: ApiSettings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("served"))

    with pytest.raises(SystemExit) as raised:
        api_server.main([])

    assert raised.value.code == ConfigError.exit_code
    err = capsys.readouterr().err
    assert "exactly one worker" in err
    assert "WEB_CONCURRENCY" in err
