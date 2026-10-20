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
    def test_droplet_requires_global_ipv4_and_binds_caddy_to_it(self):
        config = sample_config()
        config["deployment"] = {"role": "droplet", "public_ipv4": "8.8.8.8"}
        config["caddy"]["listen_ip"] = "8.8.8.8"

        compose = generate_compose(config)
        self.assertIn("8.8.8.8:80:80", compose["services"]["caddy"]["ports"])
        self.assertIn("8.8.8.8:443:443", compose["services"]["caddy"]["ports"])

    def test_droplet_rejects_non_global_ipv4(self):
        for address in ("192.168.1.10", "100.64.0.1", "203.0.113.10"):
            with self.subTest(address=address):
                config = sample_config()
                config["deployment"] = {"role": "droplet", "public_ipv4": address}
                with self.assertRaisesRegex(ValueError, "globally routable IPv4"):
                    generate_compose(config)

    def test_droplet_rejects_ipv6_address(self):
        config = sample_config()
        config["deployment"] = {"role": "droplet", "public_ipv4": "2001:db8::1"}

        with self.assertRaisesRegex(ValueError, "requires a valid public IPv4"):
            generate_compose(config)

    def test_droplet_requires_canonical_string_ipv4(self):
        config = sample_config()
        config["deployment"] = {"role": "droplet", "public_ipv4": 134744072}

        with self.assertRaisesRegex(ValueError, "requires a valid public IPv4"):
            generate_compose(config)

    def test_droplet_caddy_must_bind_to_its_public_ipv4(self):
        config = sample_config()
        config["deployment"] = {"role": "droplet", "public_ipv4": "8.8.8.8"}

        with self.assertRaisesRegex(ValueError, "must bind to deployment.public_ipv4"):
            generate_compose(config)

    def test_droplet_requires_public_ipv4(self):
        config = sample_config()
        config["deployment"] = {"role": "droplet"}

        with self.assertRaisesRegex(ValueError, "requires a valid public IPv4"):
            generate_compose(config)

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

    def test_caddy_routes_additional_service_on_an_explicit_external_network(self):
        config = sample_config()
        config["caddy"].update({
            "external_networks": ["rawrarr-coordinator-ingress"],
            "proxy_routes": [{
                "hostname": "swarmsail.example.test",
                "upstream": "coordinator:8080",
                "health_upstream": "control-plane:8787",
            }],
        })
        compose = generate_compose(config)
        self.assertEqual(
            compose["services"]["caddy"]["networks"],
            ["test-net", "rawrarr-coordinator-ingress"],
        )
        self.assertEqual(
            compose["networks"]["rawrarr-coordinator-ingress"],
            {"external": True, "name": "rawrarr-coordinator-ingress"},
        )
        with tempfile.TemporaryDirectory() as directory:
            config["caddy"]["config_dir"] = directory
            ensure_caddy_config(config, Path(directory))
            self.assertIn(
                "swarmsail.example.test {\n    @swarmsail_health path /health\n    handle @swarmsail_health {\n        reverse_proxy control-plane:8787\n    }\n    handle {\n        reverse_proxy coordinator:8080",
                (Path(directory) / "Caddyfile").read_text(),
            )

    def test_caddy_rejects_invalid_health_upstream(self):
        config = sample_config()
        config["caddy"].update({
            "external_networks": ["rawrarr-coordinator-ingress"],
            "proxy_routes": [{
                "hostname": "swarmsail.example.test",
                "upstream": "coordinator:8080",
                "health_upstream": "bad upstream",
            }],
        })
        with self.assertRaisesRegex(ValueError, "health_upstream"):
            generate_compose(config)

    def test_caddy_proxy_route_requires_a_scoped_external_network(self):
        config = sample_config()
        config["caddy"]["proxy_routes"] = [
            {"hostname": "swarmsail.example.test", "upstream": "coordinator:8080"}
        ]
        with self.assertRaisesRegex(ValueError, "explicit external Docker network"):
            generate_compose(config)

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
