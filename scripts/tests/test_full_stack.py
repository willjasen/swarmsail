"""Opt-in, disposable Docker integration test; see runbooks/test-full-stack.md."""

import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from manage_headscale import (
    ensure_caddy_config,
    ensure_headscale_configs,
    generate_compose,
)


ROOT = Path(__file__).resolve().parents[2]
ENABLED = os.environ.get("SWARMSAIL_RUN_E2E") == "1"


@unittest.skipUnless(ENABLED, "set SWARMSAIL_RUN_E2E=1 to deploy the Docker test stack")
class FullStackTests(unittest.TestCase):
    def command(self, *args, input=None, timeout=60):
        result = subprocess.run(
            args, input=input, capture_output=True, text=True, timeout=timeout,
            cwd=ROOT,
        )
        if result.returncode:
            # Authentication output and container logs can contain credentials.
            detail = result.stderr
            for credential in getattr(self, "credentials", []):
                detail = detail.replace(credential, "[redacted]")
            self.fail(f"{args[0]} {args[1]} failed "
                      f"(exit {result.returncode}): {detail[-4000:]}")
        return result.stdout

    def compose(self, *args, **kwargs):
        return self.command(
            "docker", "compose", "--project-name", self.project,
            "--file", str(self.compose_path), *args, **kwargs,
        )

    def execute(self, service, *args, **kwargs):
        return self.compose("exec", "-T", service, *args, **kwargs)

    def wait_for(self, description, probe, timeout=120):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = probe()
                if value:
                    return value
            except (AssertionError, subprocess.TimeoutExpired,
                    json.JSONDecodeError, KeyError) as exc:
                last_error = str(exc)[-2000:]
            time.sleep(2)
        self.fail(f"Timed out waiting for {description} (last error: {last_error})")

    def setUp(self):
        self.credentials = []
        self.keep_requested = os.environ.get("SWARMSAIL_E2E_KEEP") == "1"
        self.keep_stack = False
        self.assertEqual(
            self.command("docker", "info", "--format", "{{.OSType}}").strip(),
            "linux", "the integration test requires Linux containers",
        )
        self.command("docker", "compose", "version")
        state_root = None
        if self.keep_requested:
            state_root = Path.home() / ".local" / "state" / "swarmsail" / "e2e"
            state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = Path(tempfile.mkdtemp(prefix="swarmsail-e2e-", dir=state_root))
        self.addCleanup(self.cleanup_files)
        self.project = f"swarmsail-e2e-{uuid.uuid4().hex[:12]}"
        self.compose_path = self.directory / "compose.json"
        self.write_stack()
        # Register before starting anything, including partially failed deployments.
        self.addCleanup(self.cleanup_stack)
        self.compose("config", "--quiet")
        self.compose("up", "--detach", "--build", timeout=600)

    def cleanup_stack(self):
        if self.keep_stack:
            print(f"\nRetained Docker project: {self.project}")
            print(f"Compose file: {self.compose_path}")
            return
        self.compose(
            "down", "--volumes", "--remove-orphans", "--rmi", "local",
            "--timeout", "10", timeout=120,
        )

    def cleanup_files(self):
        if not self.keep_stack:
            shutil.rmtree(self.directory)

    def write_stack(self):
        directory = self.directory
        instance = {
            "id": 1, "web_port": 8080, "service_name": "headscale-test",
            "server_url": "https://headscale.test",
            "base_domain": "tailnet.test", "ipv4_prefix": "100.64.0.0/26",
            "publish_web_port": False, "stun_port_udp": False,
            "config_path": str(directory / "headscale"),
            "data_path": "headscale-data",
        }
        config = {
            "headscale": {"instances": [instance]},
            "npm": {"enabled": False},
            "crowdsec": {
                "enabled": True,
                "timezone": "UTC",
                "bouncer_api_key_path": str(directory / "unused-bouncer-key"),
            },
            "caddy": {
                "enabled": True, "listen_ip": "127.0.0.1",
                "config_dir": str(directory / "caddy"),
                "log_dir": str(directory / "caddy-logs"),
                "data_dir": "caddy-data", "state_dir": "caddy-state",
                # Generation requires this path; local TLS does not use a token.
                "cloudflare_token_path": str(directory / "unused-token"),
            },
            "network": {"name": "backend", "driver": "bridge"},
        }
        (directory / "instances.json").write_text(json.dumps(config))
        ensure_headscale_configs(config, directory)
        ensure_caddy_config(config, directory)
        stack = generate_compose(config)

        # Keep the generated route, replacing only public ACME with a private CA.
        caddyfile = directory / "caddy" / "Caddyfile"
        _, separator, routes = caddyfile.read_text().partition("\n\n")
        self.assertTrue(separator)
        routes = routes.replace(
            "headscale.test {\n",
            "headscale.test {\n    tls internal\n"
            "    log {\n        output file /logs/access.log\n        format json\n    }\n",
        )
        routes = routes.replace("        crowdsec\n", "")
        routes = routes.replace(
            "/var/log/caddy/access.log", "/logs/access.log"
        )
        caddyfile.write_text(
            "{\n    admin off\n}\n\n" + routes
            + "\nswarmsail.test {\n    tls internal\n"
            "    reverse_proxy control-plane:8787\n}\n"
        )
        # Embedded DERP avoids depending on the public Tailscale relay service.
        headscale_config = directory / "headscale" / "config.yaml"
        content = headscale_config.read_text()
        content = content.replace("    enabled: false", "    enabled: true")
        content = content.replace(
            "  urls:\n    - https://controlplane.tailscale.com/derpmap/default",
            "  urls: []",
        ).replace("  auto_update_enabled: true", "  auto_update_enabled: false")
        headscale_config.write_text(content)
        (directory / "acquis.yaml").write_text(
            "filenames:\n  - /logs/access.log\nlabels:\n  type: caddy\n"
        )

        services = stack["services"]
        caddy = services["caddy"]
        # The test uses the same Caddy version, without the public DNS-01 plugin.
        caddy.pop("build")
        caddy["image"] = "caddy:2.11.7-alpine"
        caddy["volumes"] = [
            f"{caddyfile}:/etc/caddy/Caddyfile:ro",
            "caddy-data:/data", "caddy-state:/config", "caddy-logs:/logs",
        ]
        caddy.pop("secrets", None)
        stack.pop("secrets", None)
        caddy["networks"] = {
            name: {"aliases": ["headscale.test", "swarmsail.test"]}
            for name in ("backend", "client-a", "client-b")
        }
        services["crowdsec"].update({
            "image": "crowdsecurity/crowdsec:v1.8.1",
            "environment": {
                "TZ": "UTC", "COLLECTIONS": "crowdsecurity/caddy",
                "DISABLE_ONLINE_API": "true",
            },
            "volumes": [
                "crowdsec-config:/etc/crowdsec",
                "crowdsec-data:/var/lib/crowdsec/data",
                "caddy-logs:/logs:ro",
                f"{directory / 'acquis.yaml'}:/etc/crowdsec/acquis.yaml:ro",
            ],
        })
        services["control-plane"] = {
            "build": {"context": str(ROOT), "dockerfile": "Dockerfile.orbstack"},
            "volumes": [
                "swarmsail-data:/data",
                f"{directory / 'instances.json'}:/data/instances.json:ro",
            ],
            "read_only": True, "tmpfs": ["/tmp"], "networks": ["backend"],
        }
        for name in ("client-a", "client-b"):
            services[name] = {
                "image": "tailscale/tailscale:v1.104.1",
                "entrypoint": ["tailscaled"],
                "command": [
                    "--state=/var/lib/tailscale/tailscaled.state",
                    "--socket=/var/run/tailscale/tailscaled.sock",
                    "--tun=tailscale0",
                ],
                "cap_add": ["NET_ADMIN", "NET_RAW"],
                "environment": {"TS_NO_LOGS_NO_SUPPORT": "true"},
                "devices": ["/dev/net/tun:/dev/net/tun"],
                "volumes": [f"{name}-data:/var/lib/tailscale"],
                "networks": [name],
            }
            stack["networks"][name] = {"driver": "bridge", "internal": True}
        for service in services.values():
            service.pop("container_name", None)
            service.pop("restart", None)
            service.pop("ports", None)
        if self.keep_requested:
            services["control-plane"]["ports"] = ["127.0.0.1::8787"]
        stack["volumes"] = {name: {} for name in (
            "headscale-data", "caddy-data", "caddy-state", "caddy-logs",
            "crowdsec-config", "crowdsec-data", "swarmsail-data",
            "client-a-data", "client-b-data",
        )}
        self.compose_path.write_text(json.dumps(stack))

    def test_clients_exchange_ping_and_tcp_through_managed_tailnet(self):
        self.wait_for("Headscale", lambda: self.execute(
            "headscale-test", "headscale", "users", "list", "--output", "json",
            timeout=10,
        ))
        certificate = self.directory / "root.crt"
        self.wait_for("Caddy private CA", lambda: self.compose(
            "cp", "caddy:/data/caddy/pki/authorities/local/root.crt",
            str(certificate), timeout=10,
        ) or certificate.exists())
        for client in ("client-a", "client-b"):
            self.compose("cp", str(certificate),
                         f"{client}:/usr/local/share/ca-certificates/swarmsail.crt")
            self.execute(client, "update-ca-certificates")
            # Go caches system roots; restart the daemon after installing the CA.
            self.compose("restart", client)
            self.wait_for(f"{client} daemon", lambda: self.execute(
                client, "tailscale", "status", "--json", timeout=10,
            ))
            self.wait_for(f"{client} HTTPS ingress", lambda: self.execute(
                client, "wget", "-qO-", "https://headscale.test/health", timeout=10,
            ))
        state = json.loads(self.execute(
            "client-a", "wget", "-qO-", "https://swarmsail.test/api/state",
        ))
        self.assertEqual(len(state["headscale_instances"]), 1)
        self.assertEqual(state["headscale_instances"][0]["server_url"],
                         "https://headscale.test")
        health = json.loads(self.execute(
            "client-a", "wget", "-qO-", "https://swarmsail.test/healthz",
        ))
        self.assertEqual(health["status"], "ok")
        user = json.loads(self.execute(
            "headscale-test", "headscale", "users", "create", "e2e",
            "--output", "json",
        ))
        addresses = {}
        for client in ("client-a", "client-b"):
            key = json.loads(self.execute(
                "headscale-test", "headscale", "preauthkeys", "create",
                "--user", str(user["id"]), "--expiration", "10m", "--output", "json",
            ))["key"]
            self.credentials.append(key)
            # No credential in process arguments, test output, or retained files.
            self.execute(client, "sh", "-c",
                         "umask 077; cat > /tmp/enrollment-key", input=key)
            try:
                self.execute(
                    client, "tailscale", "up",
                    "--login-server=https://headscale.test",
                    "--auth-key=file:/tmp/enrollment-key",
                    f"--hostname={client}", "--accept-dns=false", "--timeout=60s",
                    timeout=75,
                )
            finally:
                self.execute(client, "rm", "-f", "/tmp/enrollment-key")
            status = json.loads(self.execute(client, "tailscale", "status", "--json"))
            self.assertEqual(status["BackendState"], "Running")
            addresses[client] = self.execute(client, "tailscale", "ip", "-4").strip()
            self.assertIn(ipaddress.ip_address(addresses[client]),
                          ipaddress.ip_network("100.64.0.0/26"))
            self.compose(
                "exec", "--detach", client, "nc", "-lk",
                "-s", addresses[client], "-p", "8088", "-e", "/bin/cat",
            )
        self.assertNotEqual(addresses["client-a"], addresses["client-b"])
        nodes = json.loads(self.execute(
            "headscale-test", "headscale", "nodes", "list", "--output", "json",
        ))
        self.assertEqual(len(nodes), 2)
        self.assertEqual({node["given_name"] for node in nodes},
                         {"client-a", "client-b"})
        for source, destination in (("client-a", "client-b"), ("client-b", "client-a")):
            address = addresses[destination]
            self.wait_for(f"{source} overlay ping to {destination}", lambda: self.execute(
                source, "ping", "-c", "3", "-W", "3", address, timeout=15,
            ))
            payload = f"{source}-to-{destination}-{uuid.uuid4().hex}\n"
            body = self.wait_for(
                f"{source} overlay TCP to {destination}",
                lambda: self.execute(
                    source, "nc", "-w", "2", address, "8088",
                    input=payload, timeout=10,
                ),
            )
            self.assertEqual(body, payload)

        self.wait_for("CrowdSec local API", lambda: self.execute(
            "crowdsec", "cscli", "lapi", "status", timeout=15,
        ))

        def acquired_caddy_logs():
            # Produce fresh access logs even if CrowdSec started after enrollment.
            self.execute("client-a", "wget", "-qO-", "https://headscale.test/health",
                         timeout=10)
            metrics = json.loads(self.execute(
                "crowdsec", "cscli", "metrics", "--output", "json", timeout=15,
            ))
            acquisition = metrics.get("acquisition", {})
            return any(
                "access.log" in name and float(values.get("parsed", 0)) > 0
                for name, values in acquisition.items()
            )

        self.wait_for("CrowdSec parsing Caddy access logs", acquired_caddy_logs)
        output = self.compose(
            "ps", "--all", "--format", "json",
        )
        containers = (json.loads(output) if output.lstrip().startswith("[")
                      else [json.loads(line) for line in output.splitlines()])
        self.assertEqual(len(containers), 6)
        self.assertTrue(all(container["State"] == "running" for container in containers))
        self.keep_stack = self.keep_requested


if __name__ == "__main__":
    unittest.main()
