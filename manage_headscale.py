#!/usr/bin/env python3
"""
Swarmsail Manager: Generate docker-compose.yml from instances.json
Usage: python manage_headscale.py generate [--apply]
"""

import argparse
import copy
import ipaddress
import json
import os
import re
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_HEADSCALE_IMAGE = "headscale/headscale:0.27.0"
TAILSCALE_POOL = ipaddress.ip_network("100.64.0.0/10")


def load_config(config_file: str = "instances.json") -> dict:
    """Load instances configuration from JSON file."""
    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: {config_file} not found")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        sys.exit(1)


def generate_headscale_service(instance_id: int, config: dict, network_name: str) -> dict:
    """Generate a Headscale service definition."""
    service = {
        "image": config.get("image_digest", config.get("image", DEFAULT_HEADSCALE_IMAGE)),
        "restart": "unless-stopped",
        "container_name": config.get("service_name", f"headscale-{instance_id}"),
        "volumes": [
            f"{config.get('config_path', f'./headscale/{instance_id}/config')}:/etc/headscale",
            f"{config.get('data_path', f'./headscale/{instance_id}/data')}:/var/lib/headscale"
        ],
        "command": "serve",
        "networks": [network_name]
    }
    ports = []
    if config.get("publish_web_port", False):
        ports.append(f"{config['web_port']}:8080")

    # Add STUN port if configured
    if config.get("stun_port_udp"):
        ports.append(f"{config['stun_port']}:3478/udp")
    if ports:
        service["ports"] = ports

    return service


def generate_npm_service(npm_config: dict, network_name: str) -> dict:
    """Generate NPM Plus service definition."""
    return {
        "container_name": npm_config["container_name"],
        "image": npm_config["image"],
        "restart": "unless-stopped",
        "networks": [network_name],
        "ports": [
            f"{npm_config['http_port']}:80",
            f"{npm_config['http_port']}:80/udp",
            f"{npm_config['admin_ui_port']}:81",
            f"{npm_config['https_port']}:443",
            f"{npm_config['https_port']}:443/udp"
        ],
        "volumes": [
            "./npm/data:/data"
        ],
        "environment": [
            f"TZ={npm_config['timezone']}",
            f"ACME_EMAIL={npm_config['acme_email']}",
            "LOGROTATE=true"
        ]
    }


def generate_crowdsec_service(crowdsec_config: dict, network_name: str) -> dict:
    """Generate CrowdSec service definition."""
    return {
        "container_name": "crowdsec",
        "image": "docker.io/crowdsecurity/crowdsec:latest",
        "restart": "unless-stopped",
        "networks": [network_name],
        "ports": ["127.0.0.1:7422:7422"],
        "environment": [
            f"TZ={crowdsec_config['timezone']}",
            "COLLECTIONS=ZoeyVid/npmplus"
        ],
        "volumes": [
            "./crowdsec/conf:/etc/crowdsec",
            "./crowdsec/data:/var/lib/crowdsec/data",
            "./npm/data/nginx:/opt/npmplus/nginx:ro"
        ]
    }


def generate_caddyfile(config: dict) -> str:
    """Generate host-based HTTPS routes for managed Headscale instances."""
    routes = []
    for instance in config["headscale"]["instances"]:
        if is_adopted(instance):
            continue
        parsed = urlsplit(instance["server_url"])
        upstream = instance.get("service_name", f"headscale-{instance['id']}")
        routes.extend((
            f"{parsed.hostname.lower()} {{",
            f"    reverse_proxy {upstream}:8080",
            "}",
            "",
        ))
    return (
        "{\n    admin off\n    acme_dns cloudflare {file./run/secrets/cloudflare-dns-token}\n"
        "    tls_resolvers 8.8.8.8 9.9.9.9\n}\n\n" + "\n".join(routes)
    )


def generate_caddy_service(caddy_config: dict, network_name: str) -> dict:
    """Generate the Swarmsail-owned Caddy DNS-01 ingress service."""
    config_dir = Path(caddy_config["config_dir"])
    return {
        "image": caddy_config.get("image", "swarmsail-caddy:local"),
        "build": {
            "context": str(Path(__file__).resolve().parent),
            "dockerfile": "Dockerfile.caddy",
        },
        "container_name": caddy_config.get("container_name", "swarmsail-caddy"),
        "restart": "unless-stopped",
        "ports": [
            f"{caddy_config['listen_ip']}:{caddy_config.get('http_port', 80)}:80",
            f"{caddy_config['listen_ip']}:{caddy_config.get('https_port', 443)}:443",
        ],
        "volumes": [
            f"{config_dir / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
            f"{caddy_config['data_dir']}:/data",
            f"{caddy_config['state_dir']}:/config",
            f"{caddy_config['cloudflare_token_path']}:/run/secrets/cloudflare-dns-token:ro",
        ],
        "read_only": True,
        "tmpfs": ["/tmp"],
        "cap_drop": ["ALL"],
        "cap_add": ["NET_BIND_SERVICE"],
        "security_opt": ["no-new-privileges:true"],
        "networks": [network_name],
    }


def generate_compose(config: dict) -> dict:
    """Generate services owned by Swarmsail, never adopted live services."""
    validate_config(config)
    services = {}
    network_name = config["network"]["name"]
    instances = config["headscale"]["instances"]
    adoption_mode = any(is_adopted(instance) for instance in instances)

    # Add Headscale instances
    for instance in instances:
        if is_adopted(instance):
            continue
        service_name = instance.get("service_name", f"headscale-{instance['id']}")
        services[service_name] = generate_headscale_service(
            instance["id"], instance, network_name
        )

    caddy_config = config.get("caddy", {})
    if caddy_config.get("enabled"):
        services[caddy_config.get("service_name", "caddy")] = generate_caddy_service(
            caddy_config, network_name
        )

    # Add NPM Plus if enabled
    if not adoption_mode and config.get("npm", {}).get("enabled"):
        services["npmplus"] = generate_npm_service(config["npm"], network_name)

    # Add CrowdSec if enabled
    if not adoption_mode and config.get("crowdsec", {}).get("enabled"):
        services["crowdsec"] = generate_crowdsec_service(config["crowdsec"], network_name)

    # Create networks section
    if adoption_mode:
        networks = {network_name: {"external": True, "name": network_name}}
    else:
        networks = {network_name: {"driver": config["network"]["driver"]}}

    return {"services": services, "networks": networks}


def is_adopted(instance: dict) -> bool:
    """Return whether an instance is inventory-only and externally managed."""
    return instance.get("management_mode", "managed") == "adopted"


def valid_dns_hostname(hostname: str) -> bool:
    labels = hostname.rstrip(".").split(".") if isinstance(hostname, str) else []
    return len(labels) >= 2 and all(
        len(label) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    )


def validate_config(config: dict):
    """Reject settings that would produce an invalid Compose project."""
    try:
        instances = config["headscale"]["instances"]
        network = config["network"]
        npm = config.get("npm", {})
        crowdsec = config.get("crowdsec", {})
    except (KeyError, TypeError) as exc:
        raise ValueError(f"missing configuration field: {exc}") from exc

    if not isinstance(instances, list):
        raise ValueError("headscale.instances must be a list")
    if not network.get("name") or not network.get("driver"):
        raise ValueError("network.name and network.driver are required")
    if crowdsec.get("required_for_public_hosts"):
        if not npm.get("enabled"):
            raise ValueError("CrowdSec-protected public hosts require NPM Plus")
        if not (crowdsec.get("enabled") or crowdsec.get("externally_managed")):
            raise ValueError(
                "CrowdSec-protected public hosts require managed or external CrowdSec"
            )

    caddy = config.get("caddy", {})
    if caddy.get("enabled"):
        if npm.get("enabled"):
            raise ValueError("Caddy and NPM Plus cannot both own public HTTP/HTTPS ports")
        for field in ("listen_ip", "config_dir", "data_dir", "state_dir", "cloudflare_token_path"):
            if not isinstance(caddy.get(field), str) or not caddy[field].strip():
                raise ValueError(f"enabled Caddy requires caddy.{field}")
        try:
            ipaddress.ip_address(caddy["listen_ip"])
        except ValueError as exc:
            raise ValueError("caddy.listen_ip must be an IP address") from exc
        for field, default in (("http_port", 80), ("https_port", 443)):
            port = caddy.get(field, default)
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ValueError(f"caddy.{field} must be an integer from 1 to 65535")
        if not any(not is_adopted(instance) for instance in instances):
            raise ValueError("Caddy requires at least one managed Headscale instance")

    seen_ids = set()
    seen_ports = set()
    seen_prefixes = []
    seen_subscriber_generations = set()
    seen_proxy_hosts = set()
    subscriber_generations = {}
    for index, instance in enumerate(instances, start=1):
        if not isinstance(instance, dict):
            raise ValueError(f"instance #{index} must be an object")
        management_mode = instance.get("management_mode", "managed")
        if management_mode not in ("managed", "adopted"):
            raise ValueError(
                f"instance #{index} management_mode must be managed or adopted"
            )
        if (
            crowdsec.get("required_for_public_hosts")
            and management_mode == "managed"
            and instance.get("publish_web_port", False)
        ):
            raise ValueError(
                f"instance {instance.get('id', index)} cannot publish its web port "
                "when CrowdSec ingress is required"
            )
        if caddy.get("enabled") and management_mode == "managed" and instance.get("publish_web_port", False):
            raise ValueError(
                f"instance {instance.get('id', index)} must keep its web port private when Caddy is enabled"
            )
        for field in ("id", "web_port"):
            value = instance.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
                raise ValueError(
                    f"instance #{index} field {field} must be an integer from 1 to 65535"
                )
        if instance["id"] in seen_ids:
            raise ValueError(f"duplicate instance id {instance['id']}")
        seen_ids.add(instance["id"])
        if caddy.get("enabled") and management_mode == "managed":
            server_url = instance.get("server_url", "")
            parsed = urlsplit(server_url)
            try:
                valid_origin = (
                    parsed.scheme == "https" and parsed.hostname and not parsed.username
                    and not parsed.password and parsed.path in ("", "/")
                    and not parsed.query and not parsed.fragment and parsed.port in (None, 443)
                )
            except ValueError:
                valid_origin = False
            if not valid_origin:
                raise ValueError(
                    f"instance {instance['id']} requires a bare HTTPS origin on port 443 for Caddy"
                )
            if not valid_dns_hostname(parsed.hostname):
                raise ValueError(f"instance {instance['id']} has an invalid Caddy hostname")
            hostname = parsed.hostname.lower()
            if hostname in seen_proxy_hosts:
                raise ValueError(f"duplicate Caddy hostname {parsed.hostname}")
            seen_proxy_hosts.add(hostname)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", instance.get("service_name", f"headscale-{instance['id']}")):
                raise ValueError(f"instance {instance['id']} has an invalid service_name for Caddy")
        subscriber_id = instance.get("subscriber_account_id")
        if subscriber_id is not None:
            try:
                normalized_id = str(uuid.UUID(str(subscriber_id)))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"instance {instance['id']} has an invalid subscriber_account_id") from exc
            if str(subscriber_id) != normalized_id or is_adopted(instance):
                raise ValueError(f"instance {instance['id']} requires a canonical managed subscriber_account_id")
            generation = instance.get("subscriber_generation", 1)
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                raise ValueError(f"instance {instance['id']} has an invalid subscriber_generation")
            pair = (normalized_id, generation)
            if pair in seen_subscriber_generations:
                raise ValueError(f"subscriber account {normalized_id} has a duplicate generation")
            seen_subscriber_generations.add(pair)
            subscriber_generations.setdefault(normalized_id, set()).add(generation)

        ports = [instance["web_port"]]
        if instance.get("stun_port_udp"):
            stun_port = instance.get("stun_port")
            if not isinstance(stun_port, int) or isinstance(stun_port, bool) or not 1 <= stun_port <= 65535:
                raise ValueError(
                    f"instance {instance['id']} requires a valid stun_port"
                )
            ports.append(stun_port)
        for port in ports:
            if port in seen_ports:
                raise ValueError(f"duplicate host port {port}")
            seen_ports.add(port)

        prefix_value = instance.get("ipv4_prefix")
        if not prefix_value:
            raise ValueError(f"instance {instance['id']} is missing ipv4_prefix")
        try:
            prefix = ipaddress.ip_network(prefix_value, strict=True)
        except ValueError as exc:
            raise ValueError(f"instance {instance['id']} has an invalid ipv4_prefix") from exc
        inside_pool = (
            prefix.network_address >= TAILSCALE_POOL.network_address
            and prefix.broadcast_address <= TAILSCALE_POOL.broadcast_address
        )
        valid_prefix_length = (
            prefix.prefixlen >= 10 if is_adopted(instance) else prefix.prefixlen == 26
        )
        if prefix.version != 4 or not valid_prefix_length or not inside_pool:
            expected = "an existing subnet" if is_adopted(instance) else "a /26"
            raise ValueError(
                f"instance {instance['id']} ipv4_prefix must be {expected} within {TAILSCALE_POOL}"
            )
        if any(prefix.overlaps(existing) for existing in seen_prefixes):
            raise ValueError(f"overlapping ipv4_prefix {prefix}")
        seen_prefixes.append(prefix)
    for account_id, generations in subscriber_generations.items():
        if generations != set(range(1, max(generations) + 1)):
            raise ValueError(f"subscriber account {account_id} has a missing generation")


def assign_missing_prefixes(config: dict) -> bool:
    """Assign and persist random, non-overlapping /26s for new tailnets."""
    instances = config.get("headscale", {}).get("instances", [])
    used = []
    for instance in instances:
        value = instance.get("ipv4_prefix")
        if value:
            used.append(ipaddress.ip_network(value, strict=True))
    available = [
        str(network)
        for network in TAILSCALE_POOL.subnets(new_prefix=26)
        if not any(network.overlaps(existing) for existing in used)
    ]
    changed = False
    for instance in instances:
        if is_adopted(instance):
            continue
        if instance.get("ipv4_prefix"):
            continue
        if not available:
            raise ValueError("no /26 networks remain in the Tailscale address pool")
        position = secrets.randbelow(len(available))
        instance["ipv4_prefix"] = available.pop(position)
        changed = True
    return changed


def dict_to_yaml(data, indent=0):
    """Simple YAML formatter without external libraries."""
    lines = []
    indent_str = "  " * indent

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append(f"{indent_str}{key}:")
                lines.append(dict_to_yaml(value, indent + 1))
            elif isinstance(value, list):
                lines.append(f"{indent_str}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        lines.append(f"{indent_str}  -")
                        for k, v in item.items():
                            if isinstance(v, dict) or isinstance(v, list):
                                lines.append(f"{indent_str}    {k}:")
                                lines.append(dict_to_yaml(v, indent + 3).lstrip())
                            else:
                                v_str = json.dumps(v) if isinstance(v, (bool, type(None))) else v
                                lines.append(f"{indent_str}    {k}: {v_str}")
                    else:
                        item_str = json.dumps(item) if isinstance(item, (bool, type(None))) else item
                        lines.append(f"{indent_str}  - {item_str}")
            else:
                # Quote numeric values and strings that need quoting
                if key == "version" or isinstance(value, float):
                    v_str = f'"{value}"'
                elif isinstance(value, (bool, type(None))):
                    v_str = json.dumps(value)
                else:
                    v_str = value
                lines.append(f"{indent_str}{key}: {v_str}")

    return "\n".join(lines)


def save_compose(compose_config: dict, output_file: str = "docker-compose.yml"):
    """Save docker-compose configuration to YAML file."""
    yaml_content = dict_to_yaml(compose_config)
    with open(output_file, 'w') as f:
        f.write(yaml_content + "\n")
    print(f"✓ Generated {output_file}")


def count_instances(config: dict) -> int:
    """Return number of Headscale instances."""
    return len(config["headscale"]["instances"])


def ensure_headscale_configs(config: dict, project_dir: Path):
    """Create missing instance configs while preserving operator-managed files."""
    template_file = Path(__file__).with_name("headscale-config.template.yaml")
    template = template_file.read_text(encoding="utf-8")
    for instance in config["headscale"]["instances"]:
        if is_adopted(instance):
            continue
        configured_path = instance.get("config_path")
        if configured_path:
            config_file = Path(configured_path) / "config.yaml"
        else:
            config_file = (
                project_dir / "headscale" / str(instance["id"]) / "config" / "config.yaml"
            )
        if config_file.exists():
            continue
        rendered = template.replace(
            "__SERVER_URL__",
            instance.get("server_url", f"http://localhost:{instance['web_port']}"),
        ).replace(
            "__BASE_DOMAIN__",
            instance.get("base_domain", f"tailnet-{instance['id']}.internal"),
        ).replace(
            "__IPV4_PREFIX__", instance["ipv4_prefix"],
        )
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(rendered, encoding="utf-8")
        print(f"✓ Generated {config_file}")


def ensure_caddy_config(config: dict, project_dir: Path):
    """Write the generated Caddyfile into node-local state when Caddy is enabled."""
    caddy = config.get("caddy", {})
    if not caddy.get("enabled"):
        return
    config_dir = Path(caddy.get("config_dir", project_dir / "caddy"))
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_dir, 0o700)
    caddyfile = config_dir / "Caddyfile"
    content = "# Generated by Swarmsail from instances.json; do not put credentials here.\n" + generate_caddyfile(config)
    temporary = caddyfile.with_name(f".{caddyfile.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, caddyfile)
    print(f"✓ Generated {caddyfile}")


def generate_command(config_file: str = "instances.json", output_file=None):
    """Main generate command."""
    print("Loading configuration...")
    config_path = Path(config_file).resolve()
    config = load_config(str(config_path))
    try:
        if assign_missing_prefixes(config):
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            print(f"✓ Assigned address space in {config_path}")
        validate_config(config)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    num_instances = count_instances(config)
    adopted_count = sum(
        is_adopted(item) for item in config["headscale"]["instances"]
    )
    managed_count = num_instances - adopted_count
    output_name = "docker-compose.managed.yml" if adopted_count else "docker-compose.yml"
    output_path = Path(output_file).resolve() if output_file else config_path.parent / output_name
    print(
        f"Generating {output_name} with {managed_count} managed Headscale instance(s); "
        f"preserving {adopted_count} adopted instance(s)..."
    )

    ensure_headscale_configs(config, config_path.parent)
    ensure_caddy_config(config, config_path.parent)
    compose = generate_compose(config)
    save_compose(compose, str(output_path))

    print(f"\n✓ Done! Configuration:")
    print(f"  - Headscale instances: {num_instances}")
    print(f"  - Adopted (never generated): {adopted_count}")
    print(f"  - NPM Plus: {'enabled' if config['npm'].get('enabled') else 'disabled'}")
    print(f"  - Caddy: {'enabled' if config.get('caddy', {}).get('enabled') else 'disabled'}")
    print(f"  - CrowdSec: {'enabled' if config['crowdsec'].get('enabled') else 'disabled'}")
    print(f"\nNext steps:")
    print(f"  1. Edit instances.json to customize your instances")
    if adopted_count:
        print(f"  2. Review {output_name}; do not replace the adopted Compose project")
    else:
        print(f"  2. Run: docker-compose up -d")
    return 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Plan or generate managed Headscale files")
    parser.add_argument("command", choices=("generate",))
    parser.add_argument("--config", default="instances.json")
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true", help="write config and Compose files")
    args = parser.parse_args()
    if args.apply:
        sys.exit(generate_command(args.config, args.output))
    config = copy.deepcopy(load_config(args.config))
    try:
        assign_missing_prefixes(config)
        validate_config(config)
    except ValueError as exc:
        parser.exit(1, f"invalid configuration: {exc}\n")
    managed = sum(not is_adopted(item) for item in config["headscale"]["instances"])
    print(f"Dry run: {managed} managed Headscale instance(s); no files changed.")


if __name__ == "__main__":
    main()
