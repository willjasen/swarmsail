# Provision an authenticated account-default tailnet

## Purpose and boundaries

The opt-in `swarmsail-provisioner` binary is a private v1 service, separate from
the unauthenticated operator dashboard. Rawrarr authenticates the user, derives
the account/fossil node ownership, then signs a short-lived service assertion.
All authenticated accounts, including free accounts, qualify. Trial/subscription
requirements of the old operator CLI do not apply to this API.

The initial hosting authority is do3. Rawrarr never receives Docker access.
New control planes use `https://<tailnet-id>.swarm.rawrarr.net`; the application
is separately hosted at `https://roarlay.rawrarr.net`.

**No deployment is automatic.** The operator separately installs direct,
unproxied wildcard DNS, DNS-challenge TLS/renewal, private HTTPS API ingress and
trust keys. The worker has no DNS provider integration. A raw Docker socket is
host-admin authority: the supervised worker is trusted on its node, not a
sandbox against the host administrator. The API process must have no Docker
socket/group, privileged worker configuration or subscriber private root keys.

The peer profile is currently gated on the matching Rawrarr userspace HTTPS
datapath fixture. `peer_profile_unavailable` is a real blocked state, not readiness.
Do not widen ACLs or set a flag to work around it. Pre-enrollment readiness checks
control-plane capability and installed policy; live peer connectivity is checked
after enrollment, not before the first node exists.

## Prerequisites

- Linux hosting node, Rust 1.90+, Python 3.10+, Docker/Compose, `dig`, and pinned Headscale
  `headscale/headscale:0.29.5`. Do not assume a newer CLI is compatible.
- A private, owner-only state directory outside the checkout/synced filesystem.
  SQLite ledger must be on a local filesystem, not shared between hosts/NFS.
- Verified `*.swarm.rawrarr.net` wildcard directly resolves to the hosting node's
  real public IPv4. Reject proxying and conflicting explicit A/AAAA/CNAME routes.
  Separate firewall/external ingress acceptance; a node-local probe is insufficient.
- Operator-managed wildcard TLS certificate and renewal, with Caddy configured
  to use it for generated tenant hostname routes. No account worker ACME secrets.
- Operator-prepared Caddy baseline, mounted **as a directory**, a private Unix
  admin socket, and `curl`, `cat` and Caddy commands in the ingress container.
  The existing generator's `admin off`/single-file mount is NOT reload-ready.
  Baseline SHA-256 protects global/operator routes. SwarmSail appends only its
  marked owned route section, verifies active host/upstream pairing and restores
  disk/active configuration on a failed reload. Unexpected drift blocks.
- Existing generations require explicit adoption, not automatic highest/first
  selection. No container/ACL migration, cross-host failover or provider creation.

## Build and configure (operator steps, not deployment authorization)

```sh
cargo build --release --locked --bin swarmsail-provisioner
install -d -m 700 /var/lib/swarmsail-provisioning
install -m 600 /dev/null /var/lib/swarmsail-provisioning/ledger.db
```

Install an independently created, 32-byte receipt encryption key at a mode-600
node-local path. Install only Rawrarr's Ed25519 SPKI **public** verification PEM
on this host; the PKCS#8 private signing key stays in Rawrarr. Never install
`tests/fixtures/provisioning-v1/private-test-only.pem`: it is a publicly known
RFC8032 test vector, not a service credential.

API settings, mode 600, outside Git:

```json
{
  "ledger": "/var/lib/swarmsail-provisioning/ledger.db",
  "issuer": "https://roarlay.rawrarr.net",
  "service": "rawrarr",
  "listen": "127.0.0.1:8788",
  "public_keys": {"service-key-1": "/etc/swarmsail/trust/service-key-1.pem"},
  "receipt_key_file": "/var/lib/swarmsail-provisioning/receipt.key",
  "tenant_capacity": 100
}
```

`tenant_capacity` is an explicit operator limit, not a free-account eligibility
rule. The example is not a validated host sizing recommendation. API binds
loopback only; terminate private HTTPS in separately installed ingress, with
verified client-side TLS and no redirect/HTTP fallback. Do not proxy operator
dashboard routes publicly.

Worker process settings use the same ledger/trust/receipt fields, plus:

```json
{
  "worker": {
    "repository": "/opt/swarmsail/release",
    "settings": "/etc/swarmsail/worker.json"
  }
}
```

Worker adapter settings, mode 600, contain operator-approved node-local paths:

```json
{
  "host_id": "do3",
  "config": "/var/lib/swarmsail/instances.json",
  "mapping_database": "/var/lib/swarmsail/swarmsail.db",
  "data_root": "/var/lib/swarmsail/tenants",
  "compose_project": "swarmsail-tenants",
  "public_ipv4": "<hosting-node-public-ipv4>",
  "ingress_container": "swarmsail-ingress",
  "ingress_file": "/var/lib/swarmsail/ingress/Caddyfile",
  "ingress_container_file": "/etc/caddy/Caddyfile",
  "ingress_admin_socket": "/run/caddy/admin.sock",
  "ingress_tls_certificate": "/run/tls/wildcard.crt",
  "ingress_tls_key": "/run/tls/wildcard.key",
  "ingress_baseline_sha256": "<sha256-of-exact-operator-baseline-bytes>"
}
```

Public IP/digest placeholders must be replaced only after independent operational
verification. Do not record production values or credentials in this repository.
TLS paths are container paths to the separately installed wildcard certificate
and private key, read by Caddy only. Explicit `tls` directives prevent implicit
account-triggered ACME issuance. Generated routes retain the existing generator's
CrowdSec enforcement/logging when configured; global/operator blocks are preserved.

Supervise these as separate processes with restrictive users/permissions:

```sh
target/release/swarmsail-provisioner api /etc/swarmsail/private-api.json
target/release/swarmsail-provisioner worker /etc/swarmsail/worker-process.json
```

The worker holds a lifetime node-local file lock; a second executor refuses to
start. A process restart reconciles durable phase/creation receipts. Do not
steal locks or start a second worker while old Docker operations may still run.
`Dockerfile.provisioner` packages only the unprivileged API, not a remotely
accessible Docker worker.

The API image uses UID/GID 65532. Prepare its mode-700 state directory and
mode-600 ledger, receipt key and API settings for that owner. If the worker uses
the same numeric UID on the host, give only that host worker Docker authority;
the API container must not mount the Docker socket, host worker settings,
tenant data or ingress configuration. Mount only its ledger directory, receipt
key, API settings and public trust files. SQLite creates WAL/SHM files in the
ledger directory, so a single-file ledger mount is insufficient. Different
worker/API filesystem owners are not supported by the current owner-only mode
checks; do not solve this with group/world-readable credentials. Loopback API
binding requires Linux host networking or HTTPS termination in the same network
namespace; simply publishing a bridge-network container port will not work.

## Private v1 contract

All JSON responses are `Cache-Control: no-store`. Pending/overload responses use
`Retry-After: 5`. Body limit is 8 KiB; admitted requests are bounded to eight,
queued allocation/enrollment jobs to 32. Identity/idempotency tombstones persist.

JWT algorithm is pinned EdDSA/Ed25519. Required claims:
`iss`, `aud: swarmsail-provisioning-v1`, `azp`, `sub: <account-uuid>`,
`scope: [<endpoint-scope>]`, integer `iat/nbf/exp`, and `jti`. Maximum lifetime
300 seconds, clock skew 30 seconds. `kid` selects the installed public key map.
Issuer/service must match configured values and ledger authority. No request
JWKS URL, payment assertion or callback. Enrollment also requires exact
`node_id: fossil-<24 lowercase hex>` and separate `setup_intent_id: <uuid>`.

| Method and path below `/private/v1/accounts/{account}` | Scope | Result |
| --- | --- | --- |
| POST `/default-tailnet:ensure`, body `{}` | `tailnet:ensure` | 202 pending or 200 freshly ready, never a key |
| GET `/default-tailnet` | `tailnet:read` | Owned metadata, 404 if absent |
| POST `/default-tailnet/enrollments` | `node:enroll` | 202 issuing, 201 first receipt or 200 identical safe replay |
| GET `/default-tailnet/enrollments/{id}` | `node:enroll` | Metadata only, never a key |
| POST `/default-tailnet/enrollments/{id}:replace`, body `{}` | `node:enroll` | Verified old-key revocation/expiry/use before one successor |
| POST `/default-tailnet/enrollments/{id}:recover`, body below | `node:recover` | Fresh owner/root-setup-authorized successor with a new setup UUID |
| GET `/default-tailnet/enrollment-requests/{original-request-id}` | `node:recover:lookup` | Fresh owner-authorized metadata-only predecessor discovery |
| GET `/default-tailnet/nodes/{fossil-id}` | `node:read` | Fresh authoritative device mapping, or 202 pending |

Every POST requires a canonical UUID `Idempotency-Key`. Enrollment body:
`{"node_id":"fossil-<24 hex>","setup_intent_id":"<uuid>"}`. Reuse exact body/key
after timeout. New/lost ensure request keys still resolve the same default, not
another generation. Another setup UUID cannot evade one-live-attempt-per-node.

Default metadata has `api_version:"1"`, account/tailnet UUIDs, positive generation,
operation UUID, `state: queued|provisioning|ready|degraded|blocked`, phase, reserved
server URL, readiness timestamps and structured error. A reserved URL is NOT
enrollment readiness. Once reservation is materialized, private metadata adds
`node_address_policy: {version:"1",ipv4_prefix:"<actual-/26>",ipv6_prefix:null}`.
IPv4 scope alone is not node ownership. IPv6 peer advertisements are unsupported,
even though Headscale may independently allocate an IPv6 address.

Exact 202 and test-only signing fixtures are under
`tests/fixtures/provisioning-v1/`. Pending enrollment has no key/expiry.
Issued receipt adds `auth_key`, UTC `expires_at`, `reusable:false`, `max_uses:1`.
TTL is 900 seconds. Dedicated attempt user/key identity lets the worker recover
the encrypted original after HTTP response loss. Headscale 0.29.5 key-list output
is an abbreviated identifier, not a usable secret. A lost creation receipt before
encrypted persistence is unrecoverable: the durable creation fence prevents a
second mint, and explicit replacement must verify revocation/expiry/use first.
Headscale enforces consumption, not the HTTP receipt.
Ciphertext is AEAD-bound to authority/account/node/setup/tailnet/key/expiry.
It is removed on expiry/use/revocation, with non-secret tombstones retained.
This is a bearer credential, not hardware-bound; Rawrarr must authenticate the
node's root-certified advertisement independently.

Enrollment POST returns 201 on first delivery, or 200 with the identical original
key after fresh worker verification. If verification exceeds the bounded wait,
202 contains metadata only and a metadata Location; even `state:issued` without
`auth_key` is not a usable receipt. GET always returns 200 stored metadata and
queues reconciliation, never fresh unused-key attestation or secret material.
Freshly verified terminal/uncertain POST states return 409 with
`enrollment_consumed`, `enrollment_expired`, `enrollment_revoked`, or
`enrollment_uncertain`, respectively, in the standard error envelope.
Terminal receipt 409 additionally includes top-level `enrollment_id` with the
original canonical UUID (or the single successor UUID for replacement).
Replaying the exact POST body/idempotency key recovers that identifier even if
the initial 202/receipt response was lost; it authorizes only owned metadata
lookup or explicit replacement, never a new key. Other errors, including
`idempotency_conflict`, do not assert an enrollment identity.
Replay polling retains a completed authoritative verification for at most five
seconds; it does not reset a still-fresh receipt and starve delivery. An older
verification queues a new worker check. Headscale still enforces single use
even if the key is consumed between verification and client enrollment.
GET retains the actual state and structured cause. An issuing document has the
exact minimal fixture shape; other metadata retains account, server URL,
address policy, `reusable:false`, `max_uses:1`, and null or historical expiry.

Replacement returns the successor's same receipt/pending/error shapes.
There is at most one successor per predecessor even with different replacement
request keys. Reusing a request key for another operation/body is
`409 idempotency_conflict`. A successor remains uncertain with
`revocation_uncertain` if original-key invalidation cannot be verified.

### Expired setup recovery, version 1

Default metadata advertises `enrollment_recovery_version:"1"` when the additive
recovery operations are supported. It is not a readiness or peer-approval
assertion. Absent/unknown capability fails closed; do not fall back to ordinary
enrollment or extend an expired setup. Original `:replace` still uses the same
setup UUID, and cannot target a predecessor already reserved by `:recover`.
Ordinary enrollment cannot create another attempt for a fossil with any retained
enrollment history; use exact original request replay or explicit replacement/
recovery. Request and enrollment tombstones are never erased for a retry.

Both recovery endpoints require a newly signed, bounded Ed25519 assertion with
audience **`swarmsail-enrollment-recovery-v1`**, not the ordinary v1 audience.
The issuer, `azp`, account `sub`, `kid`, time limits and signature trust remain
pinned as above. Recovery POST requires scope exactly `["node:recover"]`,
`node_id`, fresh `setup_intent_id`, `recovery_request_id` equal to its canonical
UUID Idempotency-Key, and `recovery` exactly equal to this body:

```json
{
  "version": "1",
  "predecessor_enrollment_id": "<old-enrollment-uuid>",
  "predecessor_setup_intent_id": "<old-setup-uuid>",
  "setup_intent_id": "<different-fresh-setup-uuid>",
  "node_id": "fossil-0123456789abcdef01234567",
  "owner_authorization_id": "<fresh-owner-authorization-uuid>",
  "setup_evidence_sha256": "<lowercase-64-hex-evidence-sha256>",
  "account_root_fingerprint": "<independently-pinned-lowercase-64-hex-root-fingerprint>",
  "role": "metadata",
  "capability_digest": "<lowercase-64-hex-capability-sha256>"
}
```

This single signed envelope attests that Rawrarr freshly authenticated the owner
and independently verified account-root-signed browser setup evidence covering
the exact account/fossil/role/capability/root/predecessor/old+new setup and owner/
recovery authorization bindings. No separate mixable JWT, arbitrary caller
assertion, subscriber private-root custody, or claimed pre-install node-key
possession. The node generates/proves its key later; Rawrarr must separately
certify/approve that key before application or authenticated peer access.
Provider active device metadata proves Headscale association, not Rawrarr node
approval. Established-device or root changes require explicit adoption/rebinding.
The role supported by this recovery version is `metadata`; root fingerprint
is not the unrelated tailnet generation. Identical retry assertions may renew
timestamps/JTI/signatures, but signed logical bindings cannot change.
Rawrarr's root-signed evidence also commits its immutable original
`recoveryRequestId`, which Rawrarr verifies against its durable original
authorization before attesting the evidence digest. The outer JWT's
`recovery_request_id` is the current transport Idempotency-Key and may be an
alias; an alias does not change the original root-signed UUID or evidence
digest. SwarmSail does not receive or reverify the account-root signature.

Under one SQLite transaction, the provider reserves the unique successor,
persists the full recovery object and original recovery request UUID, clears
predecessor encrypted material, and invalidates authoritative mapping freshness/
device proof with an increased revision. Old receipt delivery is fenced by the
durable successor relation, including a worker result returning after reservation.
The worker then verifies no established fossil device needs rebinding and proves
all predecessor key candidates revoked/expired/consumed before minting. Unknown
outcomes remain uncertain, never automatic reminting. A real existing device
requires `recovery_device_rebinding_required`; unavailable/ambiguous safety
mapping is `recovery_mapping_uncertain`, not proof of absence.

At most one successor exists per predecessor across request IDs/restarts.
Identical logical bindings under another request UUID alias that same successor;
the response retains the first `recovery_request_id`. Different bindings or a
competing `:replace` return `409 recovery_binding_conflict`; request UUID reuse
with another operation/body is `409 idempotency_conflict`. Further recovery
targets the last successor, never retargets the original predecessor.
Response 202 has no secret; normal 201/200 issued successor receipts and retained
metadata additionally contain `recovery` and the first `recovery_request_id`.
Successor terminal 409 additionally contains those fields plus its canonical
`enrollment_id`. Original receipt replay is `409 enrollment_superseded` with
original `enrollment_id` and `successor_enrollment_id`, never original material.

If every initial response was lost, discover the predecessor without replaying
secret-capable `node:enroll`. Lookup JWT scope is exactly
`["node:recover:lookup"]`, `node_id` and old `setup_intent_id`, plus:

```json
{
  "recovery_lookup": {
    "version": "1",
    "original_request_id": "<original-enrollment-post-idempotency-uuid>",
    "node_id": "fossil-0123456789abcdef01234567",
    "setup_intent_id": "<old-setup-uuid>",
    "owner_authorization_id": "<fresh-owner-authorization-uuid>"
  }
}
```

The old setup UUID is an identity selector, not renewed setup authorization.
Lookup verifies the account's original ordinary enrollment fingerprint and
stored fossil/setup binding. It returns 200 retained metadata only, never
decrypts material or queues/mints work. Issuing uses the minimal existing fixture
shape; issued/terminal metadata uses the same non-secret enrollment fields.
Non-issuing expiry is present but may be null if no complete receipt was
persisted, or retained historical UTC after persistence. Superseded original
metadata additionally exposes canonical `successor_enrollment_id` immediately
after reservation; underlying state remains the stored snapshot, not a new
`superseded` enum. Both metadata GET endpoints expose this relation. A queued
recovery replay may return 202 with any retained metadata state, never a key.
Missing request is `404 enrollment_request_not_found`: **blocked/unknown**, not
proof that a delayed original POST cannot arrive or permission to issue a new
request. No absent-request cancellation fence is implemented. Later lookup may
discover the delayed exact original allocation. Mismatched operation/fossil/
setup is `409 recovery_binding_conflict`. A concrete verified predecessor is
required before acquiring fresh predecessor-bound setup evidence and recovery.
Exact examples are under `tests/fixtures/provisioning-v1/recovery-*.json`.

Node mapping response has account/tailnet/fossil, `state:pending|active|removed|
uncertain`, monotonic `revision`, freshness timestamps, enrollment/setup IDs and
`device:null|{id,public_key,ipv4}` from authoritative Headscale queries. Freshness
is five seconds. Headscale's tagged nodes have a synthetic node user; the worker
requires the CLI's embedded `pre_auth_key.user.id` and exact key ID to match the
dedicated enrollment attempt's verified, consumed key-list entry. Caller-supplied
device keys/addresses and node display names cannot establish this binding.
Missing/contradictory associations fail closed.
Multiple live candidates across attempts are uncertain, never
selected by latest ID. Removal/reassignment/key/IP changes invalidate old proofs.
Rawrarr must match the exact device key/address/binding with its independently
root-certified, node-signed TLS advertisement before dialing. On stale/unavailable
mapping, 202 clears device/freshness; no prefix-only fallback.

Errors are `{api_version:"1",error:{code,retryable},operation_id:null}`.
Code is lowercase ASCII `[a-z0-9_]{1,64}`. 400 malformed request, 401 invalid
assertion, 403 claim/scope mismatch, 404 absent owned resource, 409 conflict or
uncertain/used receipt, 413 oversized body, 429 admission limit, 503 unavailable
worker/state/capacity/readiness. Do not expose command output or credentials.

## Readiness and verification

Ready requires assigned owned running container/image/config/mounts, local
Headscale management and policy checks, direct wildcard DNS, active exact Caddy
host/upstream route, trusted HTTPS/no redirects, and durable operator mapping.
Generation or `/healthz` alone is not ready. Readiness expires after 60 seconds
and is rechecked before issuance/replay. Missing prerequisites remain explicit.
New supported peer policy grants only the verified profile's required protocol/
port between tagged nodes in the same tenant; no broad tag-to-tag ports, raw
backend, GUI/Arr/admin/media/LAN/subnet routes or unapproved Syncthing.

```sh
cargo test --locked --bin swarmsail-provisioner
cargo fmt --check
cargo check --locked --all-targets
python3 -m unittest tests.test_private_worker scripts.tests.test_provision_subscriber -q
SWARMSAIL_RUN_E2E=1 python3 -m unittest \
  scripts.tests.test_private_provisioning_e2e.PrivateProvisioningTests \
  scripts.tests.test_private_lifecycle_e2e.PrivateLifecycleTests -v
```

Tests use only temporary state and disposable Compose projects. The real-key
test races two fresh clients on one key, checks exactly one enrollment, original
receipt recovery, later reuse rejection, expiry/revocation, distinct-user
enrollment and same-client reconnect. Candidate packet ACL tests are not a claim
that Rawrarr's userspace TLS/trusted discovery/application channel is implemented.
That separate positive/negative transport fixture is a release acceptance gate.
The lifecycle fixture requires the debug provisioner binary built first. It runs
the real private API and supervised worker against disposable pinned Headscale,
directory-mounted Caddy with a Unix admin socket and independently installed
test TLS. It checks owned existing-container adoption without recreation,
readiness expiry, actual ingress validation failure/rollback/fencing,
quiescent reconciliation, and interruption after key creation before encrypted
persistence followed by terminal replay identity and verified replacement.
It also checks fresh-setup recovery of issued/expired/uncertain predecessors,
concurrent identical recovery, restarted receipt replay, metadata-only discovery
before and after encrypted persistence, and refusal to replace an established
device implicitly.
Only its generated test adapter substitutes isolated Docker DNS/test CA probes
and embedded test DERP for public infrastructure, and injects the candidate
TCP 443 profile. Those substitutions do not validate live wildcard DNS,
public CA renewal, host firewalls or the Rawrarr userspace peer application.
Network tests use separate foreign-account control planes/clients, listening
denied destinations, UDP, and approved fixture-only subnet/host routes. They
require either tailnet routing with no payload delivery or explicit unreachable
route rejection; they never send fixture destinations through a host/LAN route.

### Local consumer integration endpoint

For an explicitly coordinated local Rawrarr integration test, build the debug
provisioner first, then run the bounded disposable fixture:

```sh
SWARMSAIL_RUN_E2E=1 python3 -m scripts.tests.run_private_provider_fixture \
  --connection-file /tmp/swarmsail-test-connection.json --duration 1800
```

The connection file must not exist. It contains only test connection metadata:
a uniquely named internal Docker network, an HTTP provider origin accessible
only from that network, account/default allocation selectors and pinned test
service public-key path. JWT issuer is `https://rawrarr.example.test`, authorized
service is `rawrarr-test`, and key ID is `rfc8032-test-only`; audiences/scopes
follow the contract above. The signing key is the publicly known RFC 8032 vector
in the fixture directory, never a live credential. The test account accepts
independently selected fossil/setup identities. Attach the disposable consumer
container using `docker run --network <docker_network> ...`; do not expose ports.
The Caddy fixture forwards internal port 8088 through `host.docker.internal`
to the real native loopback Rust API. This requires a local Docker engine such
as OrbStack that can bridge to host loopback; startup fails if unavailable.
API and worker both remain native and use the same local SQLite filesystem.
Never share a SQLite WAL ledger between native processes and Linux containers
through a macOS bind mount: cross-platform locking/WAL behavior is not supported.
The consumer receives neither ledger access nor a Docker socket. Production
remains API UID 65532 and HTTPS-only.
The worker, pinned Headscale and directory-mounted Caddy are the existing
isolated lifecycle fixture; no production state or subscriber roots are used.
The allocation still reports `https://<tailnet-id>.swarm.rawrarr.net`; the real
Headscale configuration, Caddy route, temporary trusted certificate SAN and
Docker-only DNS aliases use that exact origin. Consumers must not relax their
canonical hostname validation for this fixture. No live DNS record is created.

SIGINT/SIGTERM or lease expiry (maximum one hour) tears down the owned stack,
worker/API processes and temporary TLS/receipt material. Remove the specific
connection file afterward; its paths become invalid after teardown.
This endpoint intentionally uses the narrow test candidate peer adapter and
does not enable the production profile or prove Rawrarr peer acceptance.
Disconnect consumer containers from the named network before teardown. To stop
the running harness, send SIGTERM to its specific Python PID and allow cleanup
to finish; forcibly terminating its process tree can interrupt Docker cleanup.

The Rawrarr Rust client has exercised this canonical isolated provider:
missing-request lookup stays 404 unknown, original issuance and secret-free
lookup agree, explicit recovery reserves a successor, original replay is fenced
with predecessor/successor identities, and successor metadata/replay agree.
This used synthetic service-attested root evidence and establishes protocol
interoperability only. It does not establish authenticated browser/owner/root
bootstrap, durable Rawrarr recovery, node approval or production peer readiness.

## Existing generation adoption

Preview explicit binding of an owned isolated managed generation:

```sh
python3 -m scripts.adopt_default_tailnet \
  --ledger /var/lib/swarmsail-provisioning/ledger.db \
  --worker-settings /etc/swarmsail/worker.json \
  --account-id <canonical-account-uuid> \
  --tailnet-id <reserved-default-uuid> \
  --instance-id <exact-owned-instance-id>
```

Repeat with `--apply` only after reviewing account/instance/generation/origin.
This binds desired identity, not readiness or deployment. It preserves old
origin/generation, refuses reassignment, and requires the worker stopped.
Existing container ownership labels, pinned config/image, externally prepared
ingress and policy migration still require explicit operator verification.
Never silently rewrite adopted policy or delete old active generations.

## Recovery, rotation and rollback

Timeouts keep the allocation/enrollment intent. Retry identical request or read
owned status. If key creation outcome cannot be uniquely reconciled, state is
uncertain; do not issue another until all original candidates are proven revoked,
expired or used. Explicit replacement is idempotent and must verify old-key
revocation. Replacing a consumed enrollment does not erase the old enrolled
device: multiple devices for the same fossil remain uncertain until an operator
verifies/removes the obsolete device. Existing authenticated clients reconnect
without reusing the preauth key.

An unknown Caddy mutation or rollback leaves the durable `ingress-fence=unknown`
receipt. It cannot be cleared by a retry or operator acceptance boolean.
Stop admissions and the supervised worker, then establish that timed-out
container exec/reload operations are quiescent before repairing ingress.
Inspect only the configured ingress container's processes. Restore the reviewed
baseline plus owned tenant routes; do not discard unrelated operator routes.
With `jq`, `diff` and `sqlite3` installed, compare disk-adapted and active native
configuration in an owner-only temporary directory:

```sh
umask 077
docker exec <ingress-container> caddy adapt \
  --config <container-caddyfile> --adapter caddyfile | jq -S . > <private-dir>/disk.json
docker exec <ingress-container> curl --fail --silent \
  --unix-socket <container-admin-socket> http://localhost/config/ \
  | jq -S . > <private-dir>/active.json
diff -u <private-dir>/disk.json <private-dir>/active.json
```

Expected: no diff, correct baseline hash, and each owned exact hostname paired
with its intended Headscale upstream. Recheck trusted tenant HTTPS and ownership.
Only after those checks and quiescence, with the worker still stopped:

```sh
sqlite3 <ledger-path> \
  "DELETE FROM worker_receipts WHERE resource='ingress-fence' AND value='unknown';"
```

Restart the worker to revalidate every route; an unresolved outcome re-fences it.
Remove only the two named temporary JSON files after inspection. Clearing the
fence alone is not readiness and never authorizes wider policy.

Before rollout, verify backups of ledger, config/mappings, Headscale DB/noise
keys, encryption-key recovery and Caddy disk/active state. Install new verifier
keys through bounded overlap covering outstanding assertions plus skew, then
remove retired `kid`. Losing the encryption key makes outstanding receipts
uncertain, never a reusable/new-key fallback.

Disable admissions and drain/fence the worker before rollback. Preserve ledger
and Headscale state, run a schema-compatible binary, and leave wildcard DNS/TLS
renewal under its separate operator owner. Code rollback does not unprovision
tenants. Do not restore an old DB snapshot over newer enrollments, erase default
bindings, reuse addresses, downgrade Headscale DB migrations blindly, or delete
tenant volumes. Cleanup requires explicit verification/approval and removes only
owned resources while retaining identity tombstones. Never use Docker prune,
global Compose recreation or an app URL/manual-URL success fallback.
