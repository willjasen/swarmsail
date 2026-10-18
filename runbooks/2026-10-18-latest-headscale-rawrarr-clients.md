# Latest Headscale and Rawrarr client cutover

## Objective and initial state

Upgrade the MacBook Swarmsail Headscale instance to the latest stable release and connect the Rawrarr Docker Tailscale clients on MacBook and Mac mini to the account's private Headscale tailnet. Do not change either Mac's host-level Tailscale client. Do not use Tailscale Serve with Headscale; preserve Rawrarr coordinator access through a separate HTTPS hostname routed by Caddy.

Initial live Headscale was v0.27.0 with one Rawrarr account user and no registered Headscale nodes. Current latest stable is v0.29.5. The official upgrade guide requires stepping through each stable minor release, so the planned path is v0.27.0 → v0.27.1 → v0.28.0 → v0.29.0 → v0.29.5. The coordinator's MacBook Tailscale sidecar currently serves `rawrarr.risk-mermaid.ts.net` and a Syncthing TCP forward; changing its control server will remove those handlers. Replace app ingress with Caddy on a separate `.willjasen.net` hostname before the client cutover. The Headscale hostname remains `rawrarr.willjasen.net`.

## Identity backups

Before any Tailscale container changes, copied the complete persistent state directories on each host into node-local, mode-700 backup trees:

- MacBook: `~/Library/Application Support/rawrarr/backups/tailscale-identities-20261018T222233Z/macbook`
- Mac mini: `~/Library/Application Support/rawrarr/backups/tailscale-identities-20261018T222233Z/mac-mini`

Both backups contain 20 entries. A recursive content and permission comparison succeeded on each host. The originals remain intact. These directories contain recoverable Tailscale credentials; never sync, commit, upload, or print their contents. Restore by stopping only the matching Tailscale container and copying its saved directory back to the corresponding original state path.

## Upgrade and cutover procedure

Stop Headscale before taking the migration backup. Copy its configuration and full data directory, including SQLite WAL/SHM files if present, into a mode-700 Swarmsail backup directory. Preserve the generated Compose file and current image reference. Upgrade one stable release at a time, validate the configuration and service version after each step, and verify the Caddy `/health` route and existing Headscale user remain present. Retain all prior images and backups until both clients and coordinator routing are verified.

Create a fresh single-use, short-lived Headscale pre-authentication key for each client. Mount each key from its own node-local mode-600 file; pass it to the Tailscale container as `TS_AUTHKEY=file:/run/secrets/headscale-preauth-key` so Compose YAML and process output do not contain the key. Set `TS_EXTRA_ARGS=--login-server=https://rawrarr.willjasen.net`. Do not reuse the old Tailscale state directory for a live cutover until its complete backup is verified. Keep the two node state directories separate.

The MacBook coordinator's old Tailscale Serve configuration is not supported as an ingress plan for Headscale. Configure a distinct HTTPS Caddy hostname for the Rawrarr coordinator and verify its certificate, DNS resolution, `/health`, and authenticated browser behavior before changing the MacBook Tailscale client. Keep the local `http://127.0.0.1:7297` path and passkey origin available during this transition. The proposed hostname is `coordinator.willjasen.net`; verify the app's current WebAuthn relying-party settings and Cloudflare DNS before deployment.

## Verification and recovery

After each Headscale release, confirm the running binary reports the intended version, `headscale configtest` passes, `https://rawrarr.willjasen.net/health` returns `{"status":"pass"}`, and the existing Rawrarr Headscale user remains. After client enrollment, verify exactly the two intended Docker clients appear under that user, both report connected, and a TCP request succeeds between their Headscale addresses. Verify Rawrarr coordinator access over its Caddy hostname without Tailscale Serve. Do not claim enrollment or app linking from a successful coordinator health check alone.

If a database migration fails, stop Headscale and restore the pre-upgrade configuration and complete data-directory backup before starting the prior image. If client enrollment or coordinator ingress fails, restore the affected container's backed-up Tailscale state and prior Compose configuration. Do not reset or replace either host's native Tailscale state.

## Current progress

Completed: backed up and verified both Docker Tailscale state directories. Confirmed MacBook container client version 1.104.1, Mini sidecar is running, Caddy/Headscale ingress is healthy, and the MacBook Serve configuration currently has coordinator HTTPS plus Syncthing TCP handlers. Updated Swarmsail's generated and example Compose defaults to Headscale v0.29.5. Headscale's sequential upgrade, coordinator Caddy hostname, and both client enrollments remain in progress.
