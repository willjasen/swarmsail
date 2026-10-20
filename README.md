# Swarmsail

Swarmsail inventories and manages multiple Headscale control servers (Masts). In the Captain's console, a Crew is a managed network, and its Headscale users are mateys. A persistent left navigation rail lists Crews and links to setup, access policy, inventory, and security. Open a Crew to inspect its Mast, then filter its device list by matey. Captains can create mateys, enroll devices with one-time keys or pending node keys, and revoke mateys or devices from a Crew's Mast. Removing a matey also revokes that matey's devices; removals delete the related local device records and planned share/ACL entries. Confirm each removal carefully. Cross-Crew sharing rules remain planned intent and are not applied to Headscale yet.

For subscriber accounts, an operator can reserve a managed, isolated Headscale instance for an opaque account UUID using [`scripts/provision_subscriber.py`](scripts/README.md). One generation is active per account. A shared account can reserve a brand new private generation without downgrading the old one. The command previews by default and requires `--apply` to write `instances.json` and the Swarmsail SQLite mapping. The operator must verify an active trial or subscription before invoking it; this checkout does not provide hosted account authentication or payment verification. Reservation alone does not deploy Headscale, configure DNS/HTTPS ingress, enroll devices, or activate sharing.

See [the provisioning runbook](runbooks/2026-10-18-subscriber-tailnet-provisioning.md) and [the current do2 deployment](runbooks/do2-current-setup.md) before operating on the live host. Swarmsail DNS records must point directly to the host, without record proxying.

See [the Crew administration runbook](runbooks/manage-crews-and-mateys.md) for device enrollment, user and device revocation, verification, and recovery limits.

For a local OrbStack control plane, use [`docker-compose.orbstack.yml`](docker-compose.orbstack.yml) and [its runbook](runbooks/2026-10-18-orbstack-local-control-plane.md). It listens only on `127.0.0.1:8787`, uses node-local state, and does not start subscriber Headscale instances.

To deploy a disposable full stack and verify two real Tailscale clients can ping
and exchange TCP traffic, run
`SWARMSAIL_RUN_E2E=1 python3 -m unittest scripts.tests.test_full_stack -v`.
The [full-stack test runbook](runbooks/test-full-stack.md) covers prerequisites,
HTTPS, CrowdSec verification, isolation, and automatic cleanup.
