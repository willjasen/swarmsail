"""Subscriber account mappings must be isolated and stable across retries."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from swarmsail_web import AppServer, Handler, InputError, Store


ACCOUNT_A = "11111111-1111-4111-8111-111111111111"
ACCOUNT_B = "22222222-2222-4222-8222-222222222222"
INSTANCES = [
    {"id": 10, "management_mode": "managed", "subscriber_account_id": ACCOUNT_A,
     "subscriber_generation": 1},
    {"id": 11, "management_mode": "managed", "subscriber_account_id": ACCOUNT_A,
     "subscriber_generation": 2},
    {"id": 1, "management_mode": "adopted"},
]


class SubscriberTailnetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "swarmsail.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_one_account_gets_one_isolated_managed_instance(self):
        first = self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES)
        retry = self.store.link_subscriber_tailnet(ACCOUNT_A, "10", INSTANCES)
        self.assertEqual(first, retry)
        state = self.store.organization_state(first["organization_id"], INSTANCES)
        self.assertEqual(state["organization"]["mode"], "isolated")
        self.assertEqual(state["organization"]["headscale_instance"], "10")
        self.assertEqual(state["connections"], [])
        self.assertNotIn("subscriber_account_id", state["tailnet"])
        self.assertNotIn("subscriber_account_id", self.store.state(INSTANCES)["headscale_instances"][0])

    def test_reconciliation_never_resets_a_shared_account(self):
        linked = self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES)
        self.store.enable_sharing(linked["organization_id"])
        self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES)
        state = self.store.organization_state(linked["organization_id"], INSTANCES)
        self.assertEqual(state["organization"]["mode"], "collaborative")

    def test_new_private_generation_preserves_shared_predecessor(self):
        old = self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES)
        self.store.enable_sharing(old["organization_id"])
        new = self.store.link_subscriber_tailnet(ACCOUNT_A, 11, INSTANCES)
        self.assertEqual(new["generation"], 2)
        self.assertEqual(new["active"], 1)
        self.assertNotEqual(new["organization_id"], old["organization_id"])
        with self.store.connect() as db:
            previous = db.execute(
                "SELECT active FROM subscriber_tailnets WHERE id=?", (old["id"],)
            ).fetchone()
        self.assertEqual(previous["active"], 0)
        old_state = self.store.organization_state(old["organization_id"], INSTANCES)
        new_state = self.store.organization_state(new["organization_id"], INSTANCES)
        self.assertEqual(old_state["organization"]["mode"], "collaborative")
        self.assertEqual(new_state["organization"]["mode"], "isolated")

    def test_binding_cannot_be_reassigned_with_direct_sql(self):
        linked = self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE subscriber_tailnets SET account_id=? WHERE id=?",
                    (ACCOUNT_B, linked["id"]),
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE organizations SET headscale_instance='11' WHERE id=?",
                    (linked["organization_id"],),
                )

    def test_account_and_instance_cannot_be_reassigned(self):
        self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES)
        with self.assertRaisesRegex(InputError, "shared predecessor"):
            self.store.link_subscriber_tailnet(ACCOUNT_A, 11, INSTANCES)
        with self.assertRaisesRegex(InputError, "another account"):
            self.store.link_subscriber_tailnet(ACCOUNT_B, 10, INSTANCES)

    def test_can_link_a_fresh_instance_already_seeded_for_inventory(self):
        self.store.sync_instances(INSTANCES)
        linked = self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES)
        self.assertEqual(linked["headscale_instance"], "10")
        self.assertEqual(self.store.link_subscriber_tailnet(ACCOUNT_A, 10, INSTANCES), linked)

    def test_web_startup_links_a_reserved_subscriber_instance(self):
        config_path = Path(self.directory.name) / "instances.json"
        config_path.write_text(json.dumps({"headscale": {"instances": [INSTANCES[0], INSTANCES[2]]}}))
        server = AppServer(("127.0.0.1", 0), Handler, self.store, config_path)
        try:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT headscale_instance FROM subscriber_tailnets WHERE account_id=?",
                    (ACCOUNT_A,),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["headscale_instance"], "10")
        finally:
            server.server_close()

    def test_adopted_or_unknown_instance_is_rejected(self):
        for instance_id in (1, 99):
            with self.subTest(instance_id=instance_id), self.assertRaises(InputError):
                self.store.link_subscriber_tailnet(ACCOUNT_A, instance_id, INSTANCES)

    def test_invalid_account_id_is_rejected(self):
        with self.assertRaisesRegex(InputError, "UUID"):
            self.store.link_subscriber_tailnet("person@example.test", 10, INSTANCES)


if __name__ == "__main__":
    unittest.main()
