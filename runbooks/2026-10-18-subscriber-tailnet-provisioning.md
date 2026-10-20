# Build subscriber tailnet provisioning

## Purpose and initial state

Start the subscriber account-to-Swarmsail integration with a repeatable, idempotent provisioning path. The Codebox Git checkout had the web control plane and SQLite organization model, but its imported `manage_headscale.py` and Headscale config template were absent. The live `do2` release under `/opt/swarmsail/current` contains those files and owns the active deployment. See [current do2 setup](do2-current-setup.md) and [the prior sharing transition](2026-10-18-tailnet-sharing-transition.md).

## Actions

- Retrieved only the manager source and non-secret Headscale template from the live release; did not copy `instances.json`, databases, environment files, keys, or runtime state.
- Restored those files in Git and removed the test-only import stub.
- Added `subscriber_tailnets` SQLite mapping and startup reconciliation. A canonical opaque account UUID can own multiple active managed Headscale instances. Each instance has an immutable generation and matching `subscriber_account_id`; retries return the same mapping. Each newly linked organization starts isolated. Startup removes the legacy one-active-instance-per-account index without changing existing rows.
- Added `scripts/provision_subscriber.py`, which requires an operator-provided active trial/subscription expiry and plans a fresh managed instance with a non-overlapping account-derived `/26`. Preview is the default and produces the same prefix as apply while config is unchanged. `--apply` writes the config atomically under a local lock and links SQLite; it does not start a container. The account service, not the CLI, must verify eligibility before invoking this workflow.
- Replaced the prior shared-account/new-private-generation transition with independent active tailnets. To add another tailnet for the same account, use unused server and base-domain names; existing tailnets remain active and isolated.
- Made direct `manage_headscale.py generate` preview by default; `--apply` is required to write generated Compose and Headscale config files.

## Operator invocation

From the Swarmsail repository, use an account UUID and an expiry already verified by the account service. These example names are placeholders and must be replaced with assigned DNS names and node-local paths. Run the command first without `--apply`; review its JSON plan, then repeat the same command with `--apply` to reserve the instance. The CLI does not deploy it.

```sh
python3 -m scripts.provision_subscriber \
  --config /opt/swarmsail/current/instances.json \
  --database /opt/swarmsail/current/swarmsail.db \
  --account-id 11111111-1111-4111-8111-111111111111 \
  --eligibility trial --eligible-until 2099-01-01T00:00:00Z \
  --server-url https://subscriber.example.test \
  --base-domain subscriber-tailnet.example.test \
  --data-root /opt/swarmsail/subscriber-instances
```

To create another independent tailnet for the same account, repeat the preview with new, unused DNS names and then apply it. Existing tailnets remain active; no devices, data, or routes are shared.

## Verification

Run `python3 -m unittest discover -s tests -q` and `python3 -m unittest discover -s scripts/tests -q`; tests use temporary JSON and SQLite files to verify preview has no mutations, apply is idempotent, adopted instances are preserved, expired eligibility is rejected, generated Headscale config remains private, web startup reconciles reserved account mappings, and one account can keep multiple isolated tailnets active. No live Headscale instance, DNS record, proxy route, or account is created by these tests.

## Recovery and remaining work

Source changes can be reverted by a scoped Git revert. A failed reservation after the config write can be retried with the same account UUID and network settings to complete the SQLite mapping. Before any live deployment, take and verify restorable backups of the current release, config, and SQLite state; stage a separate release and update only the Swarmsail-owned service. The hosted account and verified trial/subscription service do not exist yet, so automatic sign-in-triggered provisioning remains blocked on that separate component. DNS, HTTPS ingress, Headscale policy, actual container startup, and device enrollment are separate unimplemented steps. Cross-tailnet sharing is intentionally not part of the initial product.
