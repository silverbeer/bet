"""User-scoped repositories.

The contract from docs/OWNERSHIP.md section 6:

    No repository method may execute a query against an owned table without an
    OwnerScope.

That is enforced by construction rather than by discipline. ``OwnerScope`` is a
constructor argument with no default and no ``None`` sentinel, and every query
goes through helpers that inject ``tenant_id`` and ``user_id`` themselves.
Repositories never assemble a bare ``WHERE`` clause, so there is no place for
someone to forget one.

Column lists are derived from the Pydantic models rather than written twice, so
a model field with no matching column fails loudly instead of silently reading
back as missing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from bet.errors import DatabaseError, NotFoundError
from bet.models.bet import Bet, BetLeg, BetLegGroup, BetPromotion, Promotion
from bet.models.ownership import OwnedModel, OwnerScope, SportsbookAccount

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class ScopedRepository[ModelT: OwnedModel]:
    """Base for every repository over an owned table.

    Subclasses declare a table and a model; they do not write scope predicates,
    because this class writes them.
    """

    table: ClassVar[str]
    model: ClassVar[type[OwnedModel]]

    def __init__(self, conn: DuckDBPyConnection, scope: OwnerScope) -> None:
        if not isinstance(scope, OwnerScope):  # pragma: no cover - typing backstop
            raise TypeError("a repository requires an OwnerScope")
        self._conn = conn
        self._scope = scope

    # ------------------------------------------------------------- internals

    @property
    def scope(self) -> OwnerScope:
        return self._scope

    @classmethod
    def columns(cls) -> list[str]:
        """Persisted columns, taken from the model so the two cannot diverge."""
        return list(cls.model.model_fields)

    def _scoped(self, extra: str | None = None) -> str:
        """Build a WHERE clause that always begins with the ownership predicate."""
        clause = "tenant_id = ? AND user_id = ?"
        return f"{clause} AND {extra}" if extra else clause

    def _scope_params(self) -> list[Any]:
        return [str(self._scope.tenant_id), str(self._scope.user_id)]

    def _rows(
        self,
        where: str | None = None,
        params: Sequence[Any] = (),
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        columns = ", ".join(self.columns())
        sql = f"SELECT {columns} FROM {self.table} WHERE {self._scoped(where)}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cursor = self._conn.execute(sql, [*self._scope_params(), *params])
        names = [d[0] for d in cursor.description or []]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def _build(self, row: Mapping[str, Any]) -> ModelT:
        return self.model.model_validate(dict(row))  # type: ignore[return-value]

    # ----------------------------------------------------------------- reads

    def get(self, entity_id: UUID) -> ModelT:
        """Fetch one row owned by this scope, or raise."""
        rows = self._rows("id = ?", [str(entity_id)])
        if not rows:
            raise NotFoundError(
                f"no {self.table} with id {entity_id} for this user.",
                remediation="Check the id, or run `bet doctor` to confirm the local identity.",
            )
        return self._build(rows[0])

    def find(self, entity_id: UUID) -> ModelT | None:
        rows = self._rows("id = ?", [str(entity_id)])
        return self._build(rows[0]) if rows else None

    def fetch_all(self, *, order_by: str | None = None, limit: int | None = None) -> list[ModelT]:
        return [self._build(r) for r in self._rows(order_by=order_by, limit=limit)]

    def count(self) -> int:
        row = self._conn.execute(
            f"SELECT count(*) FROM {self.table} WHERE {self._scoped()}", self._scope_params()
        ).fetchone()
        return int(row[0]) if row else 0

    # ---------------------------------------------------------------- writes

    def add(self, entity: ModelT) -> ModelT:
        """Insert, forcing the row into this scope regardless of what it claims.

        An entity carrying someone else's ownership is a programming error, so it
        is rejected rather than silently rewritten — quietly reassigning a row to
        the current user would be the exact leak this class exists to prevent.
        """
        if entity.tenant_id != self._scope.tenant_id or entity.user_id != self._scope.user_id:
            raise DatabaseError(
                f"refusing to write a {self.table} row owned by a different user.",
                remediation="Construct the entity with this repository's scope.",
            )

        values = entity.model_dump()
        columns = [c for c in self.columns() if c in values]
        placeholders = ", ".join("?" * len(columns))
        self._conn.execute(
            f"INSERT INTO {self.table} ({', '.join(columns)}) VALUES ({placeholders})",
            [_as_param(values[c]) for c in columns],
        )
        return entity

    def add_all(self, entities: Sequence[ModelT]) -> list[ModelT]:
        return [self.add(e) for e in entities]

    def delete(self, entity_id: UUID) -> int:
        """Delete within scope. Returns rows removed, so a no-op is visible."""
        before = self.count()
        self._conn.execute(
            f"DELETE FROM {self.table} WHERE {self._scoped('id = ?')}",
            [*self._scope_params(), str(entity_id)],
        )
        return before - self.count()


def _as_param(value: Any) -> Any:
    """UUIDs bind as strings; everything else DuckDB handles natively."""
    return str(value) if isinstance(value, UUID) else value


# --------------------------------------------------------------- repositories


class SportsbookAccountRepository(ScopedRepository[SportsbookAccount]):
    table = "core.sportsbook_account"
    model = SportsbookAccount

    def for_sportsbook(self, code: str) -> list[SportsbookAccount]:
        return [self._build(r) for r in self._rows("sportsbook_code = ?", [code])]


class BetRepository(ScopedRepository[Bet]):
    table = "core.bet"
    model = Bet

    def current(self, *, limit: int | None = None) -> list[Bet]:
        """Only current versions. Corrections supersede rather than overwrite."""
        return [
            self._build(r) for r in self._rows("is_current", order_by="placed_at DESC", limit=limit)
        ]

    def open_bets(self) -> list[Bet]:
        return [
            self._build(r)
            for r in self._rows("status = 'pending' AND is_current", order_by="placed_at DESC")
        ]

    def history(self, bet_id: UUID) -> list[Bet]:
        """The full correction chain for a bet, oldest version first.

        Walks ``supersedes_id`` backwards from the given row. Corrections create a
        new version and never overwrite, so this is the audit trail — and it is
        scoped like everything else, so it cannot walk into another user's rows.
        """
        chain: list[Bet] = []
        seen: set[UUID] = set()
        cursor: UUID | None = bet_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            found = self.find(cursor)
            if found is None:
                break
            chain.append(found)
            cursor = found.supersedes_id
        return list(reversed(chain))

    def provenance(self, bet_id: UUID) -> dict[str, Any]:
        """Where a bet came from: capture method, import run, source record, profile.

        The import-run and source-record tables arrive with SB-703, so the ids are
        reported as recorded rather than resolved. Reporting the id is honest;
        pretending to resolve it would not be.
        """
        bet = self.get(bet_id)
        return {
            "bet_id": bet.id,
            "capture_method": bet.capture_method,
            "import_run_id": bet.import_run_id,
            "source_record_id": bet.source_record_id,
            "profile_version": bet.profile_version,
            "external_bet_id": bet.external_bet_id,
            "external_receipt_id": bet.external_receipt_id,
            "version": bet.version,
            "is_current": bet.is_current,
        }


class BetLegRepository(ScopedRepository[BetLeg]):
    table = "core.bet_leg"
    model = BetLeg

    def for_bet(self, bet_id: UUID) -> list[BetLeg]:
        return [
            self._build(r) for r in self._rows("bet_id = ?", [str(bet_id)], order_by="leg_order")
        ]


class BetLegGroupRepository(ScopedRepository[BetLegGroup]):
    table = "core.bet_leg_group"
    model = BetLegGroup

    def for_bet(self, bet_id: UUID) -> list[BetLegGroup]:
        return [self._build(r) for r in self._rows("bet_id = ?", [str(bet_id)])]


class PromotionRepository(ScopedRepository[Promotion]):
    table = "core.promotion"
    model = Promotion


class BetPromotionRepository(ScopedRepository[BetPromotion]):
    table = "core.bet_promotion"
    model = BetPromotion

    def for_bet(self, bet_id: UUID) -> list[BetPromotion]:
        return [
            self._build(r) for r in self._rows("bet_id = ?", [str(bet_id)], order_by="apply_order")
        ]


OWNED_REPOSITORIES: tuple[type[ScopedRepository[Any]], ...] = (
    SportsbookAccountRepository,
    BetRepository,
    BetLegRepository,
    BetLegGroupRepository,
    PromotionRepository,
    BetPromotionRepository,
)


class Warehouse:
    """A unit of work: one connection, one scope, every repository.

    Commands take a Warehouse rather than a connection, so there is no ambient
    unscoped handle for an ad-hoc query to borrow.
    """

    def __init__(self, conn: DuckDBPyConnection, scope: OwnerScope) -> None:
        self._conn = conn
        self._scope = scope
        self.accounts = SportsbookAccountRepository(conn, scope)
        self.bets = BetRepository(conn, scope)
        self.legs = BetLegRepository(conn, scope)
        self.leg_groups = BetLegGroupRepository(conn, scope)
        self.promotions = PromotionRepository(conn, scope)
        self.bet_promotions = BetPromotionRepository(conn, scope)

    @property
    def scope(self) -> OwnerScope:
        return self._scope

    @contextmanager
    def transaction(self) -> Iterator[Warehouse]:
        """Commit on success, roll back on any failure.

        A partially written ticket — a bet with some of its legs — is worse than
        no ticket at all, because nothing downstream can tell it is incomplete.
        """
        self._conn.execute("BEGIN TRANSACTION")
        try:
            yield self
        except Exception as exc:
            self._conn.execute("ROLLBACK")
            if isinstance(exc, DatabaseError | NotFoundError):
                raise
            raise DatabaseError(
                f"the write was rolled back: {exc}",
                remediation="Nothing was committed. Correct the input and retry.",
            ) from exc
        else:
            self._conn.execute("COMMIT")


__all__ = [
    "OWNED_REPOSITORIES",
    "BetLegGroupRepository",
    "BetLegRepository",
    "BetPromotionRepository",
    "BetRepository",
    "PromotionRepository",
    "ScopedRepository",
    "SportsbookAccountRepository",
    "Warehouse",
]
