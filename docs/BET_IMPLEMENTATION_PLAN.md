# BET Implementation Plan

**Status:** Draft for architectural review  
**Scope:** Planning only; this document authorizes no implementation work.

## 1. Product and architecture review

### Vision

BET is a local-first, CLI-first personal betting intelligence platform. It turns a user's historical betting activity into evidence-based learning:

> Transform betting history into betting intelligence.

BET is not a sportsbook, automated betting bot, or prediction engine. Its primary output is transparent analysis of historical behavior, including where a user has performed well, poorly, consistently, or inconsistently.

### Product goals

1. Build a complete, trustworthy warehouse of historical bets across sportsbooks.
2. Normalize bets so performance can be compared by sportsbook, sport, team, player, market, odds, wager type, promotion, and situation.
3. Identify profitable and unprofitable historical patterns.
4. Enrich betting history with independent sports data.
5. Compare a prospective opportunity with historical patterns without claiming to predict outcomes.
6. Support repeatable review workflows through a CLI and, later, evidence-bound agent workflows.
7. Begin local-first while safely supporting many users and opt-in shared intelligence in a future service.

### Core workflows

```text
Sportsbook export / document
  -> importer profile
  -> immutable raw capture
  -> normalization and validation
  -> deduplication and identity resolution
  -> canonical betting warehouse
  -> analytics and review reports
  -> optional sports / market enrichment
  -> opportunity comparison and agent workflows
```

Historical sportsbook ingestion is the critical workflow. Sports and market data must enrich, rather than block, sportsbook ingestion.

### MVP definition

The documented Phase 1 is the functional MVP:

- Import historical CSV, XLSX, and PDF records.
- Support FanDuel, DraftKings, BetMGM, Fanatics, Caesars, Bally Bet, and theScore Bet through a common framework.
- Persist a traceable canonical betting model in DuckDB.
- Prevent duplicate records and preserve source evidence.
- Report lifetime and grouped ROI by sportsbook, sport, team, and wager type.
- Operate entirely through the CLI.

Recommended delivery sequence within the MVP:

1. Agree financial, settlement, privacy, and source-data definitions.
2. Build the common warehouse and importer framework.
3. Prove it with one representative CSV importer.
4. Add XLSX/PDF extraction and all sportsbook profiles using actual redacted fixtures.
5. Release baseline analytics after ingestion correctness and reconciliation are proven.

### Long-term roadmap

| Phase | Outcome |
|---|---|
| 1. Historical data warehouse | Complete, normalized, traceable betting history and baseline analytics |
| 2. Post-bet intelligence | Strategies, lessons, trends, recurring reviews |
| 3. Sports data platform | Independent team, player, schedule, result, and context data |
| 4. Market intelligence | Line snapshots, promotions, boosts, and market comparisons |
| 5. Agent workflows | Auditable daily, weekly, monthly, lesson, and opportunity workflows |
| Future | Screenshot/OCR, inbox and email ingestion, optional dashboard, hosted multi-user service |

### Missing decisions and open questions

- Settlement semantics for cash-out, partial cash-out, push, void, dead heat, partial results, insurance, free bets, and bonus bets.
- Currency, timezone, rounding, fees, and tax treatment.
- Exact sportsbook export samples by operator, region, and format.
- Whether PDFs are text-based, scanned, or rendered statements.
- Sports identity-resolution rules for team/player aliases and ambiguous names.
- Strategy definition: saved user filters, automatically discovered cohorts, or both.
- Minimum sample size and statistical-confidence rules for findings.
- Local data protection, future account authentication, consent, export, and deletion policy.
- Current-market data providers, licensing, and permitted collection methods.
- Explicit boundary for stake recommendations and bankroll-risk policy.

### Architectural risks

- Sportsbook exports vary by provider, region, format version, and wager type; importers must be versioned adapters.
- A ticket can contain multiple legs. A flat bet table would distort parlays, same-game parlays, and promotions.
- PDF/screenshot extraction may produce false records and needs a review path.
- Small samples and multiple comparisons can create misleading strategy findings.
- Sports data is a separate product domain and can overwhelm the core project if placed on the ingestion critical path.
- DuckDB suits local single-user analytics. A future concurrent hosted service needs a transactional authorization and write layer in addition to analytical storage.

## 2. Multi-user, ownership, and shared intelligence

BET begins as a local deployment for Tom, but the model must be multi-user from the first migration. Tom may own five or more sportsbook accounts. Future users, such as Jim, may follow one another and opt into aggregate learning.

### Ownership model

```text
Tenant
  -> User
    -> SportsbookAccount
      -> Bet
        -> BetLeg

User -> follows -> User
User -> grants consent -> BET aggregate learning
```

All user-owned records include `user_id`. In a future hosted deployment, all tenant-bound records also include `tenant_id`.

### New entities

| Entity | Purpose |
|---|---|
| `Tenant` | Future isolation boundary; initially one local personal tenant |
| `User` | Identity, profile, locale, timezone, preferences, and status |
| `SportsbookAccount` | One provider account owned by one user |
| `DataConsent` | Versioned permission to use defined data for defined purposes |
| `SharingPolicy` | Private, follower-visible, aggregate-only, or excluded data policy |
| `FollowRelationship` | Tom follows Jim; does not grant raw-data access |
| `BenchmarkCohort` | Privacy-safe aggregate population for comparisons |

### Privacy principles

- Follow relationships never expose raw bet history by default.
- A user may explicitly share selected reports or published strategies with followers.
- Direct record sharing requires a separate, revocable grant.
- Cross-user insights use only consented data and privacy-preserving aggregate cohorts.
- Suppress cohorts below a defined threshold, initially no fewer than 20 users plus sufficient bet volume.
- Never expose another user's exact bets, stakes, account identities, performance, or data that permits reasonable re-identification.
- Consent, revocation, and data-access decisions are auditable.

### Future service boundary

DuckDB remains appropriate for local data and analytical workloads. A hosted service will introduce a transactional database/service for identity, authorization, consent, concurrent writes, and social relationships. The analytical warehouse receives only appropriately scoped data.

## 3. Domain model design

### Core entities

| Entity | Purpose | Principal relationships |
|---|---|---|
| `User` | BET identity and personal ownership | Owns accounts, imports, bets, strategies, analyses |
| `Sportsbook` | Operator identity and export capabilities | Has accounts, profiles, promotions, line snapshots |
| `SportsbookAccount` | One user's account at an operator | Owns bets and import context |
| `ImportRun` | One auditable import attempt | Owns source files, raw records, issues, reconciliation |
| `Bet` | Canonical ticket/wager financial record | Belongs to account; owns one or more legs |
| `BetLeg` | Atomic selection within a bet | References event, line, team/player/selection |
| `Event` | Sporting contest or future event | Has participants, schedule, venue, result |
| `Team` | Canonical team identity | Participates in events; has aliases/provider IDs |
| `Player` | Canonical athlete identity | Has team tenure; may be a prop target |
| `Line` | Offered selection and price at a point in time | Belongs to a bet leg or market snapshot |
| `Promotion` | Boost, token, insurance, free bet, or offer | Applies to bets and affects economics |
| `Strategy` | Versioned, saved cohort rule | Evaluated against settled bets |
| `Analysis` | Immutable analysis result | References an input period, rules, metrics, findings |

Supporting entities include `League`, `Venue`, `EventParticipant`, `SourceFile`, `RawRecord`, `ExternalIdentity`, `EntityAlias`, `Settlement`, `Tag`, `StrategyVersion`, `Recommendation`, `AgentRun`, and `ReportArtifact`.

### Canonical bet model

`Bet` is the ticket-level aggregate. `BetLeg` is the selection-level record. This accommodates straight bets, parlays, same-game parlays, teasers, round robins, and futures without losing financial integrity.

**Bet fields**

- Internal immutable ID; `tenant_id`; `user_id`; sportsbook; sportsbook account.
- External ticket ID, raw source key, import run, parser/profile version.
- `placed_at`, `accepted_at`, `settled_at`, stored as timezone-aware timestamps.
- Canonical status and result.
- Wager kind: straight, parlay, same-game parlay, teaser, round robin, future, system, or unknown.
- Cash stake, bonus/free-bet stake, total risk, currency, gross return, cash-out amount, net profit/loss.
- Original and normalized odds.
- Promotion-adjusted and cash-only financial metrics.
- Correction/version metadata and full source lineage.

**Bet leg fields**

- Parent bet ID and stable leg order.
- Sport, league, event reference or unresolved source text.
- Market family, market name, selection name, side, line value, and source/normalized odds.
- Target team, player, event participant, or future subject.
- Home/away context when known.
- Leg settlement result where supplied.

**Financial definitions**

```text
net_profit = realized_cash_return - realized_cash_cost
cash_roi = net_cash_profit / cash_stake
economic_roi = economic_net_value / economic_risk
```

Free bets, boosts, insurance, and bonus stakes must be represented explicitly and never silently treated as ordinary cash stake.

### Relationships

```text
User -> SportsbookAccount -> Bet -> BetLeg -> Event
                                  |       -> Line
                                  |       -> Promotion
                                  -> ImportRun / RawRecord

League -> Event <- EventParticipant -> Team
Player -> PlayerTeamTenure -> Team
Player ----------------------> BetLeg

Strategy -> StrategyVersion -> Analysis -> Finding / Recommendation
```

### Aggregate boundaries

- **Bet:** ticket, legs, promotions, settlement, and correction state.
- **ImportRun:** source artifact, extractor/profile version, raw records, validation outcomes, and reconciliation.
- **Sports identity:** canonical teams, players, events, aliases, and provider IDs. Sportsbook imports cannot mutate it implicitly.
- **Strategy:** rule definition, version history, sharing policy, and lifecycle.
- **Analysis:** reproducible inputs, calculations, findings, and rendered artifact.
- **Consent/following:** independently auditable aggregates with separate lifecycles.

### Model evolution

- Phase 1 retains unresolved event/team/player text while preserving source truth.
- Phase 2 adds tags, strategies, findings, and versioned analyses.
- Phase 3 maps sportsbook labels to canonical sports identities.
- Phase 4 adds independent market/line snapshots and promotion terms.
- Corrections create a new canonical/correction version; they never overwrite original raw source evidence.

## 4. DuckDB data architecture

### Namespace organization

| Schema | Contents |
|---|---|
| `control` | migrations, configuration, import runs, source files, parser versions, validation and reconciliation |
| `raw` | immutable imported source rows and extraction candidates |
| `core` | users, accounts, bets, legs, promotions, identities, aliases, corrections |
| `sports` | leagues, teams, players, events, participants, schedules, results, aliases |
| `market` | future line snapshots, boosts, offers, collection metadata |
| `analytics` | derived views, rollups, strategy evaluations, findings, recommendations |
| `agent` | agent run audit trails and report artifacts |
| `reference` | controlled taxonomies and mapping values |

### Principal tables

```text
control.import_run
control.source_file
control.import_profile_version
control.validation_issue
control.reconciliation_result

raw.source_record
raw.extraction_candidate

core.tenant
core.user
core.follow_relationship
core.data_consent
core.sharing_policy
core.sportsbook
core.sportsbook_account
core.bet
core.bet_leg
core.promotion
core.bet_promotion
core.external_identity
core.entity_alias
core.bet_correction

sports.league
sports.competition
sports.team
sports.player
sports.player_team_tenure
sports.event
sports.event_participant
sports.event_result
sports.venue

market.collection_run
market.market
market.line_snapshot
market.offered_promotion
market.line_movement_summary

analytics.analysis_run
analytics.strategy
analytics.strategy_version
analytics.strategy_evaluation
analytics.finding
analytics.recommendation
```

### Views and derived analytics

Use transparent views for live calculations:

- `analytics.v_settled_bets`
- `analytics.v_bet_financials`
- `analytics.v_bet_dimensions`
- `analytics.v_roi_by_sportsbook`
- `analytics.v_roi_by_sport`
- `analytics.v_roi_by_team`
- `analytics.v_roi_by_player`
- `analytics.v_roi_by_wager_type`
- `analytics.v_roi_by_odds_band`
- `analytics.v_promotion_performance`
- `analytics.v_open_bets`
- `analytics.v_monthly_performance`

For expensive recurring work, create refreshable derived tables such as daily rollups and strategy evaluations. Record the input watermark and definition version for reproducibility. Do not depend on a traditional materialized-view design in DuckDB.

### Partitioning and retention

- Keep authoritative canonical tables in the local DuckDB database.
- Retain original source files unchanged with hashes, source metadata, and import-run linkage.
- Store high-volume raw extracts and future market snapshots as Parquet, partitioned by source system, sport/league where useful, and year/month.
- Avoid premature partitioning of personal canonical bet tables; expected volume does not justify it.
- Retain canonical bets, raw evidence, and import audit data indefinitely by default.
- Permit explicit retention policies only for high-volume raw market snapshots.
- Preserve source hashes and parser versions so every canonical record is traceable and rebuildable.

### Domain separation

- **Sportsbook data:** wager acceptance, ticket details, promotions, and settlement.
- **Sports data:** independently sourced teams, players, events, schedules, and outcomes.
- **Analytics data:** derived calculations, findings, reports, and recommendations.

Sports enrichment may add references and context; it must not rewrite sportsbook source facts.

## 5. CLI design

```text
bet
├── init
├── doctor
├── config
│   ├── show
│   ├── set
│   └── path
├── import
│   ├── <sportsbook> <file>
│   ├── detect <file>
│   ├── validate <sportsbook> <file>
│   ├── review <import-run-id>
│   ├── history
│   └── rollback <import-run-id>
├── ingest
│   ├── pdf <file>
│   ├── screenshot <file>                 # future
│   └── email <source>                    # future
├── sportsbook
│   ├── list
│   ├── status
│   ├── summary [sportsbook]
│   └── accounts
├── bets
│   ├── list
│   ├── show <bet-id>
│   ├── open
│   ├── search
│   └── correct <bet-id>
├── roi
├── sport
│   ├── list
│   └── summary [sport]
├── league
├── team
│   ├── search <query>
│   └── summary <team>
├── player
│   ├── search <query>
│   └── summary <player>
├── wager-type
├── promotion
├── strategy
│   ├── list
│   ├── create
│   ├── show <strategy>
│   ├── evaluate <strategy>
│   ├── compare
│   └── archive <strategy>
├── analyze
│   ├── summary
│   ├── trends
│   ├── opportunity
│   ├── cohort
│   └── refresh
├── lessons
│   ├── show
│   └── history
├── review
│   ├── daily
│   ├── weekly
│   ├── monthly
│   └── period
├── sports
│   ├── sync
│   ├── events
│   ├── teams
│   ├── players
│   └── resolve
├── market
│   ├── import
│   ├── lines
│   ├── movements
│   └── promotions
├── agent
│   ├── daily-review
│   ├── weekly-review
│   ├── monthly-review
│   ├── opportunity-analysis
│   ├── lessons-learned
│   ├── runs
│   └── show <run-id>
├── report
│   ├── export
│   └── list
└── watch                                  # future inbox workflow
```

`import` is reserved for trusted sportsbook sources. `ingest` is for lower-confidence document channels. `strategy` manages saved intent; `analyze` executes transparent analytical work; `review` and `lessons` provide curated output; `agent` adds audited orchestration.

Relevant commands should consistently support `--db`, `--format table|json|csv`, `--since`, `--until`, `--sport`, `--sportsbook`, `--include-void`, and `--verbose` options.

## 6. Importer framework design

### Contract

Each sportsbook profile is a focused, versioned adapter over a shared pipeline:

```text
Source reader
  -> format extractor
  -> sportsbook field mapper
  -> canonical candidate builder
  -> validator
  -> duplicate matcher
  -> commit and reconciliation reporter
```

A profile declares its supported formats, detection fingerprints, version, column/document mappings, outcome mappings, wager-type mappings, validation rules, and conformance fixtures.

### Import stages

1. Discover file type, encoding, header, and likely profile.
2. Capture and hash the original file; create an import run.
3. Extract records from CSV/XLSX directly or from PDF text/tables with confidence scores.
4. Normalize source values into typed canonical candidates.
5. Validate model shape, dates, money, taxonomy mappings, and ticket/leg consistency.
6. Resolve known accounts and optional sports identities.
7. Deduplicate records.
8. Commit valid records atomically and quarantine invalid/review-required records.
9. Produce a reconciliation report with imported, duplicate, unresolved, warning, and rejected counts.

### Normalization rules

Preserve both source truth and canonical interpretation:

- Keep original source labels and values with raw-record lineage.
- Convert American/fractional/decimal odds to normalized decimal odds while retaining original odds format.
- Map operator outcomes and statuses to controlled values.
- Retain unresolved team/player/event labels until confidently mapped.
- Represent each ticket as a `Bet` and each selection as a `BetLeg`.
- Model promotions as structured economics, not unstructured text.

### Validation and error handling

- **Blocking:** malformed monetary/date values, impossible ticket structures, missing required source identity.
- **Warning:** unknown market types, unresolved entities, unexpected status, or nonstandard odds.
- **Review required:** likely duplicate conflict or low-confidence document extraction.
- **Accepted:** valid normalized record with full provenance.

Source files and raw records are never discarded because parsing fails. Commits must be transactional. CLI output must provide actionable remediation plus machine-readable output.

### Deduplication

Use layers in order:

1. Exact match: sportsbook, account, and external ticket ID.
2. Raw source record/content hash.
3. Deterministic fallback: sportsbook, placed time, stake, odds, and normalized leg fingerprint.
4. Similarity-scored probable duplicate requiring review.

Do not deduplicate only by team, stake, and date; identical legitimate wagers may occur.

### Sportsbook coverage

FanDuel, DraftKings, BetMGM, Fanatics, Caesars, Bally Bet, and theScore are separate profile implementations sharing the common contract. New providers require representative redacted samples, mapping definitions, fixtures for normal/parlay/promotion/pending/cancelled/correction scenarios, and shared conformance testing.

## 7. Analytics engine design

### Question model

The engine answers:

- **What works?** Ranked cohorts and strategy metrics.
- **What does not work?** Negative ROI, deteriorating, and costly patterns.
- **Why?** Transparent filters, data period, financial totals, sample size, stability, and relevant context.

Correlation is not causation. Every finding must disclose evidence and caveats.

### Metrics

For each cohort, calculate:

- number of bets and settled bets;
- total risk/stake, total return, and net profit/loss;
- cash ROI and economic ROI;
- win/loss/push/void rates;
- mean and median stake/odds;
- promotion impact;
- period-over-period change;
- optional confidence interval or uncertainty label.

Weighted ROI (`sum(profit) / sum(stake)`) is the primary ROI. Never average individual-bet ROI values without weighting.

### Strategy analysis

A strategy is a versioned declarative cohort rule, for example: MLB moneyline favorites, Red Sox home games with no parlays, or NFL player props in a stated odds range. Evaluations store the rule version, data period, query definition, input watermark, metrics, and evidence. Retrospective counterfactuals must be clearly marked as historical analysis.

### Trend and recommendation design

Compare weekly, monthly, seasonal, and lifetime windows. Measure performance changes by sport, wager type, odds band, sportsbook, team, player, and promotion.

Findings require configurable minimum settled-bet count, minimum risk, and stability across time windows. Small samples remain visible but are labeled exploratory.

Recommendations are deterministic evidence objects before any LLM narration:

- observation;
- exact cohort and period;
- metrics and sample size;
- confidence/stability labels;
- bounded interpretation;
- suggested review action;
- caveats.

BET must not claim guaranteed edges, automate wagers, or prescribe stake sizes absent an explicit user-defined bankroll policy.

## 8. Sports data architecture

Sports data is an independent data domain for MLB, NFL, NBA, WNBA, and MLS. It must never prevent a sportsbook import from being useful.

### Data scope

- **Teams:** canonical identity, abbreviations, aliases, league membership, active period.
- **Players:** canonical identity, aliases, positions, league eligibility, and team tenure.
- **Events:** schedule, status, participants, venue, result, and neutral-site context.
- **Context:** standings, records, home/away splits, streaks, starters, starting pitchers, lineups, injuries, and provider timestamps.

### Enrichment flow

```text
Sports provider data
  -> raw capture
  -> canonical sports normalization
  -> identity alias mapping
  -> event matching
  -> versioned enrichment facts
  -> analytics joins to BetLeg
```

Prefer stable provider IDs. Fallback event matching uses league, schedule tolerance, teams, season, and a confidence score with a review path. Preserve facts as known at the time when historical context is required.

## 9. Agent framework design

Agents are reporting/workflow orchestrators over trusted, read-only analytical services. They cannot place bets, log into sportsbooks, alter accounts, or access hidden network services.

### Shared architecture

1. Typed workflow request and time window.
2. Deterministic data-gathering plan.
3. Read-only tools returning bounded structured results.
4. Deterministic metrics and evidence assembly.
5. Optional LLM narration using supplied evidence only.
6. Validation that every narrative claim references an evidence item.
7. Persisted agent run with template version, tool inputs/results, artifact, and status.

### Workflows

| Workflow | Inputs | Output |
|---|---|---|
| Daily Review | Open bets, latest settlements, optional market context, historical cohorts | Operational review and comparisons |
| Weekly Review | Seven days plus rolling baselines | ROI, volume, behavior changes, emerging trends |
| Monthly Review | Month plus historical baseline | Best/worst strategies, significant wins/losses, review actions |
| Opportunity Analysis | Structured candidate plus optional market data | Similar historical cohorts and caveats, not a prediction |
| Lessons Learned | Settled history or defined period | Durable positive/negative findings ranked by evidence |

Initial releases should generate deterministic reports. LLM output is optional narrative, never the factual source of truth.

## 10. Milestone plan

| Milestone | Objective | Deliverables | Dependencies | Complexity | Primary risks |
|---|---|---|---|---|---|
| M0: Architecture decisions | Freeze definitions and acceptance criteria | Data dictionary, settlement rules, source sample inventory, privacy policy, MVP tests | None | Medium | Ambiguous data semantics |
| M0.5: Multi-user foundation | Make ownership, consent, and service boundaries first class | User/account model, authorization matrix, sharing and consent policy, cohort privacy rules | M0 | Medium | Retrofitting privacy later |
| M1: Foundation | Establish maintainable local baseline | uv project, typed config, CLI conventions, logging, migration/test harness | M0, M0.5 | Low | Premature abstractions |
| M2: Warehouse core | Build traceable canonical persistence | DuckDB schemas, models, repositories, audit tables | M1 | Medium | Incorrect ticket/leg modeling |
| M3: Import platform | Build generic ingestion system | Capture, extraction, normalization, validation, dedupe, reconciliation | M2 | High | Export variation |
| M4: Sportsbook coverage | Support all documented providers/formats | Seven profile implementations and fixtures | M3, source samples | High | PDF quality and unavailable exports |
| M5: Baseline analytics | Answer core historical questions | ROI views and command reports | M2, useful imported data | Medium | Financial calculation errors |
| M6: Post-bet intelligence | Turn analysis into learning | Strategies, trends, findings, lessons, reviews | M5 | Medium-High | False discovery |
| M7: Sports platform | Add independent enrichment | Sports schemas, provider adapters, resolution, enrichment | M2 | High | Entity matching/provider terms |
| M8: Market intelligence | Add market context | Line/promotion snapshots and comparisons | M3, M7, provider decision | High | Data rights and volume |
| M9: Agent workflows | Add audited decision support | Read-only tools, audit trail, deterministic/optional LLM workflows | M6; M8 for market-aware work | Medium | Unsupported claims/privacy |
| M10: Hardening | Prepare for reliable personal use/service evolution | Backup/restore, quality monitoring, documentation, release process | Relevant prior milestones | Medium | Long-term migrations |

### Parallel work

- M1 can begin while M0/M0.5 decisions are finalized.
- Warehouse models and database migration design can progress together after the data dictionary is approved.
- Individual sportsbook profiles can be developed in parallel after the importer contract is stable.
- Baseline analytics can begin with fixtures while profile coverage is completed.
- Sports data can progress separately from post-bet intelligence after core identity rules exist.
- Agent infrastructure can begin after post-bet intelligence, while market-aware opportunity workflows wait for market data.

## 11. Task hierarchy for coding agents

```text
Epic: Product and architecture decisions
  Feature: Data governance
    Task: Define financial, promotion, and settlement semantics
    Task: Define currency, timezone, and rounding policy
    Task: Define correction, provenance, retention, privacy, export, and deletion policy
    Task: Approve MVP acceptance criteria
  Feature: Multi-user policy
    Task: Define tenant, user, and sportsbook-account ownership rules
    Task: Define authorization matrix and sharing-policy states
    Task: Define consent lifecycle, revocation, and audit requirements
    Task: Define aggregate cohort privacy thresholds
  Feature: Source discovery
    Task: Collect redacted exports for every sportsbook and format
    Task: Catalog region, export versions, headers, and limitations
    Task: Classify PDF sample/extraction expectations
  Dependencies:
    Data governance + multi-user policy -> warehouse, importers, analytics
    Source discovery -> sportsbook profiles

Epic: Application foundation
  Feature: Tooling and configuration
    Task: Create Python 3.13 uv project configuration
    Task: Configure typing, testing, logging, and local data paths
  Feature: CLI framework
    Task: Implement root CLI and common output/error conventions
    Task: Implement config and doctor commands
  Dependencies:
    Governance decisions -> configuration defaults

Epic: Canonical warehouse
  Feature: Database lifecycle
    Task: Define DuckDB migration strategy
    Task: Create control, raw, core, reference, and analytics namespaces
    Task: Implement backup, restore, and health checks
  Feature: Domain models
    Task: Model tenants, users, sharing, consent, sportsbooks, and accounts
    Task: Model bets, legs, promotions, and settlements
    Task: Model imports, raw records, issues, aliases, and corrections
  Feature: Persistence
    Task: Implement user-scoped repositories and provenance history
    Task: Add migration and repository tests
  Dependencies:
    Database lifecycle + domain models -> persistence

Epic: Import platform
  Feature: Shared pipeline
    Task: Implement source capture and hashing
    Task: Implement CSV/XLSX readers and PDF extraction pipeline
    Task: Define versioned importer profile contract
    Task: Implement normalization candidates, validation, and review queue
    Task: Implement layered duplicate matching
    Task: Implement transactional commit and reconciliation report
  Feature: Import CLI
    Task: Implement import, detect, validate, review, history, and rollback commands
  Feature: Quality suite
    Task: Create profile conformance fixtures
    Task: Test idempotence, reconciliation, failure, and rollback paths
  Dependencies:
    Warehouse -> shared pipeline
    Shared pipeline -> import CLI and provider profiles

Epic: Sportsbook importer coverage
  Feature: FanDuel
    Task: Implement mappings and fixtures
    Task: Support defined CSV/XLSX/PDF sources
    Task: Pass conformance suite
  Feature: DraftKings
    Task: Implement mappings and fixtures
    Task: Support defined CSV/XLSX/PDF sources
    Task: Pass conformance suite
  Feature: BetMGM
    Task: Implement mappings and fixtures
    Task: Support defined CSV/XLSX/PDF sources
    Task: Pass conformance suite
  Feature: Fanatics
    Task: Implement mappings and fixtures
    Task: Support defined CSV/XLSX/PDF sources
    Task: Pass conformance suite
  Feature: Caesars
    Task: Implement mappings and fixtures
    Task: Support defined CSV/XLSX/PDF sources
    Task: Pass conformance suite
  Feature: Bally Bet
    Task: Implement mappings and fixtures
    Task: Support defined CSV/XLSX/PDF sources
    Task: Pass conformance suite
  Feature: theScore Bet
    Task: Implement mappings and fixtures
    Task: Support defined CSV/XLSX/PDF sources
    Task: Pass conformance suite
  Dependencies:
    Shared importer contract + source samples -> all provider features
  Parallelism:
    Provider features can proceed independently after contract stabilization.

Epic: Baseline analytics
  Feature: Financial metrics
    Task: Implement settled-bet financial view
    Task: Implement cash and economic ROI
    Task: Validate metrics against hand-calculated fixtures
  Feature: Dimensions and reporting
    Task: Build sportsbook, sport, wager-type, odds-band, and promotion views
    Task: Implement roi, sportsbook, sport, team, player, and wager-type commands
  Dependencies:
    Canonical warehouse + imported data -> analytics

Epic: Post-bet intelligence
  Feature: Strategies
    Task: Define declarative strategy-rule grammar
    Task: Implement strategy versioning and evaluation
  Feature: Findings and reviews
    Task: Define sample-size and uncertainty policy
    Task: Implement trend/stability calculations and evidence-backed findings
    Task: Implement lessons, weekly review, and monthly review reports
  Dependencies:
    Baseline analytics -> post-bet intelligence

Epic: Sports data platform
  Feature: Canonical sports data
    Task: Model leagues, teams, players, events, participants, and results
    Task: Implement provider provenance and identity resolution
  Feature: League adapters
    Task: Implement MLB adapter
    Task: Implement NFL adapter
    Task: Implement NBA adapter
    Task: Implement WNBA adapter
    Task: Implement MLS adapter
  Feature: Enrichment
    Task: Implement event matching, review, and bet-leg enrichment views
  Dependencies:
    Sports identity model -> league adapters
    League adapters + bets -> enrichment

Epic: Market intelligence
  Feature: Collection
    Task: Select permitted sources and retention policy
    Task: Model markets, line snapshots, boosts, offers, and collection audits
  Feature: Analytics
    Task: Implement line movement and market comparison analysis
    Task: Implement historical opportunity comparison
  Dependencies:
    Provider decision + sports identities -> collection
    Collection + baseline analytics -> market analysis

Epic: Agent workflows
  Feature: Agent platform
    Task: Define typed read-only tool contracts
    Task: Implement agent-run audit artifacts and claim/evidence validation
    Task: Define optional LLM privacy/provider configuration
  Feature: Workflows
    Task: Implement deterministic daily, weekly, monthly, lessons, and opportunity workflows
  Dependencies:
    Post-bet intelligence -> reviews and lessons
    Market intelligence -> market-aware daily/opportunity workflows

Epic: Production readiness
  Feature: Reliability
    Task: Add end-to-end import-to-report tests
    Task: Add reconciliation/data-quality monitoring and backup/restore tests
  Feature: Documentation
    Task: Document supported formats, definitions, caveats, and troubleshooting
  Dependencies:
    Each completed feature -> its quality and documentation work
```

## 12. Approval gates before implementation

Implementation should not start until these are approved:

1. Canonical financial and settlement definitions.
2. Multi-user ownership, consent, sharing, and privacy policy.
3. Representative sportsbook export samples and supported-format boundaries.
4. Statistical thresholds and recommendation language policy.
5. The local-first to hosted-service transition boundary.

