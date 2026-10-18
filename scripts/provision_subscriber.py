#!/usr/bin/env python3
"""Plan or reserve one managed Headscale instance for an eligible Rawrarr account."""

import argparse
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from manage_headscale import TAILSCALE_POOL, validate_config
from swarmsail_web import Store


def canonical_account_id(value):
    try:
        result = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("account ID must be a UUID") from exc
    if result != value:
        raise ValueError("account ID must be a canonical UUID")
    return result


def active_eligibility(kind, eligible_until, now=None):
    if kind not in ("trial", "subscription"):
        raise ValueError("eligibility must be trial or subscription")
    try:
        expires = datetime.fromisoformat(eligible_until.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("eligible-until must be an ISO 8601 timestamp") from exc
    if expires.tzinfo is None:
        raise ValueError("eligible-until must include a UTC offset")
    if expires <= (now or datetime.now(timezone.utc)):
        raise ValueError("trial or subscription must still be active")


def https_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("server URL must be a bare HTTPS origin")
    return value.rstrip("/")


def dns_name(value):
    if (not isinstance(value, str) or len(value) > 253
            or not re.fullmatch(r"[a-z0-9]+(?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9]+(?:[-a-z0-9]*[a-z0-9])?)+", value)
            or any(len(label) > 63 for label in value.split("."))):
        raise ValueError("base domain must be a valid lower-case DNS name")
    return value


def next_prefix(instances, account_id):
    used = [ipaddress.ip_network(item["ipv4_prefix"], strict=True)
            for item in instances if item.get("ipv4_prefix")]
    # The opaque account UUID gives a stable random-looking plan across preview
    # and apply. Probe only if a prior account or adopted range owns its slot.
    prefixes = list(TAILSCALE_POOL.subnets(new_prefix=26))
    digest = hashlib.sha256(uuid.UUID(account_id).bytes + b"swarmsail-tailnet-v1").digest()
    start = int.from_bytes(digest[:8], "big") % len(prefixes)
    for offset in range(len(prefixes)):
        prefix = prefixes[(start + offset) % len(prefixes)]
        if not any(prefix.overlaps(existing) for existing in used):
            return str(prefix)
    raise ValueError("no free /26 prefix remains")


def active_tailnet(database, account_id):
    path = Path(database).resolve()
    if not path.exists():
        raise ValueError("account has no existing tailnet")
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT st.*, o.mode FROM subscriber_tailnets st
                   JOIN organizations o ON o.id=st.organization_id
                   WHERE st.account_id=? AND st.active=1""",
                (account_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("could not read existing subscriber tailnet") from exc
    if row is None:
        raise ValueError("account has no active tailnet")
    return dict(row)


def plan(config, account_id, kind, eligible_until, server_url, base_domain,
         data_root, new_private=False, current=None):
    account_id = canonical_account_id(account_id)
    active_eligibility(kind, eligible_until)
    server_url = https_origin(server_url)
    base_domain = dns_name(base_domain)
    data_root = Path(data_root)
    if not data_root.is_absolute():
        raise ValueError("data root must be an absolute path")
    original = copy.deepcopy(config)
    validate_config(original)
    instances = original["headscale"]["instances"]
    matching = [item for item in instances
                if item.get("subscriber_account_id") == account_id]
    if matching:
        latest = max(matching, key=lambda item: item.get("subscriber_generation", 1))
        if not new_private:
            if (latest.get("server_url", "").rstrip("/") != server_url
                    or latest.get("base_domain") != base_domain):
                raise ValueError("account is already reserved with different network settings")
            return original, latest, False
        if (current and current["generation"] > 1
                and current["headscale_instance"] == str(latest["id"])
                and current["mode"] == "isolated"):
            if (latest.get("server_url", "").rstrip("/") != server_url
                    or latest.get("base_domain") != base_domain):
                raise ValueError("new private tailnet is already reserved with different settings")
            return original, latest, False
        if (not current or current["mode"] != "collaborative"
                or current["headscale_instance"] not in {str(item["id"]) for item in matching}):
            raise ValueError("a new private tailnet requires an active shared predecessor")
        next_generation = current["generation"] + 1
        if latest.get("subscriber_generation", 1) == next_generation:
            if (latest.get("server_url", "").rstrip("/") != server_url
                    or latest.get("base_domain") != base_domain):
                raise ValueError("new private tailnet is already reserved with different settings")
            return original, latest, False
        if latest.get("subscriber_generation", 1) != current["generation"]:
            raise ValueError("subscriber tailnet generations are inconsistent")
    elif new_private:
        raise ValueError("account has no tailnet to replace")
    if any(item.get("server_url", "").rstrip("/") == server_url for item in instances):
        raise ValueError("server URL is already assigned")
    if any(item.get("base_domain") == base_domain for item in instances):
        raise ValueError("base domain is already assigned")
    instance_id = max((item["id"] for item in instances), default=0) + 1
    used_ports = {item["web_port"] for item in instances}
    used_ports.update(item.get("stun_port") for item in instances if item.get("stun_port_udp"))
    web_port = next((port for port in range(8081, 65536) if port not in used_ports), None)
    if web_port is None:
        raise ValueError("no available configured port remains")
    instance = {
        "id": instance_id,
        "slug": f"rawrarr-{instance_id}",
        "display_name": f"Rawrarr tailnet {instance_id}",
        "subscriber_account_id": account_id,
        "subscriber_generation": current["generation"] + 1 if new_private else 1,
        "organization_mode": "isolated",
        "management_mode": "managed",
        "service_name": f"headscale-rawrarr-{instance_id}",
        "server_url": server_url,
        "base_domain": base_domain,
        "ipv4_prefix": next_prefix(instances, account_id),
        "web_port": web_port,
        "publish_web_port": False,
        "stun_port_udp": False,
        "config_path": str(data_root / str(instance_id) / "config"),
        "data_path": str(data_root / str(instance_id) / "data"),
    }
    instances.append(instance)
    validate_config(original)
    return original, instance, True


def write_config_atomically(path, config):
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as file:
            temporary = Path(file.name)
            json.dump(config, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def run(args):
    config_path = Path(args.config).resolve()
    # The account service must verify eligibility first. This local CLI accepts
    # only an operator-supplied assertion and is never a public signup endpoint.
    def load():
        return json.loads(config_path.read_text(encoding="utf-8"))

    if not args.apply:
        current = active_tailnet(args.database, args.account_id) if args.new_private else None
        _, instance, created = plan(load(), args.account_id, args.eligibility,
                                    args.eligible_until, args.server_url,
                                    args.base_domain, args.data_root,
                                    new_private=args.new_private, current=current)
        return {"action": "reserve" if created else "already_reserved",
                "instance_id": instance["id"], "ipv4_prefix": instance["ipv4_prefix"],
                "applied": False}

    lock_path = config_path.with_name(f".{config_path.name}.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = active_tailnet(args.database, args.account_id) if args.new_private else None
        updated, instance, created = plan(load(), args.account_id, args.eligibility,
                                           args.eligible_until, args.server_url,
                                           args.base_domain, args.data_root,
                                           new_private=args.new_private, current=current)
        if created:
            write_config_atomically(config_path, updated)
        Store(args.database).link_subscriber_tailnet(
            args.account_id, instance["id"], updated["headscale"]["instances"]
        )
        return {"action": "reserved" if created else "already_reserved",
                "instance_id": instance["id"], "ipv4_prefix": instance["ipv4_prefix"],
                "applied": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--eligibility", choices=("trial", "subscription"), required=True)
    parser.add_argument("--eligible-until", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--base-domain", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--new-private", action="store_true",
                        help="reserve a new private generation after the active tailnet became shared")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"provisioning failed: {exc}\n")


if __name__ == "__main__":
    main()
