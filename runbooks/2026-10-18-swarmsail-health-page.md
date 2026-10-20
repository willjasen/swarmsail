# Swarmsail health page

## Objective and initial state

Show a browser-friendly Swarmsail status page at `https://swarmsail.willjasen.net/health`. Caddy currently sends the whole hostname to the coordinator, so `/health` displays its health page. The Swarmsail control plane is healthy at its loopback-only port and shares Caddy's private Docker network.

## Implementation

The Swarmsail HTTP service now serves a small status page at `/health`; `/healthz` remains the JSON liveness endpoint used by Docker. Caddy proxy routes may set `health_upstream` to route exactly `/health` to the control plane, leaving all other paths on the configured primary upstream.

## Deployment and verification

The active node-local configuration was backed up to `~/Library/Application Support/Swarmsail/backups/instances-20261018-184927-health-page.json` (mode 600), then `health_upstream` was set to `control-plane:8787` for the `swarmsail.willjasen.net` proxy route. Regenerate the runtime files with:

```sh
python3 manage_headscale.py generate \
  --config "$HOME/Library/Application Support/Swarmsail/instances.json" \
  --output "$HOME/Library/Application Support/Swarmsail/docker-compose.headscale.yml" \
  --apply
```

Validate the mounted Caddyfile, rebuild only the control plane, and restart Caddy with the runtime's required Compose environment (`SWARMSAIL_LOCAL_DATA_DIR` and `SWARMSAIL_LAN_IP`). Caddy and the control plane are both on `swarmsail-local-private`; the coordinator remains reached through the existing external ingress bridge.

Verification passed: all 14 script tests; Python bytecode compilation; `caddy validate`; local `/healthz` returned `{"status": "ok"}` and the rebuilt container became healthy; HTTPS `/health` returned the Swarmsail status HTML with a trusted certificate; and HTTPS `/` still returned the coordinator landing page. A Compose `ps` check without required interpolation variables printed warnings/errors after the Caddy restart succeeded; Docker inspection confirmed both target containers were running, and subsequent checks passed.

## Recovery

Remove `health_upstream` from the node-local route configuration, regenerate the Caddyfile, and restart Caddy. This restores all hostname paths to the existing coordinator. The app page change is harmless if retained because it is not routed publicly after rollback.
