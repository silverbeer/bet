#!/usr/bin/env bash
# Install (or refresh) the hourly backup puller on THIS Mac -- the MacBook
# Air (SB-1404). Idempotent.
# bet-guard: synthetic-amounts -- the "$" here is shell expansion, not money.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
LABEL=io.silverbeer.bet.backup-pull
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__REPO__|$HERE|g" -e "s|__HOME__|$HOME|g" "$HERE/deploy/$LABEL.plist" >"$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "launchd $LABEL loaded; log: ~/Library/Logs/bet-backup-pull.log"
