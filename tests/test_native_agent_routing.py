"""Native Claude subagent routing and resolved-model telemetry."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = REPO_ROOT / "settings.json"
OBSERVER = REPO_ROOT / "hooks" / "native-agent-route-observer.py"


def settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    block = text.split("---\n", 2)[1]
    result: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip('"')
    return result


def payload(agent_type: str, resolved: str, requested: str | None = None) -> dict:
    tool_input = {
        "description": "route test",
        "prompt": "SECRET_PROMPT_MUST_NOT_BE_LOGGED",
        "subagent_type": agent_type,
    }
    if requested is not None:
        tool_input["model"] = requested
    return {
        "session_id": "session-1",
        "tool_use_id": "tool-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        "tool_response": {
            "status": "async_launched",
            "agentId": "agent-1",
            "resolvedModel": resolved,
            "modelsUsed": [resolved],
            "prompt": "SECRET_RESULT_MUST_NOT_BE_LOGGED",
        },
    }


def run_observer(tmp_path: Path, body: dict | str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["AI_ROUTER_STATE"] = str(tmp_path / "state")
    stdin = body if isinstance(body, str) else json.dumps(body)
    return subprocess.run(
        [sys.executable, "-S", "-E", str(OBSERVER)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def records(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "native-agent-routes.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_unspecified_agents_default_to_current_sonnet():
    assert settings()["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-5-5"
    assert "CLAUDE_CODE_SUBAGENT_MODEL_FORCE" not in settings()["env"]


def test_named_roles_pin_full_model_ids():
    expected = {
        "Explore.md": ("Explore", "claude-haiku-4-5-20251001"),
        "worker-easy.md": ("worker-easy", "claude-sonnet-5-5"),
        "worker-hard.md": ("worker-hard", "claude-opus-5-5"),
        "advisor.md": ("advisor", "claude-fable-5-1"),
    }
    for filename, (name, model) in expected.items():
        metadata = frontmatter(REPO_ROOT / "agents" / filename)
        assert metadata["name"] == name
        assert metadata["model"] == model


def test_settings_observes_resolved_agent_models_without_a_model_hook():
    configured = settings()["hooks"]
    post_commands = [
        hook.get("command", "")
        for entry in configured["PostToolUse"]
        if entry.get("matcher") == "Agent"
        for hook in entry.get("hooks", [])
    ]
    assert post_commands == [
        "python3 -S -E $HOME/.claude/hooks/native-agent-route-observer.py"
    ]
    pre_commands = [
        hook.get("command", "")
        for entry in configured["PreToolUse"]
        for hook in entry.get("hooks", [])
    ]
    assert not any(command.endswith("hooks/ai-router-agent-gate.py") for command in pre_commands)


def test_observer_records_expected_and_resolved_models_without_prompt_text(tmp_path):
    proc = run_observer(tmp_path, payload("Explore", "claude-haiku-4-5-20251001"))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    record = records(tmp_path)[0]
    assert record["agent_type"] == "Explore"
    assert record["requested_model"] is None
    assert record["resolved_model"] == "claude-haiku-4-5-20251001"
    assert record["expected_family"] == "haiku"
    assert record["mismatch"] is False
    raw = (tmp_path / "state" / "native-agent-routes.jsonl").read_text(encoding="utf-8")
    assert "SECRET_PROMPT" not in raw and "SECRET_RESULT" not in raw


def test_observer_flags_a_named_role_overridden_to_the_wrong_family(tmp_path):
    proc = run_observer(
        tmp_path,
        payload("worker-hard", "claude-sonnet-5-5", requested="sonnet"),
    )
    assert proc.returncode == 0
    record = records(tmp_path)[0]
    assert record["expected_family"] == "opus"
    assert record["resolved_family"] == "sonnet"
    assert record["mismatch"] is True


def test_observer_uses_sonnet_as_the_missing_general_purpose_default(tmp_path):
    proc = run_observer(tmp_path, payload("general-purpose", "claude-sonnet-5-5"))
    assert proc.returncode == 0
    record = records(tmp_path)[0]
    assert record["expected_family"] == "sonnet"
    assert record["mismatch"] is False


def test_observer_does_not_guess_fork_model(tmp_path):
    proc = run_observer(tmp_path, payload("fork", "claude-opus-5-5[1m]"))
    assert proc.returncode == 0
    record = records(tmp_path)[0]
    assert record["expected_family"] is None
    assert record["mismatch"] is None


def test_observer_parallel_appends_are_valid_and_private(tmp_path):
    def one(index: int) -> int:
        body = payload("worker-easy", "claude-sonnet-5-5")
        body["tool_use_id"] = f"tool-{index}"
        return run_observer(tmp_path, body).returncode

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(one, range(24))) == [0] * 24
    assert len(records(tmp_path)) == 24
    state = tmp_path / "state"
    log = state / "native-agent-routes.jsonl"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_observer_is_silent_and_fail_open_on_bad_or_irrelevant_input(tmp_path):
    for body in ("not json", {"tool_name": "Read"}, {"tool_name": "Agent"}):
        proc = run_observer(tmp_path, body)
        assert (proc.returncode, proc.stdout) == (0, "")
    assert not (tmp_path / "state" / "native-agent-routes.jsonl").exists()
