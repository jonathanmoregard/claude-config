#!/usr/bin/env python3
"""Contract tests for the host-pinned smarthome SSH broker."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"


def load_server():
    name = f"smarthome_ssh_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load smarthome-ssh server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.known_hosts = root / "known_hosts"
        self.known_hosts.write_text(
            "home-server,192.0.2.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA root@home-server\n",
            encoding="utf-8",
        )
        self.ssh = root / "ssh"
        self.ssh.write_text("#!/bin/sh\n", encoding="utf-8")
        self.ssh.chmod(0o755)
        self.agent_root = root / "run"
        self.agent_root.mkdir()
        self.agent = self.agent_root / "agent.sock"
        self.agent.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def raw_policy(self, **overrides) -> dict:
        raw = {
            "version": 1,
            "host": "home-server",
            "user": "jonathan",
            "knownHostsFile": str(self.known_hosts),
            "sshExecutable": str(self.ssh),
            "identityAgent": str(self.agent),
            "identityAgentRoot": str(self.agent_root),
            "connectTimeoutSec": 10,
            "maxTimeoutMs": 600000,
            "outputLimitChars": 60000,
        }
        raw.update(overrides)
        return raw

    def policy(self, **overrides) -> dict:
        return self.server._load_policy({"smarthomeSsh": self.raw_policy(**overrides)})


class ToolSurfaceTests(Fixture):
    def test_exposes_only_ssh_tool(self) -> None:
        self.assertEqual([tool["name"] for tool in self.server.TOOLS], ["ssh"])

    def test_schema_rejects_unknown_fields(self) -> None:
        schema = self.server.TOOLS[0]["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["command"])
        self.assertEqual(set(schema["properties"]), {"command", "timeout"})


class PolicyTests(Fixture):
    def test_valid_policy_loads(self) -> None:
        policy = self.policy()
        self.assertEqual(policy["host"], "home-server")
        self.assertEqual(policy["user"], "jonathan")

    def test_missing_and_unknown_keys_fail_closed(self) -> None:
        raw = self.raw_policy()
        del raw["host"]
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.server._load_policy({"smarthomeSsh": raw})
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.policy(extra=True)

    def test_host_and_user_must_be_plain_names(self) -> None:
        for bad in ("-oProxyCommand=x", "home server", "a@b", ""):
            with self.subTest(host=bad):
                with self.assertRaises(RuntimeError):
                    self.policy(host=bad)
            with self.subTest(user=bad):
                with self.assertRaises(RuntimeError):
                    self.policy(user=bad)

    def test_known_hosts_must_pin_host(self) -> None:
        self.known_hosts.write_text(
            "other ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA root@other\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "known_hosts"):
            self.policy()

    def test_relative_known_hosts_resolves_next_to_server(self) -> None:
        policy = self.server._load_policy(
            {"smarthomeSsh": self.raw_policy(knownHostsFile="known_hosts")},
            base_dir=self.known_hosts.parent,
        )
        self.assertEqual(policy["knownHostsFile"], self.known_hosts)

    def test_agent_socket_must_stay_under_agent_root(self) -> None:
        outside = Path(self.tmp.name) / "elsewhere.sock"
        outside.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "identity agent"):
            self.policy(identityAgent=str(outside))

    def test_agent_paths_expand_runtime_dir_token(self) -> None:
        with mock.patch.object(self.server, "_runtime_dir", return_value=self.agent_root):
            policy = self.policy(identityAgent="%t/agent.sock", identityAgentRoot="%t")
        self.assertEqual(policy["identityAgent"], self.agent)
        self.assertEqual(policy["identityAgentRoot"], self.agent_root)

    def test_runtime_dir_is_derived_from_uid(self) -> None:
        self.assertEqual(self.server._runtime_dir(), Path(f"/run/user/{os.getuid()}"))

    def test_ssh_executable_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "sshExecutable"):
            self.policy(sshExecutable="ssh")


class RequestTests(Fixture):
    def test_command_is_required_and_bounded(self) -> None:
        policy = self.policy()
        for bad in ({}, {"command": ""}, {"command": "   "}, {"command": 3},
                    {"command": "a\x00b"}, {"command": "x" * 20000}):
            with self.subTest(args=bad):
                with self.assertRaises(RuntimeError):
                    self.server._validate_request(bad, policy)

    def test_unknown_fields_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            self.server._validate_request({"command": "true", "host": "x"}, self.policy())

    def test_timeout_bounds(self) -> None:
        policy = self.policy()
        self.assertEqual(
            self.server._validate_request({"command": "true"}, policy)["timeout"],
            self.server.DEFAULT_TIMEOUT_MS,
        )
        for bad in (0, -1, 600001, True, "10"):
            with self.subTest(timeout=bad):
                with self.assertRaises(RuntimeError):
                    self.server._validate_request({"command": "true", "timeout": bad}, policy)


class ArgvTests(Fixture):
    def argv(self, command: str = "systemctl --failed") -> list[str]:
        return self.server._build_ssh_argv(command, self.policy())

    def test_target_is_pinned_and_command_is_last(self) -> None:
        argv = self.argv()
        self.assertEqual(argv[0], str(self.ssh))
        self.assertEqual(argv[-3:], ["--", "jonathan@home-server", "systemctl --failed"])

    def test_hardening_options_present(self) -> None:
        joined = " ".join(self.argv())
        for option in (
            "-F /dev/null",
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            f"UserKnownHostsFile={self.known_hosts}",
            "GlobalKnownHostsFile=/dev/null",
            "UpdateHostKeys=no",
            f"IdentityAgent={self.agent}",
            "IdentityFile=none",
            "ForwardAgent=no",
            "ForwardX11=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "ProxyCommand=none",
            "ProxyJump=none",
            "ControlMaster=no",
            "ControlPath=none",
            "ConnectTimeout=10",
        ):
            with self.subTest(option=option):
                self.assertIn(option, joined)

    def test_command_text_cannot_become_ssh_options(self) -> None:
        argv = self.argv("-oProxyCommand=evil")
        self.assertEqual(argv[-1], "-oProxyCommand=evil")
        self.assertLess(argv.index("--"), argv.index("-oProxyCommand=evil"))

    def test_environment_is_minimal(self) -> None:
        env = self.server._ssh_env(self.policy())
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(set(env), {"PATH", "HOME", "LANG"})

    def test_missing_agent_socket_fails_closed(self) -> None:
        policy = self.policy()
        self.agent.unlink()
        with self.assertRaisesRegex(RuntimeError, "identity agent"):
            self.server._build_ssh_argv("true", policy)


class OutputTests(Fixture):
    def test_output_is_redacted_and_capped(self) -> None:
        body = self.server._compose_output(
            0, "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", "", 60000
        )
        self.assertNotIn("abc", body)
        self.assertIn("[REDACTED:private-key", body)
        long_body = self.server._compose_output(0, "x" * 500, "", 100)
        self.assertIn("output truncated", long_body)


class ToolTests(Fixture):
    def test_tool_runs_built_argv_without_shell(self) -> None:
        policy_file = Path(self.tmp.name) / "config.json"
        policy_file.write_text(json.dumps({"smarthomeSsh": self.raw_policy()}), encoding="utf-8")
        calls = []

        def fake_run(argv, env, timeout_ms, rec=None):
            calls.append((argv, env, timeout_ms))
            return 0, "home-server\n", ""

        with mock.patch.object(self.server, "CONFIG_PATH", str(policy_file)), \
                mock.patch.object(self.server, "_run_argv", fake_run):
            text = self.server._tool_ssh({"command": "hostname", "timeout": 5000})
        self.assertIn("home-server", text)
        self.assertIn("--- exit 0 ---", text)
        argv, env, timeout_ms = calls[0]
        self.assertEqual(argv[-1], "hostname")
        self.assertEqual(timeout_ms, 5000)
        self.assertNotIn("SSH_AUTH_SOCK", env)


class ShippedConfigTests(unittest.TestCase):
    def test_shipped_config_is_valid(self) -> None:
        server = load_server()
        data = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        raw = data["smarthomeSsh"]
        self.assertEqual(raw["host"], "home-server")
        self.assertEqual(raw["user"], "jonathan")
        with mock.patch.object(server, "_require_agent_socket", lambda policy: None), \
                mock.patch.object(server, "_require_executable", lambda path, key: path):
            server._load_policy(data, base_dir=HERE)


class StdioTests(unittest.TestCase):
    def test_initialize_and_list(self) -> None:
        frames = "\n".join(
            json.dumps(msg)
            for msg in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
        )
        proc = subprocess.run(
            [sys.executable, "-B", str(SERVER)],
            input=frames + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "smarthome-ssh")
        self.assertEqual([t["name"] for t in responses[1]["result"]["tools"]], ["ssh"])


if __name__ == "__main__":
    unittest.main()
