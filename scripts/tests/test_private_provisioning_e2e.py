"""Opt-in pinned Headscale key/policy tests using the disposable full-stack harness."""

import concurrent.futures
import contextlib
import json
import os
import sqlite3
import subprocess
import time
import unittest
import uuid
from unittest.mock import patch

from scripts.provisioning_worker import Worker
from scripts.tests.test_full_stack import FullStackTests, ENABLED


@unittest.skipUnless(ENABLED, "set SWARMSAIL_RUN_E2E=1 for disposable Docker tests")
class PrivateProvisioningTests(FullStackTests):
    # Keep legacy connectivity coverage in its own suite.
    test_clients_exchange_ping_and_tcp_through_managed_tailnet = None

    def write_stack(self):
        super().write_stack()
        stack = json.loads(self.compose_path.read_text())
        stack["services"].pop("control-plane")
        stack["services"].pop("crowdsec")
        for name in ("swarmsail-data", "crowdsec-config", "crowdsec-data"):
            stack["volumes"].pop(name)
        foreign = json.loads(json.dumps(stack["services"]["headscale-test"]))
        foreign["container_name"] = self.project + "-foreign-headscale"
        foreign_config = self.directory / "foreign-headscale"
        foreign_config.mkdir()
        foreign_config.joinpath("config.yaml").write_text(
            self.directory.joinpath("headscale/config.yaml").read_text()
            .replace("https://headscale.test", "https://foreign.test")
            .replace("100.64.0.0/26", "100.64.1.0/26"))
        foreign["volumes"] = [
            f"{foreign_config}:/etc/headscale:ro",
            "foreign-headscale-data:/var/lib/headscale",
        ]
        stack["services"]["foreign-headscale"] = foreign
        stack["volumes"]["foreign-headscale-data"] = {}
        stack["services"]["foreign-client"] = json.loads(json.dumps(stack["services"]["client-a"]))
        stack["services"]["foreign-client"]["volumes"] = ["foreign-client-data:/var/lib/tailscale"]
        stack["services"]["foreign-client"]["networks"] = ["foreign-client"]
        stack["networks"]["foreign-client"] = {"internal": True}
        stack["volumes"]["foreign-client-data"] = {}
        stack["services"]["caddy"]["networks"]["foreign-client"] = {"aliases": ["foreign.test"]}
        self.directory.joinpath("caddy/Caddyfile").write_text(
            self.directory.joinpath("caddy/Caddyfile").read_text()
            + "\nforeign.test {\n    tls internal\n    reverse_proxy foreign-headscale:8080\n}\n")
        self.compose_path.write_text(json.dumps(stack))

    def trust(self):
        self.wait_for("Headscale", lambda: self.execute(
            "headscale-test", "headscale", "users", "list", "--output", "json", timeout=10))
        certificate = self.directory / "root.crt"
        self.wait_for("private CA", lambda: self.compose(
            "cp", "caddy:/data/caddy/pki/authorities/local/root.crt",
            str(certificate), timeout=10) or certificate.exists())
        for client in ("client-a", "client-b", "foreign-client"):
            self.compose("cp", str(certificate),
                         f"{client}:/usr/local/share/ca-certificates/swarmsail.crt")
            self.execute(client, "update-ca-certificates")
            self.compose("restart", client)
            origin = "foreign.test" if client == "foreign-client" else "headscale.test"
            self.wait_for("trusted control TLS", lambda client=client, origin=origin: self.execute(
                client, "wget", "-qO-", f"https://{origin}/health", timeout=10))

    def login(self, client, key, duration="10s"):
        self.credentials.append(key)
        self.execute(client, "sh", "-c", "umask 077; cat >/tmp/private-key", input=key)
        try:
            # Capture exit status without dumping authentication output.
            result = subprocess.run([
                "docker", "compose", "--project-name", self.project,
                "--file", str(self.compose_path), "exec", "-T", client,
                "tailscale", "up", "--login-server=https://"
                + ("foreign.test" if client == "foreign-client" else "headscale.test"),
                "--auth-key=file:/tmp/private-key", "--accept-dns=false",
                f"--hostname={client}", f"--timeout={duration}",
            ], capture_output=True, timeout=30)
            if result.returncode:
                detail = result.stderr.decode(errors="replace")
                for credential in self.credentials:
                    detail = detail.replace(credential, "[redacted]")
                self.login_errors = getattr(self, "login_errors", []) + [detail[-1500:]]
            return result.returncode == 0
        finally:
            self.execute(client, "rm", "-f", "/tmp/private-key")

    def test_real_single_use_recovery_revocation_and_candidate_policy(self):
        self.trust()
        ledger = self.directory / "ledger.db"
        with contextlib.closing(sqlite3.connect(ledger)) as db, db:
            db.executescript("""
                CREATE TABLE provisioning_version(version INTEGER);
                INSERT INTO provisioning_version VALUES(1);
                CREATE TABLE account_defaults(account TEXT,tailnet TEXT,spec TEXT);
                CREATE TABLE enrollments(id TEXT PRIMARY KEY,account TEXT,node TEXT,document TEXT);
                CREATE TABLE node_mappings(account TEXT,node TEXT,document TEXT);
                CREATE TABLE worker_receipts(resource TEXT PRIMARY KEY,value TEXT);
            """)
        spec = {
            "service_name": "headscale-test", "config_path": str(self.directory / "headscale"),
            "provisioning_tailnet_id": str(uuid.uuid4()), "ipv4_prefix": "100.64.0.0/26",
        }
        worker = object.__new__(Worker)
        worker.ledger = ledger
        worker.s = {}
        worker.spec = lambda _: spec
        worker.command = lambda *args: self.execute("headscale-test", *args[3:])
        # Candidate policy is tested, not declared as a proven Rawrarr transport.
        with patch("scripts.provisioning_worker.PEER_PROFILE", "candidate-test-only"), \
                patch("scripts.provisioning_worker.PEER_PORT", 443):
            worker.policy(spec)
            worker.policy(spec)
        policy = worker.headscale(spec, "policy", "get")
        if isinstance(policy, str):
            policy = json.loads(policy)
        self.assertEqual(policy["acls"], [
            {"action": "accept", "src": ["tag:account-node"],
             "dst": ["tag:account-node:443"], "proto": "tcp"},
        ])

        def request(node):
            request = {
                "account_id": "11111111-1111-4111-8111-111111111111",
                "tailnet_id": spec["provisioning_tailnet_id"],
                "enrollment_id": str(uuid.uuid4()),
                "node_id": "fossil-" + node * 24, "setup_intent_id": str(uuid.uuid4()),
            }
            with contextlib.closing(sqlite3.connect(ledger)) as db, db:
                db.execute("INSERT INTO enrollments VALUES(?,?,?,?)",
                           (request["enrollment_id"], request["account_id"],
                            request["node_id"], json.dumps(request)))
            return request

        first = request("a")
        receipt = worker.enrollment(first)
        self.assertEqual(receipt["state"], "issued")
        first["receipt_key"] = receipt["auth_key"]
        self.assertEqual(worker.enrollment(first)["auth_key"], receipt["auth_key"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda client: self.login(client, receipt["auth_key"]),
                ("client-a", "client-b"),
            ))
        self.assertEqual(sum(results), 1, "exactly one fresh client may consume a key: "
                         + repr(getattr(self, "login_errors", [])))
        winner = "client-a" if results[0] else "client-b"
        loser = "client-b" if results[0] else "client-a"
        self.assertEqual(worker.enrollment(first)["state"], "consumed")
        self.assertFalse(self.login(loser, receipt["auth_key"], "3s"))
        nodes = worker.headscale(spec, "nodes", "list")
        self.assertEqual(len(nodes), 1)
        self.assertTrue(nodes[0].get("nodeKey") or nodes[0].get("node_key"))
        second = request("b")
        key2 = worker.enrollment(second)["auth_key"]
        self.assertTrue(self.login(loser, key2))
        self.assertEqual(len(worker.headscale(spec, "nodes", "list")), 2)
        active = worker.mapping(first)
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["enrollment_id"], first["enrollment_id"])
        self.assertEqual(active["setup_intent_id"], first["setup_intent_id"])
        self.assertEqual(active["device"]["ipv4"],
                         self.execute(winner, "tailscale", "ip", "-4").strip())
        authoritative = next(n for n in worker.headscale(spec, "nodes", "list")
                             if str(n["id"]) == active["device"]["id"])
        self.assertEqual(active["device"]["public_key"],
                         authoritative.get("nodeKey", authoritative.get("node_key")))
        self.compose("restart", winner)
        self.wait_for("same-client reconnect", lambda: json.loads(self.execute(
            winner, "tailscale", "status", "--json"))["BackendState"] == "Running")

        # Listening forbidden ports ensures rejection is ACL enforcement, not no service.
        address = self.execute(loser, "tailscale", "ip", "-4").strip()
        for port in (443, 8080, 8082, 8385, 7878, 8989, 8686, 5432, 22, 22000):
            self.compose("exec", "--detach", loser, "nc", "-lk",
                         "-s", address, "-p", str(port), "-e", "/bin/cat")
        payload = "candidate-policy-packet\n"
        self.wait_for("narrow peer TCP datapath", lambda: self.execute(
            winner, "nc", "-w", "2", address, "443", input=payload, timeout=5))
        for port in (8080, 8082, 8385, 7878, 8989, 8686, 5432, 22, 22000):
            result = subprocess.run([
                "docker", "compose", "--project-name", self.project,
                "--file", str(self.compose_path), "exec", "-T", winner,
                "nc", "-w", "1", address, str(port),
            ], input=payload, text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.stdout, payload, f"port {port} must be denied")
        self.compose("exec", "--detach", loser, "nc", "-u", "-lk",
                     "-s", address, "-p", "443", "-e", "/bin/cat")
        udp = self.execute(winner, "sh", "-c",
                           f"printf candidate-udp | nc -u -w 1 {address} 443 || true", timeout=5)
        self.assertNotIn("candidate-udp", udp)
        # Advertise real fixture-only routes with listening destinations: denial
        # cannot be attributed to a missing service or missing route approval.
        for destination in ("192.0.2.1", "198.51.100.7"):
            self.execute(loser, "ip", "addr", "add", destination + "/32", "dev", "lo")
            self.compose("exec", "--detach", loser, "nc", "-lk", "-s",
                         destination, "-p", "443", "-e", "/bin/cat")
            self.wait_for("fixture route listener", lambda destination=destination: self.execute(
                loser, "nc", "-w", "1", destination, "443", input=payload, timeout=5) == payload)
        self.execute(loser, "tailscale", "set",
                     "--advertise-routes=192.0.2.0/24,198.51.100.7/32")
        second_device = worker.mapping(second)["device"]["id"]
        self.execute("headscale-test", "headscale", "nodes", "approve-routes",
                     "--identifier", second_device, "--routes", "192.0.2.0/24,198.51.100.7/32")
        approved = next(n for n in worker.headscale(spec, "nodes", "list")
                        if str(n["id"]) == second_device)
        self.assertEqual(set(approved.get("approved_routes", approved.get("approvedRoutes", []))),
                         {"192.0.2.0/24", "198.51.100.7/32"})
        self.execute(winner, "tailscale", "set", "--accept-routes=true")
        for destination in ("192.0.2.1", "198.51.100.7"):
            route = subprocess.run([
                "docker", "compose", "--project-name", self.project,
                "--file", str(self.compose_path), "exec", "-T", winner,
                "ip", "route", "get", destination,
            ], capture_output=True, text=True, timeout=5)
            self.assertTrue(
                (route.returncode == 0 and "tailscale0" in route.stdout)
                or (route.returncode != 0 and "Network unreachable" in route.stderr),
                "fixture traffic must not escape through a non-tailnet route",
            )
            result = self.execute(winner, "sh", "-c",
                                  f"printf denied-route | nc -w 1 {destination} 443 || true", timeout=5)
            self.assertNotIn("denied-route", result)

        third = request("c")
        receipt3 = worker.enrollment(third)
        self.credentials.append(receipt3["auth_key"])
        self.assertTrue(worker.revoke(third)["ok"])
        self.assertNotEqual(worker.enrollment(third)["state"], "issued")
        expiry_user = worker.headscale(spec, "users", "create", "expiry-test")
        expiring = worker.headscale(spec, "preauthkeys", "create",
                                   "--user", str(expiry_user["id"]), "--expiration", "1s")
        self.credentials.append(expiring["key"])
        time.sleep(2)
        self.assertEqual(worker.key_state(expiring), "expired")
        self.execute(loser, "tailscale", "logout")
        self.assertFalse(self.login(loser, expiring["key"], "3s"))
        self.assertFalse(self.login(loser, receipt3["auth_key"], "3s"))
        foreign_user = json.loads(self.execute(
            "foreign-headscale", "headscale", "users", "create",
            "foreign-account", "--output", "json"))
        foreign_key = json.loads(self.execute(
            "foreign-headscale", "headscale", "preauthkeys", "create",
            "--user", str(foreign_user["id"]), "--expiration", "15m",
            "--output", "json"))["key"]
        self.credentials.append(foreign_key)
        self.assertFalse(self.login(loser, foreign_key, "3s"),
                         "a key from another tenant instance must be rejected")
        self.assertTrue(self.login("foreign-client", foreign_key))
        foreign_ip = self.execute("foreign-client", "tailscale", "ip", "-4").strip()
        self.compose("exec", "--detach", "foreign-client", "nc", "-lk",
                     "-s", foreign_ip, "-p", "443", "-e", "/bin/cat")
        self.compose("exec", "--detach", winner, "nc", "-lk",
                     "-s", active["device"]["ipv4"], "-p", "443", "-e", "/bin/cat")
        for source, destination in ((winner, foreign_ip), ("foreign-client", active["device"]["ipv4"])):
            result = self.execute(source, "sh", "-c",
                                  f"printf cross-account | nc -w 1 {destination} 443 || true",
                                  timeout=5)
            self.assertNotIn("cross-account", result)
            status = json.loads(self.execute(source, "tailscale", "status", "--json"))
            peer_ips = [ip for peer in (status.get("Peer") or {}).values()
                        for ip in peer.get("TailscaleIPs", [])]
            self.assertNotIn(destination, peer_ips)
        with contextlib.closing(sqlite3.connect(ledger)) as db, db:
            db.execute("UPDATE enrollments SET node=? WHERE id=?",
                       (first["node_id"], second["enrollment_id"]))
        from scripts.provisioning_worker import Failure
        with self.assertRaises(Failure):
            worker.mapping(first)
        with contextlib.closing(sqlite3.connect(ledger)) as db, db:
            db.execute("UPDATE enrollments SET node=? WHERE id=?",
                       (second["node_id"], second["enrollment_id"]))
        self.execute("headscale-test", "headscale", "nodes", "delete",
                     "--identifier", active["device"]["id"], "--force")
        removed = worker.mapping(first)
        self.assertEqual(removed["state"], "removed")
        self.assertIsNone(removed["device"])
        self.keep_stack = False


if __name__ == "__main__":
    unittest.main()
