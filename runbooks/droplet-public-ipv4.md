# Configure a droplet public IPv4

## Purpose

Record the droplet's globally routable IPv4 before generating public Caddy
ingress. Config generation validates the address and binding; the separate
DigitalOcean verifier checks host assignment and DNS. This does not provision
a droplet. Automatic DigitalOcean/provider provisioning is deferred.

## Prerequisites

- A user-provided droplet with a public IPv4 address.
- A node-local `instances.json` configuration.
- Public DNS A records configured to point directly to the droplet; do not
  enable record proxying.
- Host and provider firewall rules for the services that will be enabled.

## Configuration

Set the deployment role and the droplet's actual public IPv4. When Caddy is
enabled, set `caddy.listen_ip` to the same address:

```json
{
  "deployment": {
    "role": "droplet",
    "public_ipv4": "<droplet-public-ipv4>"
  },
  "caddy": {
    "enabled": true,
    "listen_ip": "<droplet-public-ipv4>"
  }
}
```

Merge these fields into the existing configuration; preserve its Headscale,
Caddy, CrowdSec, and network settings. Keep credentials in node-local secret
files, not in `instances.json`.

## Verification

Preview config validation without writing generated files:

```sh
python3 manage_headscale.py generate --config /path/to/instances.json
```

The command must reject missing, non-global, or non-IPv4 droplet addresses and
reject a Caddy listener that differs from `deployment.public_ipv4`. For a
valid configuration it prints a dry-run summary and does not write generated
files.

After reviewing the target configuration, generate the Compose and Caddy
files:

```sh
python3 manage_headscale.py generate --config /path/to/instances.json --apply
docker compose -f /path/to/docker-compose.yml config
```

Verify every public service hostname resolves directly to the configured
address:

```sh
python3 -m scripts.verify_droplet_ipv4 --config /path/to/instances.json
dig +short A <service-hostname>
```

The verifier compares `deployment.public_ipv4` with the address reported by
the DigitalOcean metadata service and requires each configured service
hostname's complete A-record set to contain only that address. The generated
Caddy port bindings must use the same IPv4. This procedure does not yet verify
firewall reachability, TLS issuance, or WebRTC ICE connectivity.

## Rollback and cleanup

Restore the previous `deployment` and Caddy listener values in the node-local
configuration, then regenerate the Compose/Caddy files. Do not remove the
droplet or its persistent Headscale/Caddy/CrowdSec data as part of this
rollback.

## Unresolved assumptions

- The first deployment uses a user-provided droplet; Swarmsail does not create
  or reserve a provider resource.
- Config validation checks address classification and binding consistency,
  not ownership or reachability.
- Required TCP/UDP ports depend on the chosen Headscale, ingress, STUN, and
  TURN configuration and must be specified before production deployment.
- The public coordinator/droplet protocol and browser-to-droplet WebRTC are
  not implemented.
