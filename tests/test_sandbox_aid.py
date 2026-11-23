"""sandbox-aid.py names the working route after a sandbox-caused failure and
logs requests to leave the sandbox. It must stay silent everywhere else."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "runtime" / "hooks" / "sandbox-aid.py"
AUTH_FAILURE = "fatal: Authentication failed for 'https://github.com/o/r.git/'"


def run_hook(tmp_path: Path, event: dict | str, *, enabled: bool = True):
    hooks = tmp_path / "config" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy(HOOK, hooks / HOOK.name)
    (tmp_path / "config" / "settings.json").write_text(
        json.dumps({"sandbox": {"enabled": enabled}}))
    payload = event if isinstance(event, str) else json.dumps(event)
    proc = subprocess.run(
        [sys.executable, "-S", "-E", str(hooks / HOOK.name)], input=payload,
        capture_output=True, text=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    log = tmp_path / ".local" / "state" / "claude-sandbox" / "events.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return proc, records


def failed(stderr: str, **tool_input) -> dict:
    return {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "session_id": "s",
            "tool_input": {"command": "git push", **tool_input}, "error": stderr}


def test_git_auth_failure_points_at_the_broker(tmp_path: Path) -> None:
    proc, records = run_hook(tmp_path, failed(AUTH_FAILURE))
    assert proc.returncode == 2
    assert "mcp__git_broker__git" in proc.stderr
    assert [(r["kind"], r["hint"]) for r in records] == [("hint", "git-auth")]


def test_flake_eval_blocked_by_the_gitmodules_placeholder_names_the_fix(tmp_path: Path) -> None:
    err = ("error: parsing .gitmodules file: failed open - "
           "'/home/u/worktrees/r/.gitmodules' is locked: Permission denied")
    proc, records = run_hook(tmp_path, failed(err, command="nix build .#x"))
    assert proc.returncode == 2
    assert "empty .gitmodules" in proc.stderr
    assert [r["hint"] for r in records] == ["gitmodules"]


def test_hint_also_reads_a_completed_call(tmp_path: Path) -> None:
    event = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": "systemctl --user status x | head"},
             "tool_response": {"stdout": "", "interrupted": False,
                               "stderr": "Failed to connect to user scope bus via local transport"}}
    proc, _ = run_hook(tmp_path, event)
    assert proc.returncode == 2
    assert "wait-for unit" in proc.stderr


def test_silent_when_the_sandbox_is_off(tmp_path: Path) -> None:
    proc, records = run_hook(tmp_path, failed(AUTH_FAILURE), enabled=False)
    assert (proc.returncode, proc.stderr, records) == (0, "", [])


def test_silent_for_a_command_that_ran_outside_the_sandbox(tmp_path: Path) -> None:
    proc, records = run_hook(tmp_path, failed(AUTH_FAILURE, dangerouslyDisableSandbox=True))
    assert (proc.returncode, proc.stderr, records) == (0, "", [])


def test_silent_for_an_unrelated_failure_and_for_other_tools(tmp_path: Path) -> None:
    proc, _ = run_hook(tmp_path, failed("error: pathspec 'x' did not match"))
    assert (proc.returncode, proc.stderr) == (0, "")
    other = {**failed(AUTH_FAILURE), "tool_name": "Read"}
    proc, _ = run_hook(tmp_path, other)
    assert (proc.returncode, proc.stderr) == (0, "")


def test_request_to_leave_the_sandbox_is_logged_not_blocked(tmp_path: Path) -> None:
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s",
             "cwd": "/w", "tool_input": {"command": "ssh host uptime",
                                         "dangerouslyDisableSandbox": True}}
    proc, records = run_hook(tmp_path, event)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert [(r["kind"], r["command"], r["cwd"]) for r in records] == [("hatch", "ssh host uptime", "/w")]
    proc, records = run_hook(tmp_path, {**event, "tool_input": {"command": "ls"}})
    assert proc.returncode == 0 and len(records) == 1


def test_unparseable_input_fails_open(tmp_path: Path) -> None:
    proc, _ = run_hook(tmp_path, "not json")
    assert (proc.returncode, proc.stdout) == (0, "")
