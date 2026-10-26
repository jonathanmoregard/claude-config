#!/usr/bin/env python3
"""Security and protocol tests for the host-side ai-router MCP."""

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
    name = f"ai_router_mcp_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load ai-router MCP server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ToolSurfaceTests(unittest.TestCase):
    def test_one_structured_tool_has_no_raw_escape_fields(self) -> None:
        server = load_server()
        self.assertEqual(server.SERVER_INFO["name"], "ai-router")
        self.assertEqual([tool["name"] for tool in server.TOOLS], ["ai_router"])
        schema = server.TOOLS[0]["inputSchema"]
        self.assertEqual(schema["required"], ["operation"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["operation"]["enum"],
            ["delegate", "status", "wait", "result", "log"],
        )
        for field in ("command", "args", "argv", "env", "force", "allow_downgrade"):
            self.assertNotIn(field, schema["properties"])


class RequestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()

    def delegate(self, **overrides):
        request = {
            "operation": "delegate",
            "prompt": "Review this change",
            "model": "gpt-5.6-sol",
            "cwd": str(self.cwd),
        }
        request.update(overrides)
        return self.server._validate_request(request)

    def test_delegate_requires_exactly_model_or_tier_and_valid_typed_fields(self) -> None:
        request = self.delegate(
            effort="high",
            sandbox="workspace-write",
            name="review lane",
            timeout_s=900,
            dev_gates=False,
        )
        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertEqual(request["cwd"], self.cwd)
        tier = self.server._validate_request(
            {
                "operation": "delegate",
                "prompt": "Review this change",
                "tier": "standard",
                "cwd": str(self.cwd),
            }
        )
        self.assertNotIn("model", tier)
        self.assertEqual(tier["tier"], "standard")
        for fields in (
            {"tier": "fast"},
            {"model": None},
            {"effort": "infinite"},
            {"sandbox": "danger-full-access"},
            {"timeout_s": True},
            {"dev_gates": "yes"},
            {"force": True},
        ):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                self.delegate(**fields)

    def test_cwd_must_be_canonical_under_an_approved_root(self) -> None:
        alias = self.cwd.parent / f"{self.cwd.name}-alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.addCleanup(alias.unlink)
        for value in ("relative", "/etc", str(alias), "/tmp/missing-ai-router-mcp"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.delegate(cwd=value)

    def test_schema_is_owned_canonical_json_inside_cwd(self) -> None:
        schema = self.cwd / "schema.json"
        schema.write_text('{"type":"object"}\n', encoding="utf-8")
        self.assertEqual(self.delegate(schema=str(schema))["schema"], schema)
        outside = self.cwd.parent / f"{self.cwd.name}-outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        link = self.cwd / "link.json"
        link.symlink_to(schema)
        for value in (str(outside), str(link)):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.delegate(schema=value)

    def test_follow_operations_validate_ids_and_bounds(self) -> None:
        job_id = "20261019-214923-4d461a"
        self.assertEqual(
            self.server._validate_request({"operation": "wait", "job_id": job_id})["timeout_s"],
            600,
        )
        self.assertEqual(
            self.server._validate_request(
                {"operation": "log", "job_id": job_id, "tail_lines": 17}
            )["tail_lines"],
            17,
        )
        for request in (
            {"operation": "status", "job_id": "../meta.json"},
            {"operation": "wait", "job_id": job_id, "timeout_s": 601},
            {"operation": "log", "job_id": job_id, "tail_lines": 0},
            {"operation": "result", "job_id": job_id, "prompt": "smuggled"},
        ):
            with self.subTest(request=request), self.assertRaises(RuntimeError):
                self.server._validate_request(request)


class InvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()

    def test_delegate_writes_prompt_and_builds_only_fixed_flags(self) -> None:
        request = self.server._validate_request(
            {
                "operation": "delegate",
                "prompt": "line one\nline two\n",
                "tier": "standard",
                "effort": "high",
                "sandbox": "read-only",
                "cwd": str(self.cwd),
                "name": "review",
                "timeout_s": 123,
                "dev_gates": False,
            }
        )
        seen = {}

        def fake(argv, *, cwd, timeout_s, rec):
            seen.update(argv=list(argv), cwd=cwd, timeout_s=timeout_s)
            prompt_path = Path(argv[argv.index("--prompt-file") + 1])
            seen["prompt_path"] = prompt_path
            seen["prompt"] = prompt_path.read_text(encoding="utf-8")
            return {"ok": True, "job_id": "20261019-214923-4d461a"}

        with mock.patch.object(self.server, "_run_cli", side_effect=fake):
            result = self.server._delegate(request, None)
        self.assertEqual(result["job_id"], "20261019-214923-4d461a")
        self.assertEqual(seen["prompt"], request["prompt"])
        self.assertFalse(seen["prompt_path"].exists())
        self.assertEqual(seen["cwd"], self.cwd)
        self.assertIn("--host", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--host") + 1], "codex")
        self.assertIn("--provider", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--provider") + 1], "codex")
        self.assertNotIn("--wait", seen["argv"])
        self.assertNotIn("--force", seen["argv"])
        self.assertNotIn("--allow-downgrade", seen["argv"])

    def test_real_cli_rejects_claude_models_and_tier_fallback(self) -> None:
        root = HERE.parent.parent
        fake_codex = root / "tools" / "ai-router" / "tests" / "fixtures" / "fake_codex.py"
        fake_claude = root / "tools" / "ai-router" / "tests" / "fixtures" / "fake_claude.py"
        env = {
            "AI_ROUTER_STATE": str(self.cwd / "state"),
            "AI_ROUTER_CODEX_BIN": str(fake_codex),
            "AI_ROUTER_CLAUDE_BIN": str(fake_claude),
            "HOME": str(self.cwd / "home"),
            "PATH": os.environ["PATH"],
        }
        Path(env["HOME"]).mkdir()
        with (
            mock.patch.object(self.server, "AI_ROUTER", root / "bin" / "ai-router"),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            claude_results = []
            for model in ("claude-opus-5", "opus"):
                claude_results.append(
                    self.server._delegate(
                        self.server._validate_request(
                            {
                                "operation": "delegate",
                                "prompt": "Review this change",
                                "model": model,
                                "cwd": str(self.cwd),
                            }
                        ),
                        None,
                    )
                )
            tier_result = self.server._delegate(
                self.server._validate_request(
                    {
                        "operation": "delegate",
                        "prompt": "Review this change",
                        "tier": "fast",
                        "cwd": str(self.cwd),
                    }
                ),
                None,
            )

        for result in claude_results:
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_class"], "usage")
            self.assertIn("belongs to claude, not --provider codex", result["message"])
        self.assertTrue(tier_result["ok"])
        self.assertEqual(tier_result["provider"], "codex")

    def test_host_environment_drops_only_codex_sandbox_markers(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_SANDBOX": "seatbelt",
                "CODEX_SANDBOX_NETWORK_DISABLED": "1",
                "AI_ROUTER_STATE": "/tmp/state",
                "PATH": "/bin",
            },
            clear=True,
        ):
            env = self.server._host_env()
        self.assertEqual(env["AI_ROUTER_STATE"], "/tmp/state")
        self.assertEqual(env["PATH"], "/bin")
        self.assertFalse(any(key.startswith("CODEX_SANDBOX") for key in env))

    def test_fixed_cli_receives_clean_environment_and_json_only(self) -> None:
        fake = self.cwd / "fake-ai-router"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'ok': True, 'argv': sys.argv[1:], "
            "'sandbox': [k for k in os.environ if k.startswith('CODEX_SANDBOX')]}))\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        with (
            mock.patch.object(self.server, "AI_ROUTER", fake),
            mock.patch.dict(os.environ, {"CODEX_SANDBOX_TEST": "1"}),
        ):
            result = self.server._run_cli(
                ["status", "20261019-214923-4d461a"],
                cwd=self.cwd,
                timeout_s=5,
            )
        self.assertEqual(result["sandbox"], [])
        self.assertEqual(result["argv"][-1], "--json")

    def test_wait_and_log_are_bounded_and_forwarded_without_shell(self) -> None:
        calls = []

        def fake(argv, *, cwd, timeout_s, rec):
            calls.append((argv, cwd, timeout_s))
            return {"ok": True, "job_id": "20261019-214923-4d461a"}

        with mock.patch.object(self.server, "_run_cli", side_effect=fake):
            self.server._tool_ai_router(
                {"operation": "wait", "job_id": "20261019-214923-4d461a", "timeout_s": 30}
            )
            self.server._tool_ai_router(
                {"operation": "log", "job_id": "20261019-214923-4d461a", "tail_lines": 25}
            )
        self.assertEqual(calls[0][0], ["wait", "20261019-214923-4d461a", "--timeout-s", "30"])
        self.assertEqual(calls[0][2], 40)
        self.assertEqual(calls[1][0], ["log", "20261019-214923-4d461a", "--tail", "25"])


class ProtocolTests(unittest.TestCase):
    def test_stdio_initialize_and_tool_list(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(
            '\n'.join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                    "",
                ]
            ),
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0, stderr)
        messages = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(messages[0]["result"]["serverInfo"]["name"], "ai-router")
        self.assertEqual(messages[1]["result"]["tools"][0]["name"], "ai_router")


if __name__ == "__main__":
    unittest.main()
