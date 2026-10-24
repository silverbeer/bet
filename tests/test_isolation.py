"""Multi-user isolation on the server path (SB-1398, ADR 0002 section 5).

Three users: ``alice`` (A) and ``bob`` (B) share the local tenant, ``carol`` (C)
lives in a second tenant. Each has a password, an account, a source file, a
settled parlay and a pending single with legs, a promotion applied to a bet, a
balance snapshot and a cash flow. A is the first user in ``core.user`` -- the
one the CLI's ``identity.existing_scope`` shortcut would return -- so a server
path that fell back to it would hand A's data to everyone.

Every protected route is **discovered** from the app, not listed here, so an
endpoint added later is covered without anyone remembering to add it. A route
whose path parameters, query or body cannot be built automatically fails with
instructions; register it in ``ROUTE_CALLS`` (see below).

Every key and password here is generated per test and thrown away.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import itertools
import json
import os
import re
import secrets
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import duckdb
import httpx2 as httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from pydantic import SecretStr

import bet
from bet import log
from bet.analytics.card import card_to_dict
from bet.api import ApiSettings, create_app
from bet.api.deps import current_scope
from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import ScopedRepository, Warehouse
from bet.models.balance import BalanceSnapshot, CashFlow
from bet.models.bet import Bet, BetLeg, BetPromotion, Promotion
from bet.models.ownership import OwnerScope, SportsbookAccount
from bet.models.source import SourceFile
from bet.services import auth
from bet.services.balance import BalanceQuery, balance_report, require_known_sportsbook
from bet.services.capture import require_seeded
from bet.services.card import CardQuery, card_summary

PLACED = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
SETTLED = PLACED + timedelta(hours=4)

API_SOURCE = Path(bet.__file__).parent / "api"
SRC = Path(bet.__file__).parent

# Routes that are meant to answer without a token.
PUBLIC_PATHS = frozenset(
    {"/healthz", "/readyz", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
)
PUBLIC_PREFIXES = ("/auth/",)

# What user B (or C) may get back when aiming at user A's data: an empty or
# own-data success, or not found. Never 401 (the token is valid, so the route
# was not exercised), never 422 (the request was malformed, ditto).
ISOLATED_STATUSES = frozenset({200, 201, 204, 404})


# ------------------------------------------------------------ the seeded world


@dataclass(frozen=True)
class Seeded:
    """One user's credentials, ids and the strings that identify their data."""

    name: str
    password: str
    scope: OwnerScope
    account_id: UUID
    source_file_id: UUID
    settled_bet_id: UUID
    pending_bet_id: UUID
    leg_ids: tuple[UUID, ...]
    promotion_id: UUID
    bet_promotion_id: UUID
    snapshot_id: UUID
    cash_flow_id: UUID
    # Ids, labels and amounts that appear nowhere in any other user's data.
    # The tenant id is excluded: B shares it with A legitimately.
    markers: frozenset[str]


@dataclass(frozen=True)
class Amounts:
    settled_stake: str
    settled_return: str
    pending_stake: str
    balance: str
    deposit: str
    face_value: str
    delivered: str


# Synthetic figures, chosen so none of A's is a substring of anyone else's and
# none ends in 0 (so a float rendering is the same string as the Decimal).
AMOUNTS = {
    "alice": Amounts("37.19", "83.17", "41.73", "613.29", "271.43", "11.47", "6.83"),
    "bob": Amounts("5.00", "12.50", "6.00", "400.00", "50.00", "2.00", "1.00"),
    "carol": Amounts("10.00", "25.00", "8.00", "300.00", "40.00", "3.00", "1.50"),
}


def _seed(conn: duckdb.DuckDBPyConnection, scope: OwnerScope, name: str) -> Seeded:
    """Give ``scope`` one of everything owned, tagged so it can be recognised."""
    tag = f"{name.title()}-Isolation"
    money = AMOUNTS[name]
    owner: dict[str, Any] = {"tenant_id": scope.tenant_id, "user_id": scope.user_id}
    password = secrets.token_urlsafe(18)
    w = Warehouse(conn, scope)
    auth.set_password(w, password)

    with w.transaction():
        account = w.accounts.add(
            SportsbookAccount(
                **owner, id=uuid.uuid4(), sportsbook_code="fanduel", label=f"{tag} Account"
            )
        )
        source = w.sources.add(
            SourceFile(
                **owner,
                id=uuid.uuid4(),
                archived_path=f"{name}/slip.png",
                original_name=f"{tag}-slip.png",
                sha256=hashlib.sha256(tag.encode()).hexdigest(),
                byte_size=2048,
                media_type="image/png",
            )
        )
        settled = w.bets.add(
            Bet(
                **owner,
                id=uuid.uuid4(),
                sportsbook_account_id=account.id,
                source_file_id=source.id,
                capture_method="screenshot",
                external_bet_id=f"{tag}-EXT-1",
                placed_at=PLACED,
                settled_at=SETTLED,
                status="settled",
                result="won",
                wager_kind="parlay",
                settlement_source="amount",
                cash_staked=Decimal(money.settled_stake),
                cash_returned=Decimal(money.settled_return),
                notes=f"{tag} settled note",
            )
        )
        pending = w.bets.add(
            Bet(
                **owner,
                id=uuid.uuid4(),
                sportsbook_account_id=account.id,
                external_bet_id=f"{tag}-EXT-2",
                placed_at=PLACED + timedelta(days=1),
                cash_staked=Decimal(money.pending_stake),
                notes=f"{tag} pending note",
            )
        )
        legs = [
            w.legs.add(
                BetLeg(
                    **owner,
                    id=uuid.uuid4(),
                    bet_id=bet_id,
                    leg_order=order,
                    sport="baseball",
                    league="MLB",
                    event_label=f"{tag} Event {label}",
                    market_name="Moneyline",
                    selection_name=f"{tag} Selection {label}",
                    target_team=f"{tag} Team {label}",
                    result="won" if bet_id == settled.id else None,
                )
            )
            for bet_id, order, label in (
                (settled.id, 1, "S1"),
                (settled.id, 2, "S2"),
                (pending.id, 1, "P1"),
            )
        ]
        promotion = w.promotions.add(
            Promotion(
                **owner,
                id=uuid.uuid4(),
                sportsbook_code="fanduel",
                promotion_type="profit_boost",
                state="played",
                label=f"{tag} Boost",
                face_value=Decimal(money.face_value),
                awarded_at=PLACED - timedelta(days=1),
            )
        )
        applied = w.bet_promotions.add(
            BetPromotion(
                **owner,
                id=uuid.uuid4(),
                bet_id=settled.id,
                promotion_id=promotion.id,
                promotion_type="profit_boost",
                label=f"{tag} Boost Applied",
                triggered=True,
                value_delivered=Decimal(money.delivered),
            )
        )
        snapshot = w.balances.add(
            BalanceSnapshot(
                **owner,
                id=uuid.uuid4(),
                sportsbook_account_id=account.id,
                observed_at=SETTLED + timedelta(hours=1),
                cash_balance=Decimal(money.balance),
                notes=f"{tag} snapshot note",
            )
        )
        flow = w.cash_flows.add(
            CashFlow(
                **owner,
                id=uuid.uuid4(),
                sportsbook_account_id=account.id,
                kind="deposit",
                occurred_at=PLACED - timedelta(days=2),
                amount=Decimal(money.deposit),
                notes=f"{tag} deposit note",
            )
        )

    ids = [
        scope.user_id,
        account.id,
        source.id,
        settled.id,
        pending.id,
        *(leg.id for leg in legs),
        promotion.id,
        applied.id,
        snapshot.id,
        flow.id,
    ]
    labels = [
        name,
        tag,
        account.label,
        source.original_name,
        source.sha256,
        *(leg.selection_name or "" for leg in legs),
        *(leg.event_label or "" for leg in legs),
    ]
    return Seeded(
        name=name,
        password=password,
        scope=scope,
        account_id=account.id,
        source_file_id=source.id,
        settled_bet_id=settled.id,
        pending_bet_id=pending.id,
        leg_ids=tuple(leg.id for leg in legs),
        promotion_id=promotion.id,
        bet_promotion_id=applied.id,
        snapshot_id=snapshot.id,
        cash_flow_id=flow.id,
        markers=frozenset([*map(str, ids), *labels, *vars(money).values()]),
    )


@dataclass(frozen=True)
class World:
    settings: ApiSettings
    signing_key: str
    a: Seeded
    b: Seeded
    c: Seeded

    @property
    def users(self) -> dict[str, Seeded]:
        return {u.name: u for u in (self.a, self.b, self.c)}


@pytest.fixture(autouse=True)
def server_log() -> None:
    """Route structlog to a fresh stream; earlier CLI tests leave it on a closed one."""
    log.configure(json_logs=True, stream=io.StringIO())


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
def world(clean: Path) -> World:
    """The three users, seeded; the connection is closed before any server opens it."""
    bet_settings = Settings(data_dir=clean)
    with connect(bet_settings) as conn:
        migrator.apply(conn)
        a = identity.bootstrap(conn, display_name="alice")  # the "first user"
        b = identity.create_user(conn, a.tenant_id, "bob")
        other_tenant = uuid.uuid4()
        conn.execute(
            "INSERT INTO core.tenant (id, name) VALUES (?, ?)", [str(other_tenant), "elsewhere"]
        )
        c = identity.create_user(conn, other_tenant, "carol")
        seeded = [
            _seed(conn, scope, name) for scope, name in ((a, "alice"), (b, "bob"), (c, "carol"))
        ]

    key = secrets.token_urlsafe(48)
    settings = ApiSettings(bet=bet_settings, auth=auth.TokenSettings(signing_key=SecretStr(key)))
    return World(settings, key, *seeded)


@pytest.fixture
def tripwire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make the CLI's first-user shortcut fail, and record any call to it.

    Installed before the app is built, so construction, startup and every
    request run under it. ``resolve_scope`` is patched too, although it reaches
    ``existing_scope`` through the module global and would trip it anyway.
    """
    calls: list[str] = []

    def tripped(name: str) -> Callable[..., Any]:
        def refuse(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"the server path called identity.{name}")

        return refuse

    for name in ("existing_scope", "resolve_scope"):
        monkeypatch.setattr(identity, name, tripped(name))
    return calls


@dataclass
class Api:
    client: TestClient
    app: FastAPI
    world: World
    access: dict[str, str]
    refresh: dict[str, str]

    def bearer(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access[name]}"}

    def cursor(self) -> duckdb.DuckDBPyConnection:
        conn: duckdb.DuckDBPyConnection = self.app.state.conn
        return conn.cursor()


@pytest.fixture
def api(world: World, tripwire: list[str]) -> Iterator[Api]:
    """A served warehouse, and a token pair per user obtained by logging in."""
    app = create_app(world.settings)
    with TestClient(app) as client:
        access, refresh = {}, {}
        for name, user in world.users.items():
            response = client.post(
                "/auth/token", json={"username": name, "password": user.password}
            )
            assert response.status_code == 200, f"{name} could not log in"
            access[name] = response.json()["access_token"]
            refresh[name] = response.json()["refresh_token"]
        yield Api(client, app, world, access, refresh)
    assert tripwire == [], f"the server path used the first-user shortcut: {tripwire}"


# ------------------------------------------------------ route discovery and calls


@dataclass(frozen=True)
class Call:
    """One request aimed at a victim's data: path parameters, query and body."""

    path_params: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None
    # A raw body (an upload) instead of JSON, sent with these headers.
    content: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # Whether the victim's own response must contain their data. False for a
    # route whose success carries nothing back (a 204, say).
    owner_sees_data: bool = True


RouteKey = tuple[str, str]  # (method, path template), e.g. ("GET", "/bets/{bet_id}")
CallBuilder = Callable[[Seeded], list[Call]]

# The registry. A route that needs more than the automatic treatment -- a
# required query parameter, a request body, a path parameter not named in
# PATH_PARAMS, or several filter variants worth trying -- is registered here
# with a builder that aims the request at the victim's (user A's) data. The
# same calls are then made as B, as C, without a token, and as A.
#
#     ROUTE_CALLS[("GET", "/bets")] = lambda a: [
#         Call(),
#         Call(query={"sportsbook": "fanduel", "status": "settled"}),
#     ]
#     ROUTE_CALLS[("POST", "/bets/{bet_id}/settle")] = lambda a: [
#         Call(path_params={"bet_id": str(a.pending_bet_id)}, body={"result": "won"}),
#     ]
ROUTE_CALLS: dict[RouteKey, CallBuilder] = {}

# SB-1399 read endpoints. Filters are aimed at A's own data, so a leak would
# have to come back through them; the aggregate routes are also checked
# against the caller's own service result (test_aggregates_are_the_callers_own).
ROUTE_CALLS[("GET", "/bets")] = lambda a: [
    Call(),
    Call(query={"status": "settled", "sportsbook": "fanduel"}),
    Call(query={"open": "true"}),
    Call(query={"text": a.name.title()}),
    Call(query={"sport": "baseball", "kind": "parlay"}),
]
ROUTE_CALLS[("GET", "/card")] = lambda a: [
    Call(query={"days": "60", "tz": "America/New_York"}),
    Call(query={"days": "60", "sportsbook": "fanduel", "include_void": "true"}),
    # A share card carries no money and no balances: nothing of A's to show.
    Call(query={"days": "60", "share": "true"}, owner_sees_data=False),
]
ROUTE_CALLS[("GET", "/balances")] = lambda a: [
    Call(),
    Call(query={"sportsbook": "fanduel", "history": "true"}),
]
# Similar returns aggregates (counts and sums), not rows that name a bet;
# its isolation is proven by the aggregate test, so the owner check is off.
ROUTE_CALLS[("POST", "/similar")] = lambda a: [
    Call(
        body={"legs": [{"sport": "baseball", "league": "MLB", "market_name": "Moneyline"}]},
        owner_sees_data=False,
    ),
]

# SB-1400 write endpoints. Each is aimed at A's ids: B and C must get 404
# before anything is written, and A's rows must be unchanged afterwards. The
# ledger writes cite A's source file, which is what B and C must be refused.
WRITE_AMOUNT = "1.11"


def _cites(a: Seeded) -> dict[str, str]:
    return {"capture_method": "screenshot", "source_file_id": str(a.source_file_id)}


ROUTE_CALLS[("POST", "/bets")] = lambda a: [
    Call(
        body={
            "sportsbook": "fanduel",
            "legs": [{"sport": "baseball", "odds_american": -110}],
            "cash_staked": WRITE_AMOUNT,
            **_cites(a),
        }
    ),
]
ROUTE_CALLS[("POST", "/bets/{bet_id}/settle")] = lambda a: [
    Call(path_params={"bet_id": str(a.pending_bet_id)}, body={"result": "lost"}),
    Call(path_params={"bet_id": str(a.settled_bet_id)}, body={"result": "lost", "force": True}),
]
ROUTE_CALLS[("POST", "/balances")] = lambda a: [
    Call(body={"sportsbook": "fanduel", "cash_balance": WRITE_AMOUNT, **_cites(a)}),
]
ROUTE_CALLS[("POST", "/cash-flows")] = lambda a: [
    Call(body={"kind": "deposit", "sportsbook": "fanduel", "amount": WRITE_AMOUNT, **_cites(a)}),
    Call(body={"kind": "withdrawal", "sportsbook": "fanduel", "amount": WRITE_AMOUNT, **_cites(a)}),
]
# A's own image bytes (see _seed). A gets A's existing row back; anyone else
# gets a new row of their own, and must not learn that A holds the image.
ROUTE_CALLS[("POST", "/sources")] = lambda a: [
    Call(content=f"{a.name.title()}-Isolation".encode(), headers={"Content-Type": "image/png"}),
]

# Path parameters filled in automatically, by name, with every matching id of
# the victim's. A route whose parameters are all named here needs no entry in
# ROUTE_CALLS; the calls are the product of the candidates.
PATH_PARAMS: dict[str, Callable[[Seeded], list[UUID]]] = {
    "bet_id": lambda u: [u.settled_bet_id, u.pending_bet_id],
    "leg_id": lambda u: list(u.leg_ids),
    "account_id": lambda u: [u.account_id],
    "sportsbook_account_id": lambda u: [u.account_id],
    "source_file_id": lambda u: [u.source_file_id],
    "promotion_id": lambda u: [u.promotion_id],
    "snapshot_id": lambda u: [u.snapshot_id],
    "balance_id": lambda u: [u.snapshot_id],
    "cash_flow_id": lambda u: [u.cash_flow_id],
}

_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


@dataclass(frozen=True)
class Endpoint:
    """One method on one route, with the path and dependencies it is served with."""

    method: str
    path: str
    dependant: Dependant
    has_body: bool


def protected_routes(app: FastAPI) -> list[Endpoint]:
    """Every endpoint that must require a token, as the app serves it.

    ``app.routes`` holds included routers unexpanded, so routes are walked with
    ``iter_route_contexts`` -- what FastAPI's own OpenAPI generation uses --
    which yields each route with its router prefix applied. Unlike the OpenAPI
    document, it also sees routes excluded from the schema.
    """
    found = [
        Endpoint(
            method=method,
            path=str(context.path),
            dependant=context.dependant,
            has_body=context.body_field is not None or bool(context.dependant.body_params),
        )
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and not is_public(str(context.path))
        for method in sorted(context.methods or ())
    ]
    return sorted(found, key=lambda e: (e.path, e.method))


def _unregistered(method: str, path: str, reason: str) -> None:
    pytest.fail(
        f"{method} {path} cannot be exercised by the isolation suite automatically: "
        f"{reason}. Register a builder in tests/test_isolation.py, "
        f"ROUTE_CALLS[({method!r}, {path!r})] = lambda a: [Call(...)], aiming the "
        "request at user A's ids, filters and body, so every user is checked "
        "against it.",
        pytrace=False,
    )


def calls_for(endpoint: Endpoint, victim: Seeded) -> list[Call]:
    """The requests that aim ``endpoint`` at ``victim``'s data."""
    method, path = endpoint.method, endpoint.path
    builder = ROUTE_CALLS.get((method, path))
    if builder is not None:
        return builder(victim)

    if endpoint.has_body:
        _unregistered(method, path, "it takes a request body")
    required_query = [p.name for p in endpoint.dependant.query_params if p.field_info.is_required()]
    if required_query:
        _unregistered(method, path, f"it requires query parameters {required_query}")
    names = _PARAM.findall(path)
    unknown = [name for name in names if name not in PATH_PARAMS]
    if unknown:
        _unregistered(method, path, f"no id is known for path parameters {unknown}")

    candidates = [[str(i) for i in PATH_PARAMS[name](victim)] for name in names]
    return [
        Call(path_params=dict(zip(names, combo, strict=True)))
        for combo in itertools.product(*candidates)
    ]


def _url(template: str, call: Call) -> str:
    return _PARAM.sub(lambda m: call.path_params[m.group(1)], template)


def send(
    client: TestClient, method: str, template: str, call: Call, headers: dict[str, str]
) -> httpx.Response:
    response: httpx.Response = client.request(
        method,
        _url(template, call),
        params=call.query or None,
        json=call.body,
        content=call.content,
        headers=call.headers | headers,
    )
    return response


def _discovery_app() -> FastAPI:
    """An app built only to read its routes: no warehouse is opened."""
    scratch = Path(tempfile.gettempdir()) / "bet-isolation-route-discovery"
    return create_app(
        ApiSettings(
            bet=Settings(data_dir=scratch, state_dir=scratch / "state"),
            auth=auth.TokenSettings(signing_key=SecretStr(secrets.token_urlsafe(48))),
        )
    )


PROTECTED: list[RouteKey] = [(e.method, e.path) for e in protected_routes(_discovery_app())]
_IDS = [f"{m} {p}" for m, p in PROTECTED]


def _endpoint(app: FastAPI, method: str, path: str) -> Endpoint:
    for endpoint in protected_routes(app):
        if (endpoint.method, endpoint.path) == (method, path):
            return endpoint
    raise AssertionError(f"{method} {path} is not a route of the served app")


def _depends_on(dependant: Dependant, target: Callable[..., Any]) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def owned_rows(w: Warehouse) -> dict[str, list[str]]:
    """Every row the scope owns, per repository, for before-and-after comparison.

    Found by reflection over the unit of work, so a repository added later is
    included without being listed.
    """
    return {
        name: sorted(row.model_dump_json() for row in repo.fetch_all())
        for name, repo in vars(w).items()
        if isinstance(repo, ScopedRepository)
    }


def _supplied(call: Call | None) -> str:
    """Everything the caller sent. Echoing a caller's own input back (a 404
    naming the id they asked for) discloses nothing they did not already hold."""
    if call is None:
        return ""
    parts = [*call.path_params.values(), *call.query.values()]
    if call.body is not None:
        parts.append(json.dumps(call.body, default=str))
    if call.content is not None:
        # The digest of the caller's own bytes is theirs to compute.
        parts.append(hashlib.sha256(call.content).hexdigest())
    return "\n".join(parts)


def _leaked(response: httpx.Response, victim: Seeded, call: Call | None = None) -> list[str]:
    sent = _supplied(call)
    return sorted(m for m in victim.markers if m in response.text and m not in sent)


# ------------------------------------------------------------- discovery itself


def test_discovery_finds_the_protected_surface() -> None:
    """If this shrinks to nothing, the parametrized tests below prove nothing."""
    assert ("GET", "/me") in PROTECTED
    assert not any(is_public(path) for _, path in PROTECTED)


def test_an_unbuildable_route_fails_loudly_rather_than_skipping() -> None:
    app = FastAPI()

    @app.get("/widgets/{widget_id}")
    def widget(widget_id: str) -> None:  # pragma: no cover - never served
        pass

    @app.post("/widgets")
    def make(body: dict[str, str]) -> None:  # pragma: no cover - never served
        pass

    @app.get("/search")
    def search(q: str) -> None:  # pragma: no cover - never served
        pass

    victim = Seeded(
        name="x",
        password="",
        scope=OwnerScope(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        account_id=uuid.uuid4(),
        source_file_id=uuid.uuid4(),
        settled_bet_id=uuid.uuid4(),
        pending_bet_id=uuid.uuid4(),
        leg_ids=(),
        promotion_id=uuid.uuid4(),
        bet_promotion_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        cash_flow_id=uuid.uuid4(),
        markers=frozenset(),
    )
    endpoints = protected_routes(app)
    assert len(endpoints) == 3
    for endpoint in endpoints:
        with pytest.raises(pytest.fail.Exception, match="ROUTE_CALLS"):
            calls_for(endpoint, victim)


def test_known_path_parameters_are_filled_with_the_victims_ids(world: World) -> None:
    app = FastAPI()

    @app.get("/bets/{bet_id}")
    def one(bet_id: str) -> None:  # pragma: no cover - never served
        pass

    (endpoint,) = protected_routes(app)
    urls = {_url(endpoint.path, call) for call in calls_for(endpoint, world.a)}
    assert urls == {f"/bets/{world.a.settled_bet_id}", f"/bets/{world.a.pending_bet_id}"}


# ----------------------------------------------------- every protected endpoint


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=_IDS)
def test_every_protected_route_depends_on_the_token_scope(method: str, path: str) -> None:
    """Structural: the route's dependency tree contains ``current_scope``."""
    endpoint = _endpoint(_discovery_app(), method, path)
    assert _depends_on(endpoint.dependant, current_scope), (
        f"{method} {path} does not depend on CurrentScope; every non-public route must."
    )


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=_IDS)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-jwt"}],
    ids=["no-token", "garbage-token"],
)
def test_every_protected_route_is_401_without_a_token(
    api: Api, method: str, path: str, headers: dict[str, str]
) -> None:
    for call in calls_for(_endpoint(api.app, method, path), api.world.a):
        response = send(api.client, method, path, call, headers)
        assert response.status_code == 401, f"{method} {_url(path, call)}"
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert not _leaked(response, api.world.a)


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=_IDS)
def test_no_other_user_reaches_user_as_data(api: Api, method: str, path: str) -> None:
    """As B (same tenant) and C (another tenant), aimed at A's ids: 404 or nothing of A's.

    Then the same calls as A must succeed and show A's data -- otherwise B's
    404 would only prove the request was wrong.
    """
    a = api.world.a
    calls = calls_for(_endpoint(api.app, method, path), a)
    before = owned_rows(Warehouse(api.cursor(), a.scope))

    for viewer in ("bob", "carol"):
        for call in calls:
            response = send(api.client, method, path, call, api.bearer(viewer))
            where = f"{viewer} -> {method} {_url(path, call)}"
            assert response.status_code in ISOLATED_STATUSES, (
                f"{where} answered {response.status_code}; expected 404 or an "
                "empty/own-data success. Fix the route or its ROUTE_CALLS entry."
            )
            leaked = _leaked(response, a, call)
            assert not leaked, f"{where} leaked user A's data: {leaked}"

    assert owned_rows(Warehouse(api.cursor(), a.scope)) == before, (
        f"another user's {method} {path} changed user A's rows"
    )

    for call in calls:
        response = send(api.client, method, path, call, api.bearer("alice"))
        where = f"alice -> {method} {_url(path, call)}"
        assert 200 <= response.status_code < 300, f"{where} answered {response.status_code}"
        if call.owner_sees_data:
            assert _leaked(response, a), (
                f"{where} shows none of A's own data, so B's result proves nothing. "
                "Aim its ROUTE_CALLS entry at A's data, or set owner_sees_data=False."
            )


# --------------------------------------------------- scope only from the token


def test_the_api_source_never_names_the_first_user_shortcut() -> None:
    for module in sorted(API_SOURCE.rglob("*.py")):
        text = module.read_text()
        for name in ("existing_scope", "resolve_scope"):
            assert name not in text, f"{module.relative_to(SRC)} references identity.{name}"


def test_the_shortcut_is_never_imported_by_name() -> None:
    """Only ``identity.existing_scope`` attribute access, so the tripwire sees every call."""
    for module in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names}
                assert not names & {"existing_scope", "resolve_scope"}, (
                    f"{module.relative_to(SRC)} imports the shortcut by name"
                )


def test_serving_never_calls_the_shortcut(api: Api, tripwire: list[str]) -> None:
    """Startup, login, /me and a refresh all ran under the tripwire (see ``api``)."""
    assert api.client.get("/me", headers=api.bearer("bob")).json()["display_name"] == "bob"
    rotated = api.client.post("/auth/refresh", json={"refresh_token": api.refresh["carol"]})
    assert rotated.status_code == 200
    assert tripwire == []


def test_the_tripwire_is_armed(world: World, tripwire: list[str]) -> None:
    with connect(world.settings.bet, create=False) as conn, pytest.raises(AssertionError):
        identity.existing_scope(conn)
    assert tripwire == ["existing_scope"]
    tripwire.clear()


# ------------------------------------------------------------ tokens and scope


def _claims(token: str) -> dict[str, Any]:
    return dict(jwt.decode(token, options={"verify_signature": False}))


def _forge(world: World, scope: OwnerScope, jti: str, typ: str = "refresh") -> str:
    """A correctly signed token naming any scope and jti: what only the key holder can mint."""
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "sub": str(scope.user_id),
        "tid": str(scope.tenant_id),
        "iat": now,
        "exp": now + 600,
        "jti": jti,
        "typ": typ,
    }
    return jwt.encode(claims, world.signing_key, algorithm=auth.ALGORITHM)


def _swap_sub(token: str, user_id: UUID) -> str:
    """Change the payload's ``sub`` without re-signing."""
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = str(user_id)
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"


def test_logging_in_as_a_yields_a_scope_that_sees_only_a(api: Api) -> None:
    a = api.world.a
    claims = _claims(api.access["alice"])
    assert (claims["tid"], claims["sub"]) == (str(a.scope.tenant_id), str(a.scope.user_id))

    cursor = api.cursor()
    scope = auth.authenticate(cursor, api.access["alice"], api.world.settings.auth)
    assert scope == a.scope
    rows = owned_rows(Warehouse(cursor, scope))
    owners = {(r["tenant_id"], r["user_id"]) for rs in rows.values() for r in map(json.loads, rs)}
    assert owners == {(str(a.scope.tenant_id), str(a.scope.user_id))}
    assert {b.id for b in Warehouse(cursor, scope).bets.fetch_all()} == {
        a.settled_bet_id,
        a.pending_bet_id,
    }
    assert api.client.get("/me", headers=api.bearer("alice")).json()["display_name"] == "alice"


def test_each_users_token_resolves_to_that_user(api: Api) -> None:
    cursor = api.cursor()
    for name, user in api.world.users.items():
        assert auth.authenticate(cursor, api.access[name], api.world.settings.auth) == user.scope


def test_b_logging_out_does_not_touch_as_session(api: Api) -> None:
    out = api.client.post("/auth/logout", json={"refresh_token": api.refresh["bob"]})
    assert out.status_code == 204
    rotated = api.client.post("/auth/refresh", json={"refresh_token": api.refresh["alice"]})
    assert rotated.status_code == 200


@pytest.mark.parametrize("route", ["/auth/refresh", "/auth/logout"])
def test_a_token_in_bs_name_cannot_use_as_refresh_token(api: Api, route: str) -> None:
    """Even correctly signed: the refresh-token row is looked up in the claimed scope."""
    a, b = api.world.a, api.world.b
    a_jti = _claims(api.refresh["alice"])["jti"]
    for forged in (
        _forge(api.world, b.scope, a_jti),  # B's identity, A's token id
        _swap_sub(api.refresh["bob"], a.scope.user_id),  # B's token, re-pointed at A
        _swap_sub(api.refresh["alice"], b.scope.user_id),  # A's token, re-pointed at B
    ):
        response = api.client.post(route, json={"refresh_token": forged})
        assert response.status_code == 401

    a_row = Warehouse(api.cursor(), a.scope).refresh_tokens.get(UUID(a_jti))
    assert a_row.revoked_at is None
    assert (
        api.client.post("/auth/refresh", json={"refresh_token": api.refresh["alice"]}).status_code
        == 200
    )


def test_bs_access_token_is_not_a_refresh_token_for_anyone(api: Api) -> None:
    for route in ("/auth/refresh", "/auth/logout"):
        response = api.client.post(route, json={"refresh_token": api.access["bob"]})
        assert response.status_code == 401


def test_the_tenant_is_part_of_the_identity(api: Api) -> None:
    """A's user id in C's tenant names nobody: the token is refused."""
    a, c = api.world.a, api.world.c
    elsewhere = OwnerScope(tenant_id=c.scope.tenant_id, user_id=a.scope.user_id)
    access = _forge(api.world, elsewhere, str(uuid.uuid4()), typ="access")
    assert api.client.get("/me", headers={"Authorization": f"Bearer {access}"}).status_code == 401
    a_jti = _claims(api.refresh["alice"])["jti"]
    for route in ("/auth/refresh", "/auth/logout"):
        response = api.client.post(
            route, json={"refresh_token": _forge(api.world, elsewhere, a_jti)}
        )
        assert response.status_code == 401


# ----------------------------------------------------------- shared reference data


def test_reference_data_is_shared_by_every_user(world: World) -> None:
    """Sportsbooks and the reference taxonomies read the same for A, B and C.

    No endpoint exposes them yet, so this goes through the service and
    repository layer the endpoints will use.
    """
    with connect(world.settings.bet, create=False) as conn:
        codes = {
            name: Warehouse(conn, u.scope).sportsbook_codes() for name, u in world.users.items()
        }
        assert "fanduel" in codes["alice"]
        assert codes["alice"] == codes["bob"] == codes["carol"]
        for user in world.users.values():
            w = Warehouse(conn, user.scope)
            require_seeded(w, "fanduel")
            (account,) = w.accounts.fetch_all()
            assert account.sportsbook_code == "fanduel"
        require_known_sportsbook(conn, "fanduel")
        wager_kinds = {
            r[0] for r in conn.execute("SELECT code FROM reference.wager_kind").fetchall()
        }
        assert {"straight", "parlay"} <= wager_kinds


# ------------------------------------------------------- aggregates (SB-1399)


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _as_json(value: Any) -> Any:
    """JSON-comparable form: timestamps parsed, so ``Z`` and ``+00:00`` agree."""
    encoded = json.loads(
        json.dumps(value, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))
    )
    return _instants(encoded)


def _instants(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _instants(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_instants(v) for v in value]
    if isinstance(value, str) and _ISO.match(value):
        return datetime.fromisoformat(value)
    return value


def test_aggregates_are_the_callers_own(api: Api) -> None:
    """A leaked total names no bet, so markers cannot catch it. Instead, B's
    card and balances must equal what the services compute for B's scope alone,
    and B's similar-bet counts and stakes must come only from B's one settled bet.
    """
    w = Warehouse(api.cursor(), api.world.b.scope)
    tz = ZoneInfo("America/New_York")

    card = api.client.get(
        "/card", params={"days": 60, "tz": "America/New_York"}, headers=api.bearer("bob")
    )
    assert card.status_code == 200
    expected_card = card_to_dict(card_summary(w, CardQuery(tz=tz, days=60)), share=False)
    assert _instants(card.json()) == _as_json(expected_card)

    balances = api.client.get("/balances", headers=api.bearer("bob"))
    assert balances.status_code == 200
    expected_rows = balance_report(w, BalanceQuery())
    assert _instants(balances.json()) == _as_json(expected_rows)

    similar = api.client.post(
        "/similar",
        json={"legs": [{"sport": "baseball", "league": "MLB", "market_name": "Moneyline"}]},
        headers=api.bearer("bob"),
    )
    assert similar.status_code == 200
    rows = similar.json()
    assert rows, "similar returned nothing to check"
    own_stake = Decimal(AMOUNTS["bob"].settled_stake)
    for row in rows:
        assert int(row["bets"]) <= 1, f"B's similar row counts more bets than B has: {row}"
        if row.get("staked") is not None and int(row["bets"]) == 1:
            assert Decimal(str(row["staked"])) == own_stake, (
                f"B's similar stake {row['staked']} is not B's own {own_stake}"
            )


# ---------------------------------------------------------- writes (SB-1400)


def _rows_of(api: Api, user: Seeded) -> dict[str, list[str]]:
    return owned_rows(Warehouse(api.cursor(), user.scope))


def test_b_settling_as_bet_is_404_and_leaves_it_unchanged(api: Api) -> None:
    a = api.world.a
    before = _rows_of(api, a)
    for bet_id in (a.pending_bet_id, a.settled_bet_id):
        response = api.client.post(
            f"/bets/{bet_id}/settle",
            json={"result": "won", "cash_returned": WRITE_AMOUNT, "force": True},
            headers=api.bearer("bob"),
        )
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "NotFoundError"
    assert _rows_of(api, a) == before
    pending = Warehouse(api.cursor(), a.scope).bets.get(a.pending_bet_id)
    assert (pending.status, pending.result) == ("pending", None)


@pytest.mark.parametrize("viewer", ["bob", "carol"])
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/bets",
            {"sportsbook": "fanduel", "legs": [{"odds_american": -110}], "cash_staked": "1.11"},
        ),
        ("/balances", {"sportsbook": "fanduel", "cash_balance": "1.11"}),
        ("/cash-flows", {"kind": "deposit", "sportsbook": "fanduel", "amount": "1.11"}),
    ],
)
def test_citing_as_source_is_refused_like_an_unknown_id(
    api: Api, viewer: str, path: str, body: dict[str, Any]
) -> None:
    a, b = api.world.a, api.world.users[viewer]
    before = {u: _rows_of(api, u) for u in (a, b)}
    unknown = uuid.uuid4()

    answers = []
    for source_id in (a.source_file_id, unknown):
        cited = body | {"capture_method": "screenshot", "source_file_id": str(source_id)}
        response = api.client.post(path, json=cited, headers=api.bearer(viewer))
        assert response.status_code == 404, response.text
        error = response.json()["error"]
        answers.append((error["type"], error["message"].replace(str(source_id), "<id>")))
        assert not _leaked(response, a, Call(body=cited))

    assert answers[0] == answers[1], "A's source id is distinguishable from an unknown one"
    assert {u: _rows_of(api, u) for u in (a, b)} == before


def test_bs_writes_land_only_in_bs_scope(api: Api) -> None:
    a, b, c = api.world.a, api.world.b, api.world.c
    others = {u.name: _rows_of(api, u) for u in (a, c)}
    bob = api.bearer("bob")

    upload = api.client.post(
        "/sources", content=b"bob's own slip", headers=bob | {"Content-Type": "image/png"}
    )
    assert upload.status_code == 201
    source_id = upload.json()["source_file_id"]
    cited = {"capture_method": "screenshot", "source_file_id": source_id}
    added = api.client.post(
        "/bets",
        json={"sportsbook": "fanduel", "legs": [{"odds_american": 120}], "cash_staked": "2.00"}
        | cited,
        headers=bob,
    )
    assert added.status_code == 201, added.text
    bet_id = added.json()["bet"]["id"]
    settled = api.client.post(f"/bets/{bet_id}/settle", json={"result": "won"}, headers=bob)
    assert settled.status_code == 200, settled.text
    for path, body in (
        ("/balances", {"sportsbook": "fanduel", "cash_balance": "4.40"}),
        ("/cash-flows", {"kind": "withdrawal", "sportsbook": "fanduel", "amount": "1.00"}),
    ):
        response = api.client.post(path, json=body | cited, headers=bob)
        assert response.status_code == 201, response.text

    w = Warehouse(api.cursor(), b.scope)
    assert w.sources.get(UUID(source_id)).capture_method == "screenshot"
    bet_row = w.bets.get(UUID(bet_id))
    assert (bet_row.status, bet_row.source_file_id) == ("settled", UUID(source_id))
    assert bet_row.sportsbook_account_id == b.account_id
    assert len(w.balances.fetch_all()) == 2 and len(w.cash_flows.fetch_all()) == 2
    assert {u.name: _rows_of(api, u) for u in (a, c)} == others


def test_uploading_as_bytes_tells_b_nothing(api: Api) -> None:
    """Same image, two users: B gets a row of B's own, as if nobody had it."""
    a, b = api.world.a, api.world.b
    image = f"{a.name.title()}-Isolation".encode()  # A's archived bytes (see _seed)
    png = {"Content-Type": "image/png"}

    as_b = api.client.post("/sources", content=image, headers=api.bearer("bob") | png)
    assert as_b.status_code == 201
    body = as_b.json()
    assert body["already_archived"] is False
    assert body["source_file_id"] != str(a.source_file_id)
    assert body["original_name"] == "upload.png"
    assert Warehouse(api.cursor(), b.scope).sources.get(UUID(body["source_file_id"]))

    as_a = api.client.post("/sources", content=image, headers=api.bearer("alice") | png)
    assert as_a.status_code == 200
    assert as_a.json()["source_file_id"] == str(a.source_file_id)
    assert as_a.json()["already_archived"] is True
