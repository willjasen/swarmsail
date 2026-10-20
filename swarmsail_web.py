#!/usr/bin/env python3
"""Local Swarmsail control plane for Crews, devices, and sharing intent."""

import argparse
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlparse

from manage_headscale import generate_command, load_config


def mask_address(value):
    """Return a useful network hint without publishing a visitor's full IP."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "redacted"
    prefix = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def nested_total(value):
    if isinstance(value, dict):
        return sum(nested_total(item) for item in value.values())
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def public_instance(instance):
    """Expose display settings, never operator paths or subscriber account IDs."""
    fields = ("id", "server_url", "ipv4_prefix", "base_domain",
              "management_mode", "security_path", "display_name")
    return {field: instance[field] for field in fields if field in instance}


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 100),
    mode TEXT NOT NULL CHECK(mode IN ('collaborative', 'isolated')),
    headscale_instance TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS organization_no_private_reversion
BEFORE UPDATE OF mode ON organizations
WHEN OLD.mode = 'collaborative' AND NEW.mode = 'isolated'
BEGIN
    SELECT RAISE(ABORT, 'a shared organization cannot become isolated');
END;
CREATE TABLE IF NOT EXISTS subscriber_tailnets (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    organization_id INTEGER NOT NULL UNIQUE REFERENCES organizations(id) ON DELETE RESTRICT,
    headscale_instance TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, generation)
);
CREATE TRIGGER IF NOT EXISTS subscriber_tailnet_identity_immutable
BEFORE UPDATE OF account_id, generation, organization_id, headscale_instance
ON subscriber_tailnets
BEGIN
    SELECT RAISE(ABORT, 'subscriber tailnet identity cannot be changed');
END;
CREATE TRIGGER IF NOT EXISTS subscriber_organization_instance_immutable
BEFORE UPDATE OF headscale_instance ON organizations
WHEN EXISTS(SELECT 1 FROM subscriber_tailnets WHERE organization_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'subscriber organization instance cannot be changed');
END;
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
    headscale_node_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(organization_id, name)
);
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY,
    organization_a_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    organization_b_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'applied', 'error')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(organization_a_id < organization_b_id),
    UNIQUE(organization_a_id, organization_b_id)
);
CREATE TABLE IF NOT EXISTS shares (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    target_organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK(permission IN ('connect', 'manage')),
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'applied', 'error')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(device_id, target_organization_id)
);
CREATE TABLE IF NOT EXISTS acl_rules (
    id INTEGER PRIMARY KEY,
    source_organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    destination_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    protocol TEXT NOT NULL CHECK(protocol IN ('any', 'tcp', 'udp', 'icmp')),
    ports TEXT NOT NULL DEFAULT '*',
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned', 'applied', 'error')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_organization_id, destination_device_id, protocol, ports)
);
"""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript(SCHEMA)
            # Earlier builds allowed only one active tailnet per account.
            # Keep existing rows intact while allowing independent tailnets.
            db.execute("DROP INDEX IF EXISTS one_active_subscriber_tailnet")
            columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
            migrations = {
                "source": "TEXT NOT NULL DEFAULT 'manual'",
                "user_name": "TEXT",
                "ip_addresses": "TEXT NOT NULL DEFAULT '[]'",
                "online": "INTEGER",
                "last_seen": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE devices ADD COLUMN {name} {definition}")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def state(self, instances):
        with self.connect() as db:
            organizations = [dict(row) for row in db.execute(
                "SELECT * FROM organizations ORDER BY name"
            )]
            devices = [dict(row) for row in db.execute(
                """SELECT d.*, o.name AS organization_name, o.mode AS organization_mode
                   FROM devices d JOIN organizations o ON o.id=d.organization_id
                   ORDER BY o.name, d.name"""
            )]
            connections = [dict(row) for row in db.execute(
                """SELECT c.*, a.name AS organization_a_name, b.name AS organization_b_name
                   FROM connections c
                   JOIN organizations a ON a.id=c.organization_a_id
                   JOIN organizations b ON b.id=c.organization_b_id
                   ORDER BY a.name, b.name"""
            )]
            shares = [dict(row) for row in db.execute(
                """SELECT s.*, d.name AS device_name,
                          owner.id AS owner_organization_id, owner.name AS owner_organization_name,
                          target.name AS target_organization_name
                   FROM shares s
                   JOIN devices d ON d.id=s.device_id
                   JOIN organizations owner ON owner.id=d.organization_id
                   JOIN organizations target ON target.id=s.target_organization_id
                   ORDER BY d.name, target.name"""
            )]
            acl_rules = [dict(row) for row in db.execute(
                """SELECT acl.*, source.name AS source_organization_name,
                          device.name AS destination_device_name,
                          destination.id AS destination_organization_id,
                          destination.name AS destination_organization_name
                   FROM acl_rules acl
                   JOIN organizations source ON source.id=acl.source_organization_id
                   JOIN devices device ON device.id=acl.destination_device_id
                   JOIN organizations destination ON destination.id=device.organization_id
                   ORDER BY source.name, destination.name, device.name"""
            )]
        return {
            "organizations": organizations,
            "devices": devices,
            "connections": connections,
            "shares": shares,
            "acl_rules": acl_rules,
            "headscale_instances": [public_instance(item) for item in instances],
            "sharing_note": (
                "Planned shares describe desired policy. Only collaborative Crews "
                "can participate; a future Headscale adapter must apply them."
            ),
        }

    def organization_state(self, organization_id, instances):
        """Return one organization's tailnet-oriented policy view."""
        state = self.state(instances)
        organization = next(
            (item for item in state["organizations"] if item["id"] == organization_id),
            None,
        )
        if organization is None:
            raise InputError("Crew does not exist", HTTPStatus.NOT_FOUND)
        instance_id = organization.get("headscale_instance")
        instance = next(
            (item for item in instances if str(item.get("id")) == str(instance_id)),
            None,
        )
        devices = [
            item for item in state["devices"]
            if item["organization_id"] == organization_id
        ]
        connections = [
            item for item in state["connections"]
            if organization_id in (
                item["organization_a_id"], item["organization_b_id"]
            )
        ]
        shares = [
            item for item in state["shares"]
            if organization_id in (
                item["owner_organization_id"], item["target_organization_id"]
            )
        ]
        acl_rules = [
            item for item in state["acl_rules"]
            if organization_id in (
                item["source_organization_id"],
                item["destination_organization_id"],
            )
        ]
        return {
            "organization": organization,
            "tailnet": public_instance(instance) if instance else None,
            "devices": devices,
            "connections": connections,
            "shares": shares,
            "acl_rules": acl_rules,
            "default_action": "deny",
            "policy_status": (
                "Desired policy only. Planned rules are not active in Headscale "
                "until a policy reconciler applies them."
            ),
        }

    def sync_instances(self, instances):
        """Seed configured tailnets without changing their deployment."""
        with self.connect() as db:
            for instance in instances:
                name = instance.get("display_name") or instance.get("slug") or str(instance["id"])
                mode = instance.get(
                    "organization_mode",
                    "collaborative" if instance.get("management_mode") == "adopted" else "isolated",
                )
                db.execute(
                    """INSERT OR IGNORE INTO organizations(name, mode, headscale_instance)
                       VALUES (?, ?, ?)""",
                    (name, mode, str(instance["id"])),
                )

    def sync_node_inventory(self, inventory):
        """Upsert scrubbed Headscale node metadata into the local device catalog."""
        with self.connect() as db:
            for tailnet in inventory:
                organization = db.execute(
                    "SELECT id FROM organizations WHERE headscale_instance=?",
                    (str(tailnet.get("instance_id")),),
                ).fetchone()
                if not organization:
                    continue
                for node in tailnet.get("nodes", []):
                    node_id = str(node.get("id"))
                    name = node.get("given_name") or node.get("name") or f"node-{node_id}"
                    values = (
                        name, node.get("user_name"),
                        json.dumps(node.get("ip_addresses") or []),
                        int(bool(node.get("online"))), node.get("last_seen"),
                    )
                    existing = db.execute(
                        """SELECT id FROM devices
                           WHERE organization_id=? AND headscale_node_id=?""",
                        (organization["id"], node_id),
                    ).fetchone()
                    if existing:
                        db.execute(
                            """UPDATE devices SET name=?, user_name=?, ip_addresses=?,
                                      online=?, last_seen=?, source='headscale'
                               WHERE id=?""",
                            values + (existing["id"],),
                        )
                        continue
                    candidate = name
                    if db.execute(
                        "SELECT 1 FROM devices WHERE organization_id=? AND name=?",
                        (organization["id"], candidate),
                    ).fetchone():
                        candidate = f"{name} #{node_id}"
                    db.execute(
                        """INSERT INTO devices
                           (organization_id, name, headscale_node_id, source,
                            user_name, ip_addresses, online, last_seen)
                           VALUES (?, ?, ?, 'headscale', ?, ?, ?, ?)""",
                        (organization["id"], candidate, node_id) + values[1:],
                    )

    def create_organization(self, data):
        name = required_text(data, "name")
        mode = data.get("mode", "collaborative")
        if mode not in ("collaborative", "isolated"):
            raise InputError("mode must be collaborative or isolated")
        instance = optional_text(data.get("headscale_instance"))
        if mode == "isolated" and not instance:
            raise InputError("isolated Crews require a Mast")
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO organizations(name, mode, headscale_instance) VALUES (?, ?, ?)",
                (name, mode, instance),
            )
            return self._one(db, "SELECT * FROM organizations WHERE id=?", cursor.lastrowid)

    def enable_sharing(self, organization_id):
        """Permanently mark an existing isolated tailnet as eligible for planned shares."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM organizations WHERE id=?", (organization_id,)
            ).fetchone()
            if row is None:
                raise InputError("organization does not exist", HTTPStatus.NOT_FOUND)
            if row["mode"] == "isolated":
                db.execute(
                    "UPDATE organizations SET mode='collaborative' WHERE id=?",
                    (organization_id,),
                )
            return self._one(db, "SELECT * FROM organizations WHERE id=?", organization_id)

    def link_subscriber_tailnet(self, account_id, instance_id, instances):
        """Bind one configured isolated tailnet without deactivating its siblings."""
        try:
            account_id = str(uuid.UUID(str(account_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise InputError("account_id must be a UUID") from exc
        instance_id = str(instance_id).strip()
        instance = next(
            (item for item in instances if str(item.get("id")) == instance_id), None
        )
        if not instance or instance.get("management_mode", "managed") != "managed":
            raise InputError("account requires a configured managed Headscale instance")
        if str(instance.get("subscriber_account_id", "")) != account_id:
            raise InputError("Headscale instance is assigned to another account", HTTPStatus.CONFLICT)
        generation = instance.get("subscriber_generation", 1)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise InputError("subscriber generation must be a positive integer")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM subscriber_tailnets WHERE headscale_instance=?", (instance_id,)
            ).fetchone()
            if existing:
                if existing["account_id"] != account_id or existing["generation"] != generation:
                    raise InputError("Headscale instance is already assigned", HTTPStatus.CONFLICT)
                return dict(existing)
            generation_owner = db.execute(
                "SELECT headscale_instance FROM subscriber_tailnets WHERE account_id=? AND generation=?",
                (account_id, generation),
            ).fetchone()
            if generation_owner:
                raise InputError("subscriber generation is already assigned", HTTPStatus.CONFLICT)
            organization = db.execute(
                "SELECT id, mode FROM organizations WHERE headscale_instance=?",
                (instance_id,),
            ).fetchone()
            if organization:
                in_use = db.execute(
                    "SELECT 1 FROM devices WHERE organization_id=?",
                    (organization["id"],),
                ).fetchone()
                if organization["mode"] != "isolated" or in_use:
                    raise InputError("Headscale instance is already in use", HTTPStatus.CONFLICT)
                organization_id = organization["id"]
            else:
                name = f"rawrarr-tailnet-{instance_id}"
                cursor = db.execute(
                    "INSERT INTO organizations(name, mode, headscale_instance) VALUES (?, 'isolated', ?)",
                    (name, instance_id),
                )
                organization_id = cursor.lastrowid
            cursor = db.execute(
                """INSERT INTO subscriber_tailnets
                   (account_id, generation, organization_id, headscale_instance)
                   VALUES (?, ?, ?, ?)""",
                (account_id, generation, organization_id, instance_id),
            )
            return self._one(db, "SELECT * FROM subscriber_tailnets WHERE id=?", cursor.lastrowid)

    def create_device(self, data):
        organization_id = required_id(data, "organization_id")
        name = required_text(data, "name")
        node_id = optional_text(data.get("headscale_node_id"))
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO devices(organization_id, name, headscale_node_id) VALUES (?, ?, ?)",
                (organization_id, name, node_id),
            )
            return self._one(db, "SELECT * FROM devices WHERE id=?", cursor.lastrowid)

    def get_device(self, organization_id, device_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM devices WHERE id=? AND organization_id=?",
                (device_id, organization_id),
            ).fetchone()
            if row is None:
                raise InputError("device does not exist in this Crew", HTTPStatus.NOT_FOUND)
            return dict(row)

    def delete_device(self, organization_id, device_id):
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM devices WHERE id=? AND organization_id=?",
                (device_id, organization_id),
            )
            if cursor.rowcount != 1:
                raise InputError("device does not exist in this Crew", HTTPStatus.NOT_FOUND)

    def delete_headscale_devices(self, organization_id, user_name):
        with self.connect() as db:
            cursor = db.execute(
                """DELETE FROM devices
                   WHERE organization_id=? AND source='headscale' AND user_name=?""",
                (organization_id, user_name),
            )
            return cursor.rowcount

    def create_connection(self, data):
        first = required_id(data, "organization_a_id")
        second = required_id(data, "organization_b_id")
        if first == second:
            raise InputError("a Crew cannot connect to itself")
        first, second = sorted((first, second))
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, mode FROM organizations WHERE id IN (?, ?)", (first, second)
            ).fetchall()
            if len(rows) != 2:
                raise InputError("both Crews must exist")
            if any(row["mode"] == "isolated" for row in rows):
                raise InputError("isolated Crews cannot be connected")
            cursor = db.execute(
                "INSERT INTO connections(organization_a_id, organization_b_id) VALUES (?, ?)",
                (first, second),
            )
            return self._one(db, "SELECT * FROM connections WHERE id=?", cursor.lastrowid)

    def create_share(self, data):
        device_id = required_id(data, "device_id")
        target_id = required_id(data, "target_organization_id")
        permission = data.get("permission", "connect")
        if permission not in ("connect", "manage"):
            raise InputError("permission must be connect or manage")
        with self.connect() as db:
            row = db.execute(
                """SELECT d.organization_id, owner.mode AS owner_mode, target.mode AS target_mode
                   FROM devices d
                   JOIN organizations owner ON owner.id=d.organization_id
                   JOIN organizations target ON target.id=?
                   WHERE d.id=?""",
                (target_id, device_id),
            ).fetchone()
            if not row:
                raise InputError("device or target organization does not exist")
            if row["organization_id"] == target_id:
                raise InputError("a device is already available to its owning organization")
            if row["owner_mode"] == "isolated" or row["target_mode"] == "isolated":
                raise InputError("isolated Crews cannot create or receive device shares")
            pair = sorted((row["organization_id"], target_id))
            connected = db.execute(
                """SELECT 1 FROM connections
                   WHERE organization_a_id=? AND organization_b_id=?""",
                pair,
            ).fetchone()
            if not connected:
                raise InputError("Crews must be connected before devices can be shared")
            cursor = db.execute(
                """INSERT INTO shares(device_id, target_organization_id, permission)
                   VALUES (?, ?, ?)""",
                (device_id, target_id, permission),
            )
            return self._one(db, "SELECT * FROM shares WHERE id=?", cursor.lastrowid)

    def delete_share(self, share_id):
        with self.connect() as db:
            cursor = db.execute("DELETE FROM shares WHERE id=?", (share_id,))
            if cursor.rowcount != 1:
                raise InputError("share does not exist", HTTPStatus.NOT_FOUND)

    def create_acl_rule(self, data):
        source_id = required_id(data, "source_organization_id")
        device_id = required_id(data, "destination_device_id")
        protocol = data.get("protocol", "any")
        if protocol not in ("any", "tcp", "udp", "icmp"):
            raise InputError("protocol must be any, tcp, udp, or icmp")
        ports = str(data.get("ports", "*")).strip()
        if protocol == "icmp":
            ports = "*"
        if not valid_ports(ports):
            raise InputError("ports must be *, a port, a range, or a comma-separated list")
        with self.connect() as db:
            row = db.execute(
                """SELECT d.organization_id AS destination_id,
                          source.mode AS source_mode, destination.mode AS destination_mode
                   FROM devices d
                   JOIN organizations source ON source.id=?
                   JOIN organizations destination ON destination.id=d.organization_id
                   WHERE d.id=?""",
                (source_id, device_id),
            ).fetchone()
            if not row:
                raise InputError("source Crew or destination device does not exist")
            if source_id != row["destination_id"]:
                if row["source_mode"] == "isolated" or row["destination_mode"] == "isolated":
                    raise InputError("isolated Crews cannot have cross-Crew ACLs")
                pair = sorted((source_id, row["destination_id"]))
                if not db.execute(
                    """SELECT 1 FROM connections
                       WHERE organization_a_id=? AND organization_b_id=?""", pair
                ).fetchone():
                    raise InputError("Crews must be connected before adding an ACL")
            cursor = db.execute(
                """INSERT INTO acl_rules
                   (source_organization_id, destination_device_id, protocol, ports)
                   VALUES (?, ?, ?, ?)""",
                (source_id, device_id, protocol, ports),
            )
            return self._one(db, "SELECT * FROM acl_rules WHERE id=?", cursor.lastrowid)

    def delete_acl_rule(self, rule_id):
        with self.connect() as db:
            cursor = db.execute("DELETE FROM acl_rules WHERE id=?", (rule_id,))
            if cursor.rowcount != 1:
                raise InputError("ACL rule does not exist", HTTPStatus.NOT_FOUND)

    @staticmethod
    def _one(db, query, value):
        return dict(db.execute(query, (value,)).fetchone())


class InputError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def required_text(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        raise InputError(f"{key} must be between 1 and 100 characters")
    return value.strip()


def optional_text(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value.strip()) > 200:
        raise InputError("optional text fields must be at most 200 characters")
    return value.strip()


def required_id(data, key):
    value = data.get(key)
    if isinstance(value, bool):
        raise InputError(f"{key} must be an integer")
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{key} must be an integer") from exc
    if value < 1:
        raise InputError(f"{key} must be positive")
    return value


def valid_ports(value):
    if value == "*":
        return True
    if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        return False
    for item in value.split(","):
        bounds = [int(part) for part in item.split("-")]
        if any(port < 1 or port > 65535 for port in bounds):
            return False
        if len(bounds) == 2 and bounds[0] > bounds[1]:
            return False
    return True


class AppServer(ThreadingHTTPServer):
    def __init__(self, address, handler, store, config_path, allow_generation=False,
                 node_inventory=None):
        super().__init__(address, handler)
        self.store = store
        self.config_path = Path(config_path)
        self.allow_generation = allow_generation
        self.node_inventory = Path(node_inventory) if node_inventory else None
        self.inventory_cache = []
        instances = self.instances()
        self.store.sync_instances(instances)
        for instance in instances:
            if instance.get("subscriber_account_id"):
                self.store.link_subscriber_tailnet(
                    instance["subscriber_account_id"], instance["id"], instances
                )
        self.refresh_inventory()

    def instances(self):
        config = load_config(str(self.config_path))
        return config.get("headscale", {}).get("instances", [])

    def refresh_inventory(self):
        if not self.node_inventory or not self.node_inventory.exists():
            return
        try:
            inventory = json.loads(self.node_inventory.read_text())
            if isinstance(inventory, list):
                self.inventory_cache = inventory
                self.store.sync_node_inventory(inventory)
        except (OSError, json.JSONDecodeError):
            return

    def dns_settings(self, instance_id):
        tailnet = next((item for item in self.inventory_cache
                        if str(item.get("instance_id")) == str(instance_id)), {})
        return tailnet.get("dns") if isinstance(tailnet.get("dns"), dict) else None

    def inventory_users(self, instance_id):
        tailnet = next((item for item in self.inventory_cache
                        if str(item.get("instance_id")) == str(instance_id)), {})
        return tailnet.get("users") if isinstance(tailnet.get("users"), list) else []

    def create_registration(self, organization, data):
        instance_id = organization.get("headscale_instance")
        instance = next((item for item in self.instances()
                         if str(item.get("id")) == str(instance_id)), None)
        if not instance:
            raise InputError("Crew has no dedicated Mast")
        try:
            user_id = int(data.get("user_id"))
        except (TypeError, ValueError) as exc:
            raise InputError("select a valid matey") from exc
        users = self.inventory_users(instance_id)
        if user_id not in {item.get("id") for item in users}:
            raise InputError("selected matey does not belong to this Mast")
        helper = os.environ.get(
            "SWARMSAIL_REGISTRATION_HELPER",
            "/opt/swarmsail/current/create_preauthkey.py",
        )
        command = ([] if os.environ.get("SWARMSAIL_CONTAINER") == "true" else ["sudo"]) + [
            helper,
            "--config", str(self.config_path), "--instance", str(instance_id),
            "--user", str(user_id), "--expiration", "1h",
        ]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
            key = json.loads(result.stdout)["key"]
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError) as exc:
            raise InputError("Headscale could not create a registration key", HTTPStatus.BAD_GATEWAY) from exc
        server_url = str(instance.get("server_url") or "").rstrip("/")
        return {
            "server_url": server_url, "auth_key": key, "expires_in": "1 hour",
            "command": f"tailscale login --login-server={server_url} --auth-key={key}",
        }

    def create_headscale_user(self, organization, data):
        instance_id = organization.get("headscale_instance")
        if not any(str(item.get("id")) == str(instance_id) for item in self.instances()):
            raise InputError("Crew has no dedicated Mast")
        name = str(data.get("name") or "").strip()
        helper = os.environ.get(
            "SWARMSAIL_USER_HELPER", "/app/create_headscale_user.py"
        )
        command = ([] if os.environ.get("SWARMSAIL_CONTAINER") == "true" else ["sudo"]) + [
            helper, "--config", str(self.config_path), "--instance", str(instance_id),
            "--name", name,
        ]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
            user = json.loads(result.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            detail = (getattr(exc, "stderr", "") or "").strip()
            message = "Headscale could not create the user"
            if "user name must" in detail:
                message = detail.splitlines()[-1]
            raise InputError(message, HTTPStatus.BAD_GATEWAY) from exc
        tailnet = next((item for item in self.inventory_cache
                        if str(item.get("instance_id")) == str(instance_id)), None)
        if tailnet is not None:
            tailnet.setdefault("users", []).append(user)
            self.write_inventory_cache()
        return user

    def headscale_instance(self, organization):
        instance_id = organization.get("headscale_instance")
        instance = next(
            (item for item in self.instances()
             if str(item.get("id")) == str(instance_id)),
            None,
        )
        if not instance:
            raise InputError("Crew has no dedicated Mast")
        return instance

    def headscale_command(self, organization, *arguments):
        instance = self.headscale_instance(organization)
        instance_id = str(instance["id"])
        service = str(instance.get("service_name", f"headscale-{instance_id}"))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", service):
            raise InputError("configured Mast service name is invalid", HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            return subprocess.run(
                ["docker", "exec", service, "headscale", *arguments],
                check=True, capture_output=True, text=True, timeout=15,
            ).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise InputError("Mast rejected the management request", HTTPStatus.BAD_GATEWAY) from exc

    def headscale_records(self, organization, *arguments):
        try:
            result = json.loads(self.headscale_command(
                organization, *arguments, "--output", "json"
            ))
        except json.JSONDecodeError as exc:
            raise InputError("Mast returned invalid management data", HTTPStatus.BAD_GATEWAY) from exc
        if result is None:
            return []
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise InputError("Mast returned invalid management data", HTTPStatus.BAD_GATEWAY)
        return result

    def update_inventory_after_removal(self, organization, user=None, node_ids=()):
        instance_id = str(organization.get("headscale_instance"))
        for tailnet in self.inventory_cache:
            if str(tailnet.get("instance_id")) != instance_id:
                continue
            if user:
                tailnet["users"] = [
                    item for item in tailnet.get("users", [])
                    if str(item.get("id")) != str(user["id"])
                ]
            removed = {str(node_id) for node_id in node_ids}
            if user:
                removed.update(
                    str(node.get("id")) for node in tailnet.get("nodes", [])
                    if node.get("user_name") == user["name"]
                )
            tailnet["nodes"] = [
                node for node in tailnet.get("nodes", [])
                if str(node.get("id")) not in removed
            ]
            try:
                self.write_inventory_cache()
            except OSError as exc:
                raise InputError(
                    "Mast removal succeeded, but the local inventory could not be refreshed",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                ) from exc
            return

    def delete_headscale_user(self, organization, data):
        if data.get("confirm") is not True:
            raise InputError("confirm the matey removal before continuing")
        user_id = required_id(data, "user_id")
        users = self.headscale_records(organization, "users", "list")
        user = next(
            (item for item in users if str(item.get("id")) == str(user_id)),
            None,
        )
        if user is None or not isinstance(user.get("name"), str) or not user["name"]:
            raise InputError("matey does not exist on this Mast", HTTPStatus.NOT_FOUND)
        nodes = self.headscale_records(organization, "nodes", "list")
        owned_nodes = [
            node for node in nodes
            if isinstance(node.get("user"), dict)
            and str(node["user"].get("id")) == str(user_id)
        ]
        expected_count = data.get("device_count")
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count < 0
            or expected_count != len(owned_nodes)
            or data.get("user_name") != user["name"]
        ):
            raise InputError(
                "matey or device inventory changed; refresh the Crew and confirm again",
                HTTPStatus.CONFLICT,
            )
        self.headscale_command(
            organization, "users", "destroy", "--identifier", str(user_id), "--force"
        )
        self.store.delete_headscale_devices(organization["id"], str(user["name"]))
        self.update_inventory_after_removal(
            organization, user=user,
            node_ids=[node.get("id") for node in owned_nodes],
        )
        return {"deleted": True, "devices_removed": len(owned_nodes)}

    def delete_device(self, organization, data):
        if data.get("confirm") is not True:
            raise InputError("confirm the device removal before continuing")
        device_id = required_id(data, "device_id")
        device = self.store.get_device(organization["id"], device_id)
        if data.get("device_name") != device["name"]:
            raise InputError(
                "device inventory changed; refresh the Crew and confirm again",
                HTTPStatus.CONFLICT,
            )
        node_id = device.get("headscale_node_id")
        if node_id:
            if not str(node_id).isdigit() or int(node_id) < 1:
                raise InputError("linked device has an invalid Mast node ID", HTTPStatus.CONFLICT)
            nodes = self.headscale_records(organization, "nodes", "list")
            node = next(
                (item for item in nodes if str(item.get("id")) == str(node_id)),
                None,
            )
            if node is None:
                raise InputError("linked device is no longer present on this Mast", HTTPStatus.CONFLICT)
            node_name = node.get("given_name") or node.get("name") or f"node-{node_id}"
            valid_names = {node_name, f"{node_name} #{node_id}"}
            if device["name"] not in valid_names:
                raise InputError(
                    "device name changed on the Mast; refresh the Crew and confirm again",
                    HTTPStatus.CONFLICT,
                )
            self.headscale_command(
                organization, "nodes", "delete", "--identifier", str(node_id), "--force"
            )
        self.store.delete_device(organization["id"], device_id)
        if node_id:
            self.update_inventory_after_removal(organization, node_ids=(node_id,))
        return {"deleted": True, "revoked": bool(node_id)}

    def write_inventory_cache(self):
        if not self.node_inventory:
            return
        temporary = self.node_inventory.with_suffix(self.node_inventory.suffix + ".tmp")
        temporary.write_text(json.dumps(self.inventory_cache, indent=2) + "\n")
        os.chmod(temporary, 0o640)
        os.replace(temporary, self.node_inventory)

    def register_pending_node(self, organization, data):
        instance_id = organization.get("headscale_instance")
        try:
            user_id = int(data.get("user_id"))
        except (TypeError, ValueError) as exc:
            raise InputError("select a valid matey") from exc
        if user_id not in {item.get("id") for item in self.inventory_users(instance_id)}:
            raise InputError("selected matey does not belong to this Mast")
        key = str(data.get("key") or "").strip()
        helper = os.environ.get(
            "SWARMSAIL_NODE_REGISTRATION_HELPER", "/app/register_pending_node.py"
        )
        command = ([] if os.environ.get("SWARMSAIL_CONTAINER") == "true" else ["sudo"]) + [
            helper, "--config", str(self.config_path), "--instance", str(instance_id),
            "--user", str(user_id), "--key", key,
        ]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
            registered = json.loads(result.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise InputError(
                "Headscale could not register that pending node key",
                HTTPStatus.BAD_GATEWAY,
            ) from exc
        return registered

    def public_enrollment(self, data=None):
        instance_id = os.environ.get("SWARMSAIL_PUBLIC_ENROLL_INSTANCE", "").strip()
        instance = next((item for item in self.instances()
                         if str(item.get("id")) == instance_id), None)
        if not instance:
            raise InputError("public enrollment is disabled", HTTPStatus.NOT_FOUND)
        if data is None:
            return {"server_url": instance.get("server_url", "")}
        user_name = str(data.get("user_name") or "").strip()
        user = next((item for item in self.inventory_users(instance_id)
                     if item.get("name") == user_name), None)
        if not user:
            raise InputError("owner does not exist in this tailnet")
        organizations = self.store.state(self.instances())["organizations"]
        organization = next((item for item in organizations
                             if str(item.get("headscale_instance")) == instance_id), None)
        if not organization:
            raise InputError("tailnet organization is not configured")
        return self.register_pending_node(organization, {
            "user_id": user["id"], "key": data.get("key"),
        })

    def cloudflare_status(self):
        return {
            "configured": bool(os.environ.get("CLOUDFLARE_API_TOKEN")),
            "zone": os.environ.get("CLOUDFLARE_ZONE", "nerdhirn.tech"),
            "name": os.environ.get(
                "CLOUDFLARE_DNS_NAME", "swarmsail.nerdhirn.tech"
            ),
            "target": os.environ.get("CLOUDFLARE_DNS_TARGET", "147.182.215.245"),
            "proxied": False,
            "activation_ready": os.environ.get(
                "SWARMSAIL_DNS_ACTIVATION_READY", "false"
            ).lower() == "true",
        }

    def security_status(self):
        config = load_config(str(self.config_path))
        crowdsec = config.get("crowdsec", {})
        if config.get("caddy", {}).get("enabled"):
            http_path = "Caddy → CrowdSec bouncer → Headscale"
        else:
            http_path = "NPM Plus → CrowdSec bouncer + AppSec → Headscale"
        return {
            "crowdsec_required": bool(crowdsec.get("required_for_public_hosts")),
            "crowdsec_management": (
                "external" if crowdsec.get("externally_managed") else "swarmsail"
            ),
            "http_path": http_path,
            "stun_note": "STUN uses direct UDP and is not HTTP/AppSec traffic.",
        }

    def crowdsec_data(self):
        container = os.environ.get("SWARMSAIL_CROWDSEC_CONTAINER", "crowdsec")

        def docker(*arguments, timeout=12):
            try:
                result = subprocess.run(
                    ["docker", *arguments], check=True, capture_output=True,
                    text=True, timeout=timeout,
                )
                return result.stdout or result.stderr
            except (subprocess.SubprocessError, OSError) as exc:
                raise InputError(
                    "CrowdSec local data is unavailable", HTTPStatus.BAD_GATEWAY
                ) from exc

        status = docker("inspect", "--format", "{{.State.Status}}|{{.State.StartedAt}}", container).strip()
        running, _, started_at = status.partition("|")
        try:
            metrics = json.loads(docker("exec", container, "cscli", "metrics", "-o", "json"))
            decisions_raw = json.loads(docker(
                "exec", container, "cscli", "decisions", "list", "-o", "json"
            ))
        except json.JSONDecodeError as exc:
            raise InputError("CrowdSec returned invalid local data", HTTPStatus.BAD_GATEWAY) from exc

        acquisitions = []
        for source, values in metrics.get("acquisition", {}).items():
            acquisitions.append({
                "source": source, "read": int(values.get("reads", 0)),
                "parsed": int(values.get("parsed", 0)),
                "unparsed": int(values.get("unparsed", 0)),
                "whitelisted": int(values.get("whitelisted", 0)),
            })
        alerts = sorted(
            ({"scenario": name, "count": int(count)}
             for name, count in metrics.get("alerts", {}).items()),
            key=lambda item: item["count"], reverse=True,
        )[:12]
        appsec = next(iter(metrics.get("appsec-engine", {}).values()), {})
        decisions = []
        for alert in decisions_raw if isinstance(decisions_raw, list) else []:
            for decision in alert.get("decisions", []):
                decisions.append({
                    "scenario": decision.get("scenario", "unknown"),
                    "type": decision.get("type", "unknown"),
                    "origin": decision.get("origin", "unknown"),
                    "duration": decision.get("duration", "unknown"),
                    "scope": decision.get("scope", "unknown"),
                    "target": mask_address(str(decision.get("value", ""))),
                })
                if len(decisions) >= 50:
                    break
            if len(decisions) >= 50:
                break

        logs = docker("logs", "--since", "24h", "--tail", "500", container)
        levels = {"info": 0, "warning": 0, "error": 0}
        recent_issues = []
        for line in logs.splitlines():
            match = re.search(r"level=(info|warning|error)", line)
            if match:
                levels[match.group(1)] += 1
                if match.group(1) != "info":
                    message = re.search(r'msg="(.*)"$', line)
                    text = message.group(1) if message else line
                    text = re.sub(
                        r"(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?![\w:])",
                        lambda found: mask_address(found.group(0)), text,
                    )
                    recent_issues.append({"level": match.group(1), "message": text[-240:]})
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "container": {"name": container, "status": running, "started_at": started_at},
            "acquisition": acquisitions,
            "top_alerts": alerts,
            "appsec": {
                "processed": int(appsec.get("processed", 0)),
                "blocked": int(appsec.get("blocked", 0)),
            },
            "bouncer": {
                "processed": nested_total(metrics.get("bouncers", {}).get("npmplus", {}).get("", {})),
                "dropped": nested_total({
                    key: value for key, value in metrics.get("bouncers", {}).get("npmplus", {}).items()
                    if key != ""
                }),
            },
            "active_decisions": decisions,
            "log_window": {"hours": 24, "sampled_lines": sum(levels.values()), "levels": levels},
            "recent_issues": recent_issues[-20:][::-1],
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "Swarmsail/0.1"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or re.fullmatch(r"/organizations/\d+", path):
            return self.send_html(DASHBOARD)
        if path == "/crowdsec":
            return self.send_html(CROWDSEC_PAGE)
        if path == "/health":
            checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            return self.send_html(HEALTH_PAGE.replace("__CHECKED_AT__", checked_at))
        if path == "/public-enroll":
            try:
                status = self.server.public_enrollment()
                return self.send_html(PUBLIC_ENROLLMENT.replace(
                    "__SERVER_URL__", html_escape(str(status["server_url"]))
                ))
            except InputError as exc:
                return self.send_json({"error": str(exc)}, exc.status)
        if path == "/healthz":
            return self.send_json({"status": "ok"})
        if path == "/api/state":
            self.server.refresh_inventory()
            state = self.server.store.state(self.server.instances())
            state["cloudflare"] = self.server.cloudflare_status()
            state["security"] = self.server.security_status()
            return self.send_json(state)
        if path == "/api/crowdsec":
            try:
                return self.send_json(self.server.crowdsec_data())
            except InputError as exc:
                return self.send_json({"error": str(exc)}, exc.status)
        match = re.fullmatch(r"/api/organizations/(\d+)", path)
        if match:
            try:
                self.server.refresh_inventory()
                result = self.server.store.organization_state(
                    int(match.group(1)), self.server.instances()
                )
                result["dns"] = self.server.dns_settings(
                    result["organization"].get("headscale_instance")
                )
                result["headscale_users"] = self.server.inventory_users(
                    result["organization"].get("headscale_instance")
                )
                return self.send_json(result)
            except InputError as exc:
                return self.send_json({"error": str(exc)}, exc.status)
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            registration = re.fullmatch(r"/api/organizations/(\d+)/registration", path)
            user_creation = re.fullmatch(r"/api/organizations/(\d+)/users", path)
            pending_node = re.fullmatch(r"/api/organizations/(\d+)/pending-node", path)
            if path == "/api/public-enroll":
                result = self.server.public_enrollment(data)
            elif registration:
                detail = self.server.store.organization_state(
                    int(registration.group(1)), self.server.instances()
                )
                result = self.server.create_registration(detail["organization"], data)
            elif user_creation:
                detail = self.server.store.organization_state(
                    int(user_creation.group(1)), self.server.instances()
                )
                result = self.server.create_headscale_user(detail["organization"], data)
            elif pending_node:
                detail = self.server.store.organization_state(
                    int(pending_node.group(1)), self.server.instances()
                )
                result = self.server.register_pending_node(detail["organization"], data)
            elif path == "/api/organizations":
                result = self.server.store.create_organization(data)
            elif path == "/api/devices":
                result = self.server.store.create_device(data)
            elif path == "/api/connections":
                result = self.server.store.create_connection(data)
            elif path == "/api/shares":
                result = self.server.store.create_share(data)
            elif path == "/api/acls":
                result = self.server.store.create_acl_rule(data)
            elif path == "/api/dns/cloudflare":
                result = configure_cloudflare_dns(self.server.cloudflare_status())
            elif path == "/api/generate":
                if not self.server.allow_generation:
                    raise InputError(
                        "generation is disabled for this deployment", HTTPStatus.FORBIDDEN
                    )
                if generate_command(str(self.server.config_path)) != 0:
                    raise InputError("configuration generation failed")
                result = {"generated": True}
            else:
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            self.send_json(result, HTTPStatus.CREATED)
        except InputError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except sqlite3.IntegrityError as exc:
            self.send_json({"error": "record conflicts with existing data", "detail": str(exc)}, HTTPStatus.CONFLICT)

    def do_DELETE(self):
        path = urlparse(self.path).path
        user_removal = re.fullmatch(r"/api/organizations/(\d+)/users/(\d+)", path)
        device_removal = re.fullmatch(r"/api/organizations/(\d+)/devices/(\d+)", path)
        try:
            if user_removal or device_removal:
                data = self.read_json()
                self.server.refresh_inventory()
                organization_id = int(
                    (user_removal or device_removal).group(1)
                )
                detail = self.server.store.organization_state(
                    organization_id, self.server.instances()
                )
                if user_removal:
                    data["user_id"] = user_removal.group(2)
                    result = self.server.delete_headscale_user(detail["organization"], data)
                else:
                    data["device_id"] = device_removal.group(2)
                    result = self.server.delete_device(detail["organization"], data)
                return self.send_json(result)
            if path.startswith("/api/acls/"):
                self.server.store.delete_acl_rule(int(path.rsplit("/", 1)[1]))
                return self.send_json({"deleted": True})
            if path.startswith("/api/shares/"):
                self.server.store.delete_share(int(path.rsplit("/", 1)[1]))
                return self.send_json({"deleted": True})
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, InputError) as exc:
            status = exc.status if isinstance(exc, InputError) else HTTPStatus.BAD_REQUEST
            return self.send_json({"error": str(exc)}, status)
        except sqlite3.IntegrityError as exc:
            return self.send_json(
                {"error": "record conflicts with existing data", "detail": str(exc)},
                HTTPStatus.CONFLICT,
            )

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise InputError("invalid Content-Length") from exc
        if length < 1 or length > 64 * 1024:
            raise InputError("JSON body must be between 1 byte and 64 KiB")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InputError("invalid JSON body") from exc
        if not isinstance(value, dict):
            raise InputError("JSON body must be an object")
        return value

    def send_json(self, value, status=HTTPStatus.OK):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, value):
        payload = value.encode()
        self.send_response(HTTPStatus.OK)
        self.security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline' 'self'; script-src 'unsafe-inline' 'self'")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


def cloudflare_request(path, token, method="GET", body=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path,
        data=payload, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise InputError(
            f"Cloudflare API rejected the request with HTTP {exc.code}",
            HTTPStatus.BAD_GATEWAY,
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise InputError("Cloudflare API could not be reached", HTTPStatus.BAD_GATEWAY) from exc
    if not result.get("success"):
        messages = "; ".join(
            item.get("message", "unknown error") for item in result.get("errors", [])
        )
        raise InputError(f"Cloudflare API error: {messages}", HTTPStatus.BAD_GATEWAY)
    return result.get("result")


def configure_cloudflare_dns(status):
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        raise InputError("CLOUDFLARE_API_TOKEN is not configured", HTTPStatus.SERVICE_UNAVAILABLE)
    if not status.get("activation_ready"):
        raise InputError(
            "DNS activation is locked until the TLS proxy and CrowdSec path are verified",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    zone = status["zone"].strip().rstrip(".").lower()
    name = status["name"].strip().rstrip(".").lower()
    target = status["target"].strip()
    try:
        address = ipaddress.ip_address(target)
    except ValueError as exc:
        raise InputError("CLOUDFLARE_DNS_TARGET is not a valid IP address") from exc
    if address.version != 4:
        raise InputError("Cloudflare automation currently requires an IPv4 target")
    if name != zone and not name.endswith("." + zone):
        raise InputError("CLOUDFLARE_DNS_NAME must belong to CLOUDFLARE_ZONE")

    zones = cloudflare_request(
        "/zones?" + urllib.parse.urlencode({"name": zone, "status": "active"}), token
    )
    if len(zones) != 1:
        raise InputError("Cloudflare zone was not found or was ambiguous", HTTPStatus.BAD_GATEWAY)
    zone_id = zones[0]["id"]
    records = cloudflare_request(
        f"/zones/{zone_id}/dns_records?" + urllib.parse.urlencode({"type": "A", "name": name}),
        token,
    )
    record = {"type": "A", "name": name, "content": target, "ttl": 300, "proxied": False}
    if records:
        record_id = records[0]["id"]
        saved = cloudflare_request(
            f"/zones/{zone_id}/dns_records/{record_id}", token, method="PUT", body=record
        )
        action = "updated"
    else:
        saved = cloudflare_request(
            f"/zones/{zone_id}/dns_records", token, method="POST", body=record
        )
        action = "created"
    return {
        "action": action, "id": saved["id"], "name": name, "target": target,
        "proxied": False,
    }


PUBLIC_ENROLLMENT = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Swarmsail device enrollment</title><style>
:root{font-family:Inter,system-ui,sans-serif;color-scheme:light dark}body{max-width:620px;margin:48px auto;padding:24px;background:#101828;color:#f2f4f7}.card{background:#1d2939;border:1px solid #344054;border-radius:14px;padding:24px}label{display:block;margin-top:16px}input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:6px;border-radius:8px;border:1px solid #475467}button{background:#175cd3;color:white;font-weight:700;cursor:pointer}.note{color:#98a2b3}.result{min-height:24px}
</style></head><body><main class="card"><h1>Connect this Mac</h1><p>In Tailscale Settings → Accounts, add this custom control server:</p><input value="__SERVER_URL__" readonly><p class="note">After the Tailscale app shows a Headscale registration command, paste only its pending key below.</p><label for="owner">Existing owner</label><input id="owner" autocomplete="username" placeholder="Headscale user name"><label for="key">Pending node key</label><input id="key" autocomplete="off" placeholder="Value after --key"><button id="register">Register device</button><p id="result" class="result" aria-live="polite"></p></main><script>
document.getElementById('register').onclick=async()=>{let key=document.getElementById('key'),result=document.getElementById('result');let body={user_name:document.getElementById('owner').value,key:key.value};key.value='';result.textContent='Registering…';try{let response=await fetch('/admin/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await response.json();if(!response.ok)throw Error(data.error||'Registration failed');result.textContent='Device registered. Return to the Tailscale app.'}catch(error){result.textContent=error.message}};
</script></body></html>"""


CROWDSEC_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CrowdSec · Swarmsail</title><style>
:root{font-family:Inter,system-ui,sans-serif;color-scheme:light;--page:#f4f7fb;--text:#172033;--muted:#596579;--card:#fff;--border:#dce3ef;--pill:#e5edfa;--shadow:#2030500b;--good:#067647;--warn:#b54708;--fresh:#175cd3}:root[data-theme="dark"]{color-scheme:dark;--page:#0d1420;--text:#edf3fc;--muted:#aab7c9;--card:#151f2e;--border:#2b3a50;--pill:#253750;--shadow:#00000045;--good:#75e0a7;--warn:#fdb022;--fresh:#53b1fd}*{box-sizing:border-box}body{max-width:1100px;margin:auto;padding:32px;background:var(--page);color:var(--text);min-height:100vh}.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px}.actions{display:flex;gap:8px}.button,a.button{width:auto;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:7px;padding:8px 12px;text-decoration:none;cursor:pointer}.note{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px;margin-top:18px}.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px;box-shadow:0 5px 18px var(--shadow)}.wide{grid-column:1/-1}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}.stat strong{display:block;font-size:26px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:11px;border-bottom:1px solid var(--border);vertical-align:top}th{font-size:12px;text-transform:uppercase;color:var(--muted)}.pill{display:inline-block;font-size:12px;padding:3px 8px;border-radius:99px;background:var(--pill)}.running{color:var(--good)}.warning,.error{color:var(--warn)}ul{padding-left:20px}li{margin:8px 0}.data-changed{animation:data-changed 2.2s ease-out}@keyframes data-changed{0%{color:var(--fresh);text-shadow:0 0 12px color-mix(in srgb,var(--fresh) 65%,transparent)}100%{color:inherit;text-shadow:none}}@media(prefers-reduced-motion:reduce){.data-changed{animation:none}}
</style><style>body{max-width:none;margin:0;padding:24px clamp(20px,4vw,56px)}.app-layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:clamp(24px,4vw,58px);max-width:1540px;margin:0 auto}.app-sidebar{align-self:start;position:sticky;top:22px}.app-sidebar .crew-picker{margin-bottom:16px}.app-sidebar .crew-picker summary{cursor:pointer;font-weight:700}.app-sidebar .crew-picker ul{padding:0;list-style:none;max-height:260px;overflow:auto}.app-sidebar .crew-picker li{margin:4px 0}.app-sidebar .crew-picker a,.app-sidebar .side-nav a{display:block;padding:9px 11px;border-radius:8px;color:var(--muted);text-decoration:none}.app-sidebar .crew-picker a:hover,.app-sidebar .side-nav a:hover,.app-sidebar .side-nav a[aria-current=page]{background:var(--pill);color:var(--text)}.app-sidebar .crew-picker small{display:block}.app-sidebar .side-nav{display:grid;gap:5px}.app-content{min-width:0}.app-content>.topbar{padding:0}.app-content>.grid{margin-top:18px}@media(max-width:900px){.app-layout{grid-template-columns:1fr;gap:12px}.app-sidebar{position:static}.app-sidebar .crew-picker ul{display:flex;gap:6px;max-height:none;overflow-x:auto}.app-sidebar .crew-picker li{flex:none}.app-sidebar .side-nav{display:flex;gap:6px;overflow-x:auto}.app-sidebar .side-nav a{white-space:nowrap;border:1px solid var(--border)}}
</style><script>document.documentElement.dataset.theme=localStorage.getItem('swarmsail-theme')||'dark'</script></head><body>
<div class="app-layout"><aside class="app-sidebar" aria-label="Captain navigation"><details class="crew-picker" open><summary>Crews</summary><ul id="crowdsec-crews"><li>Loading Crews…</li></ul></details><nav class="side-nav" aria-label="Pages"><a href="/#setup">Set up</a><a href="/#policy">Access policy</a><a href="/#inventory">Inventory</a><a href="/crowdsec" aria-current="page">Security</a></nav></aside><main class="app-content">
<div class="topbar"><div><h1>CrowdSec</h1><p class="note">Read-only security activity from this server’s local CrowdSec service.</p></div><div class="actions"><button class="button" id="theme" onclick="toggleTheme()">Dark mode</button></div></div>
<p id="status" class="note" aria-live="polite">Loading local data…</p><p id="error" class="error"></p>
<div class="stats"><div class="stat"><strong id="appsec-blocked" data-live-value>—</strong>AppSec blocks</div><div class="stat"><strong id="appsec-processed" data-live-value>—</strong>AppSec requests</div><div class="stat"><strong id="bouncer-dropped" data-live-value>—</strong>Bouncer drops</div><div class="stat"><strong id="decision-count" data-live-value>—</strong>Active decisions shown</div></div>
<div class="grid"><section class="card"><h2>Service</h2><dl id="service"></dl></section><section class="card"><h2>24-hour log sample</h2><dl id="logs"></dl><p class="note">Counts use the latest 500 local service-log lines.</p></section><section class="card wide"><h2>Log acquisition</h2><div class="table-wrap"><table><thead><tr><th>Source</th><th>Read</th><th>Parsed</th><th>Unparsed</th><th>Whitelisted</th></tr></thead><tbody id="acquisition"></tbody></table></div></section><section class="card"><h2>Top alert scenarios</h2><ol id="alerts"></ol></section><section class="card"><h2>Recent service issues</h2><ul id="issues"></ul></section><section class="card wide"><h2>Active decisions</h2><p class="note">Visitor addresses are reduced to network prefixes. No raw IP addresses or log files are exposed.</p><div class="table-wrap"><table><thead><tr><th>Scenario</th><th>Action</th><th>Origin</th><th>Target network</th><th>Remaining</th></tr></thead><tbody id="decisions"></tbody></table></div></section></div>
</main></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),num=n=>Number(n||0).toLocaleString();let loading=false;
function themeLabel(){theme.textContent=document.documentElement.dataset.theme==='dark'?'Light mode':'Dark mode'}function toggleTheme(){let next=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=next;localStorage.setItem('swarmsail-theme',next);themeLabel()}
async function loadCrews(){try{let response=await fetch('/api/state'),data=await response.json();if(!response.ok)throw Error(data.error||'Crew list unavailable');crowdsecCrews.innerHTML=data.organizations.map(o=>`<li><a href="/organizations/${o.id}">${esc(o.name)}<small>${o.headscale_instance?'Mast '+esc(o.headscale_instance):'Shared Mast'}</small></a></li>`).join('')||'<li>No Crews yet</li>'}catch(e){crowdsecCrews.innerHTML=`<li class="error">${esc(e.message)}</li>`}}
async function load(){if(loading||document.hidden)return;loading=true;try{let response=await fetch('/api/crowdsec'),data=await response.json();if(!response.ok)throw Error(data.error||'CrowdSec data unavailable');error.textContent='';status.textContent='Updated '+new Date(data.generated_at).toLocaleString();appsecBlocked.textContent=num(data.appsec.blocked);appsecProcessed.textContent=num(data.appsec.processed);bouncerDropped.textContent=num(data.bouncer.dropped);decisionCount.textContent=num(data.active_decisions.length);service.innerHTML=`<dt>Status</dt><dd><span class="pill ${data.container.status==='running'?'running':'warning'}">${esc(data.container.status)}</span></dd><dt>Container</dt><dd>${esc(data.container.name)}</dd><dt>Started</dt><dd>${esc(new Date(data.container.started_at).toLocaleString())}</dd>`;logs.innerHTML=`<dt>Sampled</dt><dd>${num(data.log_window.sampled_lines)} lines</dd><dt>Info</dt><dd>${num(data.log_window.levels.info)}</dd><dt>Warnings</dt><dd>${num(data.log_window.levels.warning)}</dd><dt>Errors</dt><dd>${num(data.log_window.levels.error)}</dd>`;acquisition.innerHTML=data.acquisition.map(x=>`<tr><td>${esc(x.source)}</td><td>${num(x.read)}</td><td>${num(x.parsed)}</td><td>${num(x.unparsed)}</td><td>${num(x.whitelisted)}</td></tr>`).join('')||'<tr><td colspan="5">No acquisition metrics.</td></tr>';alerts.innerHTML=data.top_alerts.map(x=>`<li>${esc(x.scenario)} <span class="pill">${num(x.count)}</span></li>`).join('')||'<li>No alerts reported.</li>';issues.innerHTML=data.recent_issues.map(x=>`<li><span class="pill ${esc(x.level)}">${esc(x.level)}</span> ${esc(x.message)}</li>`).join('')||'<li>No warnings or errors in the sampled lines.</li>';decisions.innerHTML=data.active_decisions.map(x=>`<tr><td>${esc(x.scenario)}</td><td><span class="pill">${esc(x.type)}</span></td><td>${esc(x.origin)}</td><td>${esc(x.target)}</td><td>${esc(x.duration)}</td></tr>`).join('')||'<tr><td colspan="5">No active decisions.</td></tr>'}catch(e){error.textContent=e.message;status.textContent='Refresh delayed'}finally{loading=false}}
function watchChanges(){let values=new Map([...document.querySelectorAll('[data-live-value]')].map(node=>[node,node.textContent]));let observer=new MutationObserver(records=>{for(let record of records){let node=record.target.nodeType===3?record.target.parentElement:record.target,target=node&&node.closest('[data-live-value]');if(!target)continue;let next=target.textContent,previous=values.get(target);values.set(target,next);if(previous===undefined||previous===next)continue;target.classList.remove('data-changed');void target.offsetWidth;target.classList.add('data-changed');setTimeout(()=>target.classList.remove('data-changed'),2300)}});document.querySelectorAll('[data-live-value]').forEach(node=>observer.observe(node,{subtree:true,childList:true,characterData:true}))}
const theme=document.getElementById('theme'),error=document.getElementById('error'),status=document.getElementById('status'),crowdsecCrews=document.getElementById('crowdsec-crews'),appsecBlocked=document.getElementById('appsec-blocked'),appsecProcessed=document.getElementById('appsec-processed'),bouncerDropped=document.getElementById('bouncer-dropped'),decisionCount=document.getElementById('decision-count'),service=document.getElementById('service'),logs=document.getElementById('logs'),acquisition=document.getElementById('acquisition'),alerts=document.getElementById('alerts'),issues=document.getElementById('issues'),decisions=document.getElementById('decisions');themeLabel();loadCrews();load().then(watchChanges);setInterval(load,15000);window.addEventListener('focus',load);document.addEventListener('visibilitychange',load);
</script></body></html>"""


HEALTH_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#101714"><meta http-equiv="refresh" content="30">
<title>Swarmsail · Service status</title><style>
:root{color-scheme:dark;--bg:#101714;--panel:#18211c;--line:#2d3a32;--text:#eef5ef;--muted:#a2b0a6;--green:#a8ed72;--green-bg:#1d3926;font:16px Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
*{box-sizing:border-box}body{min-height:100vh;margin:0;display:grid;place-items:center;padding:24px}.page{width:min(100%,680px)}.brand{display:flex;align-items:center;gap:12px;margin-bottom:24px;font-weight:700;letter-spacing:.02em}.mark{width:38px;height:38px;display:grid;place-items:center;border-radius:12px;background:var(--green-bg);color:var(--green);font-size:22px}.card{border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:clamp(24px,6vw,42px);box-shadow:0 24px 80px #0004}h1{font-size:clamp(28px,6vw,38px);letter-spacing:-.035em;margin:0 0 12px}.intro{color:var(--muted);line-height:1.6;margin:0 0 30px}.status{display:flex;align-items:center;gap:12px;border:1px solid #31543a;border-radius:12px;background:#17281d;padding:16px}.dot{width:11px;height:11px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px #a8ed721c}.status strong{display:block;font-size:16px}.status small{display:block;color:#b7c6ba;margin-top:3px}.meta{border-top:1px solid var(--line);margin-top:28px;padding-top:18px;color:var(--muted);font-size:13px;display:flex;justify-content:space-between;gap:12px}.footer{color:var(--muted);font-size:12px;margin:18px 4px}
</style></head><body><main class="page"><div class="brand"><span class="mark" aria-hidden="true">↗</span><span>Swarmsail</span></div><section class="card" aria-labelledby="title"><h1 id="title">Service status</h1><p class="intro">Swarmsail’s control plane is responding and ready to coordinate managed tailnets.</p><div class="status" role="status"><span class="dot" aria-hidden="true"></span><div><strong>Operational</strong><small>Control plane is healthy</small></div></div><div class="meta"><span>Last checked</span><time>__CHECKED_AT__</time></div></section><p class="footer">This status page refreshes automatically every 30 seconds.</p></main></body></html>"""


DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Swarmsail · Captain’s console</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color-scheme:light;--page:#edf7fd;--text:#18344b;--muted:#607b90;--card:#ffffff;--border:#d3e7f4;--field:#f9fdff;--pill:#e4f2fb;--isolated:#dceffc;--shadow:#194b6810;--accent:#1682bb;--accent-hover:#116b9b;--accent-soft:#e2f2fb;--danger:#a33e2f;--online-bg:#e2f4eb;--online-text:#276749;--offline-bg:#f4e2dc;--offline-text:#a33e2f;--fresh:#087eb3}
:root[data-theme="dark"]{color-scheme:dark;--page:#101f2b;--text:#e9f5fc;--muted:#a3bfd0;--card:#192c3a;--border:#30495a;--field:#142634;--pill:#254255;--isolated:#244a64;--shadow:#00000030;--accent:#80c8ec;--accent-hover:#a2daf4;--accent-soft:#203b4e;--danger:#f0a18f;--online-bg:#25483c;--online-text:#b8e8d0;--offline-bg:#4b302b;--offline-text:#f0a18f;--fresh:#89d2f2}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:var(--page);color:var(--text);min-height:100vh;line-height:1.5}a{color:inherit}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;max-width:1540px;margin:0 auto;padding:24px clamp(20px,4vw,56px) 10px}.topbar h1{margin:0;font:700 27px/1.1 Georgia,"Times New Roman",serif;letter-spacing:-.04em}.topbar h1 a{color:var(--text);text-decoration:none}.topbar h1 a:before{content:"S";display:inline-grid;place-items:center;width:34px;height:34px;margin-right:10px;border-radius:11px;background:var(--accent);color:var(--page);font:700 19px/1 ui-sans-serif,system-ui;vertical-align:-3px}.topbar>div{display:flex;align-items:center;gap:14px}.back-link{display:inline-block;color:var(--accent);font-weight:650;text-decoration:none}.theme-toggle{width:auto!important;background:transparent!important;color:var(--text)!important;border:1px solid var(--border)!important;padding:8px 12px!important}h2{font:700 20px/1.2 Georgia,"Times New Roman",serif;letter-spacing:-.02em;margin:0 0 12px}.note{color:var(--muted)}body>.note{max-width:1540px;margin:0 auto;padding:2px clamp(20px,4vw,56px) 14px}#error{max-width:1540px;margin:0 auto;padding:0 clamp(20px,4vw,56px)}.dashboard-layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:clamp(24px,4vw,58px);max-width:1540px;margin:0 auto;padding:20px clamp(20px,4vw,56px) 64px}.sidebar{align-self:start;position:sticky;top:22px;padding:18px 0}.sidebar-label,.eyebrow{font-size:11px;font-weight:750;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}.sidebar-label{margin:0 0 12px 12px}.side-nav{display:grid;gap:5px}.side-nav a{display:flex;align-items:center;gap:12px;padding:11px 12px;border-radius:9px;color:var(--muted);text-decoration:none;font-size:14px;font-weight:600}.side-nav a:before{content:"";width:7px;height:7px;border:1px solid currentColor;border-radius:50%}.side-nav a:hover,.side-nav a:focus-visible{background:var(--accent-soft);color:var(--accent)}.sidebar-foot{margin:26px 12px 0;padding-top:17px;border-top:1px solid var(--border);font-size:12px;color:var(--muted)}.dashboard-content{min-width:0}.content-section{margin:0 0 36px;scroll-margin-top:22px}.section-heading{display:flex;align-items:end;justify-content:space-between;gap:18px;margin:0 0 18px}.section-heading h2{font-size:28px;margin:4px 0 0}.section-heading p{margin:5px 0 0;color:var(--muted)}.form-grid,.inventory-grid,.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{min-width:0;background:var(--card);border:1px solid var(--border);border-radius:13px;padding:22px;box-shadow:0 8px 24px var(--shadow)}.card h2{font-size:19px}.card .note{font-size:13px}.card>p:first-of-type{margin-top:0}.form-grid .card{border-top:3px solid var(--accent)}.form-grid .card:nth-child(2){border-top-color:#bb7950}.form-grid .card:nth-child(3){border-top-color:#788b67}.form-grid .card:nth-child(4){border-top-color:#b29862}.inventory-grid .card:first-child{grid-row:span 2}.inventory-grid .card ul{padding:0;list-style:none}.inventory-grid .card li{padding:10px 0;margin:0;border-bottom:1px solid var(--border);overflow-wrap:anywhere}.inventory-grid .card li:last-child{border-bottom:0}.inventory-grid .card li:only-child{color:var(--muted);font-style:italic}.wide{grid-column:1/-1}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:18px 0}.stat{background:var(--accent-soft);border:1px solid var(--border);border-radius:10px;padding:14px;color:var(--muted);font-size:13px}.stat strong{display:block;color:var(--text);font-size:26px;line-height:1.2}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:11px;border-bottom:1px solid var(--border);vertical-align:top}th{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}label{display:block;margin:11px 0 3px;color:var(--muted);font-size:12px;font-weight:700}input,select,button{width:100%;padding:11px 12px;margin:4px 0 9px;border:1px solid var(--border);border-radius:8px;background:var(--field);color:var(--text);font:inherit}input::placeholder{color:var(--muted);opacity:.8}input:focus,select:focus,button:focus-visible,a:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 35%,transparent);outline-offset:2px}button{background:var(--accent);color:#fffdf8;border-color:var(--accent);font-weight:700;cursor:pointer;transition:background-color .15s ease,transform .15s ease}button:hover:not(:disabled){background:var(--accent-hover);transform:translateY(-1px)}button:disabled{opacity:.5;cursor:not-allowed}.danger{background:var(--danger);border-color:var(--danger);color:#fff;width:auto;padding:5px 9px;font-size:12px}.pill{display:inline-block;font-size:11px;font-weight:700;padding:4px 9px;border-radius:999px;background:var(--pill);white-space:nowrap}.status-online{background:var(--online-bg);color:var(--online-text)}.status-offline{background:var(--offline-bg);color:var(--offline-text)}.isolated{background:var(--isolated)}dl{display:grid;grid-template-columns:minmax(110px,.7fr) 1.3fr;gap:8px 14px}dt{color:var(--muted);font-size:13px}dd{margin:0;overflow-wrap:anywhere}.error{color:var(--danger);min-height:24px}[hidden]{display:none!important}.data-changed{animation:data-changed 2.2s ease-out}@keyframes data-changed{0%{color:var(--fresh);text-shadow:0 0 12px color-mix(in srgb,var(--fresh) 55%,transparent)}100%{color:inherit;text-shadow:none}}@media(max-width:900px){.dashboard-layout{grid-template-columns:1fr;gap:8px;padding-top:8px}.sidebar{position:static;padding:8px 0}.sidebar-label,.sidebar-foot{display:none}.side-nav{display:flex;gap:6px;overflow-x:auto;padding:3px 0 9px}.side-nav a{flex:none;padding:9px 11px;background:var(--card);border:1px solid var(--border)}.inventory-grid .card:first-child{grid-row:auto}}@media(max-width:560px){.topbar{padding-top:17px}.topbar h1{font-size:23px}.topbar h1 a:before{width:30px;height:30px}.topbar>div{gap:8px}.theme-toggle{padding:7px 9px!important;font-size:12px}.back-link{font-size:13px}.form-grid,.inventory-grid,.detail-grid{grid-template-columns:1fr}.section-heading{display:block}.section-heading h2{font-size:25px}.card{padding:18px}.wide{grid-column:auto}#organization-view{padding:0 20px 36px}#organization-view>.card{margin-top:14px!important}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}dl{grid-template-columns:1fr;gap:2px}dd{margin-bottom:10px}}@media(prefers-reduced-motion:reduce){*,html{scroll-behavior:auto!important;transition:none!important}.data-changed{animation:none}}
</style><style>.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}#organization-view{max-width:1200px;margin:0 auto;padding:8px clamp(20px,4vw,56px) 56px}.form-grid .card:nth-child(2){border-top-color:#61b6dc}.form-grid .card:nth-child(3){border-top-color:#3c9dc8}.form-grid .card:nth-child(4){border-top-color:#92cbe1}.crew-picker{margin:0 0 12px;padding:0 12px}.crew-picker summary{cursor:pointer;color:var(--text);font-size:14px;font-weight:750;list-style:none}.crew-picker summary::-webkit-details-marker{display:none}.crew-picker summary:after{content:"＋";float:right;color:var(--muted)}.crew-picker[open] summary:after{content:"−"}.crew-picker ul{display:grid;gap:4px;max-height:260px;overflow:auto;margin:10px 0 0;padding:0;list-style:none}.crew-picker li a{display:block;padding:8px 10px;border-radius:8px;color:var(--muted);font-size:13px;text-decoration:none;overflow-wrap:anywhere}.crew-picker li a:hover,.crew-picker li a:focus-visible,.crew-picker li a[aria-current=page]{background:var(--accent-soft);color:var(--accent)}.crew-picker li a small{display:block;color:var(--muted);font-size:11px}.crew-picker li.empty{padding:8px 10px;color:var(--muted);font-size:12px}.side-nav{margin-top:20px}.side-nav a[aria-current=page]{background:var(--accent-soft);color:var(--accent)}.side-nav a[href="/crowdsec"]:before{border-radius:2px}#crew-devices-section>label{max-width:360px}#crew-devices-section>select{max-width:360px}@media(max-width:900px){.crew-picker{padding:0}.crew-picker summary{padding:9px 11px;background:var(--card);border:1px solid var(--border);border-radius:9px}.crew-picker ul{display:flex;max-height:none;overflow-x:auto;padding:5px 0}.crew-picker li{flex:none}.crew-picker li a{background:var(--card);border:1px solid var(--border)}.side-nav{margin-top:8px}}@media(max-width:560px){#organization-view{padding:0 20px 36px}}</style><script>const preferredTheme=localStorage.getItem('swarmsail-theme')||'dark';document.documentElement.dataset.theme=preferredTheme;</script></head><body><div class="topbar"><h1><a href="/">Swarmsail</a></h1><div><a class="back-link" href="/crowdsec">CrowdSec</a> <button id="theme-toggle" class="theme-toggle" onclick="toggleTheme()" aria-label="Switch color theme">Dark mode</button></div></div><p class="note">Captain’s console for Crews, Masts, mateys, devices, and sharing intent. <span id="refresh-status" aria-live="polite"></span></p><div id="error" class="error"></div>
<main class="dashboard-layout">
<aside class="sidebar" aria-label="Captain navigation">
<details class="crew-picker" open><summary>Crews</summary><ul id="sidebar-crews"></ul></details>
<nav class="side-nav" aria-label="Pages"><a href="/#setup" data-page="setup">Set up</a><a href="/#policy" data-page="policy">Access policy</a><a href="/#inventory" data-page="inventory">Inventory</a><a href="/crowdsec">Security</a></nav>
<p class="sidebar-foot">Changes here model intent. Planned access rules are not applied to Headscale yet.</p></aside>
<div class="dashboard-content">
<div id="dashboard-view">
<section id="setup" class="content-section"><div class="section-heading"><div><span class="eyebrow">Captain’s console</span><h2>Build your network</h2><p>Create Crews, add devices, and define how they connect.</p></div></div>
<div class="form-grid"><section class="card"><h2>New Crew</h2><label for="org-name">Crew name</label><input id="org-name" placeholder="e.g. Northstar Studio"><label for="org-mode">Network model</label><select id="org-mode"><option value="collaborative">Collaborative</option><option value="isolated">Totally isolated tailnet</option></select><label for="org-instance">Mast</label><select id="org-instance"><option value="">Shared Mast</option></select><button onclick="addOrg()">Create Crew</button></section>
<section class="card"><h2>Connect Crews</h2><label for="connection-a">First Crew</label><select id="connection-a"></select><label for="connection-b">Second Crew</label><select id="connection-b"></select><button onclick="addConnection()">Plan connection</button></section>
<section class="card"><h2>Manual device record</h2><p class="note">Adds an inventory entry only; use a Crew’s enrollment controls to connect a real device.</p><label for="device-org">Crew</label><select id="device-org"></select><label for="device-name">Device name</label><input id="device-name" placeholder="e.g. design-macbook"><label for="device-node">Headscale node ID <span class="note">(optional)</span></label><input id="device-node" placeholder="Node ID"><button onclick="addDevice()">Add inventory record</button></section>
<section class="card"><h2>Share device</h2><label for="share-device">Device</label><select id="share-device"></select><label for="share-target">Target Crew</label><select id="share-target"></select><label for="share-permission">Permission</label><select id="share-permission"><option value="connect">Connect</option><option value="manage">Manage</option></select><button onclick="addShare()">Plan share</button></section></div></section>
<section id="policy" class="content-section"><div class="section-heading"><div><span class="eyebrow">Guardrails</span><h2>Shape access</h2><p>Keep trust explicit and DNS records direct to their host.</p></div></div>
<div class="form-grid"><section class="card"><h2>ACL rule</h2><label for="acl-source">Source Crew</label><select id="acl-source"></select><label for="acl-device">Destination device</label><select id="acl-device"></select><label for="acl-protocol">Protocol</label><select id="acl-protocol"><option value="any">Any protocol</option><option value="tcp">TCP</option><option value="udp">UDP</option><option value="icmp">ICMP</option></select><label for="acl-ports">Ports</label><input id="acl-ports" value="*" placeholder="*, 443, or 8000-8100"><button onclick="addAcl()">Plan ACL rule</button></section>
<section class="card"><h2>Cloudflare DNS</h2><p class="note" id="cloudflare-status"></p><button id="cloudflare-button" onclick="configureDns()">Configure DNS-only record</button></section>
<section class="card"><h2>Traffic protection</h2><p id="security-path"></p><p class="note" id="security-note"></p></section></div></section>
<section id="inventory" class="content-section"><div class="section-heading"><div><span class="eyebrow">At a glance</span><h2>Crew inventory</h2><p>Open a Crew to inspect its Mast, mateys, devices, and policy.</p></div></div>
<div class="inventory-grid"><section class="card"><h2>Crews</h2><p class="note">Select a Crew to view its details.</p><ul id="orgs"></ul></section><section class="card"><h2>Connections</h2><ul id="connections"></ul></section><section class="card"><h2>Devices</h2><ul id="devices"></ul></section><section class="card"><h2>Shares</h2><p class="note" id="sharing-note"></p><ul id="shares"></ul></section><section class="card"><h2>ACL policy</h2><p class="note">Deny by default. Planned rules do not change Headscale until reconciled.</p><ul id="acls"></ul></section></div></section>
</div>
<div id="organization-view" hidden>
<a class="back-link" href="/">← All Crews</a>
<section class="card"><div class="topbar"><div><h2 id="detail-name"></h2><p class="note" id="detail-summary"></p></div><span class="pill" id="detail-mode"></span></div><div class="stats"><div class="stat"><strong id="detail-user-count" data-live-value>0</strong>Mateys</div><div class="stat"><strong id="detail-device-count" data-live-value>0</strong>Devices</div><div class="stat"><strong id="detail-online-count" data-live-value>0</strong>Online devices</div><div class="stat"><strong id="detail-acl-count" data-live-value>0</strong>ACL rules</div><div class="stat"><strong id="detail-connection-count" data-live-value>0</strong>Connections</div><div class="stat"><strong id="detail-share-count" data-live-value>0</strong>Shares</div></div></section>
<section class="card" style="margin-top:18px"><h2>Enroll a device</h2><p class="note">Choose its matey, then generate a one-time key that expires in one hour.</p><div class="grid"><div><label for="registration-user">Matey</label><select id="registration-user"></select><button id="registration-button" onclick="createRegistration()">Generate connection command</button></div><div><label for="new-user-name">New matey</label><input id="new-user-name" maxlength="63" placeholder="Matey name"><button class="theme-toggle" onclick="createHeadscaleUser()">Create matey</button></div></div><div id="registration-result" hidden><p><strong>Run this on the new device:</strong></p><input id="registration-command" readonly><button class="theme-toggle" onclick="copyRegistration()">Copy command</button><p class="note">This key is shown only here. Generate another if it expires or leaves this page.</p></div></section>
<section class="card" style="margin-top:18px"><h2>Register a macOS GUI device</h2><p class="note">In Tailscale Settings → Accounts, add this custom control server. Paste the pending node key shown by the client below, then choose its owner above.</p><label for="macos-server-url">Custom control server</label><input id="macos-server-url" readonly><label for="pending-node-key">Pending node key</label><input id="pending-node-key" autocomplete="off" placeholder="Paste the key after --key"><button onclick="registerPendingNode()">Register pending device</button><p class="note" id="pending-node-result"></p></section>
<div class="grid" style="margin-top:18px"><section class="card"><h2>Mast and tailnet</h2><dl id="detail-tailnet"></dl></section><section class="card"><h2>DNS settings</h2><dl id="detail-dns"></dl><div id="detail-split-dns"></div></section><section class="card"><h2>Trust and sharing</h2><ul id="detail-trust"></ul></section></div>
<section class="card" style="margin-top:18px"><h2>Access controls</h2><p><span class="pill">Default: deny</span></p><p class="note" id="detail-policy-status"></p><div class="table-wrap"><table><thead><tr><th>Direction</th><th>Source</th><th>Destination</th><th>Protocol</th><th>Ports</th><th>Status</th></tr></thead><tbody id="detail-acls"></tbody></table></div></section>
<section class="card" style="margin-top:18px"><h2>Mateys</h2><div class="table-wrap"><table><thead><tr><th>Name</th><th>Devices</th><th>Online</th><th>Matey ID</th><th>Actions</th></tr></thead><tbody id="detail-users"></tbody></table></div></section>
<section id="crew-devices-section" class="card" style="margin-top:18px"><h2>Devices</h2><label for="device-matey-filter">Show devices for</label><select id="device-matey-filter"><option value="">All mateys</option></select><p class="note" id="filtered-device-caption"></p><div class="table-wrap"><table><thead><tr><th>Name</th><th>Matey</th><th>Addresses</th><th>Status</th><th>Last seen</th><th>Node ID</th><th>Actions</th></tr></thead><tbody id="detail-devices"></tbody></table></div></section>
</div>
</div>
</main>
<script>
let state={},organizationState=null,loading=false;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,method='GET',body){let r=await fetch(path,{method,headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):null});let d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
async function load(silent=false){if(loading)return;loading=true;try{let match=location.pathname.match(/^\/organizations\/(\d+)$/);if(match){let results=await Promise.all([api('/api/state'),api('/api/organizations/'+match[1])]);state=results[0];organizationState=results[1];renderOrganization()}else{state=await api('/api/state');organizationState=null;render()}refreshStatus.textContent='Updated '+new Date().toLocaleTimeString();if(silent)error.textContent=''}catch(e){if(!silent)error.textContent=e.message;refreshStatus.textContent='Refresh delayed'}finally{loading=false}}
function refreshIfIdle(){if(document.hidden)return;let editing=document.activeElement&&document.activeElement.matches('input,select,textarea');if(!editing)load(true)}
function formatLastSeen(value){if(!value)return 'Never';let date=new Date(value);if(Number.isNaN(date.getTime()))return value;let seconds=Math.round((date.getTime()-Date.now())/1000),absolute=Math.abs(seconds),unit='second',amount=seconds;if(absolute>=86400){unit='day';amount=Math.round(seconds/86400)}else if(absolute>=3600){unit='hour';amount=Math.round(seconds/3600)}else if(absolute>=60){unit='minute';amount=Math.round(seconds/60)}let relative=new Intl.RelativeTimeFormat(undefined,{numeric:'auto'}).format(amount,unit);return `<span title="${esc(date.toLocaleString())}">${esc(relative)}</span>`}
function options(rows,label){return rows.map(x=>`<option value="${x.id}">${esc(label(x))}</option>`).join('')}
function renderSidebar(){let crewId=organizationState&&organizationState.organization?String(organizationState.organization.id):'';sidebarCrews.innerHTML=(state.organizations||[]).map(o=>`<li><a href="/organizations/${o.id}" ${String(o.id)===crewId?'aria-current="page"':''}>${esc(o.name)}<small>${o.headscale_instance?'Mast '+esc(o.headscale_instance):'Shared Mast'}</small></a></li>`).join('')||'<li class="empty">No Crews yet</li>'}
function showDashboardPage(){if(dashboardView.hidden)return;let page=['setup','policy','inventory'].includes(location.hash.slice(1))?location.hash.slice(1):'setup';dashboardView.querySelectorAll('.content-section').forEach(section=>section.hidden=section.id!==page);document.querySelectorAll('.side-nav [data-page]').forEach(link=>{if(link.dataset.page===page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')})}
function render(){dashboardView.hidden=false;organizationView.hidden=true;organizationState=null;error.textContent='';renderSidebar();showDashboardPage();let collaborative=state.organizations.filter(o=>o.mode==='collaborative');orgs.innerHTML=state.organizations.map(o=>`<li><a class="org-link" href="/organizations/${o.id}">${esc(o.name)}</a> <span class="pill ${o.mode==='isolated'?'isolated':''}">${o.mode}</span>${o.headscale_instance?' · tailnet '+esc(o.headscale_instance):''}</li>`).join('')||'<li>None yet</li>';connections.innerHTML=state.connections.map(c=>`<li>${esc(c.organization_a_name)} ↔ ${esc(c.organization_b_name)} <span class="pill">${esc(c.status)}</span></li>`).join('')||'<li>None yet</li>';devices.innerHTML=state.devices.map(d=>{let status=d.online?'online':'offline';return `<li>${esc(d.name)} · ${esc(d.organization_name)} <span class="pill status-${status}">${status}</span></li>`}).join('')||'<li>None yet</li>';shares.innerHTML=state.shares.map(s=>`<li>${esc(s.device_name)} → ${esc(s.target_organization_name)} <span class="pill">${esc(s.permission)} · ${esc(s.status)}</span> <button class="danger" onclick="removeShare(${s.id})">Remove</button></li>`).join('')||'<li>None yet</li>';acls.innerHTML=state.acl_rules.map(a=>`<li>${esc(a.source_organization_name)} → ${esc(a.destination_device_name)} (${esc(a.destination_organization_name)}) <span class="pill">${esc(a.protocol)}:${esc(a.ports)} · ${esc(a.status)}</span> <button class="danger" onclick="removeAcl(${a.id})">Remove</button></li>`).join('')||'<li>Deny all cross-organization traffic</li>';sharingNote.textContent=state.sharing_note;deviceOrg.innerHTML=options(state.organizations,o=>o.name);connectionA.innerHTML=options(collaborative,o=>o.name);connectionB.innerHTML=options(collaborative,o=>o.name);shareDevice.innerHTML=options(state.devices,d=>d.name+' · '+d.organization_name);shareTarget.innerHTML=options(collaborative,o=>o.name);aclSource.innerHTML=options(state.organizations,o=>o.name);aclDevice.innerHTML=options(state.devices,d=>d.name+' · '+d.organization_name);orgInstance.innerHTML='<option value="">Shared control plane</option>'+state.headscale_instances.map(i=>`<option value="${esc(i.id)}">Instance ${esc(i.id)} · ${esc(i.server_url||'')}</option>`).join('');cloudflareStatus.textContent=!state.cloudflare.configured?'Set CLOUDFLARE_API_TOKEN in the server environment file':!state.cloudflare.activation_ready?'Token configured; waiting for TLS proxy and CrowdSec verification':`${state.cloudflare.name} → ${state.cloudflare.target} (DNS only)`;cloudflareButton.disabled=!state.cloudflare.configured||!state.cloudflare.activation_ready;securityPath.textContent=state.security.crowdsec_required?'Required: '+state.security.http_path:'CrowdSec protection is optional';securityNote.textContent=state.security.stun_note}
function renderOrganization(){dashboardView.hidden=true;organizationView.hidden=false;error.textContent='';renderSidebar();let d=organizationState,o=d.organization,t=d.tailnet||{},dns=d.dns;detailName.textContent=o.name;detailMode.textContent=o.mode;detailMode.className='pill '+(o.mode==='isolated'?'isolated':'');detailSummary.textContent=o.mode==='isolated'?'Dedicated, totally isolated tailnet':'Collaborative organization with explicit trust and ACLs';detailDeviceCount.textContent=d.devices.length;detailAclCount.textContent=d.acl_rules.length;detailConnectionCount.textContent=d.connections.length;detailShareCount.textContent=d.shares.length;registrationUser.innerHTML=(d.headscale_users||[]).map(u=>`<option value="${esc(u.id)}">${esc(u.name)}</option>`).join('')||'<option value="">No Headscale users found</option>';registrationButton.disabled=!(d.headscale_users||[]).length;detailPolicyStatus.textContent=d.policy_status;detailTailnet.innerHTML=t.id?`<dt>Control server</dt><dd>${esc(t.server_url||'Not set')}</dd><dt>IPv4 space</dt><dd>${esc(t.ipv4_prefix||'Not set')}</dd><dt>Base domain</dt><dd>${esc(t.base_domain||'Not set')}</dd><dt>Management</dt><dd>${esc(t.management_mode||'managed')}</dd><dt>Ingress</dt><dd>${esc(t.security_path||'existing deployment')}</dd>`:'<p class="note">This organization is not assigned to a dedicated Headscale instance.</p>';if(dns){let flag=v=>v===true?'Enabled':v===false?'Disabled':'Unknown';detailDns.innerHTML=`<dt>MagicDNS</dt><dd><span class="pill">${flag(dns.magic_dns)}</span></dd><dt>Tailnet domain</dt><dd>${esc(dns.base_domain||'Not set')}</dd><dt>Override local DNS</dt><dd>${flag(dns.override_local_dns)}</dd><dt>Global resolvers</dt><dd>${(dns.global_nameservers||[]).map(esc).join('<br>')||'None'}</dd><dt>Search domains</dt><dd>${(dns.search_domains||[]).map(esc).join('<br>')||'None'}</dd>`;let routes=Object.entries(dns.split_nameservers||{});detailSplitDns.innerHTML=`<h3>Split DNS routes</h3><ul>${routes.map(([domain,servers])=>`<li><strong>${esc(domain)}</strong> → ${servers.map(esc).join(', ')}</li>`).join('')||'<li>None</li>'}</ul>${(dns.extra_records||[]).length?`<h3>Extra records</h3><ul>${dns.extra_records.map(r=>`<li>${esc(r.name)} ${esc(r.type)} ${esc(r.value)}</li>`).join('')}</ul>`:''}`}else{detailDns.innerHTML='<p class="note">DNS inventory is not available yet.</p>';detailSplitDns.innerHTML=''}detailTrust.innerHTML=d.connections.map(c=>`<li>Connected to ${esc(c.organization_a_id===o.id?c.organization_b_name:c.organization_a_name)} <span class="pill">${esc(c.status)}</span></li>`).concat(d.shares.map(s=>`<li>${esc(s.device_name)}: ${esc(s.owner_organization_id===o.id?'shared with '+s.target_organization_name:'shared into this organization')} <span class="pill">${esc(s.permission)} · ${esc(s.status)}</span></li>`)).join('')||'<li>No organization trust or device shares</li>';detailAcls.innerHTML=d.acl_rules.map(a=>{let direction=a.source_organization_id===o.id?(a.destination_organization_id===o.id?'internal':'outbound'):'inbound';return `<tr><td><span class="pill">${direction}</span></td><td>${esc(a.source_organization_name)}</td><td>${esc(a.destination_device_name)}<br><span class="note">${esc(a.destination_organization_name)}</span></td><td>${esc(a.protocol)}</td><td>${esc(a.ports)}</td><td><span class="pill">${esc(a.status)}</span></td></tr>`}).join('')||'<tr><td colspan="6">No allow rules. Default deny applies.</td></tr>';detailDevices.innerHTML=d.devices.map(x=>{let ips=[];try{ips=JSON.parse(x.ip_addresses||'[]')}catch(_){ips=[]}let status=x.online?'online':'offline';return `<tr><td>${esc(x.name)}</td><td>${esc(x.user_name||'Unknown')}</td><td>${ips.map(esc).join('<br>')||'—'}</td><td><span class="pill status-${status}">${status}</span></td><td>${formatLastSeen(x.last_seen)}</td><td>${esc(x.headscale_node_id||'Not linked')}</td></tr>`}).join('')||'<tr><td colspan="6">No devices recorded for this organization.</td></tr>'}
const renderOrganizationBase=renderOrganization;
renderOrganization=function(){
  renderOrganizationBase();
  let d=organizationState,users=d.headscale_users||[];
  document.getElementById('detail-user-count').textContent=users.length;
  document.getElementById('detail-online-count').textContent=d.devices.filter(x=>x.online).length;
  let selectedMatey=deviceMateyFilter.value;
  deviceMateyFilter.innerHTML='<option value="">All mateys</option>'+users.map(u=>`<option value="${esc(u.id)}">${esc(u.name)}</option>`).join('');
  deviceMateyFilter.value=users.some(u=>String(u.id)===selectedMatey)?selectedMatey:'';
  document.getElementById('macos-server-url').value=(d.tailnet||{}).server_url||'';
  document.getElementById('detail-summary').textContent=d.organization.mode==='isolated'
    ?'Dedicated, totally isolated tailnet'
    :'Collaborative Crew with explicit trust and ACLs';
  let mastLabel=detailTailnet.querySelector('dt');
  if(mastLabel)mastLabel.textContent='Mast';
  document.getElementById('detail-users').innerHTML=users.map(u=>{
    let owned=d.devices.filter(x=>x.user_name===u.name),online=owned.filter(x=>x.online).length;
    return `<tr><td>${esc(u.name)}</td><td>${owned.length}</td><td><span class="pill ${online?'status-online':'status-offline'}">${online}</span></td><td>${esc(u.id)}</td><td><button type="button" class="theme-toggle" data-view-matey data-id="${esc(u.id)}">Devices</button> <button class="danger" data-remove-matey data-id="${esc(u.id)}" data-name="${esc(u.name)}" data-device-count="${owned.length}">Remove</button></td></tr>`
  }).join('')||'<tr><td colspan="5">No mateys in this tailnet.</td></tr>';
  renderFilteredDevices();
}
function renderFilteredDevices(){
  let d=organizationState;if(!d)return;
  let users=d.headscale_users||[],selected=users.find(u=>String(u.id)===deviceMateyFilter.value);
  let visible=selected?d.devices.filter(x=>x.user_name===selected.name):d.devices;
  filteredDeviceCaption.textContent=selected?`Showing devices for ${selected.name} on this Mast.`:`Showing all ${d.devices.length} devices in this Crew.`;
  detailDevices.innerHTML=visible.map(x=>{
    let ips=[];try{ips=JSON.parse(x.ip_addresses||'[]')}catch(_){ips=[]}
    let status=x.online?'online':'offline';
    return `<tr><td>${esc(x.name)}</td><td>${esc(x.user_name||'Unknown')}</td><td>${ips.map(esc).join('<br>')||'—'}</td><td><span class="pill status-${status}">${status}</span></td><td>${formatLastSeen(x.last_seen)}</td><td>${esc(x.headscale_node_id||'Not linked')}</td><td><button class="danger" data-remove-device data-id="${esc(x.id)}" data-name="${esc(x.name)}" data-linked="${x.headscale_node_id?'true':'false'}">Remove</button></td></tr>`
  }).join('')||`<tr><td colspan="7">${selected?'No devices for this matey on this Mast.':'No devices recorded for this Crew.'}</td></tr>`;
}
async function run(fn){try{await fn();await load()}catch(e){error.textContent=e.message}}
async function createRegistration(){try{registrationButton.disabled=true;let result=await api('/api/organizations/'+organizationState.organization.id+'/registration','POST',{user_id:registrationUser.value});registrationCommand.value=result.command;registrationResult.hidden=false}catch(e){error.textContent=e.message}finally{registrationButton.disabled=false}}
async function createHeadscaleUser(){try{let user=await api('/api/organizations/'+organizationState.organization.id+'/users','POST',{name:newUserName.value});newUserName.value='';await load();registrationUser.value=String(user.id)}catch(e){error.textContent=e.message}}
async function removeMatey(id,name,deviceCount){
  let impact=deviceCount?` This will also revoke ${deviceCount} device${deviceCount===1?'':'s'}.`:'';
  if(!window.confirm(`Remove matey “${name}” from this Crew?${impact} Planned device shares and ACL entries will also be removed.`))return;
  try{await api('/api/organizations/'+organizationState.organization.id+'/users/'+id,'DELETE',{confirm:true,user_name:name,device_count:deviceCount});await load()}
  catch(e){error.textContent=e.message}
}
async function removeDevice(id,name,linked){
  let impact=linked?' This will revoke the device from its Mast. Planned shares and ACL entries will also be removed.':' This only removes the local inventory record.';
  if(!window.confirm(`Remove device “${name}” from this Crew?${impact}`))return;
  try{await api('/api/organizations/'+organizationState.organization.id+'/devices/'+id,'DELETE',{confirm:true,device_name:name});await load()}
  catch(e){error.textContent=e.message}
}
async function registerPendingNode(){let input=document.getElementById('pending-node-key'),result=document.getElementById('pending-node-result'),key=input.value;input.value='';try{await api('/api/organizations/'+organizationState.organization.id+'/pending-node','POST',{user_id:registrationUser.value,key});result.textContent='Device registered. It will appear in the inventory shortly.';setTimeout(()=>load(true),2000)}catch(e){result.textContent=e.message}}
async function copyRegistration(){await navigator.clipboard.writeText(registrationCommand.value)}
function setThemeLabel(){themeToggle.textContent=document.documentElement.dataset.theme==='dark'?'Light mode':'Dark mode'}function toggleTheme(){let next=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=next;localStorage.setItem('swarmsail-theme',next);setThemeLabel()}
function configureDns(){run(()=>api('/api/dns/cloudflare','POST',{}))}
function addOrg(){run(()=>api('/api/organizations','POST',{name:orgName.value,mode:orgMode.value,headscale_instance:orgInstance.value}))}function addConnection(){run(()=>api('/api/connections','POST',{organization_a_id:connectionA.value,organization_b_id:connectionB.value}))}function addDevice(){run(()=>api('/api/devices','POST',{organization_id:deviceOrg.value,name:deviceName.value,headscale_node_id:deviceNode.value}))}function addShare(){run(()=>api('/api/shares','POST',{device_id:shareDevice.value,target_organization_id:shareTarget.value,permission:sharePermission.value}))}function addAcl(){run(()=>api('/api/acls','POST',{source_organization_id:aclSource.value,destination_device_id:aclDevice.value,protocol:aclProtocol.value,ports:aclPorts.value}))}function removeShare(id){run(()=>api('/api/shares/'+id,'DELETE'))}function removeAcl(id){run(()=>api('/api/acls/'+id,'DELETE'))}
function watchChanges(){let values=new Map([...document.querySelectorAll('[data-live-value]')].map(node=>[node,node.textContent]));let observer=new MutationObserver(records=>{for(let record of records){let node=record.target.nodeType===3?record.target.parentElement:record.target,target=node&&node.closest('[data-live-value]');if(!target)continue;let next=target.textContent,previous=values.get(target);values.set(target,next);if(previous===undefined||previous===next)continue;target.classList.remove('data-changed');void target.offsetWidth;target.classList.add('data-changed');setTimeout(()=>target.classList.remove('data-changed'),2300)}});document.querySelectorAll('[data-live-value]').forEach(node=>observer.observe(node,{subtree:true,childList:true,characterData:true}))}
const sidebarCrews=document.getElementById('sidebar-crews'),deviceMateyFilter=document.getElementById('device-matey-filter'),filteredDeviceCaption=document.getElementById('filtered-device-caption'),crewDevicesSection=document.getElementById('crew-devices-section');
const error=document.getElementById('error'),refreshStatus=document.getElementById('refresh-status'),dashboardView=document.getElementById('dashboard-view'),organizationView=document.getElementById('organization-view'),themeToggle=document.getElementById('theme-toggle'),cloudflareStatus=document.getElementById('cloudflare-status'),cloudflareButton=document.getElementById('cloudflare-button'),securityPath=document.getElementById('security-path'),securityNote=document.getElementById('security-note'),orgs=document.getElementById('orgs'),connections=document.getElementById('connections'),devices=document.getElementById('devices'),shares=document.getElementById('shares'),acls=document.getElementById('acls'),sharingNote=document.getElementById('sharing-note'),deviceOrg=document.getElementById('device-org'),connectionA=document.getElementById('connection-a'),connectionB=document.getElementById('connection-b'),shareDevice=document.getElementById('share-device'),shareTarget=document.getElementById('share-target'),aclSource=document.getElementById('acl-source'),aclDevice=document.getElementById('acl-device'),aclProtocol=document.getElementById('acl-protocol'),aclPorts=document.getElementById('acl-ports'),orgInstance=document.getElementById('org-instance'),orgName=document.getElementById('org-name'),orgMode=document.getElementById('org-mode'),deviceName=document.getElementById('device-name'),deviceNode=document.getElementById('device-node'),sharePermission=document.getElementById('share-permission'),registrationUser=document.getElementById('registration-user'),registrationButton=document.getElementById('registration-button'),registrationResult=document.getElementById('registration-result'),registrationCommand=document.getElementById('registration-command'),newUserName=document.getElementById('new-user-name'),detailName=document.getElementById('detail-name'),detailSummary=document.getElementById('detail-summary'),detailMode=document.getElementById('detail-mode'),detailDeviceCount=document.getElementById('detail-device-count'),detailAclCount=document.getElementById('detail-acl-count'),detailConnectionCount=document.getElementById('detail-connection-count'),detailShareCount=document.getElementById('detail-share-count'),detailTailnet=document.getElementById('detail-tailnet'),detailDns=document.getElementById('detail-dns'),detailSplitDns=document.getElementById('detail-split-dns'),detailTrust=document.getElementById('detail-trust'),detailPolicyStatus=document.getElementById('detail-policy-status'),detailAcls=document.getElementById('detail-acls'),detailDevices=document.getElementById('detail-devices');setThemeLabel();load().then(watchChanges);setInterval(refreshIfIdle,10000);document.addEventListener('visibilitychange',refreshIfIdle);window.addEventListener('focus',refreshIfIdle);window.addEventListener('hashchange',showDashboardPage);deviceMateyFilter.addEventListener('change',renderFilteredDevices);
document.getElementById('detail-users').addEventListener('click',event=>{
  let viewButton=event.target.closest('[data-view-matey]');
  if(viewButton){deviceMateyFilter.value=viewButton.dataset.id;renderFilteredDevices();crewDevicesSection.scrollIntoView({block:'start'});return}
  let button=event.target.closest('[data-remove-matey]');
  if(button)removeMatey(button.dataset.id,button.dataset.name,Number(button.dataset.deviceCount||0));
});
detailDevices.addEventListener('click',event=>{
  let button=event.target.closest('[data-remove-device]');
  if(button)removeDevice(button.dataset.id,button.dataset.name,button.dataset.linked==='true');
});
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Run the local Swarmsail web control plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--database", default="swarmsail.db")
    parser.add_argument("--config", default="instances.json")
    parser.add_argument("--node-inventory")
    parser.add_argument("--allow-generation", action="store_true")
    args = parser.parse_args()
    server = AppServer(
        (args.host, args.port), Handler, Store(args.database), args.config,
        allow_generation=args.allow_generation, node_inventory=args.node_inventory,
    )
    print(f"Swarmsail is available at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
