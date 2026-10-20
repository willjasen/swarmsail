"""Headscale Crew administration must remain scoped and confirmation-gated."""

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from swarmsail_web import AppServer, CROWDSEC_PAGE, DASHBOARD, Handler, InputError, Store


class AdminManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config_path = self.root / "instances.json"
        self.config_path.write_text(json.dumps({
            "headscale": {"instances": [{
                "id": 7,
                "service_name": "headscale-seven",
                "server_url": "https://mast.example.test",
            }]},
        }))
        self.inventory_path = self.root / "node-inventory.json"
        self.inventory = [{
            "instance_id": "7",
            "users": [{"id": 9, "name": "alex"}],
            "nodes": [{
                "id": 100,
                "given_name": "alex-mac",
                "user_name": "alex",
                "ip_addresses": ["100.64.0.1"],
            }],
        }]
        self.inventory_path.write_text(json.dumps(self.inventory))
        self.store = Store(self.root / "swarmsail.db")
        self.organization = self.store.create_organization({
            "name": "Northstar",
            "mode": "isolated",
            "headscale_instance": "7",
        })
        self.store.sync_node_inventory(self.inventory)
        self.server = object.__new__(AppServer)
        self.server.config_path = self.config_path
        self.server.node_inventory = self.inventory_path
        self.server.inventory_cache = self.inventory
        self.server.store = self.store

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def completed(stdout=""):
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def test_navigation_and_matey_device_filter_are_available(self):
        self.assertIn('<details class="crew-picker" open>', DASHBOARD)
        self.assertIn('id="sidebar-crews"', DASHBOARD)
        self.assertIn('href="/#setup"', DASHBOARD)
        self.assertIn('data-page="policy"', DASHBOARD)
        self.assertIn('href="/crowdsec"', DASHBOARD)
        self.assertIn('id="device-matey-filter"', DASHBOARD)
        self.assertIn("function renderFilteredDevices()", DASHBOARD)
        self.assertIn('id="crowdsec-crews"', CROWDSEC_PAGE)
        self.assertIn('aria-current="page">Security</a>', CROWDSEC_PAGE)

    def test_matey_removal_requires_explicit_confirmation(self):
        with patch("swarmsail_web.subprocess.run") as run:
            with self.assertRaisesRegex(InputError, "confirm"):
                self.server.delete_headscale_user(
                    self.organization, {
                        "user_id": 9, "user_name": "alex",
                        "device_count": 1, "confirm": False,
                    }
                )
        run.assert_not_called()

    def test_matey_removal_destroys_headscale_user_and_owned_devices(self):
        results = [
            self.completed(json.dumps([{"id": 9, "name": "alex"}])),
            self.completed(json.dumps([{
                "id": 100,
                "user": {"id": 9, "name": "alex"},
            }])),
            self.completed(),
        ]
        with patch("swarmsail_web.subprocess.run", side_effect=results) as run:
            result = self.server.delete_headscale_user(
                self.organization, {
                    "user_id": 9, "user_name": "alex",
                    "device_count": 1, "confirm": True,
                }
            )

        self.assertEqual(result, {"deleted": True, "devices_removed": 1})
        self.assertEqual(run.call_args_list[-1].args[0], [
            "docker", "exec", "headscale-seven", "headscale", "users", "destroy",
            "--identifier", "9", "--force",
        ])
        state = self.store.organization_state(self.organization["id"], [])
        self.assertEqual(state["devices"], [])
        self.assertEqual(self.server.inventory_cache[0]["users"], [])
        self.assertEqual(self.server.inventory_cache[0]["nodes"], [])
        self.assertEqual(json.loads(self.inventory_path.read_text())[0]["users"], [])

    def test_matey_removal_rejects_a_stale_device_count(self):
        results = [
            self.completed(json.dumps([{"id": 9, "name": "alex"}])),
            self.completed(json.dumps([{
                "id": 100,
                "user": {"id": 9, "name": "alex"},
            }])),
        ]
        with patch("swarmsail_web.subprocess.run", side_effect=results) as run:
            with self.assertRaisesRegex(InputError, "inventory changed"):
                self.server.delete_headscale_user(
                    self.organization, {
                        "user_id": 9, "user_name": "alex",
                        "device_count": 0, "confirm": True,
                    }
                )
        self.assertEqual(run.call_count, 2)

    def test_device_removal_is_crew_scoped_and_revokes_headscale_node(self):
        device = self.store.state([])["devices"][0]
        results = [
            self.completed(json.dumps([{"id": 100, "given_name": "alex-mac"}])),
            self.completed(),
        ]
        with patch("swarmsail_web.subprocess.run", side_effect=results) as run:
            result = self.server.delete_device(
                self.organization, {
                    "device_id": device["id"], "device_name": device["name"],
                    "confirm": True,
                }
            )

        self.assertEqual(result, {"deleted": True, "revoked": True})
        self.assertEqual(run.call_args_list[-1].args[0], [
            "docker", "exec", "headscale-seven", "headscale", "nodes", "delete",
            "--identifier", "100", "--force",
        ])
        self.assertEqual(self.store.state([])["devices"], [])
        self.assertEqual(self.server.inventory_cache[0]["nodes"], [])

    def test_manual_device_removal_only_deletes_local_inventory(self):
        device = self.store.create_device({
            "organization_id": self.organization["id"],
            "name": "local-only",
        })
        with patch("swarmsail_web.subprocess.run") as run:
            result = self.server.delete_device(
                self.organization, {
                    "device_id": device["id"], "device_name": device["name"],
                    "confirm": True,
                }
            )

        self.assertEqual(result, {"deleted": True, "revoked": False})
        run.assert_not_called()

    def test_delete_endpoint_scopes_matey_to_crew_and_requires_body_confirmation(self):
        payload = json.dumps({
            "confirm": True,
            "user_name": "alex",
            "device_count": 1,
        }).encode()
        handler = object.__new__(Handler)
        handler.path = "/api/organizations/12/users/9"
        handler.server = MagicMock()
        handler.server.instances.return_value = []
        handler.server.store.organization_state.return_value = {
            "organization": {"id": 12},
        }
        handler.server.delete_headscale_user.return_value = {"deleted": True}
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.send_json = Mock()

        Handler.do_DELETE(handler)

        handler.server.refresh_inventory.assert_called_once_with()
        handler.server.store.organization_state.assert_called_once_with(12, [])
        handler.server.delete_headscale_user.assert_called_once_with(
            {"id": 12}, {
                "confirm": True, "user_name": "alex",
                "device_count": 1, "user_id": "9",
            },
        )
        handler.send_json.assert_called_once_with({"deleted": True})


if __name__ == "__main__":
    unittest.main()
