# Swarmsail

Swarmsail inventories and manages multiple Headscale control servers (Masts). In the Captain's console, a Crew is a managed network, and its Headscale users are mateys. A persistent left navigation rail lists Crews and links to setup, access policy, inventory, and security. Open a Crew to inspect its Mast, then filter its device list by matey. Captains can create mateys, enroll devices with one-time keys or pending node keys, and revoke mateys or devices from a Crew's Mast. Removing a matey also revokes that matey's devices; removals delete the related local device records and planned share/ACL entries. Confirm each removal carefully. Cross-Crew sharing rules remain planned intent and are not applied to Headscale yet.

For subscriber accounts, an operator can reserve multiple independent, isolated Headscale tailnets for one opaque account UUID using [`scripts/provision_subscriber.py`](scripts/README.md). Each tailnet remains active; the instances do not share devices or policy. The command previews by default and requires `--apply` to write `instances.json` and the Swarmsail SQLite mappings. The operator must verify an active trial or subscription before invoking it; this checkout does not provide hosted account authentication or payment verification. Reservation alone does not deploy Headscale, configure DNS/HTTPS ingress, or enroll devices.

The opt-in private `swarmsail-provisioner` API and supervised host-local worker add durable account-default allocation, authenticated v1 service contracts, authoritative readiness and recoverable short-lived single-use enrollment. All authenticated accounts, including free accounts, qualify. See [the default-tailnet runbook](runbooks/provision-default-tailnet.md) for trust, externally installed wildcard DNS/TLS, adoption, tests and recovery. The required Rawrarr peer transport fixture is a separate readiness gate; this does not authorize deployment or expose the operator dashboard.

See [the provisioning runbook](runbooks/2026-10-18-subscriber-tailnet-provisioning.md) and [the current do2 deployment](runbooks/do2-current-setup.md) before operating on the live host. Swarmsail DNS records must point directly to the host, without record proxying.

See [the Crew administration runbook](runbooks/manage-crews-and-mateys.md) for device enrollment, user and device revocation, verification, and recovery limits.

For a local OrbStack control plane, use [`docker-compose.orbstack.yml`](docker-compose.orbstack.yml) and [its runbook](runbooks/2026-10-18-orbstack-local-control-plane.md). It listens only on `127.0.0.1:8787`, uses node-local state, and does not start subscriber Headscale instances.

The Swarmsail HTTP backend and SQLite API are implemented in Rust (`src/`) and built into the local control-plane image. The existing Python `manage_headscale.py` generator and `scripts/provision_subscriber.py` operator workflow remain available as compatibility tooling; they are not the HTTP server.

To deploy a disposable full stack and verify two real Tailscale clients can ping
and exchange TCP traffic, run
`SWARMSAIL_RUN_E2E=1 python3 -m unittest scripts.tests.test_full_stack -v`.
The [full-stack test runbook](runbooks/test-full-stack.md) covers prerequisites,
HTTPS, CrowdSec verification, isolation, and automatic cleanup.

The intended public coordinator and droplet-node architecture is documented in [the deployment architecture](docs/coordinator-droplet-architecture.md). It is not implemented yet: the existing dashboard is a local operator UI without user authentication and must not be exposed publicly. The droplet model requires its own globally routable IPv4; provider-side provisioning is deferred. Configurations marked `deployment.role: "droplet"` reject non-global IPv4 addresses and require enabled Caddy ingress to bind to the configured address; a DigitalOcean metadata/DNS check verifies the address and direct DNS records. See [the droplet IPv4 runbook](runbooks/droplet-public-ipv4.md).
