"""API read endpoints (SB-1399): bets, card, balances and similar over HTTP.

Each route is exercised through a TestClient over a temp warehouse holding real
rows -- bets with legs and promotions, balance snapshots and cash flows -- for
two users. The JSON must carry everything the CLI renders, so the remote CLI
can rebuild the same output from it.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import io
import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from bet import log
from bet.analytics.balance import COLUMNS as BALANCE_COLUMNS
from bet.analytics.card import card_to_dict
from bet.analytics.similar import COLUMNS as SIMILAR_COLUMNS
from bet.api import ApiSettings, balances, bets, card, create_app, similar
from bet.api.card import resolve_zone
from bet.api.deps import current_scope
from bet.api.settings import SIGNING_KEY_ENV, token_settings
from bet.cli.commands.list_cmd import _row
from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet, BetLeg, BetPromotion
from bet.models.ownership import OwnerScope, SportsbookAccount
from bet.services.auth import MAX_PASSWORD_LENGTH
from bet.services.balance import BalanceQuery, balance_report
from bet.services.bets import BetDetail, BetListing, ListQuery, bet_detail, list_bets
from bet.services.card import CardQuery, card_summary
from bet.services.similar import SimilarQuery, similar_bets
from bet.services.tickets import LegInput, build_legs
from bet.services.users import create_user, set_password_for

NOW = datetime.now(UTC).replace(microsecond=0)
NY = ZoneInfo("America/New_York")
JSON: TypeAdapter[Any] = TypeAdapter(Any)

# Every key card_to_dict leaves out of a shared card.
MONEY_KEYS = {
    "staked",
    "net",
    "exposure",
    "balances",
    "balance",
    "balance_total",
    "balance_change",
    "change",
}


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


# ------------------------------------------------------------- seed data


@dataclass
class Seeded:
    alice: OwnerScope
    bob: OwnerScope
    won: Bet
    parlay: Bet
    pending: Bet
    void: Bet
    bobs_bet: Bet


def _account(w: Warehouse, code: str, label: str | None = None) -> SportsbookAccount:
    return w.accounts.add(
        SportsbookAccount(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_code=code,
            label=label or code,
        )
    )


def _bet(
    w: Warehouse,
    account: SportsbookAccount,
    legs: list[dict[str, Any]],
    **fields: Any,
) -> Bet:
    defaults: dict[str, Any] = {
        "placed_at": NOW - timedelta(days=2),
        "cash_staked": Decimal("10.00"),
    }
    bet = w.bets.add(
        Bet(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            **(defaults | fields),
        )
    )
    for order, leg in enumerate(legs, start=1):
        w.legs.add(
            BetLeg(
                tenant_id=w.scope.tenant_id,
                user_id=w.scope.user_id,
                id=uuid.uuid4(),
                bet_id=bet.id,
                leg_order=order,
                **leg,
            )
        )
    return bet


def _settled(result: str, returned: str, days_ago: int = 1) -> dict[str, Any]:
    return {
        "status": "settled",
        "result": result,
        "settled_at": NOW - timedelta(days=days_ago),
        "cash_returned": Decimal(returned),
    }


def _snapshot(w: Warehouse, account: SportsbookAccount, at: datetime, amount: str) -> None:
    w.balances.add(
        BalanceSnapshot(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=account.id,
            observed_at=at,
            cash_balance=Decimal(amount),
        )
    )


def _seed(conn: Any) -> Seeded:
    alice = identity.bootstrap(conn, display_name="alice")
    w = Warehouse(conn, alice)
    fanduel = _account(w, "fanduel")
    boosts = _account(w, "draftkings", "boosts")

    chiefs = {"sport": "NFL", "selection_name": "Chiefs ML", "odds_american": 150}
    won = _bet(w, fanduel, [chiefs], odds_american_placed=150, **_settled("won", "25.00"))
    w.bet_promotions.add(
        BetPromotion(
            tenant_id=alice.tenant_id,
            user_id=alice.user_id,
            id=uuid.uuid4(),
            bet_id=won.id,
            promotion_type="profit_boost",
            generosity_pct=Decimal("25"),
        )
    )
    parlay = _bet(
        w,
        boosts,
        [
            {"sport": "NBA", "selection_name": "Celtics ML", "odds_american": -150},
            {"sport": "NBA", "selection_name": "Knicks +4.5", "odds_american": -110},
        ],
        wager_kind="parlay",
        odds_american_placed=264,
        placed_at=NOW - timedelta(days=3),
        **_settled("lost", "0.00"),
    )
    pending = _bet(
        w,
        fanduel,
        [{"sport": "NFL", "selection_name": "Bills -3.5", "odds_american": -110}],
        cash_staked=Decimal("5.00"),
        placed_at=NOW - timedelta(hours=3),
    )
    void = _bet(
        w,
        fanduel,
        [{"sport": "NFL", "selection_name": "Jets ML", "odds_american": 200}],
        placed_at=NOW - timedelta(days=4),
        **_settled("void", "10.00"),
    )

    _snapshot(w, fanduel, NOW - timedelta(days=10), "100.00")
    w.cash_flows.add(
        CashFlow(
            tenant_id=alice.tenant_id,
            user_id=alice.user_id,
            id=uuid.uuid4(),
            sportsbook_account_id=fanduel.id,
            kind="deposit",
            occurred_at=NOW - timedelta(days=5),
            amount=Decimal("20.00"),
        )
    )
    _snapshot(w, fanduel, NOW - timedelta(hours=1), "130.00")
    _snapshot(w, boosts, NOW - timedelta(days=6), "50.00")
    _snapshot(w, boosts, NOW - timedelta(hours=2), "40.00")

    bob = create_user(conn, "bob")
    bw = Warehouse(conn, bob)
    bobs_bet = _bet(bw, _account(bw, "fanduel"), [chiefs], **_settled("won", "25.00"))
    _snapshot(bw, bw.accounts.fetch_all()[0], NOW - timedelta(hours=1), "999.00")
    return Seeded(alice, bob, won, parlay, pending, void, bobs_bet)


@dataclass
class Api:
    app: FastAPI
    client: TestClient
    seeded: Seeded
    alice: dict[str, str]
    bob: dict[str, str]

    def get(self, path: str, **params: Any) -> Any:
        response = self.client.get(path, params=params, headers=self.alice)
        assert response.status_code == 200, response.text
        return response.json()

    def warehouse(self, scope: OwnerScope | None = None) -> Warehouse:
        """A scoped warehouse on the server's own connection, for expected values."""
        return Warehouse(self.app.state.conn.cursor(), scope or self.seeded.alice)


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/auth/token", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def api(clean: Path) -> Iterator[Api]:
    bet_settings = Settings(data_dir=clean)
    passwords = {name: secrets.token_urlsafe(18) for name in ("alice", "bob")}
    with connect(bet_settings) as conn:
        migrator.apply(conn)
        seeded = _seed(conn)
        for name, password in passwords.items():
            set_password_for(conn, name, password)

    settings = ApiSettings(
        bet=bet_settings, auth=token_settings({SIGNING_KEY_ENV: secrets.token_urlsafe(48)})
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield Api(
            app=app,
            client=client,
            seeded=seeded,
            alice=_login(client, "alice", passwords["alice"]),
            bob=_login(client, "bob", passwords["bob"]),
        )


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["bet"]["id"] for row in rows]


def _keys(value: Any) -> set[str]:
    """Every dict key anywhere inside ``value``."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


# ------------------------------------------------------------- protection

READ_ROUTES = {
    ("GET", "/bets"),
    ("GET", "/bets/{bet_id}"),
    ("GET", "/card"),
    ("GET", "/balances"),
    ("POST", "/similar"),
}


def _calls(dependant: Any) -> set[Any]:
    return {dependant.call} | {c for d in dependant.dependencies for c in _calls(d)}


def test_every_read_route_requires_a_scope(api: Api) -> None:
    # The routers' own routes: FastAPI includes a router lazily, so app.routes
    # holds a wrapper per router rather than its APIRoutes.
    routes = {
        (method, route.path): route
        for router in (bets.router, card.router, balances.router, similar.router)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert set(routes) == READ_ROUTES
    for key in READ_ROUTES:
        assert current_scope in _calls(routes[key].dependant), key

    served = api.client.get("/openapi.json").json()["paths"]
    assert {(m.upper(), p) for p, ops in served.items() for m in ops} >= READ_ROUTES


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/bets"),
        ("GET", f"/bets/{uuid.uuid4()}"),
        ("GET", "/card"),
        ("GET", "/balances"),
        ("POST", "/similar"),
    ],
)
def test_reads_without_a_token_are_401(api: Api, method: str, path: str) -> None:
    response = api.client.request(method, path, json={"legs": [{"sport": "NFL"}]})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["type"] == "AuthenticationError"


# ------------------------------------------------------------- GET /bets


def test_list_returns_every_current_bet_newest_first(api: Api) -> None:
    s = api.seeded
    rows = api.get("/bets")
    assert _ids(rows) == [str(b.id) for b in (s.pending, s.won, s.parlay, s.void)]


def test_list_rows_rebuild_the_cli_rows(api: Api) -> None:
    expected = list_bets(api.warehouse(), ListQuery())
    rebuilt = [BetListing.model_validate(row) for row in api.get("/bets")]
    assert rebuilt == expected
    assert [_row(r) for r in rebuilt] == [_row(r) for r in expected]


def test_list_serialises_money_as_strings_and_times_with_an_offset(api: Api) -> None:
    (row,) = [r for r in api.get("/bets") if r["bet"]["id"] == str(api.seeded.won.id)]
    bet = row["bet"]
    assert bet["cash_staked"] == "10.00"
    assert bet["cash_returned"] == "25.00"
    assert row["account_label"] == "fanduel"
    assert row["legs"][0]["selection_name"] == "Chiefs ML"
    for name in ("placed_at", "settled_at", "created_at"):
        assert datetime.fromisoformat(bet[name]).utcoffset() is not None


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"open": True}, ["pending"]),
        ({"status": "pending"}, ["pending"]),
        ({"status": "settled"}, ["won", "parlay", "void"]),
        ({"kind": "parlay"}, ["parlay"]),
        ({"sportsbook": "draftkings"}, ["parlay"]),
        ({"sport": "NBA"}, ["parlay"]),
        ({"text": "chiefs"}, ["won"]),
        ({"limit": 1}, ["pending"]),
    ],
)
def test_list_filters(api: Api, params: dict[str, Any], expected: list[str]) -> None:
    ids = [str(getattr(api.seeded, name).id) for name in expected]
    assert _ids(api.get("/bets", **params)) == ids


def test_list_date_bounds(api: Api) -> None:
    s = api.seeded
    day = (NOW - timedelta(days=3)).date()
    assert _ids(api.get("/bets", since=day.isoformat(), until=day.isoformat())) == [
        str(s.parlay.id)
    ]


def test_list_contradictory_filters_are_400(api: Api) -> None:
    response = api.client.get(
        "/bets", params={"open": True, "status": "settled"}, headers=api.alice
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "UsageError"


@pytest.mark.parametrize("params", [{"status": "open"}, {"kind": "moneyline"}, {"limit": 0}])
def test_list_unknown_values_are_422(api: Api, params: dict[str, Any]) -> None:
    response = api.client.get("/bets", params=params, headers=api.alice)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "ValidationError"
    assert error["details"][0]["loc"] == ["query", next(iter(params))]


def test_list_schema_names_the_known_statuses(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()
    params = {p["name"]: p for p in schema["paths"]["/bets"]["get"]["parameters"]}
    assert set(params["status"]["schema"]["anyOf"][0]["enum"]) == {"pending", "settled"}
    assert "same_game_parlay" in params["kind"]["schema"]["anyOf"][0]["enum"]
    assert "open" in params


def test_list_never_shows_another_users_bets(api: Api) -> None:
    rows = api.client.get("/bets", headers=api.bob).json()
    assert _ids(rows) == [str(api.seeded.bobs_bet.id)]


# ------------------------------------------------------------- GET /bets/{id}


def test_detail_round_trips(api: Api) -> None:
    won = api.seeded.won
    body = api.get(f"/bets/{won.id}")
    expected = bet_detail(api.warehouse(), won.id)
    rebuilt = BetDetail.model_validate(body)

    assert rebuilt.model_dump(exclude={"provenance"}) == expected.model_dump(exclude={"provenance"})
    assert body["provenance"] == JSON.dump_python(expected.provenance, mode="json")
    assert body["promotions"][0]["promotion_type"] == "profit_boost"
    assert body["promotions"][0]["generosity_pct"] == "25.0000"


@pytest.mark.parametrize("who", ["unknown", "other user"])
def test_detail_is_404_for_ids_this_user_does_not_own(api: Api, who: str) -> None:
    bet_id = uuid.uuid4() if who == "unknown" else api.seeded.bobs_bet.id
    response = api.client.get(f"/bets/{bet_id}", headers=api.alice)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "NotFoundError"
    assert str(bet_id) in error["message"]


def test_detail_with_a_malformed_id_is_422(api: Api) -> None:
    response = api.client.get("/bets/not-a-uuid", headers=api.alice)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["path", "bet_id"]


# ------------------------------------------------------------- GET /card


def test_card_equals_card_to_dict(api: Api) -> None:
    body = api.get("/card", tz="America/New_York", include_void=True)
    card = card_summary(api.warehouse(), CardQuery(tz=NY, include_void=True))
    assert body == JSON.dump_python(card_to_dict(card), mode="json")
    assert body["timezone"] == "America/New_York"
    assert body["record"]["label"] == "1-1"
    assert body["record"]["net"] == "5.00"
    assert body["voids"] == 1
    assert body["pending"] == {"count": 1, "exposure": "5.00"}
    assert [b["account"] for b in body["balances"]] == ["draftkings:boosts", "fanduel"]


def test_card_filters(api: Api) -> None:
    body = api.get("/card", sportsbook="FanDuel", sport="NFL", days=3)
    assert body["days"] == 3
    assert body["record"]["label"] == "1-0"
    assert [b["account"] for b in body["balances"]] == ["fanduel"]
    assert "voids" not in body


def test_share_card_has_no_money(api: Api) -> None:
    full = api.get("/card")
    shared = api.get("/card", share=True)
    assert _keys(full) >= MONEY_KEYS
    assert not _keys(shared) & MONEY_KEYS
    assert shared["record"]["label"] == full["record"]["label"]
    assert shared["best"]["bet_id"] == str(api.seeded.won.id)


def test_card_defaults_to_the_stored_zone_as_stored(api: Api) -> None:
    assert api.get("/card")["timezone"] == "UTC"

    cursor = api.app.state.conn.cursor()
    cursor.execute(
        "UPDATE core.user SET timezone = 'America/Chicago' WHERE id = ?",
        [str(api.seeded.alice.user_id)],
    )
    assert api.get("/card")["timezone"] == "America/Chicago"
    assert api.get("/card", tz="Asia/Tokyo")["timezone"] == "Asia/Tokyo"


@pytest.mark.parametrize("tz", ["Mars/Olympus_Mons", "../etc/passwd", ""])
def test_card_refuses_an_unknown_zone(api: Api, tz: str) -> None:
    response = api.client.get("/card", params={"tz": tz}, headers=api.alice)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "UsageError"
    assert "IANA" in error["remediation"]


def test_a_stored_zone_that_names_none_reads_as_utc() -> None:
    assert resolve_zone(None, "Not/AZone") == ZoneInfo("UTC")
    assert resolve_zone(None, None) == ZoneInfo("UTC")
    with pytest.raises(UsageError):
        resolve_zone("Not/AZone", "UTC")


@pytest.mark.parametrize("days", [0, 61])
def test_card_days_out_of_range_is_422(api: Api, days: int) -> None:
    response = api.client.get("/card", params={"days": days}, headers=api.alice)
    assert response.status_code == 422


# ------------------------------------------------------------- GET /balances


def test_balances_equal_the_report(api: Api) -> None:
    rows = api.get("/balances")
    expected = balance_report(api.warehouse(), BalanceQuery())
    assert rows == JSON.dump_python(expected, mode="json")
    assert [r["account"] for r in rows] == ["draftkings:boosts", "fanduel", "TOTAL"]
    assert all(set(BALANCE_COLUMNS) <= set(r) for r in rows)
    assert rows[1]["balance"] == "130.00"


def test_balances_history_shows_every_interval(api: Api) -> None:
    rows = api.get("/balances", history=True)
    assert rows == JSON.dump_python(
        balance_report(api.warehouse(), BalanceQuery(history=True)), mode="json"
    )
    assert [r["account"] for r in rows].count("fanduel") == 2


def test_balances_filters(api: Api) -> None:
    by_book = api.get("/balances", sportsbook="FanDuel")
    assert [r["account"] for r in by_book] == ["fanduel", "TOTAL"]
    by_label = api.get("/balances", account_label="boosts")
    assert [r["account"] for r in by_label] == ["draftkings:boosts", "TOTAL"]


def test_balances_refuse_an_unknown_sportsbook(api: Api) -> None:
    response = api.client.get("/balances", params={"sportsbook": "nope"}, headers=api.alice)
    assert response.status_code == 400
    assert "Known sportsbooks" in response.json()["error"]["remediation"]


def test_balances_are_scoped(api: Api) -> None:
    rows = api.client.get("/balances", headers=api.bob).json()
    assert [r["balance"] for r in rows] == ["999.00", "999.00"]


# ------------------------------------------------------------- POST /similar


def _similar(api: Api, body: dict[str, Any]) -> Any:
    return api.client.post("/similar", json=body, headers=api.alice)


def test_similar_equals_the_service(api: Api) -> None:
    body = {
        "legs": [{"sport": "NFL", "selection_name": "Chiefs ML", "odds_american": 150}],
        "promotions": [{"promotion_type": "profit_boost", "generosity_pct": "25"}],
    }
    response = _similar(api, body)
    assert response.status_code == 200, response.text
    rows = response.json()

    scope = api.seeded.alice
    legs = build_legs(
        [LegInput(sport="NFL", selection_name="Chiefs ML", odds_american=150)],
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
        bet_id=uuid.uuid4(),
    )
    thresholds = Settings().thresholds
    expected = similar_bets(
        api.warehouse(),
        SimilarQuery(
            legs=legs,
            wager_kind="straight",
            odds_american=150,
            promo_types=["profit_boost"],
            min_settled=thresholds.min_settled_bets,
            exploratory_below=thresholds.exploratory_below,
        ),
    )
    assert rows == JSON.dump_python(expected, mode="json")
    assert rows and all(set(r) == set(SIMILAR_COLUMNS) for r in rows)


def test_similar_compares_an_unpriced_parlay(api: Api) -> None:
    response = _similar(
        api,
        {
            "legs": [{"sport": "NBA"}, {"sport": "NBA"}],
            "wager_kind": "same_game_parlay",
            "sportsbook": "draftkings",
            "since": (NOW - timedelta(days=30)).date().isoformat(),
            "until": NOW.date().isoformat(),
            "include_void": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()


@pytest.mark.parametrize(
    "body",
    [
        {"legs": [{"sport": "NFL"}, {"sport": "NFL"}], "wager_kind": "straight"},
        {"legs": [{"sport": "NFL"}], "odds_american": 50},
        {"legs": [{"sport": "NFL"}], "promotions": [{"promotion_type": "nope"}]},
    ],
)
def test_similar_refuses_an_invalid_proposal(api: Api, body: dict[str, Any]) -> None:
    response = _similar(api, body)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "UsageError"


@pytest.mark.parametrize(
    "body",
    [
        {"legs": []},
        {"legs": [{"sport": "NFL"}], "extra": 1},
        {"legs": [{"colour": "red"}]},
        {"legs": [{"sport": "NFL"}], "wager_kind": "accumulator"},
    ],
)
def test_similar_malformed_bodies_are_422(api: Api, body: dict[str, Any]) -> None:
    response = _similar(api, body)
    assert response.status_code == 422
    assert response.json()["error"]["details"]


# ------------------------------------------------------------- error bodies


def test_unknown_route_has_the_bet_error_body(api: Api) -> None:
    response = api.client.get("/nowhere", headers=api.alice)
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "type": "NotFoundError",
            "message": "no route /nowhere.",
            "remediation": "See /docs for the available routes.",
        }
    }


def test_wrong_method_has_the_bet_error_body(api: Api) -> None:
    response = api.client.delete("/bets", headers=api.alice)
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
    error = response.json()["error"]
    assert error["type"] == "MethodNotAllowedError"
    assert error["message"] == "DELETE is not allowed on /bets."


def test_other_http_errors_keep_their_status(api: Api) -> None:
    @api.app.get("/teapot")
    def teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    response = api.client.get("/teapot")
    assert response.status_code == 418
    assert response.json()["error"] == {
        "type": "HTTPError",
        "message": "short and stout",
        "remediation": None,
    }


def test_validation_errors_never_echo_the_input(api: Api) -> None:
    password = secrets.token_urlsafe(MAX_PASSWORD_LENGTH)
    response = api.client.post("/auth/token", json={"username": "alice", "password": password})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "ValidationError"
    assert error["message"] == "the request is invalid."
    assert error["details"] == [
        {
            "loc": ["body", "password"],
            "msg": error["details"][0]["msg"],
            "type": "too_long",
        }
    ]
    assert password not in response.text
