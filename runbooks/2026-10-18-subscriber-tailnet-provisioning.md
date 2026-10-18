# Build subscriber tailnet provisioning

## Purpose and initial state

Start the Rawrarr account-to-Swarmsail integration with a repeatable, idempotent provisioning path. The Codebox Git checkout had the web control plane and SQLite organization model, but its imported `manage_headscale.py` and Headscale config template were absent. The live `do2` release under `/opt/swarmsail/current` contains those files and owns the active deployment. See [current do2 setup](do2-current-setup.md) and [the prior sharing transition](2026-10-18-rawrarr-tailnet-sharing-transition.md).

## Actions

- Retrieved only the manager source and non-secret Headscale template from the live release; did not copy `instances.json`, databases, environment files, keys, or runtime state.
- Restored those files in Git and removed the test-only import stub.
- Added `subscriber_tailnets` SQLite mapping and startup reconciliation. A canonical opaque account UUID has one active managed Headscale instance at a time. Each instance has an immutable generation and matching `subscriber_account_id`; retries return the same mapping. A newly linked organization starts isolated.
- Added `scripts/provision_subscriber.py`, which requires an operator-provided active trial/subscription expiry and plans a fresh managed instance with a non-overlapping account-derived `/26`. Preview is the default and produces the same prefix as apply while config is unchanged. `--apply` writes the config atomically under a local lock and links SQLite; it does not start a container. The account service, not the CLI, must verify eligibility before invoking this workflow.
- Added `--new-private` for a previously shared account. It creates the next managed instance and new isolated organization, deactivates the old mapping, and leaves the old shared organization shared. It does not migrate nodes, data, or network routes.
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

Only after the active organization's mode is collaborative, add `--new-private` and supply unused DNS names to reserve a replacement. Never use the previous Headscale origin or base domain for the replacement.

## Verification

Ran `python3 -m unittest discover -s tests -q` (20 passed), `python3 -m unittest discover -s scripts/tests -q` (5 passed), `python3 -m py_compile manage_headscale.py swarmsail_web.py scripts/provision_subscriber.py` (passed), and `git diff --check` (passed). The tests use temporary JSON and SQLite files to verify preview has no mutations, apply is idempotent, adopted instances are preserved, expired eligibility is rejected, generated Headscale config remains private, web startup reconciles a reserved account mapping, and replacement with a new private generation preserves the shared predecessor. No new Headscale instance, DNS record, proxy route, or account was created on `do2`.

## Recovery and remaining work

Source changes can be reverted by a scoped Git revert. A failed reservation after the config write can be retried with the same account UUID and network settings to complete the SQLite mapping. Before any live deployment, take and verify restorable backups of the current release, config, and SQLite state; stage a separate release and update only the Swarmsail-owned service. The hosted Rawrarr account and verified trial/subscription service do not exist yet, so automatic sign-in-triggered provisioning remains blocked on that separate component. DNS, HTTPS ingress, Headscale policy, actual container startup, device enrollment, and pairwise cross-instance routes are separate unimplemented steps.
