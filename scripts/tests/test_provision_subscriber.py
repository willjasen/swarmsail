"""Plan and reserve subscriber tailnets without touching live infrastructure."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.provision_subscriber import plan, run
from swarmsail_web import Store


ACCOUNT = "abcdefab-1111-4111-8111-111111111111"
EXPIRES = "2099-01-01T00:00:00Z"


def config():
    return {
        "headscale": {"instances": [{
            "id": 1, "management_mode": "adopted", "web_port": 8081,
            "ipv4_prefix": "100.64.1.0/24",
        }]},
        "npm": {"enabled": False},
        "crowdsec": {"enabled": False},
        "network": {"name": "test-net", "driver": "bridge"},
    }


class ProvisionSubscriberTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.config_path = root / "instances.json"
        self.config_path.write_text(json.dumps(config()) + "\n")
        self.database = root / "swarmsail.db"
        self.args = SimpleNamespace(
            config=str(self.config_path), database=str(self.database), account_id=ACCOUNT,
            eligibility="trial", eligible_until=EXPIRES,
            server_url="https://headscale-test.example.test",
            base_domain="tailnet-test.example.test", data_root=str(root / "data"),
            apply=False,
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_dry_run_changes_neither_config_nor_database(self):
        before = self.config_path.read_bytes()
        result = run(self.args)
        self.assertEqual(result["action"], "reserve")
        self.assertFalse(result["applied"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.database.exists())
        self.args.apply = True
        self.assertEqual(run(self.args)["ipv4_prefix"], result["ipv4_prefix"])

    def test_apply_is_idempotent_and_preserves_adopted_instance(self):
        self.args.apply = True
        first = run(self.args)
        second = run(self.args)
        self.assertEqual(first["instance_id"], second["instance_id"])
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(len(saved["headscale"]["instances"]), 2)
        self.assertEqual(saved["headscale"]["instances"][0], config()["headscale"]["instances"][0])
        subscriber = saved["headscale"]["instances"][1]
        self.assertEqual(subscriber["subscriber_account_id"], ACCOUNT)
        self.assertEqual(subscriber["organization_mode"], "isolated")
        self.assertEqual(subscriber["publish_web_port"], False)
        self.assertTrue(subscriber["ipv4_prefix"].startswith("100."))
        state = Store(self.database).state(saved["headscale"]["instances"])
        self.assertEqual(len(state["organizations"]), 1)
        self.assertEqual(state["organizations"][0]["mode"], "isolated")

    def test_expired_eligibility_is_rejected_without_mutation(self):
        self.args.eligible_until = "2000-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "still be active"):
            run(self.args)
        self.assertFalse(self.database.exists())

    def test_existing_account_is_not_moved_to_another_origin(self):
        updated, _, _ = plan(config(), ACCOUNT, "subscription", EXPIRES,
                             self.args.server_url, self.args.base_domain, self.args.data_root)
        with self.assertRaisesRegex(ValueError, "already reserved"):
            plan(updated, ACCOUNT, "subscription", EXPIRES,
                 "https://different.example.test", self.args.base_domain, self.args.data_root)

    def test_account_can_reserve_multiple_independent_active_tailnets(self):
        self.args.apply = True
        first = run(self.args)
        self.args.server_url = "https://headscale-next.example.test"
        self.args.base_domain = "tailnet-next.example.test"
        self.args.apply = False
        preview = run(self.args)
        self.assertNotEqual(preview["instance_id"], first["instance_id"])
        self.args.apply = True
        applied = run(self.args)
        self.assertEqual(applied["ipv4_prefix"], preview["ipv4_prefix"])
        self.assertEqual(run(self.args)["action"], "already_reserved")
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(len(saved["headscale"]["instances"]), 3)
        state = Store(self.database).state(saved["headscale"]["instances"])
        self.assertEqual(
            [organization["mode"] for organization in state["organizations"]],
            ["isolated", "isolated"],
        )
        with Store(self.database).connect() as db:
            rows = db.execute(
                "SELECT generation, active FROM subscriber_tailnets WHERE account_id=? ORDER BY generation",
                (ACCOUNT,),
            ).fetchall()
        self.assertEqual([(row["generation"], row["active"]) for row in rows], [(1, 1), (2, 1)])


if __name__ == "__main__":
    unittest.main()
