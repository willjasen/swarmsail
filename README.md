# Swarmsail

Swarmsail inventories and manages multiple Headscale control servers. Its local web UI models organizations and devices. Subscriber tailnets are independent; cross-tailnet sharing is outside the initial product scope.

For subscriber accounts, an operator can reserve multiple independent, isolated Headscale tailnets for one opaque account UUID using [`scripts/provision_subscriber.py`](scripts/README.md). Each tailnet remains active; the instances do not share devices or policy. The command previews by default and requires `--apply` to write `instances.json` and the Swarmsail SQLite mappings. The operator must verify an active trial or subscription before invoking it; this checkout does not provide hosted account authentication or payment verification. Reservation alone does not deploy Headscale, configure DNS/HTTPS ingress, or enroll devices.

See [the provisioning runbook](runbooks/2026-10-18-subscriber-tailnet-provisioning.md) and [the current do2 deployment](runbooks/do2-current-setup.md) before operating on the live host. Swarmsail DNS records must point directly to the host, without record proxying.

For a local OrbStack control plane, use [`docker-compose.orbstack.yml`](docker-compose.orbstack.yml) and [its runbook](runbooks/2026-10-18-orbstack-local-control-plane.md). It listens only on `127.0.0.1:8787`, uses node-local state, and does not start subscriber Headscale instances.

To deploy a disposable full stack and verify two real Tailscale clients can ping
and exchange TCP traffic, run
`SWARMSAIL_RUN_E2E=1 python3 -m unittest scripts.tests.test_full_stack -v`.
The [full-stack test runbook](runbooks/test-full-stack.md) covers prerequisites,
HTTPS, CrowdSec verification, isolation, and automatic cleanup.

The intended public coordinator and droplet-node architecture is documented in [the deployment architecture](docs/coordinator-droplet-architecture.md). It is not implemented yet: the existing dashboard is a local operator UI without user authentication and must not be exposed publicly. The droplet model requires its own globally routable IPv4; provider-side provisioning is deferred. Configurations marked `deployment.role: "droplet"` reject non-global IPv4 addresses and require enabled Caddy ingress to bind to the configured address; a DigitalOcean metadata/DNS check verifies the address and direct DNS records. See [the droplet IPv4 runbook](runbooks/droplet-public-ipv4.md).
