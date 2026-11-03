# Swarmsail runbooks

- [Restore do3 overview layout](2026-11-02-restore-do3-overview-layout.md) — Restore the overview-first landing page in the Rust dashboard while retaining its light-blue styling.
- [Update do3 from GitHub](2026-11-02-update-do3-from-github.md) — Build the latest public `main` revision and update only the live Swarmsail service on `do3`.
- [Overview-first dashboard layout](2026-11-02-overview-first-dashboard-layout.md) — Lead with inventory and summary counts, and deploy the updated Swarmsail dashboard to `do3`.
- [Analyze disk space](analyze-disk-space.md) — Inspect storage usage and cleanup candidates.
- [Full-stack integration test](test-full-stack.md) — Deploy a disposable Swarmsail, Headscale, Caddy, and CrowdSec stack and verify bidirectional connectivity between two Tailscale containers.
- [Local OrbStack control plane](2026-10-18-orbstack-local-control-plane.md) — Run Swarmsail locally on loopback with node-local state.
- [Local OrbStack support stack](2026-10-18-orbstack-support-stack.md) — Deploy local Headscale, NPM Plus, and CrowdSec; finish LAN HTTPS after DNS-01 issuance.
- [Headscale URL access](2026-10-18-headscale-url-access.md) — Diagnose and correct Cloudflare DNS for the Mac LAN endpoint, then account for resolver cache and LAN-only routing.
- [Link an existing subscriber account to a tailnet](2026-10-18-link-existing-subscriber-tailnet.md) — Link an account to the empty local Headscale instance as its first isolated tailnet.
- [Swarmsail Caddy Headscale ingress](2026-10-18-swarmsail-caddy-headscale-ingress.md) — Generate and deploy Caddy with optional CrowdSec blocking and access-log acquisition.
- [CrowdSec and Caddy OrbStack test](crowdsec-caddy-orbstack-test.md) — Deploy an isolated local stack and verify Caddy blocking plus CrowdSec log parsing.
- [Latest Headscale client cutover](2026-10-18-latest-headscale-client-cutover.md) — Upgrade local Headscale through the required stable versions, preserve existing Docker Tailscale identities, and connect clients without Tailscale Serve.
- [Swarmsail health page](2026-10-18-swarmsail-health-page.md) — Serve a Swarmsail status page at `/health` while preserving the Rawrarr coordinator route.
- [Current do2 setup](do2-current-setup.md) — Describe the existing deployment and its service boundaries.
- [Crew administration](manage-crews-and-mateys.md) — Create mateys, enroll devices, and safely revoke users or devices within a Crew.
- [Droplet public IPv4](droplet-public-ipv4.md) — Configure and validate the public IPv4 required for a future droplet node.
- [Tailnet sharing transition](2026-10-18-tailnet-sharing-transition.md) — Record the irreversible isolated-to-shared model.
- [Subscriber tailnet provisioning](2026-10-18-subscriber-tailnet-provisioning.md) — Reserve account-bound managed instances and document remaining deployment work.
