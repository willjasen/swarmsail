"""Isolated API/worker acceptance; test adapters never approve a runtime profile."""

import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import sqlite3
import subprocess
import time
import unittest
import urllib.error
import urllib.request
import uuid

from scripts.adopt_default_tailnet import adopt
from scripts.provisioning_worker import IMAGE, IMAGE_DIGEST, canonical
from scripts.tests.test_full_stack import ENABLED, ROOT
from scripts.tests.test_private_provisioning_e2e import PrivateProvisioningTests
from types import SimpleNamespace


@unittest.skipUnless(ENABLED, "set SWARMSAIL_RUN_E2E=1 for disposable Docker tests")
class PrivateLifecycleTests(PrivateProvisioningTests):
    test_real_single_use_recovery_revocation_and_candidate_policy = None

    def execute(self, service, *args, **kwargs):
        if service == "headscale-test" and hasattr(self, "spec"):
            service = self.spec["service_name"]
        return super().execute(service, *args, **kwargs)

    def cleanup_stack(self):
        for process in getattr(self, "processes", []):
            self.stop(process)
        super().cleanup_stack()

    def trust(self):
        if hasattr(self, "integration_baseline"):
            self.wait_for("Headscale", lambda: self.execute(
                "headscale-test", "headscale", "users", "list", "--output", "json"))
            return
        super().trust()
        for client in ("client-a", "client-b"):
            self.compose("cp", str(self.directory / "caddy/fixture.crt"),
                         f"{client}:/usr/local/share/ca-certificates/fixture.crt")
            self.execute(client, "update-ca-certificates")

    def stop(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)

    def private_file(self, name, content):
        path = self.directory / name
        path.write_text(content)
        path.chmod(0o600)
        return path

    def jwt(self, recovery=None, request_id=None, lookup=None):
        encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode()
        timestamp = int(time.time())
        claims = {
            "iss": "https://rawrarr.example.test", "aud": "swarmsail-provisioning-v1",
            "azp": "rawrarr-test", "sub": self.account,
            "scope": ["tailnet:ensure", "tailnet:read", "node:enroll", "node:read"],
            "iat": timestamp, "nbf": timestamp, "exp": timestamp + 300,
            "jti": str(uuid.uuid4()), "node_id": self.node, "setup_intent_id": self.intent,
        }
        if recovery is not None:
            claims["aud"] = "swarmsail-enrollment-recovery-v1"
            claims["scope"] = ["node:recover"]
            claims["recovery"] = recovery
            claims["recovery_request_id"] = request_id
            claims["node_id"] = recovery["node_id"]
            claims["setup_intent_id"] = recovery["setup_intent_id"]
        if lookup is not None:
            claims["aud"] = "swarmsail-enrollment-recovery-v1"
            claims["scope"] = ["node:recover:lookup"]
            claims["recovery_lookup"] = {
                "version": "1", "original_request_id": lookup, "node_id": self.node,
                "setup_intent_id": self.intent, "owner_authorization_id": str(uuid.uuid4()),
            }
        payload = encode(json.dumps({"alg": "EdDSA", "kid": "test-only"}).encode())
        payload += "." + encode(json.dumps(claims).encode())
        signed = self.private_file("jwt-input-" + uuid.uuid4().hex, payload)
        signature = subprocess.run([
            "openssl", "pkeyutl", "-sign", "-rawin", "-inkey",
            str(ROOT / "tests/fixtures/provisioning-v1/private-test-only.pem"),
            "-in", str(signed),
        ], capture_output=True, check=True).stdout
        return payload + "." + encode(signature)

    def api(self, path="", body=None, key=None):
        key = key or str(uuid.uuid4())
        headers = {"Authorization": "Bearer " + self.jwt(
            body if path.endswith(":recover") else None, key,
            path.rsplit("/", 1)[-1] if path.startswith("/enrollment-requests/") else None)}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.origin + "/private/v1/accounts/" + self.account + "/default-tailnet" + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        except urllib.error.URLError:
            return 0, {}
        with response:
            return response.status, json.load(response)

    def launch_worker(self):
        process = subprocess.Popen(
            [str(self.binary), "worker", str(self.worker_process_settings)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(self.stop, process)
        self.processes.append(process)
        return process

    def ready(self):
        status, document = self.api()
        if status == 200 and document.get("state") == "ready":
            return True
        command = self.directory / "failed-command.json"
        raise AssertionError(canonical(document) + (command.read_text()[-700:] if command.exists() else "")
                             + self.directory.joinpath("caddy/Caddyfile").read_text())

    def receipt(self, path, body, key, expected=(200, 201)):
        status, document = self.api(path, body, key)
        if status in expected:
            return document
        raise AssertionError(canonical({
            "status": status, "state": document.get("state"),
            "error": document.get("error"), "enrollment_id": document.get("enrollment_id"),
        }))

    def quiescent(self):
        for container, binary, action in (
                (self.ingress, "caddy", "run"),
                (self.spec["service_name"], "headscale", "serve")):
            processes = self.command("docker", "top", container, "-eo", "pid,args").splitlines()[1:]
            # Engine ExecIDs can retain exited commands; inspect the actual process tree.
            if len(processes) != 1:
                return False
            fields = processes[0].split()[1:]
            if len(fields) < 2 or Path(fields[0]).name != binary or fields[1] != action:
                return False
        return True

    def write_stack(self):
        self.directory = self.directory.resolve()
        super().write_stack()
        self.account = "11111111-1111-4111-8111-111111111111"
        self.processes = []
        self.node = "fossil-0123456789abcdef01234567"
        self.intent = str(uuid.uuid4())
        self.binary = ROOT / "target/debug/swarmsail-provisioner"
        if not self.binary.exists():
            self.fail("build the swarmsail-provisioner debug binary before this fixture")
        ledger = self.private_file("ledger.db", "")
        self.ledger = ledger
        receipt_key = self.directory / "receipt.key"
        receipt_key.write_bytes(os.urandom(32))
        receipt_key.chmod(0o600)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = {
            "ledger": str(ledger), "issuer": "https://rawrarr.example.test",
            "service": "rawrarr-test", "listen": f"127.0.0.1:{port}",
            "public_keys": {
                kid: str(ROOT / "tests/fixtures/provisioning-v1/public.pem")
                for kid in ("test-only", "rfc8032-test-only")
            },
            "receipt_key_file": str(receipt_key), "tenant_capacity": 10,
        }
        api_settings = self.private_file("api.json", json.dumps(settings))
        self.origin = f"http://127.0.0.1:{port}"
        api_process = subprocess.Popen(
            [str(self.binary), "api", str(api_settings)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(self.stop, api_process)
        self.processes.append(api_process)
        self.wait_for("private API", lambda: self.api()[0] == 404, timeout=20)
        status, document = self.api(":ensure", {})
        self.assertEqual(status, 202)
        self.tailnet = document["tailnet_id"]
        config = json.loads(self.directory.joinpath("instances.json").read_text())
        config["crowdsec"] = {"enabled": False}
        config["caddy"]["listen_ip"] = "0.0.0.0"
        spec = config["headscale"]["instances"][0]
        spec.update(
            management_mode="managed", organization_mode="isolated",
            subscriber_account_id=self.account, subscriber_generation=1,
            image=IMAGE, image_digest=IMAGE_DIGEST,
            data_path=str(self.directory / "headscale-data"),
            service_name=self.project + "-headscale",
        )
        if hasattr(self, "integration_baseline"):
            spec["server_url"] = f"https://{self.tailnet}.swarm.rawrarr.net"
        self.directory.joinpath("headscale-data").mkdir()
        self.spec = spec
        self.config = self.private_file("instances.json", json.dumps(config))
        stack = json.loads(self.compose_path.read_text())
        headscale = stack["services"].pop("headscale-test")
        stack["services"][spec["service_name"]] = headscale
        headscale["container_name"] = spec["service_name"]
        headscale["restart"] = "unless-stopped"
        headscale["image"] = IMAGE_DIGEST
        headscale["volumes"] = [
            f"{spec['config_path']}:/etc/headscale",
            f"{spec['data_path']}:/var/lib/headscale",
        ]
        expected = self.directory.joinpath("headscale/config.yaml").read_text()
        if hasattr(self, "integration_baseline"):
            expected = expected.replace("https://headscale.test", spec["server_url"])
            self.directory.joinpath("headscale/config.yaml").write_text(expected)
        headscale["labels"] = {
            "swarmsail.tailnet": self.tailnet,
            "swarmsail.config": hashlib.sha256(expected.encode()).hexdigest(),
        }
        self.private_file("fixture-headscale.yaml", expected)
        dockerfile = self.private_file(
            "Dockerfile.caddy", "FROM caddy:2.11.7-alpine\nRUN apk add --no-cache curl\n")
        caddy = stack["services"]["caddy"]
        caddy["container_name"] = self.project + "-ingress"
        caddy.pop("image")
        caddy["build"] = {"context": str(self.directory), "dockerfile": dockerfile.name}
        caddy["volumes"][0] = f"{self.directory / 'caddy'}:/etc/caddy"
        caddy["networks"]["backend"]["aliases"].extend(
            [f"probe-{self.tailnet}.swarm.rawrarr.net"])
        hostname = spec["server_url"].removeprefix("https://")
        if hasattr(self, "integration_baseline"):
            for network in caddy["networks"].values():
                network["aliases"].append(hostname)
        caddyfile = self.directory / "caddy/Caddyfile"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(caddyfile.parent / "fixture.key"),
            "-out", str(caddyfile.parent / "fixture.crt"), "-days", "1",
            "-subj", f"/CN={hostname}", "-addext", f"subjectAltName=DNS:{hostname}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        caddyfile.parent.joinpath("fixture.key").chmod(0o600)
        caddyfile.write_text(caddyfile.read_text().replace(
            "admin off", "admin unix//etc/caddy/admin.sock").replace(
            "headscale-test:8080", spec["service_name"] + ":8080").replace(
            "headscale.test {", hostname + " {"))
        self.compose_path.write_text(json.dumps(stack))
        self.ingress = caddy["container_name"]
        worker_settings = {
            "host_id": "do3", "config": str(self.config),
            "mapping_database": str(self.directory / "operator.db"),
            "data_root": str(self.directory / "tenants"), "compose_project": self.project,
            "public_ipv4": "8.8.8.8", "ingress_container": self.ingress,
            "ingress_file": str(caddyfile), "ingress_container_file": "/etc/caddy/Caddyfile",
            "ingress_admin_socket": "/etc/caddy/admin.sock",
            "ingress_baseline_sha256": "0" * 64,
            "ingress_tls_certificate": "/etc/caddy/fixture.crt",
            "ingress_tls_key": "/etc/caddy/fixture.key",
        }
        self.worker_settings = self.private_file("worker.json", json.dumps(worker_settings))
        adapter = self.directory / "adapter/scripts"
        adapter.mkdir(parents=True)
        adapter.joinpath("__init__.py").write_text(
            f"__path__.append({str(ROOT / 'scripts')!r})\n")
        # Only the test process uses these network/DERP/profile boundaries.
        # Production Worker container/ingress/key/mapping code remains unchanged.
        adapter.joinpath("provisioning_worker.py").write_text(f"""
import importlib.util,json,pathlib,sys,os
sys.path.append({str(ROOT)!r})
s=importlib.util.spec_from_file_location('isolated_worker',{str(ROOT / 'scripts/provisioning_worker.py')!r})
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.PEER_PROFILE='candidate-isolated-only';m.PEER_PORT=443
original_command=m.Worker.command
original_enrollment=m.Worker.enrollment
def enrollment(self,request):
    result=original_enrollment(self,request)
    marker=pathlib.Path({str(self.directory / 'interrupt-once')!r})
    if marker.exists() and marker.read_text()==request['node_id'] and result.get('state')=='issued':
        marker.unlink();os._exit(55)
    return result
def command(self,*args,**kwargs):
    try: return original_command(self,*args,**kwargs)
    except m.Failure:
        pathlib.Path({str(self.directory / 'failed-command.json')!r}).write_text(json.dumps(args))
        raise
def render(self,spec):
    return pathlib.Path({str(self.directory / 'fixture-headscale.yaml')!r}).read_text()
def dns(self,spec):
    addresses=[]
    for host in ('headscale.test','probe-'+spec['provisioning_tailnet_id']+'.swarm.rawrarr.net'):
        result=self.command('docker','exec',self.s['ingress_container'],'getent','hosts',host)
        if not result.strip(): raise m.Failure('wildcard_dns_not_ready','dns')
        addresses.append(set(line.split()[0] for line in result.splitlines()))
    if len(addresses[0])!=1 or addresses[0]!=addresses[1]: raise m.Failure('wildcard_dns_not_ready','dns')
def https(self,spec):
    output=self.command('docker','exec',self.s['ingress_container'],'curl','--fail','--silent',
        '--noproxy','*','--cacert','/etc/caddy/fixture.crt',
        spec['server_url']+'/health')
    if json.loads(output).get('status')!='pass': raise m.Failure('headscale_public_not_ready')
m.Worker.enrollment=enrollment;m.Worker.command=command;m.Worker.render=render;m.Worker.dns=dns;m.Worker.https=https
m.main()
""")
        settings["worker"] = {
            "repository": str(adapter.parent), "settings": str(self.worker_settings),
        }
        self.worker_process_settings = self.private_file("worker-process.json", json.dumps(settings))

    def test_api_worker_ingress_and_adoption_recovery(self):
        worker = self.start_ready_provider()
        settings = json.loads(self.worker_settings.read_text())
        caddyfile = self.directory / "caddy/Caddyfile"
        status, ready = self.api(":ensure", {})
        self.assertEqual(status, 200)
        self.assertEqual(ready["tailnet_id"], self.tailnet)
        self.assertEqual(ready["enrollment_recovery_version"], "1")
        self.exercise_recovery(worker, settings, caddyfile)

    def start_ready_provider(self):
        self.trust()
        caddyfile = self.directory / "caddy/Caddyfile"
        baseline = getattr(self, "integration_baseline",
                           "{\n    admin unix//etc/caddy/admin.sock\n}\n\n")
        self.baseline = baseline
        existing_container = self.command("docker", "inspect", "--format", "{{.Id}}",
                                          self.spec["service_name"]).strip()
        caddyfile.write_text(baseline)
        settings = json.loads(self.worker_settings.read_text())
        settings["ingress_baseline_sha256"] = hashlib.sha256(baseline.encode()).hexdigest()
        self.worker_settings.write_text(json.dumps(settings))
        args = SimpleNamespace(
            ledger=str(self.ledger), worker_settings=str(self.worker_settings),
            account_id=self.account, tailnet_id=self.tailnet,
            instance_id=1, apply=False,
        )
        self.assertFalse(adopt(args)["applied"])
        args.apply = True
        adopt(args)
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            initial = json.loads(db.execute("SELECT document FROM account_defaults").fetchone()[0])
        adopt(args)
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            repeated = json.loads(db.execute("SELECT document FROM account_defaults").fetchone()[0])
        self.assertEqual(initial["operation_id"], repeated["operation_id"])
        worker = self.launch_worker()
        self.wait_for("combined allocation readiness", self.ready, timeout=45)
        self.assertEqual(self.command("docker", "inspect", "--format", "{{.Id}}",
                                      self.spec["service_name"]).strip(), existing_container)
        return worker

    def exercise_recovery(self, worker, settings, caddyfile):
        key = str(uuid.uuid4())
        original_setup = self.intent
        body = {"node_id": self.node, "setup_intent_id": self.intent}
        issued = self.wait_for("encrypted issuance",
                               lambda: self.receipt("/enrollments", body, key), timeout=60)
        self.credentials.append(issued["auth_key"])
        self.stop(worker)
        worker = self.launch_worker()
        recovered = self.wait_for("receipt restart recovery",
                                 lambda: self.receipt("/enrollments", body, key, (200,)))
        self.assertEqual(recovered["auth_key"], issued["auth_key"])
        self.assertTrue(self.login("client-a", recovered["auth_key"]))
        self.stop(worker)
        # A real Caddy validation failure must restore disk/active config and fence.
        settings["ingress_tls_certificate"] = "/etc/caddy/missing-fixture.crt"
        self.worker_settings.write_text(json.dumps(settings))
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("UPDATE account_defaults SET due=1")
        worker = self.launch_worker()
        self.wait_for("ingress fence", lambda: (self.api()[1].get("error") or {}).get("code")
                      in ("ingress_reload_or_verification_failed",
                          "ingress_outcome_requires_reconciliation"))
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM worker_receipts WHERE resource='ingress-fence'")
                             .fetchone()[0], "unknown")
        self.stop(worker)
        # Only after the real active/disk configuration is equal and worker quiescent.
        self.wait_for("quiescent fixture container commands", self.quiescent, timeout=20)
        disk = json.loads(self.execute("caddy", "caddy", "adapt", "--config",
                                       "/etc/caddy/Caddyfile", "--adapter", "caddyfile"))
        active = json.loads(self.execute("caddy", "curl", "--fail", "--silent",
                                         "--unix-socket", "/etc/caddy/admin.sock",
                                         "http://localhost/config/"))
        self.assertEqual(disk, active)
        settings["ingress_tls_certificate"] = "/etc/caddy/fixture.crt"
        self.worker_settings.write_text(json.dumps(settings))
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("DELETE FROM worker_receipts WHERE resource='ingress-fence'")
            db.execute("UPDATE account_defaults SET due=1")
        worker = self.launch_worker()
        self.wait_for("reconciled ingress readiness", self.ready, timeout=45)
        self.stop(worker)
        with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("UPDATE account_defaults SET document=json_set(document,'$.valid_until_epoch',0)")
        self.assertEqual(self.api()[1]["state"], "degraded")
        current = caddyfile.read_text()
        caddyfile.write_text("# unexpected operator drift\n" + current)
        worker = self.launch_worker()
        self.wait_for("failed ingress readiness", lambda: (self.api()[1].get("error") or {}).get("code")
                      == "ingress_baseline_drift")
        self.stop(worker)
        caddyfile.write_text(current)
        worker = self.launch_worker()
        self.wait_for("restored readiness", lambda: self.api()[1].get("state") == "ready")
        self.wait_for("original consumption", lambda: self.api(
            "/enrollments/" + issued["enrollment_id"])[1].get("state") == "consumed")
        for character, expire in (("b", False), ("c", True)):
            self.node = "fossil-" + character * 24
            self.intent = str(uuid.uuid4())
            enrollment_body = {"node_id": self.node, "setup_intent_id": self.intent}
            original_key = str(uuid.uuid4())
            original = self.wait_for("recovery predecessor", lambda: self.receipt(
                "/enrollments", enrollment_body, original_key))
            self.credentials.append(original["auth_key"])
            _, metadata = self.api("/enrollment-requests/" + original_key)
            self.assertEqual(metadata["enrollment_id"], original["enrollment_id"])
            self.assertNotIn("auth_key", metadata)
            if expire:
                with contextlib.closing(sqlite3.connect(self.ledger)) as db, db:
                    key_id = json.loads(db.execute("SELECT document FROM enrollments WHERE id=?",
                                                   (original["enrollment_id"],)).fetchone()[0])["headscale_key_id"]
                self.execute("headscale-test", "headscale", "preauthkeys", "expire", "--id", str(key_id))
            binding = self.recovery_binding(original["enrollment_id"], self.intent)
            request_id = str(uuid.uuid4())
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: self.api(
                    "/enrollments/" + original["enrollment_id"] + ":recover", binding, request_id),
                    range(2)))
            self.assertTrue(all(status in (200, 201, 202) for status, _ in results))
            self.assertEqual(results[0][1]["enrollment_id"], results[1][1]["enrollment_id"])
            successor = self.wait_for("issued/expired predecessor recovery", lambda: self.receipt(
                "/enrollments/" + original["enrollment_id"] + ":recover", binding, request_id))
            self.credentials.append(successor["auth_key"])
            self.assertEqual(successor["recovery"], binding)
            self.assertEqual(successor["recovery_request_id"], request_id)
            _, fenced = self.api("/enrollments", enrollment_body, original_key)
            self.assertEqual(fenced["error"]["code"], "enrollment_superseded")
            self.assertEqual(fenced["successor_enrollment_id"], successor["enrollment_id"])
            self.stop(worker)
            worker = self.launch_worker()
            replay = self.wait_for("recovery lost response replay", lambda: self.receipt(
                "/enrollments/" + original["enrollment_id"] + ":recover", binding, request_id))
            self.assertEqual(replay["auth_key"], successor["auth_key"])
        self.node = "fossil-" + "d" * 24
        self.intent = str(uuid.uuid4())
        lost_key = str(uuid.uuid4())
        body = {"node_id": self.node, "setup_intent_id": self.intent}
        self.private_file("interrupt-once", self.node)
        self.api("/enrollments", body, lost_key)
        self.wait_for("interrupted creation worker exit", lambda: worker.poll() is not None)
        _, interrupted = self.api("/enrollment-requests/" + lost_key)
        self.assertNotIn("auth_key", interrupted)
        self.assertTrue(interrupted["enrollment_id"])
        self.launch_worker()
        terminal = self.wait_for("uncertain creation replay", lambda: self.receipt(
            "/enrollments", body, lost_key, (409,)))
        self.assertEqual(terminal["error"]["code"], "enrollment_uncertain")
        self.assertTrue(terminal["enrollment_id"])
        predecessor = terminal["enrollment_id"]
        self.assertNotIn("auth_key", terminal)
        replacement_key = str(uuid.uuid4())
        binding = self.recovery_binding(predecessor, self.intent)
        successor = self.wait_for("verified explicit replacement", lambda: self.receipt(
            "/enrollments/" + predecessor + ":recover", binding, replacement_key))
        self.credentials.append(successor["auth_key"])
        self.assertNotEqual(successor["enrollment_id"], predecessor)
        status, old = self.api("/enrollments/" + predecessor)
        self.assertEqual(old["state"], "revoked")
        self.node = "fossil-0123456789abcdef01234567"
        self.intent = original_setup
        binding = self.recovery_binding(issued["enrollment_id"], original_setup)
        request_id = str(uuid.uuid4())
        rebinding = self.wait_for("established device recovery rejected", lambda: self.receipt(
            "/enrollments/" + issued["enrollment_id"] + ":recover", binding, request_id, (409,)))
        self.assertEqual(rebinding["error"]["code"], "enrollment_uncertain")
        successor_id = rebinding["enrollment_id"]
        self.intent = binding["setup_intent_id"]
        _, metadata = self.api("/enrollments/" + successor_id)
        self.assertEqual(metadata["error"]["code"], "recovery_device_rebinding_required")
        self.assertNotIn("auth_key", metadata)

    def recovery_binding(self, predecessor, old_setup):
        return {
            "version": "1", "predecessor_enrollment_id": predecessor,
            "predecessor_setup_intent_id": old_setup, "setup_intent_id": str(uuid.uuid4()),
            "node_id": self.node, "owner_authorization_id": str(uuid.uuid4()),
            "setup_evidence_sha256": "a" * 64, "account_root_fingerprint": "b" * 64,
            "role": "metadata", "capability_digest": "c" * 64,
        }
