# BET

**Personal betting intelligence platform.**

> Transform betting history into betting intelligence.

BET is a local-first, CLI-first system that answers a single question: *which bets
actually work for me?* It aggregates wagering history across sportsbooks,
normalizes it into a common model, and measures which teams, sports, wager types,
odds ranges and promotions have actually generated positive ROI.

BET is **not** a sportsbook, **not** an automated betting bot, and **not** a
prediction engine. It analyzes what already happened.

## Status

Pre-implementation. The repository currently contains planning documents only.

- [`docs/PRD.md`](docs/PRD.md) — product vision and the questions BET must answer
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — technical architecture
- [`docs/BET_IMPLEMENTATION_PLAN.md`](docs/BET_IMPLEMENTATION_PLAN.md) — domain model, milestones, task breakdown
- [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) — the money model: settlement vocabulary, promotion economics, ROI formulas
- [`docs/OWNERSHIP.md`](docs/OWNERSHIP.md) — tenant/user column conventions and the scoped-repository rule
- [`docs/decisions/`](docs/decisions/) — architecture decision records

## License

Copyright (c) 2026 Tom Drake. All rights reserved.

Released under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).

**You may** read, run, modify and share this software for any noncommercial
purpose — personal study, research, hobby projects, and use by charitable,
educational, public research and government organizations.

**You may not** use this software, or anything derived from it, for a commercial
purpose. That includes offering it as a product or service, using it inside a
business, or building a commercial betting or analytics product on top of it.

This is a source-available license, not an open-source license. For commercial
licensing, contact the copyright holder.

## Data locations

This repository is public. It contains **no betting data and never will.**

**All data lives outside the repository.** This is structural, not a convention —
there is no `data/` directory inside the working tree and nothing in the codebase
may create one. Sportsbook exports, the DuckDB warehouse and the source-file
archive hold a complete record of personal financial activity, and the only
reliable way to keep them out of git is for them not to be inside git.

Default locations (XDG, overridable):

| Contents | Default path |
|---|---|
| DuckDB warehouse | `$XDG_DATA_HOME/bet/bet.duckdb` (`~/.local/share/bet/bet.duckdb`) |
| Source-file archive (imported exports, unchanged + hashed) | `$XDG_DATA_HOME/bet/sources/` |
| Backups | `$XDG_DATA_HOME/bet/backups/` |
| Config | `$XDG_CONFIG_HOME/bet/config.toml` (`~/.config/bet/config.toml`) |
| Logs | `$XDG_STATE_HOME/bet/` |

Enforcement, in layers:

1. **Config validation** rejects any `data_dir` that resolves inside a git work
   tree, and `bet init` refuses to initialize there.
2. **`.gitignore`** covers `data/`, `exports/`, `samples/`, and every database,
   spreadsheet, statement and archive extension.
3. **A pre-commit guard** (`scripts/check-no-personal-data.sh`) blocks the commit
   outright if any of it is ever staged.

The single exception is `tests/fixtures/` — redacted samples only, with no real
account identifiers, balances or stakes. Redaction is a review requirement, not
a best effort.

If you fork this project, the same applies to you. Git history is permanent and
forks cannot be recalled.
