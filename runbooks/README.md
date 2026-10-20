# Swarmsail runbooks

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
- [Tailnet sharing transition](2026-10-18-tailnet-sharing-transition.md) — Record the irreversible isolated-to-shared model.
- [Subscriber tailnet provisioning](2026-10-18-subscriber-tailnet-provisioning.md) — Reserve account-bound managed instances and document remaining deployment work.
