# BET
## Personal Betting Intelligence Platform

Status: Draft v1

---

# Vision

BET is a full-featured betting intelligence platform delivered through a powerful CLI experience.

By combining:

- Complete betting history
- Real-time sportsbook lines
- Sportsbook boosts and promotions
- Team statistics
- Player statistics
- Historical betting performance
- Market analysis

BET puts everything needed to make informed betting decisions at the user's fingertips.

Unlike traditional bet trackers, BET is designed to learn.

Every wager, win, loss, line movement, boost, and outcome becomes part of a continuously growing knowledge base.

Agentic workflows and AI-powered skills analyze historical results, identify profitable patterns, surface potential opportunities, and help eliminate unprofitable behaviors.

BET aggregates data across all sportsbooks, enriches it with real-time sports data, evaluates current betting markets, and continuously feeds results back into the system for ongoing improvement.

BET does not attempt to predict the future.

Instead, it leverages historical betting behavior, market data, and sports intelligence to identify opportunities that align with historically successful betting patterns.

Every bet becomes a learning opportunity.

Every result improves the system.

Every decision is backed by data.

## Mission

Transform betting history into betting intelligence.

---

# Executive Summary

BET is a personal betting intelligence platform.

BET is not a sportsbook.

BET is not an automated betting bot.

BET is not a prediction engine.

BET is a local-first platform designed to answer a single question:

> Which betting strategies have historically made me money?

The platform aggregates betting activity across multiple sportsbooks, normalizes betting history into a common model, analyzes outcomes, identifies profitable and unprofitable patterns, and continuously learns from results.

---

# Product Goals

## Primary Goal

Determine which betting behaviors generate positive ROI.

Examples:

- Sports
- Teams
- Players
- Wager types
- Odds ranges
- Sportsbooks
- Promotions
- Boosts
- Situational betting patterns

## Secondary Goal

Create a betting data warehouse containing the user's complete betting history.

## Long-Term Goal

Provide decision support using historical betting performance, market data, and sports intelligence.

---

# User Profile

Single-user platform.

Technical user.

Comfortable with:

- CLI workflows
- Python
- DuckDB
- AI agents
- Automation

Strong preference for:

- Local-first systems
- Automation
- Data ownership
- AI-assisted workflows

---

# Core Principles

## Local First

All data remains local.

Primary datastore:

- DuckDB

Avoid cloud dependencies in MVP.

---

## Automation First

Preferred ingestion order:

1. Sportsbook exports
2. PDF imports
3. Screenshot ingestion
4. Email ingestion
5. Manual entry

Manual entry should be considered a fallback.

---

## CLI First

CLI is the primary interface.

Future dashboards are optional.

All functionality must be available through the CLI.

---

## Learn From Results

BET is a learning system.

Every settled wager should improve future analysis.

The system should continuously identify:

- Winning patterns
- Losing patterns
- Profitable behaviors
- Unprofitable behaviors

The primary asset of BET is not the database.

The primary asset of BET is the knowledge derived from the database.

---

# Supported Sportsbooks

Initial target sportsbooks:

- FanDuel
- DraftKings
- BetMGM
- Fanatics
- Caesars
- Bally Bet
- theScore Bet

The architecture must allow additional sportsbooks to be added later.

---

# Technical Requirements

## Language

Python 3.13+

## Package Management

Use:

- uv

Required:

- pyproject.toml

Do not use:

- requirements.txt
- pip workflows

## Libraries

CLI:
- Typer

Terminal UI:
- Rich

Data Models:
- Pydantic v2

Database:
- DuckDB

Data Processing:
- Pandas
- PyArrow

Logging:
- structlog

Testing:
- pytest

---

# Project Structure

```text
bet/
├── pyproject.toml
├── src/
│   └── bet/
│       ├── cli/
│       ├── database/
│       ├── models/
│       ├── importers/
│       ├── analytics/
│       ├── reports/
│       ├── settlement/
│       ├── extraction/
│       ├── sports/
│       ├── agents/
│       └── services/
├── tests/
├── docs/
└── data/
```

---

# High Level Architecture

```text
Sportsbooks
      ↓
Importers
      ↓
Normalization
      ↓
DuckDB
      ↓
Analytics Engine
      ↓
Reports
      ↓
CLI

                ↑

Sports Data
      ↓

Teams
Players
Schedules
Results
Standings

                ↑

Agentic Workflows
```

---

# Phase 1
# Historical Data Warehouse

## Objective

Import all historical betting activity.

Historical data is the foundation of the platform.

Without historical data the platform has little value.

---

## Requirements

Support importing:

- CSV
- XLSX
- PDF

Supported sportsbooks:

- FanDuel
- DraftKings
- BetMGM
- Fanatics
- Caesars
- Bally Bet
- theScore Bet

---

## Canonical Bet Model

The system must support:

- Moneyline
- Spread
- Totals
- Parlays
- Props
- Futures

Suggested fields:

```python
sportsbook
external_id

placed_at
settled_at

sport
league

event
selection

wager_type

odds

stake

payout

profit_loss

status

result
```

---

## Import Commands

```bash
bet import fanduel history.csv

bet import draftkings bets.csv

bet import betmgm activity.csv
```

---

## Phase 1 Analytics

### ROI

```bash
bet roi
```

### Sportsbook Analysis

```bash
bet sportsbook
```

### Sport Analysis

```bash
bet sport
```

### Team Analysis

```bash
bet team BOS
```

### Wager Type Analysis

```bash
bet wager-type
```

---

# Phase 2
# Post-Bet Intelligence Engine

## Objective

Learn from historical results.

Every wager should contribute to future analysis.

---

## Required Analysis

### Winning Patterns

Examples:

- Red Sox Moneyline
- MLB Favorites
- Home Teams
- Specific Odds Ranges

### Losing Patterns

Examples:

- Parlays
- Heavy Favorites
- Specific Sports
- Specific Sportsbooks

---

## Lessons Learned Command

```bash
bet lessons
```

Example output:

```text
Top Positive Findings

1. MLB Moneyline Favorites
ROI +11%

2. Red Sox Home Games
ROI +14%

Top Negative Findings

1. Same Game Parlays
ROI -24%

2. NFL Props
ROI -18%
```

---

## Monthly Review Command

```bash
bet review
```

Should include:

- Profit/Loss
- ROI
- Win Rate
- Best Strategy
- Worst Strategy
- Largest Win
- Largest Loss
- Recommendations

---

# Phase 3
# Sports Data Platform

## Objective

Enrich betting data with sports intelligence.

---

## Initial Leagues

- MLB
- NFL
- NBA
- WNBA
- MLS

---

## Team Data

Examples:

- Team records
- Home/Away records
- Standings
- Streaks

---

## Player Data

Examples:

- MLB pitchers
- NFL quarterbacks
- NBA player stats
- Injury reports

---

## Game Data

Examples:

- Schedules
- Results
- Starting lineups
- Starting pitchers
- Venue information

---

## Purpose

Allow BET to answer:

- Which pitchers generate profitable bets?
- Which teams generate profitable bets?
- Which situations generate profitable bets?

---

# Phase 4
# Market Intelligence

## Objective

Analyze betting markets.

---

## Data Sources

Sportsbook lines

Sportsbook boosts

Promotions

Line movement

Alternate lines

---

## Questions To Answer

- Which sportsbook offers the best value?
- Which boosts are worthwhile?
- Which boosts are marketing?
- How do current opportunities compare to historical winners?

---

# Phase 5
# Agentic Workflows

## Objective

Create reusable AI-powered betting workflows.

---

## Daily Betting Review

```bash
bet agent daily-review
```

Review:

- Open bets
- Today's opportunities
- Historical comparisons

---

## Weekly Performance Review

```bash
bet agent weekly-review
```

Review:

- ROI
- Performance changes
- New trends

---

## Monthly Strategy Review

```bash
bet agent monthly-review
```

Review:

- Best strategies
- Worst strategies
- Recommended changes

---

## Opportunity Analysis

```bash
bet analyze opportunity
```

Purpose:

Compare a current betting opportunity to historically successful patterns.

---

# Future Features

## Screenshot Ingestion

```bash
bet ingest screenshot.png
```

Workflow:

Screenshot
→ OCR
→ LLM Extraction
→ Validation
→ Database

---

## Inbox Monitoring

```bash
bet watch
```

Monitor:

```text
~/bet/inbox
```

---

## Email Processing

Automatically process:

- Bet confirmations
- Settlement notifications

---

## Dashboard

Optional future capability.

Potential stack:

- FastAPI
- HTMX
- Jinja

CLI remains primary.

---

# Non-Goals

Not building:

- Automated wagering
- Sportsbook bots
- Arbitrage bots
- Real-time prediction systems
- Mobile applications

---

# Success Criteria

BET is successful when it can answer:

1. What is my lifetime ROI?
2. Which sportsbooks perform best?
3. Which sports perform best?
4. Which wager types perform best?
5. Which teams perform best?
6. Which players perform best?
7. Which strategies perform best?
8. Which strategies perform worst?
9. What should I do more of?
10. What should I stop doing?
11. How does today's opportunity compare to historical winners?
12. What has my betting history taught me?

The platform's ultimate goal is not to track bets.

The platform's ultimate goal is to learn from bets.
