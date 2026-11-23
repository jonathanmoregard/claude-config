#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import codex_hook_output_adapter as adapter  # noqa: E402


def decoded(event: str, stdout: str) -> dict[str, object]:
    rendered = adapter.codex_payload_from(event, stdout)
    assert rendered
    value = json.loads(rendered)
    assert isinstance(value, dict)
    return value


def test_plain_session_start_text_is_visible_model_context() -> None:
    payload = decoded("SessionStart", "[nixos] host policy\n")

    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "[nixos] host policy",
        },
    }


def test_user_prompt_context_is_visible_and_kept_for_the_model() -> None:
    source = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "private model reminder",
            }
        }
    )

    payload = decoded("UserPromptSubmit", source)

    assert "suppressOutput" not in payload
    assert payload["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "private model reminder",
    }


def test_post_tool_model_context_is_preserved() -> None:
    source = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "run visual verification",
            }
        }
    )

    assert decoded("PostToolUse", source)["hookSpecificOutput"]["additionalContext"] == (
        "run visual verification"
    )


def test_explicit_user_system_message_remains_visible() -> None:
    source = json.dumps(
        {
            "systemMessage": "operator warning",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "model warning",
            },
        }
    )

    payload = decoded("SessionStart", source)

    assert payload["systemMessage"] == "operator warning"
    assert "suppressOutput" not in payload


def test_non_context_hook_output_is_preserved() -> None:
    source = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "blocked",
            }
        }
    )

    assert json.loads(adapter.codex_payload_from("PreToolUse", source)) == json.loads(source)


def test_main_executes_wrapper_without_shell_interpretation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        marker = root / "must-not-exist"
        wrapper = root / "hook;touch must-not-exist.sh"
        wrapper.write_text(
            "#!/usr/bin/env bash\nprintf 'model context'\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codex_hook_output_adapter.py"), "SessionStart", str(wrapper)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "model context"
        assert not marker.exists()


def test_context_hook_failure_is_silent_and_fail_open() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wrapper = root / "broken-context-hook.sh"
        wrapper.write_text(
            "#!/usr/bin/env bash\nprintf 'internal diagnostic' >&2\nexit 1\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codex_hook_output_adapter.py"), "UserPromptSubmit", str(wrapper)],
            text=True,
            input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


def test_enforcement_hook_failure_remains_a_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wrapper = root / "broken-enforcement-hook.sh"
        wrapper.write_text(
            "#!/usr/bin/env bash\nprintf 'blocked' >&2\nexit 1\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codex_hook_output_adapter.py"), "PreToolUse", str(wrapper)],
            text=True,
            input=json.dumps({"hook_event_name": "PreToolUse"}),
            capture_output=True,
            check=False,
        )

        assert result.returncode == 1
        assert result.stderr == "blocked"


def test_apply_patch_input_is_split_into_claude_file_tool_payloads() -> None:
    source = {
        "hook_event_name": "PreToolUse",
        "cwd": "/work",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": """*** Begin Patch
*** Add File: first.tsx
+one
*** Update File: second.html
@@
-old
+new
*** Move to: moved.html
*** End Patch"""
        },
    }

    payloads = adapter.claude_input_payloads(json.dumps(source), adapt_file_tools=True)

    decoded_payloads = [json.loads(payload) for payload in payloads]
    assert [payload["tool_name"] for payload in decoded_payloads] == [
        "Write",
        "Edit",
        "Edit",
    ]
    assert [payload["tool_input"]["file_path"] for payload in decoded_payloads] == [
        "first.tsx",
        "second.html",
        "moved.html",
    ]


def test_apply_patch_adapter_blocks_when_any_touched_file_is_denied() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        seen = root / "seen.jsonl"
        wrapper = root / "guard.py"
        wrapper.write_text(
            """#!/usr/bin/env python3
import json
import sys
payload = json.load(sys.stdin)
with open(sys.argv[1], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(payload) + "\\n")
if payload["tool_input"]["file_path"] == ".claude/settings.json":
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "guarded"}}, sys.stdout)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        launcher = root / "launcher.sh"
        launcher.write_text(
            f"#!/usr/bin/env bash\nexec {sys.executable} {wrapper} {seen}\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": """*** Begin Patch
*** Update File: harmless.txt
@@
-a
+b
*** Update File: .claude/settings.json
@@
-a
+b
*** End Patch"""
            },
        }

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "codex_hook_output_adapter.py"),
                "PreToolUse",
                str(launcher),
                "--adapt-file-tools",
            ],
            text=True,
            input=json.dumps(payload),
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        seen_payloads = [json.loads(line) for line in seen.read_text().splitlines()]
        assert [item["tool_input"]["file_path"] for item in seen_payloads] == [
            "harmless.txt",
            ".claude/settings.json",
        ]


if __name__ == "__main__":
    test_plain_session_start_text_is_visible_model_context()
    test_user_prompt_context_is_visible_and_kept_for_the_model()
    test_post_tool_model_context_is_preserved()
    test_explicit_user_system_message_remains_visible()
    test_non_context_hook_output_is_preserved()
    test_main_executes_wrapper_without_shell_interpretation()
    test_context_hook_failure_is_silent_and_fail_open()
    test_enforcement_hook_failure_remains_a_failure()
    test_apply_patch_input_is_split_into_claude_file_tool_payloads()
    test_apply_patch_adapter_blocks_when_any_touched_file_is_denied()
    print("Codex hook output adapter tests OK")
