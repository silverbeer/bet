"""BET-issued JWT auth (SB-1397): passwords, tokens, rotation, and the API surface.

Every key and password here is generated per test and thrown away. Nothing in
an assertion message or a log may contain one.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import duckdb
import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from bet import log
from bet.api import ApiSettings, create_app
from bet.api import server as api_server
from bet.api.deps import CurrentScope, Cursor
from bet.api.settings import (
    ACCESS_TTL_ENV,
    REFRESH_TTL_ENV,
    SIGNING_KEY_ENV,
    token_settings,
)
from bet.config import Settings, resolve
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import AuthenticationError, ConfigError, UsageError
from bet.models.bet import Bet
from bet.models.credential import RefreshToken, UserCredential
from bet.models.ownership import OwnerScope, SportsbookAccount
from bet.services import auth
from bet.services.users import create_user, set_password_for

PLACED = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)


def _password() -> str:
    return secrets.token_urlsafe(18)


def _key() -> str:
    return secrets.token_urlsafe(48)


@pytest.fixture(autouse=True)
def server_log() -> io.StringIO:
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


@pytest.fixture
def tokens() -> auth.TokenSettings:
    return auth.TokenSettings(signing_key=SecretStr(_key()))


@pytest.fixture
def conn(clean: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A migrated warehouse whose local user is ``alice``."""
    with connect(Settings(data_dir=clean)) as connection:
        migrator.apply(connection)
        identity.bootstrap(connection, display_name="alice")
        yield connection


@pytest.fixture
def alice(conn: duckdb.DuckDBPyConnection) -> tuple[Warehouse, str]:
    """Alice's warehouse and her (throwaway) password."""
    scope = identity.existing_scope(conn)
    assert scope is not None
    password = _password()
    w = Warehouse(conn, scope)
    auth.set_password(w, password)
    return w, password


def _claims(token: str) -> dict[str, Any]:
    return dict(jwt.decode(token, options={"verify_signature": False}))


# ------------------------------------------------------------- passwords


def test_hash_roundtrip() -> None:
    password = _password()
    hashed = auth.hash_password(password)
    assert hashed.startswith("$argon2id$")
    assert password not in hashed
    assert auth.password_matches(hashed, password)


def test_wrong_password_does_not_match() -> None:
    hashed = auth.hash_password(_password())
    assert not auth.password_matches(hashed, _password())


def test_a_malformed_hash_never_matches() -> None:
    assert not auth.password_matches("not-a-hash", _password())


@pytest.mark.parametrize("length", [auth.MIN_PASSWORD_LENGTH - 1, auth.MAX_PASSWORD_LENGTH + 1])
def test_passwords_outside_the_length_bounds_are_refused(length: int) -> None:
    with pytest.raises(UsageError, match="characters"):
        auth.hash_password("x" * length)


def test_set_password_stores_only_an_argon2_hash(alice: tuple[Warehouse, str]) -> None:
    w, password = alice
    credential = w.credentials.current()
    assert credential is not None
    assert credential.password_hash.startswith("$argon2id$")
    assert password not in credential.password_hash
    assert credential.rotated_at is None
    assert credential.password_hash not in repr(credential)


def test_set_password_again_is_a_rotation(alice: tuple[Warehouse, str]) -> None:
    w, old = alice
    new = _password()
    auth.set_password(w, new)

    credential = w.credentials.current()
    assert credential is not None and credential.rotated_at is not None
    assert auth.verify_password(w, new)
    assert not auth.verify_password(w, old)


def test_verify_password_without_a_credential_is_false_after_a_dummy_check(
    conn: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = identity.existing_scope(conn)
    assert scope is not None
    checks = _count_verifications(monkeypatch)
    assert not auth.verify_password(Warehouse(conn, scope), _password())
    assert checks == [1]


def test_the_database_refuses_a_non_argon2_hash(conn: duckdb.DuckDBPyConnection) -> None:
    scope = identity.existing_scope(conn)
    assert scope is not None
    with pytest.raises(duckdb.ConstraintException):
        Warehouse(conn, scope).credentials.add(
            UserCredential(
                tenant_id=scope.tenant_id, user_id=scope.user_id, password_hash="plaintext"
            )
        )


def test_credential_models_reject_naive_timestamps() -> None:
    scope = OwnerScope(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    with pytest.raises(ValidationError, match="timezone-aware"):
        UserCredential(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            password_hash="$argon2id$x",
            created_at=datetime(2026, 1, 1),  # naive: the point of the test
        )
    with pytest.raises(ValidationError, match="after issued_at"):
        RefreshToken(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            id=uuid.uuid4(),
            token_hash="0" * 64,
            issued_at=PLACED,
            expires_at=PLACED,
        )


# ------------------------------------------------------------------ login


def _count_verifications(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count argon2 verifications, so the unknown-user path is shown to pay one."""
    calls = [0]
    real = auth.password_matches

    def counting(password_hash: str, password: str) -> bool:
        calls[0] += 1
        return real(password_hash, password)

    monkeypatch.setattr(auth, "password_matches", counting)
    return calls


def test_login_issues_an_access_and_a_refresh_token(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, password = alice
    pair = auth.login(conn, "alice", password, tokens)

    access, refresh = _claims(pair.access_token), _claims(pair.refresh_token)
    assert set(access) == set(auth.CLAIMS)
    assert access["typ"] == "access" and refresh["typ"] == "refresh"
    assert access["sub"] == str(w.scope.user_id)
    assert access["tid"] == str(w.scope.tenant_id)
    assert access["exp"] - access["iat"] == 15 * 60
    assert refresh["exp"] - refresh["iat"] == 30 * 24 * 3600
    assert access["jti"] != refresh["jti"]
    assert pair.token_type == "bearer"
    assert pair.access_token not in repr(pair)


def test_login_ignores_name_case_and_padding(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    _, password = alice
    assert auth.login(conn, "  ALICE ", password, tokens).access_token


def test_only_a_digest_of_the_refresh_token_is_stored(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, password = alice
    pair = auth.login(conn, "alice", password, tokens)

    (row,) = w.refresh_tokens.fetch_all()
    assert str(row.id) == _claims(pair.refresh_token)["jti"]
    assert row.token_hash != pair.refresh_token
    assert len(row.token_hash) == 64
    assert row.revoked_at is None
    assert row.expires_at - row.issued_at == auth.DEFAULT_REFRESH_TTL


def test_a_wrong_password_and_an_unknown_user_fail_identically(
    conn: duckdb.DuckDBPyConnection,
    alice: tuple[Warehouse, str],
    tokens: auth.TokenSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = _count_verifications(monkeypatch)
    failures = []
    for username in ("alice", "nobody"):
        with pytest.raises(AuthenticationError) as raised:
            auth.login(conn, username, _password(), tokens)
        failures.append((raised.value.message, raised.value.remediation))

    assert failures[0] == failures[1] == (auth.INVALID_LOGIN, None)
    # One argon2 verification each: the unknown user is checked against a dummy.
    assert checks == [2]


def test_a_user_with_no_password_cannot_log_in(
    conn: duckdb.DuckDBPyConnection, tokens: auth.TokenSettings
) -> None:
    with pytest.raises(AuthenticationError, match="invalid username or password"):
        auth.login(conn, "alice", _password(), tokens)


def test_a_disabled_user_cannot_log_in(
    conn: duckdb.DuckDBPyConnection, tokens: auth.TokenSettings
) -> None:
    scope = create_user(conn, "carol")
    conn.execute(
        "UPDATE core.user SET status = 'disabled' WHERE id = ?", [str(scope.user_id)]
    )  # before any row references the user: DuckDB refuses it after (see 0006)
    password = _password()
    set_password_for(conn, "carol", password)

    with pytest.raises(AuthenticationError, match="invalid username or password"):
        auth.login(conn, "carol", password, tokens)


def test_an_ambiguous_name_cannot_log_in(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, password = alice
    identity.create_user(conn, w.scope.tenant_id, "Alice")  # bypasses the uniqueness check
    with pytest.raises(AuthenticationError, match="invalid username or password"):
        auth.login(conn, "alice", password, tokens)


# ------------------------------------------------------------ access token


def test_a_valid_access_token_yields_its_scope(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, password = alice
    pair = auth.login(conn, "alice", password, tokens)
    assert auth.authenticate(conn, pair.access_token, tokens) == w.scope


def test_an_expired_access_token_is_refused(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    pair = auth.issue_tokens(w, tokens, now=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(AuthenticationError, match="expired"):
        auth.authenticate(conn, pair.access_token, tokens)


def _tamper(token: str) -> str:
    """Re-sign nothing; swap the payload's sub for another user's id."""
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = str(uuid.uuid4())
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"


def test_a_tampered_access_token_is_refused(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    pair = auth.issue_tokens(w, tokens)
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.authenticate(conn, _tamper(pair.access_token), tokens)


def test_a_token_signed_with_another_key_is_refused(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    other = auth.TokenSettings(signing_key=SecretStr(_key()))
    pair = auth.issue_tokens(w, other)
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.authenticate(conn, pair.access_token, tokens)


def test_an_unsigned_token_is_refused(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    claims = _claims(auth.issue_tokens(w, tokens).access_token)
    unsigned = jwt.encode(claims, key=cast(Any, None), algorithm="none")
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.authenticate(conn, unsigned, tokens)


def test_a_token_missing_a_claim_is_refused(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    claims = _claims(auth.issue_tokens(w, tokens).access_token)
    del claims["tid"]
    token = jwt.encode(claims, tokens.signing_key.get_secret_value(), algorithm="HS256")
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.authenticate(conn, token, tokens)


def test_a_refresh_token_is_not_an_access_token(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    pair = auth.issue_tokens(w, tokens)
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.authenticate(conn, pair.refresh_token, tokens)


def test_an_access_token_is_not_a_refresh_token(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    pair = auth.issue_tokens(w, tokens)
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.refresh(conn, pair.access_token, tokens)
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.logout(conn, pair.access_token, tokens)


def test_a_token_for_a_user_who_does_not_exist_is_refused(
    conn: duckdb.DuckDBPyConnection, tokens: auth.TokenSettings
) -> None:
    ghost = Warehouse(conn, OwnerScope(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()))
    pair, _ = auth._new_pair(ghost, tokens, datetime.now(UTC))
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.authenticate(conn, pair.access_token, tokens)


def test_a_token_for_a_disabled_user_is_refused(
    conn: duckdb.DuckDBPyConnection, tokens: auth.TokenSettings
) -> None:
    scope = create_user(conn, "dave")
    conn.execute("UPDATE core.user SET status = 'disabled' WHERE id = ?", [str(scope.user_id)])
    pair, _ = auth._new_pair(Warehouse(conn, scope), tokens, datetime.now(UTC))
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.authenticate(conn, pair.access_token, tokens)
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.refresh(conn, pair.refresh_token, tokens)


# ------------------------------------------------- refresh rotation and logout


def test_refresh_rotates_and_revokes_the_presented_token(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    first = auth.issue_tokens(w, tokens)
    second = auth.refresh(conn, first.refresh_token, tokens)

    old = w.refresh_tokens.get(uuid.UUID(_claims(first.refresh_token)["jti"]))
    new_id = uuid.UUID(_claims(second.refresh_token)["jti"])
    assert old.revoked_at is not None
    assert old.replaced_by == new_id
    assert w.refresh_tokens.get(new_id).revoked_at is None
    assert auth.authenticate(conn, second.access_token, tokens) == w.scope
    # The new refresh token rotates in turn.
    assert auth.refresh(conn, second.refresh_token, tokens).refresh_token


def test_reusing_a_rotated_refresh_token_revokes_the_whole_chain(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    first = auth.issue_tokens(w, tokens)
    second = auth.refresh(conn, first.refresh_token, tokens)
    third = auth.refresh(conn, second.refresh_token, tokens)
    other_session = auth.issue_tokens(w, tokens)

    with pytest.raises(AuthenticationError, match="already used"):
        auth.refresh(conn, first.refresh_token, tokens)

    # The newest token in the chain -- the one an attacker or the owner holds --
    # is dead too.
    with pytest.raises(AuthenticationError, match="already used"):
        auth.refresh(conn, third.refresh_token, tokens)
    assert all(
        t.revoked_at is not None
        for t in w.refresh_tokens.fetch_all()
        if str(t.id) != _claims(other_session.refresh_token)["jti"]
    )
    # Another login's chain is untouched.
    assert auth.refresh(conn, other_session.refresh_token, tokens).access_token


def test_a_signed_refresh_token_that_was_never_issued_is_refused(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    pair, _ = auth._new_pair(w, tokens, datetime.now(UTC))  # row never stored
    with pytest.raises(AuthenticationError, match="invalid"):
        auth.refresh(conn, pair.refresh_token, tokens)


def test_an_expired_refresh_token_is_refused(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    short = tokens.model_copy(update={"refresh_ttl": timedelta(seconds=1)})
    pair = auth.issue_tokens(w, short, now=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(AuthenticationError, match="expired"):
        auth.refresh(conn, pair.refresh_token, tokens)


def test_logout_revokes_the_refresh_token(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], tokens: auth.TokenSettings
) -> None:
    w, _ = alice
    pair = auth.issue_tokens(w, tokens)
    auth.logout(conn, pair.refresh_token, tokens)

    (row,) = w.refresh_tokens.fetch_all()
    assert row.revoked_at is not None and row.replaced_by is None
    with pytest.raises(AuthenticationError):
        auth.refresh(conn, pair.refresh_token, tokens)
    auth.logout(conn, pair.refresh_token, tokens)  # already revoked: not an error


# ----------------------------------------------------------- token settings


def test_a_short_signing_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        auth.TokenSettings(signing_key=SecretStr("x" * 31))


def test_a_non_positive_lifetime_is_refused() -> None:
    with pytest.raises(ValidationError, match="positive"):
        auth.TokenSettings(signing_key=SecretStr(_key()), access_ttl=timedelta(0))


def test_the_signing_key_never_appears_in_a_repr() -> None:
    key = _key()
    settings = auth.TokenSettings(signing_key=SecretStr(key))
    assert key not in repr(settings) and key not in str(settings)


def test_token_settings_read_the_environment() -> None:
    settings = token_settings(
        {SIGNING_KEY_ENV: _key(), ACCESS_TTL_ENV: "60", REFRESH_TTL_ENV: "3600"}
    )
    assert settings.access_ttl == timedelta(minutes=1)
    assert settings.refresh_ttl == timedelta(hours=1)
    assert token_settings({SIGNING_KEY_ENV: _key()}).access_ttl == auth.DEFAULT_ACCESS_TTL


@pytest.mark.parametrize("value", ["", "   "])
def test_a_missing_signing_key_is_a_config_error(value: str) -> None:
    with pytest.raises(ConfigError, match=f"{SIGNING_KEY_ENV} is not set") as raised:
        token_settings({SIGNING_KEY_ENV: value} if value else {})
    assert raised.value.remediation is not None
    assert "never from the config file" in raised.value.remediation


def test_a_short_signing_key_from_the_environment_is_not_echoed() -> None:
    key = "k" * 20
    with pytest.raises(ConfigError, match="at least 32 bytes") as raised:
        token_settings({SIGNING_KEY_ENV: key})
    assert key not in raised.value.message
    assert key not in (raised.value.remediation or "")
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize(("name", "value"), [(ACCESS_TTL_ENV, "soon"), (REFRESH_TTL_ENV, "-5")])
def test_bad_lifetimes_from_the_environment_are_refused(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name if value == "soon" else "positive"):
        token_settings({SIGNING_KEY_ENV: _key(), name: value})


def test_the_signing_key_variable_does_not_break_cli_configuration(
    clean: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings forbids unknown BET_* keys; BET_JWT_* is skipped, not rejected."""
    key = _key()
    monkeypatch.setenv(SIGNING_KEY_ENV, key)
    monkeypatch.setenv(ACCESS_TTL_ENV, "60")
    resolved = resolve()
    assert key not in resolved.settings.model_dump_json()
    assert not any(k.startswith("jwt") for k in resolved.sources)


def test_the_api_refuses_to_start_without_a_signing_key(clean: Path) -> None:
    with pytest.raises(ConfigError, match=SIGNING_KEY_ENV):
        ApiSettings.from_env()
    with pytest.raises(ConfigError, match=SIGNING_KEY_ENV):
        create_app()


def test_the_entry_point_explains_a_missing_signing_key(
    clean: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("served"))
    with pytest.raises(SystemExit) as raised:
        api_server.main([])
    assert raised.value.code == ConfigError.exit_code
    err = capsys.readouterr().err
    assert f"{SIGNING_KEY_ENV} is not set" in err
    assert "openssl rand" in err


# --------------------------------------------------------------------- API


@pytest.fixture
def api(
    conn: duckdb.DuckDBPyConnection, alice: tuple[Warehouse, str], clean: Path
) -> Iterator[tuple[TestClient, str]]:
    """A client over alice's warehouse, and her password.

    The fixture's own connection is closed first: the server must be the only
    process holding the file, exactly as in deployment.
    """
    _, password = alice
    conn.close()
    settings = ApiSettings(
        bet=Settings(data_dir=clean), auth=token_settings({SIGNING_KEY_ENV: _key()})
    )
    with TestClient(create_app(settings)) as client:
        yield client, password


def _login(client: TestClient, username: str, password: str) -> dict[str, Any]:
    response = client.post("/auth/token", json={"username": username, "password": password})
    assert response.status_code == 200, response.json()["error"]["message"]
    body: dict[str, Any] = response.json()
    return body


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_401(response: Any) -> dict[str, Any]:
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    error: dict[str, Any] = response.json()["error"]
    assert error["type"] == "AuthenticationError"
    return error


def test_api_login_returns_a_bearer_pair(api: tuple[TestClient, str]) -> None:
    client, password = api
    body = _login(client, "alice", password)
    assert set(body) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
        "refresh_expires_in",
    }
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["refresh_expires_in"] == 30 * 24 * 3600


def test_api_login_failures_are_indistinguishable(api: tuple[TestClient, str]) -> None:
    client, _ = api
    wrong = client.post("/auth/token", json={"username": "alice", "password": _password()})
    unknown = client.post("/auth/token", json={"username": "nobody", "password": _password()})
    assert _assert_401(wrong) == _assert_401(unknown)
    assert wrong.json()["error"]["message"] == auth.INVALID_LOGIN


def test_me_with_a_valid_token(api: tuple[TestClient, str]) -> None:
    client, password = api
    access = _login(client, "alice", password)["access_token"]
    response = client.get("/me", headers=_bearer(access))
    assert response.status_code == 200
    assert response.json() == {"display_name": "alice", "timezone": "UTC"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic YWxpY2U6eA=="}],
)
def test_me_without_a_usable_token_is_401(
    api: tuple[TestClient, str], headers: dict[str, str]
) -> None:
    client, _ = api
    _assert_401(client.get("/me", headers=headers))


def test_me_refuses_a_refresh_token(api: tuple[TestClient, str]) -> None:
    client, password = api
    refresh = _login(client, "alice", password)["refresh_token"]
    _assert_401(client.get("/me", headers=_bearer(refresh)))


def test_api_refresh_rotates_and_detects_reuse(api: tuple[TestClient, str]) -> None:
    client, password = api
    first = _login(client, "alice", password)

    second = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    assert client.get("/me", headers=_bearer(second.json()["access_token"])).status_code == 200

    error = _assert_401(
        client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    )
    assert "already used" in error["message"]
    _assert_401(
        client.post("/auth/refresh", json={"refresh_token": second.json()["refresh_token"]})
    )


def test_api_refresh_refuses_an_access_token(api: tuple[TestClient, str]) -> None:
    client, password = api
    access = _login(client, "alice", password)["access_token"]
    _assert_401(client.post("/auth/refresh", json={"refresh_token": access}))


def test_api_logout_revokes(api: tuple[TestClient, str]) -> None:
    client, password = api
    refresh = _login(client, "alice", password)["refresh_token"]

    response = client.post("/auth/logout", json={"refresh_token": refresh})
    assert response.status_code == 204
    assert response.content == b""
    _assert_401(client.post("/auth/refresh", json={"refresh_token": refresh}))


def test_there_is_no_signup_route(api: tuple[TestClient, str]) -> None:
    client, _ = api
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {p for p in paths if p.startswith("/auth")} == {
        "/auth/token",
        "/auth/refresh",
        "/auth/logout",
    }
    assert not any("signup" in p or "register" in p for p in paths)


def test_health_needs_no_token(api: tuple[TestClient, str]) -> None:
    client, _ = api
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_no_password_or_token_reaches_the_log(
    api: tuple[TestClient, str], server_log: io.StringIO
) -> None:
    client, password = api
    body = _login(client, "alice", password)
    client.post("/auth/token", json={"username": "alice", "password": "wrong-" + password})
    client.get("/me", headers=_bearer(body["access_token"]))

    logged = server_log.getvalue()
    for secret in (password, body["access_token"], body["refresh_token"]):
        assert secret not in logged


# ------------------------------------------------- a user who predates auth


def test_a_pre_existing_user_sees_exactly_their_bets(clean: Path) -> None:
    """The user `bet init` made owns bets recorded before credentials existed.

    Their token must resolve to that same scope; a user added later sees none
    of them.
    """
    bet_settings = Settings(data_dir=clean)
    with connect(bet_settings) as conn:
        migrator.apply(conn)
        local = identity.bootstrap(conn, display_name="tom")
        w = Warehouse(conn, local)
        account = w.accounts.add(
            SportsbookAccount(
                tenant_id=local.tenant_id,
                user_id=local.user_id,
                id=uuid.uuid4(),
                sportsbook_code="fanduel",
                label="fanduel",
            )
        )
        recorded = {
            w.bets.add(
                Bet(
                    tenant_id=local.tenant_id,
                    user_id=local.user_id,
                    id=uuid.uuid4(),
                    sportsbook_account_id=account.id,
                    placed_at=PLACED + timedelta(days=n),
                    cash_staked=Decimal("5.00"),
                )
            ).id
            for n in range(3)
        }
        tom_password, guest_password = _password(), _password()
        set_password_for(conn, "tom", tom_password)
        create_user(conn, "guest")
        set_password_for(conn, "guest", guest_password)

    app = create_app(ApiSettings(bet=bet_settings, auth=token_settings({SIGNING_KEY_ENV: _key()})))

    @app.get("/probe/bets")
    def probe(scope: CurrentScope, cursor: Cursor) -> dict[str, Any]:
        return {
            "user_id": str(scope.user_id),
            "bets": sorted(str(b.id) for b in Warehouse(cursor, scope).bets.fetch_all()),
        }

    with TestClient(app) as client:
        tom = _bearer(_login(client, "tom", tom_password)["access_token"])
        assert client.get("/me", headers=tom).json()["display_name"] == "tom"
        seen = client.get("/probe/bets", headers=tom).json()
        assert seen == {"user_id": str(local.user_id), "bets": sorted(map(str, recorded))}

        guest = _bearer(_login(client, "guest", guest_password)["access_token"])
        assert client.get("/probe/bets", headers=guest).json()["bets"] == []
