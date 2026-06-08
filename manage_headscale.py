#!/usr/bin/env python3
"""
Swarmsail Manager: Generate docker-compose.yml from instances.json
Usage: python manage_headscale.py generate
"""

import json
import sys
from pathlib import Path


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


def generate_headscale_service(instance_id: int, config: dict) -> dict:
    """Generate a Headscale service definition."""
    service = {
        "image": "headscale/headscale:latest",
        "restart": "unless-stopped",
        "container_name": f"headscale-{instance_id}",
        "volumes": [
            f"./headscale/{instance_id}/config:/etc/headscale",
            f"./headscale/{instance_id}/data:/var/lib/headscale"
        ],
        "ports": [
            f"{config['web_port']}:8080"  # Web interface
        ],
        "command": "serve",
        "networks": ["npm-net"]
    }
    
    # Add STUN port if configured
    if config.get("stun_port_udp"):
        service["ports"].append(f"{config['stun_port']}:3478/udp")
    
    return service


def generate_npm_service(npm_config: dict) -> dict:
    """Generate NPM Plus service definition."""
    return {
        "container_name": npm_config["container_name"],
        "image": npm_config["image"],
        "restart": "unless-stopped",
        "networks": ["npm-net"],
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


def generate_crowdsec_service(crowdsec_config: dict) -> dict:
    """Generate CrowdSec service definition."""
    return {
        "container_name": "crowdsec",
        "image": "docker.io/crowdsecurity/crowdsec:latest",
        "restart": "unless-stopped",
        "networks": ["npm-net"],
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


def generate_compose(config: dict) -> dict:
    """Generate complete docker-compose configuration."""
    services = {}
    
    # Add Headscale instances
    for instance in config["headscale"]["instances"]:
        service_name = f"headscale-{instance['id']}"
        services[service_name] = generate_headscale_service(instance["id"], instance)
    
    # Add NPM Plus if enabled
    if config["npm"].get("enabled"):
        services["npmplus"] = generate_npm_service(config["npm"])
    
    # Add CrowdSec if enabled
    if config["crowdsec"].get("enabled"):
        services["crowdsec"] = generate_crowdsec_service(config["crowdsec"])
    
    # Create networks section
    networks = {
        config["network"]["name"]: {
            "driver": config["network"]["driver"]
        }
    }
    
    return {
        "version": "3.8",
        "services": services,
        "networks": networks
    }


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


def generate_command(config_file: str = "instances.json"):
    """Main generate command."""
    print("Loading configuration...")
    config = load_config(config_file)
    
    num_instances = count_instances(config)
    print(f"Generating docker-compose.yml with {num_instances} Headscale instance(s)...")
    
    compose = generate_compose(config)
    save_compose(compose)
    
    print(f"\n✓ Done! Configuration:")
    print(f"  - Headscale instances: {num_instances}")
    print(f"  - NPM Plus: {'enabled' if config['npm'].get('enabled') else 'disabled'}")
    print(f"  - CrowdSec: {'enabled' if config['crowdsec'].get('enabled') else 'disabled'}")
    print(f"\nNext steps:")
    print(f"  1. Edit instances.json to customize your instances")
    print(f"  2. Run: docker-compose up -d")


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python manage_headscale.py <command>")
        print("\nCommands:")
        print("  generate   Generate docker-compose.yml from instances.yaml")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "generate":
        generate_command()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
