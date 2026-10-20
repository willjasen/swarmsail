# Coordinator and droplet architecture

## Product scope

One Swarmsail environment consists of a public coordinator and one or more
droplet nodes. A user can create multiple independent Headscale tailnets on a
droplet. Tailnets are isolated from one another; cross-tailnet sharing is out
of scope.

The coordinator arranges access but does not manage a droplet's Headscale,
Caddy, or CrowdSec containers. Those services and their configuration are
owned and operated by the droplet node.

## Roles and network paths

| Component | Responsibility | Must not do |
| --- | --- | --- |
| Public coordinator | Passkey account entry point, node discovery, authorization handoff, and WebRTC signaling | Hold droplet Docker access, decrypt application configuration, or proxy browser application requests as HTTP |
| Droplet node | Run Swarmsail locally and manage its own Headscale, Caddy, and CrowdSec services | Send plaintext configuration or private user keys to the coordinator |
| Coordinator Tailscale client | Provide the coordinator's private management-network identity | Act as the browser-to-droplet application channel |
| Droplet Tailscale client | Establish the droplet's outbound management connection to the coordinator | Replace user authorization or expose the droplet's management interface publicly |
| Browser | Authenticate the user and establish the WebRTC application channel to the selected droplet | Treat a node ID, tailnet address, or coordinator ticket as permission to read configuration |

The droplet's outbound Tailscale connection is for management rendezvous and
bootstrap. The browser-to-droplet application channel is a separate WebRTC
data channel protected by WebRTC's DTLS transport encryption. The coordinator
may exchange SDP/ICE signaling and, if TURN fallback is used, relay encrypted
WebRTC packets. It must not terminate the application channel or handle its
plaintext payloads. The browser and droplet must independently authenticate
the session and authorize each operation. SDP/ICE received from the coordinator
is untrusted: the WebRTC DTLS certificate fingerprint and session transcript
must be authenticated with a previously trusted node key or an owner-root
signature. Accepting a key merely because the coordinator supplied it would
allow an active coordinator to substitute its own peer and decrypt traffic.

## Droplet public IPv4

A droplet needs a real, globally routable IPv4 address for public ingress and
reliable direct WebRTC connectivity. The initial deployment expects the user
to provide a droplet; automatic provider-side provisioning is deferred. The
droplet configuration records that address and binds Caddy to it rather than
to an unspecified interface. The current config validation rejects private,
shared-address, documentation-only, and IPv6 values for this field.

DNS A records for droplet services must resolve directly to the droplet's
public IPv4, without DNS proxying. The address must also be permitted by the
provider firewall and host firewall for the specific TCP/UDP services that
are enabled. A public address alone does not prove direct WebRTC reachability:
ICE/STUN connectivity, firewall rules, and an encrypted TURN fallback still
need end-to-end verification.

The current validation checks that the configured address is a globally
routable IPv4 and that Caddy binds to that same value. A separate DigitalOcean
metadata/DNS check verifies host assignment and direct A records. Firewall
reachability, TLS issuance, and provider API provisioning remain future work.

## User keys and privacy boundary

Passkey authentication, the user's age identity, and the user's root signing
key are separate concerns. The private age identity and signing key must
remain client-side or on the user's node; signed/encrypted configuration is
handled by the droplet. The coordinator may retain account identity, opaque
node/tailnet identifiers, public keys, transient presence, and narrowly scoped
connection tickets, but not private keys or plaintext configuration.

For the first implementation, the browser UI may be served by the coordinator
as requested. This means the confidentiality guarantee covers passive
signaling/packet relay only: a coordinator capable of changing that UI could
serve code that captures user input or plaintext. A coordinator-independent,
signed UI client is required before claiming protection against an actively
malicious coordinator.

## Current implementation status

- The existing Swarmsail dashboard is a local operator interface. Its
  management API has no user authentication and must not be exposed as a
  public coordinator.
- The repository can generate Headscale, Caddy, and CrowdSec Compose
  configuration on the machine where it runs; it does not yet provide a
  remote droplet agent or a coordinator-to-node protocol.
- Browser-to-droplet WebRTC, signaling authorization, node identity
  verification and signed signaling transcripts, passkey account flows,
  age-based configuration, and root-key-signed operations are not implemented.
- Droplet provider provisioning is deferred. Public IPv4 format and Caddy
  binding validation are implemented; a DigitalOcean-specific check verifies
  host assignment and direct A records. Firewall reachability is not checked.

Do not deploy the existing dashboard as the public coordinator. The first
safe deployment milestone must separate coordinator signaling from node-local
management before exposing any user-facing public endpoint.
