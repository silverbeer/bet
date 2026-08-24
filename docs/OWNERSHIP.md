# BET Ownership Model

**Status:** Frozen v1
**Date:** 2026-08-23
**Ticket:** SB-687
**Blocks:** SB-701 (tenants/users/accounts), SB-704 (repositories)

BET is a **single-user product with a multi-user schema** (SB-686). This document
fixes which tables carry ownership columns, how the local tenant and user are
bootstrapped, and how the repository layer guarantees that every query is scoped.

The goal is that a future multi-user mode **cannot leak by omission** — that
forgetting to scope a query is a schema error or a type error, not a silent
disclosure of someone else's betting history.

---

## 1. The ownership test

> **If deleting a user should delete the row, the row is owned.**

Delete a user and their bets, imports, source files, strategies and findings go
with them. FanDuel does not go. The Red Sox do not go. Last Tuesday's closing
line does not go.

That single question classifies every table, and it is the rule to apply when a
new table is added.

---

## 2. Table classification

### 2.1 Owned — `tenant_id` **and** `user_id`, both `NOT NULL`

| Schema | Tables |
|---|---|
| `core` | `sportsbook_account`, `bet`, `bet_leg`, `promotion`, `bet_promotion`, `bet_correction` |
| `control` | `import_run`, `source_file`, `validation_issue`, `reconciliation_result` |
| `raw` | `source_record`, `extraction_candidate` |
| `analytics` | `analysis_run`, `strategy`, `strategy_version`, `strategy_evaluation`, `finding`, `recommendation` |
| `agent` | `agent_run`, `report_artifact` |

`core.promotion` is the promotion **a user holds or was awarded** — a specific
free bet, a specific boost token. The market-wide offer that generated it is
`market.offered_promotion`, which is reference data.

### 2.2 Tenant-scoped only — `tenant_id`, no `user_id`

| Table | Reason |
|---|---|
| `core.user` | A user belongs to a tenant. Its own id *is* the user id; primary key is `(tenant_id, id)`. |

Nothing else. Tenant-shared-but-not-user-owned rows (a syndicate's shared
strategy, say) do not exist yet and will not be speculatively modelled. Adding
one later is a deliberate migration, which is the correct cost for a real
requirement.

### 2.3 Reference — neither column

| Schema | Tables |
|---|---|
| `core` | `tenant`, `sportsbook`, `external_identity`, `entity_alias` |
| `control` | `import_profile_version` |
| `sports` | all — `league`, `competition`, `team`, `player`, `player_team_tenure`, `event`, `event_participant`, `event_result`, `venue` |
| `market` | all — `collection_run`, `market`, `line_snapshot`, `offered_promotion`, `line_movement_summary` |
| `reference` | all controlled taxonomies |

Sports facts and market facts are world facts. FanDuel is FanDuel for everyone;
a game's final score is not personal data. Attaching ownership to them would
duplicate the entire sports database per user for no benefit.

### 2.4 Deferred, not built

`core.follow_relationship`, `core.data_consent`, `core.sharing_policy`, and
`benchmark_cohort` are **not created** by any migration. See
`BET_IMPLEMENTATION_PLAN.md` §2 and its trigger condition: a real second user.

---

## 3. Both columns, always — no nullable ownership

Owned rows carry `tenant_id` **and** `user_id`, both `NOT NULL`.

`tenant_id` is denormalized rather than reached through `user_id`. It is the
isolation boundary a hosted deployment filters on first, and the natural
partition key; requiring a join to discover it puts the most security-critical
predicate behind the most forgettable `JOIN`.

Nullable ownership is prohibited. A nullable `user_id` means every scoped query
needs `(user_id = ? OR user_id IS NULL)`, and the day someone omits the second
half is the day the model silently changes meaning.

---

## 4. Divergence is impossible, not merely tested

The denormalization in §3 creates an obvious hazard: a `bet_leg` could claim a
different owner than its parent `bet`.

**DuckDB prevents this structurally via composite foreign keys**, which were
verified to be enforced rather than merely accepted (§7):

```sql
CREATE TABLE core.bet (
  tenant_id UUID NOT NULL,
  user_id   UUID NOT NULL,
  id        UUID NOT NULL DEFAULT uuidv7(),
  PRIMARY KEY (tenant_id, user_id, id),
  FOREIGN KEY (tenant_id, user_id) REFERENCES core.user (tenant_id, id)
);

CREATE TABLE core.bet_leg (
  tenant_id UUID NOT NULL,
  user_id   UUID NOT NULL,
  bet_id    UUID NOT NULL,
  leg_order INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, user_id, bet_id, leg_order),
  FOREIGN KEY (tenant_id, user_id, bet_id)
    REFERENCES core.bet (tenant_id, user_id, id)
);
```

A leg quoting a **real** `bet_id` but the **wrong** `user_id` is rejected by the
database:

```
Constraint Error: Violates foreign key constraint
```

The ticket asked for the convention to be "enforced by the repository tests."
It is enforced one layer lower than that. Tests can be forgotten; the constraint
cannot.

**Rule:** every owned child table references its parent through the full
`(tenant_id, user_id, …)` key, never through the bare parent id.

---

## 5. Identifiers

### 5.1 `uuidv7()` for owned rows

DuckDB 1.5.5 provides `uuidv7()` natively, and it is time-ordered — verified
sortable (§7). That gives globally unique ids that still cluster by insertion
time, so primary-key locality is preserved without a sequence.

```sql
id UUID NOT NULL DEFAULT uuidv7()
```

Sequential `BIGINT` keys are rejected: they collide the instant two BET
databases are ever merged, which is precisely the scenario these columns exist
to enable.

### 5.2 Natural keys for reference rows

Reference tables use stable natural keys where one exists — `sportsbook.code =
'fanduel'`. These are readable in queries and fixtures, and they are the same
value in every deployment, which is what makes a merge tractable.

### 5.3 The local tenant and user get **generated** ids, not well-known ones

`bet init` creates exactly one tenant and one user, both with generated
`uuidv7()` values.

Fixed well-known ids — a nil UUID, `tenant-1` — are tempting because they make
fixtures readable. They are rejected for the same reason as sequential keys: if
every BET install uses the same local tenant id, then two installs are
guaranteed to collide on the one operation the multi-user schema was built to
support.

Fixture readability is recovered by naming ids in the fixture layer, not by
making them globally constant.

### 5.4 Resolving "the current user"

`bet init` writes the generated ids to config (`$XDG_CONFIG_HOME/bet/config.toml`).
The CLI resolves them **once** at startup into an `OwnerScope`, and every
downstream call receives that object.

If config and database disagree — a restored backup against a stale config, say
— `bet doctor` reports it and commands refuse to run rather than silently
operating as a user that does not exist.

---

## 6. Repository rule: scoped by construction

> **No repository method may execute a query against an owned table without an
> `OwnerScope`.**

The mechanism is that scope is a *constructor* argument, not a parameter that
each method must remember to accept:

```python
@dataclass(frozen=True)
class OwnerScope:
    tenant_id: UUID
    user_id: UUID


class ScopedRepository:
    """Base for every repository over an owned table."""

    def __init__(self, conn: DuckDBPyConnection, scope: OwnerScope) -> None: ...
```

There is no default scope, no `None` sentinel, and no module-level connection an
ad-hoc query could borrow. A repository that cannot be constructed without a
scope cannot be used without one.

### 6.1 Enforcement, in layers

1. **Schema** — composite foreign keys prevent cross-owner rows entirely (§4).
2. **Types** — `ScopedRepository.__init__` requires `OwnerScope`; there is no
   unscoped construction path.
3. **Tests** — SB-706 asserts that (a) every repository over an owned table
   derives from `ScopedRepository`, by reflection, so a new repository cannot
   quietly opt out, and (b) seeding two users and querying as one returns only
   that user's rows, for every owned table.
4. **CI** — a lint step rejects raw SQL naming an owned table outside the
   repository layer.

Layer 3(a) matters more than it looks: it converts "remember to scope the new
repository" from a code-review responsibility into a failing test.

### 6.2 Views carry ownership through

Every analytics view over owned data **selects `tenant_id` and `user_id`** so it
can be scoped by the same mechanism. A view that aggregates them away is
unscopable, and would become a leak the moment a second user exists.

Aggregate views therefore group by ownership even when there is exactly one
owner and the grouping looks redundant. It is not redundant; it is the property
that keeps the view correct later.

---

## 7. Verified DuckDB capabilities

Confirmed by execution against DuckDB 1.5.5, not from documentation. Each
"should fail" case did fail.

| Capability | Result |
|---|---|
| `uuidv7()` available | ✅ |
| `uuidv7()` time-ordered / sortable | ✅ |
| `DEFAULT uuidv7()` on a column | ✅ |
| Composite `PRIMARY KEY` | ✅ |
| Composite `FOREIGN KEY` (2- and 3-column) | ✅ |
| `CHECK` constraint | ✅ |
| FK **enforced** — orphan insert rejected | ✅ blocked |
| FK **enforced** — child claiming wrong owner rejected | ✅ blocked |
| FK **enforced** — deleting a parent with children rejected | ✅ blocked |
| `CHECK` **enforced** — violating insert rejected | ✅ blocked |

This matters because "supports foreign keys" and "enforces foreign keys" are
different claims, and the whole enforcement argument in §4 rests on the second.

---

## 8. Consequences

- Ownership columns exist from migration 1 and cost two columns per owned table.
- Cross-owner corruption is structurally impossible rather than conventionally
  avoided.
- Adding a second user later is a product and privacy problem, not a data
  migration.
- Reference data stays single-copy and shared.
- Every owned child table carries a wider primary key than it strictly needs
  today. That is the price of §4, and it is worth paying.
