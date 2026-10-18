# Swarmsail scripts

- `provision_subscriber.py` — Reserve a dedicated managed Headscale instance for an opaque Rawrarr account after an operator has verified an active trial or subscription. Run `python3 -m scripts.provision_subscriber --help`. It reads an existing `instances.json`, previews by default, and requires `--apply` to write the config and local Swarmsail SQLite mapping. Add `--new-private` only when the active tailnet is shared and the owner is creating a new private generation. It does not deploy the instance or configure DNS/ingress.

The existing root-level `manage_headscale.py` generates managed Compose and Headscale config files. `python3 manage_headscale.py generate --config <path>` previews; add `--apply` only after reviewing the target. The Swarmsail web process also imports its config loader and generator.
