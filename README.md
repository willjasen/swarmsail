# Swarmsail

Swarmsail inventories and manages multiple Headscale control servers. Its local web UI models organizations, devices, and proposed sharing rules. Cross-instance sharing rules are not applied to Headscale yet.

For Rawrarr, an operator can reserve a managed, isolated Headscale instance for an opaque account UUID using [`scripts/provision_subscriber.py`](scripts/README.md). One generation is active per account. A shared account can reserve a brand new private generation without downgrading the old one. The command previews by default and requires `--apply` to write `instances.json` and the Swarmsail SQLite mapping. The operator must verify an active trial or subscription before invoking it; this checkout does not provide hosted account authentication or payment verification. Reservation alone does not deploy Headscale, configure DNS/HTTPS ingress, enroll devices, or activate sharing.

See [the provisioning runbook](runbooks/2026-10-18-subscriber-tailnet-provisioning.md) and [the current do2 deployment](runbooks/do2-current-setup.md) before operating on the live host. Swarmsail DNS records must point directly to the host, without record proxying.
