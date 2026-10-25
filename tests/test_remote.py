"""Remote mode (SB-1401): the CLI against the BET API, output identical to local mode.

One temp warehouse, seeded through the local CLI, is served in-process by the
real FastAPI app. Each command then runs twice against it -- once locally
(``api_url`` unset) and once remotely (``api_url`` set, every HTTP call routed
into the app) -- and the two outputs must be the same: stdout, stderr, exit
code and, for a refusal, the error's message and remediation.

The remote runs point ``data_dir`` and ``db_path`` at a directory that does not
exist, and every test checks it still does not: remote mode never opens, or
creates, a local warehouse. Tokens live in the in-memory keyring from
``conftest.py``, never the real Keychain.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from rich.prompt import Confirm, Prompt
from typer.testing import CliRunner, Result

from bet import log
from bet.analytics.card import card_from_dict, card_to_dict
from bet.api import ApiSettings, create_app
from bet.api.settings import SIGNING_KEY_ENV, token_settings
from bet.cli.backend import RemoteBackend, _local_times, api_session
from bet.cli.main import app, run
from bet.client import api as api_module
from bet.client.api import ApiClient, error_from
from bet.client.tokens import SERVICE, Session, TokenStore
from bet.config import Settings, resolve
from bet.database import identity
from bet.database.connection import connect
from bet.errors import (
    ApiUnreachableError,
    AuthenticationError,
    BetError,
    ConfigError,
    DatabaseError,
    NotFoundError,
    UsageError,
)
from bet.services import auth
from bet.services.card import CardQuery
from bet.services.users import set_password_for
from tests.conftest import MemoryKeyring

URL = "http://bet.test"
NOW = datetime.now(UTC).replace(microsecond=0)
IMAGE = b"\x89PNG\r\n\x1a\n" + b"synthetic slip " * 8
JSON: TypeAdapter[Any] = TypeAdapter(Any)

runner = CliRunner()


def _at(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat()


@dataclass
class World:
    data: Path
    nowhere: Path
    tmp: Path
    app: FastAPI
    client: TestClient
    username: str
    password: str
    ids: dict[str, str]

    def _env(self, remote: bool) -> dict[str, str | None]:
        if remote:
            return {
                "BET_API_URL": URL,
                "BET_DATA_DIR": str(self.nowhere),
                "BET_DB_PATH": str(self.nowhere / "bet.duckdb"),
            }
        return {"BET_API_URL": None, "BET_DATA_DIR": str(self.data), "BET_DB_PATH": None}

    def local(self, *args: str, stdin: str | None = None) -> Result:
        return runner.invoke(app, list(args), env=self._env(False), input=stdin)

    def remote(self, *args: str, stdin: str | None = None) -> Result:
        return runner.invoke(app, list(args), env=self._env(True), input=stdin)

    def store(self) -> TokenStore:
        return TokenStore(URL)

    def session(self) -> Session:
        session = self.store().load()
        assert session is not None
        return session


def _ok(result: Result) -> Result:
    assert result.exit_code == 0, (result.output, result.exception)
    return result


def _json(result: Result) -> Any:
    return json.loads(_ok(result).stdout)


def _added(world: World, *args: str) -> str:
    rows = _json(world.local("--format", "json", "add", "--non-interactive", *args))
    return str(rows[0]["id"])


def _seed(world: World) -> None:
    ids = world.ids
    ids["won"] = _added(
        world,
        "--sportsbook",
        "fanduel",
        "--leg",
        "sport=NFL,selection=Chiefs ML,odds=+150",
        "--stake",
        "10",
        "--placed-at",
        _at(days=3),
    )
    _ok(world.local("settle", ids["won"], "--result", "won", "--settled-at", _at(days=1)))
    ids["parlay"] = _added(
        world,
        "--sportsbook",
        "fanduel",
        "--leg",
        "sport=NBA,selection=Celtics ML,odds=-150",
        "--leg",
        "sport=NBA,selection=Knicks +4.5,odds=-110",
        "--stake",
        "5",
        "--placed-at",
        _at(days=2),
    )
    _ok(world.local("settle", ids["parlay"], "--result", "lost", "--settled-at", _at(days=1)))
    ids["void"] = _added(
        world,
        "--sportsbook",
        "fanduel",
        "--leg",
        "sport=NFL,selection=Jets ML,odds=+200",
        "--stake",
        "10",
        "--placed-at",
        _at(days=4),
    )
    _ok(world.local("settle", ids["void"], "--result", "void", "--settled-at", _at(days=2)))
    ids["pending"] = _added(
        world,
        "--sportsbook",
        "draftkings",
        "--account-label",
        "boosts",
        "--leg",
        "sport=NFL,selection=Bills -3.5,odds=-110",
        "--stake",
        "5",
        "--placed-at",
        _at(hours=3),
    )
    for args in (
        ("balance", "add", "--sportsbook", "fanduel", "--amount", "100", "--at", _at(days=10)),
        ("deposit", "--sportsbook", "fanduel", "--amount", "20", "--at", _at(days=5)),
        ("balance", "add", "--sportsbook", "fanduel", "--amount", "130", "--at", _at(hours=1)),
        ("balance", "add", "--sportsbook", "draftkings", "--amount", "50", "--at", _at(days=6)),
        ("balance", "add", "--sportsbook", "draftkings", "--amount", "40", "--at", _at(hours=2)),
    ):
        _ok(world.local(*args))


def _routed(client: TestClient) -> Any:
    def factory(api_url: str) -> AbstractContextManager[httpx.Client]:
        assert api_url == URL
        return nullcontext(client)

    return factory


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    for name in list(dict(os.environ)):
        if name.startswith(("BET_", "XDG_")) or name == "WEB_CONCURRENCY":
            monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    nowhere = tmp_path / "nowhere"

    _ok(runner.invoke(app, ["init"]))
    settings = Settings(data_dir=data)
    password = secrets.token_urlsafe(18)
    with connect(settings) as conn:
        scope = identity.existing_scope(conn)
        assert scope is not None
        user = identity.find_user(conn, scope)
        assert user is not None
        username = user.display_name
        set_password_for(conn, username, password)

    api = create_app(
        ApiSettings(bet=settings, auth=token_settings({SIGNING_KEY_ENV: secrets.token_urlsafe(48)}))
    )
    with TestClient(api, base_url=URL) as client:
        monkeypatch.setattr(api_module, "http_client", _routed(client))
        w = World(data, nowhere, tmp_path, api, client, username, password, {})
        _seed(w)
        _ok(w.remote("login", "--username", username, "--password-stdin", stdin=password + "\n"))
        log.configure()
        yield w
    assert not nowhere.exists(), "remote mode created a local data directory"


def _error(result: Result) -> tuple[int, str, str | None]:
    exc = result.exception
    assert isinstance(exc, BetError), (result.output, exc)
    return exc.exit_code, exc.message, exc.remediation


def _same(world: World, *args: str) -> Result:
    """Run ``args`` locally and remotely, and require the same outcome."""
    argv = [a.format(**world.ids) for a in args]
    local, remote = world.local(*argv), world.remote(*argv)
    assert remote.exit_code == local.exit_code, (local.output, remote.output, remote.exception)
    if isinstance(local.exception, BetError):
        assert _error(remote) == _error(local)
    else:
        assert local.exception is None or isinstance(local.exception, SystemExit)
    assert remote.stdout == local.stdout
    assert remote.stderr == local.stderr
    return local


# ------------------------------------------------------------- read parity

READS: list[tuple[str, ...]] = [
    ("list",),
    ("--format", "json", "list"),
    ("--format", "csv", "list"),
    ("--sportsbook", "fanduel", "list"),
    ("--sport", "NBA", "list"),
    ("list", "--open"),
    ("list", "--status", "settled", "--limit", "2"),
    ("list", "--kind", "parlay"),
    ("--since", NOW.date().isoformat(), "list"),
    ("bets", "show", "{won}"),
    ("--format", "json", "bets", "show", "{parlay}"),
    ("--format", "csv", "bets", "show", "{pending}"),
    ("bets", "open"),
    ("--format", "json", "bets", "search", "Chiefs"),
    ("card",),
    ("card", "--share"),
    ("--format", "json", "card"),
    ("--format", "json", "card", "--share"),
    ("--sportsbook", "fanduel", "card", "--days", "3"),
    ("--include-void", "--sport", "NFL", "card"),
    ("similar", "--leg", "sport=NFL,selection=Chiefs ML,odds=+150"),
    ("--format", "json", "similar", "--leg", "sport=NBA,selection=Celtics ML", "--odds", "-150"),
    ("--include-void", "similar", "--leg", "sport=NFL,odds=+200", "--wager-kind", "straight"),
    ("balance",),
    ("balance", "--history"),
    ("--verbose", "balance"),
    ("--format", "json", "balance"),
    ("--format", "csv", "balance", "--history"),
    ("balance", "--sportsbook", "draftkings"),
    ("balance", "--account-label", "fanduel"),
]


@pytest.mark.parametrize("args", READS, ids=" ".join)
def test_reads_match_local_mode(world: World, args: tuple[str, ...]) -> None:
    result = _same(world, *args)
    assert result.stdout.strip()


def test_the_card_carries_real_figures(world: World) -> None:
    """Guards the parity tests against two empty outputs agreeing."""
    card = _json(world.remote("--format", "json", "--include-void", "card"))
    assert card["record"]["label"] == "1-1"
    assert card["voids"] == 1
    assert card["pending"] == {"count": 1, "exposure": "5.00"}
    assert [b["account"] for b in card["balances"]] == ["draftkings:boosts", "fanduel"]


ERRORS: list[tuple[str, ...]] = [
    ("bets", "show", "00000000-0000-4000-8000-000000000000"),
    ("bets", "show", "not-a-uuid"),
    ("list", "--status", "bogus"),
    ("list", "--kind", "bogus"),
    ("list", "--open", "--status", "settled"),
    ("balance", "--sportsbook", "nosuchbook"),
    ("card", "--format", "csv"),
    ("--format", "csv", "card"),
    ("--since", "2026-01-01", "card"),
    ("similar",),
    ("similar", "--leg", "sport"),
    ("similar", "--leg", "sport=NFL,odds=+150", "--wager-kind", "bogus"),
    ("similar", "--leg", "sport=NFL", "--odds", "+50"),
    ("settle", "{won}", "--result", "won"),
    ("settle", "00000000-0000-4000-8000-000000000000", "--result", "won"),
    ("settle", "{pending}", "--result", "bogus"),
    ("add", "--non-interactive", "--sportsbook", "nosuchbook", "--leg", "odds=+100"),
    ("add", "--non-interactive", "--sportsbook", "fanduel", "--leg", "odds=+100"),
    ("balance", "add", "--sportsbook", "draftkings", "--account-label", "[x]", "--amount", "5"),
    ("deposit", "--sportsbook", "fanduel", "--amount", "0"),
]


@pytest.mark.parametrize("args", ERRORS, ids=" ".join)
def test_errors_match_local_mode(world: World, args: tuple[str, ...]) -> None:
    result = _same(world, *args)
    assert result.exit_code != 0


def test_error_output_and_exit_code_match_through_the_entry_point(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = "00000000-0000-4000-8000-000000000000"
    outcomes = []
    for env in (world._env(False), world._env(True)):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        monkeypatch.setattr("sys.argv", ["bet", "bets", "show", missing])
        with pytest.raises(SystemExit) as exit_call:
            run()
        outcomes.append((exit_call.value.code, capsys.readouterr()))
    (local_code, local_out), (remote_code, remote_out) = outcomes
    assert local_code == remote_code == NotFoundError.exit_code
    assert remote_out == local_out
    assert missing in remote_out.err


# ------------------------------------------------------------ write parity


def _without(rows: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k not in keys} for row in rows]


def _bet_args(*extra: str) -> list[str]:
    return [
        "add",
        "--non-interactive",
        "--sportsbook",
        "fanduel",
        "--leg",
        "sport=NFL,selection=Ravens ML,odds=-120",
        "--leg",
        "sport=NFL,selection=Lions ML,odds=+110",
        "--promo",
        "type=profit_boost,generosity_pct=25",
        "--stake",
        "5",
        "--placed-at",
        _at(minutes=5),
        *extra,
    ]


def test_add_matches_local_mode(world: World) -> None:
    local = _json(world.local("--format", "json", *_bet_args()))
    remote = _json(world.remote("--format", "json", *_bet_args()))
    assert _without(remote, "id") == _without(local, "id")
    assert remote[0]["id"] != local[0]["id"]
    _same(world, "--format", "json", "bets", "show", remote[0]["id"])


UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _anonymous(text: str) -> str:
    """``text`` with every id blanked: two writes never share one."""
    return UUID_TEXT.sub("<id>", text)


def test_add_table_output_matches_local_mode(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "300")  # wide enough that no id is cut short
    local, remote = _ok(world.local(*_bet_args())), _ok(world.remote(*_bet_args()))
    assert "<id>" in _anonymous(local.stdout)
    assert _anonymous(remote.stdout) == _anonymous(local.stdout)
    assert remote.stderr == local.stderr


def test_add_uploads_the_source_file_first(world: World) -> None:
    slip = world.tmp / "IMG_0001.PNG"
    slip.write_bytes(IMAGE)
    args = _bet_args("--capture-method", "screenshot", "--source-file", str(slip))
    added = _json(world.remote("--format", "json", *args))[0]
    assert added["capture_method"] == "screenshot"
    assert added["source_file_id"] is not None

    shown = _json(world.remote("--format", "json", "bets", "show", added["id"]))
    assert {row["source_file_id"] for row in shown} == {added["source_file_id"]}
    archive = world.data / "sources"
    assert any(p.read_bytes() == IMAGE for p in archive.rglob("*") if p.is_file())

    # The same bytes again are the same archived file.
    again = _json(world.remote("--format", "json", *args))[0]
    assert again["source_file_id"] == added["source_file_id"]


def test_an_upload_the_server_refuses_is_a_usage_error(world: World) -> None:
    statement = world.tmp / "statement.pdf"
    statement.write_bytes(b"%PDF-1.7 synthetic")
    result = world.remote(*_bet_args("--capture-method", "pdf", "--source-file", str(statement)))
    exit_code, message, _ = _error(result)
    assert exit_code == UsageError.exit_code
    assert "application/pdf" in message


def test_settle_matches_local_mode(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "300")  # wide enough that no id is cut short
    first = _added(world, "--sportsbook", "fanduel", "--leg", "odds=+100", "--stake", "4")
    second = _added(world, "--sportsbook", "fanduel", "--leg", "odds=+100", "--stake", "4")
    settled_at = _at(minutes=1)
    args = ("--result", "won", "--settled-at", settled_at)
    local, remote = world.local("settle", first, *args), world.remote("settle", second, *args)
    assert _anonymous(_ok(remote).stdout) == _anonymous(_ok(local).stdout)
    local_json = _json(world.local("--format", "json", "bets", "show", first))
    remote_json = _json(world.remote("--format", "json", "bets", "show", second))
    assert _without(remote_json, "bet_id", "placed_at") == _without(
        local_json, "bet_id", "placed_at"
    )


def test_settle_with_leg_results_and_a_return(world: World) -> None:
    bet_id = _added(
        world,
        "--sportsbook",
        "fanduel",
        "--leg",
        "odds=+100",
        "--leg",
        "odds=+100",
        "--stake",
        "4",
    )
    rows = _json(
        world.remote(
            "--format",
            "json",
            "settle",
            bet_id,
            "--leg-result",
            "1=won",
            "--leg-result",
            "2=won",
            "--result",
            "cashed_out",
            "--return",
            "7.5",
        )
    )
    assert rows[0]["result"] == "cashed_out"
    assert rows[0]["cash_returned"] == "7.50"


@pytest.mark.parametrize(
    "command",
    [
        ("balance", "add", "--sportsbook", "fanduel", "--amount", "125.5", "--notes", "n"),
        ("deposit", "--sportsbook", "fanduel", "--amount", "15"),
        ("withdraw", "--sportsbook", "fanduel", "--amount", "15"),
        ("balance", "add", "--sportsbook", "betmgm", "--amount", "9", "--account-label", "main"),
    ],
    ids=" ".join,
)
def test_ledger_writes_match_local_mode(world: World, command: tuple[str, ...]) -> None:
    args = [*command, "--at", _at(minutes=2)]
    local = _json(world.local("--format", "json", *args))
    remote_result = world.remote("--format", "json", *args)
    remote = _json(remote_result)
    assert _without(remote, "id") == _without(local, "id")


def test_a_new_account_is_announced_in_both_modes(world: World) -> None:
    local = world.local("deposit", "--sportsbook", "betmgm", "--amount", "3")
    assert "Created account betmgm." in _ok(local).stderr
    remote = world.remote("deposit", "--sportsbook", "caesars", "--amount", "3")
    assert "Created account caesars." in _ok(remote).stderr


def test_ledger_writes_upload_their_source_file(world: World) -> None:
    shot = world.tmp / "balance.png"
    shot.write_bytes(IMAGE + b"balance")
    for command in (
        ("balance", "add", "--sportsbook", "fanduel", "--amount", "131"),
        ("deposit", "--sportsbook", "fanduel", "--amount", "1"),
    ):
        rows = _json(
            world.remote(
                "--format",
                "json",
                *command,
                "--capture-method",
                "screenshot",
                "--source-file",
                str(shot),
            )
        )
        assert rows[0]["source_file_id"] is not None


def test_interactive_settle_walks_open_bets_remotely(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    newest = _added(world, "--sportsbook", "fanduel", "--leg", "odds=+100", "--stake", "4")
    prompts = iter(["won", "won", "", ""])  # leg result, ticket result, amount, settled-at
    confirms = iter([True, True])  # the newest bet: "settle now?" then "confirm?"
    monkeypatch.setattr(Prompt, "ask", lambda *a, **k: next(prompts))
    monkeypatch.setattr(Confirm, "ask", lambda *a, **k: next(confirms, False))

    result = _ok(world.remote("settle"))
    assert "1 bet(s) settled" in result.output
    shown = _json(world.remote("--format", "json", "bets", "show", newest))
    assert shown[0]["status"] == "settled"


def test_interactive_add_is_refused_remotely(world: World) -> None:
    exit_code, message, remediation = _error(world.remote("add"))
    assert exit_code == UsageError.exit_code
    assert "--non-interactive" in message
    assert remediation is not None and "--non-interactive" in remediation


# ------------------------------------------------------------------ tokens


def _expired_session(world: World) -> Session:
    """A real pair whose access token lapsed an hour ago; its refresh token is good."""
    cursor = world.app.state.conn.cursor()
    pair = auth.login(
        cursor,
        world.username,
        world.password,
        world.app.state.settings.auth,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    return Session(world.username, pair.access_token, pair.refresh_token)


def test_an_expired_access_token_is_refreshed_and_rotated(world: World) -> None:
    expired = _expired_session(world)
    world.store().save(expired)
    _ok(world.remote("list"))
    renewed = world.session()
    assert renewed.access_token != expired.access_token
    assert renewed.refresh_token != expired.refresh_token
    # The rotated-away refresh token no longer works.
    response = world.client.post("/auth/refresh", json={"refresh_token": expired.refresh_token})
    assert response.status_code == 401


def test_a_failed_refresh_clears_the_tokens(world: World) -> None:
    world.store().save(Session(world.username, "not-a-token", "not-a-token"))
    exit_code, message, remediation = _error(world.remote("list"))
    assert exit_code == AuthenticationError.exit_code
    assert "expired or was revoked" in message
    assert remediation is not None and "bet login" in remediation
    assert world.store().load() is None


def test_a_reused_refresh_token_revokes_the_session(world: World) -> None:
    original = _expired_session(world)
    world.store().save(original)
    _ok(world.remote("list"))  # rotates: original.refresh_token is now spent
    current = world.session()

    world.store().save(Session(world.username, "not-a-token", original.refresh_token))
    assert _error(world.remote("list"))[0] == AuthenticationError.exit_code
    assert world.store().load() is None
    # Reuse revoked the whole chain, including the token rotated in.
    response = world.client.post("/auth/refresh", json={"refresh_token": current.refresh_token})
    assert response.status_code == 401


def test_a_rejected_retry_is_an_authentication_error(world: World) -> None:
    """A token refused even right after a good refresh: no loop, one clear error."""
    calls: list[str] = []
    real = world.client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/auth/refresh":
            return real.post("/auth/refresh", content=request.content, headers=request.headers)
        return httpx.Response(
            401,
            json={"error": {"type": "AuthenticationError", "message": "no.", "remediation": "x"}},
        )

    with httpx.Client(base_url=URL, transport=httpx.MockTransport(handler)) as http:
        client = ApiClient(URL, http, world.store())
        with pytest.raises(AuthenticationError) as raised:
            client.get("/bets")
    assert calls == ["/bets", "/auth/refresh", "/bets"]
    assert raised.value.remediation == f"Run `bet login` to sign in to {URL}."


def test_not_logged_in_is_an_authentication_error(world: World) -> None:
    world.store().clear()
    exit_code, message, remediation = _error(world.remote("list"))
    assert exit_code == AuthenticationError.exit_code
    assert message == f"not logged in to {URL}."
    assert remediation == f"Run `bet login` to sign in to {URL}."


def test_tokens_are_keyed_by_api_url(world: World, memory_keyring: MemoryKeyring) -> None:
    assert list(memory_keyring.items) == [(SERVICE, URL)]
    stored = json.loads(memory_keyring.items[(SERVICE, URL)])
    assert stored["username"] == world.username
    assert set(stored) == {"username", "access_token", "refresh_token"}


def test_no_token_appears_in_a_repr(world: World) -> None:
    session = world.session()
    for text in (
        repr(session),
        repr(world.store()),
        repr(ApiClient(URL, world.client, world.store())),
    ):
        assert session.access_token not in text
        assert session.refresh_token not in text


# ------------------------------------------------------------ login/logout


def test_login_never_prints_a_token_or_password(world: World) -> None:
    result = _ok(
        world.remote(
            "--format",
            "json",
            "login",
            "-u",
            world.username,
            "--password-stdin",
            stdin=world.password,
        )
    )
    session = world.session()
    for secret in (world.password, session.access_token, session.refresh_token):
        assert secret not in result.output
    assert json.loads(result.stdout) == [
        {"user": world.username, "api_url": URL, "action": "logged in"}
    ]


def test_login_prompts_for_what_it_is_not_given(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.store().clear()
    asked: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return world.password

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    result = _ok(world.remote("login", stdin=world.username + "\n"))
    assert asked == ["Password: "]
    assert world.password not in result.output
    assert world.session().username == world.username


def test_a_wrong_password_stores_nothing(world: World) -> None:
    world.store().clear()
    result = world.remote("login", "-u", world.username, "--password-stdin", stdin="wrong")
    exit_code, message, remediation = _error(result)
    assert exit_code == AuthenticationError.exit_code
    assert "wrong" not in message
    assert remediation is not None and "set-password" in remediation
    assert world.store().load() is None


@pytest.mark.parametrize(("username", "password"), [("someone", "\n"), ("  ", "pw")])
def test_login_needs_a_name_and_a_password(world: World, username: str, password: str) -> None:
    result = world.remote("login", "-u", username, "--password-stdin", stdin=password)
    assert _error(result)[0] == UsageError.exit_code


def test_logout_revokes_the_session_and_forgets_it(world: World) -> None:
    session = world.session()
    rows = _json(world.remote("--format", "json", "logout"))
    assert rows == [{"user": world.username, "api_url": URL, "action": "logged out"}]
    assert world.store().load() is None
    response = world.client.post("/auth/refresh", json={"refresh_token": session.refresh_token})
    assert response.status_code == 401

    again = _json(world.remote("--format", "json", "logout"))
    assert again[0]["action"] == "not logged in"


def _refusing(exc: Exception) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    def factory(api_url: str) -> AbstractContextManager[httpx.Client]:
        return httpx.Client(base_url=api_url, transport=httpx.MockTransport(handler))

    return factory


def test_logout_still_forgets_the_tokens_when_the_server_is_down(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "http_client", _refusing(httpx.ConnectError("refused")))
    result = _ok(world.remote("--format", "json", "logout"))
    assert json.loads(result.stdout)[0]["action"] == "logged out locally"
    assert "could not reach" in result.stderr
    assert world.store().load() is None


def test_logout_still_forgets_the_tokens_when_the_server_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"type": "DatabaseError", "message": "down."}})

    monkeypatch.setattr(
        api_module,
        "http_client",
        lambda url: httpx.Client(base_url=url, transport=httpx.MockTransport(handler)),
    )
    result = _ok(world.remote("logout"))
    assert "refused to revoke" in result.stderr
    assert world.store().load() is None


@pytest.mark.parametrize("command", [("login",), ("logout",)])
def test_login_and_logout_need_an_api_url(world: World, command: tuple[str, ...]) -> None:
    exit_code, message, _ = _error(world.local(*command))
    assert exit_code == UsageError.exit_code
    assert "api_url" in message


# -------------------------------------------------------------- unreachable


def test_an_unreachable_api_is_a_clear_error(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(api_module, "http_client", _refusing(httpx.ConnectError("refused")))
    for key, value in world._env(True).items():
        assert value is not None
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["bet", "list"])
    with pytest.raises(SystemExit) as exit_call:
        run()
    assert exit_call.value.code == ApiUnreachableError.exit_code == DatabaseError.exit_code
    err = capsys.readouterr().err
    assert URL in err
    assert "Tailscale" in err
    assert "Traceback" not in err


def test_a_timeout_is_unreachable_too(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module, "http_client", _refusing(httpx.ReadTimeout("slow")))
    assert isinstance(world.remote("card").exception, ApiUnreachableError)


def test_a_real_refused_connection_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real HTTP client, against a local port nothing listens on."""
    url = "http://127.0.0.1:9"
    monkeypatch.setenv("BET_API_URL", url)
    monkeypatch.setenv("BET_DATA_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    TokenStore(url).save(Session("someone", "a", "r"))
    result = runner.invoke(app, ["list"])
    assert isinstance(result.exception, ApiUnreachableError)
    assert url in result.exception.message
    assert not (tmp_path / "nowhere").exists()


def test_a_response_that_is_not_bet_json_is_reported(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    monkeypatch.setattr(
        api_module,
        "http_client",
        lambda url: httpx.Client(base_url=url, transport=httpx.MockTransport(handler)),
    )
    exit_code, message, _ = _error(world.remote("list"))
    assert exit_code == BetError.exit_code
    assert message == f"the BET API at {URL} answered HTTP 502."


def test_error_bodies_map_onto_bet_errors() -> None:
    def response(status: int, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, json={"error": body})

    validation = error_from(
        response(
            422,
            {
                "type": "ValidationError",
                "message": "the request is invalid.",
                "remediation": "See `details`.",
                "details": [{"loc": ["body"], "msg": "Field required", "type": "missing"}],
            },
        ),
        URL,
    )
    assert type(validation) is UsageError
    assert validation.remediation == "See `details`. Field required"

    teapot = error_from(response(418, {"type": "Teapot", "message": "short and stout"}), URL)
    assert type(teapot) is BetError
    assert teapot.message == "short and stout"

    by_status = error_from(response(404, {"type": "Mystery", "message": "gone"}), URL)
    assert type(by_status) is NotFoundError
    no_remediation = error_from(response(415, {"type": "X", "message": "m", "details": []}), URL)
    assert no_remediation.remediation is None


# ------------------------------------------------------------- doctor


def _doctor(result: Result) -> dict[str, dict[str, str]]:
    return {row["check"]: row for row in json.loads(result.stdout)}


def test_doctor_reports_remote_mode_and_api_health(world: World) -> None:
    rows = _doctor(_ok(world.remote("--format", "json", "doctor")))
    assert rows["mode"]["detail"] == f"remote: {URL}"
    assert rows["api health"]["status"] == "pass"
    assert rows["api readiness"] == {
        "check": "api readiness",
        "status": "pass",
        "detail": "/readyz: ready",
        "remediation": "",
    }
    assert rows["api login"]["detail"] == f"logged in as {world.username}"
    assert not {"database", "schema version", "local identity"} & set(rows)
    session = world.session()
    table = _ok(world.remote("doctor")).stdout
    assert session.access_token not in table and session.refresh_token not in table


def test_doctor_warns_when_not_logged_in(world: World) -> None:
    world.store().clear()
    rows = _doctor(_ok(world.remote("--format", "json", "doctor")))
    assert rows["api login"]["status"] == "warn"
    assert "bet login" in rows["api login"]["remediation"]


def test_doctor_fails_on_a_rejected_session(world: World) -> None:
    world.store().save(Session(world.username, "bad", "bad"))
    result = world.remote("--format", "json", "doctor")
    assert result.exit_code != 0
    assert _doctor(result)["api login"]["status"] == "fail"


def test_doctor_fails_when_the_api_is_unreachable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "http_client", _refusing(httpx.ConnectError("refused")))
    result = world.remote("--format", "json", "doctor")
    assert isinstance(result.exception, BetError)
    rows = _doctor(result)
    for name in ("api health", "api readiness", "api login"):
        assert rows[name]["status"] == "fail"
        assert URL in rows[name]["detail"]


def test_doctor_reports_local_mode(world: World) -> None:
    rows = _doctor(world.local("--format", "json", "doctor"))
    assert rows["mode"]["detail"] == "local (api_url unset)"
    assert "database" in rows


# ------------------------------------------------- local-only commands


@pytest.mark.parametrize(
    "command",
    [("init",), ("user", "create", "carol"), ("user", "set-password", "carol")],
    ids=" ".join,
)
def test_admin_commands_refuse_in_remote_mode(world: World, command: tuple[str, ...]) -> None:
    exit_code, message, remediation = _error(world.remote(*command, stdin="pw\npw\n"))
    assert exit_code == UsageError.exit_code
    assert "remote mode" in message
    assert remediation is not None and "kubectl exec" in remediation and URL in remediation


def test_config_works_in_remote_mode(world: World) -> None:
    rows = _json(world.remote("--format", "json", "config", "show"))
    assert {"setting": "api_url", "value": URL, "source": "environment"} in rows


# ------------------------------------------------------------- config


def test_api_url_is_normalised(tmp_path: Path) -> None:
    env = {"BET_API_URL": " https://mini.example.ts.net:8443/ ", "BET_DATA_DIR": str(tmp_path)}
    resolved = resolve(config_path=tmp_path / "none.toml", env=env)
    assert resolved.settings.api_url == "https://mini.example.ts.net:8443"
    assert (
        resolve(
            config_path=tmp_path / "none.toml", env={"BET_DATA_DIR": str(tmp_path)}
        ).settings.api_url
        is None
    )


@pytest.mark.parametrize("url", ["ftp://mini", "mini:8000", "http://"])
def test_api_url_must_be_http(tmp_path: Path, url: str) -> None:
    with pytest.raises(ConfigError):
        resolve(
            config_path=tmp_path / "none.toml",
            env={"BET_API_URL": url, "BET_DATA_DIR": str(tmp_path)},
        )


def test_api_url_is_settable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(dict(os.environ)):
        if name.startswith("BET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _ok(runner.invoke(app, ["config", "set", "api_url", URL]))
    assert resolve().settings.api_url == URL


# --------------------------------------------------------------- pieces


def test_remote_card_needs_a_named_zone(world: World) -> None:
    with api_session(URL) as client, pytest.raises(UsageError, match="IANA"):
        RemoteBackend(client).card(CardQuery(tz=timezone(timedelta(hours=9)), days=7))


def test_card_from_dict_inverts_card_to_dict(world: World) -> None:
    headers = {"Authorization": f"Bearer {world.session().access_token}"}
    body = world.client.get("/card", params={"include_void": True}, headers=headers).json()
    card = card_from_dict(body)
    assert JSON.dump_python(card_to_dict(card), mode="json") == body
    assert card_from_dict(card_to_dict(card)) == card


def test_local_times_moves_timestamps_to_this_machine() -> None:
    utc = "2026-03-01T12:00:00Z"
    moved = _local_times({"placed_at": utc, "rows": [{"settled_at": utc}], "note_at": "soon"})
    expected = datetime(2026, 3, 1, 12, tzinfo=UTC).astimezone().isoformat()
    assert moved == {"placed_at": expected, "rows": [{"settled_at": expected}], "note_at": "soon"}
    assert _local_times({"created_at": "2026-03-01T12:00:00"}) == {
        "created_at": "2026-03-01T12:00:00"
    }


def test_a_corrupt_keychain_item_reads_as_logged_out(memory_keyring: MemoryKeyring) -> None:
    memory_keyring.items[(SERVICE, URL)] = "{not json"
    assert TokenStore(URL).load() is None
    assert TokenStore(URL).clear() is True
    assert TokenStore(URL).clear() is False


def test_a_keychain_failure_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyring.errors import KeyringLocked

    class Locked(MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise KeyringLocked("locked")

        def set_password(self, service: str, username: str, password: str) -> None:
            raise KeyringLocked("locked")

        def delete_password(self, service: str, username: str) -> None:
            raise KeyringLocked("locked")

    import keyring

    keyring.set_keyring(Locked())
    store = TokenStore(URL)
    for action in (store.load, store.clear, lambda: store.save(Session("u", "a", "r"))):
        with pytest.raises(ConfigError) as raised:
            action()
        assert "keychain" in raised.value.message


def test_a_malformed_token_pair_is_reported_without_its_contents() -> None:
    leaked = "tok-" + secrets.token_urlsafe(12)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": leaked})

    transport = httpx.MockTransport(handler)
    with httpx.Client(base_url=URL, transport=transport) as http, pytest.raises(BetError) as raised:
        ApiClient(URL, http, TokenStore(URL)).login("someone", "pw")
    assert leaked not in str(raised.value)
    assert raised.value.__cause__ is None
    assert TokenStore(URL).load() is None
