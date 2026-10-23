# ADR 0002: Serve the warehouse through a FastAPI service that embeds DuckDB

**Status:** Accepted
**Date:** 2026-10-23
**Ticket:** SB-1393
**Epic:** BET — API Service (2.0), SB-1393 … SB-1405
**Resolves:** the "future service boundary" open question in
[`BET_IMPLEMENTATION_PLAN.md`](../BET_IMPLEMENTATION_PLAN.md) §2, whose revisit
trigger was DuckDB 2.0 GA (released 2026-10-21).

## Context

BET is a CLI that opens a DuckDB file on the machine it runs on. That file lives
on one laptop. The author wants to:

- run the warehouse 24×7 on a home server (a Mac mini already running k3s) and
  use `bet` from the laptop over Tailscale;
- keep the Rich CLI as the primary interface;
- authenticate with JWTs;
- leave room for more than one user, without building a social product.

Three facts constrain the design.

1. **DuckDB allows one read-write process per database file.** Any design with
   two processes writing the same file — two CLIs over a network share, two
   pods during a rolling update, two uvicorn workers — is either a lock error or
   corruption risk.
2. **BET's correctness rules live in Python, not in the database.** Owner
   scoping (`ScopedRepository`, [`OWNERSHIP.md`](../OWNERSHIP.md)), screenshot
   provenance, append-only balance and cash-flow records, and Decimal settlement
   and boost arithmetic are enforced by the repository and command layers. A
   client that reaches the tables without going through that code bypasses all
   of them.
3. **The schema is already multi-user.** `tenant_id` and `user_id` are on every
   owned row from migration 1, and every query is scoped through an
   `OwnerScope`. What is missing is identity: credentials, tokens, and a way to
   resolve a scope from a request instead of from the local config file.

## Decision

### 1. One FastAPI process owns the warehouse

A FastAPI application opens the DuckDB file **in-process** — exactly as the CLI
does today — and is the only process that ever opens it read-write.

- **Exactly one uvicorn worker.** The app refuses to start if more are
  configured. Concurrency inside the process uses one connection with a
  `cursor()` per request.
- DuckDB stays **embedded**. No client/server protocol sits between the API and
  its own file.
- ADR 0001 carries over unchanged: DuckDB 1.5.x, storage format pinned via
  `storage_compatibility_version`. The DuckDB 2.0 upgrade is an independent
  decision and does not block or follow from this one.

### 2. A service layer is the single implementation

Business logic moves out of the Typer commands into `bet/services/`: plain
functions that take a scoped `Warehouse` and typed (pydantic) input and return
typed results, with no Typer or Rich imports. Validation and provenance rules
live there, so an API caller cannot skip them.

Both front ends call the same services:

```
CLI (local mode)  ──────────────▶ services ──▶ repository ──▶ DuckDB
CLI (remote mode) ──HTTPS──▶ FastAPI ──▶ services ──▶ repository ──▶ DuckDB
```

### 3. The CLI keeps its Rich output and gains a remote backend

- **local**: calls the services directly, as today. Retained permanently — tests,
  offline use, and the admin commands depend on it.
- **remote**: calls the API with httpx, selected by config `api_url`.
- Rendering stays on the client. The API returns data (e.g. `card_to_dict`); the
  CLI draws the same Rich panels from it.
- `bet login` / `bet logout`; tokens are stored in the macOS Keychain, never in
  the config file.

### 4. BET issues its own JWTs

- Passwords hashed with argon2 in a new `core.user_credential` table. Hashes are
  never logged or returned.
- `POST /auth/token` issues a short-lived access token (~15 minutes) and a
  refresh token (~30 days); refresh tokens rotate and can be revoked.
- The signing key comes from the environment (a Kubernetes Secret sourced from
  the 1Password `agents` vault). It is never committed.
- Every route except health checks requires a token. The request's
  `OwnerScope` is built **only** from the token's `tenant_id` and `user_id`
  claims.

### 5. Multi-user capable, admin-invite only

- A second user can exist and is fully isolated: the server path never uses the
  local "first user in `core.user`" shortcut (`identity.existing_scope`).
- Users are created only by the operator, with `bet user create` in local mode.
  There is no signup endpoint.
- Isolation is proven by tests: as user B, every endpoint is exercised against
  user A's ids and must return 404 or empty, never A's data.
- Sharing, following, consent and benchmark cohorts stay deferred, exactly as
  `BET_IMPLEMENTATION_PLAN.md` §2 says. Their trigger condition (a real second
  user) is unchanged.

### 6. Deployment: one pod on k3s, tailnet only

- Container image: Python 3.14-slim, uv, non-root, single-worker uvicorn; built
  and pushed to GHCR by CI.
- Deployment `replicas: 1`, `strategy: Recreate` — Kubernetes never runs an old
  and a new pod against the file at the same time.
- Warehouse on a `local-path` PersistentVolume; migrations run in an init
  container (`migrator.require_current` gates readiness).
- Reachable only over Tailscale, which also provides TLS. No public ingress.
- A nightly CronJob takes a **consistent** backup (EXPORT DATABASE, or a
  read-only open while no write is in flight — never a raw copy mid-write) and
  ships a copy off the mini.

### 7. Cutover is the last step, and reversible

The laptop's local warehouse remains authoritative until the cutover ticket
(SB-1405): back up, copy into the volume, verify counts and `bet card` match,
point the CLI at the API, rename the local file so nothing writes to it. Until
then, every merged ticket must leave local mode fully working — the author
records bets daily.

## Alternatives rejected

### DuckDB 2.0 client/server mode (Quack)

Quack shipped in 2.0.0 and is real, but it makes the **database** remote, and
BET needs the **application** remote.

- A Quack client holds full SQL access: the docs state a server "exposes the full
  SQL surface of the underlying DuckDB instance, including read and write access
  to every table." Owner scoping, provenance and append-only rules (fact 2) would
  be bypassed or enforced client-side, where nothing guarantees them.
- Authorization is permissive by default (`quack_nop_authorization`). Per-user
  row isolation would have to be written as SQL macros, which the docs limit to
  a single expression and whose own read-only example "is not a robust
  read-only filter".
- It solves nothing this design leaves open: one embedded writer behind FastAPI
  already gives remote access and a single writer, on DuckDB 1.5.
- Maturity: every client must run 2.0; no documented Python client API; client
  reconnects are off by default; DuckDB says to "expect further improvements in
  upcoming patch releases".

**Revisit if** Quack gains real per-user authorization, or a read-only SQL door
for ad-hoc analysis (notebooks, the DuckDB UI) is wanted alongside the API.

### A second transactional engine (e.g. PostgreSQL) for identity and writes

The implementation plan left this open. It is not needed at BET's scale: a single
embedded writer handles one operator's (or a handful of invited users') write
volume, and identity is two small tables. A second engine would split the source
of truth and double the operational surface. **Revisit if** concurrent write
volume ever exceeds what one process serves, or open signup is pursued.

### A Go or Rust API server

Writing it is cheap with AI assistance; keeping two implementations correct is
not. All domain logic — settlement, boosts, Decimal money, reconciliation, card
analytics — is Python, guarded by the test suite. A non-Python server would
either reimplement it (two sources of truth for money) or proxy to Python (a
layer that buys nothing). Performance is not a constraint: DuckDB does the heavy
work in C++ regardless of the host language. New, independent services (e.g. an
odds collector) are the right place for another language.

### An external identity provider (Auth0, Clerk, Keycloak)

Adds a dependency and an internet round-trip for a service that is reachable
only on a tailnet by invited users. **Revisit if** the API is ever exposed
publicly or SSO is wanted.

### Tailscale identity instead of passwords

Attractive (no credentials at all) but ties authentication to the network path:
local mode, tests and any non-tailnet client would need a second mechanism
anyway. JWTs work everywhere; Tailscale remains the network boundary.

### Multiple workers, or more than one replica

Violates fact 1. Horizontal scaling is not a goal.

## Consequences

- `ARCHITECTURE.md` no longer describes BET as login-free and laptop-only; the
  "Local First" principle becomes "self-hosted first" (data stays on hardware the
  author owns). See the amendments made with this ADR.
- `bet` in remote mode depends on the network and on the mini being up. Local
  mode remains for offline use.
- Releases gain a build-and-deploy step (image, GitOps tag) in addition to
  `git pull`.
- A JWT signing key and user passwords become secrets that must be rotated and
  never printed.
- Adding an endpoint means: service function first (tested), route second, CLI
  remote call third.
