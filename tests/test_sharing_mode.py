"""Regression checks for the irreversible isolated-to-shared transition."""

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The deployment provides manage_headscale.py, which this checkout does not contain.
sys.modules.setdefault("manage_headscale", types.SimpleNamespace(
    generate_command=lambda path: 0,
    load_config=lambda path: {"headscale": {"instances": []}},
))

from swarmsail_web import InputError, Store  # noqa: E402


class SharingModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "swarmsail.db")
        self.organization = self.store.create_organization({
            "name": "subscriber", "mode": "isolated", "headscale_instance": "dedicated-1",
        })

    def tearDown(self):
        self.directory.cleanup()

    def test_transition_is_idempotent_and_does_not_create_grants(self):
        organization_id = self.organization["id"]
        self.assertEqual(self.store.enable_sharing(organization_id)["mode"], "collaborative")
        self.assertEqual(self.store.enable_sharing(organization_id)["mode"], "collaborative")
        state = self.store.organization_state(organization_id, [])
        self.assertEqual(state["connections"], [])
        self.assertEqual(state["shares"], [])
        self.assertEqual(state["acl_rules"], [])

    def test_database_rejects_reversion_even_from_direct_sql(self):
        self.store.enable_sharing(self.organization["id"])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot become isolated"):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE organizations SET mode='isolated' WHERE id=?",
                    (self.organization["id"],),
                )

    def test_existing_database_gets_the_guard_on_open(self):
        with self.store.connect() as db:
            db.execute("DROP TRIGGER organization_no_private_reversion")
            db.execute(
                "UPDATE organizations SET mode='collaborative' WHERE id=?",
                (self.organization["id"],),
            )
        Store(Path(self.directory.name) / "swarmsail.db")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot become isolated"):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE organizations SET mode='isolated' WHERE id=?",
                    (self.organization["id"],),
                )

    def test_existing_isolated_organization_cannot_share(self):
        other = self.store.create_organization({
            "name": "other", "mode": "collaborative",
        })
        with self.assertRaisesRegex(InputError, "isolated"):
            self.store.create_connection({
                "organization_a_id": self.organization["id"],
                "organization_b_id": other["id"],
            })

    def test_unknown_organization_is_rejected(self):
        with self.assertRaises(InputError):
            self.store.enable_sharing(9999)


if __name__ == "__main__":
    unittest.main()
