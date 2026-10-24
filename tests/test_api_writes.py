"""API write endpoints (SB-1400): bets, settlement, balances, cash flows and uploads.

Each route runs through a TestClient over a temp warehouse. Refusals are
compared with the CLI's: the same input is given to the real ``bet`` command,
against a temp warehouse of its own, and the API's ``message`` and
``remediation`` must be the text the CLI raised.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import io
import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bet import log
from bet.api import ApiSettings, create_app
from bet.api import sources as sources_api
from bet.api.errors import status_for
from bet.api.settings import SIGNING_KEY_ENV, token_settings
from bet.cli.main import app as cli_app
from bet.config import Settings
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import BetError
from bet.models.ownership import OwnerScope
from bet.services.add import AddBetRequest, add_bet
from bet.services.bets import BetDetail, bet_detail
from bet.services.ledger import RecordedCashFlow, RecordedSnapshot
from bet.services.sources import SourceToArchive
from bet.services.tickets import LegInput
from bet.services.users import create_user, set_password_for

runner = CliRunner()

# Not a real PNG: nothing sniffs the bytes, and synthetic ones cannot be personal.
IMAGE = b"\x89PNG\r\n\x1a\n" + b"synthetic slip " * 8
PNG = {"Content-Type": "image/png"}


@pytest.fixture(autouse=True)
def server_log() -> None:
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


@dataclass
class Api:
    app: FastAPI
    client: TestClient
    archive: Path
    scopes: dict[str, OwnerScope]
    tokens: dict[str, dict[str, str]] = field(default_factory=dict)

    def post(self, path: str, who: str = "alice", **kwargs: Any) -> httpx.Response:
        headers = self.tokens[who] | kwargs.pop("headers", {})
        response: httpx.Response = self.client.post(path, headers=headers, **kwargs)
        return response

    def upload(self, data: bytes = IMAGE, who: str = "alice", **params: str) -> httpx.Response:
        return self.post("/sources", who, content=data, headers=PNG, params=params)

    def warehouse(self, who: str = "alice") -> Warehouse:
        return Warehouse(self.app.state.conn.cursor(), self.scopes[who])

    def archived_files(self) -> list[Path]:
        return sorted(p for p in self.archive.rglob("*") if p.is_file())


@pytest.fixture
def api(clean: Path) -> Iterator[Api]:
    bet_settings = Settings(data_dir=clean)
    passwords = {name: secrets.token_urlsafe(18) for name in ("alice", "bob")}
    with connect(bet_settings) as conn:
        migrator.apply(conn)
        scopes = {
            "alice": identity.bootstrap(conn, display_name="alice"),
            "bob": create_user(conn, "bob"),
        }
        for name, password in passwords.items():
            set_password_for(conn, name, password)

    settings = ApiSettings(
        bet=bet_settings, auth=token_settings({SIGNING_KEY_ENV: secrets.token_urlsafe(48)})
    )
    assert bet_settings.source_archive_dir is not None
    app = create_app(settings)
    with TestClient(app) as client:
        api = Api(app, client, bet_settings.source_archive_dir, scopes)
        for name, password in passwords.items():
            response = client.post("/auth/token", json={"username": name, "password": password})
            assert response.status_code == 200
            api.tokens[name] = {"Authorization": f"Bearer {response.json()['access_token']}"}
        yield api


def _error(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error: dict[str, Any] = response.json()["error"]
    return error


BET = {"sportsbook": "fanduel", "legs": [{"sport": "NFL", "odds_american": -110}]}


def _add(api: Api, who: str = "alice", **fields: Any) -> dict[str, Any]:
    response = api.post("/bets", who, json=BET | {"cash_staked": "5.00"} | fields)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------- tokens


WRITES = [
    ("/bets", {"json": BET | {"cash_staked": "5.00"}}),
    (f"/bets/{uuid.uuid4()}/settle", {"json": {"result": "won"}}),
    ("/balances", {"json": {"sportsbook": "fanduel", "cash_balance": "5.00"}}),
    ("/cash-flows", {"json": {"kind": "deposit", "sportsbook": "fanduel", "amount": "5.00"}}),
    ("/sources", {"content": IMAGE, "headers": PNG}),
]


@pytest.mark.parametrize(("path", "request_kwargs"), WRITES, ids=[w[0] for w in WRITES])
def test_writes_without_a_token_are_401_and_write_nothing(
    api: Api, path: str, request_kwargs: dict[str, Any]
) -> None:
    response = api.client.post(path, **request_kwargs)
    assert _error(response, 401)["type"] == "AuthenticationError"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    w = api.warehouse()
    assert (w.bets.count(), w.balances.count(), w.cash_flows.count(), w.sources.count()) == (
        0,
        0,
        0,
        0,
    )
    assert api.archived_files() == []


# --------------------------------------------------------------- uploads


def test_upload_archives_the_image_and_returns_its_id(api: Api) -> None:
    response = api.upload(filename="IMG_4417.PNG")
    assert response.status_code == 201, response.text
    body = response.json()
    row = api.warehouse().sources.get(UUID(body["source_file_id"]))
    assert body == {
        "source_file_id": str(row.id),
        "sha256": row.sha256,
        "byte_size": len(IMAGE),
        "media_type": "image/png",
        "original_name": "IMG_4417.PNG",
        "capture_method": "screenshot",
        "already_archived": False,
    }
    (archived,) = api.archived_files()
    assert archived == api.archive / row.archived_path
    assert archived.read_bytes() == IMAGE
    assert archived.name == f"{row.sha256}.png"


def test_reuploading_the_same_image_archives_it_once(api: Api) -> None:
    first = api.upload(filename="IMG_4417.PNG")
    again = api.upload(filename="renamed.png")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json() | {"already_archived": True}
    assert api.warehouse().sources.count() == 1
    assert len(api.archived_files()) == 1

    other = api.upload(IMAGE + b"!")
    assert other.status_code == 201
    assert other.json()["source_file_id"] != first.json()["source_file_id"]
    assert len(api.archived_files()) == 2


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("image/jpeg", None, "upload.jpg"),
        ("IMAGE/JPEG; charset=binary", "slip.JPEG", "slip.JPEG"),
        ("image/heic", "../../Photos/IMG_0001.HEIC", "IMG_0001.HEIC"),
        ("image/webp", "a.webp", "a.webp"),
        ("image/gif", None, "upload.gif"),
    ],
)
def test_upload_names_the_file(
    api: Api, content_type: str, filename: str | None, expected: str
) -> None:
    params = {"filename": filename} if filename is not None else {}
    response = api.post(
        "/sources", content=IMAGE, headers={"Content-Type": content_type}, params=params
    )
    assert response.status_code == 201, response.text
    assert response.json()["original_name"] == expected
    assert response.json()["media_type"] == content_type.partition(";")[0].lower()


@pytest.mark.parametrize("filename", ["slip.txt", "slip", ".png", "slip.jpg", "dir/"])
def test_upload_refuses_a_name_that_disagrees_with_the_type(api: Api, filename: str) -> None:
    error = _error(api.upload(filename=filename), 400)
    assert error["type"] == "UsageError"
    assert error["message"] == f"{filename!r} does not name a image/png file."
    assert error["remediation"] == "Send the file's own name, ending in .png, or omit filename."
    assert api.archived_files() == []


@pytest.mark.parametrize(
    ("content_type", "message"),
    [
        ("text/plain", "'text/plain' is not an image type BET archives."),
        ("application/pdf", "'application/pdf' is not an image type BET archives."),
        ("application/json", "'application/json' is not an image type BET archives."),
        ("", "the upload has no Content-Type."),
    ],
)
def test_upload_refuses_what_is_not_an_image(api: Api, content_type: str, message: str) -> None:
    response = api.post("/sources", content=IMAGE, headers={"Content-Type": content_type})
    error = _error(response, 415)
    assert error == {
        "type": "UnsupportedMediaTypeError",
        "message": message,
        "remediation": "Send the image as the request body with Content-Type "
        "image/gif, image/heic, image/jpeg, image/png, image/webp.",
    }
    assert api.archived_files() == []


def test_upload_refuses_an_empty_body(api: Api) -> None:
    error = _error(api.upload(b""), 400)
    assert (error["message"], error["remediation"]) == (
        "the upload is empty.",
        "Send the image's bytes as the body.",
    )


def test_upload_refuses_more_than_the_cap(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_api, "MAX_UPLOAD_BYTES", len(IMAGE))
    assert api.upload().status_code == 201  # exactly the cap

    declared = _error(api.upload(IMAGE + b"!"), 413)
    assert declared["type"] == "PayloadTooLargeError"

    # No Content-Length: the body is counted as it streams in.
    chunked = api.post("/sources", content=iter([IMAGE, b"!"]), headers=PNG)
    assert _error(chunked, 413)["type"] == "PayloadTooLargeError"
    assert api.warehouse().sources.count() == 1


def test_the_default_cap_is_twenty_mebibytes() -> None:
    assert sources_api.MAX_UPLOAD_BYTES == 20 * 1024 * 1024
    assert sources_api._too_large().message == "the upload is larger than 20 MiB."


def test_the_upload_body_is_documented(api: Api) -> None:
    schema = api.client.get("/openapi.json").json()["paths"]["/sources"]["post"]
    assert set(schema["requestBody"]["content"]) == set(sources_api.IMAGE_TYPES)
    assert {"200", "201", "400", "401", "413", "415"} <= set(schema["responses"])


# ------------------------------------------------------------- POST /bets


def test_add_records_the_bet_and_answers_with_its_detail(api: Api) -> None:
    body = _add(api)
    rebuilt = BetDetail.model_validate(body)
    expected = bet_detail(api.warehouse(), rebuilt.bet.id)
    assert rebuilt.model_dump(exclude={"provenance"}) == expected.model_dump(exclude={"provenance"})
    assert body["bet"]["cash_staked"] == "5.00"
    assert body["bet"]["capture_method"] == "manual"
    assert body["bet"]["source_file_id"] is None
    assert body["account_label"] == "fanduel"
    assert api.client.get(f"/bets/{rebuilt.bet.id}", headers=api.tokens["alice"]).json() == body


def test_add_records_every_field(api: Api) -> None:
    placed = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    body = _add(
        api,
        account_label="boosts",
        legs=[
            {"sport": "NBA", "selection_name": "Celtics ML", "odds_american": -150},
            {"sport": "NBA", "selection_name": "Tatum 25+", "line_value": "24.5"},
        ],
        leg_group={"odds_american": 300, "category": "SGP"},
        wager_kind="same_game_parlay",
        promotions=[{"promotion_type": "profit_boost", "generosity_pct": "25"}],
        cash_staked="3.00",
        bonus_staked="2.00",
        placed_at=placed.astimezone().isoformat(),
    )
    bet = body["bet"]
    assert (bet["wager_kind"], bet["odds_american_placed"]) == ("same_game_parlay", 300)
    assert (bet["cash_staked"], bet["bonus_staked"]) == ("3.00", "2.00")
    assert datetime.fromisoformat(bet["placed_at"]) == placed
    assert body["account_label"] == "boosts"
    assert [leg["selection_name"] for leg in body["legs"]] == ["Celtics ML", "Tatum 25+"]
    assert len({leg["group_id"] for leg in body["legs"]}) == 1
    assert body["promotions"][0]["promotion_type"] == "profit_boost"


def test_a_bet_cites_an_upload_exactly_as_the_cli_archives_one(api: Api, tmp_path: Path) -> None:
    """``bet add --capture-method screenshot --source-file`` hands the path to
    ``add_bet``; the API cites the uploaded id. Same row, same provenance."""
    source_id = api.upload(filename="slip.png").json()["source_file_id"]
    body = _add(api, capture_method="Screenshot ", source_file_id=source_id)
    assert body["bet"]["capture_method"] == "screenshot"
    assert body["bet"]["source_file_id"] == source_id
    assert body["provenance"]["capture_method"] == "screenshot"
    assert body["provenance"]["source_file_id"] == source_id

    on_disk = tmp_path / "slip.png"
    on_disk.write_bytes(IMAGE)
    w = api.warehouse()
    via_cli_path = add_bet(
        w,
        AddBetRequest(
            sportsbook="fanduel",
            legs=[LegInput(sport="NFL", odds_american=-110)],
            cash_staked=Decimal("5.00"),
            capture_method="screenshot",
        ),
        source=SourceToArchive(archive_dir=api.archive, path=on_disk),
    ).bet
    assert str(via_cli_path.source_file_id) == source_id
    assert via_cli_path.capture_method == body["bet"]["capture_method"]
    assert w.bets.provenance(via_cli_path.id) == {
        **w.bets.provenance(UUID(body["bet"]["id"])),
        "bet_id": via_cli_path.id,
    }
    assert w.sources.count() == 1


def test_a_bet_citing_an_unknown_source_is_404(api: Api) -> None:
    unknown = uuid.uuid4()
    response = api.post(
        "/bets",
        json=BET
        | {"cash_staked": "5.00", "capture_method": "screenshot", "source_file_id": str(unknown)},
    )
    error = _error(response, 404)
    assert error["type"] == "NotFoundError"
    assert str(unknown) in error["message"]
    assert api.warehouse().bets.count() == 0


def test_a_bet_citing_another_users_upload_is_404(api: Api) -> None:
    bobs = api.upload(who="bob").json()["source_file_id"]
    response = api.post(
        "/bets",
        json=BET | {"cash_staked": "5.00", "capture_method": "screenshot", "source_file_id": bobs},
    )
    assert _error(response, 404)["type"] == "NotFoundError"
    assert api.warehouse().bets.count() == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"legs": []},
        {"colour": "red"},
        {"legs": [{"sport": "NFL", "odds": -110}]},
        {"placed_at": "2026-10-01T12:00:00"},  # no offset: whose noon?
        {"cash_staked": "five"},
        {"source_file_id": "not-a-uuid"},
    ],
)
def test_add_malformed_bodies_are_422(api: Api, fields: dict[str, Any]) -> None:
    response = api.post("/bets", json=BET | {"cash_staked": "5.00"} | fields)
    assert _error(response, 422)["details"]


# ------------------------------------------------------ POST /bets/{id}/settle


def test_settle_computes_the_return_and_settles_the_single_leg(api: Api) -> None:
    bet_id = _add(api, legs=[{"sport": "NFL", "odds_american": 150}], cash_staked="10.00")["bet"][
        "id"
    ]
    settled_at = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)
    response = api.post(
        f"/bets/{bet_id}/settle", json={"result": "won", "settled_at": settled_at.isoformat()}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    expected = bet_detail(api.warehouse(), UUID(bet_id))
    assert BetDetail.model_validate(body).model_dump(exclude={"provenance"}) == (
        expected.model_dump(exclude={"provenance"})
    )
    assert (body["bet"]["status"], body["bet"]["result"]) == ("settled", "won")
    assert body["bet"]["cash_returned"] == "25.00"
    assert datetime.fromisoformat(body["bet"]["settled_at"]) == settled_at
    assert body["legs"][0]["result"] == "won"


def test_settle_a_lost_leg_forces_the_ticket_lost(api: Api) -> None:
    legs = [{"sport": "NBA", "odds_american": -110}, {"sport": "NBA", "odds_american": 120}]
    bet_id = _add(api, legs=legs)["bet"]["id"]
    body = api.post(f"/bets/{bet_id}/settle", json={"leg_results": {"2": "lost"}}).json()
    assert (body["bet"]["result"], body["bet"]["cash_returned"]) == ("lost", "0.00")
    assert [leg["result"] for leg in body["legs"]] == [None, "lost"]


def test_settle_with_force_resettles(api: Api) -> None:
    bet_id = _add(api)["bet"]["id"]
    api.post(f"/bets/{bet_id}/settle", json={"result": "lost"})
    body = api.post(
        f"/bets/{bet_id}/settle", json={"result": "won", "cash_returned": "9.55", "force": True}
    ).json()
    assert (body["bet"]["result"], body["bet"]["cash_returned"]) == ("won", "9.55")


def test_settling_another_users_bet_is_404_and_changes_nothing(api: Api) -> None:
    bobs = _add(api, who="bob")["bet"]["id"]
    error = _error(api.post(f"/bets/{bobs}/settle", json={"result": "won"}), 404)
    assert error["type"] == "NotFoundError"
    assert api.warehouse("bob").bets.get(UUID(bobs)).status == "pending"


@pytest.mark.parametrize(
    "body", [{"result": "won", "extra": 1}, {"leg_results": {"one": "won"}}, {"force": "maybe"}]
)
def test_settle_malformed_bodies_are_422(api: Api, body: dict[str, Any]) -> None:
    bet_id = _add(api)["bet"]["id"]
    assert _error(api.post(f"/bets/{bet_id}/settle", json=body), 422)["details"]


# ------------------------------------------------ POST /balances, /cash-flows


def test_balance_snapshot_is_recorded_and_reconciled(api: Api) -> None:
    observed = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
    first = api.post(
        "/balances",
        json={
            "sportsbook": "FanDuel",
            "cash_balance": "120.50",
            "bonus_balance": "5.00",
            "observed_at": observed.isoformat(),
            "notes": "after the late game",
        },
    )
    assert first.status_code == 201, first.text
    recorded = RecordedSnapshot.model_validate(first.json())
    assert recorded.account_is_new and recorded.account.label == "fanduel"
    assert recorded.snapshot.cash_balance == Decimal("120.50")
    assert recorded.snapshot.observed_at == observed
    assert first.json()["snapshot"]["cash_balance"] == "120.50"
    assert api.warehouse().balances.get(recorded.snapshot.id) == recorded.snapshot

    second = api.post("/balances", json={"sportsbook": "fanduel", "cash_balance": "99.00"})
    assert second.json()["account_is_new"] is False
    rows = api.client.get("/balances", headers=api.tokens["alice"]).json()
    assert rows[0]["balance"] == "99.00"


@pytest.mark.parametrize("kind", ["deposit", "withdrawal"])
def test_cash_flow_is_recorded(api: Api, kind: str) -> None:
    source_id = api.upload().json()["source_file_id"]
    response = api.post(
        "/cash-flows",
        json={
            "kind": kind,
            "sportsbook": "fanduel",
            "amount": "40.00",
            "capture_method": "screenshot",
            "source_file_id": source_id,
        },
    )
    assert response.status_code == 201, response.text
    recorded = RecordedCashFlow.model_validate(response.json())
    assert (recorded.cash_flow.kind, recorded.cash_flow.amount) == (kind, Decimal("40.00"))
    assert str(recorded.cash_flow.source_file_id) == source_id
    assert recorded.cash_flow.capture_method == "screenshot"
    assert api.warehouse().cash_flows.get(recorded.cash_flow.id) == recorded.cash_flow


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/cash-flows", {"kind": "bonus", "sportsbook": "fanduel", "amount": "1.00"}),
        ("/cash-flows", {"sportsbook": "fanduel", "amount": "1.00"}),
        ("/balances", {"sportsbook": "fanduel"}),
        ("/balances", {"sportsbook": "fanduel", "cash_balance": "1", "observed_at": "today"}),
    ],
)
def test_ledger_malformed_bodies_are_422(api: Api, path: str, body: dict[str, Any]) -> None:
    assert _error(api.post(path, json=body), 422)["details"]


def test_ledger_refuses_a_moment_in_the_future(api: Api) -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    error = _error(
        api.post(
            "/balances",
            json={"sportsbook": "fanduel", "cash_balance": "1.00", "observed_at": future},
        ),
        400,
    )
    assert error["message"] == f"observed_at {future} is in the future."
    assert error["remediation"] == (
        "Pass the moment it actually happened, or omit observed_at for now."
    )


# ------------------------------------------- refusals carry the CLI's text


@dataclass(frozen=True)
class Refusal:
    """One invalid input, as ``bet`` arguments and as an API request.

    ``{source}`` in ``cli`` is a file on disk; ``{source_id}`` in ``body`` is
    the same bytes uploaded. ``{bet}`` is a pending bet on each side.
    ``field_named`` is set where the message quotes the input's own name --
    ``stake`` to the CLI, ``cash_staked`` to the API -- and is that name.
    """

    cli: list[str]
    path: str
    body: dict[str, Any]
    settle_first: bool = False
    field_named: str | None = None


ADD_CLI = ["add", "--non-interactive", "--sportsbook", "fanduel", "--stake", "5"]
NFL_LEG = ["--leg", "sport=NFL,odds=-110"]
ADD_BODY: dict[str, Any] = {"sportsbook": "fanduel", "cash_staked": "5"}
NFL = {"sport": "NFL", "odds_american": -110}
SETTLE = ["settle", "{bet}"]
BALANCE_CLI = ["balance", "add", "--sportsbook", "fanduel", "--amount", "5"]
BALANCE_BODY = {"sportsbook": "fanduel", "cash_balance": "5"}
CITED = {"source_file_id": "{source_id}"}
SCREENSHOT_CITED = CITED | {"capture_method": "screenshot"}
UNKNOWN_BET = str(uuid.uuid4())

REFUSALS: dict[str, Refusal] = {
    "add: unknown capture method": Refusal(
        [*ADD_CLI, *NFL_LEG, "--capture-method", "telepathy"],
        "/bets",
        ADD_BODY | {"legs": [NFL], "capture_method": "telepathy"},
    ),
    "add: a source contradicts manual": Refusal(
        [*ADD_CLI, *NFL_LEG, "--source-file", "{source}"],
        "/bets",
        ADD_BODY | {"legs": [NFL]} | CITED,
    ),
    "add: unknown sportsbook": Refusal(
        ["add", "--non-interactive", "--sportsbook", "nope", "--stake", "5", *NFL_LEG],
        "/bets",
        ADD_BODY | {"sportsbook": "nope", "legs": [NFL]},
    ),
    "add: straight with two legs": Refusal(
        [*ADD_CLI, *NFL_LEG, *NFL_LEG, "--wager-kind", "straight"],
        "/bets",
        ADD_BODY | {"legs": [NFL, NFL], "wager_kind": "straight"},
    ),
    "add: parlay with one leg": Refusal(
        [*ADD_CLI, *NFL_LEG, "--wager-kind", "parlay"],
        "/bets",
        ADD_BODY | {"legs": [NFL], "wager_kind": "parlay"},
    ),
    "add: unknown wager kind": Refusal(
        [*ADD_CLI, *NFL_LEG, "--wager-kind", "accumulator"],
        "/bets",
        ADD_BODY | {"legs": [NFL], "wager_kind": "accumulator"},
    ),
    "add: odds in the dead zone": Refusal(
        [*ADD_CLI, *NFL_LEG, "--odds", "50"],
        "/bets",
        ADD_BODY | {"legs": [NFL], "odds_american": 50},
    ),
    "add: a leg priced in the dead zone": Refusal(
        [*ADD_CLI, "--leg", "sport=NFL,odds=-99"],
        "/bets",
        ADD_BODY | {"legs": [{"sport": "NFL", "odds_american": -99}]},
    ),
    "add: an unpriced ticket": Refusal(
        [*ADD_CLI, "--leg", "sport=NFL"],
        "/bets",
        ADD_BODY | {"legs": [{"sport": "NFL"}]},
    ),
    "add: unknown promotion type": Refusal(
        [*ADD_CLI, *NFL_LEG, "--promo", "type=nope"],
        "/bets",
        ADD_BODY | {"legs": [NFL], "promotions": [{"promotion_type": "nope"}]},
    ),
    "add: a stake too large to store": Refusal(
        ["add", "--non-interactive", "--sportsbook", "fanduel", *NFL_LEG, "--stake", "1e10"],
        "/bets",
        ADD_BODY | {"legs": [NFL], "cash_staked": "1e10"},
        field_named="cash_staked",
    ),
    "settle: unknown result": Refusal(
        [*SETTLE, "--result", "maybe"], "/bets/{bet}/settle", {"result": "maybe"}
    ),
    "settle: no result": Refusal(SETTLE, "/bets/{bet}/settle", {}),
    "settle: a return on a loss": Refusal(
        [*SETTLE, "--result", "lost", "--return", "1"],
        "/bets/{bet}/settle",
        {"result": "lost", "cash_returned": "1"},
    ),
    "settle: a return on a push": Refusal(
        [*SETTLE, "--result", "push", "--return", "1"],
        "/bets/{bet}/settle",
        {"result": "push", "cash_returned": "1"},
    ),
    "settle: a negative return": Refusal(
        [*SETTLE, "--result", "won", "--return", "-1"],
        "/bets/{bet}/settle",
        {"result": "won", "cash_returned": "-1"},
    ),
    "settle: a cash-out with no return": Refusal(
        [*SETTLE, "--result", "cashed_out"], "/bets/{bet}/settle", {"result": "cashed_out"}
    ),
    "settle: unknown leg order": Refusal(
        [*SETTLE, "--result", "won", "--leg-result", "9=won"],
        "/bets/{bet}/settle",
        {"result": "won", "leg_results": {"9": "won"}},
    ),
    "settle: unknown leg result": Refusal(
        [*SETTLE, "--result", "won", "--leg-result", "1=meh"],
        "/bets/{bet}/settle",
        {"result": "won", "leg_results": {"1": "meh"}},
    ),
    "settle: won with a lost leg": Refusal(
        [*SETTLE, "--result", "won", "--leg-result", "1=lost"],
        "/bets/{bet}/settle",
        {"result": "won", "leg_results": {"1": "lost"}},
    ),
    "settle: already settled": Refusal(
        [*SETTLE, "--result", "won"],
        "/bets/{bet}/settle",
        {"result": "won"},
        settle_first=True,
    ),
    "settle: unknown bet": Refusal(
        ["settle", UNKNOWN_BET, "--result", "won"], f"/bets/{UNKNOWN_BET}/settle", {"result": "won"}
    ),
    "balance: unknown capture method": Refusal(
        [*BALANCE_CLI, "--capture-method", "telepathy"],
        "/balances",
        BALANCE_BODY | {"capture_method": "telepathy"},
    ),
    "balance: a source contradicts manual": Refusal(
        [*BALANCE_CLI, "--source-file", "{source}"], "/balances", BALANCE_BODY | CITED
    ),
    "balance: unknown account label": Refusal(
        [*BALANCE_CLI, "--account-label", "nope"],
        "/balances",
        BALANCE_BODY | {"account_label": "nope"},
    ),
    "balance: unknown sportsbook": Refusal(
        ["balance", "add", "--sportsbook", "nope", "--amount", "5"],
        "/balances",
        BALANCE_BODY | {"sportsbook": "nope"},
    ),
    "deposit: nothing moved": Refusal(
        ["deposit", "--sportsbook", "fanduel", "--amount", "0"],
        "/cash-flows",
        {"kind": "deposit", "sportsbook": "fanduel", "amount": "0"},
    ),
    "withdraw: nothing moved": Refusal(
        ["withdraw", "--sportsbook", "fanduel", "--amount", "0.00"],
        "/cash-flows",
        {"kind": "withdrawal", "sportsbook": "fanduel", "amount": "0.00"},
    ),
    "withdraw: a source contradicts manual": Refusal(
        ["withdraw", "--sportsbook", "fanduel", "--amount", "5", "--source-file", "{source}"],
        "/cash-flows",
        {"kind": "withdrawal", "sportsbook": "fanduel", "amount": "5"} | CITED,
    ),
    "deposit: unknown capture method": Refusal(
        ["deposit", "--sportsbook", "fanduel", "--amount", "5", "--capture-method", "fax"],
        "/cash-flows",
        {"kind": "deposit", "sportsbook": "fanduel", "amount": "5", "capture_method": "fax"},
    ),
    "deposit: unknown sportsbook": Refusal(
        ["deposit", "--sportsbook", "nope", "--amount", "5"],
        "/cash-flows",
        {"kind": "deposit", "sportsbook": "nope", "amount": "5"},
    ),
}


@dataclass
class Cli:
    """The real ``bet`` command, on a temp warehouse of its own."""

    source: Path
    bet_id: str

    @staticmethod
    def invoke(args: list[str]) -> Any:
        return runner.invoke(cli_app, args, catch_exceptions=True)

    def refusal(self, args: list[str]) -> BetError:
        filled = [
            a.replace("{source}", str(self.source)).replace("{bet}", self.bet_id) for a in args
        ]
        result = self.invoke(filled)
        assert isinstance(result.exception, BetError), result.output
        return result.exception


@pytest.fixture
def cli(api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cli:
    """Initialised after the server has its warehouse open, so neither sees the other's."""
    monkeypatch.setenv("BET_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cli-config"))
    assert Cli.invoke(["init"]).exit_code == 0
    added = Cli.invoke(["--format", "json", *ADD_CLI, *NFL_LEG])
    assert added.exit_code == 0, added.output
    bet_id = added.stdout[added.stdout.index('"id": "') + 7 :][:36]
    source = tmp_path / "slip.png"
    source.write_bytes(IMAGE)
    return Cli(source=source, bet_id=bet_id)


@pytest.mark.parametrize("case", list(REFUSALS.values()), ids=list(REFUSALS))
def test_refusals_carry_the_clis_text(api: Api, cli: Cli, case: Refusal) -> None:
    # The same state on both sides: one pending fanduel bet, one archived image.
    bet_id = _add(api, legs=[NFL])["bet"]["id"]
    source_id = api.upload().json()["source_file_id"]
    if case.settle_first:
        assert cli.invoke(["settle", cli.bet_id, "--result", "lost"]).exit_code == 0
        assert api.post(f"/bets/{bet_id}/settle", json={"result": "lost"}).status_code == 200

    expected = cli.refusal(case.cli)
    body = {k: (source_id if v == "{source_id}" else v) for k, v in case.body.items()}
    response = api.post(case.path.replace("{bet}", bet_id), json=body)

    error = _error(response, status_for(expected))
    assert 400 <= response.status_code < 500
    assert error["remediation"] == expected.remediation
    if case.field_named is not None:
        assert error["message"].endswith(f" for {case.field_named}.")
        return
    assert error["message"].replace(bet_id, "<bet>") == expected.message.replace(
        cli.bet_id, "<bet>"
    )


def test_screenshot_provenance_with_a_source_is_accepted_by_both(api: Api, cli: Cli) -> None:
    """The positive twin of the manual refusals: the CLI and the API agree it is fine."""
    result = cli.invoke(
        [*ADD_CLI, *NFL_LEG, "--capture-method", "screenshot", "--source-file", str(cli.source)]
    )
    assert result.exit_code == 0, result.output
    source_id = api.upload().json()["source_file_id"]
    body = _add(api, legs=[NFL], **SCREENSHOT_CITED | {"source_file_id": source_id})
    assert body["bet"]["capture_method"] == "screenshot"
