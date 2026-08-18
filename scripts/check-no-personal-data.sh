#!/usr/bin/env bash
# Pre-commit guard: refuse to commit personal betting or financial data.
#
# This repository is PUBLIC. Sportsbook exports and the DuckDB warehouse contain
# a complete personal financial record. Git history is permanent and forks cannot
# be recalled, so a single accidental commit is an irreversible disclosure.
#
# Install:  ln -sf ../../scripts/check-no-personal-data.sh .git/hooks/pre-commit
# Bypass:   only with --no-verify, and only if you are certain.
set -euo pipefail

staged="$(git diff --cached --name-only --diff-filter=AM)"
[ -z "$staged" ] && exit 0

blocked=""
while IFS= read -r f; do
  [ -z "$f" ] && continue

  # Redacted test fixtures are the one permitted home for tabular data.
  case "$f" in tests/fixtures/*) continue ;; esac

  case "$f" in
    data/*|exports/*|samples/*) blocked+="$f  (personal data directory)"$'\n' ;;
    *.duckdb|*.duckdb.wal|*.db|*.sqlite|*.sqlite3) blocked+="$f  (database file)"$'\n' ;;
    *.csv|*.xlsx|*.xls|*.parquet) blocked+="$f  (tabular data outside tests/fixtures/)"$'\n' ;;
    *.pdf|*.ofx|*.qfx) blocked+="$f  (statement/export file)"$'\n' ;;
    .env|.env.*|*.pem|*.key|config.toml|secrets.toml) blocked+="$f  (secret or local config)"$'\n' ;;
  esac
done <<< "$staged"

if [ -n "$blocked" ]; then
  cat >&2 <<MSG

  COMMIT BLOCKED — staged files look like personal data or secrets.

$blocked
  This repository is public. Committing any of the above discloses it
  permanently: git history retains it and existing forks cannot be recalled.

  If a file is a genuinely redacted test fixture, move it under
  tests/fixtures/ and stage it from there.

MSG
  exit 1
fi
exit 0
