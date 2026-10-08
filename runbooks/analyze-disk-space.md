# Analyze disk space on a Swarmsail host

## Purpose

Provide a read-only, repeatable procedure for finding what is consuming space on a Swarmsail/Headscale host before any cleanup or service restart.

This runbook was prepared after checking a host whose root filesystem was 91% full. The observed largest consumers were Docker container logs, retained systemd journal data, Docker storage, APT cache, and Resilio Sync state/logs. Values are examples from that inspection and will change over time.

## Prerequisites

- Authorized SSH access as `root` to the target host.
- A maintenance window before restarting Docker, Resilio Sync, or other services.
- Confirm the target host identity before making any change.
- Do not record credentials, tokens, private keys, or full private service addresses in this runbook.

Use a placeholder for the host in commands:

```sh
HOST='<host>'
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 root@${HOST}"
```

## Read-only inventory

Run the following without modifying the host:

```sh
$SSH 'hostname; date; df -hT; df -ih'
$SSH 'findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS | head -100'
$SSH 'du -xhd1 / 2>/dev/null | sort -h'
$SSH 'du -xhd1 /var 2>/dev/null | sort -h'
$SSH 'du -xhd1 /var/lib 2>/dev/null | sort -h'
$SSH 'du -xhd1 /var/log 2>/dev/null | sort -h'
$SSH 'find /var /usr /root /tmp /opt /sync -xdev -type f -printf "%s %p\\n" 2>/dev/null | sort -n | tail -50 | numfmt --field=1 --to=iec --suffix=B'
$SSH 'lsof +L1 2>/dev/null | head -100'
```

Expected results:

- `df -hT` identifies the full filesystem and distinguishes mounted filesystems from directories on the root filesystem.
- `du -x` avoids counting filesystems mounted below `/` twice.
- `lsof +L1` identifies deleted files that still consume blocks because a process has them open.
- A large difference between `df` and `du` commonly indicates deleted-open files, filesystem reservations, or data hidden below a mount point.

## Docker-specific inspection

Run these commands if `/var/lib/docker` is significant:

```sh
$SSH 'docker system df'
$SSH 'docker ps -a --size --format "{{.ID}}\\t{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Size}}"'
$SSH 'for c in $(docker ps -aq); do n=$(docker inspect -f "{{.Name}}" "$c" | sed "s#^/##"); p=$(docker inspect -f "{{.LogPath}}" "$c"); s=$(stat -c %s "$p" 2>/dev/null || echo 0); printf "%s\\t%s\\t%s\\n" "$s" "$n" "$p"; done | sort -n | numfmt --field=1 --to=iec --suffix=B'
$SSH 'du -xhd1 /var/lib/docker 2>/dev/null | sort -h'
```

Interpretation:

- `docker system df` reports reclaimable images, containers, volumes, and build cache; do not assume all overlay storage is reclaimable.
- Map large `*-json.log` files back to container names before changing logging.
- Never remove files directly from Docker's overlay or container directories while Docker is running.
- The repository's Compose file uses persistent bind mounts and an external network; verify the actual deployment before proposing prune or recreation commands.

## Logging and service inspection

Check journal, host logs, and service configuration:

```sh
$SSH 'journalctl --disk-usage; journalctl --list-boots --no-pager | tail -20'
$SSH 'du -ah /var/log 2>/dev/null | sort -h | tail -50'
$SSH 'systemctl --no-pager --type=service --state=running | grep -Ei "docker|resilio|headscale|crowdsec|npm" || true'
$SSH 'systemctl cat <service> 2>/dev/null'
$SSH 'docker logs --tail 200 <container>'
```

For unusually large logs, inspect recent lines and timestamps first. Determine whether the volume is caused by an error loop, access logging, expected traffic, or a missing rotation policy.

## Resilio Sync considerations

If Resilio Sync appears in the inventory:

```sh
$SSH 'du -xhd2 /var/lib/resilio-sync 2>/dev/null | sort -h | tail -40'
$SSH 'lsof +L1 2>/dev/null | grep -i resilio || true'
$SSH 'systemctl status --no-pager resilio-sync 2>/dev/null || true'
```

A deleted-open log will not release its space until the owning process closes the file. Restarting Resilio Sync is service-impacting and requires explicit approval or an approved maintenance window. Do not delete its databases or synchronized content as a space-recovery shortcut.

## Approval-gated cleanup options

Do not perform these actions during analysis. Present the exact target and expected recovery first:

1. Rotate or truncate a confirmed oversized Docker log, then configure a bounded Docker logging policy and verify the affected container.
2. Vacuum old systemd journal entries using an approved size or age limit, then verify journal operation.
3. Remove only confirmed-unused APT package archives or Docker build cache.
4. Restart Resilio Sync only when the deleted-open file is confirmed and service impact is acceptable.
5. Investigate and fix the source of repeated Headscale/CrowdSec logging before recurring cleanup.

Avoid broad recursive deletion, Docker overlay manipulation, removal of Resilio databases, and deletion of synchronized data.

## Applied Headscale log policy

The `headscale-resicom` Compose service now uses Docker's `json-file` driver with:

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "3"
    compress: "true"
```

This policy applies when the container is created or recreated. The active log remains plain text; rotated logs are compressed. Recreating the container does not compress the prior active log, so preserve or download that file separately before removing it.

Verify the deployed policy with:

```sh
docker inspect -f '{{json .HostConfig.LogConfig}}' headscale-resicom
docker ps --filter name=headscale-resicom --format '{{.Names}}\t{{.Status}}'
```

The previous active log from the 2026-10-08 rotation was preserved on the host as:

```text
/opt/headscale/headscale/resicom/logs/headscale-resicom-json.log.20261008T030948Z
```

Download it with an authorized SCP client, using the actual host alias or address locally:

```sh
scp root@<host>:/opt/headscale/headscale/resicom/logs/headscale-resicom-json.log.20261008T030948Z .
```

After confirming the download and any required retention, remove the preserved copy only with explicit approval. The Compose backup created before the change is stored alongside the deployment as `docker-compose.yml.pre-log-rotation-<timestamp>`.

## Verification after an approved change

Record before-and-after values and confirm service behavior:

```sh
$SSH 'df -hT /; docker ps --format "table {{.Names}}\\t{{.Status}}"'
$SSH 'journalctl --disk-usage'
$SSH 'systemctl is-active <affected-service>'
$SSH 'docker logs --since 5m <affected-container> 2>&1 | tail -100'
$SSH 'curl -fsS http://127.0.0.1:<port>/<health-path>'
```

For Headscale, verify the relevant administrative/API or client-facing health check used by the deployment, not only that the container is running. Confirm that DNS remains direct to the hosting server and is not proxied.

## Rollback and cleanup

- Configuration changes: restore the saved, sanitized configuration and restart only the affected service during an approved window.
- Log rotation or vacuuming: deleted historical logs are generally not recoverable; retain required evidence before cleanup.
- Docker changes: preserve the Compose/configuration files and verify bind-mounted data before recreating a container.
- Resilio restart: start the service again if it does not return automatically, then verify synchronization status and recent logs.
- Remove temporary diagnostic output from the host unless it is part of an approved incident record.

## Unresolved assumptions

- The target host may use a different service name, Docker Compose project, or logging driver; inspect live state before using examples.
- Disk usage changes continuously, so rerun the inventory immediately before cleanup.
- A large log identifies a symptom, not necessarily the root cause; correlate timestamps with service errors and traffic.
