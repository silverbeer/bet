# ADR 0001: Build on DuckDB 1.5.x, pin the storage format, run 2.0 in CI

**Status:** Accepted
**Date:** 2026-08-23
**Ticket:** SB-781
**Blocks:** SB-699 (migration strategy)

## Context

SB-781 was raised because DuckDB 2.0 was reported to add client/server operation
(the Quack protocol), triggers, asynchronous I/O, a new storage format, and an
extensible SQL parser. No warehouse code exists yet, so this is the cheapest
moment to evaluate and the most expensive one to skip.

The ticket instructed that the reported feature list be treated as unverified.
It was verified, both against DuckDB's published material and by running the
preview build directly.

## Findings

### Release status

| | |
|---|---|
| Latest stable | 1.5.5, released 2026-07-22 |
| Next release | 1.5.6, scheduled 2026-09-16 |
| 2.0.0 | "Fall 2026" on the release calendar — **preview only** |

No `2.x` version exists on PyPI. `uv add duckdb --prerelease allow` resolves to
package version `1.6.0.dev365`, but the engine inside it reports:

```
SELECT version()  ->  v2.0.0-alpha38615   ("Cyanoptera")
```

The package version string lags the engine. This *is* the 2.0 line, and it is an
**alpha** — not a beta, not a release candidate.

DuckDB's own preview post states that v2.0 "will also come with a small set of
breaking changes … which we will cover in detail in the release announcement."
**The breaking-change list is not yet published.**

### Storage format — the decisive finding

A table written by the 2.0 alpha in default mode cannot be read by 1.5.5:

```
IO Error: Trying to read a database file with version number 999,
but we can only read versions between 64 and 68.
```

Real storage versions are sequential: 65 = v1.2.x, 66 = v1.3.x, 67 = v1.4.x,
68 = v1.5.x. **999 is a development sentinel.** The on-disk format is not
finalised, so data written by today's alpha carries no compatibility guarantee
with anything — including 2.0 GA itself.

BET's premise is that canonical bets and raw evidence are retained indefinitely.
An unfinalised storage format is therefore disqualifying for real data, not
merely risky.

The escape hatch works, however. Verified cross-version behaviour:

| Writer | Format | Read by 1.5.5 |
|---|---|---|
| 2.0-alpha | default | ✗ version 999 |
| 2.0-alpha | `STORAGE_VERSION 'v1.5.0'` | ✓ |
| 1.5.5 | default | ✓ (read by 2.0-alpha) |

Backward compatibility — a newer engine reading older files — holds in practice
and is a documented DuckDB guarantee. Forward compatibility is best-effort only,
which is irrelevant here because BET only ever moves forward.

### Migration cost is small, and scales with data size

Migration between storage versions is `EXPORT DATABASE` / `IMPORT DATABASE`, or
`COPY FROM DATABASE a TO b`. That cost scales with data volume. BET's canonical
data is one person's betting history — thousands of rows, not billions.

**SB-699's migration harness does not need to model storage-format migration.**
A documented runbook plus a backup covers it. Building format-migration
machinery for a database that fits in RAM is the definition of designing it
twice.

### Triggers — the published claim overstates the alpha

The preview post claims triggers arrive "in full: BEFORE and AFTER triggers,
FOR EACH ROW and FOR EACH STATEMENT, transition tables via REFERENCING OLD/NEW
TABLE." Measured against the alpha:

```
[OK]   AFTER INSERT ... FOR EACH ROW            fires correctly, verified
[FAIL] BEFORE UPDATE ... FOR EACH ROW           "not yet supported"
[FAIL] FOR EACH STATEMENT + FOR EACH ROW mixed  "not yet supported"
```

`duckdb_triggers()` exists and reports correctly.

BEFORE-ROW is the variant that would matter for provenance, because it is what
allows a write to be intercepted before it lands. AFTER-only permits auditing,
not prevention. This reinforces the position already taken in SB-704:
**provenance enforcement stays in the repository layer, with tests.** Logic
hidden in triggers is hard to test and harder to reason about; that argument was
already the ticket's own, and the missing BEFORE-ROW support settles it for now.

### Quack — real, and it does undercut a documented assumption

The extension installs and loads in the alpha, exposing `quack_serve`,
`quack_check_token`, `quack_identify`, `has_server_privilege`, and configurable
`quack_authentication_function` / `quack_authorization_function` settings.

Published material states Quack supports multiple concurrent writers with
pluggable authentication and authorization. Stated limitations: binds to
localhost by default, no SSL by default, and throughput around 5,400 txn/s
attributed to a DuckDB concurrent-insert limitation rather than the protocol.

This genuinely weakens the assumption in `BET_IMPLEMENTATION_PLAN.md` §2 that a
hosted deployment must introduce a second transactional engine. It does not
settle it — that cannot be decided against an alpha whose production release is
explicitly scheduled alongside 2.0 GA.

### Async I/O and partition-aware optimisation

The alpha exposes an `async_threads` setting (default 32). No data volume exists
in BET to justify evaluating this. Deferred entirely.

### Incidental findings

- Reading `TIMESTAMPTZ` values into Python requires `pytz` to be installed. BET
  stores timezone-aware timestamps throughout, so this is a real runtime
  dependency, not optional.
- duckdb 1.5.5 publishes cp310–cp314 wheels, so it runs on the local Python
  3.14 as well as the 3.13 floor in `ARCHITECTURE.md`.

## Decision

1. **Build on DuckDB 1.5.x**, currently 1.5.5. It is the current stable line and
   receives patch releases.

2. **Pin the storage format explicitly** rather than relying on the default.
   In Python this is a connection setting, applied when the file is created:

   ```python
   duckdb.connect(path, config={"storage_compatibility_version": "v1.5.0"})
   ```

   An explicit pin means the format BET writes is a deliberate choice recorded
   in code, and it cannot drift when the library is upgraded.

   **Amendment, 2026-08-26 (SB-699).** The pin is not belt-and-braces; it
   changes what is written. DuckDB 1.5.5's *default* is storage version **64**
   — the v0.9–v1.1 format — not its own native 68. Measured:

   | pin | on-disk version |
   |---|---|
   | *(none — the default)* | 64 |
   | `v1.2.0` | 65 |
   | `v1.4.0` | 67 |
   | `v1.5.0` | **68** |

   DuckDB defaults to the oldest broadly-readable format so that files stay
   openable by older clients. BET pins 68 instead, because its only access path
   is its own CLI, which constrains `duckdb>=1.5.5,<2`; compatibility with
   DuckDB releases older than 1.5 buys nothing here. The trade-off is real
   though — a third-party tool bundling an older DuckDB cannot open a version-68
   file. `storage_version` is therefore a configuration setting, not a constant,
   and `bet doctor` reports the format actually on disk alongside the configured
   pin, since the two legitimately differ for a warehouse created before the
   setting changed.

3. **Run the test suite against the 2.0 preview in CI**, as a second job that is
   allowed to fail without blocking:

   ```bash
   uv sync --prerelease allow      # or UV_PRERELEASE=allow
   ```

   The breaking-change list for 2.0 is unpublished. A CI job that exercises BET
   against the alpha on every commit discovers those changes as they land,
   months before the announcement, at near-zero cost and with no real data at
   risk. This is how BET "tries 2.0" — continuously, without betting the
   warehouse on it.

4. **Do not adopt triggers** for provenance. Revisit only if application-layer
   enforcement proves leaky in practice, and only once BEFORE-ROW triggers are
   supported.

5. **Do not build the Parquet offload** described in plan §4. Keep canonical and
   raw data in DuckDB. Revisit only if market snapshots (M8) ever create real
   volume.

6. **Re-evaluate at 2.0 GA**, specifically: the final storage version number and
   its migration path, the published breaking-change list, whether 2.0.0 is an
   LTS release, and whether Quack's production release changes the hosted-service
   boundary.

### Not chosen: start on the 1.4.x LTS line

LTS is every other release from 1.4.0, so 1.5.x is not an LTS line. But 1.4.0's
end-of-life is 2026-09-16 — roughly three weeks from this decision — which makes
the LTS alternative already dead. Whether 2.0.0 carries LTS status could not be
confirmed from current documentation and is listed above as a GA follow-up.

## Consequences

- The warehouse is built on a format that a future DuckDB is guaranteed to read.
- Upgrading to 2.0 becomes: change the pin, run `COPY FROM DATABASE`, verify.
  The migration is a runbook, not a subsystem.
- 2.0 breaking changes surface in CI as they are committed upstream rather than
  at adoption time.
- BET forgoes async I/O, partition-aware optimisation, triggers, and Quack until
  2.0 GA. None of these are on the path to the current milestone, which is
  recording bets and reporting ROI.

## Verification

Findings above were produced by installing both versions side by side and
exercising them directly:

```bash
uv venv /tmp/ddb-preview --python 3.13
uv pip install --python /tmp/ddb-preview/bin/python --pre duckdb   # 2.0.0-alpha38615
uv venv /tmp/ddb-stable  --python 3.13
uv pip install --python /tmp/ddb-stable/bin/python  duckdb==1.5.5
```

Cross-version reads, `STORAGE_VERSION` pinning, trigger creation and firing, and
Quack loading were each executed rather than inferred.
