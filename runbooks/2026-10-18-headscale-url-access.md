# Headscale URL access

## Objective and initial state

Diagnose why `https://headscale.example.test/` was unreachable after the MacBook Headscale service and Let's Encrypt certificate were deployed. Headscale was healthy on `192.168.128.210:443`; a forced hostname resolution returned HTTP 200 with normal certificate verification. The DNS zone had no explicit record, so its wildcard A record resolved the hostname to `100.84.23.71`.

## DNS correction

Using the locally stored scoped Cloudflare token, create one explicit DNS-only A record for `headscale.example.test` pointing to `192.168.128.210` with a 60-second TTL. Do not change the wildcard record or other hostnames. The DNS provider's API confirmed the explicit record and both authoritative nameservers returned the new address. Public recursive resolvers updated after their cached wildcard answer expired.

## Verification

Confirmed the exact record is `A headscale.example.test -> 192.168.128.210`, with proxying disabled. Google and Quad9 resolvers returned the new address. The host OS resolver and Docker's embedded resolver continued returning cached `100.84.23.71` temporarily. `curl --resolve headscale.example.test:443:192.168.128.210 https://headscale.example.test/health` returned `{"status":"pass"}` with certificate verification. Normal hostname requests failed while the local resolver cache was stale.

On the Mac, flush the local DNS cache with `sudo dscacheutil -flushcache` and `sudo killall -HUP mDNSResponder`, then test with `curl -fsS https://headscale.example.test/health`. Browser DNS may also need a refresh/restart. Docker clients use the host's OrbStack resolver; retry after its cache expires. From outside the LAN, this private A record is not routable; the requester must be on the MacBook LAN or use a separately configured reachable path.

## Recovery

The explicit DNS A record can be edited or deleted independently of the wildcard record. Restore the prior lookup behavior by deleting only the explicit `headscale.example.test` record; do not remove the wildcard record.
