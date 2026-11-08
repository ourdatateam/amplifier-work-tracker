"""Static safety checks for the non-destructive hosted deployment."""

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "hosted"


class HostedDeploymentSafetyTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (DEPLOY / name).read_text(encoding="utf-8")

    def test_provision_checks_pve_vm_and_ip_in_both_managers(self):
        script = self.read("provision-lxc.sh")
        for value in (
            "11200160",
            "beads160",
            "192.168.200.160/24",
            "vmbr0",
            "local-lvm:32",
            "pve21",
        ):
            self.assertIn(value, script)
        self.assertIn("--unprivileged 1", script)
        self.assertIn("arping -D", script)
        self.assertIn("pvesh get /cluster/nextid --vmid", script)
        self.assertIn("/etc/pve/lxc", script)
        self.assertIn("/etc/pve/qemu-server", script)
        self.assertIn("qm list", script)
        self.assertIn("pct list", script)
        self.assertNotRegex(script, re.compile(r"\bpct\s+(?:destroy|stop|shutdown|delete)\b"))

    def test_install_is_pinned_and_uses_service_user_context(self):
        script = self.read("install-host.sh")
        self.assertLess(script.index("hostname -s"), script.index("apt-get update"))
        self.assertIn("sha256sum -c SHA256SUMS", script)
        self.assertIn("exactly the three reviewed artifact pins", script)
        self.assertIn("--no-same-owner", script)
        self.assertIn("mcp==1.30.0", script)
        self.assertIn("amplifier-core==2.0.2", script)
        self.assertNotIn("BEADS_DB", script)
        self.assertNotRegex(script, re.compile(r"^export HOME=", re.M))
        self.assertNotRegex(script, re.compile(r"After=.*amplifier-work-tracker\.service"))
        self.assertIn("amplifier-work-tracker service install --root", script)
        self.assertIn("systemctl --user is-active --quiet amplifier-work-tracker.service", script)
        self.assertIn('new "$name" --root "$PROJECTS"', script)
        self.assertIn('"$PROJECTS/projects/home"', script)
        self.assertIn('"$PROJECTS/projects/system"', script)
        self.assertIn("WorkingDirectory=/var/lib/beads/projects/projects/home", script)
        self.assertIn("/var/lib/beads/projects/projects/*", script)
        self.assertIn("merge-base --is-ancestor", script)
        self.assertIn("deployed-source-manifest.json", script)
        self.assertIn("systemctl reload nginx", script)
        self.assertIn(
            "TRACKER_UNIT=/var/lib/beads/.config/systemd/user/amplifier-work-tracker.service",
            script,
        )
        self.assertIn("BEADS_MCP_TOKEN_FILE=/etc/beads/mcp-tokens.json", script)
        self.assertIn("BEADS_MCP_PORT=8104", script)
        self.assertIn('"human" if actor == "human" else "worker"', script)
        self.assertIn("OnUnitActiveSec=5s", script)

    def test_proxy_targets_only_local_services(self):
        script = self.read("install-host.sh")
        nginx = self.read("nginx-beads.conf")
        self.assertIn("127.0.0.1:3308", script)
        self.assertIn("127.0.0.1:3000", nginx)
        self.assertIn("127.0.0.1:8104", nginx)
        self.assertIn("proxy_buffering off", nginx)
        self.assertIn("proxy_set_header Host $host", nginx)
        self.assertIn("/healthz", nginx)

    def test_release_and_source_pins_are_explicit(self):
        pins = dict(
            line.split("=", 1)
            for line in self.read("versions.env").splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(pins["BEADS_VERSION"], "1.1.2")
        self.assertEqual(pins["DOLT_VERSION"], "2.2.3")
        self.assertEqual(pins["NODE_VERSION"], "22.23.4")
        self.assertEqual(pins["BEADS_UI_VERSION"], "0.12.6")
        self.assertEqual(pins["SOURCE_REVISION"], "00cba285493d1de4b020500eee112525215db06d")
        self.assertEqual(
            pins["NODE_SHA256"], "1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce"
        )
        for key in ("BEADS_SHA256", "DOLT_SHA256", "NODE_SHA256"):
            self.assertRegex(pins[key], r"^[0-9a-f]{64}$")

    def test_backup_quiesces_writers_and_preserves_cold_archive(self):
        script = self.read("backup.sh")
        for service in (
            "beads-ui.service",
            "beads-mcp.service",
            "beads-watcher-touch.timer",
            "beads-watcher-touch.service",
        ):
            self.assertIn(service, script)
        self.assertIn("systemctl --user stop", script)
        self.assertIn("systemctl --user start", script)
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn("restart_previously_active", script)
        self.assertIn("RESTART_FAILED=1", script)
        self.assertIn("retain_verified_backups", script)
        self.assertIn("[:7]", script)
        self.assertIn("beads-(\\d{8}T\\d{6}Z)\\.tar\\.gz", script)
        self.assertIn("--numeric-owner --xattrs --acls", script)
        for path in ("/var/lib/beads", "/etc/beads", "/opt/beads"):
            self.assertIn(path, script)
        self.assertIn("tar -tzf", script)
        self.assertIn("sha256sum -c", script)
        self.assertIn("chmod 0600", script)
        self.assertNotIn("jsonl", script.lower())

    def test_host_verification_probes_ui_and_mcp_responses(self):
        script = self.read("verify-host.sh")
        self.assertIn("http://127.0.0.1:3000/", script)
        self.assertIn("http://127.0.0.1:8104/mcp", script)
        self.assertIn("http://127.0.0.1/mcp", script)
        self.assertIn("== 401", script)
        self.assertIn("http://127.0.0.1/healthz", script)

    def test_new_project_invocation_uses_subcommand_root_and_nested_workspace_path(self):
        script = self.read("install-host.sh")
        match = re.search(r"(?ms)^create_workspace_project\(\) \{\n.*?^\}", script)
        self.assertIsNotNone(match, "installer must keep project creation in the tested helper")
        assert match is not None
        function = match.group(0)
        self.assertIn('"$PROJECTS/projects/$name"', function)
        self.assertIn('"$TRACKER_CLI" new "$name" --root "$PROJECTS"', function)

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            workspace_root = base / "workspace-root"
            mock_bin = base / "bin"
            mock_bin.mkdir()
            cli = base / "mock-tracker"
            cli.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import argparse, json, os, sys
                    parser = argparse.ArgumentParser()
                    sub = parser.add_subparsers(dest="command", required=True)
                    new = sub.add_parser("new")
                    new.add_argument("name")
                    new.add_argument("--root", required=True)
                    args = parser.parse_args()
                    with open(os.environ["MOCK_CLI_LOG"], "w") as out:
                        json.dump(vars(args), out)
                    """
                ),
                encoding="utf-8",
            )
            cli.chmod(0o755)
            runuser = mock_bin / "runuser"
            runuser.write_text('#!/usr/bin/env bash\nshift 3\nexec "$@"\n', encoding="utf-8")
            runuser.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{mock_bin}:{os.environ['PATH']}",
                "PROJECTS": str(workspace_root),
                "TRACKER_CLI": str(cli),
                "uid": "4242",
                "MOCK_CLI_LOG": str(base / "cli.json"),
            }
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'fail() {{ echo "$*" >&2; exit 1; }}\n{function}\n'
                    "create_workspace_project home",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = json.loads(Path(env["MOCK_CLI_LOG"]).read_text(encoding="utf-8"))
            self.assertEqual(
                recorded, {"command": "new", "name": "home", "root": str(workspace_root)}
            )
            self.assertEqual(
                workspace_root / "projects" / "home",
                Path(recorded["root"]) / "projects" / recorded["name"],
            )
            self.assertFalse((workspace_root / "home").exists())

    def test_npm_proxy_uses_reviewed_internal_api_and_fails_closed(self):
        script = self.read("npm-proxy.mjs")
        for expected in (
            "new Access(null)",
            "await access.load(true)",
            "db()",
            'knex("certificate")',
            'knex("proxy_host")',
            "proxyHost.create(access",
            "proxyHost.update(access",
            "certificate_id: 6",
            "access_list_id: 0",
            "192.168.200.160",
            "meta?.nginx_online === true",
            'spawnSync("nginx", ["-t"]',
            "mode: 0o600",
            "sqlite3.connect",
        ):
            self.assertIn(expected, script)
        self.assertIn("refusing to alter them", script)
        self.assertNotRegex(script, re.compile(r"\b(?:password|token|credentials)\s*=\s*", re.I))
        self.assertNotIn("TODO", script)


if __name__ == "__main__":
    unittest.main()
