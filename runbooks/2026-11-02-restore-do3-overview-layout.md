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

For this update, `main` was pushed as `343a9480e2bca737412aef6a819315d7642e6b4e`.
The Rust app compiled with the cached builder stage from the preceding image;
only the Swarmsail crate rebuilt with the new embedded page. The runtime image
is `swarmsail:github-343a948`, image ID
`sha256:107ead296b9b5463ab6468123b57f2ea9f54383272ae8d50c647b509a3e48713`.
The prior image is retained as `swarmsail:rollback-20261103-layout-343a948`.

The SQLite-aware backup under
`<deployment-root>/backups/swarmsail-layout-343a948/` passed
`PRAGMA integrity_check`. A loopback-only candidate using copies of the
database and config passed `/healthz`, `/`, `/api/state`, and `/organizations/1`;
its HTML contained the Overview section, four counter IDs, Overview navigation,
and the light-blue `--page:#edf7fd` value. Production was recreated alone with
`--no-deps --force-recreate`; local and public root, state, and Crew page
requests returned HTTP 200, and both local and public `/healthz` returned
`{"status":"ok"}`. The live container used about 1.3 MiB of its 64 MiB limit.

The public route and Swarmsail service remained healthy. The Compose status
also showed Caddy as unhealthy and a separate CrowdSec container running; this
task did not recreate or alter those services. Their status should be handled
as a separate operational issue.
