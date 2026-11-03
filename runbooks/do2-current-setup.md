# `do2` Swarmsail deployment: current setup and operations

## Purpose

Describe the live `do2` deployment as observed on 2026-10-07, including the
files that are authoritative on the host, the service relationships, recovery
and upgrade procedures, and the checks needed to prove that a change worked.

This is an operational record, not a substitute for a fresh inventory. The
host is the source of truth for deployed state. The repository root
`docker-compose.yml` is a development/minimal example and does not describe
the complete `do2` deployment.

Do not record or copy secrets, private keys, database contents, enrollment
keys, or real public/private service addresses into this repository. Replace
the placeholders below locally when operating on the host.

## Current topology

`do2` is an Ubuntu 24.04 host using Docker Compose projects and systemd:

| Component | Current role | Authoritative location or access |
| --- | --- | --- |
| `headscale-resicom` | Headscale control server for the ResiCom tailnet | Compose project `/opt/headscale`; config/data/log bind mounts under `/opt/headscale/headscale/resicom/` |
| `headscale-nerdhirn` | Second Headscale control server for the Nerdhirn tailnet | Managed Swarmsail release at `/opt/swarmsail/current`; data under `/opt/swarmsail/data/headscale/nerdhirn/` |
| `npmplus` | Public HTTP/HTTPS reverse proxy and ACME integration | `/opt/headscale/docker-compose.yml`; persistent data `/opt/headscale/npm/data` |
| `crowdsec` | CrowdSec local API and NPM log consumer | `/opt/headscale/docker-compose.yml`; config/data under `/opt/headscale/crowdsec/` |
| `swarmsail` | Containerized Swarmsail control plane and inventory UI/API | Release selected by `/opt/swarmsail/current`; persistent data `/opt/swarmsail/data` |
| `resilio-sync.service` | Synchronizes selected host/deployment content | `/etc/resilio-sync/config.json` and systemd unit |
| `tailscaled.service` | Tailnet access and private administration paths | `/var/lib/tailscale`, `/etc/default/tailscaled` |
| `beszel-agent` | Host monitoring agent | Docker container; inspect its current image/config before changing |

The Docker network `npm-net` is an external bridge network shared by the
Headscale, NPM Plus, CrowdSec, and related containers. Do not remove it while
any of those containers are attached.

Observed host-level facts:

- OS: Ubuntu 24.04 LTS.
- Docker Engine: 29.8.2 at the time of capture; verify before upgrades.
- Root filesystem: approximately 8.7 GiB, 80% used at capture time.
- NPM Plus publishes HTTP/HTTPS publicly and binds its admin backend only to
  loopback. Tailscale Serve exposes the NPM admin UI privately.
- DNS records for Swarmsail names must point directly to the hosting server;
  do not enable DNS proxying.

## Live inventory snapshot (2026-10-20)

This read-only SSH inventory supersedes the older resource and container
observations above; it is a snapshot, not a guarantee of current state:

- SSH access succeeded as `root@do2.risk-mermaid.ts.net`.
- Docker, Resilio Sync, and Tailscale systemd services were active.
- Compose projects were `headscale` (`/opt/headscale/docker-compose.yml`),
  `swarmsail-managed` (`/opt/swarmsail/current/docker-compose.managed.yml`),
  and the release-named Swarmsail control-plane project
  (`/opt/swarmsail/current/compose.swarmsail.yml`, project
  `20260902t043000z-value-only-fade`).
- `/opt/swarmsail/current` now resolves to
  `/opt/swarmsail/releases/20261020T0317Z-crew-navigation-links`. The healthy
  `swarmsail:local` container runs image
  `c23bb58df7f20609db73c3f77598b8ed37f23fa1734fe34176b08882fba895bc`.
  This immutable release was copied from
  `20261020T0313Z-crew-navigation`, then only `swarmsail_web.py` was replaced
  from the checkout (SHA-256
  `74c5fd62db10436b71a63c60f5eb45598ad460aacc32285df7702677047e11fb`).
  The `swarmsail:rollback-20261020T0317Z-crew-navigation-links` image,
  previous releases, and earlier rollback images remain available. A
  verified SQLite backup is at
  `/opt/swarmsail/backups/swarmsail-20261020T0317Z-crew-navigation-links.db`
  (SHA-256
  `7a2ee983ff8a4ebf4602cfd46c1a4080c93297e9cf72569917e3bdce613be887`);
  `PRAGMA integrity_check` returned `ok`. No SQLite schema migration was
  needed. The active release mounts its `instances.json` read-only,
  persistent data at `/opt/swarmsail/data`, and the Docker socket read/write.
  The Docker socket mount grants the control plane substantial host-level
  control; preserve access restrictions around the UI and its Docker network.
  `swarmsail:rollback-dark-default-20261020t022000z` preserves the preceding
  sky-blue release with system-preference default.
  `swarmsail:rollback-sky-blue-20261020t021000z` preserves the previous
  parchment/forest design.
  Rollback image `swarmsail:rollback-dashboard-20261020t014500z` preserves the
  pre-redesign image.
- The 2026-10-20 Captain-console release added Captain, Crew, matey, and Mast
  terminology, per-Crew matey creation and device enrollment, and
  confirmation-gated matey/device removal. The follow-up
  `crew-navigation-links` release adds a persistent left navigation rail, a Crew
  selector on both the console and CrowdSec page, separate setup/access
  policy/inventory/security destinations, and a matey filter for Crew devices.
  Local health, dashboard markup, CrowdSec markup, `/api/state`, and a Crew
  detail route were verified. From a tailnet-connected client, HTTPS
  `/healthz`, `/`, and `/crowdsec` were verified with the new navigation and
  device-filter markup. Browser interaction confirmed that page links from a
  Crew detail return to the selected dashboard page, and the Mateys table's
  **Devices** action filters the Crew device list; **All mateys** restores the
  complete list. The Headscale, NPM Plus, and CrowdSec container IDs remained
  respectively `c1a5644c3c72`, `41fd6260d934`, `781a23cac5f4`; both Headscale
  containers and both named services remained running. No
  `cloudflared` container was present in the Docker inventory. The update
  recreated only `swarmsail`; no other service, Docker network, or Tailscale
  Serve route was changed.
- `headscale-resicom` was running `headscale/headscale:latest`, with TCP
  8080 published as host port 8081 on all IPv4 and IPv6 interfaces and UDP
  3478 published on all interfaces. `headscale-nerdhirn` used a
  digest-qualified image reference and published UDP 3478 as host port 3479
  on all interfaces. Both containers reported the same image ID during this
  inventory, and both reported Headscale v0.27.0. The `latest` tag is
  mutable; record the resolved digest and intended version before upgrades.
  Confirm the intended exposure of the ResiCom TCP listener and both STUN
  listeners before changing these bindings; they are broader than
  loopback-only.
- NPM Plus and CrowdSec were healthy/running. NPM Plus published 80/443 on
  all interfaces and its admin port as `127.0.0.1:8181`; CrowdSec's API was
  bound to `127.0.0.1:7422`. Tailscale Serve reported the NPM admin path as
  tailnet-only and also reported a separate tailnet-only listener on port
  8888 forwarding to `localhost:8888`; confirm the owner and intended use of
  that additional listener before changing it. Swarmsail is now served at
  `https://<do2-tailscale-hostname>/` on tailnet-only HTTPS port 443, proxied
  to `http://127.0.0.1:8787`; this route was verified from an authorized
  tailnet client on 2026-10-19.
- The root filesystem was 78% used (6.8 GiB of 8.7 GiB, about 2.0 GiB free)
  and inode use was 14%. Docker reported about 904 MiB of images and 44 MiB
  of build cache. Continue treating disk capacity as a maintenance concern.

This snapshot confirms container health and local deployment state only. It
does not establish external DNS correctness or Headscale enrollment. The
tailnet-only Tailscale Serve route was subsequently verified from an
authorized client: `https://<do2-tailscale-hostname>/` returned the Swarmsail
UI and `/healthz` returned `{"status": "ok"}`. This does not imply public
internet exposure; verify split-horizon routing and direct, non-proxied DNS
separately if a public hostname is introduced.

## First-response inventory

Use the local SSH alias or an approved address. These commands are read-only:

```sh
HOST='<do2-host>'
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 root@${HOST}"

$SSH 'hostname; date -Is; uname -a; df -hT; df -ih'
$SSH 'docker ps -a --format "table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}"'
$SSH 'docker network inspect npm-net --format "{{range .Containers}}{{.Name}} {{.IPv4Address}}{{println}}{{end}}"'
$SSH 'systemctl --no-pager --type=service --state=running | grep -Ei "docker|resilio|tailscale" || true'
$SSH 'readlink -f /opt/swarmsail/current; systemctl status --no-pager swarmsail.service'
$SSH 'tailscale serve status'
$SSH 'docker system df; du -xhd1 /opt/headscale /opt/swarmsail /var/lib/docker 2>/dev/null | sort -h | tail -30'
```

Do not treat `Up` or a green Docker health check as complete verification.
Continue with the service-specific checks below.

## Files and data that must be preserved

Back up the relevant paths before changing or upgrading them. Keep the backup
off the host when practical, and verify its checksum.

```text
/opt/headscale/docker-compose.yml
/opt/headscale/headscale/resicom/config/
/opt/headscale/headscale/resicom/data/
/opt/headscale/headscale/resicom/logs/
/opt/headscale/npm/data/
/opt/headscale/crowdsec/conf/
/opt/headscale/crowdsec/data/
/opt/swarmsail/data/swarmsail.db
/opt/swarmsail/data/swarmsail.env
/opt/swarmsail/data/node-inventory.json
/opt/swarmsail/data/headscale/nerdhirn/
/opt/swarmsail/releases/<release>/
/etc/resilio-sync/config.json
/var/lib/tailscale/tailscaled.state
```

Never publish these files in Git or chat: `swarmsail.env`, Headscale private
keys, CrowdSec credential files, NPM secrets, Tailscale state, or databases
containing device/user metadata.

## Recreate the host

Recreation is a staged operation. Restore data before starting services, and
use the exact release and configuration captured from the old host.

1. Provision an Ubuntu host with sufficient disk for Docker layers, logs,
   databases, backups, and growth. The observed 8.7 GiB root disk is a
   capacity warning, not a recommended baseline.
2. Install Docker Engine and the Compose plugin. Create the `npm-net` external
   network:

   ```sh
   docker network create npm-net
   ```

3. Restore `/opt/headscale` and `/opt/swarmsail` with ownership and
   permissions preserved. Restore the Swarmsail release tree and make
   `/opt/swarmsail/current` point to the selected release.
4. Restore the sanitized-equivalent configuration files, then supply secrets
   through protected files or environment files with restrictive permissions.
5. Start the Headscale/NPM/CrowdSec project from its directory:

   ```sh
   cd /opt/headscale
   docker compose config
   docker compose up -d
   ```

6. Start the managed Swarmsail Headscale project using the restored release’s
   `docker-compose.managed.yml` and its documented environment. Start the
   containerized Swarmsail control plane using the release’s
   `compose.swarmsail.yml` (or the exact Compose file recorded by the backup).
   Enable/start `resilio-sync.service` and `tailscaled.service` only after
   their state/configuration has been restored. `swarmsail.service` exists as
   a legacy host-process unit but was disabled/inactive at capture time; do
   not start it alongside the container without confirming the intended
   deployment mode and port ownership.
7. Reapply the private Tailscale Serve configuration only after confirming
   the local listeners. Do not expose the NPM admin UI publicly.
8. Restore DNS records as DNS-only/direct records, then run the verification
   section. Do not cut traffic over until both Headscale endpoints and the
   reverse-proxy certificate path work.

The repository’s root Compose file is not a complete recreation input: it
does not include the live Swarmsail application, the Nerdhirn managed stack,
the current NPM admin binding, or the current CrowdSec configuration.

## Normal operations

### Headscale

The two instances use separate configuration/data trees and separate address
spaces. Both use SQLite and WAL; the live configuration also defines MagicDNS,
DERP-map retrieval, and TLS termination at NPM. Preserve each instance’s
database and noise/private-key material together. Never initialize a new empty
database over an existing data directory.

Useful commands:

```sh
cd /opt/headscale
docker compose ps
docker compose logs --tail=200 headscale-resicom
docker exec headscale-resicom headscale nodes list
docker exec headscale-nerdhirn headscale nodes list
docker inspect headscale-resicom --format '{{json .HostConfig.LogConfig}}'
```

Use the corresponding instance’s documented public URL when testing a client
login. A working container alone does not prove that the client control path,
TLS, DNS, or enrollment path is working.

### Swarmsail control plane

The active container uses the release selected by `/opt/swarmsail/current`,
uses `/opt/swarmsail/data/swarmsail.db`, and reads its environment from
`/opt/swarmsail/data/swarmsail.env`. It mounts the Docker socket for its
integration functions and mounts the selected release’s `instances.json`
read-only. A legacy `swarmsail.service` also exists, but was disabled and
inactive at capture time.

The UI is exposed to the tailnet using Tailscale Serve on HTTPS port 443.
Clients must be connected to the tailnet, and the tailnet policy must allow
TCP 443 to this host. The current client policy did not allow the custom Serve
ports 81, 8443, or 8888, so use the default HTTPS URL without a port suffix.
The backend process currently listens on `0.0.0.0:8787`; Serve proxies to
loopback, but this does not by itself prevent direct access to port 8787 from
other networks. Confirm the host/cloud firewall and intended NPM upstream
before restricting that listener.

#### Swarmsail-only release and SQLite migration

Use an immutable release directory and build the Swarmsail image from that
release. The active `compose.swarmsail.yml` contains only the `swarmsail`
service. Do not use `swarmsail_deploy.py apply` or a whole-project
`docker compose up`: its managed Headscale project is separate and may
recreate Headscale services. Do not stop or recreate Headscale, NPM Plus,
CrowdSec, cloudflared, Docker networks, or Tailscale Serve routes.

The Crew/matey administration release adds no SQLite tables or columns. Keep
using `/opt/swarmsail/data/swarmsail.db` without a schema migration. Before
replacing the control-plane container, create and verify a SQLite-aware
backup; do not `cp` only the live database file while WAL is active.

From the source checkout, choose a unique lowercase release ID, for example
`<release-id>`, and copy the current release on `do2`:

```sh
HOST='root@do2.risk-mermaid.ts.net'
RELEASE='<release-id>'
PREVIOUS="$(ssh -o BatchMode=yes "$HOST" 'readlink -f /opt/swarmsail/current')"
ssh -o BatchMode=yes "$HOST" \
  "mkdir -p '/opt/swarmsail/releases/$RELEASE' && cp -a '$PREVIOUS/.' '/opt/swarmsail/releases/$RELEASE/'"
scp swarmsail_web.py "$HOST:/opt/swarmsail/releases/$RELEASE/swarmsail_web.py"
```

On `do2`, use Python's SQLite backup API and verify the resulting database:

```sh
RELEASE='<release-id>'
BACKUP="/opt/swarmsail/backups/swarmsail-${RELEASE}.db"
mkdir -p /opt/swarmsail/backups
python3 - "$BACKUP" <<'PY'
import sqlite3
import sys

source = sqlite3.connect("/opt/swarmsail/data/swarmsail.db")
backup = sqlite3.connect(sys.argv[1])
source.backup(backup)
assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
backup.close()
source.close()
PY
```

Validate the release and available disk before building. Save the current
image under a unique rollback tag, build only `swarmsail:local`, then
recreate only the Swarmsail service without dependencies:

```sh
RELEASE='<release-id>'
PROJECT='20260902t043000z-value-only-fade'
RELEASE_DIR="/opt/swarmsail/releases/${RELEASE}"
docker compose -p "$PROJECT" -f "$RELEASE_DIR/compose.swarmsail.yml" config --quiet
df -h /
docker tag swarmsail:local "swarmsail:rollback-${RELEASE}"
docker build -t swarmsail:local "$RELEASE_DIR"
docker compose -p "$PROJECT" -f "$RELEASE_DIR/compose.swarmsail.yml" \
  up -d --no-deps --force-recreate swarmsail
docker inspect --format '{{.State.Health.Status}}' swarmsail
curl -fsS http://127.0.0.1:8787/healthz
```

Run the symlink commands only after the new container is healthy and the UI/API
checks pass. Preserve the old release, rollback image, and database backup
until the observation period ends. If rollback is needed, retag the retained
image as `swarmsail:local`, recreate only the Swarmsail service from the
previous release's Compose file, and restore the previous release symlink.
This application-only update does not restore deleted Headscale users or
nodes; those require a separate, verified Headscale data recovery.

```sh
ln -s "$RELEASE_DIR" "/opt/swarmsail/current.$RELEASE"
mv -Tf "/opt/swarmsail/current.$RELEASE" /opt/swarmsail/current
```

For rollback, use the previously recorded release path and its matching image
tag:

```sh
PREVIOUS='/opt/swarmsail/releases/<previous-release>'
docker tag "swarmsail:rollback-${RELEASE}" swarmsail:local
docker compose -p "$PROJECT" -f "$PREVIOUS/compose.swarmsail.yml" \
  up -d --no-build --no-deps --force-recreate swarmsail
ln -s "$PREVIOUS" "/opt/swarmsail/current.rollback.$RELEASE"
mv -Tf "/opt/swarmsail/current.rollback.$RELEASE" /opt/swarmsail/current
```

The deployed dashboard uses a persistent left navigation rail with a Crew
selector, separate setup, access-policy, inventory, and security destinations,
and a matey filter on each Crew's device table. The navigation is available on
both the Captain's console and CrowdSec page. The UI retains its light
sky-blue palette, coordinated dark mode, and responsive layout. Dark mode is
the default for first-time dashboard and CrowdSec page visitors; an explicitly
saved theme choice in browser local storage still takes precedence.

```sh
readlink -f /opt/swarmsail/current
docker inspect --format '{{json .Mounts}}' swarmsail
docker inspect --format '{{json .State.Health}}' swarmsail
docker logs --tail=200 swarmsail
docker exec swarmsail sh -c 'wget -qO- http://${SWARMSAIL_HOST:-127.0.0.1}:${SWARMSAIL_PORT:-8787}/healthz'
tailscale serve status
tailscale serve --bg --https=443 http://127.0.0.1:8787
curl -fsS https://<do2-tailscale-hostname>/healthz
```

Verify the rendered UI/API and the inventory refresh, not only the container
health status. Keep database backups before release changes.
Open `https://<do2-tailscale-hostname>/` in a browser on a tailnet-connected
client. To remove only this route, run
`tailscale serve --https=443 off` on `do2`; this leaves the other Serve routes
intact.

To roll back the UI-only update, restore the saved image tag, recreate only
the Swarmsail control-plane service from the retained release, then restore
the release symlink:

```sh
docker tag swarmsail:rollback-dark-default-20261020t022000z swarmsail:local
docker compose -p 20260902t043000z-value-only-fade \
  -f /opt/swarmsail/releases/20261020T021000Z-dashboard-sky-blue/compose.swarmsail.yml \
  up -d --no-build --force-recreate swarmsail
docker inspect --format '{{.State.Health.Status}}' swarmsail
ln -s /opt/swarmsail/releases/20261020T021000Z-dashboard-sky-blue /opt/swarmsail/current.rollback
mv -Tf /opt/swarmsail/current.rollback /opt/swarmsail/current
```

Remove `swarmsail:rollback-dark-default-20261020t022000z` only after the new UI has
passed its observation period and rollback is no longer needed.

### NPM Plus and CrowdSec

NPM Plus owns public ports 80/443 and stores its database/certificates under
`/opt/headscale/npm/data`. Its admin port is bound to loopback and is reached
through the private Tailscale Serve path. CrowdSec reads NPM logs through a
read-only mount and exposes its local API on loopback only.

```sh
docker logs --tail=200 npmplus
docker logs --tail=200 crowdsec
docker exec crowdsec cscli decisions list
tailscale serve status
```

Inspect the relevant NPM proxy-host configuration before changing upstream
ports or hostnames. Keep the NPM database and certificate material together.

## Upgrade procedure

1. Record the inventory, current release path, image digests, and disk space.
2. Create and verify backups of the affected configuration, databases, keys,
   and bind-mounted data. Preserve the current Compose files.
3. Read the release notes for Headscale, NPM Plus, CrowdSec, Docker, and the
   Swarmsail application. Do not use `latest` as a rollback identifier; record
   the resolved image digest before pulling.
4. Upgrade one component or stack at a time during an approved window. For
   the Compose stack, validate first:

   ```sh
   cd /opt/headscale
   docker compose config
   docker compose pull <service>
   docker compose up -d <service>
   ```

5. Verify logs, health, the control-server endpoint, client registration/list
   behavior, public TLS, CrowdSec log ingestion, and private NPM admin access.
6. Upgrade the Swarmsail release by deploying a new release directory,
   running its tests/migrations as documented by that release, switching
   `/opt/swarmsail/current` only after validation, and recreating the active
   Swarmsail container from the release’s Compose file. Keep the previous
   release available until verification is complete.
7. Recheck disk usage and Docker logs after the upgrade. The small root disk
   leaves little room for overlapping image generations.

## Troubleshooting

### Container is running but clients cannot connect

Check the public DNS answer, certificate chain, NPM proxy-host upstream, and
the Headscale `server_url`/listen settings. Test from an authorized external
network and from the host. Confirm DNS is direct, not proxied. Then inspect
the relevant Headscale and NPM logs by the same timestamp.

### Swarmsail UI is stale or unhealthy

Check the selected release symlink, the database/environment mounts, container
health output, and the inventory timer or refresh process. Confirm that the
Headscale socket/API command works from the container and that the Docker
socket mount has not been removed. Do not start the disabled legacy
`swarmsail.service` merely because it exists.

### NPM or CrowdSec fails after a change

Run `docker compose config`, inspect the external `npm-net` network, and check
that NPM log paths are still mounted read-only into CrowdSec. Verify that NPM
has the expected public bindings and that CrowdSec’s local API remains bound
to loopback.

### Disk pressure

Use [the disk-analysis runbook](analyze-disk-space.md) before cleanup. Capture
`df`, Docker usage, container log sizes, and deleted-open files first. Do not
delete Docker overlay files, Headscale databases, CrowdSec data, NPM data,
Resilio state, or synchronized content as a shortcut. The live Headscale
container has bounded `json-file` logging (`50m`, three files, compression),
but existing historical files still need separate retention decisions.

## Verification checklist

Record the results after recreation or upgrade:

```sh
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker network inspect npm-net >/dev/null
systemctl is-active docker resilio-sync tailscaled
docker inspect --format '{{json .State.Health}}' swarmsail
docker exec headscale-resicom headscale nodes list
docker exec headscale-nerdhirn headscale nodes list
tailscale serve status
df -hT /
```

Also verify, using approved non-secret values:

- Both Headscale client-facing URLs resolve to the intended host and pass TLS.
- A known test client can authenticate/connect to each intended tailnet.
- NPM Plus serves the expected proxy hosts and its admin UI is private.
- CrowdSec is consuming NPM logs and has a working local API.
- Swarmsail displays current organizations/devices and refreshes inventory.
- DNS remains direct/DNS-only and no proxying was enabled.
- Backups exist, are readable, and have verified checksums.

## Rollback and cleanup

- Compose/image rollback: restore the prior Compose file and image digest,
  then run `docker compose up -d <service>` after validating the config.
- Swarmsail rollback: point `/opt/swarmsail/current` back to the previous
  retained release, restore only the database/config backup required by the
  release’s compatibility rules, and recreate the active container. Leave the
  legacy host-process unit disabled unless that deployment mode is explicitly
  selected.
- Configuration rollback: restore the pre-change file, validate it, and
  restart only the affected service.
- Do not delete old releases, backups, logs, images, or databases until the
  new state has passed verification and the retention decision is explicit.

## Unresolved or intentionally local details

- Exact public hostnames, IP addresses, DNS zone names, credentials, private
  keys, and enrollment values are intentionally omitted.
- The live host contains historical backups and many Swarmsail release
  directories; the selected release and backup retention policy should be
  confirmed during each maintenance event.
- The root repository Compose file should be reconciled with this operational
  runbook in a future task if a reproducible deployment artifact is desired.
