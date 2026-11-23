#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


RENDERER = Path(__file__).resolve().parents[2] / "codex"
FANOUT = RENDERER / "scripts" / "codex_hook_fanout.py"


def write_wrapper(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_fanout(
    root: Path,
    event: str,
    handlers: list[dict[str, object]],
    payload: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    plan = root / "plan.json"
    plan.write_text(json.dumps({"handlers": handlers}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(FANOUT), event, str(plan)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def handler(
    wrapper: Path,
    *,
    matcher: str | None = None,
    timeout: float = 5,
    adapt_file_tools: bool = False,
) -> dict[str, object]:
    return {
        "matcher": matcher,
        "wrapper": str(wrapper),
        "timeout": timeout,
        "adapt_file_tools": adapt_file_tools,
    }


def context_wrapper(path: Path, message: str, delay: float = 0) -> Path:
    return write_wrapper(
        path,
        "import json\n"
        "import time\n"
        f"time.sleep({delay!r})\n"
        "json.dump({\"hookSpecificOutput\": {"
        "\"hookEventName\": \"SessionStart\", "
        f"\"additionalContext\": {message!r}"
        "}}, sys.stdout)\n".replace("import json\n", "import json\nimport sys\n"),
    )


def test_matching_handlers_run_concurrently_and_merge_in_completion_order() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        slow = context_wrapper(root / "slow.py", "slow", 1.0)
        fast = context_wrapper(root / "fast.py", "fast", 0.8)

        started = time.monotonic()
        result = run_fanout(
            root,
            "SessionStart",
            [handler(slow), handler(fast)],
            {"hook_event_name": "SessionStart", "source": "startup"},
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 0, result.stderr
        assert elapsed < 1.5, f"handlers appear sequential: {elapsed:.3f}s"
        assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == (
            "fast\nslow"
        )


def test_tool_matchers_include_codex_name_and_claude_aliases() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wrappers = []
        for name in ("codex", "claude", "unmatched"):
            wrappers.append(
                write_wrapper(
                    root / f"{name}.py",
                    "import json\nimport sys\n"
                    f"json.dump({{'systemMessage': {name!r}}}, sys.stdout)\n",
                )
            )
        result = run_fanout(
            root,
            "PreToolUse",
            [
                handler(wrappers[0], matcher="^apply_patch$"),
                handler(wrappers[1], matcher="^(Write|Edit)$"),
                handler(wrappers[2], matcher="^Bash$"),
            ],
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_input": {"command": "*** Begin Patch\n*** End Patch"},
            },
        )

        assert result.returncode == 0, result.stderr
        assert set(json.loads(result.stdout)["systemMessage"].splitlines()) == {
            "codex",
            "claude",
        }

        agent_result = run_fanout(
            root,
            "PreToolUse",
            [handler(wrappers[1], matcher="^Agent$")],
            {"hook_event_name": "PreToolUse", "tool_name": "spawn_agent"},
        )
        assert json.loads(agent_result.stdout)["systemMessage"] == "claude"


def test_event_specific_matchers_use_source_and_trigger() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wrapper = write_wrapper(
            root / "selected.py",
            "import json\nimport sys\njson.dump({'systemMessage': 'selected'}, sys.stdout)\n",
        )
        session = run_fanout(
            root,
            "SessionStart",
            [handler(wrapper, matcher="^(resume|compact)$")],
            {"hook_event_name": "SessionStart", "source": "resume"},
        )
        compact = run_fanout(
            root,
            "PreCompact",
            [handler(wrapper, matcher="^auto$")],
            {"hook_event_name": "PreCompact", "trigger": "auto"},
        )
        prompt = run_fanout(
            root,
            "UserPromptSubmit",
            [handler(wrapper, matcher="this matcher is ignored")],
            {"hook_event_name": "UserPromptSubmit", "prompt": "hello"},
        )

        assert json.loads(session.stdout)["systemMessage"] == "selected"
        assert json.loads(compact.stdout)["systemMessage"] == "selected"
        assert json.loads(prompt.stdout)["systemMessage"] == "selected"


def test_apply_patch_payload_adaptation_is_per_handler() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        adapted_log = root / "adapted.jsonl"
        raw_log = root / "raw.jsonl"
        adapted = write_wrapper(
            root / "adapted.py",
            "import json\nimport pathlib\nimport sys\n"
            f"path = pathlib.Path({str(adapted_log)!r})\n"
            "with path.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(json.load(sys.stdin)) + '\\n')\n",
        )
        raw = write_wrapper(
            root / "raw.py",
            "import json\nimport pathlib\nimport sys\n"
            f"path = pathlib.Path({str(raw_log)!r})\n"
            "with path.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(json.load(sys.stdin)) + '\\n')\n",
        )
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": """*** Begin Patch
*** Add File: first.txt
+first
*** Update File: second.txt
@@
-old
+new
*** End Patch"""
            },
        }

        result = run_fanout(
            root,
            "PreToolUse",
            [
                handler(adapted, matcher="^(Write|Edit)$", adapt_file_tools=True),
                handler(raw, matcher="*", adapt_file_tools=False),
            ],
            payload,
        )

        assert result.returncode == 0, result.stderr
        adapted_payloads = [json.loads(line) for line in adapted_log.read_text().splitlines()]
        raw_payloads = [json.loads(line) for line in raw_log.read_text().splitlines()]
        assert sorted(
            (item["tool_name"], item["tool_input"]["file_path"])
            for item in adapted_payloads
        ) == [
            ("Edit", "second.txt"),
            ("Write", "first.txt"),
        ]
        assert raw_payloads == [payload]


def test_apply_patch_targets_only_invoke_matching_file_tool_hooks() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_marker = root / "write-ran"
        write_only = write_wrapper(
            root / "write-only.py",
            "from pathlib import Path\n"
            f"Path({str(write_marker)!r}).touch()\n",
        )
        result = run_fanout(
            root,
            "PreToolUse",
            [handler(write_only, matcher="^Write$", adapt_file_tools=True)],
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: existing.txt\n@@\n-old\n+new\n*** End Patch"
                },
            },
        )

        assert result.returncode == 0, result.stderr
        assert not write_marker.exists()


def test_multiple_context_and_system_messages_form_one_json_response() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        slow = write_wrapper(
            root / "slow.py",
            "import json\nimport sys\nimport time\n"
            "time.sleep(0.2)\n"
            "json.dump({'systemMessage': 'slow system', 'hookSpecificOutput': "
            "{'hookEventName': 'SessionStart', 'additionalContext': 'slow context'}}, sys.stdout)\n",
        )
        fast = write_wrapper(
            root / "fast.py",
            "import json\nimport sys\n"
            "json.dump({'systemMessage': 'fast system', 'hookSpecificOutput': "
            "{'hookEventName': 'SessionStart', 'additionalContext': 'fast context'}}, sys.stdout)\n",
        )

        result = run_fanout(
            root,
            "SessionStart",
            [handler(slow), handler(fast)],
            {"hook_event_name": "SessionStart", "source": "startup"},
        )

        assert result.returncode == 0, result.stderr
        merged = json.loads(result.stdout)
        assert merged == {
            "systemMessage": "fast system\nslow system",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "fast context\nslow context",
            },
        }


def test_deny_takes_precedence_over_block_and_context() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        deny = write_wrapper(
            root / "deny.py",
            "import json\nimport sys\nimport time\n"
            "time.sleep(0.15)\n"
            "json.dump({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'permissionDecision': 'deny', 'permissionDecisionReason': 'denied'}}, sys.stdout)\n",
        )
        block = write_wrapper(
            root / "block.py",
            "import json\nimport sys\n"
            "json.dump({'decision': 'block', 'reason': 'blocked'}, sys.stdout)\n",
        )
        context = write_wrapper(
            root / "context.py",
            "import json\nimport sys\n"
            "json.dump({'systemMessage': 'informational'}, sys.stdout)\n",
        )

        result = run_fanout(
            root,
            "PreToolUse",
            [handler(block), handler(context), handler(deny)],
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        )

        assert result.returncode == 0, result.stderr
        merged = json.loads(result.stdout)
        assert merged["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert merged["hookSpecificOutput"]["permissionDecisionReason"] == "denied"
        assert merged.get("decision") != "block"


def test_rewrite_fields_survive_informational_output_merging() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        rewrite = write_wrapper(
            root / "rewrite.py",
            "import json\nimport sys\n"
            "json.dump({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'permissionDecision': 'allow', 'updatedInput': {'command': 'safe'}}}, sys.stdout)\n",
        )
        information = write_wrapper(
            root / "information.py",
            "import json\nimport sys\n"
            "json.dump({'systemMessage': 'rewritten safely', 'hookSpecificOutput': "
            "{'hookEventName': 'PreToolUse', 'additionalContext': 'audit note'}}, sys.stdout)\n",
        )

        result = run_fanout(
            root,
            "PreToolUse",
            [handler(information), handler(rewrite)],
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        )

        assert result.returncode == 0, result.stderr
        merged = json.loads(result.stdout)
        assert merged["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert merged["hookSpecificOutput"]["updatedInput"] == {"command": "safe"}
        assert merged["hookSpecificOutput"]["additionalContext"] == "audit note"
        assert merged["systemMessage"] == "rewritten safely"


def test_nonconflicting_structured_outputs_are_merged() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allow = write_wrapper(
            root / "allow.py",
            "import json\nimport sys\n"
            "json.dump({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'permissionDecision': 'allow'}}, sys.stdout)\n",
        )
        rewrite = write_wrapper(
            root / "rewrite.py",
            "import json\nimport sys\nimport time\ntime.sleep(0.15)\n"
            "json.dump({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'updatedInput': {'command': 'sanitized'}}}, sys.stdout)\n",
        )
        result = run_fanout(
            root,
            "PreToolUse",
            [handler(allow), handler(rewrite)],
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        )

        assert result.returncode == 0, result.stderr
        merged = json.loads(result.stdout)["hookSpecificOutput"]
        assert merged["permissionDecision"] == "allow"
        assert merged["updatedInput"] == {"command": "sanitized"}


def test_timeout_and_nonzero_children_do_not_cancel_successful_siblings() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        success = context_wrapper(root / "success.py", "survived", 0)
        timed_out = write_wrapper(
            root / "timeout.py",
            "import time\ntime.sleep(5)\n",
        )
        failed = write_wrapper(
            root / "failed.py",
            "import sys\nsys.stderr.write('leaf failed')\nraise SystemExit(7)\n",
        )

        started = time.monotonic()
        result = run_fanout(
            root,
            "SessionStart",
            [handler(timed_out, timeout=0.15), handler(failed), handler(success)],
            {"hook_event_name": "SessionStart", "source": "startup"},
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1
        assert result.returncode == 124
        assert "leaf failed" in result.stderr
        assert "timed out" in result.stderr
        assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "survived"


def test_timeout_kills_descendant_processes_holding_output_pipes() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        descendant = write_wrapper(
            root / "descendant.py",
            "import subprocess\nimport time\n"
            "subprocess.Popen(['sleep', '5'])\n"
            "time.sleep(5)\n",
        )

        started = time.monotonic()
        result = run_fanout(
            root,
            "SessionStart",
            [handler(descendant, timeout=0.1)],
            {"hook_event_name": "SessionStart", "source": "startup"},
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 124
        assert elapsed < 1, f"descendant kept pipes open for {elapsed:.3f}s"


def test_exit_two_wins_and_context_exit_one_only_failures_are_silent() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        exit_one = write_wrapper(
            root / "exit-one.py",
            "import sys\nsys.stderr.write('routine context miss')\nraise SystemExit(1)\n",
        )
        exit_two = write_wrapper(
            root / "exit-two.py",
            "import sys\nsys.stderr.write('blocking failure')\nraise SystemExit(2)\n",
        )
        exit_seven = write_wrapper(
            root / "exit-seven.py",
            "import sys\nsys.stderr.write('ordinary failure')\nraise SystemExit(7)\n",
        )
        success = context_wrapper(root / "success.py", "available context", 0)

        context_result = run_fanout(
            root,
            "SessionStart",
            [handler(exit_one), handler(success)],
            {"hook_event_name": "SessionStart", "source": "startup"},
        )
        blocking_result = run_fanout(
            root,
            "PreToolUse",
            [handler(exit_seven), handler(exit_two)],
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        )

        assert context_result.returncode == 0
        assert context_result.stderr == ""
        assert json.loads(context_result.stdout)["hookSpecificOutput"][
            "additionalContext"
        ] == "available context"
        assert blocking_result.returncode == 2
        assert blocking_result.stderr.count("ordinary failure") == 1
        assert blocking_result.stderr.count("blocking failure") == 1


def test_first_nonzero_exit_is_selected_in_invocation_order() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first = write_wrapper(
            root / "first.py",
            "import time\ntime.sleep(0.1)\nraise SystemExit(7)\n",
        )
        second = write_wrapper(root / "second.py", "raise SystemExit(9)\n")

        result = run_fanout(
            root,
            "PreToolUse",
            [handler(first), handler(second)],
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        )

        assert result.returncode == 7


def test_malformed_plan_and_payload_fail_closed_without_tracebacks() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        marker = root / "must-not-exist"
        wrapper = write_wrapper(
            root / "must-not-run.py",
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).touch()\n",
        )
        malformed_plan = root / "malformed-plan.json"
        malformed_plan.write_text("{", encoding="utf-8")

        plan_result = subprocess.run(
            [sys.executable, str(FANOUT), "SessionStart", str(malformed_plan)],
            input="{}",
            text=True,
            capture_output=True,
            check=False,
        )
        plan = root / "plan.json"
        plan.write_text(
            json.dumps({"handlers": [handler(wrapper)]}),
            encoding="utf-8",
        )
        payload_result = subprocess.run(
            [sys.executable, str(FANOUT), "SessionStart", str(plan)],
            input="not json",
            text=True,
            capture_output=True,
            check=False,
        )
        invalid_matcher_plan = root / "invalid-matcher-plan.json"
        invalid_matcher_plan.write_text(
            json.dumps({"handlers": [handler(wrapper, matcher="[")]}),
            encoding="utf-8",
        )
        matcher_result = subprocess.run(
            [sys.executable, str(FANOUT), "SessionStart", str(invalid_matcher_plan)],
            input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}),
            text=True,
            capture_output=True,
            check=False,
        )

        assert plan_result.returncode != 0
        assert "Traceback" not in plan_result.stderr
        assert payload_result.returncode != 0
        assert "Traceback" not in payload_result.stderr
        assert matcher_result.returncode != 0
        assert "Traceback" not in matcher_result.stderr
        assert not marker.exists()


def test_wrapper_paths_are_not_interpreted_by_a_shell() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        marker = root / "injected"
        wrapper = context_wrapper(root / "hook;touch injected", "safe", 0)

        result = run_fanout(
            root,
            "SessionStart",
            [handler(wrapper)],
            {"hook_event_name": "SessionStart", "source": "startup"},
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "safe"
        assert not marker.exists()


if __name__ == "__main__":
    test_matching_handlers_run_concurrently_and_merge_in_completion_order()
    test_tool_matchers_include_codex_name_and_claude_aliases()
    test_event_specific_matchers_use_source_and_trigger()
    test_apply_patch_payload_adaptation_is_per_handler()
    test_apply_patch_targets_only_invoke_matching_file_tool_hooks()
    test_multiple_context_and_system_messages_form_one_json_response()
    test_deny_takes_precedence_over_block_and_context()
    test_rewrite_fields_survive_informational_output_merging()
    test_nonconflicting_structured_outputs_are_merged()
    test_timeout_and_nonzero_children_do_not_cancel_successful_siblings()
    test_timeout_kills_descendant_processes_holding_output_pipes()
    test_exit_two_wins_and_context_exit_one_only_failures_are_silent()
    test_first_nonzero_exit_is_selected_in_invocation_order()
    test_malformed_plan_and_payload_fail_closed_without_tracebacks()
    test_wrapper_paths_are_not_interpreted_by_a_shell()
    print("Codex hook fanout tests OK")
