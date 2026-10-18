# Local OrbStack support stack

## Objective and starting state

Run the services Swarmsail needs on the MacBook in the `swarmsail-local` Compose project. The existing [local control plane](2026-10-18-orbstack-local-control-plane.md) was healthy on loopback with no Headscale instance. The active `do2` services are separate; this task creates fresh node-local state and does not copy their identities. Mac LAN address at the start was `192.168.128.210`. `rawrarr.willjasen.net` still resolved to the existing `100.84.23.71` tailnet host, so ordinary DNS did not yet reach the Mac LAN address.

## Deployment

Set `SWARMSAIL_LOCAL_DATA_DIR` to `~/Library/Application Support/Swarmsail` and `SWARMSAIL_LAN_IP` to the Mac LAN address. Keep this directory outside the synced repository, with mode 700 and sensitive files mode 600. `docker-compose.orbstack.yml` puts NPM Plus on LAN ports 80/8443, its admin UI on host loopback 8181, CrowdSec's local endpoint on loopback 7422, and Swarmsail's dashboard on loopback 8787. NPM and CrowdSec volumes are node-local. The initial NPM HTTPS certificate is self-signed and must not be treated as valid Headscale TLS.

The operator Headscale instance was created through `manage_headscale.py generate --apply` from the node-local `instances.json`. Its generated Compose file and Headscale config remain node-local. Use both Compose files together to run it in the existing `swarmsail-local` project:

```sh
export SWARMSAIL_LOCAL_DATA_DIR="$HOME/Library/Application Support/Swarmsail"
export SWARMSAIL_LAN_IP=<current-mac-lan-ip>
docker compose -p swarmsail-local -f docker-compose.orbstack.yml \
  -f "$SWARMSAIL_LOCAL_DATA_DIR/docker-compose.headscale.yml" up -d
```

The Headscale listener is initially internal to the private Docker network while a publicly trusted certificate is pending. This is an operator instance with no Rawrarr account or asserted trial/subscription. Its address pool is a fresh random `/26`; the host subnet mask does not by itself authorize sharing.

## HTTPS plan

Issue a certificate for `rawrarr.willjasen.net` with ACME DNS-01 through Cloudflare, using a node-local token file with Zone Read and DNS Edit for `willjasen.net`. `bash scripts/issue_lan_certificate.sh` previews; `bash scripts/issue_lan_certificate.sh --apply` obtains or renews it with an ephemeral lego container. Do not add a cloudflared container. DNS-01 does not require a public inbound connection to the Mac.

Once the certificate is issued, add the following lines to the node-local `headscale/local/config/config.yaml` and use the TLS Compose override:

```yaml
tls_cert_path: /certs/rawrarr.willjasen.net.crt
tls_key_path: /certs/rawrarr.willjasen.net.key
```

```sh
docker compose -p swarmsail-local -f docker-compose.orbstack.yml \
  -f "$SWARMSAIL_LOCAL_DATA_DIR/docker-compose.headscale.yml" \
  -f docker-compose.orbstack-headscale-tls.yml up -d headscale-rawrarr-local
```

NPM's LAN HTTPS listener was moved to 8443, freeing port 443 for Headscale. A DNS-only Cloudflare record and local DNS resolution must point clients at the Mac LAN IP. A private IP record will serve LAN clients only; off-LAN clients need an explicit reachable path.

## Verification and rollback

Verified four running containers: `swarmsail-local-control-plane-1` healthy, `headscale-rawrarr-local` running, `swarmsail-local-npmplus-1` responding on admin 8181 and LAN 8443, and `swarmsail-local-crowdsec-1` running. Headscale CLI `users list` and `nodes list` succeeded, and NPM reached Headscale `/health` over the private Docker network with HTTP 200. The dashboard `/healthz` returned `{"status":"ok"}` and `/api/state` listed the new managed instance. LAN port 443 was free after moving NPM. Before that move, a Rawrarr container reached NPM on the Mac LAN address, which proves the Docker-to-LAN path but does not yet verify the final Headscale endpoint.

After HTTPS activation, verify the certificate name and trust with `curl --resolve rawrarr.willjasen.net:443:<LAN_IP> https://rawrarr.willjasen.net/health` and then test ordinary DNS resolution. A service listening internally is not proof of LAN HTTPS.

To stop this new local stack, stop only the named services with `docker compose -p swarmsail-local -f docker-compose.orbstack.yml -f <node-local-generated-compose> stop <service>`; keep node-local config and database for recovery. Reverting the LAN listener change requires restoring the prior Compose bindings and recreating only the affected services.

## Remaining work

The Cloudflare token file was still absent at this milestone, so certificate issuance, native Headscale TLS, LAN 443 publishing, DNS change, and a final Rawrarr-to-Headscale HTTPS test remain. Subscriber eligibility-to-deployment automation and cross-tailnet routing are separate implementation work.
