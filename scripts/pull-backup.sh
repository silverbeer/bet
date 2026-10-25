#!/usr/bin/env bash
# Pull the newest nightly backup off the mini (SB-1404). Runs on the MacBook
# Air from launchd (deploy/io.silverbeer.bet.backup-pull.plist), hourly; a run
# that already has the newest file does nothing, so a night the Air slept
# through is caught up the next time it is awake.
#
# The file comes over the existing SSH trust (`ssh mini`) via `kubectl exec
# cat` from the API pod's volume -- no API token leaves the mini. The copy is
# checked against the pod's sha256 before it replaces anything, and kept for
# KEEP_DAYS in DEST (iCloud by default, so it is off both machines).
#
#   scripts/pull-backup.sh            # pull if newer, prune
#   scripts/pull-backup.sh --check    # print newest remote and local, change nothing
#
# bet-guard: synthetic-amounts -- the "$" here is shell expansion, not money.
set -euo pipefail

DEST=${BET_BACKUP_PULL_DIR:-$HOME/iCloud/bet/backups-mini}
KEEP_DAYS=${BET_BACKUP_PULL_KEEP_DAYS:-30}
SSH_HOST=${BET_BACKUP_PULL_HOST:-mini}
REMOTE_DIR=/data/backups
PATTERN='bet-[0-9]{8}T[0-9]{6}Z\.duckdb'

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*"; }

# Run one command in the API pod, through the mini's login shell (for PATH).
in_pod() {
  # shellcheck disable=SC2029  # expanded here on purpose: fixed, quoted args
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" \
    "zsh -lc 'kubectl --context rancher-desktop -n bet exec deploy/bet-api -c api -- $*'"
}

latest=$(in_pod ls -1 "$REMOTE_DIR" | grep -E "^${PATTERN}$" | sort | tail -n 1 || true)
[ -n "$latest" ] || { log "no backups on the mini yet"; exit 0; }

mkdir -p "$DEST"
newest_local=$(find "$DEST" -maxdepth 1 -type f -print 2>/dev/null |
  sed 's|.*/||' | grep -E "^${PATTERN}$" | sort | tail -n 1 || true)

if [ "${1:-}" = "--check" ]; then
  echo "remote newest: $latest"
  echo "local newest:  ${newest_local:-none} ($DEST)"
  exit 0
fi

if [ -e "$DEST/$latest" ]; then
  log "up to date ($latest)"
else
  remote_sha=$(in_pod sha256sum "$REMOTE_DIR/$latest" | awk '{print $1}')
  tmp="$DEST/.$latest.partial"
  trap 'rm -f "$tmp"' EXIT
  in_pod cat "$REMOTE_DIR/$latest" >"$tmp"
  local_sha=$(shasum -a 256 "$tmp" | awk '{print $1}')
  if [ "$local_sha" != "$remote_sha" ]; then
    log "checksum mismatch for $latest (remote ${remote_sha:0:12}, local ${local_sha:0:12}); not kept"
    exit 1
  fi
  mv "$tmp" "$DEST/$latest"
  trap - EXIT
  log "pulled $latest ($(wc -c <"$DEST/$latest" | tr -d ' ') bytes, sha256 ${local_sha:0:12})"
fi

# Retention: only files named like a backup, and never the newest one.
find "$DEST" -maxdepth 1 -type f -mtime +"$KEEP_DAYS" -print | while read -r f; do
  name=${f##*/}
  if [[ $name =~ ^${PATTERN}$ ]] && [ "$name" != "$latest" ]; then
    rm -f -- "$f" && log "pruned $name"
  fi
done
