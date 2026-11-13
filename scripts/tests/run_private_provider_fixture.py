"""Bounded internal Docker integration endpoint; never a deployable service."""

import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import urlsplit
import uuid

from scripts.tests.test_private_lifecycle_e2e import PrivateLifecycleTests, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=1800)
    args = parser.parse_args()
    if os.environ.get("SWARMSAIL_RUN_E2E") != "1":
        parser.error("requires SWARMSAIL_RUN_E2E=1")
    if not 1 <= args.duration <= 3600:
        parser.error("duration must be 1..3600 seconds")
    if not args.connection_file.parent.is_dir() or args.connection_file.exists():
        parser.error("connection file must be new with an existing parent directory")
    if os.environ.get("SWARMSAIL_E2E_KEEP") == "1":
        parser.error("integration fixture must clean up; unset SWARMSAIL_E2E_KEEP")
    fixture = PrivateLifecycleTests("test_api_worker_ingress_and_adoption_recovery")
    fixture.integration_baseline = ""
    stopped = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopped.set())
    try:
        fixture.setUp()
        fixture.integration_baseline = (
            "{\n    admin unix//etc/caddy/admin.sock\n}\n\n"
            "http://:8088 {\n    reverse_proxy host.docker.internal:"
            + str(urlsplit(fixture.origin).port) + "\n}\n\n"
        )
        network = fixture.project + "-rawrarr-integration"
        fixture.command("docker", "network", "create", "--internal", network)
        fixture.addCleanup(fixture.command, "docker", "network", "rm", network)
        fixture.command("docker", "network", "connect", "--alias", "provider.test",
                        network, fixture.ingress)
        fixture.addCleanup(fixture.command, "docker", "network", "disconnect",
                           network, fixture.ingress)
        worker = fixture.start_ready_provider()

        def probe():
            response = fixture.execute(
                "caddy", "curl", "--fail", "--silent", "--noproxy", "*",
                "--header", "Authorization: Bearer " + fixture.jwt(),
                "http://127.0.0.1:8088/private/v1/accounts/" + fixture.account
                + "/default-tailnet",
            )
            metadata = json.loads(response)
            if metadata.get("server_url") != f"https://{fixture.tailnet}.swarm.rawrarr.net":
                raise AssertionError("isolated_allocation_origin_not_canonical")
            return (metadata.get("state"), metadata.get("phase")) == ("ready", "complete")

        fixture.wait_for("container provider route", probe, timeout=30)
        original_node = fixture.node
        fixture.node = "fossil-" + uuid.uuid4().hex[:24]
        request_id = str(uuid.uuid4())
        issued = fixture.wait_for("integration fixture single-use issuance", lambda:
                                  fixture.receipt("/enrollments", {
                                      "node_id": fixture.node,
                                      "setup_intent_id": fixture.intent,
                                  }, request_id))
        fixture.credentials.append(issued["auth_key"])
        status, lookup = fixture.api("/enrollment-requests/" + request_id)
        if (status != 200 or lookup.get("enrollment_id") != issued["enrollment_id"]
                or "auth_key" in lookup):
            raise RuntimeError("isolated_metadata_lookup_failed")
        fixture.node = original_node
        manifest = {
            "test_only": True, "docker_network": network,
            "provider_origin": "http://provider.test:8088",
            "account_id": fixture.account, "tailnet_id": fixture.tailnet,
            "server_url": f"https://{fixture.tailnet}.swarm.rawrarr.net",
            "node_id": fixture.node, "setup_intent_id": fixture.intent,
            "issuer": "https://rawrarr.example.test", "service": "rawrarr-test",
            "signing_kid": "rfc8032-test-only",
            "service_public_key_file": str(ROOT / "tests/fixtures/provisioning-v1/public.pem"),
            "source_snapshot": str(ROOT), "lease_seconds": args.duration,
            "production_peer_profile_enabled": False,
            "peer_profile": "candidate-isolated-only",
        }
        with args.connection_file.open("x") as output:
            json.dump(manifest, output, indent=2)
            output.write("\n")
        print("Isolated provider ready; nonsecret connection file: " + str(args.connection_file),
              flush=True)
        deadline = time.monotonic() + args.duration
        while not stopped.wait(min(1, max(0, deadline - time.monotonic()))):
            if worker.poll() is not None:
                raise RuntimeError("isolated_worker_stopped")
            if time.monotonic() >= deadline:
                break
    finally:
        if not fixture.doCleanups():
            raise RuntimeError("isolated_fixture_cleanup_failed")


if __name__ == "__main__":
    main()
