# Swarmsail CrowdSec and Caddy OrbStack test

## Purpose

Deploy an isolated local Swarmsail control plane, Headscale, Caddy, and CrowdSec stack. Verify that Caddy loads its CrowdSec bouncer, CrowdSec parses Caddy access logs, and a CrowdSec decision blocks a request. This test stack does not replace the production `swarmsail-local` project or the existing E2E project. It binds only `127.0.0.1:18787`, `127.0.0.1:19080`, and `127.0.0.1:19443`.

## Prerequisites

- OrbStack running with Docker context `orbstack`.
- Docker Compose and Python 3.
- Repository root as the current directory.

## Deploy

Test data lives outside the repository at `~/Library/Application Support/Swarmsail/crowdsec-test/`. Create it once and seed the test-only Swarmsail inventory:

```sh
export SWARMSAIL_CROWDSEC_TEST_DIR="$HOME/Library/Application Support/Swarmsail/crowdsec-test"
mkdir -p "$SWARMSAIL_CROWDSEC_TEST_DIR"
chmod 700 "$SWARMSAIL_CROWDSEC_TEST_DIR"
python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["SWARMSAIL_CROWDSEC_TEST_DIR"])
config = root / "instances.json"
if not config.exists():
    config.write_text(json.dumps({
        "headscale": {"instances": [{
            "id": 1,
            "web_port": 18081,
            "stun_port_udp": False,
            "ipv4_prefix": "100.64.0.0/26",
            "server_url": "https://headscale.test",
            "management_mode": "adopted",
        }]},
        "network": {"name": "swarmsail-crowdsec-test", "driver": "bridge"},
        "npm": {"enabled": False},
        "crowdsec": {"enabled": True, "required_for_public_hosts": True},
        "caddy": {"enabled": True},
    }, indent=2) + "\n")
    config.chmod(0o600)
PY
umask 077
touch "$SWARMSAIL_CROWDSEC_TEST_DIR/caddy-bouncer-api-key"
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  up -d --build control-plane headscale-test crowdsec
```

Compose runs the one-shot `prepare-caddy-logs` service before CrowdSec, creating the shared access-log file so CrowdSec can attach its acquisition on first startup.

Wait for CrowdSec startup and verify its API responds before generating a test-only bouncer key:

```sh
until docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  exec -T crowdsec cscli lapi status >/dev/null 2>&1; do sleep 2; done
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  exec -T crowdsec cscli bouncers add caddy-bouncer --output raw \
  > "$SWARMSAIL_CROWDSEC_TEST_DIR/caddy-bouncer-api-key"
chmod 600 "$SWARMSAIL_CROWDSEC_TEST_DIR/caddy-bouncer-api-key"
test -s "$SWARMSAIL_CROWDSEC_TEST_DIR/caddy-bouncer-api-key"
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  up -d --build caddy
```

Do not print the key file or copy the key into Compose, logs, or the repository. The Compose secret is local to this test stack.

## Verify

The Swarmsail health endpoint should return `{"status":"ok"}`:

```sh
curl -fsS http://127.0.0.1:18787/healthz
```

Wait for Caddy to start, then confirm the proxied Headscale health endpoint over local internal TLS:

```sh
curl -kfsS --resolve headscale.test:19443:127.0.0.1 \
  https://headscale.test:19443/health
```

Confirm Caddy is built with the CrowdSec module and CrowdSec has parsed Caddy logs:

```sh
docker exec swarmsail-crowdsec-test-caddy /usr/bin/caddy list-modules | grep http.handlers.crowdsec
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  exec -T crowdsec cscli metrics
```

In acquisition metrics, `file:/var/log/caddy/access.log` must show `parsed` greater than zero. To test enforcement, start the verification client, obtain its Docker IP, request the upstream successfully, add a temporary CrowdSec decision for that test-client IP, and confirm Caddy returns 403:

```sh
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  --profile verification up -d test-client
CLIENT_IP=$(docker inspect --format '{{(index .NetworkSettings.Networks "swarmsail-crowdsec-test").IPAddress}}' \
  swarmsail-crowdsec-test-client)
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  exec -T test-client curl -kfsS --resolve "headscale.test:443:$(docker inspect --format '{{(index .NetworkSettings.Networks "swarmsail-crowdsec-test").IPAddress}}' swarmsail-crowdsec-test-caddy)" \
  https://headscale.test/health
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  exec -T crowdsec cscli decisions add --ip "$CLIENT_IP" --duration 5m --reason swarmsail-caddy-test
sleep 4
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  exec -T test-client curl -k -sS -o /dev/null -w '%{http_code}\n' \
  --resolve "headscale.test:443:$(docker inspect --format '{{(index .NetworkSettings.Networks "swarmsail-crowdsec-test").IPAddress}}' swarmsail-crowdsec-test-caddy)" \
  https://headscale.test/health
```

The first request should return the Headscale health response; the request after adding the decision should return HTTP 403. Confirm parsing again after both requests. Delete only the test decision and stop/remove the temporary verification client:

```sh
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  exec -T crowdsec cscli decisions delete --ip "$CLIENT_IP"
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml \
  --profile verification rm -sf test-client
```

Caddy's default logger is set to `ERROR` because the bouncer module logs its configured API key at `INFO`. Explicit site access logging remains enabled. Verify the secret was not written to the Caddy container's logs without printing either value:

```sh
python3 - <<'PY'
import os
import subprocess
from pathlib import Path

key = (Path(os.environ["SWARMSAIL_CROWDSEC_TEST_DIR"]) / "caddy-bouncer-api-key").read_text().strip()
logs = subprocess.run(
    ["docker", "logs", "swarmsail-crowdsec-test-caddy"],
    check=True, capture_output=True, text=True,
).stdout
if not key or key in logs:
    raise SystemExit("Caddy test secret is empty or appears in container logs")
print("Caddy bouncer key is absent from container logs")
PY
```

## Rollback and cleanup

To stop the test stack while preserving its test data and bouncer key:

```sh
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml down
```

To delete only this dedicated test stack and its named volumes after verification, first ensure `docker compose ls` identifies `swarmsail-crowdsec-test`, then run:

```sh
docker compose -p swarmsail-crowdsec-test -f docker-compose.crowdsec-test.yml down --volumes
```

Remove `$SWARMSAIL_CROWDSEC_TEST_DIR` manually only if the test inventory and local bouncer key are no longer needed. This does not alter the separate production `swarmsail-local` state.

## Assumptions

- The test Headscale endpoint intentionally uses Caddy's local internal certificate; `curl -k` is limited to this isolated test.
- The temporary test decision applies only to the verification container's private Docker IP and is removed after the check.
- This stack verifies the HTTP bouncer and Caddy access-log acquisition, not AppSec or direct UDP/STUN protection.
