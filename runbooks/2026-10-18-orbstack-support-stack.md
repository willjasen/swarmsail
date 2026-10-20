# Local OrbStack support stack

> Current ingress is Swarmsail-managed Caddy. See [the Caddy Headscale ingress runbook](2026-10-18-swarmsail-caddy-headscale-ingress.md); the NPM/native-TLS steps below describe the earlier rollout and are retained as recovery context.

## Objective and starting state

Run the services Swarmsail needs on the MacBook in the `swarmsail-local` Compose project. The existing [local control plane](2026-10-18-orbstack-local-control-plane.md) was healthy on loopback with no Headscale instance. The active `do2` services are separate; this task creates fresh node-local state and does not copy their identities. Mac LAN address at the start was `192.168.128.210`. `headscale.example.test` still resolved to the existing `100.84.23.71` tailnet host, so ordinary DNS did not yet reach the Mac LAN address.

## Deployment

Set `SWARMSAIL_LOCAL_DATA_DIR` to `~/Library/Application Support/Swarmsail` and `SWARMSAIL_LAN_IP` to the Mac LAN address. Keep this directory outside the synced repository, with mode 700 and sensitive files mode 600. `docker-compose.orbstack.yml` puts NPM Plus on LAN ports 80/8443, its admin UI on host loopback 8181, CrowdSec's local endpoint on loopback 7422, and Swarmsail's dashboard on loopback 8787. NPM and CrowdSec volumes are node-local. The initial NPM HTTPS certificate is self-signed and must not be treated as valid Headscale TLS.

The operator Headscale instance was created through `manage_headscale.py generate --apply` from the node-local `instances.json`. Its generated Compose file and Headscale config remain node-local. Use both Compose files together to run it in the existing `swarmsail-local` project:

```sh
export SWARMSAIL_LOCAL_DATA_DIR="$HOME/Library/Application Support/Swarmsail"
export SWARMSAIL_LAN_IP=<current-mac-lan-ip>
docker compose -p swarmsail-local -f docker-compose.orbstack.yml \
  -f "$SWARMSAIL_LOCAL_DATA_DIR/docker-compose.headscale.yml" up -d
```

The Headscale listener is initially internal to the private Docker network while a publicly trusted certificate is pending. This is an operator instance with no linked account or asserted trial/subscription. Its address pool is a fresh random `/26`; the host subnet mask does not by itself authorize sharing.

## HTTPS plan

Issue a certificate for `headscale.example.test` with ACME DNS-01 through Cloudflare, using a node-local token file with Zone Read and DNS Edit for the configured domain. `bash scripts/issue_lan_certificate.sh` previews; `bash scripts/issue_lan_certificate.sh --apply` obtains or renews it with an ephemeral lego container and uses public recursive resolvers because the OrbStack DNS resolver timed out. After renewal, recreate Headscale with the TLS Compose command below so it reloads the certificate. Do not add a cloudflared container. DNS-01 does not require a public inbound connection to the Mac.

Once the certificate is issued, add the following lines to the node-local `headscale/local/config/config.yaml` and use the TLS Compose override:

```yaml
tls_cert_path: /certs/headscale.example.test.crt
tls_key_path: /certs/headscale.example.test.key
```

```sh
docker compose -p swarmsail-local -f docker-compose.orbstack.yml \
  -f "$SWARMSAIL_LOCAL_DATA_DIR/docker-compose.headscale.yml" \
  -f docker-compose.orbstack-headscale-tls.yml up -d headscale-local
```

NPM's LAN HTTPS listener was moved to 8443, freeing port 443 for Headscale. A DNS-only Cloudflare record and local DNS resolution must point clients at the Mac LAN IP. A private IP record will serve LAN clients only; off-LAN clients need an explicit reachable path.

## Verification and rollback

Verified four running containers: `swarmsail-local-control-plane-1` healthy, `headscale-local` running, `swarmsail-local-npmplus-1` responding on admin 8181 and LAN 8443, and `swarmsail-local-crowdsec-1` running. Headscale CLI `users list` succeeded; Headscale config validation completed. The dashboard `/healthz` returned `{"status":"ok"}` and `/api/state` listed the new managed instance. The Let's Encrypt certificate has SAN `headscale.example.test`, and its private key is mode 600. Headscale now publishes `192.168.128.210:443` with native TLS. `curl --resolve headscale.example.test:443:192.168.128.210 https://headscale.example.test/health` returned HTTP 200 with trusted certificate verification from the host and from the local coordinator container. Ordinary public DNS still resolved to `100.84.23.71` during verification; the requested DNS-only A record to `192.168.128.210` remains for the operator to set.

After HTTPS activation, verify the certificate name and trust with `curl --resolve headscale.example.test:443:<LAN_IP> https://headscale.example.test/health` and then test ordinary DNS resolution. A service listening internally is not proof of LAN HTTPS.

To stop this new local stack, stop only the named services with `docker compose -p swarmsail-local -f docker-compose.orbstack.yml -f <node-local-generated-compose> stop <service>`; keep node-local config and database for recovery. Reverting the LAN listener change requires restoring the prior Compose bindings and recreating only the affected services.

## Remaining work

The Cloudflare token file was later corrected to one token line and mode 600. The initial ACME propagation attempt failed because the OrbStack recursive resolver timed out; using explicit public resolvers succeeded. Native Headscale TLS and the coordinator HTTPS route are now verified. Update the configured DNS-only A record to `192.168.128.210` and verify propagation before relying on ordinary hostname resolution. Subscriber eligibility-to-deployment automation and cross-tailnet routing are separate implementation work.
