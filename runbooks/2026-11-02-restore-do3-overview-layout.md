# Restore do3 overview layout

## Objective and initial state

Restore the overview-first landing page on `do3` while retaining the current
light-blue styling and sidebar navigation. Local `main` matched GitHub `main`
at `f4a64ea`. The overview counts and organization/device summary were present
in the legacy Python page, but the live Rust app serves `web/index.html`, which
still opened on setup and placed inventory on a separate page. See the
[overview-first dashboard runbook](2026-11-02-overview-first-dashboard-layout.md)
and the [do3 GitHub update runbook](2026-11-02-update-do3-from-github.md).

## Implementation and deployment

Add an Overview page before setup, access policy, and inventory. Put the
organization, device, online, and unapplied-plan counts plus Crew and Device
lists there. Keep connections, shares, and ACLs in Inventory. Preserve the
existing color variables and compact sidebar navigation.

Build the Rust image from the pushed `main` commit. Before changing production,
back up `instances.json` and use SQLite's backup API for `swarmsail.db`; verify
the copy with `PRAGMA integrity_check`. Retain the prior image, then recreate
only the Swarmsail Compose service with `--no-deps --force-recreate`.

## Verification and recovery

Verify `/healthz`, `/api/state`, the root dashboard, Overview as the default
page, and the Crew detail page locally and through the `do3` URL. Check that
the four counts populate and the existing light-blue palette remains. Ensure
other production services were not recreated. If checks fail, restore the
previous `swarmsail:local` image from its rollback tag and recreate only the
Swarmsail service. Restore database/config backups only if rollback requires
their prior data state.

Record the source commit, image ID, backup location (sanitized), verification
results, final service state, and any remaining limitations before completing.
