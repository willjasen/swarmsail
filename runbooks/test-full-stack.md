# Test the complete Swarmsail stack

## Purpose

Deploy a disposable Swarmsail control plane, Swarmsail-generated Headscale
instance, Caddy HTTPS ingress, and CrowdSec, then enroll two real Tailscale
containers and verify bidirectional tailnet connectivity. This is an integration
test, not a deployment against an existing server.

## Prerequisites

- Python 3.10 or newer, Docker with Linux containers, and Docker Compose v2 or
  newer. Docker Desktop and OrbStack provide a Linux VM on macOS.
- The Docker host must provide `/dev/net/tun` and permit `NET_ADMIN` and
  `NET_RAW` for the two disposable clients. No host Tailscale installation or
  host networking is needed.
- Internet access for initial image pulls, the Python image build, and CrowdSec's
  Caddy collection download. Allow up to ten minutes for the first build/start.
- Run from the repository root. No production configuration, DNS credentials,
  public domain, existing tailnet, or privileged host ports are needed.

## Run

```sh
SWARMSAIL_RUN_E2E=1 python3 -m unittest scripts.tests.test_full_stack -v
```

Without the environment variable, this test is skipped, including when running
the normal script tests:

```sh
python3 -m unittest discover -s scripts/tests -v
```

## Keep a visible stack in OrbStack

The default test removes the stack immediately after verification, so it will
not remain visible in OrbStack. To retain a successful deployment explicitly
on the OrbStack engine:

```sh
DOCKER_CONTEXT=orbstack SWARMSAIL_RUN_E2E=1 SWARMSAIL_E2E_KEEP=1 \
  python3 -m unittest scripts.tests.test_full_stack -v
```

All six containers remain under the `swarmsail-e2e-<random>` project printed at
the end. Generated configuration is retained in an owner-only directory under
`~/.local/state/swarmsail/e2e/`, outside the synced checkout. A failed test still
cleans up automatically. Each invocation creates a new independent project.
This is an inspection environment, not a production service or restart test.

The retained dashboard is published on a randomly allocated loopback port.
Find that port using the exact project and Compose file printed by the test:

```sh
docker --context orbstack compose --project-name <exact-test-project> \
  --file <retained-directory>/compose.json port control-plane 8787
```

Open `http://127.0.0.1:<reported-port>` to inspect Swarmsail. The clients remain
enrolled, with their TCP echo listeners on tailnet port 8088. To stop and remove
this retained environment:

```sh
docker --context orbstack compose --project-name <exact-test-project> \
  --file <retained-directory>/compose.json \
  down --volumes --remove-orphans --rmi local
```

After that succeeds, delete only the exact retained directory printed by the
test. Do not remove the parent state directory or other retained deployments.

## What is deployed and verified

The test calls Swarmsail's configuration and Compose generators with a fresh
managed instance. It builds the control-plane image using `Dockerfile.orbstack`.
Headscale uses the manager's default image (`0.29.5`); the test pins Caddy to
`2.11.7-alpine`, CrowdSec to `v1.8.1`, and Tailscale to `v1.104.1`.

Test-only overrides remove fixed container names, restart policies, and host
ports (except the loopback dashboard in keep mode). All resources belong to a
unique `swarmsail-e2e-<random>` Compose project.
Configuration lives in an owner-only temporary directory, with databases,
identities, certificates, and logs in project-scoped Docker volumes.

The test uses the generated Caddy hostname route with private-CA TLS instead of
public Cloudflare DNS-01. The stock Caddy image matches the production Caddy
version but does not need the Cloudflare plugin. The CA certificate is installed
in both clients, then their daemons are restarted to refresh Go's cached trust
roots. Certificate verification is never disabled. Docker DNS resolves
`headscale.test` and `swarmsail.test` directly to Caddy.

Each Tailscale client has its own internal Docker network. Only Caddy joins both
client networks and the backend network; Headscale and Swarmsail stay on the
backend. Headscale's embedded DERP is enabled through Caddy, and the public DERP
map is disabled. Thus the clients cannot exchange ordinary bridge-network
traffic or depend on public relays. Tailscale log upload is disabled.

The test requires all of the following before passing:

- Trusted HTTPS reaches Headscale and Swarmsail through Caddy.
- Swarmsail's `/healthz` is healthy and `/api/state` lists the managed instance.
- Both clients enroll using distinct, short-lived, single-use Headscale keys,
  reach the `Running` state, and receive distinct addresses in the instance's
  `100.64.0.0/26` pool. Headscale lists exactly those two nodes.
- Each client sends three ICMP probes to the other's tailnet IP and receives a
  successful ping result.
- Each client sends a unique payload to a TCP echo listener bound specifically
  to the other's tailnet IP on port 8088 and receives the exact payload back.
- CrowdSec's local API is available and its acquisition metrics show parsed
  Caddy access logs, not merely a running container.
- All six containers remain running at the end of the checks.

Enrollment keys are passed over stdin to mode-600 temporary files inside the
clients and deleted immediately after use. They are not put in process
arguments or printed. Command errors redact issued keys; raw service logs are
not dumped.

## Cleanup and failure recovery

The test registers cleanup before starting the stack. On success or an ordinary
test/setup failure, it runs Compose `down --volumes --remove-orphans --rmi local`,
removing its containers, networks, volumes, and locally built control-plane image.
The generated temporary files are also removed. Downloaded shared images and
Docker build cache are retained.

If the process is forcibly killed or Docker becomes unavailable during cleanup,
identify only the abandoned test project:

```sh
docker compose ls --all --filter 'name=swarmsail-e2e-'
docker ps -a --filter 'label=com.docker.compose.project=<exact-test-project>'
docker volume ls --filter 'label=com.docker.compose.project=<exact-test-project>'
docker network ls --filter 'label=com.docker.compose.project=<exact-test-project>'
```

If its generated `compose.json` still exists at the temporary path shown in test
output, remove that exact project:

```sh
docker compose --project-name <exact-test-project> \
  --file <temporary-directory>/compose.json \
  down --volumes --remove-orphans --rmi local
```

Otherwise, remove only the exact container, volume, network, and test-image IDs
identified by that project label/name, after confirming they are abandoned test
resources. Do not use a global Docker prune. Remove only that project's
temporary directory after its resources are gone.

## Scope and assumptions

Clients join the same Swarmsail-managed Headscale instance. This does not test
cross-instance sharing, production DNS/ACME, subscriber billing/provisioning, or
enrollment through the optional dashboard helper scripts. User and enrollment
key creation use the managed Headscale CLI.

The repository's Caddy deployment does not include a CrowdSec bouncer. This test
therefore verifies CrowdSec's Caddy log ingestion, not request blocking or AppSec
enforcement. The test overrides the manager's older NPM-specific CrowdSec
collection and acquisition mounts without changing production generation.
