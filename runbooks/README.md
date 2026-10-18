# Swarmsail runbooks

- [Analyze disk space](analyze-disk-space.md) — Inspect storage usage and cleanup candidates.
- [Local OrbStack control plane](2026-10-18-orbstack-local-control-plane.md) — Run Swarmsail locally on loopback with node-local state.
- [Local OrbStack support stack](2026-10-18-orbstack-support-stack.md) — Deploy local Headscale, NPM Plus, and CrowdSec; finish LAN HTTPS after DNS-01 issuance.
- [Rawrarr Headscale URL access](2026-10-18-rawrarr-headscale-access.md) — Diagnose and correct Cloudflare DNS for the Mac LAN endpoint, then account for resolver cache and LAN-only routing.
- [Link the existing Rawrarr account to a tailnet](2026-10-18-link-existing-rawrarr-user-tailnet.md) — Link the passkey-backed account to the empty local Headscale instance as its first isolated tailnet.
- [Swarmsail Caddy Headscale ingress](2026-10-18-swarmsail-caddy-headscale-ingress.md) — Generate and deploy Caddy as Swarmsail-managed HTTPS ingress for private Headscale listeners.
- [Latest Headscale and Rawrarr client cutover](2026-10-18-latest-headscale-rawrarr-clients.md) — Upgrade local Headscale through the required stable versions, preserve existing Docker Tailscale identities, and connect Rawrarr clients without Tailscale Serve.
- [Swarmsail health page](2026-10-18-swarmsail-health-page.md) — Serve a Swarmsail status page at `/health` while preserving the Rawrarr coordinator route.
- [Current do2 setup](do2-current-setup.md) — Describe the existing deployment and its service boundaries.
- [Rawrarr tailnet sharing transition](2026-10-18-rawrarr-tailnet-sharing-transition.md) — Record the irreversible isolated-to-shared model.
- [Subscriber tailnet provisioning](2026-10-18-subscriber-tailnet-provisioning.md) — Reserve account-bound managed instances and document remaining deployment work.
