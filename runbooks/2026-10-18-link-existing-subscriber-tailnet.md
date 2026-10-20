# Link an existing subscriber account to a Headscale tailnet

## Objective and initial state

Give an existing subscriber account a first isolated Swarmsail tailnet while trial and subscription integration is still planned. The MacBook already runs one empty, managed Headscale instance at `https://headscale.example.test`; it has no Headscale users or enrolled devices. Its embedded DERP/STUN is disabled, and only TCP 443 is published. The HTTPS root returns 404 because Headscale does not serve a dashboard there; `/health` is the health endpoint.

The account database contains one account with passkeys, but no trial/subscription entitlement state exists. This one-time operator setup links the opaque account UUID to the existing Headscale instance without asserting any entitlement or issuing a device key.

## Actions

- Confirmed the account exists and has passkeys without displaying account identifiers or credential data.
- Confirmed the local Headscale instance is empty and the Swarmsail SQLite store has no account-tailnet mapping.
- Backed up `instances.json`, the Swarmsail SQLite database, and the Headscale SQLite database to a mode-700, node-local `backups/<timestamp>-account-tailnet-link/` directory; database backup files are mode 600.
- Marked the existing Headscale instance as generation 1 for the account in `instances.json` and created its isolated Swarmsail organization/mapping using `Store.link_subscriber_tailnet`.
- Created a Headscale user named `subscriber-<account UUID>`. The link is to the account that owns the passkeys; the passkey itself is not a Headscale credential. No reusable pre-authentication key was generated.

## Verification

Verified `http://127.0.0.1:8787/api/state` reports one organization (isolated) and one Headscale instance; `headscale users list -o json` reports one user; `headscale nodes list -o json` returns `null`, meaning zero nodes; and `curl --resolve headscale.example.test:443:192.168.128.210 https://headscale.example.test/health` returns `{"status":"pass"}` with certificate verification. The root URL returns HTTP 404 as expected; it is not a sign-in failure. Tailscale device enrollment remains separate and requires a client to use this Headscale URL.

## Recovery and remaining work

Backups are stored under the node-local Swarmsail application-support directory with restrictive permissions. To unlink, remove only the account mapping and organization after confirming no devices or shares have been added, and remove the corresponding Headscale user. Do not delete the Headscale data directory or alter the DNS record. No trial/subscription entitlement was created or asserted. Future work: connect account authentication to eligibility checks, make account-tailnet provisioning automatic, add device enrollment, and implement proxy-based ingress if additional Headscale instances need distinct hostnames.
