"""Caddy ingress generation stays tied to managed Headscale services."""

import copy
import tempfile
import unittest
from pathlib import Path

from manage_headscale import ensure_caddy_config, generate_compose


def sample_config():
    return {
        "headscale": {"instances": [{
            "id": 1,
            "web_port": 8080,
            "service_name": "headscale-owner",
            "server_url": "https://headscale.example.test",
            "publish_web_port": False,
            "stun_port_udp": False,
            "ipv4_prefix": "100.64.0.0/26",
        }]},
        "npm": {"enabled": False},
        "crowdsec": {"enabled": False},
        "caddy": {
            "enabled": True,
            "listen_ip": "192.0.2.20",
            "config_dir": "/tmp/swarmsail-caddy/config",
            "data_dir": "/tmp/swarmsail-caddy/data",
            "state_dir": "/tmp/swarmsail-caddy/state",
            "cloudflare_token_path": "/tmp/cf-token",
        },
        "network": {"name": "test-net", "driver": "bridge"},
    }


class CaddyManagerTests(unittest.TestCase):
    def test_caddy_routes_hostname_to_private_headscale_service(self):
        compose = generate_compose(sample_config())
        caddy = compose["services"]["caddy"]
        self.assertIn("192.0.2.20:80:80", caddy["ports"])
        self.assertIn("192.0.2.20:443:443", caddy["ports"])
        self.assertNotIn("443/udp", str(caddy["ports"]))
        self.assertTrue(any("/Caddyfile:/etc/caddy/Caddyfile:ro" in volume for volume in caddy["volumes"]))
        self.assertNotIn("ports", compose["services"]["headscale-owner"])

    def test_generated_caddyfile_uses_secret_file_placeholder(self):
        config = sample_config()
        with tempfile.TemporaryDirectory() as directory:
            config["caddy"]["config_dir"] = directory
            ensure_caddy_config(config, Path(directory))
            content = (Path(directory) / "Caddyfile").read_text()
            self.assertIn("acme_dns cloudflare {file./run/secrets/cloudflare-dns-token}", content)
            self.assertIn("headscale.example.test {", content)
            self.assertIn("reverse_proxy headscale-owner:8080", content)
            self.assertNotIn("/tmp/cf-token", content)
            self.assertEqual((Path(directory) / "Caddyfile").stat().st_mode & 0o777, 0o600)

    def test_caddy_and_npm_cannot_both_own_ingress_ports(self):
        config = sample_config()
        config["npm"]["enabled"] = True
        with self.assertRaisesRegex(ValueError, "cannot both own"):
            generate_compose(config)

    def test_caddy_rejects_directly_published_headscale_web_port(self):
        config = sample_config()
        config["headscale"]["instances"][0]["publish_web_port"] = True
        with self.assertRaisesRegex(ValueError, "keep its web port private"):
            generate_compose(config)

    def test_caddy_rejects_duplicate_proxy_hosts(self):
        config = sample_config()
        second = copy.deepcopy(config["headscale"]["instances"][0])
        second.update({"id": 2, "service_name": "headscale-second", "ipv4_prefix": "100.64.0.64/26"})
        config["headscale"]["instances"].append(second)
        with self.assertRaisesRegex(ValueError, "duplicate Caddy hostname"):
            generate_compose(config)


if __name__ == "__main__":
    unittest.main()
