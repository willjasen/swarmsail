"""Droplet verification must match provider metadata and direct DNS."""

import socket
import unittest

from scripts.verify_droplet_ipv4 import verify_droplet_ipv4
from scripts.tests.test_manager_caddy import sample_config


PUBLIC_IPV4 = "8.8.8.8"


def droplet_config():
    config = sample_config()
    config["deployment"] = {"role": "droplet", "public_ipv4": PUBLIC_IPV4}
    config["caddy"]["listen_ip"] = PUBLIC_IPV4
    return config


def resolved(*addresses):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ]


class VerifyDropletIpv4Tests(unittest.TestCase):
    def test_matches_provider_address_and_direct_dns(self):
        result = verify_droplet_ipv4(
            droplet_config(),
            metadata_lookup=lambda: PUBLIC_IPV4,
            resolver=lambda *_args, **_kwargs: resolved(PUBLIC_IPV4),
        )
        self.assertEqual(result["public_ipv4"], PUBLIC_IPV4)
        self.assertEqual(result["hostnames"], ["headscale.example.test"])

    def test_rejects_address_not_assigned_to_droplet(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_droplet_ipv4(
                droplet_config(),
                metadata_lookup=lambda: "1.1.1.1",
                resolver=lambda *_args, **_kwargs: resolved(PUBLIC_IPV4),
            )

    def test_rejects_proxied_or_mismatched_dns(self):
        with self.assertRaisesRegex(ValueError, "resolve directly"):
            verify_droplet_ipv4(
                droplet_config(),
                metadata_lookup=lambda: PUBLIC_IPV4,
                resolver=lambda *_args, **_kwargs: resolved("203.0.113.20"),
            )

    def test_rejects_missing_dns(self):
        with self.assertRaisesRegex(ValueError, "resolve directly"):
            verify_droplet_ipv4(
                droplet_config(),
                metadata_lookup=lambda: PUBLIC_IPV4,
                resolver=lambda *_args, **_kwargs: [],
            )

    def test_rejects_non_droplet_config(self):
        config = droplet_config()
        config.pop("deployment")
        with self.assertRaisesRegex(ValueError, "deployment.role must be droplet"):
            verify_droplet_ipv4(
                config,
                metadata_lookup=lambda: PUBLIC_IPV4,
                resolver=lambda *_args, **_kwargs: resolved(PUBLIC_IPV4),
            )

    def test_requires_caddy_ingress(self):
        config = droplet_config()
        config["caddy"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "requires enabled Caddy"):
            verify_droplet_ipv4(
                config,
                metadata_lookup=lambda: PUBLIC_IPV4,
                resolver=lambda *_args, **_kwargs: resolved(PUBLIC_IPV4),
            )


if __name__ == "__main__":
    unittest.main()
