# Script tests

- `test_provision_subscriber.py` checks dry-run behavior, active eligibility, idempotent reservation, preservation of adopted instances, stable account network settings, and new private generation after sharing. Run `python3 -m unittest discover -s scripts/tests -v` from the repository root. Fixtures use temporary JSON and SQLite files only.
- `test_manager_caddy.py` checks generated Caddy ingress ports, secret-file references, Headscale hostname routes, and rejects conflicting NPM ingress, public Headscale listeners, and duplicate hostnames. Run with `python3 -m unittest scripts.tests.test_manager_caddy -v` or the full scripts test command.
