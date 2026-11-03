# Overview-first dashboard layout

## Objective and initial state

Put Swarmsail inventory before setup controls to make the dashboard easier to scan. The live `do3.risk-mermaid.ts.net` route uses Tailscale Serve to reach the `rawrarr-roarlay-prod-swarmsail` container on loopback port 18787. Its image was `swarmsail:local`, while the synced source in `/opt/swarmsail-demo` contained the newer dashboard code.

## Implementation

The dashboard now starts with organization, device, online-device, and unapplied-plan counts, then organization and device lists. Organization/device creation, connection planning, sharing, ACL, DNS, and traffic-protection controls are grouped under a collapsed native disclosure. Connections, shares, and ACL policy remain visible below it. Existing API actions and the organization detail page are unchanged.

## Deployment

Before switching the production Swarmsail container, the `instances.json` file was copied and the SQLite database was backed up through SQLite's backup API and passed `PRAGMA integrity_check`. Both mode-600 backups are in:

```text
/opt/rawrarr-roarlay-prod/backups/swarmsail-layout-<UTC timestamp>/
```

The updated source was built as `swarmsail:demo`, tagged as `swarmsail:local`, and used to recreate only the `swarmsail` service from `/opt/rawrarr-roarlay-prod/docker-compose.roarlay-prod.yml`. No other production services or the Swarmsail database volume were changed.

## Verification

- `python3 -m py_compile swarmsail_web.py` — passed.
- `python3 -m unittest discover -s scripts/tests -v` — all 14 tests passed.
- Parsed the generated dashboard HTML and confirmed one disclosure and no duplicate element IDs.
- `git diff --check` — passed.
- `https://do3.risk-mermaid.ts.net/` returned HTTP 200 and includes the overview metrics and collapsed Manage section.
- `https://do3.risk-mermaid.ts.net/healthz` returned `{"status": "ok"}`; the recreated production Swarmsail container is running.

## Separate demo stack observation

The unrelated `swarmsail-demo` Compose project on `do3` was already restart-looping because its named data volume lacked `/data/instances.json`. An empty instance config was added to that isolated demo volume and its control-plane container was rebuilt from the synced source. Its separate Headscale container was still restarting during this task; this does not affect the production Swarmsail route above.

## Recovery

Restore the prior image tag from its retained local image reference or rebuild the previous release, then recreate only the production `swarmsail` service with the same Compose file. Restore `instances.json` and the database backup only if the application has modified them and the rollback requires the prior data state.
