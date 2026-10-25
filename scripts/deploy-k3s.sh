#!/usr/bin/env bash
# Deploy the BET API to k3s on the mac mini (SB-1403). Run it ON the mini,
# from a checkout of this repository:
#
#   ./scripts/deploy-k3s.sh            # apply manifests, wait for the rollout,
#                                      # (re)load the loopback port-forward agent
#   ./scripts/deploy-k3s.sh --serve    # ... and (re)publish it with tailscale serve
#
# The image tag is whatever k3s/bet-api/deployment.yaml says (CI bumps it on
# every merge to main), so `git pull && ./scripts/deploy-k3s.sh` deploys main.
#
# Refuses to run anywhere but the mini: the MacBook Air has a rancher-desktop
# context too, and deploying there would start a second, empty warehouse.
# bet-guard: synthetic-amounts -- the "$" here is shell expansion, not money.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
MANIFESTS="$HERE/k3s/bet-api"
NAMESPACE=bet
CONTEXT=rancher-desktop
EXPECTED_HOST=toms-mac-mini
LOCAL_PORT=8787
SERVE=0

for arg in "$@"; do
  case "$arg" in
    --serve) SERVE=1 ;;
    -h | --help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 64 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

# --- guard: right machine, right cluster -------------------------------------
host=$(tailscale status --json 2>/dev/null |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].split(".")[0])' ||
  true)
[ "$host" = "$EXPECTED_HOST" ] ||
  die "this is '${host:-unknown}', not $EXPECTED_HOST. Run this on the mini."

KUBECTL=(kubectl --context "$CONTEXT")
"${KUBECTL[@]}" get nodes >/dev/null || die "cannot reach the $CONTEXT cluster."

# --- manifests -----------------------------------------------------------------
"${KUBECTL[@]}" apply -f "$MANIFESTS/namespace.yaml"

# Secrets are created once and never rotated here: a new JWT key signs out
# every session, and the maintenance token is shared with the backup CronJob.
# Each value goes from openssl straight into its Secret, never to the terminal
# or a file.
ensure_secret() {
  local name=$1 key=$2
  if "${KUBECTL[@]}" -n "$NAMESPACE" get secret "$name" >/dev/null 2>&1; then
    echo "secret/$name unchanged (exists)"
  else
    openssl rand -hex 32 | tr -d '\n' |
      "${KUBECTL[@]}" -n "$NAMESPACE" create secret generic "$name" \
        --from-file="$key=/dev/stdin" >/dev/null
    echo "secret/$name created"
  fi
}
ensure_secret bet-api-jwt BET_JWT_SIGNING_KEY
ensure_secret bet-api-maintenance BET_MAINTENANCE_TOKEN

"${KUBECTL[@]}" apply -f "$MANIFESTS/pvc.yaml" -f "$MANIFESTS/service.yaml" \
  -f "$MANIFESTS/deployment.yaml" -f "$MANIFESTS/backup-cronjob.yaml"
"${KUBECTL[@]}" -n "$NAMESPACE" rollout status deployment/bet-api --timeout=180s

image=$("${KUBECTL[@]}" -n "$NAMESPACE" get deployment bet-api \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "deployed $image"

# --- loopback port-forward (launchd) ----------------------------------------------
# The Service is ClusterIP; this agent is what puts it on 127.0.0.1:8787.
# Reinstalled every deploy so the plist always matches this checkout, and
# kicked so its tunnel targets the new pod rather than waiting out a miss.
LABEL=io.silverbeer.bet.portforward
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__REPO__|$HERE|g" -e "s|__HOME__|$HOME|g" \
  "$HERE/deploy/$LABEL.plist" >"$PLIST"
plutil -lint "$PLIST" >/dev/null || die "generated $PLIST is not a valid plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "launchd $LABEL (re)loaded"

# --- local reachability ---------------------------------------------------------
for _ in $(seq 1 15); do
  curl -sf "http://127.0.0.1:$LOCAL_PORT/readyz" >/dev/null && break
  sleep 2
done
curl -sf "http://127.0.0.1:$LOCAL_PORT/readyz" >/dev/null ||
  die "bet-api is not answering on 127.0.0.1:$LOCAL_PORT"
echo "127.0.0.1:$LOCAL_PORT/readyz ok"

# --- tailnet --------------------------------------------------------------------
if [ "$SERVE" = 1 ]; then
  tailscale serve --bg --https=443 "http://127.0.0.1:$LOCAL_PORT"
  tailscale serve status
fi
