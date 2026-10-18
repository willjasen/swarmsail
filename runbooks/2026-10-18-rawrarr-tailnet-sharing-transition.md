# Rawrarr tailnet sharing transition

## Purpose and initial state

Prepare Swarmsail's control-plane model for Rawrarr accounts that start with a dedicated isolated tailnet and may later make it shared. The local checkout contained a SQLite `organizations.mode` field and planned connections, device shares, and ACLs. It did not contain `manage_headscale.py` or the Headscale provisioning helpers imported by `swarmsail_web.py`; the live `do2` deployment is separate. See [current do2 setup](do2-current-setup.md) before any deployment.

## Change

- Add a SQLite trigger that rejects `collaborative` to `isolated` updates. Database migration creates the trigger when `Store` opens an existing database.
- Add an idempotent isolated-to-collaborative store operation. Do not expose an owner control until identity authorization and actual cross-tailnet access can be applied together.
- Keep existing connections, shares, and ACLs unchanged by the mode transition. This transition does not apply a Headscale policy or connect separate Headscale instances.
- Use the user's two-device example as a pairwise sharing direction: the owner selects a destination device and a recipient, and access becomes active only after the route and narrow policy are applied and verified. A `/30` has two conventional usable IPv4 addresses, but its mask is address allocation, not device authorization. A point-to-point bridge may use a small link prefix; it must not advertise full subscriber `/26` networks or grant broad host access by default.

## Verification

Run `python3 -m unittest discover -s tests -v`, `python3 -m py_compile swarmsail_web.py`, and `git diff --check`. Tests cover one-way mode change, direct SQL reversion rejection, lack of implicit grants, and isolation before transition. A real Headscale instance and the separate live deployment were not exercised.

## Deployment and recovery

Before deploying, inspect the live source, current database, configured Headscale instances, backup/restore readiness, reverse-proxy authentication, and generated-policy behavior. Back up the live SQLite database consistently and verify a restore path. Copy only reviewed release files, then verify the API and live Headscale connectivity. Do not apply this repository's example Compose over the deployed stack.

If the application change fails, restore the previous release. Do not downgrade a tailnet from shared to isolated to recover: members may already retain network knowledge or data. To return to a private security domain, create a new tailnet and enroll devices again.

## Remaining work

- Add the hosted Rawrarr account, trial/subscription entitlement, and authenticated owner API. Only a verified active trial/subscription should request provisioning.
- Restore or port the Headscale manager and helpers into a complete, testable Swarmsail source tree. Provision exactly one dedicated instance per eligible account with stable account-to-instance mapping and idempotent retries.
- Design and verify a cross-instance bridge or service proxy for specific device/service pairs. Headscale policies are scoped to their own instance; current `connections`, `shares`, and `acl_rules` are intent records only. Mark a share `applied` only after both sides' route/policy read-back and a real authorized/unauthorized traffic test.
- Test against a disposable Headscale deployment before updating `do2`.
