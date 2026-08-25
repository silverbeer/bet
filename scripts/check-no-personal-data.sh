#!/usr/bin/env bash
# Guard: refuse to commit personal betting or financial data.
#
# This repository is PUBLIC. Sportsbook exports and the DuckDB warehouse contain
# a complete personal financial record. Git history is permanent and forks cannot
# be recalled, so a single accidental commit is an irreversible disclosure.
#
# Two layers:
#   1. Filename rules  — block data files by path and extension.
#   2. Content rules   — block money-shaped figures in added lines of text files.
#
# Layer 2 exists because layer 1 cannot see inside a file. Real statement totals
# were once drafted into a .md file and passed the filename check unchallenged
# (SB-815).
#
# Usage:
#   check-no-personal-data.sh                  # staged changes (pre-commit)
#   check-no-personal-data.sh FILE...          # staged changes, these paths only
#   check-no-personal-data.sh --range A..B     # a commit range (CI)
#
# Legitimate examples need amounts. Mark such a file by putting
#
#     bet-guard: synthetic-amounts
#
# anywhere in it (a comment is fine). The guard then skips its money checks and
# reports how many it skipped, so the exemption stays visible rather than silent.
#
# Install:  uv run pre-commit install     (preferred — manages the hook)
#           ln -sf ../../scripts/check-no-personal-data.sh .git/hooks/pre-commit
# Bypass:   only with --no-verify, and only if you are certain. CI re-checks.
set -euo pipefail

MARKER='bet-guard: synthetic-amounts'

range=""
paths=()
while [ $# -gt 0 ]; do
  case "$1" in
    --range) range="${2:?--range needs a git range}"; shift 2 ;;
    *)       paths+=("$1"); shift ;;
  esac
done

if [ -n "$range" ]; then
  name_cmd=(git diff --name-only --diff-filter=AM "$range" --)
  diff_cmd=(git diff -U0 --diff-filter=AM "$range" --)
else
  name_cmd=(git diff --cached --name-only --diff-filter=AM --)
  diff_cmd=(git diff --cached -U0 --diff-filter=AM --)
fi

changed="$("${name_cmd[@]}" ${paths[@]+"${paths[@]}"} 2>/dev/null || true)"
[ -z "$changed" ] && exit 0

# ---------------------------------------------------------------- layer 1
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
done <<< "$changed"

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

# ---------------------------------------------------------------- layer 2
# Money shapes in ADDED lines only. Two patterns:
#   a) an explicit currency amount            $1,234.56  $89  $0.02
#   b) a thousands-grouped decimal            3,685.16
#   c) a bare 2dp decimal on a line that also mentions a financial term
FINANCIAL_TERM='stake|staked|wager|wagered|return|returned|profit|loss|p&l|pandl|balance|handle|payout|won|win|deposit|withdraw|bankroll|awarded|played|expired'

findings=""
skipped=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  # Only text files we can reason about.
  case "$f" in
    *.md|*.py|*.sql|*.json|*.toml|*.yaml|*.yml|*.txt|*.sh|*.rst) ;;
    *) continue ;;
  esac
  case "$f" in uv.lock|*.lock) continue ;; esac

  added="$("${diff_cmd[@]}" "$f" | grep -E '^\+' | grep -Ev '^\+\+\+' | sed 's/^+//' || true)"
  [ -z "$added" ] && continue

  hits="$(printf '%s\n' "$added" | grep -nE \
      "(\\\$[0-9][0-9,]*(\.[0-9]{2})?)|([0-9]{1,3}(,[0-9]{3})+\.[0-9]{2})|(($FINANCIAL_TERM)[^0-9]{0,24}[0-9]+\.[0-9]{2})" \
      -i || true)"
  [ -z "$hits" ] && continue

  count="$(printf '%s\n' "$hits" | grep -c . || true)"

  # File-level exemption for documented synthetic examples.
  if git show ":$f" 2>/dev/null | grep -qF "$MARKER" \
     || { [ -f "$f" ] && grep -qF "$MARKER" "$f"; }; then
    skipped+="  $f — $count money figure(s) skipped (marked synthetic)"$'\n'
    continue
  fi

  findings+="  $f"$'\n'
  findings+="$(printf '%s\n' "$hits" | head -8 | sed 's/^/      /')"$'\n'
done <<< "$changed"

if [ -n "$skipped" ]; then
  printf '\n  bet-guard: synthetic amounts allowed\n%s\n' "$skipped" >&2
fi

if [ -n "$findings" ]; then
  cat >&2 <<MSG

  COMMIT BLOCKED — added lines contain money-shaped figures.

$findings
  This repository is public. README guarantees it holds no real balances or
  stakes, and git history cannot be rewritten once pushed.

  If these are real figures, remove them. Keep real reconciliations in Linear
  or the private capture repo.

  If they are illustrative, put this marker in the file and re-stage it:

      $MARKER

MSG
  exit 1
fi

exit 0
