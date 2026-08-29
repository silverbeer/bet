-- Namespaces, per BET_IMPLEMENTATION_PLAN.md section 4.
--
-- control    migrations, import runs, source files, parser versions,
--            validation issues, reconciliation results
-- raw        immutable imported source rows and extraction candidates
-- core       users, accounts, bets, legs, promotions, identities, aliases,
--            corrections
-- reference  controlled taxonomies and mapping values
-- analytics  derived views, rollups, strategy evaluations, findings
--
-- sports, market and agent are deliberately absent: they belong to their own
-- epics and creating them now would imply a schema nobody has designed yet.
--
-- control already exists — the migration runner bootstraps it before it can
-- record anything — so this is written to be safe either way.

CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS reference;
CREATE SCHEMA IF NOT EXISTS analytics;
