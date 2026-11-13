"""Fault-injection tests: no live credentials, DNS or containers."""

import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.adopt_default_tailnet import adopt
from scripts.provisioning_worker import Failure, Worker, active_route_matches, canonical


ACCOUNT = "11111111-1111-4111-8111-111111111111"


class PrivateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.ledger = self.root / "ledger.db"
        self.config = self.root / "instances.json"
        self.config.write_text(json.dumps({
            "headscale": {"instances": []}, "npm": {"enabled": False},
            "crowdsec": {"enabled": False}, "caddy": {"enabled": False},
            "network": {"name": "private-worker-test", "driver": "bridge"},
        }))
        os.chmod(self.config, 0o600)
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            db.executescript("""
                CREATE TABLE provisioning_version(version INTEGER);
                INSERT INTO provisioning_version VALUES(1);
                CREATE TABLE account_defaults(account TEXT PRIMARY KEY,tailnet TEXT,spec TEXT);
                CREATE TABLE enrollments(id TEXT PRIMARY KEY,account TEXT,node TEXT,document TEXT);
                CREATE TABLE node_mappings(account TEXT,node TEXT,document TEXT);
                CREATE TABLE worker_receipts(resource TEXT PRIMARY KEY,value TEXT);
            """)
        settings = {
            "host_id": "do3", "config": str(self.config), "mapping_database": str(self.root / "mapping.db"),
            "data_root": str(self.root / "data"), "compose_project": "private-worker-test",
            "public_ipv4": "8.8.8.8", "ingress_container": "private-ingress-test",
            "ingress_file": str(self.root / "Caddyfile"), "ingress_container_file": "/etc/caddy/Caddyfile",
            "ingress_admin_socket": "/run/caddy/admin.sock", "ingress_baseline_sha256": "0" * 64,
            "ingress_tls_certificate": "/run/tls/wildcard.crt", "ingress_tls_key": "/run/tls/wildcard.key",
        }
        self.worker = Worker(settings, self.ledger)
        self.request = {"account_id": ACCOUNT, "tailnet_id": str(uuid.uuid4())}
        self.request["server_url"] = f'https://{self.request["tailnet_id"]}.swarm.rawrarr.net'
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("INSERT INTO account_defaults VALUES(?,?,NULL)",
                       (ACCOUNT, self.request["tailnet_id"]))

    def tearDown(self):
        self.directory.cleanup()

    def test_reservation_reconciles_same_identity_after_config_write_loss(self):
        with patch("scripts.provisioning_worker.write_config_atomically", side_effect=OSError):
            with self.assertRaises(OSError):
                self.worker.reserve(self.request)
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            original = json.loads(db.execute("SELECT spec FROM account_defaults").fetchone()[0])
        first, _ = self.worker.reserve(self.request)
        second, _ = self.worker.reserve(self.request)
        self.assertEqual(first, original)
        self.assertEqual(first, second)
        self.assertEqual(len(json.loads(self.config.read_text())["headscale"]["instances"]), 1)

    def test_existing_operator_generations_require_explicit_adoption(self):
        config = json.loads(self.config.read_text())
        config["headscale"]["instances"] = [{
            "id": 12, "management_mode": "managed", "subscriber_account_id": ACCOUNT,
            "subscriber_generation": 1, "web_port": 8093, "stun_port_udp": False,
            "ipv4_prefix": "100.64.1.0/26",
        }]
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(Failure, ""):
            try:
                self.worker.reserve(self.request)
            except Failure as error:
                self.assertEqual(error.code, "default_adoption_required")
                raise

    def test_adoption_preview_and_retry_preserve_bound_operation(self):
        spec, _ = self.worker.reserve(self.request)
        settings_path = self.root / "worker.json"
        settings_path.write_text(canonical(self.worker.s))
        os.chmod(settings_path, 0o600)
        with self.worker.db() as db:
            db.execute("ALTER TABLE account_defaults ADD COLUMN document TEXT")
            db.execute("ALTER TABLE account_defaults ADD COLUMN due INTEGER DEFAULT 0")
            db.execute("UPDATE account_defaults SET spec=NULL")
        args = SimpleNamespace(account_id=ACCOUNT, tailnet_id=self.request["tailnet_id"],
                               ledger=str(self.ledger), worker_settings=str(settings_path),
                               instance_id=spec["id"], apply=False)
        before = self.config.read_bytes()
        self.assertFalse(adopt(args)["applied"])
        self.assertEqual(self.config.read_bytes(), before)
        with self.worker.db() as db:
            self.assertIsNone(db.execute("SELECT spec FROM account_defaults").fetchone()[0])
        args.apply = True
        with patch("scripts.adopt_default_tailnet.write_config_atomically", side_effect=OSError):
            with self.assertRaises(OSError):
                adopt(args)
        with self.worker.db() as db:
            original = db.execute("SELECT document FROM account_defaults").fetchone()[0]
        self.assertTrue(adopt(args)["applied"])
        with self.worker.db() as db:
            self.assertEqual(db.execute("SELECT document FROM account_defaults").fetchone()[0],
                             original)
        args.tailnet_id = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            adopt(args)

    def test_external_config_drift_is_not_overwritten(self):
        self.worker.reserve(self.request)
        config = json.loads(self.config.read_text())
        config["headscale"]["instances"][0]["server_url"] = "https://other.example.test"
        self.config.write_text(json.dumps(config))
        with self.assertRaises(Failure) as result:
            self.worker.reserve(self.request)
        self.assertEqual(result.exception.code, "instance_config_drift")
        self.assertEqual(json.loads(self.config.read_text()), config)

    def test_dns_checks_do_not_assume_installed_wildcard_or_edit_records(self):
        spec, _ = self.worker.reserve(self.request)
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]), \
                patch.object(self.worker, "command", return_value=""):
            self.worker.dns(spec)
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.0.2.1", 443))]):
            with self.assertRaises(Failure) as result:
                self.worker.dns(spec)
        self.assertEqual(result.exception.code, "wildcard_dns_not_ready")

    def key_request(self):
        spec, _ = self.worker.reserve(self.request)
        request = dict(self.request, enrollment_id=str(uuid.uuid4()),
                       node_id="fossil-0123456789abcdef01234567", setup_intent_id=str(uuid.uuid4()))
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("INSERT INTO enrollments VALUES(?,?,?,?)",
                       (request["enrollment_id"], request["account_id"], request["node_id"], canonical(request)))
        return spec, request

    def test_unknown_key_creation_never_blindly_creates_again(self):
        spec, request = self.key_request()
        commands = []

        def headscale(_, *args):
            commands.append(args)
            if args[:2] == ("users", "list"):
                return [{"id": 1, "name": "enroll-" + request["enrollment_id"].replace("-", "")}]
            if args[:2] == ("preauthkeys", "create"):
                raise Failure("host_command_outcome_unknown")
            return []
        self.worker.headscale = headscale
        for _ in range(2):
            with self.assertRaises(Failure):
                self.worker.enrollment(request)
        self.assertEqual(sum(cmd[:2] == ("preauthkeys", "create") for cmd in commands), 1)

    def test_lost_receipt_recovers_original_and_detects_used_key(self):
        _, request = self.key_request()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
        original = "hskey-auth-" + "a" * 90
        request["receipt_key"] = original
        key = {"id": 1, "user": {"id": 1}, "key": "hskey-auth-short-id", "expiration": expiry.isoformat(),
               "aclTags": ["tag:account-node"]}
        username = "enroll-" + request["enrollment_id"].replace("-", "")

        def headscale(_, *args):
            if args[:2] == ("users", "list"):
                return [{"id": 1, "name": username}]
            if args[:2] == ("preauthkeys", "list"):
                return [key]
            self.fail("key creation must not occur during recovery")
        self.worker.headscale = headscale
        self.assertEqual(self.worker.enrollment(request)["auth_key"], original)
        self.assertEqual(self.worker.enrollment(request)["auth_key"], original)
        missing = dict(request)
        missing.pop("receipt_key")
        with self.assertRaises(Failure):
            self.worker.enrollment(missing)
        key["used"] = True
        result = self.worker.enrollment(request)
        self.assertEqual(result["state"], "consumed")
        self.assertNotIn("auth_key", result)

    def test_active_caddy_verification_matches_host_and_upstream_together(self):
        config = {"routes": [
            {"match": [{"host": ["a.example.test"]}], "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "first:8080"}]}]},
            {"match": [{"host": ["b.example.test"]}], "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "second:8080"}]}]},
        ]}
        self.assertTrue(active_route_matches(config, "a.example.test", "first:8080"))
        self.assertFalse(active_route_matches(config, "a.example.test", "second:8080"))

    def test_node_mapping_is_authoritative_and_ambiguous_or_removed_is_not_active(self):
        _, request = self.key_request()
        username = "enroll-" + request["enrollment_id"].replace("-", "")
        nodes = [{"id": 1, "user": {"id": 7}, "nodeKey": "nodekey:" + "a" * 64,
                  "ipAddresses": ["100.64.0.1"],
                  "pre_auth_key": {"id": 1, "user": {"id": 7}, "used": True}}]
        # Use actual allocator prefix, not a caller's claimed address.
        spec = self.worker.spec(ACCOUNT)
        import ipaddress
        nodes[0]["ipAddresses"] = [str(next(ipaddress.ip_network(spec["ipv4_prefix"]).hosts()))]

        def headscale(_, *args):
            if args[:2] == ("users", "list"):
                return [{"id": 7, "name": username}]
            if args[:2] == ("nodes", "list"):
                return nodes
            return [{"id": 1, "user": {"id": 7}, "used": True}]
        self.worker.headscale = headscale
        result = self.worker.mapping(request)
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["device"]["public_key"], nodes[0]["nodeKey"])
        self.assertEqual(result["device"]["ipv4"], nodes[0]["ipAddresses"][0])
        nodes[0]["user"] = {"id": 2147455555, "name": "tagged-devices"}
        self.assertEqual(self.worker.mapping(request)["state"], "active")
        nodes[0]["pre_auth_key"]["id"] = 2
        with self.assertRaises(Failure):
            self.worker.mapping(request)
        nodes[0]["pre_auth_key"]["id"] = 1
        nodes.append(dict(nodes[0], id=2))
        with self.assertRaises(Failure):
            self.worker.mapping(request)
        nodes.clear()
        result = self.worker.mapping(request)
        self.assertEqual(result["state"], "removed")
        self.assertIsNone(result["device"])

    def test_unavailable_peer_profile_cannot_be_marked_ready(self):
        spec, _ = self.worker.reserve(self.request)
        with self.assertRaises(Failure) as result:
            self.worker.policy(spec)
        self.assertEqual(result.exception.code, "peer_profile_unavailable")


if __name__ == "__main__":
    unittest.main()
