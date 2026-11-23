"""Verifier for review round 3 findings on `wait-for agent` (A1-A4, B1-B10). Each test FAILS iff the claimed defect is present."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_wait_for_agent import WAIT_FOR, bash, bg_launch, hook, make_env, notified, run, write_agent  # noqa: E402
from test_wait_for_agent_review2 import append  # noqa: E402


def _end_turn() -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}}


# ---- A1 -------------------------------------------------------------------
FAST_DATE = """#!/usr/bin/env bash
# `date +%s` is real time on the first two calls (the deadline, then the first
# look at a fresh transcript) and 20000 s ahead on every later one.
n=$(cat "$STUB_DIR/date.n" 2>/dev/null || echo 0)
echo $((n + 1)) > "$STUB_DIR/date.n"
if [ "$n" -lt 2 ]; then exec REALDATE "$@"; fi
echo $(( $(REALDATE +%s) + 20000 ))
"""


def test_a1_agent_default_timeout_outlasts_the_old_14400s_default(tmp_path: Path) -> None:
    # The first draft of this reproducer passed `--timeout 6` and expected a stall
    # at ~7 s (3 s of activity + 4 s idle), which no timeout of 6 s allows. The
    # defect is the default itself, so observe it: with no --timeout, a stall
    # found 20000 s of wall clock after arming must still be reported (5), not
    # lost to a timeout (124) as with the old default of 14400 s.
    env = make_env(tmp_path)
    date = Path(env["STUB_DIR"]) / "bin" / "date"
    date.write_text(FAST_DATE.replace("REALDATE", shutil.which("date") or "/usr/bin/date"))
    date.chmod(0o755)
    write_agent(tmp_path, "aa1", [bash("ls")], finished=False, age=0)
    rc, out = run(env, "aa1", "--idle", "4")
    assert rc == 5, (rc, out)
    assert "aa1 stalled" in out and "aa1 stalled" in notified(env)
    p = subprocess.run([str(WAIT_FOR)], capture_output=True, text=True, env=env)
    assert "agent default 86400" in p.stderr, p.stderr


# ---- A2 -------------------------------------------------------------------
def test_a2_hook_context_covers_timeout_exit_124_and_rearm() -> None:
    out = json.loads(hook({"tool_name": "Agent", "session_id": "s", "hook_event_name": "PostToolUse",
                           "tool_response": {"status": "async_launched", "agentId": "abc123def"}}))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "124" in ctx, ctx
    assert "re-arm" in ctx.lower() or "rearm" in ctx.lower() or "arm it again" in ctx.lower(), ctx


# ---- A3 (real TaskOutput result format, local_bash, copied from a real transcript) ----
def test_a3_completion_read_via_taskoutput_clears_the_pending_task(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "aa3", [], finished=True)
    bg_launch(t, "toolu_1", "be8ptfdf0", "./a.sh", done=False)
    append(t, [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_o", "name": "TaskOutput",
         "input": {"task_id": "be8ptfdf0", "block": True, "timeout": 420000}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_o", "content":
         "<retrieval_status>success</retrieval_status>\n\n<task_id>be8ptfdf0</task_id>\n\n"
         "<task_type>local_bash</task_type>\n\n<status>completed</status>\n\n<exit_code>0</exit_code>\n\n<output>\nok\n</output>"}]}},
        _end_turn(),
    ])
    rc, out = run(env, "aa3", "--idle", "2", "--timeout", "6")
    assert rc == 0, (rc, out)


# ---- A4 -------------------------------------------------------------------
def test_a4_child_background_agent_that_never_reported_is_pending(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "aa4", [], finished=True)
    append(t, [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_a", "name": "Agent",
         "input": {"description": "child", "prompt": "x", "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": [
            {"type": "text", "text": "Async agent launched successfully.\nagentId: abc123 (internal ID)\nThe agent is working in the background."}]}]}},
        _end_turn(),
    ])
    rc, out = run(env, "aa4", "--idle", "2", "--timeout", "6")
    assert rc == 5, (rc, out)
    assert "aa4 stalled" in out


# ---- B1 -------------------------------------------------------------------
TIMED_SYSTEMCTL = """#!/usr/bin/env bash
end=$(cat "$STUB_DIR/end")
now=$(date +%s.%N)
if awk -v n="$now" -v e="$end" 'BEGIN{exit !(n<e)}'; then
  printf 'ActiveState=active\\nSubState=running\\n'
else
  printf 'ActiveState=active\\nSubState=exited\\n'
fi
"""


def test_b1_fresh_transcript_stall_comes_a_full_idle_after_the_unit_ended(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    stub = Path(env["STUB_DIR"])
    (stub / "bin" / "systemctl").write_text(TIMED_SYSTEMCTL)
    write_agent(tmp_path, "bb1", [bash("systemd-run --user --unit=build-b1 -- make")], finished=False, age=0)
    end = time.time() + 1
    (stub / "end").write_text(f"{end}")
    rc, out = run(env, "bb1", "--idle", "3", "--timeout", "20")
    stalled_at = time.time()
    assert rc == 5, (rc, out)
    # `date +%s` has 1 s resolution: a full idle is >= idle - 1 s.
    assert stalled_at - end >= 2, f"stall only {stalled_at - end:.2f}s after the unit ended"


# ---- B2 is a repeat-run measurement of test_wait_for_agent.py, no test here ----


# ---- B3-B5: strengthened in place in test_wait_for_agent_review2.py (r1, r3, r7-r9) ----


# ---- B6 -------------------------------------------------------------------
def test_b6_leading_zero_timeout_is_a_usage_error_or_eight_seconds(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "bb6", [bash("ls")], finished=False, age=0)
    started = time.time()
    p = subprocess.run([str(WAIT_FOR), "agent", "bb6", "--idle", "7200", "--timeout", "08"], env=env,
                       capture_output=True, text=True, timeout=40)
    took = time.time() - started
    assert "value too great" not in p.stderr, (p.returncode, p.stdout, p.stderr)
    assert (p.returncode == 124 and "timed out after 8s" in p.stdout and took >= 6) or p.returncode == 64, \
        (p.returncode, p.stdout, p.stderr, took)


ACTIVATING_SYSTEMCTL = """#!/usr/bin/env bash
printf 'LoadState=loaded\\nActiveState=activating\\nSubState=start\\nTransient=no\\nType=simple\\n'
"""


def test_b6b_non_agent_mode_leading_zero_timeout_is_eight_seconds(tmp_path: Path) -> None:
    # `--timeout 08` was octal-parsed for every mode: "value too great for base".
    env = make_env(tmp_path)
    (Path(env["STUB_DIR"]) / "bin" / "systemctl").write_text(ACTIVATING_SYSTEMCTL)
    started = time.time()
    p = subprocess.run([str(WAIT_FOR), "unit", "x", "--user", "--timeout", "08"], env=env,
                       capture_output=True, text=True, timeout=40)
    took = time.time() - started
    assert "value too great" not in p.stderr, (p.returncode, p.stdout, p.stderr)
    assert p.returncode == 124 and "timed out after 8s" in p.stdout and took >= 6, \
        (p.returncode, p.stdout, p.stderr, took)


# ---- B7 -------------------------------------------------------------------
def test_b7_stopping_task_bq10_does_not_clear_pending_bq1(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "bb7", [], finished=True)
    bg_launch(t, "toolu_1", "bq1", "./a.sh", done=False)
    append(t, [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_s", "name": "TaskStop", "input": {"task_id": "bq10"}}]}}])
    rc, out = run(env, "bb7", "--idle", "2", "--timeout", "6")
    assert rc == 5, (rc, out)
    assert "last: pending background `./a.sh`" in out


# ---- B8 -------------------------------------------------------------------
def _holds_off(tmp_path: Path, name: str, unit: str, command: str, bg: bool = False) -> tuple[int, str]:
    env = make_env(tmp_path)
    write_agent(tmp_path, name, [bash(command, bg=bg)], finished=False)
    (Path(env["STUB_DIR"]) / "units" / unit).write_text("1000000")
    return run(env, name, "--idle", "60", "--timeout", "2")


def test_b8a_quoted_wait_for_job_slug_names_its_unit(tmp_path: Path) -> None:
    # wait_job runs `systemd-run --user --unit="$target"`: the unit is the bare slug.
    rc, out = _holds_off(tmp_path, "bb8a", "my-job", '~/.claude/scripts/wait-for job "my-job" -- ./run.sh', bg=True)
    assert rc == 124, (rc, out)


def test_b8b_systemd_run_attached_short_unit_option(tmp_path: Path) -> None:
    rc, out = _holds_off(tmp_path, "bb8b", "build-x", "systemd-run --user -ubuild-x -- make")
    assert rc == 124, (rc, out)


# ---- B9 -------------------------------------------------------------------
def test_b9_plain_user_prompt_after_last_end_turn_means_resuming(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "bb9", [bash("ls")], finished=True)
    append(t, [_end_turn(), {"type": "user", "message": {"content": "please also do X"}}])
    rc, out = run(env, "bb9", "--idle", "2", "--timeout", "6")
    assert rc == 5, (rc, out)
