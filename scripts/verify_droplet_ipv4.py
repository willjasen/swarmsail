#!/usr/bin/env python3
"""Verify a DigitalOcean droplet's public IPv4 and direct DNS records."""

import argparse
import ipaddress
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from manage_headscale import load_config, validate_config


METADATA_URL = (
    "http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address"
)


def droplet_public_ipv4():
    """Read the public IPv4 assigned to this Droplet from its local metadata service."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(METADATA_URL, timeout=2) as response:
            value = response.read(64).decode("ascii").strip()
    except (OSError, urllib.error.URLError, UnicodeDecodeError) as exc:
        raise ValueError(
            "DigitalOcean public IPv4 metadata is unavailable; run this check on the droplet"
        ) from exc
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("DigitalOcean metadata returned an invalid public IPv4 address") from exc
    if not address.is_global:
        raise ValueError("DigitalOcean metadata did not return a globally routable IPv4 address")
    return str(address)


def configured_hostnames(config):
    """Return the unique hostnames which Caddy is expected to serve."""
    hosts = set()
    for instance in config["headscale"]["instances"]:
        if instance.get("management_mode", "managed") == "managed":
            hostname = urlsplit(instance["server_url"]).hostname
            if hostname:
                hosts.add(hostname.lower())
    for route in config.get("caddy", {}).get("proxy_routes", []):
        hosts.add(route["hostname"].lower())
    return sorted(hosts)


def verify_droplet_ipv4(config, metadata_lookup=droplet_public_ipv4, resolver=socket.getaddrinfo):
    validate_config(config)
    if config.get("deployment", {}).get("role") != "droplet":
        raise ValueError("deployment.role must be droplet")
    if not config.get("caddy", {}).get("enabled"):
        raise ValueError("droplet IPv4 verification requires enabled Caddy ingress")

    expected = config["deployment"]["public_ipv4"]
    actual = metadata_lookup()
    if actual != expected:
        raise ValueError(
            f"configured deployment.public_ipv4 ({expected}) does not match "
            f"the droplet-assigned address ({actual})"
        )

    for hostname in configured_hostnames(config):
        try:
            resolved = {
                result[4][0]
                for result in resolver(hostname, 443, family=socket.AF_INET)
            }
        except OSError as exc:
            raise ValueError(f"could not resolve direct DNS A record for {hostname}") from exc
        if resolved != {expected}:
            raise ValueError(
                f"{hostname} must resolve directly to {expected}; "
                "disable DNS proxying and remove other A records"
            )
    return {"public_ipv4": expected, "hostnames": configured_hostnames(config)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        result = verify_droplet_ipv4(load_config(args.config))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"droplet IPv4 verification failed: {exc}\n")
    print(
        f"Verified DigitalOcean public IPv4 {result['public_ipv4']} "
        f"and {len(result['hostnames'])} direct DNS hostname(s)."
    )


if __name__ == "__main__":
    main()
