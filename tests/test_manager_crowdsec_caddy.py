"""CrowdSec protection for Swarmsail-managed Caddy ingress."""

import tempfile
import unittest
from pathlib import Path

from manage_headscale import (
    ensure_caddy_config,
    ensure_crowdsec_config,
    generate_caddyfile,
    generate_compose,
)


def sample_config():
    return {
        "headscale": {"instances": [{
            "id": 1,
            "web_port": 8081,
            "stun_port_udp": False,
            "ipv4_prefix": "100.64.0.0/26",
            "server_url": "https://headscale.example.net",
        }]},
        "npm": {"enabled": False},
        "crowdsec": {
            "enabled": True,
            "timezone": "UTC",
            "bouncer_api_key_path": "/var/lib/swarmsail/crowdsec/caddy-bouncer-key",
        },
        "caddy": {
            "enabled": True,
            "listen_ip": "192.0.2.10",
            "config_dir": "/var/lib/swarmsail/caddy/config",
            "data_dir": "/var/lib/swarmsail/caddy/data",
            "state_dir": "/var/lib/swarmsail/caddy/state",
            "log_dir": "/var/lib/swarmsail/caddy/logs",
            "cloudflare_token_path": "/var/lib/swarmsail/caddy/cloudflare-token",
            "external_networks": ["status-net"],
            "proxy_routes": [{
                "hostname": "status.example.net",
                "upstream": "status:8787",
                "health_upstream": "status:8787",
            }],
        },
        "network": {"name": "swarmsail-private", "driver": "bridge"},
    }


class CrowdSecCaddyTests(unittest.TestCase):
    def test_compose_connects_caddy_and_crowdsec_without_exposing_the_key(self):
        config = sample_config()
        compose = generate_compose(config)

        caddy = compose["services"]["caddy"]
        crowdsec = compose["services"]["crowdsec"]
        self.assertIn("/var/lib/swarmsail/caddy/logs:/var/log/caddy", caddy["volumes"])
        self.assertEqual(caddy["secrets"], ["crowdsec-bouncer-api-key"])
        self.assertEqual(
            compose["secrets"]["crowdsec-bouncer-api-key"]["file"],
            config["crowdsec"]["bouncer_api_key_path"],
        )
        self.assertIn("COLLECTIONS=crowdsecurity/caddy", crowdsec["environment"])
        self.assertIn(
            "/var/lib/swarmsail/caddy/logs:/var/log/caddy:ro", crowdsec["volumes"]
        )
        self.assertEqual(crowdsec["ports"], ["127.0.0.1:7422:8080"])
        self.assertNotIn(config["crowdsec"]["bouncer_api_key_path"], str(caddy))

    def test_caddyfile_protects_and_logs_each_managed_host(self):
        caddyfile = generate_caddyfile(sample_config())

        self.assertIn("api_url http://crowdsec:8080", caddyfile)
        self.assertIn("log {\n        level ERROR\n    }", caddyfile)
        self.assertIn("api_key {$CROWDSEC_API_KEY}", caddyfile)
        self.assertIn("route {\n        crowdsec\n        reverse_proxy headscale-1:8080", caddyfile)
        self.assertIn("output file /var/log/caddy/access.log", caddyfile)
        self.assertIn(
            "handle @swarmsail_health {\n"
            "            reverse_proxy status:8787",
            caddyfile,
        )
        self.assertNotIn("caddy-bouncer-key", caddyfile)

    def test_generate_writes_caddy_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            config = sample_config()
            config["crowdsec"]["config_dir"] = f"{directory}/crowdsec"
            ensure_crowdsec_config(config, Path(directory))

            acquisition = Path(directory) / "crowdsec/acquis.d/swarmsail-caddy.yaml"
            self.assertEqual(
                acquisition.read_text(),
                "filenames:\n"
                "  - /var/log/caddy/*.log\n"
                "labels:\n"
                "  type: caddy\n",
            )

    def test_generate_precreates_caddy_access_log(self):
        with tempfile.TemporaryDirectory() as directory:
            config = sample_config()
            config["caddy"]["config_dir"] = f"{directory}/caddy/config"
            config["caddy"]["log_dir"] = f"{directory}/caddy/logs"
            ensure_caddy_config(config, Path(directory))

            access_log = Path(directory) / "caddy/logs/access.log"
            self.assertTrue(access_log.is_file())
            self.assertEqual(access_log.stat().st_mode & 0o777, 0o600)

    def test_external_lapi_requires_an_origin(self):
        config = sample_config()
        config["crowdsec"]["enabled"] = False
        config["crowdsec"]["externally_managed"] = True
        config["crowdsec"]["api_url"] = "not-an-origin"

        with self.assertRaisesRegex(ValueError, "crowdsec.api_url"):
            generate_compose(config)

    def test_external_lapi_url_cannot_inject_caddyfile_directives(self):
        config = sample_config()
        config["crowdsec"]["api_url"] = "http://lapi {"

        with self.assertRaisesRegex(ValueError, "crowdsec.api_url"):
            generate_compose(config)


if __name__ == "__main__":
    unittest.main()
