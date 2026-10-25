"""Where a command's data comes from: this machine's warehouse, or the BET API.

ADR 0002 section 3. Config ``api_url`` decides, once per invocation:

* unset -- **local**: ``LocalBackend`` opens the warehouse and calls the
  services, exactly as every command did before remote mode existed;
* set -- **remote**: ``RemoteBackend`` calls the API, and the local warehouse
  is never opened.

Commands build the same service requests (``ListQuery``, ``AddBetRequest``,
``SettleRequest``...) in both modes and get back the same models
(``BetListing``, ``BetDetail``, ``Card``...) -- the API serialises exactly those
-- so every renderer is shared and the output is the same. A server error comes
back as the ``BetError`` the server raised (``bet.client.api``), so exit codes
and error text are too.

Commands that act on the warehouse file itself (``bet init``, ``bet user``)
refuse in remote mode through ``refuse_remote``: run them where the warehouse
lives.

Timestamps read back from a warehouse carry the offset of the process that
read them -- for a local CLI, this machine's zone. The API's process may run in
another zone (a pod in UTC), so remote timestamps read from the warehouse are
shown in this machine's zone too (``_local_times``): the same instants, written
as local mode writes them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from bet.analytics.card import Card, card_from_dict
from bet.client import api as api_module
from bet.client.api import ApiClient
from bet.client.tokens import TokenStore
from bet.config import ResolvedConfig, Thresholds
from bet.database import identity, migrator
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import Bet, BetLeg
from bet.models.ownership import OwnerScope
from bet.services.add import AddBetRequest, add_bet
from bet.services.balance import BalanceQuery, balance_report, require_known_sportsbook
from bet.services.bets import BetDetail, BetListing, ListQuery, bet_detail, list_bets
from bet.services.card import CardQuery, card_summary
from bet.services.ledger import (
    BalanceSnapshotRequest,
    CashFlowRequest,
    RecordedCashFlow,
    RecordedSnapshot,
    add_balance_snapshot,
    record_cash_flow,
)
from bet.services.settle import SettleRequest, settle_bet
from bet.services.similar import SimilarQuery, similar_bets
from bet.services.sources import SourceToArchive
from bet.services.specs import (
    leg_input,
    parse_leg_spec,
    parse_odds,
    parse_promo_spec,
    promotion_input,
)
from bet.services.tickets import (
    LegInput,
    PromotionInput,
    build_legs,
    build_promotions,
    ticket_odds,
    wager_kind_for,
)
from bet.sources import MEDIA_TYPES

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

LISTINGS: TypeAdapter[list[BetListing]] = TypeAdapter(list[BetListing])

LOCAL_ONLY_REMEDIATION = (
    "api_url is set, so this CLI uses the BET API at {url}. Run the command where\n"
    "the warehouse lives -- on the server, e.g. `kubectl exec deploy/bet-api -- bet ...` --\n"
    "or remove api_url from {config} (and unset BET_API_URL) to use a local warehouse."
)

# --------------------------------------------------------------- choosing


def remote_url(resolved: ResolvedConfig) -> str | None:
    """The API this invocation uses, or ``None`` for local mode."""
    return resolved.settings.api_url


def refuse_remote(resolved: ResolvedConfig, command: str) -> None:
    """Refuse a command that acts on a local warehouse file, in remote mode."""
    url = remote_url(resolved)
    if url is not None:
        raise UsageError(
            f"`{command}` acts on a warehouse on this machine, and this CLI is in remote mode.",
            remediation=LOCAL_ONLY_REMEDIATION.format(url=url, config=resolved.config_path),
        )


def require_remote(resolved: ResolvedConfig, command: str) -> str:
    """The ``api_url`` a remote-only command (``bet login``) needs, or ``UsageError``."""
    url = remote_url(resolved)
    if url is None:
        raise UsageError(
            f"`{command}` needs a BET API, and api_url is not set.",
            remediation=(
                "Set it with `bet config set api_url https://<server>` (or BET_API_URL).\n"
                "Local mode needs no login."
            ),
        )
    return url


@contextmanager
def api_session(api_url: str) -> Iterator[ApiClient]:
    """An API client for ``api_url``, with its Keychain tokens."""
    with api_module.http_client(api_url) as http:
        yield ApiClient(api_url, http, TokenStore(api_url))


# ---------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class OpenBet:
    """A pending bet as interactive ``bet settle`` walks it."""

    bet: Bet
    account_label: str | None
    legs: list[BetLeg]
    # Asked only once the user chooses to settle this bet.
    has_promotions: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class Proposal:
    """``bet similar``'s proposed ticket, as the text its flags carry."""

    leg_specs: Sequence[str]
    odds: str | None
    wager_kind: str | None
    promo_specs: Sequence[str]


@dataclass(frozen=True, slots=True)
class _JudgedProposal:
    leg_inputs: list[LegInput]
    legs: list[BetLeg]
    wager_kind: str
    explicit_odds: int | None
    odds_american: int | None
    promo_inputs: list[PromotionInput]
    promo_types: Sequence[str]


def _judge(proposal: Proposal, scope: OwnerScope, bet_id: UUID) -> _JudgedProposal:
    """Parse and judge a proposal by ``bet.services.tickets``, in ``bet similar``'s order."""
    ids = {"tenant_id": scope.tenant_id, "user_id": scope.user_id, "bet_id": bet_id}
    leg_inputs = [leg_input(parse_leg_spec(spec)) for spec in proposal.leg_specs]
    legs = build_legs(leg_inputs, **ids)
    kind = wager_kind_for(proposal.wager_kind, leg_count=len(legs))
    # An unpriced SGP with no --odds is still worth comparing on every
    # other dimension; it simply has no price band.
    explicit = parse_odds(proposal.odds) if proposal.odds is not None else None
    try:
        odds_american: int | None = ticket_odds(legs, explicit)[0]
    except UsageError:
        odds_american = None
    promo_inputs = [promotion_input(parse_promo_spec(spec)) for spec in proposal.promo_specs]
    promo_types = [p.promotion_type for p in build_promotions(promo_inputs, legs=legs, **ids)]
    return _JudgedProposal(
        leg_inputs, legs, kind, explicit, odds_american, promo_inputs, promo_types
    )


@dataclass(frozen=True, slots=True)
class HistoryFilters:
    """The global options that narrow the history a proposal is compared to."""

    sportsbook: str | None = None
    sport: str | None = None
    since: date | None = None
    until: date | None = None
    include_void: bool = False


class Backend(Protocol):
    """What a command can ask for, whichever mode it is in."""

    mode: str

    def warehouse(self, purpose: str) -> Warehouse: ...
    def list_bets(self, query: ListQuery) -> list[BetListing]: ...
    def bet_detail(self, bet_id: UUID) -> BetDetail: ...
    def user_timezone(self) -> str | None: ...
    def card(self, query: CardQuery) -> Card: ...
    def similar(
        self, proposal: Proposal, filters: HistoryFilters, thresholds: Thresholds
    ) -> list[dict[str, Any]]: ...
    def balances(self, query: BalanceQuery) -> list[dict[str, Any]]: ...
    def add_bet(
        self, request: AddBetRequest, source_file: Path | None
    ) -> tuple[Bet, list[BetLeg]]: ...
    def open_bets(self) -> Iterator[OpenBet]: ...
    def settle(self, request: SettleRequest) -> Bet: ...
    def add_balance(
        self, request: BalanceSnapshotRequest, source_file: Path | None
    ) -> RecordedSnapshot: ...
    def record_cash_flow(
        self, request: CashFlowRequest, source_file: Path | None
    ) -> RecordedCashFlow: ...


@contextmanager
def open_backend(
    resolved: ResolvedConfig, *, migrated: bool = False, defer_scope: bool = False
) -> Iterator[Backend]:
    """The backend for this invocation, open for the duration of the block.

    Local mode opens the warehouse, refuses a pending migration when
    ``migrated``, and resolves the local identity -- unless ``defer_scope``, for
    a command that checks its input against the warehouse first. Remote mode
    opens nothing local.
    """
    url = remote_url(resolved)
    if url is not None:
        with api_session(url) as client:
            yield RemoteBackend(client)
        return

    with connect(resolved.settings) as conn:
        if migrated:
            migrator.require_current(conn)
        local = LocalBackend(conn, resolved)
        if not defer_scope:
            local.scope()
        yield local


# ------------------------------------------------------------------ local


class LocalBackend:
    """The warehouse on this machine, through the services."""

    mode = "local"

    def __init__(self, conn: DuckDBPyConnection, resolved: ResolvedConfig) -> None:
        self.conn = conn
        self.resolved = resolved
        self._w: Warehouse | None = None

    def scope(self) -> OwnerScope:
        return self.warehouse("").scope

    def warehouse(self, purpose: str) -> Warehouse:
        if self._w is None:
            self._w = Warehouse(self.conn, identity.resolve_scope(self.conn, self.resolved))
        return self._w

    def _source(self, source_file: Path | None) -> SourceToArchive | None:
        archive_dir = self.resolved.settings.source_archive_dir
        assert archive_dir is not None  # Settings derives it from data_dir
        return (
            SourceToArchive(archive_dir=archive_dir, path=source_file)
            if source_file is not None
            else None
        )

    def list_bets(self, query: ListQuery) -> list[BetListing]:
        return list_bets(self.warehouse(""), query)

    def bet_detail(self, bet_id: UUID) -> BetDetail:
        return bet_detail(self.warehouse(""), bet_id)

    def user_timezone(self) -> str | None:
        return identity.user_timezone(self.conn, self.scope())

    def card(self, query: CardQuery) -> Card:
        return card_summary(self.warehouse(""), query)

    def similar(
        self, proposal: Proposal, filters: HistoryFilters, thresholds: Thresholds
    ) -> list[dict[str, Any]]:
        judged = _judge(proposal, self.scope(), uuid4())
        return similar_bets(
            self.warehouse(""),
            SimilarQuery(
                legs=judged.legs,
                wager_kind=judged.wager_kind,
                odds_american=judged.odds_american,
                promo_types=judged.promo_types,
                sportsbook=filters.sportsbook,
                sport=filters.sport,
                since=filters.since,
                until=filters.until,
                include_void=filters.include_void,
                min_settled=thresholds.min_settled_bets,
                exploratory_below=thresholds.exploratory_below,
            ),
        )

    def balances(self, query: BalanceQuery) -> list[dict[str, Any]]:
        require_known_sportsbook(self.conn, query.sportsbook)
        return balance_report(self.warehouse(""), query)

    def add_bet(self, request: AddBetRequest, source_file: Path | None) -> tuple[Bet, list[BetLeg]]:
        added = add_bet(self.warehouse(""), request, source=self._source(source_file))
        return added.bet, added.legs

    def open_bets(self) -> Iterator[OpenBet]:
        w = self.warehouse("")
        for bet in w.bets.open_bets():
            account = w.accounts.find(bet.sportsbook_account_id)

            def has_promotions(bet_id: UUID = bet.id) -> bool:
                return bool(w.bet_promotions.for_bet(bet_id))

            yield OpenBet(
                bet=bet,
                account_label=account.label if account else None,
                legs=w.legs.for_bet(bet.id),
                has_promotions=has_promotions,
            )

    def settle(self, request: SettleRequest) -> Bet:
        return settle_bet(self.warehouse(""), request).bet

    def add_balance(
        self, request: BalanceSnapshotRequest, source_file: Path | None
    ) -> RecordedSnapshot:
        return add_balance_snapshot(self.warehouse(""), request, source=self._source(source_file))

    def record_cash_flow(
        self, request: CashFlowRequest, source_file: Path | None
    ) -> RecordedCashFlow:
        return record_cash_flow(self.warehouse(""), request, source=self._source(source_file))


# ----------------------------------------------------------------- remote


def _local_times(value: Any) -> Any:
    """``value`` with every ``*_at`` timestamp moved to this machine's zone.

    For JSON read back from the warehouse by the server, so its timestamps
    carry the offset a local read would have given them.
    """
    if isinstance(value, list):
        return [_local_times(v) for v in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, child in value.items():
        if key.endswith("_at") and isinstance(child, str):
            try:
                moment = datetime.fromisoformat(child)
            except ValueError:
                moment = None
            if moment is not None and moment.tzinfo is not None:
                child = moment.astimezone().isoformat()
        out[key] = _local_times(child)
    return out


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


# Nil ids: a remote proposal is judged here only to refuse bad input before
# it is sent, with the text and in the order local mode refuses it.
_NIL_SCOPE = OwnerScope(tenant_id=UUID(int=0), user_id=UUID(int=0))


class RemoteBackend:
    """The BET API, through ``bet.client``."""

    mode = "remote"

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def warehouse(self, purpose: str) -> Warehouse:
        raise UsageError(
            f"{purpose} is not available in remote mode.",
            remediation="Pass every field as a flag with --non-interactive.",
        )

    def list_bets(self, query: ListQuery) -> list[BetListing]:
        body = self.client.get(
            "/bets",
            open=query.is_open,
            status=query.status,
            kind=query.kind,
            sportsbook=query.sportsbook,
            sport=query.sport,
            text=query.text,
            since=_iso(query.since),
            until=_iso(query.until),
            limit=query.limit,
        )
        return LISTINGS.validate_python(_local_times(body))

    def bet_detail(self, bet_id: UUID) -> BetDetail:
        return BetDetail.model_validate(_local_times(self.client.get(f"/bets/{bet_id}")))

    def user_timezone(self) -> str | None:
        timezone = self.client.get("/me").get("timezone")
        return str(timezone) if timezone else None

    def card(self, query: CardQuery) -> Card:
        zone = getattr(query.tz, "key", None)
        if zone is None:
            raise UsageError(
                f"this machine's time zone ({query.tz}) has no IANA name to send the BET API.",
                remediation="Set TZ to one, e.g. TZ=America/New_York, or set your user's zone.",
            )
        # Always the full card: --share hides the money here, as render_card does.
        body = self.client.get(
            "/card",
            days=query.days,
            sportsbook=query.sportsbook,
            sport=query.sport,
            include_void=query.include_void,
            tz=zone,
        )
        return card_from_dict(_local_times(body))

    def similar(
        self, proposal: Proposal, filters: HistoryFilters, thresholds: Thresholds
    ) -> list[dict[str, Any]]:
        judged = _judge(proposal, _NIL_SCOPE, UUID(int=0))
        rows: list[dict[str, Any]] = self.client.post(
            "/similar",
            {
                "legs": [leg.model_dump(mode="json") for leg in judged.leg_inputs],
                "wager_kind": judged.wager_kind,
                "odds_american": judged.explicit_odds,
                "promotions": [p.model_dump(mode="json") for p in judged.promo_inputs],
                "sportsbook": filters.sportsbook,
                "sport": filters.sport,
                "since": _iso(filters.since),
                "until": _iso(filters.until),
                "include_void": filters.include_void,
            },
        )
        return rows

    def balances(self, query: BalanceQuery) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = _local_times(
            self.client.get(
                "/balances",
                sportsbook=query.sportsbook,
                account_label=query.account_label,
                history=query.history,
            )
        )
        for row in rows:
            if isinstance(row.get("observed_at"), str):
                row["observed_at"] = datetime.fromisoformat(row["observed_at"])
        return rows

    def _upload(self, source_file: Path | None) -> UUID | None:
        """Archive ``source_file`` on the server first; the write then cites its id."""
        if source_file is None:
            return None
        media_type = MEDIA_TYPES.get(source_file.suffix.lower(), "application/octet-stream")
        body = self.client.call(
            "POST",
            "/sources",
            params={"filename": source_file.name},
            content=source_file.read_bytes(),
            headers={"Content-Type": media_type},
        )
        return UUID(str(body["source_file_id"]))

    def add_bet(self, request: AddBetRequest, source_file: Path | None) -> tuple[Bet, list[BetLeg]]:
        source_file_id = self._upload(source_file) or request.source_file_id
        cited = request.model_copy(update={"source_file_id": source_file_id})
        detail = BetDetail.model_validate(self.client.post("/bets", cited.model_dump(mode="json")))
        return detail.bet, detail.legs

    def open_bets(self) -> Iterator[OpenBet]:
        for listing in self.list_bets(ListQuery(is_open=True)):

            def has_promotions(bet_id: UUID = listing.bet.id) -> bool:
                return bool(self.bet_detail(bet_id).promotions)

            yield OpenBet(
                bet=listing.bet,
                account_label=listing.account_label,
                legs=listing.legs,
                has_promotions=has_promotions,
            )

    def settle(self, request: SettleRequest) -> Bet:
        body = request.model_dump(mode="json", exclude={"bet_id"})
        bet = BetDetail.model_validate(self.client.post(f"/bets/{request.bet_id}/settle", body)).bet
        # Local mode shows the settled bet as written: settled_at in the zone
        # it was given in, not as the warehouse reads it back.
        if bet.settled_at is not None and request.settled_at is not None:
            bet = bet.model_copy(
                update={"settled_at": bet.settled_at.astimezone(request.settled_at.tzinfo)}
            )
        return bet

    def add_balance(
        self, request: BalanceSnapshotRequest, source_file: Path | None
    ) -> RecordedSnapshot:
        source_file_id = self._upload(source_file) or request.source_file_id
        cited = request.model_copy(update={"source_file_id": source_file_id})
        return RecordedSnapshot.model_validate(
            self.client.post("/balances", cited.model_dump(mode="json"))
        )

    def record_cash_flow(
        self, request: CashFlowRequest, source_file: Path | None
    ) -> RecordedCashFlow:
        source_file_id = self._upload(source_file) or request.source_file_id
        cited = request.model_copy(update={"source_file_id": source_file_id})
        return RecordedCashFlow.model_validate(
            self.client.post("/cash-flows", cited.model_dump(mode="json"))
        )


__all__ = [
    "Backend",
    "HistoryFilters",
    "LocalBackend",
    "OpenBet",
    "Proposal",
    "RemoteBackend",
    "api_session",
    "open_backend",
    "refuse_remote",
    "remote_url",
    "require_remote",
]
