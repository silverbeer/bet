# bet-api on the mac mini

The BET API (ADR 0002) runs in the mini's k3s (Rancher Desktop, arm64) and is
reachable **only over the tailnet**, at

    https://toms-mac-mini.tail279405.ts.net

| Piece | What it is |
|---|---|
| `namespace.yaml` | namespace `bet` |
| `pvc.yaml` | `bet-data`, 5Gi `local-path`: the warehouse, source archive, backups and config, all under `/data` |
| `deployment.yaml` | one replica, `Recreate`; init container `bet init` applies migrations first; probes on `/healthz` (liveness) and `/readyz` (readiness) |
| `service.yaml` | `ClusterIP` on port 8787 -- not published on the Mac by the cluster at all |
| `deploy/io.silverbeer.bet.portforward.plist` + `scripts/bet-portforward.sh` | launchd agent on the mini: `kubectl port-forward --address 127.0.0.1 svc/bet-api 8787`, restarted when `/healthz` stops answering through it |
| Secret `bet-api-jwt` | `BET_JWT_SIGNING_KEY`, created by the deploy script, never committed |

## How a request gets in

    MacBook Air --tailnet/TLS--> tailscale serve (mini, :443)
                                   --> 127.0.0.1:8787 (launchd kubectl port-forward)
                                   --> Service bet-api --> pod :8000

TLS is Tailscale's certificate for the mini's MagicDNS name. Nothing listens
on the LAN. That is why the Service is ClusterIP: on macOS, Rancher Desktop
forwards LoadBalancer and NodePort ports on every interface (there is no
localhost-only switch in `rdctl` there), and the mini's firewall is off.

Each deploy replaces the pod, which breaks the tunnel; the agent is reloaded
by the deploy script and also self-heals within ~30s. Log:
`~/Library/Logs/bet-portforward.log`.

## One-time setup on the mini

1. **Enable HTTPS certificates** for the tailnet: Tailscale admin console →
   DNS → HTTPS Certificates → Enable. `tailscale serve` needs it.

## Deploy

On the mini, from a checkout of this repo:

```bash
git pull
./scripts/deploy-k3s.sh            # apply, wait for rollout, load the port-forward agent, check 127.0.0.1:8787
./scripts/deploy-k3s.sh --serve    # first time: also publish on the tailnet
```

The script refuses to run on any machine but the mini. CI keeps the image tag
in `deployment.yaml` pointed at the latest `main` image (`# bet-api-image`
marker, `[skip ci]` commit; needs the `GH_PAT` repository secret), so
`git pull` plus the script deploys main.

## Admin

The image carries the `bet` CLI, which acts on the pod's warehouse directly:

```bash
kubectl -n bet exec -it deploy/bet-api -- bet doctor
kubectl -n bet exec -it deploy/bet-api -- bet user create <name>
kubectl -n bet exec -i  deploy/bet-api -- bet user set-password <name> --password-stdin
```

Only one process may open the warehouse at a time, and the server holds it, so
`exec` commands that open the database fail while the API runs. For those,
scale to zero first: `kubectl -n bet scale deploy/bet-api --replicas=0`. Then
run a one-off pod on the volume and scale back to 1.

## From the Air

```bash
bet config set api_url https://toms-mac-mini.tail279405.ts.net
bet login
bet doctor        # mode remote, api health / readiness / login
```

`bet config set api_url ''` returns to the local warehouse.
