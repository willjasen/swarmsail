# Update do3 Swarmsail from GitHub

## Objective and initial state

Deploy the latest public `main` revision to the existing Swarmsail service on
`do3`, preserving its mounted application data and all other services. The
local `main` and `origin/main` were at the same commit when this task began.
The production container used `swarmsail:local`; its Compose definition has no
build context. The host's synchronized source directory was not a Git checkout
and lacked the Rust source and web templates now used by `main`. Tailscale
Serve routes the service through loopback port 18787; Funnel is enabled for the
`do3` hostname.

## Deployment procedure

1. Read this runbook and the dashboard deployment/recovery notes in
   [overview-first dashboard layout](2026-11-02-overview-first-dashboard-layout.md).
2. Confirm the droplet, deployment Compose file, service name, image, and
   `/data` bind mount. Run all Linux host commands inside tmux session `ai`.
3. Pin the public `main` revision to its full commit SHA and download that
   commit's GitHub source archive when Git is unavailable on the host. Build
   the `Dockerfile.orbstack` image from the archive. Do not copy private
   production data or credentials into the build context.
4. Before replacement, back up `instances.json` and use SQLite's backup API
   for `swarmsail.db`; run `PRAGMA integrity_check` on the backup. Store
   backups under `<deployment-root>/backups/<update-id>/` with mode 600 files
   and a mode 700 directory.
5. Retain the current Swarmsail image ID/tag. Recreate only the Swarmsail
   service with dependency recreation disabled. Do not restart Headscale,
   Rawrarr, Postgres, Caddy, Tailscale, CrowdSec, or other services.
6. Verify `/healthz`, `/api/state`, the root page, the Crew detail page, and
   container health. Check application logs for startup and database errors.
7. If verification fails, restore the previous image tag and recreate only
   Swarmsail. Restore database/config backups only if the new app changed them
   and rollback requires the previous data state.

For an archive-based build, replace each angle-bracket placeholder before
running these commands:

```sh
REV=<full-github-commit-sha>
curl -fLsS "https://github.com/willjasen/swarmsail/archive/${REV}.tar.gz" \
  -o "/tmp/swarmsail-${REV}.tar.gz"
mkdir "<source-dir>"
tar -xzf "/tmp/swarmsail-${REV}.tar.gz" --strip-components=1 -C "<source-dir>"
docker build -f "<source-dir>/Dockerfile.orbstack" \
  -t "swarmsail:github-${REV}" "<source-dir>"
docker tag <current-image-id> swarmsail:rollback-<update-id>
docker tag "swarmsail:github-${REV}" swarmsail:local
docker compose -f "<deployment-compose-path>" \
  up -d --no-deps --force-recreate swarmsail
```

Before starting the new container, test its image against copies of the
database and config on a loopback-only temporary port. Do not mount the live
database into a candidate container while production is running.

## Findings and verification

The Rust and Python stores declare the same core tables and Rust applies the
same device-column migrations, so the existing database is structurally
compatible. For this update, GitHub `main` was `b45843e15c84ee4a15d7881f1c3788a499faee70`.
The image built successfully as `swarmsail:github-b45843e` (image ID
`sha256:5a1914f9482b7bb0b541cecb68d35f99a859935a2725e8afb717f4b2b32328ce`).
The previous image was retained as `swarmsail:rollback-20261103T014000Z`.

The SQLite-aware backup passed `PRAGMA integrity_check`. An isolated
loopback-only candidate using copies of the database and config passed
`/healthz`, `/`, `/api/state`, and `/organizations/1`. The production service
was recreated alone with `--no-deps --force-recreate`; its image ID matches
the new image and it used about 1.3 MiB of its 64 MiB limit after startup.
The same endpoints returned 200 locally and the public `do3` hostname returned
HTTP 200 for the dashboard and `{"status":"ok"}` for `/healthz`.

The repository's Rust handlers reference `/app/create_headscale_user.py` and
`/app/register_pending_node.py`, but those files are absent from the source
archive and `Dockerfile.orbstack`. Owner creation and pending-node registration
were not exercised and remain a known gap. Compose emitted an orphan-container
warning; no orphan cleanup option was used and no other service was recreated.

## Exposure note

The existing `do3` Tailscale Funnel route is public and the Swarmsail operator
API has no user authentication. This update preserved the existing route per
the user's deployment request; it does not implement the documented
public-coordinator security boundary.
