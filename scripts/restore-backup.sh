#!/usr/bin/env bash
# Restore the BET warehouse on the mini from a backup (SB-1404). Run ON the
# mini. See k3s/bet-api/RESTORE.md for when and why.
#
#   ./scripts/restore-backup.sh --list                       # backups on the volume
#   ./scripts/restore-backup.sh bet-20261025T073000Z.duckdb   # one already on the volume
#   ./scripts/restore-backup.sh --file ~/Downloads/bet-....duckdb  # one from this Mac
#
# Steps: scale the API to zero (it is the only process allowed to hold the
# file), start a helper pod on the warehouse volume, move the live bet.duckdb
# aside as bet.duckdb.pre-restore-<stamp> (never deleted), put the backup in
# its place, run `bet doctor` against it, then bring the API back and wait for
# /readyz. If doctor fails, the previous file is put back before the API
# starts.
#
# bet-guard: synthetic-amounts -- the "$" here is shell expansion, not money.
set -euo pipefail

CONTEXT=rancher-desktop
NAMESPACE=bet
HELPER=bet-restore
KUBECTL=(kubectl --context "$CONTEXT" -n "$NAMESPACE")
PATTERN='^bet-[0-9]{8}T[0-9]{6}Z\.duckdb$'

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

host=$(tailscale status --json 2>/dev/null |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].split(".")[0])' ||
  true)
[ "$host" = toms-mac-mini ] || die "this is '${host:-unknown}', not toms-mac-mini. Run this on the mini."

image=$("${KUBECTL[@]}" get deployment bet-api -o jsonpath='{.spec.template.spec.containers[0].image}')

helper_up() {
  "${KUBECTL[@]}" delete pod "$HELPER" --ignore-not-found --wait >/dev/null
  "${KUBECTL[@]}" run "$HELPER" --image="$image" --restart=Never \
    --overrides='{
      "spec": {
        "enableServiceLinks": false,
        "securityContext": {"runAsNonRoot": true, "runAsUser": 10001,
                            "runAsGroup": 10001, "fsGroup": 10001},
        "containers": [{
          "name": "restore", "image": "'"$image"'",
          "command": ["sleep", "3600"],
          "env": [{"name": "TZ", "value": "America/New_York"}],
          "volumeMounts": [{"name": "data", "mountPath": "/data"}]
        }],
        "volumes": [{"name": "data",
                     "persistentVolumeClaim": {"claimName": "bet-data"}}]
      }
    }' >/dev/null
  "${KUBECTL[@]}" wait --for=condition=Ready "pod/$HELPER" --timeout=120s >/dev/null
}
helper() { "${KUBECTL[@]}" exec -i "$HELPER" -- "$@"; }
helper_down() { "${KUBECTL[@]}" delete pod "$HELPER" --ignore-not-found --wait=false >/dev/null; }

api_up() {
  "${KUBECTL[@]}" scale deployment/bet-api --replicas=1 >/dev/null
  "${KUBECTL[@]}" rollout status deployment/bet-api --timeout=180s
}

case "${1:-}" in
  --list)
    "${KUBECTL[@]}" exec deploy/bet-api -c api -- ls -l /data/backups
    exit 0 ;;
  --file)
    [ -f "${2:-}" ] || die "--file needs a path to a backup file on this Mac"
    src_local=$2; name=${src_local##*/} ;;
  "" | -h | --help)
    sed -n '2,15p' "$0"; exit 0 ;;
  *)
    src_local=""; name=$1 ;;
esac
[[ $name =~ $PATTERN ]] || die "'$name' is not a backup file name (bet-YYYYMMDDTHHMMSSZ.duckdb)"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
trap 'helper_down' EXIT

log "scaling bet-api to 0"
"${KUBECTL[@]}" scale deployment/bet-api --replicas=0 >/dev/null
"${KUBECTL[@]}" wait --for=delete pod -l app=bet,component=api --timeout=120s >/dev/null 2>&1 || true

log "starting helper pod on the warehouse volume"
helper_up

if [ -n "$src_local" ]; then
  log "copying $name from this Mac into /data/backups"
  helper sh -c "cat > /data/backups/.$name.partial" <"$src_local"
  want=$(shasum -a 256 "$src_local" | awk '{print $1}')
  got=$(helper sha256sum "/data/backups/.$name.partial" | awk '{print $1}')
  [ "$want" = "$got" ] || { helper rm -f "/data/backups/.$name.partial"; api_up; die "upload checksum mismatch"; }
  helper mv "/data/backups/.$name.partial" "/data/backups/$name"
fi
helper test -f "/data/backups/$name" || { api_up; die "no /data/backups/$name on the volume"; }

log "moving the live warehouse aside as bet.duckdb.pre-restore-$stamp"
helper sh -c "test ! -e /data/bet.duckdb || mv /data/bet.duckdb /data/bet.duckdb.pre-restore-$stamp"
helper sh -c "rm -f /data/bet.duckdb.wal"
helper cp "/data/backups/$name" /data/bet.duckdb

log "bet doctor against the restored file"
if ! helper bet doctor; then
  log "doctor failed: putting the previous warehouse back"
  helper sh -c "rm -f /data/bet.duckdb && { test ! -e /data/bet.duckdb.pre-restore-$stamp || mv /data/bet.duckdb.pre-restore-$stamp /data/bet.duckdb; }"
  helper_down
  api_up
  die "restore of $name failed doctor; previous warehouse restored"
fi

helper_down
log "bringing bet-api back"
api_up
for _ in $(seq 1 20); do
  curl -sf http://127.0.0.1:8787/readyz >/dev/null && break
  sleep 3
done
curl -sf http://127.0.0.1:8787/readyz >/dev/null || die "bet-api is not ready after restore"
log "restored $name; previous file kept as /data/bet.duckdb.pre-restore-$stamp"
