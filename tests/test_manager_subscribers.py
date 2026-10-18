"""Managed subscriber instances must be dedicated and well formed."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from manage_headscale import generate_compose


ACCOUNT = "abcdefab-1111-4111-8111-111111111111"


def sample_config():
    return {
        "headscale": {"instances": [{
            "id": 10,
            "web_port": 8090,
            "stun_port_udp": False,
            "ipv4_prefix": "100.64.0.0/26",
            "subscriber_account_id": ACCOUNT,
            "subscriber_generation": 1,
        }]},
        "npm": {"enabled": False},
        "crowdsec": {"enabled": False},
        "network": {"name": "test-net", "driver": "bridge"},
    }


class SubscriberManagerTests(unittest.TestCase):
    def test_managed_subscriber_has_a_private_service(self):
        compose = generate_compose(sample_config())
        service = compose["services"]["headscale-10"]
        self.assertNotIn("ports", service)
        self.assertEqual(service["networks"], ["test-net"])

    def test_same_account_cannot_have_duplicate_generations(self):
        config = sample_config()
        second = copy.deepcopy(config["headscale"]["instances"][0])
        second.update({"id": 11, "web_port": 8091, "ipv4_prefix": "100.64.0.64/26"})
        config["headscale"]["instances"].append(second)
        with self.assertRaisesRegex(ValueError, "duplicate generation"):
            generate_compose(config)

    def test_subscriber_cannot_claim_adopted_instance(self):
        config = sample_config()
        config["headscale"]["instances"][0]["management_mode"] = "adopted"
        with self.assertRaisesRegex(ValueError, "managed subscriber"):
            generate_compose(config)

    def test_subscriber_id_must_be_canonical(self):
        config = sample_config()
        config["headscale"]["instances"][0]["subscriber_account_id"] = ACCOUNT.upper()
        with self.assertRaisesRegex(ValueError, "canonical managed"):
            generate_compose(config)

    def test_generate_cli_previews_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "instances.json"
            config_path.write_text(json.dumps(sample_config()))
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "manage_headscale.py"),
                 "generate", "--config", str(config_path)],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("no files changed", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [config_path])

    def test_generate_apply_creates_private_headscale_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "instances.json"
            config_path.write_text(json.dumps(sample_config()))
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "manage_headscale.py"),
                 "generate", "--config", str(config_path), "--apply"],
                check=True, capture_output=True, text=True,
            )
            headscale = (root / "headscale/10/config/config.yaml").read_text()
            self.assertIn("100.64.0.0/26", headscale)
            self.assertIn("policy:\n  mode: database", headscale)
            compose = (root / "docker-compose.yml").read_text()
            self.assertIn("headscale-10:", compose)
            self.assertNotIn("8090:8080", compose)


if __name__ == "__main__":
    unittest.main()
