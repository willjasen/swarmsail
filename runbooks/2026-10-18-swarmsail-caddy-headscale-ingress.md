# Swarmsail-managed Caddy ingress for Headscale

## Objective and initial state

Make Swarmsail generate and run Caddy alongside its managed Headscale instances. The local Headscale listener is private on Docker port 8080; Caddy owns LAN TCP 80/443, obtains and renews TLS with Cloudflare DNS-01, and routes each managed `server_url` hostname to its matching Headscale container. UDP 3478 remains disabled because embedded DERP is disabled. CrowdSec integration is deferred.

Before the handoff, NPM Plus owned LAN port 80, Headscale directly owned LAN port 443 and terminated TLS, and Swarmsail generated only the Headscale service. The local DNS-only A record for `headscale.example.test` points to the MacBook LAN IP. The existing node-local Cloudflare token is mode 600 and is mounted read-only into Caddy; neither the token nor private key is stored in Git.

## Implementation

- Added a pinned Caddy image build with the Cloudflare DNS provider module.
- Extended `manage_headscale.py` so `caddy.enabled` generates a Caddy service and a node-local Caddyfile from each managed Headscale HTTPS origin. Caddy references the mounted token file, joins the Headscale network, binds only the configured LAN IP, and uses persistent node-local data/config directories.
- Added validation to prevent duplicate hostnames, direct public Headscale listeners, invalid HTTPS origins, and simultaneous NPM/Caddy ingress ownership.
- Placed NPM behind its opt-in `npm` Compose profile so it can be retained for rollback without claiming default ingress ports.

## Deployment and verification

Back up node-local `instances.json`, Headscale config, generated Compose, Swarmsail SQLite, and Headscale SQLite state before changing the live stack. Set `caddy.enabled: true`, configure its LAN bind IP, node-local config/data/state paths, and Cloudflare token file path. Generate with `python3 manage_headscale.py generate --config <instances.json> --output <generated-compose.yml> --apply`; review the generated Caddyfile and Compose before deployment.

The Headscale config behind Caddy must keep `server_url` on HTTPS and use an internal `listen_addr` with empty TLS certificate/key paths. Deploy the generated service in the same Compose project and Docker network as the local control plane. Stop NPM Plus before binding Caddy to ports 80/443.

On 2026-10-18, the local service was deployed using Caddy 2.11.7 and `caddy-dns/cloudflare` 0.2.4. Headscale was recreated without a published host port and with TLS disabled internally. Caddy now owns only `192.168.128.210:80/tcp` and `192.168.128.210:443/tcp`; NPM Plus is stopped but its node-local data remains. The Cloudflare token is mounted read-only from its node-local mode-600 file. ACME DNS-01 authorization succeeded and Caddy obtained a trusted certificate for `headscale.example.test`.

Verification passed: Swarmsail generator and Compose validation; `headscale configtest`; `caddy validate`; Caddy reports the Cloudflare DNS module; host requests and requests from the local coordinator container both receive `{"status":"pass"}` at `/health`; `/` returns the expected HTTP 404; public resolvers 8.8.8.8 and 9.9.9.9 resolve the hostname to `192.168.128.210`; the coordinator container resolves the same LAN address; and Docker inspect confirms Headscale has no published port while Caddy publishes only TCP 80/443 on the LAN IP. No existing node Tailscale identity was changed.

## Recovery and next phase

Rollback by stopping Caddy, restoring the saved Headscale TLS config, and recreating Headscale with the TLS override; then start the retained NPM service with `--profile npm`. The private rollback backup is under `~/Library/Application Support/Swarmsail/backups/` with the `-caddy-ingress` suffix. Keep Caddy certificate data and the old NPM volume unless the user explicitly asks to remove them. Next, inspect the coordinator-owned enrollment workflow before changing the two node clients; do not expose a reusable pre-authentication key in Compose or logs. CrowdSec log acquisition and remediation can be added later.
