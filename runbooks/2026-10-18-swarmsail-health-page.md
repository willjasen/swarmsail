# Swarmsail health page

## Objective and initial state

Show a browser-friendly Swarmsail status page at `https://swarmsail.willjasen.net/health`. Caddy currently sends the whole hostname to the Rawrarr coordinator, so `/health` displays Rawrarr's health page. The Swarmsail control plane is healthy at its loopback-only port and shares Caddy's private Docker network.

## Implementation

The Swarmsail HTTP service now serves a small status page at `/health`; `/healthz` remains the JSON liveness endpoint used by Docker. Caddy proxy routes may set `health_upstream` to route exactly `/health` to the control plane, leaving all other paths on the configured primary upstream.

## Deployment and verification

Set `health_upstream` to `control-plane:8787` for the `swarmsail.willjasen.net` proxy route in node-local `instances.json`, then regenerate the Caddyfile with the repository manager. Rebuild/recreate the control plane from the Swarmsail Compose project and restart Caddy to load the generated route. Verify `/health` returns Swarmsail HTML, `/healthz` remains available internally, and `/` still reaches the Rawrarr coordinator. Validate TLS and the coordinator's existing health path after deployment.

## Recovery

Remove `health_upstream` from the node-local route configuration, regenerate the Caddyfile, and restart Caddy. This restores all hostname paths to the existing Rawrarr coordinator. The app page change is harmless if retained because it is not routed publicly after rollback.
