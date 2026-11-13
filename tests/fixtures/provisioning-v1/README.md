# Private provisioning v1 test fixtures

The PEM pair is the **publicly known RFC 8032 Ed25519 test vector 1**.
It is not a credential and must never be installed as service trust or used for
live signing. Private encoding is PKCS#8; public encoding is SPKI. JWT uses EdDSA
and `kid: rfc8032-test-only`. Tests sign fresh, short-lived assertions from this
known vector; fixed historical assertions are suitable only for signature tests.

`enrollment-pending.json` is the exact 202 body. Its Location is
`/private/v1/accounts/11111111-1111-4111-8111-111111111111/default-tailnet/enrollments/44444444-4444-4444-8444-444444444444`.
Retry-After is `5`, Cache-Control is `no-store`. No expiry or key exists yet.

`enrollment-uncertain.json` is the terminal receipt 409 envelope. The canonical
enrollment UUID lets an authenticated exact account/node/setup replay recover
the predecessor identity after losing the initial response. It contains no key
and does not authorize reminting; explicit replacement verifies invalidation.

Recovery uses the distinct `swarmsail-enrollment-recovery-v1` JWT audience.
`recovery-binding.json` is both the POST body and the identical signed JWT
`recovery` claim. JWT `recovery_request_id` equals the current transport
Idempotency-Key; immutable original authorization in the root-signed evidence
is committed by `setup_evidence_sha256` and separately verified by Rawrarr.
Aliases cannot change that evidence/body and return the first reservation ID.
`default-ready.json` advertises `enrollment_recovery_version:"1"`; this does
not approve a production peer profile.

`recovery-lookup-issued.json` is exact metadata-only HTTP 200 for an issued
original enrollment, including no bearer key. Lookup uses scope exactly
`["node:recover:lookup"]`, fossil/old setup claims and a signed `recovery_lookup`
object with version, original_request_id, node_id, setup_intent_id and fresh
owner_authorization_id. `recovery-lookup-missing.json` is HTTP 404, explicitly
blocked/unknown even if a delayed original POST later arrives.
`recovery-binding-conflict.json` is HTTP 409 for a mismatched original request
operation/fossil/setup; neither error contains an asserted enrollment ID.

`lookup-metadata-cases.json` contains fully literal HTTP 200 bodies for issuing,
uncertain, consumed, expired, revoked and superseded snapshots. Non-issuing
expiry is nullable when the original receipt was never persisted, or historical
UTC when it was; it is not authorization. Supersession is an optional canonical
`successor_enrollment_id`, not a new metadata state. It appears immediately on
reservation, before the worker updates the predecessor's underlying state.
Both metadata GET operations expose that relation and never return a key.

`recovery-pending.json` is HTTP 202 issuing. `recovery-issued.json` is the exact
same body for HTTP 201 first delivery and HTTP 200 safe replay. HTTP 202 while
verification is queued may contain the current issued/terminal metadata instead;
for an issued snapshot it is exactly `recovery-issued.json` with `auth_key`
omitted, not null. `recovery-terminal-cases.json` contains all four fully literal
HTTP 409 successor terminal envelopes. Its cause details remain in metadata.
The timestamps are historical and the visibly marked synthetic key is public
test-only data; never use these fixtures as live credentials or readiness proof.

No production peer profile is approved: `peer_profile_unavailable` blocks
allocation readiness and enrollment. Tests inject a candidate same-account
tagged-node TCP 443 policy; they do not enable it at runtime. Full Rawrarr
signed discovery, authoritative device mapping, node-terminated TLS and
authenticated config-repair acceptance must pass before profile activation.
Headscale enrollment/reconnection and outbound HTTPS application traffic to
Rawrarr are separate from peer packet ACLs.
