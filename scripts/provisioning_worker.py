"""Fixed node-local adapter for the private v1 ledger; no DNS provider access."""

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from manage_headscale import generate_caddyfile, generate_compose, validate_config
from scripts.provision_subscriber import allocate, write_config_atomically
from swarmsail_web import Store

IMAGE = "headscale/headscale:0.29.5"
IMAGE_DIGEST = "headscale/headscale@sha256:780ed2f9e85719a2211277433f2c8afb45105d0d25031949a6848a485f9e216d"
TAG = "tag:account-node"
# Filled only when the corresponding Rawrarr listener/trust fixture is agreed.
PEER_PROFILE = None
PEER_PORT = None


class Failure(Exception):
    def __init__(self, code, phase="headscale"):
        self.code, self.phase = code, phase


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def active_route_matches(value, hostname, upstream, hosts=()):
    if isinstance(value, list):
        return any(active_route_matches(item, hostname, upstream, hosts) for item in value)
    if not isinstance(value, dict):
        return False
    if "match" in value and any("host" in match for match in value["match"]):
        hosts = tuple(host for match in value["match"] for host in match.get("host", []))
    if value.get("handler") == "reverse_proxy" and hostname in hosts:
        return value.get("upstreams") == [{"dial": upstream}]
    return any(active_route_matches(item, hostname, upstream, hosts)
               for item in value.values() if isinstance(item, (list, dict)))


def private_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise Failure("private_file_permissions", "configuration")
    return json.loads(path.read_text())


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        name = Path(stream.name)
        os.chmod(name, 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        name.unlink(missing_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Worker:
    def __init__(self, settings, ledger):
        expected = {
            "host_id", "config", "mapping_database", "data_root", "compose_project",
            "public_ipv4", "ingress_container", "ingress_file", "ingress_container_file",
            "ingress_admin_socket", "ingress_baseline_sha256",
            "ingress_tls_certificate", "ingress_tls_key",
        }
        if set(settings) != expected or settings["host_id"] != "do3":
            raise Failure("invalid_worker_settings", "configuration")
        for field in ("config", "mapping_database", "data_root", "ingress_file"):
            if not Path(settings[field]).is_absolute():
                raise Failure("absolute_host_paths_required", "configuration")
        for field in ("ingress_tls_certificate", "ingress_tls_key", "ingress_container_file", "ingress_admin_socket"):
            if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,250}", settings[field]):
                raise Failure("invalid_ingress_path", "configuration")
        for field in ("compose_project", "ingress_container"):
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", settings[field]):
                raise Failure("invalid_host_resource_name", "configuration")
        if not ipaddress.ip_address(settings["public_ipv4"]).is_global:
            raise Failure("global_public_ipv4_required", "dns")
        if not re.fullmatch(r"[a-f0-9]{64}", settings["ingress_baseline_sha256"]):
            raise Failure("ingress_baseline_required", "configuration")
        self.s = settings
        self.ledger = Path(ledger)

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.ledger, timeout=2)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("SELECT version FROM provisioning_version").fetchall() != [(1,)]:
                raise Failure("ledger_version_unsupported", "allocation")
            yield db

    @contextlib.contextmanager
    def config_lock(self):
        path = Path(self.s["config"])
        with path.with_name(f".{path.name}.lock").open("a+") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield path

    def command(self, *args, input=None):
        try:
            result = subprocess.run(
                args, input=input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=45, check=True, text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Failure("host_command_outcome_unknown") from exc
        if len(result.stdout) > 65536:
            raise Failure("host_command_output_invalid")
        return result.stdout

    def headscale(self, spec, *args):
        try:
            output = self.command(
                "docker", "exec", spec["service_name"], "headscale",
                *args, "--output", "json",
            )
            if args[:2] in (("policy", "set"), ("preauthkeys", "expire")):
                # These pinned CLI mutations produce no JSON receipt; callers
                # must verify their effect with policy get/key list afterward.
                return None
            return json.loads(output)
        except ValueError as exc:
            raise Failure("headscale_response_invalid") from exc

    def spec(self, account):
        with self.db() as db:
            row = db.execute(
                "SELECT spec,tailnet FROM account_defaults WHERE account=?", (account,)
            ).fetchone()
        if not row or not row[0]:
            raise Failure("allocation_not_materialized", "allocation")
        spec = json.loads(row[0])
        self.validate_spec(spec, account, row[1], private_json(self.s["config"]))
        return spec

    def validate_spec(self, spec, account, tailnet, config):
        if (spec.get("subscriber_account_id") != account
                or spec.get("provisioning_tailnet_id") != tailnet
                or str(uuid.UUID(tailnet)) != tailnet
                or spec.get("image") != IMAGE or spec.get("image_digest") != IMAGE_DIGEST):
            raise Failure("worker_binding_or_image_drift", "allocation")
        matching = [i for i in config["headscale"]["instances"] if i["id"] == spec["id"]]
        if matching:
            if matching != [spec]:
                raise Failure("instance_config_drift", "configuration")
            return
        # The API can write intents, but never choose arbitrary privileged paths/images.
        instance_id = spec["id"]
        if not isinstance(instance_id, int) or isinstance(instance_id, bool) or instance_id < 1:
            raise Failure("invalid_worker_allocation", "allocation")
        root = Path(self.s["data_root"]) / str(instance_id)
        expected = {
            "management_mode": "managed", "organization_mode": "isolated",
            "service_name": f"headscale-swarmsail-{instance_id}",
            "server_url": f"https://{tailnet}.swarm.rawrarr.net",
            "base_domain": f"{tailnet}.tailnet.internal",
            "config_path": str(root / "config"), "data_path": str(root / "data"),
            "publish_web_port": False, "stun_port_udp": False, "subscriber_generation": 1,
        }
        if any(spec.get(key) != value for key, value in expected.items()):
            raise Failure("invalid_worker_allocation", "allocation")

    def receipt(self, resource, value=None):
        with self.db() as db:
            if value is not None:
                db.execute("INSERT INTO worker_receipts(resource,value) VALUES(?,?) "
                           "ON CONFLICT(resource) DO UPDATE SET value=excluded.value",
                           (resource, value))
            row = db.execute("SELECT value FROM worker_receipts WHERE resource=?",
                             (resource,)).fetchone()
        return row[0] if row else None

    def phase(self, account, phase):
        with self.db() as db:
            db.execute(
                "UPDATE account_defaults SET document=json_set(document,'$.phase',?,'$.state','provisioning') "
                "WHERE account=?", (phase, account),
            )

    def reserve(self, request):
        with self.config_lock() as path:
            config = private_json(path)
            validate_config(config)
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT tailnet,spec FROM account_defaults WHERE account=?",
                    (request["account_id"],),
                ).fetchone()
                if not row or row[0] != request["tailnet_id"]:
                    raise Failure("default_binding_conflict", "allocation")
                if row[1]:
                    spec = json.loads(row[1])
                else:
                    existing = [i for i in config["headscale"]["instances"]
                                if i.get("subscriber_account_id") == request["account_id"]]
                    if existing:
                        raise Failure("default_adoption_required", "allocation")
                    # Include unmaterialized ledger reservations when calculating IDs/ranges.
                    all_config = json.loads(json.dumps(config))
                    for (reserved,) in db.execute("SELECT spec FROM account_defaults WHERE spec IS NOT NULL"):
                        candidate = json.loads(reserved)
                        if not any(i["id"] == candidate["id"] for i in all_config["headscale"]["instances"]):
                            all_config["headscale"]["instances"].append(candidate)
                    _, spec, _ = allocate(
                        all_config, request["account_id"], request["server_url"],
                        f'{request["tailnet_id"]}.tailnet.internal', self.s["data_root"],
                    )
                    spec["provisioning_tailnet_id"] = request["tailnet_id"]
                    spec["image"] = IMAGE
                    spec["image_digest"] = IMAGE_DIGEST
                    db.execute("UPDATE account_defaults SET spec=? WHERE account=?",
                               (canonical(spec), request["account_id"]))
                # Commit desired identity before projecting config, including unknown outcomes.
            self.validate_spec(spec, request["account_id"], request["tailnet_id"], config)
            matches = [i for i in config["headscale"]["instances"] if i["id"] == spec["id"]]
            if matches and matches != [spec]:
                raise Failure("instance_config_drift", "configuration")
            if not matches:
                config["headscale"]["instances"].append(spec)
                validate_config(config)
                write_config_atomically(path, config)
            return spec, config

    def render(self, spec):
        template = Path(__file__).resolve().parents[1] / "headscale-config.template.yaml"
        content = template.read_text()
        for key, value in (
            ("__SERVER_URL__", spec["server_url"]),
            ("__BASE_DOMAIN__", spec["base_domain"]),
            ("__IPV4_PREFIX__", spec["ipv4_prefix"]),
        ):
            content = content.replace(key, value)
        return content

    def container(self, spec, config):
        name = spec["service_name"]
        path = Path(spec["config_path"]) / "config.yaml"
        expected = self.render(spec)
        if path.exists() and path.read_text() != expected:
            raise Failure("headscale_config_drift", "configuration")
        if not path.exists():
            atomic_text(path, expected)
        Path(spec["data_path"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        target = json.loads(json.dumps(config))
        target["headscale"]["instances"] = [spec]
        target["caddy"] = {"enabled": False}
        target["npm"] = {"enabled": False}
        target["crowdsec"] = {"enabled": False}
        compose = generate_compose(target)
        compose["services"][name]["labels"] = {
            "swarmsail.tailnet": spec["provisioning_tailnet_id"],
            "swarmsail.config": digest(expected),
        }
        compose_path = Path(self.s["data_root"]) / spec["provisioning_tailnet_id"] / "compose.json"
        atomic_text(compose_path, canonical(compose))
        # Do not adopt/recreate a container unless its immutable ownership matches.
        listed = self.command("docker", "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.ID}}").strip()
        if listed:
            found = json.loads(self.command("docker", "inspect", listed))[0]
            labels = found["Config"].get("Labels") or {}
            if (labels.get("swarmsail.tailnet") != spec["provisioning_tailnet_id"]
                    or labels.get("swarmsail.config") != digest(expected)
                    or found["Config"]["Image"] != IMAGE_DIGEST):
                raise Failure("container_ownership_or_config_drift", "container")
        self.command("docker", "compose", "--project-name", self.s["compose_project"],
                     "--file", str(compose_path), "config", "--quiet")
        self.command("docker", "compose", "--project-name", self.s["compose_project"],
                     "--file", str(compose_path), "up", "--detach", "--no-deps", name)
        found = json.loads(self.command("docker", "inspect", name))[0]
        if not found["State"]["Running"]:
            raise Failure("container_not_running", "container")
        expected_mounts = {
            "/etc/headscale": str(Path(spec["config_path"]).resolve()),
            "/var/lib/headscale": str(Path(spec["data_path"]).resolve()),
        }
        mounts = {m["Destination"]: m["Source"] for m in found["Mounts"]}
        if any(mounts.get(k) != v for k, v in expected_mounts.items()):
            raise Failure("container_mount_drift", "container")
        self.headscale(spec, "users", "list")

    def dns(self, spec):
        hostname = spec["server_url"].removeprefix("https://")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
            wildcard = {item[4][0] for item in socket.getaddrinfo(
                f'probe-{spec["provisioning_tailnet_id"]}.swarm.rawrarr.net',
                443, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise Failure("wildcard_dns_not_ready", "dns") from exc
        if addresses != {self.s["public_ipv4"]} or wildcard != addresses:
            raise Failure("wildcard_dns_not_ready", "dns")
        for record in ("CNAME", "AAAA"):
            if self.command("dig", "+short", hostname, record).strip():
                raise Failure("wildcard_dns_route_conflict", "dns")

    def ingress(self, spec):
        if self.receipt("ingress-fence") == "unknown":
            raise Failure("ingress_outcome_requires_reconciliation", "tls")
        path = Path(self.s["ingress_file"])
        current = path.read_text()
        marker = "# SwarmSail private-v1 routes\n"
        baseline = current.split(marker)[0]
        if digest(baseline) != self.s["ingress_baseline_sha256"]:
            raise Failure("ingress_baseline_drift", "configuration")
        with self.db() as db:
            rows = db.execute("SELECT spec FROM account_defaults WHERE spec IS NOT NULL").fetchall()
        routes = []
        config = private_json(self.s["config"])
        for (value,) in rows:
            item = json.loads(value)
            target = json.loads(json.dumps(config))
            target["headscale"]["instances"] = [item]
            target.setdefault("caddy", {})["proxy_routes"] = []
            _, _, route = generate_caddyfile(target).partition("\n\n")
            route = route.replace(
                "{\n", f'{{\n    tls {self.s["ingress_tls_certificate"]} {self.s["ingress_tls_key"]}\n', 1)
            routes.append(route)
        expected = baseline + marker + "\n".join(routes)
        previous_digest = self.receipt("ingress")
        if marker in current and current != expected and digest(current) != previous_digest:
            raise Failure("ingress_managed_route_drift", "configuration")
        # Baseline owns TLS (externally installed wildcard certificate/renewal).
        # Never supply provider credentials or alter global/operator routes.
        stage = path.with_name(f".{path.name}.candidate")
        atomic_text(stage, expected)
        previous = current
        try:
            atomic_text(path, expected)
            mounted = self.command("docker", "exec", self.s["ingress_container"],
                                   "cat", self.s["ingress_container_file"])
            if mounted != expected:
                raise Failure("ingress_mount_not_reloadable", "configuration")
            self.command("docker", "exec", self.s["ingress_container"], "caddy", "validate",
                         "--config", self.s["ingress_container_file"], "--adapter", "caddyfile")
            native = json.loads(self.command(
                "docker", "exec", self.s["ingress_container"], "caddy", "adapt",
                "--config", self.s["ingress_container_file"], "--adapter", "caddyfile",
            ))
            self.command("docker", "exec", self.s["ingress_container"], "caddy", "reload",
                         "--config", self.s["ingress_container_file"], "--adapter", "caddyfile",
                         "--address", f'unix/{self.s["ingress_admin_socket"]}')
            # Read active config over the local admin socket, not just disk.
            active = json.loads(self.command(
                "docker", "exec", self.s["ingress_container"], "curl", "--fail",
                "--silent", "--unix-socket", self.s["ingress_admin_socket"],
                "http://localhost/config/",
            ))
            if active != native or not active_route_matches(
                active, spec["server_url"].removeprefix("https://"),
                f'{spec["service_name"]}:8080',
            ):
                raise Failure("ingress_active_route_mismatch", "tls")
        except Failure as exc:
            if exc.code == "host_command_outcome_unknown":
                self.receipt("ingress-fence", "unknown")
            atomic_text(path, previous)
            try:
                self.command("docker", "exec", self.s["ingress_container"], "caddy", "reload",
                             "--config", self.s["ingress_container_file"], "--adapter", "caddyfile",
                             "--address", f'unix/{self.s["ingress_admin_socket"]}')
                previous_native = json.loads(self.command(
                    "docker", "exec", self.s["ingress_container"], "caddy", "adapt",
                    "--config", self.s["ingress_container_file"], "--adapter", "caddyfile"))
                restored = json.loads(self.command(
                    "docker", "exec", self.s["ingress_container"], "curl", "--fail", "--silent",
                    "--unix-socket", self.s["ingress_admin_socket"], "http://localhost/config/"))
                if restored != previous_native:
                    raise Failure("ingress_restore_not_verified", "tls")
            except Failure as rollback:
                self.receipt("ingress-fence", "unknown")
                raise Failure("ingress_restore_outcome_unknown", "tls") from rollback
            raise Failure("ingress_reload_or_verification_failed", "tls") from exc
        finally:
            stage.unlink(missing_ok=True)
        self.receipt("ingress", digest(expected))

    def policy(self, spec):
        # Versioned profile, not user-supplied ACLs or an arbitrary ports list.
        if PEER_PROFILE is None or PEER_PORT is None:
            raise Failure("peer_profile_unavailable")
        policy = {
            "tagOwners": {TAG: ["swarmsail-enroller@"]},
            "acls": [
                {"action": "accept", "src": [TAG], "dst": [f"{TAG}:{PEER_PORT}"], "proto": "tcp"},
            ],
        }
        path = Path(spec["config_path"]) / "account-policy.json"
        if path.exists() and json.loads(path.read_text()) != policy:
            raise Failure("account_policy_drift")
        users = self.headscale(spec, "users", "list") or []
        if not any(u.get("name") == "swarmsail-enroller" for u in users):
            self.headscale(spec, "users", "create", "swarmsail-enroller")
        resource = "policy/" + spec["provisioning_tailnet_id"]
        if path.exists() and self.receipt(resource) == "installed":
            actual = self.headscale(spec, "policy", "get")
            if isinstance(actual, str):
                actual = json.loads(actual)
            if actual != policy:
                raise Failure("account_policy_drift")
            return
        if not path.exists():
            atomic_text(path, canonical(policy))
        self.headscale(spec, "policy", "set", "--file", "/etc/headscale/account-policy.json")
        actual = self.headscale(spec, "policy", "get")
        if isinstance(actual, str):
            actual = json.loads(actual)
        if actual != policy:
            raise Failure("account_policy_not_verified")
        self.receipt(resource, "installed")

    def https(self, spec):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        try:
            with opener.open(f'{spec["server_url"]}/health', timeout=5) as response:
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536 or json.loads(body).get("status") != "pass":
                    raise Failure("headscale_public_not_ready")
        except (OSError, ValueError) as exc:
            raise Failure("tls_or_public_headscale_not_ready", "tls") from exc

    def ensure(self, request):
        self.phase(request["account_id"], "allocation")
        spec, config = self.reserve(request)
        self.phase(request["account_id"], "container")
        self.container(spec, config)
        self.phase(request["account_id"], "dns")
        self.dns(spec)
        self.phase(request["account_id"], "tls")
        with self.config_lock():
            self.ingress(spec)
        self.phase(request["account_id"], "headscale")
        self.policy(spec)
        self.https(spec)
        self.phase(request["account_id"], "mapping")
        Store(self.s["mapping_database"]).link_subscriber_tailnet(
            request["account_id"], spec["id"], config["headscale"]["instances"]
        )
        return {"ok": True, "phase": "complete"}

    def attempt(self, request, create):
        spec = self.spec(request["account_id"])
        if spec["provisioning_tailnet_id"] != request["tailnet_id"]:
            raise Failure("enrollment_binding_conflict")
        username = "enroll-" + request["enrollment_id"].replace("-", "")
        users = self.headscale(spec, "users", "list") or []
        if not isinstance(users, list):
            raise Failure("enrollment_uncertain")
        matching = [u for u in users if u.get("name") == username]
        if not matching and create:
            self.headscale(spec, "users", "create", username)
            users = self.headscale(spec, "users", "list") or []
            matching = [u for u in users if u.get("name") == username]
        if len(matching) != 1:
            raise Failure("enrollment_uncertain")
        user_id = matching[0]["id"]
        if isinstance(user_id, bool) or not str(user_id).isdigit():
            raise Failure("enrollment_uncertain")
        keys = self.list_keys(spec, str(user_id))
        return spec, str(user_id), keys

    def list_keys(self, spec, user_id):
        keys = self.headscale(spec, "preauthkeys", "list") or []
        if not isinstance(keys, list):
            raise Failure("enrollment_uncertain")
        for key in keys:
            if not isinstance(key.get("user"), dict) or not str(key["user"].get("id")).isdigit():
                raise Failure("headscale_key_owner_invalid")
        return [key for key in keys if str(key["user"]["id"]) == user_id]

    def time(self, value):
        if isinstance(value, dict):
            return datetime.fromtimestamp(int(value["seconds"]), timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def key_state(self, key):
        if key.get("reusable", False) is not False or key.get("ephemeral", False) is not False:
            raise Failure("enrollment_uncertain")
        if key.get("used", False):
            return "consumed"
        if self.time(key["expiration"]) <= datetime.now(timezone.utc):
            return "expired"
        return "issued"

    def enrollment(self, request):
        spec, user_id, keys = self.attempt(request, True)
        original = request.get("receipt_key")
        created = None
        if not keys:
            # Durable marker disallows blindly reissuing after a creation timeout.
            with self.db() as db:
                row = db.execute("SELECT document FROM enrollments WHERE id=?",
                                 (request["enrollment_id"],)).fetchone()
                document = json.loads(row[0])
                if document.get("creation_started"):
                    raise Failure("enrollment_uncertain")
                document["creation_started"] = True
                db.execute("UPDATE enrollments SET document=? WHERE id=?",
                           (canonical(document), request["enrollment_id"]))
            created = self.headscale(spec, "preauthkeys", "create", "--user", user_id,
                                    "--expiration", "15m", "--tags", TAG)
            original = created.get("key")
            keys = self.list_keys(spec, user_id)
        if len(keys) != 1:
            raise Failure("enrollment_uncertain")
        key = keys[0]
        if created and created["id"] != key["id"]:
            raise Failure("enrollment_uncertain")
        if request.get("headscale_key_id") is not None and request["headscale_key_id"] != key["id"]:
            raise Failure("enrollment_uncertain")
        state = self.key_state(key)
        if state != "issued":
            return {"ok": True, "state": state, "error": None}
        expiry = self.time(key["expiration"])
        if not 0 < (expiry - datetime.now(timezone.utc)).total_seconds() <= 905:
            raise Failure("enrollment_uncertain")
        tags = key.get("aclTags", key.get("acl_tags"))
        # Headscale 0.29 lists only an abbreviated identifier, not the secret.
        # Only the fresh creation receipt or the previously encrypted original is usable.
        if (tags != [TAG] or not isinstance(original, str)
                or not re.fullmatch(r"hskey-auth-[A-Za-z0-9_-]{77,512}", original)):
            raise Failure("enrollment_uncertain")
        return {"ok": True, "state": "issued", "error": None,
                "auth_key": original, "key_id": key["id"],
                "expires_at": expiry.isoformat().replace("+00:00", "Z")}

    def revoke(self, request):
        spec, user_id, keys = self.attempt(request, False)
        if not keys:
            # Only a persisted creation marker could hide an unknown live key.
            with self.db() as db:
                d = json.loads(db.execute("SELECT document FROM enrollments WHERE id=?",
                                          (request["enrollment_id"],)).fetchone()[0])
            if d.get("creation_started"):
                raise Failure("revocation_uncertain")
            return {"ok": True, "state": "revoked"}
        for key in keys:
            if self.key_state(key) == "issued":
                self.headscale(spec, "preauthkeys", "expire", "--id", str(key["id"]))
        keys = self.list_keys(spec, user_id)
        if any(self.key_state(key) == "issued" for key in keys):
            raise Failure("revocation_uncertain")
        return {"ok": True, "state": "revoked"}

    def mapping(self, request):
        spec = self.spec(request["account_id"])
        with self.db() as db:
            documents = [json.loads(row[0]) for row in db.execute(
                "SELECT document FROM enrollments WHERE account=? AND node=?",
                (request["account_id"], request["node_id"]),
            )]
            previous = db.execute("SELECT document FROM node_mappings WHERE account=? AND node=?",
                                  (request["account_id"], request["node_id"])).fetchone()
        nodes = self.headscale(spec, "nodes", "list") or []
        users = self.headscale(spec, "users", "list") or []
        if not isinstance(nodes, list) or not isinstance(users, list):
            raise Failure("node_mapping_uncertain")
        candidates = []
        recovery_ids = {d["enrollment_id"] for d in documents if d.get("recovery")}
        fenced_ids = {d["recovery"]["predecessor_enrollment_id"]
                      for d in documents if d.get("recovery")}
        consumed = bool(previous and json.loads(previous[0]).get("state") in ("active", "removed"))
        for document in documents:
            username = "enroll-" + document["enrollment_id"].replace("-", "")
            matching = [u for u in users if u.get("name") == username]
            if len(matching) > 1:
                raise Failure("node_mapping_uncertain")
            if not matching:
                continue
            user_id = str(matching[0]["id"])
            keys = self.list_keys(spec, user_id)
            if len(keys) > 1:
                raise Failure("node_mapping_uncertain")
            used = bool(keys and keys[0].get("used", False))
            consumed = consumed or used or document.get("state") == "consumed"
            for item in nodes:
                # Tagged nodes have a synthetic user. The pinned CLI retains
                # the actual enrollment association in pre_auth_key.
                association = item.get("pre_auth_key", item.get("preAuthKey"))
                if not isinstance(association, dict):
                    if (str((item.get("user") or {}).get("id")) == user_id
                            or TAG in item.get("tags", [])):
                        raise Failure("node_mapping_uncertain")
                    continue
                if str((association.get("user") or {}).get("id")) != user_id:
                    continue
                if document["enrollment_id"] in fenced_ids and not request.get("recovery_safety_check"):
                    raise Failure("node_mapping_uncertain")
                if (not used or len(keys) != 1
                        or str(association.get("id")) != str(keys[0]["id"])
                        or association.get("used") is not True):
                    raise Failure("node_mapping_uncertain")
                public_key = item.get("nodeKey", item.get("node_key"))
                addresses = item.get("ipAddresses", item.get("ip_addresses", []))
                ipv4 = [ipaddress.ip_address(a) for a in addresses
                        if isinstance(a, str) and ipaddress.ip_address(a).version == 4]
                if (len(ipv4) != 1 or ipv4[0] not in ipaddress.ip_network(spec["ipv4_prefix"])
                        or not isinstance(public_key, str)
                        or not re.fullmatch(r"nodekey:[a-f0-9]{64}", public_key)
                        or isinstance(item.get("id"), bool) or not str(item.get("id")).isdigit()):
                    raise Failure("node_mapping_uncertain")
                candidates.append({
                    "device": {"id": str(item["id"]), "public_key": public_key, "ipv4": str(ipv4[0])},
                    "enrollment_id": document["enrollment_id"],
                    "setup_intent_id": document["setup_intent_id"],
                })
        if len(candidates) > 1:
            raise Failure("node_mapping_uncertain")
        if candidates:
            return {"ok": True, "state": "active", "error": None, **candidates[0]}
        return {"ok": True, "state": "removed" if consumed and not recovery_ids else "pending", "device": None,
                "enrollment_id": None, "setup_intent_id": None, "error": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ensure", "enrollment", "revoke", "mapping"))
    parser.add_argument("--settings", required=True)
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        request = json.loads(sys.stdin.read(8193))
        worker = Worker(private_json(args.settings), args.ledger)
        result = getattr(worker, args.action)(request)
    except Failure as exc:
        result = {"ok": False, "error": {"code": exc.code, "retryable": False},
                  "code": exc.code, "phase": exc.phase, "state": "uncertain"}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # This adapter's stdout may contain a key: never echo it or dump traceback.
        print(f"worker adapter failure: {type(exc).__name__}", file=sys.stderr)
        result = {"ok": False, "error": {"code": "worker_state_unavailable", "retryable": False},
                  "code": "worker_state_unavailable", "phase": "configuration",
                  "state": "uncertain"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
