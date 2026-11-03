# Manage Crews, mateys, and devices

## Purpose

Describe the Swarmsail Captain's console workflows for managing Headscale
users and devices within a Crew. A Crew is Swarmsail's name for one logical
tailnet; a Mast is its coordinating Headscale server; a matey is a Headscale
user. "Captain" refers to the operator using Swarmsail, not a Headscale role.

## Prerequisites

- The Captain is connected to the authorized tailnet and can open the
  tailnet-only Swarmsail URL.
- The Crew is assigned to a configured Mast. Creating and removing mateys,
  and revoking Headscale devices, are unavailable for Crews without a
  dedicated Mast.
- The Swarmsail control plane has its documented Docker access so it can run
  Headscale management commands against the configured Mast container.
- Before a destructive removal, confirm that the Headscale configuration and
  data for the affected Mast are included in a restorable backup.

## Navigate Crews and inventory

1. Use the left navigation rail to open **Set up**, **Access policy**,
   **Inventory**, or **Security**. The rail remains available on both the main
   console and the CrowdSec security page.
2. Expand **Crews** at the top of the rail and select a Crew to open its Mast
   detail page. Its selected state is shown in the Crew list.
3. On the Crew detail page, choose a matey in **Show devices for** to filter
   the device table. **All mateys** restores the complete Crew inventory. The
   **Devices** action in the Mateys table selects that matey in the filter.

## Add a matey

1. Open the Captain's console and select the Crew.
2. In **Enroll a device**, enter a Headscale-compatible matey name and select
   **Create matey**.
3. Confirm the matey appears in the Crew's Mateys table and in the device-owner
   selector.

Creating a matey does not enroll a device. A matey can exist with zero
devices.

## Enroll a device

For a command-line Tailscale client:

1. Select its matey in **Enroll a device**.
2. Select **Generate connection command**. The one-time key is valid for one
   hour and is shown only in the current page.
3. Run the displayed command on the intended device, then verify it appears
   in the Crew's Devices table after inventory refresh.

For the macOS GUI client:

1. In Tailscale Settings → Accounts, add the displayed custom control server.
2. Paste the pending node key into the Captain's console, select its matey,
   and choose **Register pending device**.
3. Verify the registered device appears in the Crew's Devices table after
   inventory refresh.

Do not save or share a one-time key or pending node key after registration.

## Remove a matey or device

1. In the Crew detail page, review the matey's device count or the device's
   name and owner.
2. Choose **Remove** and read the confirmation dialog before proceeding.
   Removing a matey destroys that Headscale user and all of the user's
   Headscale devices. Removing a linked device deletes its Headscale node.
   Removing either also deletes the corresponding local device records and
   their planned share/ACL entries. A local-only device record is removed
   from Swarmsail without calling Headscale.
3. Confirm. If inventory changed since the page loaded, the operation is
   rejected; refresh the Crew and review the new state before retrying.
4. Verify the matey or device is absent from the Crew page and, for linked
   records, from the selected Mast's Headscale lists.

The Captain's console scopes deletion to the selected Crew and checks the
Headscale service, record identity, and matey device count before running a
destructive Headscale command. These deletions are permanent unless the
affected Headscale data is restored from backup; re-creating a matey does not
restore the removed devices.

## Verification

Replace placeholders with the Crew's configured Mast service and the
tailnet-only Swarmsail hostname. The service name comes from the deployed
`instances.json` configuration; do not copy credentials or runtime state.

```sh
curl -fsS https://<swarmsail-tailnet-hostname>/healthz
docker exec <mast-container> headscale users list --output json
docker exec <mast-container> headscale nodes list --output json
```

The health endpoint should return `{"status":"ok"}`. The Headscale list
commands should show the remaining users and nodes. Swarmsail inventory may
take one inventory interval to reflect changes made outside the console.

## Deployment and migration

Deploy this feature as a new immutable Swarmsail release and rebuild only the
`swarmsail:local` image. Back up `/opt/swarmsail/data/swarmsail.db` using a
SQLite-aware backup method while preserving its WAL state; do not copy only
the live database file. Retain the current image and release for rollback.

This feature adds no SQLite tables or columns, so the existing database is
reused without a schema migration. Deploy only the Swarmsail Compose service
with dependency recreation disabled. Do not run `docker compose down`, do not
restart other services, and do not modify Headscale, NPM Plus, CrowdSec,
cloudflared, Docker networks, or Tailscale Serve routes. Preserve the existing
Headscale configuration, databases, and keys. Test `/healthz`, the Crew
detail page, and enrollment/removal controls before ending the maintenance
window.

## Rollback and recovery

- For an application regression, point the `swarmsail:local` tag at the
  retained previous Swarmsail image and recreate only the Swarmsail service.
  The existing SQLite database is schema-compatible with the previous
  release.
- Do not roll back a successful Headscale user or node removal by restoring
  only the Swarmsail database. That would recreate stale UI records, not the
  deleted Headscale objects.
- To recover an accidental Headscale removal, follow the site's approved
  Headscale backup restoration procedure for that Mast and a matching
  Swarmsail database snapshot. Treat restore as a separate maintenance
  operation; never overwrite a live data directory without a verified
  recovery plan.

## Unresolved assumptions

- Headscale user and node deletion is permanent and removes the user's
  associated nodes. The console does not implement soft-delete or undo.
- Cross-Crew shares and ACL entries remain desired policy only; deleting a
  linked device removes its local planned entries but does not apply a
  cross-instance policy change to Headscale.
- The Captain's console is intended to remain reachable only through the
  authorized tailnet path configured for the deployment.
