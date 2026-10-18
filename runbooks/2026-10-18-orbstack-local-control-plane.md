# Run Swarmsail in local OrbStack

## Purpose and initial state

Make the Swarmsail control plane visible in the local OrbStack project list. On 2026-10-18, Docker context `orbstack` had the `rawrarr-local` project but no Swarmsail project or containers, and local port 8787 was free. The existing Swarmsail/Headscale services on `do2` are separate. The repository root `docker-compose.yml` is a development example with host ports 80 and 443 and was not used.

## Local layout and deployment

Use the dedicated `docker-compose.orbstack.yml` project `swarmsail-local`. The web container listens only at `127.0.0.1:8787` on the Mac. Its SQLite database and `instances.json` live under `~/Library/Application Support/Swarmsail/`, outside the synced repository. Start with an empty instance list. No trial/subscription has been asserted for a real account, so this creates no subscriber Headscale container.

From the Swarmsail repository:

```sh
export SWARMSAIL_LOCAL_DATA_DIR="$HOME/Library/Application Support/Swarmsail"
mkdir -p "$SWARMSAIL_LOCAL_DATA_DIR"
chmod 700 "$SWARMSAIL_LOCAL_DATA_DIR"
python3 - <<'PY'
import json
from pathlib import Path
config = Path.home() / 'Library/Application Support/Swarmsail/instances.json'
with config.open('x', encoding='utf-8') as output:
    json.dump({'headscale': {'instances': []},
               'network': {'name': 'swarmsail-local', 'driver': 'bridge'},
               'npm': {'enabled': False}, 'crowdsec': {'enabled': False}}, output)
    output.write('\n')
PY
chmod 600 "$SWARMSAIL_LOCAL_DATA_DIR/instances.json"
docker compose -p swarmsail-local -f docker-compose.orbstack.yml config
docker compose -p swarmsail-local -f docker-compose.orbstack.yml up -d --build control-plane
```

The Python bootstrap line uses exclusive creation and is for first install only. For later runs, export the data path and use the Compose commands without the bootstrap lines.

## Verification and recovery

Confirmed `docker compose -p swarmsail-local -f docker-compose.orbstack.yml ps` and Docker health inspection report the control plane healthy; `curl -fsS http://127.0.0.1:8787/healthz` returns `{"status":"ok"}`, and `/api/state` reports zero organizations and zero Headscale instances. The actual Docker port binding is `127.0.0.1:8787`. A real invocation of `scripts.provision_subscriber.py` without `--apply` returned a planned instance while leaving the instance list empty. The container has no published remote interface and no access to the Docker socket. Its startup uses `umask 077` for newly created SQLite files; the initial database was also set to mode 600.

To stop only this local control plane, run `docker compose -p swarmsail-local -f docker-compose.orbstack.yml stop control-plane`. The node-local data directory is retained for restart or backup. Do not remove it to recover from a failed build; inspect Compose logs and correct the cause.

## Remaining work

The local container is a control plane, not a provisioned tailnet. Verified Rawrarr account eligibility, Headscale instance generation and deployment, HTTPS ingress, enrollment, and cross-tailnet device sharing are not yet wired to this Compose project.
