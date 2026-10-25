#!/usr/bin/env bash
# Keep 127.0.0.1:8787 on the mini forwarded to svc/bet-api (SB-1403).
#
# Run by launchd (deploy/io.silverbeer.bet.portforward.plist), never by hand.
# `kubectl port-forward` is a client tunnel to one pod: when that pod is
# replaced (every deploy, Recreate) the tunnel can hang instead of exiting, so
# this loop watches /healthz through it and restarts it after three misses.
# Binding 127.0.0.1 keeps the API off the LAN; `tailscale serve` is the only
# way in from outside.
set -u

PORT=${BET_PORTFORWARD_PORT:-8787}
CONTEXT=rancher-desktop
NAMESPACE=bet

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*"; }

while true; do
  kubectl --context "$CONTEXT" -n "$NAMESPACE" port-forward \
    --address 127.0.0.1 svc/bet-api "$PORT:8787" &
  pf=$!
  log "port-forward started (pid $pf)"
  sleep 3
  misses=0
  while kill -0 "$pf" 2>/dev/null; do
    if curl -sf -m 5 "http://127.0.0.1:$PORT/healthz" >/dev/null; then
      misses=0
    else
      misses=$((misses + 1))
    fi
    if [ "$misses" -ge 3 ]; then
      log "healthz failed 3 times; restarting port-forward"
      kill "$pf" 2>/dev/null
      break
    fi
    sleep 10
  done
  wait "$pf" 2>/dev/null
  # A scaled-to-zero or crashing API would otherwise spin this loop.
  sleep 5
done
