#!/usr/bin/env python3
"""Security contract tests for the Klaffat app-runner MCP."""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from unittest import mock


# The server and its helpers live under runtime/, the tree projected into
# ~/.claude; this suite does not. HERE therefore names the SERVER's
# directory, not this file's, so every path derived from it still resolves.
HERE = Path(__file__).resolve().parents[3] / "runtime" / "mcps" / "klaffat-app-runner"
SERVER = HERE / "server.py"
CONFIG = HERE / "config.json"
HOLD = HERE / "hold.py"
SANDBOX_EXEC = HERE / "sandbox_exec.py"
E2E_DEPS = HERE / "e2e_deps.py"


def load_server():
    name = f"klaffat_app_runner_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load app-runner server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def valid_policy() -> dict:
    return {
        "version": 1,
        "worktreeRoot": "/home/jonathan/worktrees",
        "klaffatPrimary": "/home/jonathan/Repos/klaffat",
        "allowedOrigins": [
            "git@github.com:klaffat/klaffat.git",
            "https://github.com/klaffat/klaffat.git",
        ],
        "stateRoot": "/home/jonathan/.local/state/klaffat-app-runner",
        "nixExecutable": "/run/current-system/sw/bin/nix",
        "bashExecutable": "/run/current-system/sw/bin/bash",
        "pythonExecutable": "/run/current-system/sw/bin/python3",
        "systemdRunExecutable": "/run/current-system/sw/bin/systemd-run",
        "systemctlExecutable": "/run/current-system/sw/bin/systemctl",
        "journalctlExecutable": "/run/current-system/sw/bin/journalctl",
        "realCalendarExecutable": "/run/current-system/sw/bin/klaffat-local-google",
        "userBusSocket": "%t/bus",
        "unitPrefix": "klaffat-app-runner",
        "commandTimeoutMs": 30000,
        "realStartTimeoutMs": 1800000,
        "maxLogLines": 1000,
        "maxOutputChars": 60000,
    }


class AssetBootstrapTests(unittest.TestCase):
    def test_server_and_policy_assets_exist(self) -> None:
        self.assertTrue(SERVER.is_file(), "missing app-runner server")
        self.assertTrue(CONFIG.is_file(), "missing app-runner policy")
        self.assertTrue(SANDBOX_EXEC.is_file(), "missing sandbox exec helper")


class ToolSurfaceTests(unittest.TestCase):
    def test_server_exposes_one_narrow_structured_tool(self) -> None:
        server = load_server()
        self.assertTrue(hasattr(server, "SERVER_INFO"), "missing MCP server identity")
        self.assertTrue(hasattr(server, "TOOLS"), "missing MCP tool metadata")
        self.assertEqual(server.SERVER_INFO["name"], "klaffat-app-runner")
        self.assertEqual([tool["name"] for tool in server.TOOLS], ["klaffat_app"])
        tool = server.TOOLS[0]
        for term in ("start", "mode", "worktree_path", "run_id", "spec"):
            self.assertIn(term, tool["description"])
        schema = tool["inputSchema"]
        self.assertEqual(schema["required"], ["operation"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["operation"]["enum"],
            ["start", "status", "logs", "http_get", "stop", "current"],
        )
        self.assertEqual(
            schema["properties"]["mode"]["enum"],
            ["app", "e2e", "claude_design_export", "real_calendar"],
        )
        for escape_field in (
            "command",
            "args",
            "argv",
            "env",
            "port",
            "executable",
            "output_path",
            "unit",
        ):
            self.assertNotIn(escape_field, schema["properties"])
        for field in schema["properties"].values():
            self.assertIn("description", field)


class PolicySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()

    def load(self, policy: dict):
        self.assertTrue(hasattr(self.server, "_load_policy"), "missing policy loader")
        return self.server._load_policy({"appRunner": policy})

    def test_policy_is_exact_and_normalized(self) -> None:
        loaded = self.load(valid_policy())
        self.assertEqual(loaded["version"], 1)
        self.assertEqual(loaded["worktreeRoot"], Path("/home/jonathan/worktrees"))
        self.assertEqual(loaded["allowedOrigins"], tuple(valid_policy()["allowedOrigins"]))
        self.assertEqual(loaded["userBusSocket"], Path(f"/run/user/{os.getuid()}/bus"))

    def test_missing_and_unknown_policy_keys_are_rejected(self) -> None:
        policy = valid_policy()
        del policy["stateRoot"]
        with self.assertRaisesRegex(RuntimeError, "stateRoot"):
            self.load(policy)
        policy = valid_policy()
        policy["futureUnsafeDefault"] = True
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.load(policy)

    def test_policy_declares_no_git_executable(self) -> None:
        # Worktree validation reads git metadata files directly and never
        # invokes git, so a gitExecutable would be unenforced policy.
        self.assertNotIn("gitExecutable", self.server._POLICY_KEYS)
        self.assertNotIn("gitExecutable", self.server._PATH_KEYS)
        policy = valid_policy()
        policy["gitExecutable"] = "/run/current-system/sw/bin/git"
        with self.assertRaisesRegex(RuntimeError, "gitExecutable"):
            self.load(policy)

    def test_policy_version_is_exact_integer_one(self) -> None:
        for version in (True, 1.0, 0, 2, "1"):
            with self.subTest(version=version):
                policy = valid_policy()
                policy["version"] = version
                with self.assertRaisesRegex(RuntimeError, "version"):
                    self.load(policy)

    def test_paths_must_be_absolute_and_origins_nonempty(self) -> None:
        for key in (
            "worktreeRoot",
            "klaffatPrimary",
            "stateRoot",
            "nixExecutable",
            "bashExecutable",
            "pythonExecutable",
            "systemdRunExecutable",
            "systemctlExecutable",
            "journalctlExecutable",
            "realCalendarExecutable",
            "userBusSocket",
        ):
            with self.subTest(key=key):
                policy = valid_policy()
                policy[key] = "relative/path"
                with self.assertRaisesRegex(RuntimeError, key):
                    self.load(policy)
        for origins in ([], ["same", "same"], [""], "not-a-list"):
            with self.subTest(origins=origins):
                policy = valid_policy()
                policy["allowedOrigins"] = origins
                with self.assertRaisesRegex(RuntimeError, "allowedOrigins"):
                    self.load(policy)

    def test_numeric_and_unit_bounds_are_strict(self) -> None:
        cases = {
            "commandTimeoutMs": [True, "30000", 0, 3600001],
            "realStartTimeoutMs": [True, "1800000", 0, 3600001],
            "maxLogLines": [True, "1000", 0, 1001],
            "maxOutputChars": [True, "60000", 0, 60001],
            "unitPrefix": ["", "UPPER", "bad/name", "x" * 65],
        }
        for key, bad_values in cases.items():
            for value in bad_values:
                with self.subTest(key=key, value=value):
                    policy = valid_policy()
                    policy[key] = value
                    with self.assertRaisesRegex(RuntimeError, key):
                        self.load(policy)

    def test_runtime_token_uses_process_uid_not_environment(self) -> None:
        policy = valid_policy()
        policy["userBusSocket"] = "%t/bus"
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp/attacker"}):
            loaded = self.load(policy)
        self.assertEqual(loaded["userBusSocket"], Path(f"/run/user/{os.getuid()}/bus"))


class RequestSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.policy = self.server._load_policy({"appRunner": valid_policy()})
        self.run_id = "20261005T130000Z-0123456789abcdef"

    def validate(self, request: dict):
        self.assertTrue(hasattr(self.server, "_validate_request"), "missing request validator")
        return self.server._validate_request(request, self.policy)

    def test_start_requires_mode_and_absolute_worktree(self) -> None:
        request = self.validate(
            {
                "operation": "start",
                "mode": "app",
                "worktree_path": "/home/jonathan/worktrees/klaffat-topic",
            }
        )
        self.assertEqual(request["mode"], "app")
        self.assertEqual(request["worktree_path"], Path("/home/jonathan/worktrees/klaffat-topic"))
        for bad in (
            {"operation": "start", "worktree_path": "/tmp/klaffat-x"},
            {"operation": "start", "mode": "app"},
            {"operation": "start", "mode": "shell", "worktree_path": "/tmp/x"},
            {"operation": "start", "mode": "app", "worktree_path": "relative"},
            {"operation": "start", "mode": "app", "worktree_path": ""},
        ):
            with self.subTest(request=bad), self.assertRaises(RuntimeError):
                self.validate(bad)

    def test_spec_is_optional_and_only_allowed_for_e2e(self) -> None:
        request = self.validate(
            {
                "operation": "start",
                "mode": "e2e",
                "worktree_path": "/home/jonathan/worktrees/klaffat-topic",
                "spec": "tests/e2e/tests/calendar.spec.ts",
            }
        )
        self.assertEqual(request["spec"], "tests/e2e/tests/calendar.spec.ts")
        for bad in (
            {
                "operation": "start",
                "mode": "app",
                "worktree_path": "/home/jonathan/worktrees/klaffat-topic",
                "spec": "tests/e2e/tests/calendar.spec.ts",
            },
            {
                "operation": "start",
                "mode": "e2e",
                "worktree_path": "/home/jonathan/worktrees/klaffat-topic",
                "spec": "",
            },
            {
                "operation": "start",
                "mode": "e2e",
                "worktree_path": "/home/jonathan/worktrees/klaffat-topic",
                "spec": True,
            },
        ):
            with self.subTest(request=bad), self.assertRaises(RuntimeError):
                self.validate(bad)

    def test_status_and_stop_accept_only_a_canonical_run_id(self) -> None:
        for operation in ("status", "stop"):
            self.assertEqual(
                self.validate({"operation": operation, "run_id": self.run_id})["run_id"],
                self.run_id,
            )
            for run_id in ("", "../x", "unit.service", True, "20261005T130000Z-ABC"):
                with self.subTest(operation=operation, run_id=run_id), self.assertRaises(
                    RuntimeError
                ):
                    self.validate({"operation": operation, "run_id": run_id})

    def test_logs_has_a_strict_bounded_tail(self) -> None:
        request = self.validate({"operation": "logs", "run_id": self.run_id})
        self.assertEqual(request["tail_lines"], 200)
        self.assertEqual(
            self.validate(
                {"operation": "logs", "run_id": self.run_id, "tail_lines": 17}
            )["tail_lines"],
            17,
        )
        for value in (True, "17", 0, 1001):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.validate(
                    {"operation": "logs", "run_id": self.run_id, "tail_lines": value}
                )

    def test_unknown_operations_and_escape_fields_reject(self) -> None:
        for request in (
            {},
            {"operation": "exec", "command": "id"},
            {"operation": "status", "run_id": self.run_id, "env": {"A": "B"}},
            {
                "operation": "start",
                "mode": "app",
                "worktree_path": "/home/jonathan/worktrees/klaffat-topic",
                "port": 9999,
            },
            {"operation": "stop", "run_id": self.run_id, "mode": "app"},
        ):
            with self.subTest(request=request), self.assertRaises(RuntimeError):
                self.validate(request)

    def test_current_accepts_no_caller_selected_handle(self) -> None:
        self.assertEqual(self.validate({"operation": "current"}), {"operation": "current"})
        for request in (
            {"operation": "current", "run_id": self.run_id},
            {"operation": "current", "mode": "real_calendar"},
        ):
            with self.subTest(request=request), self.assertRaises(RuntimeError):
                self.validate(request)


class WorktreeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.primary = base / "Repos" / "klaffat"
        self.root = base / "worktrees"
        self.worktree = self.root / "klaffat-topic"
        self.common = self.primary / ".git"
        self.gitdir = self.common / "worktrees" / "klaffat-topic"
        self.gitdir.mkdir(parents=True)
        self.root.mkdir()
        self.worktree.mkdir()
        (self.worktree / ".git").write_text(
            f"gitdir: {self.gitdir}\n", encoding="utf-8"
        )
        (self.gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        (self.gitdir / "gitdir").write_text(
            f"{self.worktree / '.git'}\n", encoding="utf-8"
        )
        (self.common / "config").write_text(
            '[remote "origin"]\n\turl = https://github.com/example/klaffat.git\n',
            encoding="utf-8",
        )
        self.spec_root = self.worktree / "tests" / "e2e" / "tests"
        self.spec_root.mkdir(parents=True)
        self.spec_file = self.spec_root / "calendar.spec.ts"
        self.spec_file.write_text("// fixture\n", encoding="utf-8")
        policy = valid_policy()
        policy["worktreeRoot"] = str(self.root)
        policy["klaffatPrimary"] = str(self.primary)
        policy["allowedOrigins"] = ["https://github.com/example/klaffat.git"]
        policy["stateRoot"] = str(base / "state")
        self.policy = self.server._load_policy({"appRunner": policy})

    def validate(self, path: Path):
        self.assertTrue(
            hasattr(self.server, "_validate_worktree"), "missing worktree validator"
        )
        return self.server._validate_worktree(path, self.policy)

    def validate_spec(self, spec: str):
        self.assertTrue(hasattr(self.server, "_validate_spec"), "missing spec validator")
        return self.server._validate_spec(self.worktree, spec)

    def test_accepts_expected_linked_klaffat_worktree(self) -> None:
        self.assertEqual(self.validate(self.worktree), self.worktree)

    def test_rejects_primary_nested_lookalike_and_symlink_paths(self) -> None:
        nested = self.worktree / "nested"
        nested.mkdir()
        lookalike = self.root / "other-topic"
        lookalike.mkdir()
        alias = self.root / "klaffat-alias"
        alias.symlink_to(self.worktree, target_is_directory=True)
        for path in (self.primary, nested, lookalike, alias, Path("relative")):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                self.validate(path)

    def test_rejects_normal_clone_and_other_common_directory(self) -> None:
        normal = self.root / "klaffat-normal"
        (normal / ".git").mkdir(parents=True)
        other = self.root / "klaffat-other"
        other_gitdir = Path(self.temp.name) / "other.git" / "worktrees" / "other"
        other_gitdir.mkdir(parents=True)
        other.mkdir()
        (other / ".git").write_text(f"gitdir: {other_gitdir}\n", encoding="utf-8")
        (other_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        for path in (normal, other):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                self.validate(path)

    def test_rejects_directory_forged_from_another_worktree_admin_dir(self) -> None:
        forged = self.root / "klaffat-forged"
        forged.mkdir()
        (forged / ".git").write_text(
            f"gitdir: {self.gitdir}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "back-pointer"):
            self.validate(forged)

    def test_rejects_unexpected_or_ambiguous_origin(self) -> None:
        config = self.common / "config"
        for text in (
            '[remote "origin"]\n\turl = https://github.com/attacker/klaffat.git\n',
            '[remote "upstream"]\n\turl = https://github.com/example/klaffat.git\n',
            '[remote "origin"]\n\turl = https://github.com/example/klaffat.git\n'
            '\turl = https://github.com/attacker/klaffat.git\n',
        ):
            with self.subTest(config=text):
                config.write_text(text, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    self.validate(self.worktree)

    def test_rejects_symlinked_git_metadata(self) -> None:
        git_file = self.worktree / ".git"
        git_file.unlink()
        target = Path(self.temp.name) / "git-pointer"
        target.write_text(f"gitdir: {self.gitdir}\n", encoding="utf-8")
        git_file.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.validate(self.worktree)

    def test_accepts_one_existing_top_level_e2e_spec(self) -> None:
        selected = self.validate_spec("tests/e2e/tests/calendar.spec.ts")
        self.assertEqual(selected, "tests/calendar.spec.ts")

    def test_rejects_spec_escape_missing_nested_and_non_spec_paths(self) -> None:
        nested = self.spec_root / "nested"
        nested.mkdir()
        (nested / "nested.spec.ts").write_text("// nested\n", encoding="utf-8")
        for spec in (
            "/etc/passwd",
            "../calendar.spec.ts",
            "tests/e2e/tests/../calendar.spec.ts",
            "tests/e2e/tests/nested/nested.spec.ts",
            "tests/e2e/tests/missing.spec.ts",
            "tests/e2e/tests/calendar.ts",
            "--grep=calendar",
        ):
            with self.subTest(spec=spec), self.assertRaises(RuntimeError):
                self.validate_spec(spec)

    def test_rejects_symlinked_spec(self) -> None:
        target = Path(self.temp.name) / "outside.spec.ts"
        target.write_text("// outside\n", encoding="utf-8")
        linked = self.spec_root / "linked.spec.ts"
        linked.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.validate_spec("tests/e2e/tests/linked.spec.ts")


class RunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        policy = valid_policy()
        policy["worktreeRoot"] = str(base / "worktrees")
        policy["klaffatPrimary"] = str(base / "Repos" / "klaffat")
        policy["stateRoot"] = str(base / "state")
        self.policy = self.server._load_policy({"appRunner": policy})
        self.run_id = "20261005T130000Z-0123456789abcdef"
        self.state = {
            "version": 1,
            "run_id": self.run_id,
            "mode": "app",
            "worktree_path": str(base / "worktrees" / "klaffat-topic"),
            "unit_name": f"klaffat-app-runner-{self.run_id}",
            "created_at": "2026-10-05T13:00:00Z",
            "phase": "accepted",
            "exit_code": None,
            "url": None,
            "artifact_path": None,
        }

    def write(self, state: dict, *, create: bool) -> None:
        self.assertTrue(hasattr(self.server, "_write_state"), "missing state writer")
        self.server._write_state(state, self.policy, create=create)

    def read(self) -> dict:
        self.assertTrue(hasattr(self.server, "_read_state"), "missing state reader")
        return self.server._read_state(self.run_id, self.policy)

    def test_generated_run_ids_are_canonical_and_unique(self) -> None:
        self.assertTrue(hasattr(self.server, "_new_run_id"), "missing run-id generator")
        first = self.server._new_run_id()
        second = self.server._new_run_id()
        self.assertRegex(first, r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$")
        self.assertNotEqual(first, second)

    def test_state_root_is_created_private_and_run_round_trips(self) -> None:
        self.write(self.state, create=True)
        root = self.policy["stateRoot"]
        state_path = root / f"{self.run_id}.json"
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertEqual(self.read(), self.state)
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.write(self.state, create=True)

    def test_unknown_run_id_is_not_guessed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unknown run_id"):
            self.read()

    def test_state_root_and_file_permissions_fail_closed(self) -> None:
        root = self.policy["stateRoot"]
        root.mkdir(mode=0o755)
        with self.assertRaisesRegex(RuntimeError, "private"):
            self.write(self.state, create=True)
        root.chmod(0o700)
        self.write(self.state, create=True)
        state_path = root / f"{self.run_id}.json"
        state_path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "private"):
            self.read()

    def test_symlinked_or_malformed_state_is_rejected(self) -> None:
        root = self.policy["stateRoot"]
        root.mkdir(mode=0o700)
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text(json.dumps(self.state), encoding="utf-8")
        state_path = root / f"{self.run_id}.json"
        state_path.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.read()
        state_path.unlink()
        malformed = dict(self.state, injected="value")
        state_path.write_text(json.dumps(malformed), encoding="utf-8")
        state_path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "schema"):
            self.read()

    def test_real_calendar_state_cannot_claim_a_unit(self) -> None:
        state = dict(self.state, mode="real_calendar", unit_name=None)
        self.write(state, create=True)
        self.assertEqual(self.read()["mode"], "real_calendar")
        state["unit_name"] = f"klaffat-app-runner-{self.run_id}"
        with self.assertRaisesRegex(RuntimeError, "unit_name"):
            self.write(state, create=False)

    def test_state_version_is_exact_integer_one(self) -> None:
        for version in (True, 1.0, 0, 2, "1"):
            with self.subTest(version=version):
                state = dict(self.state, version=version)
                with self.assertRaisesRegex(RuntimeError, "version"):
                    self.write(state, create=True)


class ModeArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.worktree = base / "worktrees" / "klaffat-topic"
        scripts = self.worktree / "scripts"
        scripts.mkdir(parents=True)
        for name in ("e2e-run", "export-claude-design.sh"):
            path = scripts / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
        policy = valid_policy()
        policy["worktreeRoot"] = str(base / "worktrees")
        policy["klaffatPrimary"] = str(base / "Repos" / "klaffat")
        policy["stateRoot"] = str(base / "state")
        self.policy = self.server._load_policy({"appRunner": policy})
        self.run_id = "20261005T130000Z-0123456789abcdef"

    def state(self, mode: str) -> dict:
        return {
            "version": 1,
            "run_id": self.run_id,
            "mode": mode,
            "worktree_path": str(self.worktree),
            "unit_name": None
            if mode == "real_calendar"
            else f"klaffat-app-runner-{self.run_id}",
            "created_at": "2026-10-05T13:00:00Z",
            "phase": "accepted",
            "exit_code": None,
            "url": None,
            "artifact_path": None,
        }

    def build(self, mode: str, spec: str | None = None) -> list[str]:
        self.assertTrue(hasattr(self.server, "_build_start_argv"), "missing argv builder")
        return self.server._build_start_argv(self.state(mode), self.policy, spec=spec)

    def test_real_calendar_uses_only_fixed_launcher_and_selected_worktree(self) -> None:
        self.assertEqual(
            self.build("real_calendar"),
            [str(self.policy["realCalendarExecutable"]), "start", str(self.worktree)],
        )

    def dev_shell_prefix(self) -> list[str]:
        return [
            str(self.policy["pythonExecutable"]),
            str(SANDBOX_EXEC),
            str(self.worktree / "target" / "klaffat-app-runner" / "home"),
            "--",
            str(self.policy["nixExecutable"]),
            "develop",
            str(self.worktree),
            "--command",
        ]

    def test_mock_app_runs_e2e_harness_with_fixed_hold_helper(self) -> None:
        argv = self.build("app")
        separator = argv.index("--")
        payload = argv[separator + 1 :]
        self.assertEqual(
            payload,
            self.dev_shell_prefix()
            + [
                str(self.policy["bashExecutable"]),
                str(self.worktree / "scripts" / "e2e-run"),
                "--command",
                "--",
                str(self.policy["pythonExecutable"]),
                str(HERE / "hold.py"),
            ],
        )

    def test_e2e_maps_only_the_optional_validated_spec(self) -> None:
        plain = self.build("e2e")
        selected = self.build("e2e", spec="tests/calendar.spec.ts")
        plain_payload = plain[plain.index("--") + 1 :]
        selected_payload = selected[selected.index("--") + 1 :]
        prefix = self.dev_shell_prefix() + [
            str(self.policy["pythonExecutable"]),
            str(E2E_DEPS),
            "--",
            str(self.policy["bashExecutable"]),
            str(self.worktree / "scripts" / "e2e-run"),
        ]
        self.assertEqual(plain_payload, prefix)
        self.assertEqual(selected_payload, prefix + ["--", "tests/calendar.spec.ts"])

    def test_design_export_uses_only_the_repository_entry_point(self) -> None:
        argv = self.build("claude_design_export")
        self.assertEqual(
            argv[argv.index("--") + 1 :],
            self.dev_shell_prefix()
            + [
                str(self.policy["pythonExecutable"]),
                str(E2E_DEPS),
                "--",
                str(self.policy["bashExecutable"]),
                str(self.worktree / "scripts" / "export-claude-design.sh"),
            ],
        )

    def test_transient_unit_name_properties_and_environment_are_derived(self) -> None:
        argv = self.build("app")
        unit = f"klaffat-app-runner-{self.run_id}"
        runtime_name = f"k-{self.run_id[-10:]}"
        runtime = f"/run/user/{os.getuid()}/{runtime_name}"
        self.assertEqual(argv[0], str(self.policy["systemdRunExecutable"]))
        self.assertIn("--user", argv)
        self.assertIn(f"--unit={unit}", argv)
        self.assertIn(f"--working-directory={self.worktree}", argv)
        for prop in (
            "KillMode=control-group",
            "RemainAfterExit=yes",
            "UMask=0077",
            "NoNewPrivileges=yes",
            "PrivateDevices=yes",
            "PrivateTmp=yes",
            f"TemporaryFileSystem=/run/user/{os.getuid()}:mode=0700,uid={os.getuid()},gid={os.getgid()}",
            "ProtectHome=tmpfs",
            "ProtectSystem=strict",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            f"BindPaths={self.worktree}",
            f"BindReadOnlyPaths={self.policy['klaffatPrimary'] / '.git'}",
            f"BindReadOnlyPaths={HERE}",
            f"RuntimeDirectory={runtime_name}",
            "RuntimeDirectoryMode=0700",
            "RuntimeDirectoryPreserve=yes",
        ):
            self.assertIn(f"--property={prop}", argv)
        inaccessible = next(
            item for item in argv if item.startswith("--property=InaccessiblePaths=")
        )
        for path in (
            f"/run/user/{os.getuid()}/bus",
            f"/run/user/{os.getuid()}/systemd",
            "/run/dbus/system_bus_socket",
            "/run/systemd/private",
        ):
            self.assertIn(path, inaccessible)
        home = self.worktree / "target" / "klaffat-app-runner" / "home"
        self.assertIn(f"--setenv=HOME={home}", argv)
        # A user manager cannot enforce eBPF IP filters; claiming one would lie.
        self.assertNotIn("IPAddress", joined := "\n".join(argv))
        self.assertIn(f"--setenv=XDG_RUNTIME_DIR={runtime}", argv)
        self.assertNotIn("sh -c", joined)
        self.assertNotIn("EnvironmentFile", joined)


class HoldHelperTests(unittest.TestCase):
    def test_helper_waits_for_signal_and_exits_cleanly(self) -> None:
        self.assertTrue(HOLD.is_file(), "missing fixed app hold helper")
        proc = subprocess.Popen(
            [sys.executable, str(HOLD)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        self.assertEqual(proc.stdout.readline().strip(), "klaffat-app-runner: holding app")
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, (stdout, stderr))
        self.assertEqual(stderr, "")


class SandboxExecHelperTests(unittest.TestCase):
    def test_helper_tightens_only_an_owned_real_directory(self) -> None:
        name = f"klaffat_sandbox_exec_test_{os.getpid()}_{id(object())}"
        spec = importlib.util.spec_from_file_location(name, SANDBOX_EXEC)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            runtime.mkdir(mode=0o755)
            module.prepare_runtime_parent(runtime, os.getuid())
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
            link = Path(raw) / "link"
            link.symlink_to(runtime, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "real directory"):
                module.prepare_runtime_parent(link, os.getuid())


class FakeRunner:
    def __init__(self, *responses: tuple[int, str, str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, str], int]] = []

    def __call__(self, argv, env, timeout_ms, rec=None, *, settle=None):
        self.calls.append((list(argv), dict(env), timeout_ms))
        if not self.responses:
            raise AssertionError(f"unexpected runner call: {argv}")
        return self.responses.pop(0)


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.worktree = base / "worktrees" / "klaffat-topic"
        scripts = self.worktree / "scripts"
        specs = self.worktree / "tests" / "e2e" / "tests"
        scripts.mkdir(parents=True)
        specs.mkdir(parents=True)
        for name in ("e2e-run", "export-claude-design.sh"):
            path = scripts / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
        (specs / "calendar.spec.ts").write_text("// fixture\n", encoding="utf-8")
        policy = valid_policy()
        policy["worktreeRoot"] = str(base / "worktrees")
        policy["klaffatPrimary"] = str(base / "Repos" / "klaffat")
        policy["stateRoot"] = str(base / "state")
        self.policy = self.server._load_policy({"appRunner": policy})
        runtime_parent = base / "run"
        runtime_parent.mkdir()
        self.policy["userBusSocket"] = runtime_parent / "bus"
        self.first_id = "20261005T130000Z-0123456789abcdef"
        self.second_id = "20261005T130001Z-fedcba9876543210"

    def start(self, mode: str, runner: FakeRunner, *, run_id=None, spec=None):
        self.assertTrue(hasattr(self.server, "_start_run"), "missing start lifecycle")
        request = {"operation": "start", "mode": mode, "worktree_path": self.worktree}
        if spec is not None:
            request["spec"] = spec
        with (
            mock.patch.object(self.server, "_validate_worktree", return_value=self.worktree),
            mock.patch.object(self.server, "_new_run_id", return_value=run_id or self.first_id),
        ):
            return self.server._start_run(request, self.policy, runner=runner)

    def test_start_accepts_transient_app_and_records_owned_state(self) -> None:
        runner = FakeRunner((0, "Running as unit", ""))
        state = self.start("app", runner)
        self.assertEqual(state["phase"], "accepted")
        self.assertEqual(state["run_id"], self.first_id)
        self.assertEqual(
            self.server._read_state(self.first_id, self.policy), state
        )
        self.assertEqual(runner.calls[0][0][0], str(self.policy["systemdRunExecutable"]))
        self.assertNotIn("SHELL", runner.calls[0][1])

    def test_failed_transient_acceptance_remains_queryable(self) -> None:
        runner = FakeRunner((5, "", "unit refused"))
        state = self.start("e2e", runner)
        self.assertEqual((state["phase"], state["exit_code"]), ("failed", 5))
        self.assertEqual(self.server._read_state(self.first_id, self.policy), state)

    def test_real_calendar_success_is_active_and_supersedes_prior_run(self) -> None:
        first = self.start("real_calendar", FakeRunner((0, "ready", "")))
        self.assertEqual((first["phase"], first["url"]), ("active", "http://localhost:3740"))
        second = self.start(
            "real_calendar",
            FakeRunner((0, "ready", "")),
            run_id=self.second_id,
        )
        self.assertEqual(second["phase"], "active")
        self.assertEqual(self.server._read_state(self.first_id, self.policy)["phase"], "stopped")

    def test_real_calendar_refuses_dangling_owner_before_host_launch(self) -> None:
        self.start("real_calendar", FakeRunner((0, "ready", "")))
        (self.policy["stateRoot"] / f"{self.first_id}.json").unlink()
        runner = FakeRunner((0, "must not run", ""))
        with self.assertRaisesRegex(RuntimeError, "unknown run_id"):
            self.start(
                "real_calendar",
                runner,
                run_id=self.second_id,
            )
        self.assertEqual(runner.calls, [])

    def test_real_calendar_persists_recoverable_owner_before_host_launch(self) -> None:
        runner = FakeRunner((0, "must not run", ""))
        with mock.patch.object(
            self.server,
            "_write_real_owner",
            side_effect=RuntimeError("owner unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "owner unavailable"):
                self.start("real_calendar", runner)
        self.assertEqual(runner.calls, [])

    def test_real_calendar_cancellation_does_not_interrupt_activation(self) -> None:
        launcher = Path(self.temp.name) / "real-calendar-launcher"
        launcher.write_text("#!/bin/sh\nsleep 0.2\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o700)
        self.policy["realCalendarExecutable"] = launcher
        rec = self.server._InFlight()
        request_id = "real-start"
        with self.server._in_flight_lock:
            self.server._in_flight[request_id] = rec
        result = []
        request = {
            "operation": "start",
            "mode": "real_calendar",
            "worktree_path": self.worktree,
        }

        def activate() -> None:
            with (
                mock.patch.object(
                    self.server, "_validate_worktree", return_value=self.worktree
                ),
                mock.patch.object(
                    self.server, "_new_run_id", return_value=self.first_id
                ),
            ):
                result.append(
                    self.server._start_run(
                        request,
                        self.policy,
                        runner=self.server._run_argv,
                        rec=rec,
                    )
                )

        thread = threading.Thread(target=activate)
        thread.start()
        deadline = time.monotonic() + 5
        while rec.proc is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(rec.proc, "real launcher never started")
        self.server._on_cancelled(request_id)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "real activation did not settle")
        self.assertEqual(result[0]["phase"], "active")
        self.assertEqual(self.server._read_real_owner(self.policy), self.first_id)
        self.assertEqual(
            self.server._current_real_state(self.policy)["run_id"], self.first_id
        )
        with self.server._in_flight_lock:
            self.server._in_flight.pop(request_id, None)

    def start_slow_real_calendar(self, exit_code: int) -> dict:
        launcher = Path(self.temp.name) / "real-calendar-launcher"
        launcher.write_text(f"#!/bin/sh\nsleep 0.4\nexit {exit_code}\n", encoding="utf-8")
        launcher.chmod(0o700)
        self.policy["realCalendarExecutable"] = launcher
        self.policy["realStartTimeoutMs"] = 50
        request = {"operation": "start", "mode": "real_calendar", "worktree_path": self.worktree}
        with (
            mock.patch.object(self.server, "_validate_worktree", return_value=self.worktree),
            mock.patch.object(self.server, "_new_run_id", return_value=self.first_id),
        ):
            return self.server._start_run(
                request, self.policy, runner=self.server._run_argv, rec=self.server._InFlight()
            )

    def wait_for_phase(self, phase: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.server._read_state(self.first_id, self.policy)
            if state["phase"] == phase:
                return state
            time.sleep(0.02)
        self.fail(f"run never reached {phase}")

    def test_real_calendar_start_past_its_deadline_keeps_ownership_until_settled(self) -> None:
        state = self.start_slow_real_calendar(0)
        self.assertEqual((state["phase"], state["exit_code"]), ("accepted", None))
        self.assertEqual(self.server._read_real_owner(self.policy), self.first_id)
        # While the launcher still runs, status reports it unchanged without
        # querying the host, and stop refuses to race the activation.
        polled = self.server._status_run(self.first_id, self.policy, runner=FakeRunner())
        self.assertEqual(polled["phase"], "accepted")
        with self.assertRaisesRegex(RuntimeError, "still settling"):
            self.server._stop_run(self.first_id, self.policy, runner=FakeRunner())
        settled = self.wait_for_phase("active")
        self.assertEqual(settled["url"], "http://localhost:3740")
        self.assertEqual(self.server._current_real_state(self.policy)["run_id"], self.first_id)
        stopped = self.server._stop_run(
            self.first_id, self.policy, runner=FakeRunner((0, "", ""))
        )
        self.assertEqual(stopped["phase"], "stopped")

    def test_second_real_calendar_start_is_refused_while_one_settles(self) -> None:
        self.start_slow_real_calendar(0)
        runner = FakeRunner()
        with self.assertRaisesRegex(RuntimeError, "still settling"):
            self.start("real_calendar", runner, run_id=self.second_id)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.server._read_real_owner(self.policy), self.first_id)
        self.wait_for_phase("active")

    def test_real_calendar_start_failing_after_its_deadline_releases_ownership(self) -> None:
        self.start_slow_real_calendar(3)
        failed = self.wait_for_phase("failed")
        self.assertEqual(failed["exit_code"], 3)
        self.assertIsNone(self.server._read_real_owner(self.policy))
        polled = self.server._status_run(self.first_id, self.policy, runner=FakeRunner())
        self.assertEqual((polled["phase"], polled["exit_code"]), ("failed", 3))

    def test_status_snapshot_cannot_overwrite_a_concurrent_stop(self) -> None:
        self.start("app", FakeRunner((0, "accepted", "")))
        stopper = threading.Thread(
            target=self.server._stop_run,
            args=(self.first_id, self.policy),
            kwargs={"runner": FakeRunner((0, "", ""), (1, "", ""))},
        )
        show = (
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
            "Result=success\nExecMainStatus=0\n"
        )

        class StopDuringStatus(FakeRunner):
            def __call__(inner, argv, env, timeout_ms, rec=None, *, settle=None):
                if len(inner.calls) == 0:
                    stopper.start()
                    time.sleep(0.2)
                    self.assertTrue(stopper.is_alive(), "stop did not wait for status")
                return super().__call__(argv, env, timeout_ms, rec)

        status = StopDuringStatus((0, show, ""), (0, self.server._READY_MARKER + "\n", ""))
        self.assertEqual(
            self.server._status_run(self.first_id, self.policy, runner=status)["phase"],
            "running",
        )
        stopper.join(timeout=5)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(self.server._read_state(self.first_id, self.policy)["phase"], "stopped")

    def test_status_maps_running_url_success_and_failure(self) -> None:
        self.start("app", FakeRunner((0, "accepted", "")))
        running = FakeRunner(
            (
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n",
                "",
            ),
            (
                0,
                "application: http://127.0.0.1:41234\n"
                "klaffat-app-runner: holding app\n",
                "",
            ),
        )
        self.assertTrue(hasattr(self.server, "_status_run"), "missing status lifecycle")
        state = self.server._status_run(self.first_id, self.policy, runner=running)
        self.assertEqual((state["phase"], state["url"]), ("running", "http://127.0.0.1:41234"))
        # Discovery reads the whole unit journal: a cold build prints thousands
        # of lines between the early URL line and readiness.
        discovery = running.calls[1][0]
        self.assertFalse(any(arg.startswith("--lines") for arg in discovery), discovery)

        state.update(phase="accepted", exit_code=None)
        self.server._write_state(state, self.policy, create=False)
        building = FakeRunner(
            (
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n",
                "",
            ),
            (0, "application: http://127.0.0.1:41234\n", ""),
        )
        state = self.server._status_run(self.first_id, self.policy, runner=building)
        self.assertEqual((state["phase"], state["url"]), ("accepted", "http://127.0.0.1:41234"))

        state["mode"] = "e2e"
        self.server._write_state(state, self.policy, create=False)
        succeeded = FakeRunner(
            (
                0,
                "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n",
                "",
            ),
            (0, "e2e complete\n", ""),
        )
        state = self.server._status_run(self.first_id, self.policy, runner=succeeded)
        self.assertEqual((state["phase"], state["exit_code"]), ("succeeded", 0))

        failed = FakeRunner(
            (
                0,
                "LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=9\n",
                "",
            ),
            (0, "failure artifacts: /tmp/e2e-artifacts/run\n", ""),
        )
        state = self.server._status_run(self.first_id, self.policy, runner=failed)
        self.assertEqual((state["phase"], state["exit_code"]), ("failed", 9))
        self.assertEqual(state["artifact_path"], "/tmp/e2e-artifacts/run")

    def test_status_wait_blocks_until_the_app_is_ready(self) -> None:
        self.start("app", FakeRunner((0, "accepted", "")))
        show = (
            0,
            "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n",
            "",
        )
        building = (1, "application: http://127.0.0.1:41234\n", "")
        ready = (0, "application: http://127.0.0.1:41234\nklaffat-app-runner: holding app\n", "")
        sleeps: list[float] = []
        runner = FakeRunner(show, building, show, building, show, ready)
        state = self.server._await_status(
            self.first_id, self.policy, wait_s=60, runner=runner, sleep=sleeps.append
        )
        self.assertEqual((state["phase"], state["url"]), ("running", "http://127.0.0.1:41234"))
        self.assertEqual(len(sleeps), 2)
        # wait_s=0 is a single snapshot even while the build is still running.
        self.server._write_state({**state, "phase": "accepted"}, self.policy, create=False)
        once = self.server._await_status(
            self.first_id, self.policy, wait_s=0, runner=FakeRunner(show, building),
            sleep=sleeps.append,
        )
        self.assertEqual(once["phase"], "accepted")
        for bad in (-1, 601, True, "5"):
            with self.subTest(wait_s=bad), self.assertRaisesRegex(RuntimeError, "wait_s"):
                self.server._validate_request(
                    {"operation": "status", "run_id": self.first_id, "wait_s": bad},
                    self.policy,
                )

    def test_stop_targets_only_recorded_unit_and_is_idempotent(self) -> None:
        self.start("app", FakeRunner((0, "accepted", "")))
        runtime = self.policy["userBusSocket"].parent / f"k-{self.first_id[-10:]}"
        runtime.mkdir(mode=0o700)
        (runtime / "artifact").write_text("temporary", encoding="utf-8")
        # reset-failed exits 1 for a unit that is not failed; stop still succeeds.
        runner = FakeRunner((0, "", ""), (1, "", "not failed"))
        self.assertTrue(hasattr(self.server, "_stop_run"), "missing stop lifecycle")
        state = self.server._stop_run(self.first_id, self.policy, runner=runner)
        self.assertEqual(state["phase"], "stopped")
        unit = f"klaffat-app-runner-{self.first_id}.service"
        systemctl = [str(self.policy["systemctlExecutable"]), "--user"]
        self.assertEqual(
            [call[0] for call in runner.calls],
            [systemctl + ["stop", unit], systemctl + ["reset-failed", unit]],
        )
        self.assertFalse(runtime.exists())
        again = self.server._stop_run(
            self.first_id, self.policy, runner=FakeRunner()
        )
        self.assertEqual(again["phase"], "stopped")

    def test_stop_keeps_the_exit_code_a_finished_run_reported(self) -> None:
        state = self.start("e2e", FakeRunner((0, "accepted", "")))
        state.update(phase="failed", exit_code=1)
        self.server._write_state(state, self.policy, create=False)
        stopped = self.server._stop_run(
            self.first_id, self.policy, runner=FakeRunner((0, "", ""), (0, "", ""))
        )
        self.assertEqual((stopped["phase"], stopped["exit_code"]), ("stopped", 1))

    def test_only_active_real_calendar_owner_can_stop_singleton(self) -> None:
        self.start("real_calendar", FakeRunner((0, "ready", "")))
        self.start(
            "real_calendar",
            FakeRunner((0, "ready", "")),
            run_id=self.second_id,
        )
        # Superseded ownership is already recorded as stopped. The old run's
        # idempotent stop must not invoke the singleton launcher or affect the
        # new owner.
        old = self.server._stop_run(self.first_id, self.policy, runner=FakeRunner())
        self.assertEqual(old["phase"], "stopped")
        runner = FakeRunner((0, "", ""))
        stopped = self.server._stop_run(self.second_id, self.policy, runner=runner)
        self.assertEqual(stopped["phase"], "stopped")
        self.assertEqual(
            runner.calls[0][0],
            [str(self.policy["realCalendarExecutable"]), "stop"],
        )


class RedactionAndLogsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        policy = valid_policy()
        policy["worktreeRoot"] = str(base / "worktrees")
        policy["klaffatPrimary"] = str(base / "Repos" / "klaffat")
        policy["stateRoot"] = str(base / "state")
        self.policy = self.server._load_policy({"appRunner": policy})
        self.run_id = "20261005T130000Z-0123456789abcdef"
        self.state = {
            "version": 1,
            "run_id": self.run_id,
            "mode": "app",
            "worktree_path": str(base / "worktrees" / "klaffat-topic"),
            "unit_name": f"klaffat-app-runner-{self.run_id}",
            "created_at": "2026-10-05T13:00:00Z",
            "phase": "running",
            "exit_code": None,
            "url": "http://127.0.0.1:41234",
            "artifact_path": None,
        }
        self.server._write_state(self.state, self.policy, create=True)

    def test_redacts_provider_credentials_tokens_headers_and_capabilities(self) -> None:
        self.assertTrue(hasattr(self.server, "_safe_output"), "missing output redactor")
        canaries = [
            "google-client-canary",
            "google-secret-canary",
            "access-canary",
            "refresh-canary",
            "id-canary",
            "bearer-canary",
            "cookie-canary",
            "oauth-code-canary",
            "state-canary",
            "poll-capability-canary",
        ]
        raw = "\n".join(
            [
                "KLAFFAT_GOOGLE_CLIENT_ID=google-client-canary",
                "KLAFFAT_GOOGLE_CLIENT_SECRET=google-secret-canary",
                '{"access_token":"access-canary","refresh_token":"refresh-canary","id_token":"id-canary"}',
                "Authorization: Bearer bearer-canary",
                "Cookie: session=cookie-canary",
                "redirect=https://example.test/cb?code=oauth-code-canary&state=state-canary",
                "url=http://localhost:3740/p/poll-capability-canary/results",
                "application: http://127.0.0.1:41234",
                "failure artifacts: /tmp/klaffat/e2e-artifacts/run",
                "exit_code=9",
            ]
        )
        safe = self.server._safe_output(raw, self.policy)
        for canary in canaries:
            with self.subTest(canary=canary):
                self.assertNotIn(canary, safe)
        self.assertIn("[REDACTED:", safe)
        self.assertIn("application: http://127.0.0.1:41234", safe)
        self.assertIn("failure artifacts: /tmp/klaffat/e2e-artifacts/run", safe)
        self.assertIn("exit_code=9", safe)

    def test_output_is_hard_bounded_after_redaction(self) -> None:
        policy = dict(self.policy, maxOutputChars=120)
        safe = self.server._safe_output("x" * 1000, policy)
        self.assertLessEqual(len(safe), 120)
        self.assertTrue(safe.endswith("[output truncated]"))

    def test_output_bound_holds_below_the_truncation_marker(self) -> None:
        for limit in (1, 8, len("\n[output truncated]")):
            with self.subTest(limit=limit):
                policy = dict(self.policy, maxOutputChars=limit)
                safe = self.server._safe_output("x" * 1000, policy)
                self.assertLessEqual(len(safe), limit)

    def test_logs_uses_exact_owned_unit_and_requested_tail(self) -> None:
        self.assertTrue(hasattr(self.server, "_logs_run"), "missing logs lifecycle")
        runner = FakeRunner(
            (0, "Authorization: Bearer secret-value\nordinary line\n", "")
        )
        output = self.server._logs_run(
            self.run_id, self.policy, tail_lines=17, runner=runner
        )
        self.assertNotIn("secret-value", output)
        self.assertIn("ordinary line", output)
        self.assertEqual(
            runner.calls[0][0],
            [
                str(self.policy["journalctlExecutable"]),
                "--user",
                f"--unit=klaffat-app-runner-{self.run_id}.service",
                "--no-pager",
                "--output=cat",
                "--lines=17",
            ],
        )

    def test_real_calendar_logs_delegate_only_to_fixed_status(self) -> None:
        real = dict(
            self.state,
            mode="real_calendar",
            unit_name=None,
            phase="active",
            url="http://localhost:3740",
        )
        self.server._write_state(real, self.policy, create=False)
        runner = FakeRunner((0, "service active\n", ""))
        output = self.server._logs_run(
            self.run_id, self.policy, tail_lines=5, runner=runner
        )
        self.assertEqual(output, "service active")
        self.assertEqual(
            runner.calls[0][0],
            [str(self.policy["realCalendarExecutable"]), "status"],
        )


class _LocalApp(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "http://198.51.100.7/elsewhere")
            self.end_headers()
            return
        if self.path == "/callback":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/done?code=LOCATIONSECRET")
            self.end_headers()
            return
        if self.path == "/logo.png":
            body = b"\x89PNG\r\n"
            content_type = "image/png"
        elif self.path == "/token":
            body = (
                b'{"access_token": "BODYSECRET", "note": "ok"}\n'
                b"Authorization: Bearer HEADERSECRET\n"
            )
            content_type = "application/json"
        elif self.path == "/quotes":
            body = b'"' * 50_000
            content_type = "text/plain"
        else:
            body = f"<h1>klaffat home</h1> path={self.path}".encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200 if self.path != "/missing" else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


class HttpGetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        policy = valid_policy()
        policy["worktreeRoot"] = str(base / "worktrees")
        policy["klaffatPrimary"] = str(base / "Repos" / "klaffat")
        policy["stateRoot"] = str(base / "state")
        self.policy = self.server._load_policy({"appRunner": policy})
        self.app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LocalApp)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.run_id = "20261005T130000Z-0123456789abcdef"
        self.state = {
            "version": 1,
            "run_id": self.run_id,
            "mode": "app",
            "worktree_path": str(base / "worktrees" / "klaffat-topic"),
            "unit_name": f"klaffat-app-runner-{self.run_id}",
            "created_at": "2026-10-05T13:00:00Z",
            "phase": "running",
            "exit_code": None,
            "url": f"http://127.0.0.1:{self.app.server_address[1]}",
            "artifact_path": None,
        }
        self.server._write_state(self.state, self.policy, create=True)

    def get(self, path: str) -> dict:
        request = self.server._validate_request(
            {"operation": "http_get", "run_id": self.run_id, "path": path}, self.policy
        )
        return self.server._http_get(request["run_id"], request["path"], self.policy)

    def test_fetches_text_from_the_runs_own_origin(self) -> None:
        result = self.get("/p?x=1")
        self.assertEqual(result["status"], 200)
        self.assertIn("klaffat home", result["body"])
        self.assertFalse(result["truncated"])
        self.assertEqual(self.get("/missing")["status"], 404)

    def test_redirects_are_reported_not_followed(self) -> None:
        result = self.get("/moved")
        self.assertEqual(result["status"], 302)
        self.assertEqual(result["location"], "http://198.51.100.7/elsewhere")
        self.assertNotIn("klaffat home", result["body"])

    def tool_output(self, path: str) -> dict:
        text = self.server._http_output(self.get(path), self.policy)
        self.assertLessEqual(len(text), self.policy["maxOutputChars"])
        return json.loads(text)

    def test_body_secrets_are_redacted_before_serialization(self) -> None:
        result = self.tool_output("/token")
        self.assertNotIn("BODYSECRET", result["body"])
        self.assertNotIn("HEADERSECRET", result["body"])
        self.assertIn('"note": "ok"', result["body"])
        self.assertIn("Authorization: ", result["body"])

    def test_redacted_location_keeps_the_output_valid_json(self) -> None:
        result = self.tool_output("/callback")
        self.assertEqual(result["status"], 302)
        self.assertTrue(result["location"].startswith("http://127.0.0.1/done?code="))
        self.assertNotIn("LOCATIONSECRET", result["location"])

    def test_escape_heavy_body_is_shortened_not_cut(self) -> None:
        result = self.tool_output("/quotes")
        self.assertTrue(result["truncated"])
        self.assertTrue(set(result["body"]) <= {'"'})
        self.assertGreater(len(result["body"]), 0)

    def test_output_too_small_for_any_result_fails_closed(self) -> None:
        policy = dict(self.policy, maxOutputChars=20)
        with self.assertRaisesRegex(RuntimeError, "too small"):
            self.server._http_output(self.get("/p"), policy)

    def test_json_authorization_field_is_redacted(self) -> None:
        redacted = self.server._redact_output('{"authorization": "Bearer JSONAUTH"}')
        self.assertNotIn("JSONAUTH", redacted)

    def test_secret_cut_by_the_read_limit_is_still_redacted(self) -> None:
        redacted = self.server._redact_output('{"access_token": "PARTIALSECR')
        self.assertTrue(redacted.startswith('{"access_token": "'))
        self.assertNotIn("PARTIALSECR", redacted)

    def test_query_redaction_stops_at_quotes(self) -> None:
        redacted = self.server._redact_output('{"next": "/cb?code=QSECRET", "n": 1}')
        self.assertNotIn("QSECRET", redacted)
        self.assertTrue(redacted.endswith('", "n": 1}'))

    def test_binary_bodies_are_summarized(self) -> None:
        self.assertIn("omitted", self.get("/logo.png")["body"])

    def test_caller_cannot_choose_another_origin(self) -> None:
        for path in ("//198.51.100.7/x", "http://198.51.100.7/", "/a#frag", "/ a", "x", ""):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "path must"):
                self.get(path)
        with self.assertRaisesRegex(RuntimeError, "unexpected fields"):
            self.server._validate_request(
                {"operation": "http_get", "run_id": self.run_id, "url": "http://x/"},
                self.policy,
            )

    def test_refuses_runs_without_a_ready_loopback_origin(self) -> None:
        self.state["phase"] = "accepted"
        self.server._write_state(self.state, self.policy, create=False)
        with self.assertRaisesRegex(RuntimeError, "no ready app"):
            self.get("/")
        self.state.update(phase="running", url="http://198.51.100.7:80")
        self.server._write_state(self.state, self.policy, create=False)
        with self.assertRaisesRegex(RuntimeError, "not a loopback origin"):
            self.get("/")


class HelperProcessTests(unittest.TestCase):
    def test_sandbox_exec_creates_private_home_and_refuses_links(self) -> None:
        name = f"klaffat_sandbox_exec_home_{os.getpid()}_{id(object())}"
        spec = importlib.util.spec_from_file_location(name, SANDBOX_EXEC)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "target" / "klaffat-app-runner" / "home"
            module.prepare_home(home, os.getuid())
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            (Path(raw) / "elsewhere").mkdir()
            (Path(raw) / "linked").symlink_to(Path(raw) / "elsewhere")
            with self.assertRaisesRegex(RuntimeError, "canonical"):
                module.prepare_home(Path(raw) / "linked" / "home", os.getuid())

    def test_e2e_deps_installs_lockfile_before_running_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            sandbox = Path(raw)
            (sandbox / "tests" / "e2e").mkdir(parents=True)
            bin_dir = sandbox / "bin"
            bin_dir.mkdir()
            npm = bin_dir / "npm"
            npm.write_text(
                '#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" > "$NPM_LOG"\nexit "$NPM_EXIT"\n',
                encoding="utf-8",
            )
            npm.chmod(0o700)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                   "NPM_LOG": str(sandbox / "npm.log")}
            command = [sys.executable, str(E2E_DEPS), "--", "/bin/sh", "-c", "echo suite-ran"]
            ok = subprocess.run(command, cwd=sandbox, env={**env, "NPM_EXIT": "0"},
                                capture_output=True, text=True, timeout=10)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertIn("suite-ran", ok.stdout)
            cwd, args = (sandbox / "npm.log").read_text().strip().split("|")
            self.assertEqual(Path(cwd).resolve(), (sandbox / "tests" / "e2e").resolve())
            self.assertTrue(args.startswith("ci"))
            failed = subprocess.run(command, cwd=sandbox, env={**env, "NPM_EXIT": "3"},
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            self.assertNotIn("suite-ran", failed.stdout)


class ProcessCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()

    def test_cancellation_terminates_only_owned_process_group(self) -> None:
        self.assertTrue(hasattr(self.server, "_InFlight"), "missing in-flight state")
        self.assertTrue(hasattr(self.server, "_run_argv"), "missing argv runner")
        self.assertTrue(hasattr(self.server, "_on_cancelled"), "missing cancellation handler")
        rec = self.server._InFlight()
        request_id = "cancel-me"
        with self.server._in_flight_lock:
            self.server._in_flight[request_id] = rec
        result = []

        def run_child() -> None:
            result.append(
                self.server._run_argv(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    dict(os.environ),
                    30000,
                    rec,
                )
            )

        thread = threading.Thread(target=run_child)
        thread.start()
        deadline = time.monotonic() + 5
        while rec.proc is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(rec.proc, "child never started")
        pid = rec.proc.pid
        self.server._on_cancelled(request_id)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "cancelled child did not exit")
        self.assertTrue(rec.cancelled)
        self.assertNotEqual(result[0][0], 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.server._in_flight_lock:
            self.server._in_flight.pop(request_id, None)

    def test_cancelled_request_response_is_suppressed(self) -> None:
        rec = self.server._InFlight()
        rec.cancelled = True
        with self.server._in_flight_lock:
            self.server._in_flight[7] = rec
        output = StringIO()
        with mock.patch.object(sys, "stdout", output):
            self.server._send_response(7, result={"ok": True})
        self.assertEqual(output.getvalue(), "")
        with self.server._in_flight_lock:
            self.assertNotIn(7, self.server._in_flight)

    def test_noninterruptible_launcher_timeout_returns_at_deadline(self) -> None:
        rec = self.server._InFlight()
        rec.interruptible = False
        started = time.monotonic()
        return_code, stdout, stderr = self.server._run_argv(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            dict(os.environ),
            10,
            rec,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(return_code, 124)
        self.assertEqual(stdout, "")
        self.assertIn("allowed to settle", stderr)
        self.assertLess(elapsed, 0.25)


class McpProtocolTests(unittest.TestCase):
    def test_stdio_initialize_and_tool_list(self) -> None:
        messages = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                "",
            ]
        )
        completed = subprocess.run(
            [sys.executable, str(SERVER)],
            input=messages,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        frames = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(frames[0]["result"]["serverInfo"]["name"], "klaffat-app-runner")
        self.assertEqual([tool["name"] for tool in frames[1]["result"]["tools"]], ["klaffat_app"])

    def test_stdio_start_status_logs_and_stop_with_fixed_fakes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            server_dir = base / "server"
            server_dir.mkdir()
            server_copy = server_dir / "server.py"
            hold_copy = server_dir / "hold.py"
            sandbox_exec_copy = server_dir / "sandbox_exec.py"
            server_copy.write_bytes(SERVER.read_bytes())
            hold_copy.write_bytes(HOLD.read_bytes())
            sandbox_exec_copy.write_bytes(SANDBOX_EXEC.read_bytes())

            primary = base / "Repos" / "klaffat"
            common = primary / ".git"
            gitdir = common / "worktrees" / "klaffat-protocol"
            worktree_root = base / "worktrees"
            worktree = worktree_root / "klaffat-protocol"
            gitdir.mkdir(parents=True)
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text(
                f"gitdir: {gitdir}\n", encoding="utf-8"
            )
            (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
            (gitdir / "gitdir").write_text(
                f"{worktree / '.git'}\n", encoding="utf-8"
            )
            origin = "https://github.com/example/klaffat.git"
            (common / "config").write_text(
                f'[remote "origin"]\n\turl = {origin}\n', encoding="utf-8"
            )
            scripts = worktree / "scripts"
            scripts.mkdir()
            e2e = scripts / "e2e-run"
            e2e.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            e2e.chmod(0o700)

            fake_bin = base / "bin"
            fake_bin.mkdir()
            generic = "#!/bin/sh\nexit 0\n"
            executables = {}
            for name in ("nix", "bash", "python3", "systemd-run", "calendar"):
                path = fake_bin / name
                path.write_text(generic, encoding="utf-8")
                path.chmod(0o700)
                executables[name] = str(path)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "if [ \"$2\" = show ]; then\n"
                "  printf '%s\\n' 'LoadState=loaded' 'ActiveState=active' "
                "'SubState=running' 'Result=success' 'ExecMainStatus=0'\n"
                "fi\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o700)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'application: http://127.0.0.1:41234' "
                "'klaffat-app-runner: holding app' "
                "'Authorization: Bearer must-not-escape'\n",
                encoding="utf-8",
            )
            journalctl.chmod(0o700)

            bus_path = Path(f"/run/user/{os.getuid()}/bus")
            if not bus_path.exists() or not stat.S_ISSOCK(bus_path.stat().st_mode):
                self.skipTest("a user-bus socket is required for the host-runner protocol smoke")

            policy = valid_policy()
            policy.update(
                worktreeRoot=str(worktree_root),
                klaffatPrimary=str(primary),
                allowedOrigins=[origin],
                stateRoot=str(base / "state"),
                nixExecutable=executables["nix"],
                bashExecutable=executables["bash"],
                pythonExecutable=executables["python3"],
                systemdRunExecutable=executables["systemd-run"],
                systemctlExecutable=str(systemctl),
                journalctlExecutable=str(journalctl),
                realCalendarExecutable=executables["calendar"],
                userBusSocket=str(bus_path),
            )
            (server_dir / "config.json").write_text(
                json.dumps({"appRunner": policy}), encoding="utf-8"
            )

            proc = subprocess.Popen(
                [sys.executable, str(server_copy)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
            self.assertIsNotNone(proc.stdin)
            self.assertIsNotNone(proc.stdout)

            def request(msg_id: int, method: str, params: dict) -> dict:
                proc.stdin.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
                    )
                    + "\n"
                )
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            initialized = request(1, "initialize", {})
            self.assertEqual(initialized["result"]["serverInfo"]["name"], "klaffat-app-runner")
            started = request(
                2,
                "tools/call",
                {
                    "name": "klaffat_app",
                    "arguments": {
                        "operation": "start",
                        "mode": "app",
                        "worktree_path": str(worktree),
                    },
                },
            )
            state = json.loads(started["result"]["content"][0]["text"])
            run_id = state["run_id"]
            self.assertEqual(state["phase"], "accepted")

            status = request(
                3,
                "tools/call",
                {"name": "klaffat_app", "arguments": {"operation": "status", "run_id": run_id}},
            )
            status_state = json.loads(status["result"]["content"][0]["text"])
            self.assertEqual(status_state["phase"], "running")
            self.assertEqual(status_state["url"], "http://127.0.0.1:41234")

            logs = request(
                4,
                "tools/call",
                {
                    "name": "klaffat_app",
                    "arguments": {"operation": "logs", "run_id": run_id, "tail_lines": 20},
                },
            )["result"]["content"][0]["text"]
            self.assertIn("[REDACTED:HEADER]", logs)
            self.assertNotIn("must-not-escape", logs)

            stopped = request(
                5,
                "tools/call",
                {"name": "klaffat_app", "arguments": {"operation": "stop", "run_id": run_id}},
            )
            stopped_state = json.loads(stopped["result"]["content"][0]["text"])
            self.assertEqual(stopped_state["phase"], "stopped")
            proc.stdin.close()
            return_code = proc.wait(timeout=5)
            stderr = proc.stderr.read()
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(return_code, 0, stderr)


if __name__ == "__main__":
    unittest.main()
