#!/usr/bin/env python3
"""Contract tests for the ai-router broker. The ai-router it runs is a stub."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SERVER = Path(__file__).resolve().parents[3] / "runtime" / "mcps" / "ai-router-broker" / "server.py"

STUB = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
mode = open({mode!r}).read().strip()
if mode == "sleep":
    time.sleep(30)
if mode == "flood":
    print("x" * 100000)
    sys.exit(0)
prompt = None
for a in args:
    if a.startswith("--prompt-file="):
        prompt = open(a.split("=", 1)[1]).read()
print(json.dumps({{"ok": mode != "fail", "argv": args, "prompt": prompt,
                  "env": sorted(os.environ), "path": os.environ.get("PATH")}}))
sys.exit(4 if mode == "fail" else 0)
"""


def load_server():
    name = f"ai_router_broker_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


JOB = "20261120-144751-83d47a"


class BrokerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.root = base / "worktrees"
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        self.outside = base / "outside"
        self.outside.mkdir()
        self.mode = base / "mode"
        self.mode.write_text("ok\n")
        self.stub = base / "ai-router"
        self.stub.write_text(STUB.format(python=sys.executable, mode=str(self.mode)))
        self.stub.chmod(0o755)
        self.raw = {
            "version": 1, "aiRouter": str(self.stub), "roots": [str(self.root)],
            "path": ["/run/current-system/sw/bin", "/usr/bin", "/bin"], "host": "claude",
            "schemaDir": str(base / "schemas"), "callTimeoutSec": 20, "maxWaitSec": 600,
            "maxPromptBytes": 100000, "outputLimitChars": 20000,
        }
        self.config = base / "config.json"
        self.config.write_text(json.dumps({"aiRouterBroker": self.raw}))
        self.policy = self.server.load_policy({"aiRouterBroker": dict(self.raw)})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **args):
        return self.server.build_call(tool, args, self.policy)

    def refused(self, tool: str, pattern: str, **args) -> None:
        with self.assertRaisesRegex(self.server.Refused, pattern):
            self.call(tool, **args)

    def delegate(self, **over) -> dict:
        return {"tier": "fast", "prompt": "say pong", "cwd": str(self.repo), **over}

    def stdio(self, *msgs: dict, env: dict | None = None, timeout: int = 60) -> list[dict]:
        proc = subprocess.run(
            [sys.executable, str(SERVER), "--config", str(self.config)],
            input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True,
            timeout=timeout, env=env or dict(os.environ))
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def tool(self, name: str, env: dict | None = None, **args) -> dict:
        out = self.stdio({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                         {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}, env=env)
        return next(m for m in out if m.get("id") == 2)["result"]


class ArgvTests(BrokerCase):
    def test_executable_is_always_the_configured_ai_router(self) -> None:
        for tool, args in [("delegate", self.delegate()), ("status", {}), ("list", {"all": True}),
                           ("result", {"job_id": JOB}), ("quota", {"fresh": True})]:
            self.assertEqual(self.call(tool, **args).argv[:2], [str(self.stub), tool])

    def test_every_call_asks_for_json(self) -> None:
        for tool, args in [("delegate", self.delegate()), ("wait", {"job_id": JOB}),
                           ("pick", {"tier": "fast"}), ("cancel", {"job_id": JOB})]:
            self.assertIn("--json", self.call(tool, **args).argv)

    def test_values_are_attached_so_none_can_pose_as_an_option(self) -> None:
        argv = self.call("delegate", **self.delegate(effort="low", name="n1")).argv
        self.assertIn("--effort=low", argv)
        self.assertIn("--name=n1", argv)
        self.assertNotIn("low", argv)

    def test_prompt_text_reaches_ai_router_on_stdin(self) -> None:
        call = self.call("delegate", **self.delegate(prompt="say pong"))
        self.assertIn("--prompt-file=/dev/stdin", call.argv)
        self.assertEqual(call.stdin, b"say pong")

    def test_cwd_is_passed_canonical(self) -> None:
        argv = self.call("delegate", **self.delegate(cwd=str(self.root / "repo" / "."))).argv
        self.assertIn(f"--cwd={self.repo}", argv)

    def test_host_defaults_to_policy(self) -> None:
        self.assertIn("--host=claude", self.call("delegate", **self.delegate()).argv)
        self.assertIn("--host=codex", self.call("pick", tier="fast", host="codex").argv)

    def test_wait_timeout_is_capped_and_bounds_the_call(self) -> None:
        call = self.call("wait", job_id=JOB)
        self.assertIn("--timeout-s=600", call.argv)
        self.assertGreater(call.timeout, 600)
        self.refused("wait", "timeout_s", job_id=JOB, timeout_s=601)


class OptionAllowListTests(BrokerCase):
    def test_unknown_and_withheld_options_refused(self) -> None:
        for extra in ({"dev_gates": True}, {"force": True}, {"no_bare": True}, {"wait": True},
                      {"argv": ["--force"]}, {"executable": "/bin/sh"}):
            self.refused("delegate", "unexpected", **self.delegate(**extra))

    def test_sandbox_limited_to_read_only_and_workspace_write(self) -> None:
        self.assertIn("--sandbox=workspace-write",
                      self.call("delegate", **self.delegate(sandbox="workspace-write")).argv)
        for bad in ("danger-full-access", "--force", ""):
            self.refused("delegate", "sandbox", **self.delegate(sandbox=bad))

    def test_tier_xor_model(self) -> None:
        self.refused("delegate", "exactly one of tier, model", **self.delegate(model="luna"))
        args = self.delegate()
        del args["tier"]
        self.refused("delegate", "exactly one of tier, model", **args)

    def test_prompt_xor_prompt_file(self) -> None:
        f = self.repo / "task.md"
        f.write_text("x")
        self.refused("delegate", "exactly one of prompt, prompt_file",
                     **self.delegate(prompt_file=str(f)))

    def test_option_like_strings_refused(self) -> None:
        self.refused("delegate", "model", **self.delegate(tier=None, model="--force"))
        self.refused("delegate", "effort", **self.delegate(effort="--dev-gates"))
        self.refused("result", "job_id", job_id="--all")
        self.refused("result", "job_id", job_id="../../etc")

    def test_unknown_tool_refused(self) -> None:
        with self.assertRaisesRegex(self.server.Refused, "unknown tool"):
            self.call("version")

    def test_empty_and_oversized_prompts_refused(self) -> None:
        self.refused("delegate", "prompt", **self.delegate(prompt="  \n"))
        self.refused("delegate", "prompt", **self.delegate(prompt="x" * 100001))


class PathTests(BrokerCase):
    def test_prompt_file_inside_root_is_read_by_the_broker(self) -> None:
        f = self.repo / "task.md"
        f.write_text("from file")
        call = self.call("delegate", **self.delegate(prompt=None, prompt_file=str(f)))
        self.assertEqual(call.stdin, b"from file")

    def test_prompt_file_outside_roots_refused(self) -> None:
        f = self.outside / "secret.md"
        f.write_text("secret")
        self.refused("delegate", "outside", **self.delegate(prompt=None, prompt_file=str(f)))
        self.refused("delegate", "outside",
                     **self.delegate(prompt=None, prompt_file=str(self.repo / ".." / ".." / "outside" / "secret.md")))

    def test_prompt_file_symlink_refused_even_to_a_file_inside(self) -> None:
        f = self.repo / "task.md"
        f.write_text("x")
        for target in (f, self.outside / "secret.md"):
            link = self.repo / f"link-{target.name}-{target.parent.name}"
            link.symlink_to(target)
            self.refused("delegate", "symlink", **self.delegate(prompt=None, prompt_file=str(link)))

    def test_prompt_file_behind_a_linked_directory_outside_refused(self) -> None:
        (self.outside / "secret.md").write_text("secret")
        (self.repo / "door").symlink_to(self.outside)
        self.refused("delegate", "outside",
                     **self.delegate(prompt=None, prompt_file=str(self.repo / "door" / "secret.md")))

    def test_prompt_file_must_be_regular(self) -> None:
        fifo = self.repo / "fifo"
        os.mkfifo(fifo)
        started = time.monotonic()
        self.refused("delegate", "regular file", **self.delegate(prompt=None, prompt_file=str(fifo)))
        self.assertLess(time.monotonic() - started, 5)
        self.refused("delegate", "regular file", **self.delegate(prompt=None, prompt_file=str(self.repo)))

    def test_relative_paths_refused(self) -> None:
        self.refused("delegate", "absolute", **self.delegate(cwd="repo"))
        self.refused("delegate", "absolute", **self.delegate(prompt=None, prompt_file="task.md"))

    def test_cwd_outside_roots_refused(self) -> None:
        self.refused("delegate", "outside", **self.delegate(cwd=str(self.outside)))
        self.refused("delegate", "outside", **self.delegate(cwd="/"))

    def test_cwd_symlink_refused(self) -> None:
        (self.repo / "inner").mkdir()
        (self.repo / "to-inner").symlink_to(self.repo / "inner")
        (self.repo / "to-outside").symlink_to(self.outside)
        for name in ("to-inner", "to-outside"):
            self.refused("delegate", "symlink", **self.delegate(cwd=str(self.repo / name)))

    def test_cwd_behind_a_linked_directory_outside_refused(self) -> None:
        (self.outside / "sub").mkdir()
        (self.repo / "door").symlink_to(self.outside)
        self.refused("delegate", "outside", **self.delegate(cwd=str(self.repo / "door" / "sub")))

    def test_cwd_must_be_a_directory(self) -> None:
        f = self.repo / "file"
        f.write_text("x")
        self.refused("delegate", "directory", **self.delegate(cwd=str(f)))

    def test_schema_is_copied_out_of_the_callers_reach(self) -> None:
        schema = self.repo / "schema.json"
        schema.write_text('{"type": "object"}')
        argv = self.call("delegate", **self.delegate(schema_file=str(schema))).argv
        copy = Path(next(a for a in argv if a.startswith("--schema=")).split("=", 1)[1])
        self.assertEqual(copy.parent, Path(self.raw["schemaDir"]))
        self.assertEqual(json.loads(copy.read_text()), {"type": "object"})
        schema.write_text('{"type": "array"}')
        self.assertEqual(json.loads(copy.read_text()), {"type": "object"})

    def test_schema_must_be_json_under_roots(self) -> None:
        bad = self.repo / "bad.json"
        bad.write_text("not json")
        self.refused("delegate", "schema", **self.delegate(schema_file=str(bad)))
        out = self.outside / "s.json"
        out.write_text("{}")
        self.refused("delegate", "outside", **self.delegate(schema_file=str(out)))


class StdioTests(BrokerCase):
    def test_tools_list_names_the_cli_subcommands(self) -> None:
        out = self.stdio({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                         {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in next(m for m in out if m.get("id") == 2)["result"]["tools"]}
        self.assertEqual(names, {"delegate", "status", "result", "wait", "peek", "log", "send",
                                 "cancel", "list", "quota", "pick"})

    def test_success_returns_the_cli_json(self) -> None:
        res = self.tool("delegate", **self.delegate(prompt="hello there"))
        self.assertFalse(res.get("isError"))
        body = json.loads(res["content"][0]["text"])
        self.assertEqual(body["prompt"], "hello there")
        self.assertEqual(body["argv"][0], "delegate")

    def test_cli_failure_is_an_error_with_its_json(self) -> None:
        self.mode.write_text("fail\n")
        res = self.tool("result", job_id=JOB)
        self.assertTrue(res["isError"])
        self.assertIn("exit 4", res["content"][0]["text"])
        self.assertIn('"ok": false', res["content"][0]["text"])

    def test_refusal_is_an_error_and_runs_nothing(self) -> None:
        res = self.tool("delegate", **self.delegate(cwd=str(self.outside)))
        self.assertTrue(res["isError"])
        self.assertIn("outside", res["content"][0]["text"])

    def test_child_env_is_built_from_policy_not_inherited(self) -> None:
        env = dict(os.environ, GH_TOKEN="ghp_leak", SSH_AUTH_SOCK="/tmp/agent",
                   AI_ROUTER_STATE="/tmp/elsewhere", CLAUDECODE="1")
        body = json.loads(self.tool("status", env=env)["content"][0]["text"])
        for name in ("GH_TOKEN", "SSH_AUTH_SOCK", "AI_ROUTER_STATE", "CLAUDECODE"):
            self.assertNotIn(name, body["env"])
        self.assertEqual(body["path"], ":".join(self.raw["path"]))

    def test_hung_cli_is_stopped_at_the_call_timeout(self) -> None:
        self.raw["callTimeoutSec"] = 1
        self.config.write_text(json.dumps({"aiRouterBroker": self.raw}))
        self.mode.write_text("sleep\n")
        started = time.monotonic()
        res = self.tool("status")
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(res["isError"])
        self.assertIn("timed out", res["content"][0]["text"])

    def test_output_is_bounded(self) -> None:
        self.mode.write_text("flood\n")
        text = self.tool("list", all=True)["content"][0]["text"]
        self.assertLess(len(text), self.raw["outputLimitChars"] + 200)
        self.assertIn("truncated", text)

    def test_unreadable_config_fails_closed(self) -> None:
        self.config.write_text("{")
        res = self.tool("status")
        self.assertTrue(res["isError"])
        self.assertIn("config", res["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
