"""Reproducers for review round 2 findings R1-R9 on `wait-for agent`. Each FAILS while the defect is present."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_wait_for_agent import WAIT_FOR, bash, bg_launch, make_env, run, write_agent  # noqa: E402


def append(t: Path, rows: list[dict], age: int = 600) -> None:
    body = t.read_text().removesuffix('{"partial')
    t.write_text(body + "".join(json.dumps(r) + "\n" for r in rows))
    old = time.time() - age
    os.utime(t, (old, old))


def launch_rows(tool_id: str, text: str, *, bg: bool, cmd: str = "./x.sh") -> list[dict]:
    return [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
         "input": {"command": cmd, **({"run_in_background": True} if bg else {})}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "waiting"}], "stop_reason": "end_turn"}},
    ]


def notif(task: str) -> str:
    return f"<task-notification><task-id>{task}</task-id><status>completed</status></task-notification>"


def test_r1_notification_after_last_end_turn_is_not_finished(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rr1", [], finished=True)
    bg_launch(t, "toolu_1", "bq1", "./a.sh", done=True, age=0, resumed=False)  # notification row is last
    rc, out = run(env, "rr1", "--idle", "2", "--timeout", "4")
    assert rc == 5, (rc, out)


def _r2(tmp_path: Path, row: dict) -> tuple[int, str]:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rr2", [], finished=True)
    bg_launch(t, "toolu_1", "bq1", "./a.sh", done=False)
    # The notification starts a turn, which ends (as in the 2026-11-22 transcript).
    append(t, [row, {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}],
                                                      "stop_reason": "end_turn"}}])
    return run(env, "rr2", "--idle", "2", "--timeout", "6")


def test_r2a_queue_operation_notification_is_seen(tmp_path: Path) -> None:
    rc, out = _r2(tmp_path, {"type": "queue-operation", "operation": "enqueue", "content": notif("bq1")})
    assert rc == 0, (rc, out)


def test_r2b_attachment_notification_is_seen(tmp_path: Path) -> None:
    rc, out = _r2(tmp_path, {"type": "attachment", "attachment": {"type": "queued_command", "prompt": notif("bq1")}})
    assert rc == 0, (rc, out)


def test_r3_leading_zero_idle_is_eight_seconds(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "rr3", [bash("ls")], finished=False)
    rc, out = run(env, "rr3", "--idle", "08")
    assert rc == 5, (rc, out)
    assert "idle limit 8s" in out, out


def test_r4_task_id_with_underscore_matches_its_completion(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rr4", [], finished=True)
    bg_launch(t, "toolu_1", "bash_1", "./a.sh", done=True)
    rc, out = run(env, "rr4", "--idle", "2", "--timeout", "6")
    assert rc == 0, (rc, out)


def test_r4b_task_id_with_dash_matches_its_completion(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rr4b", [], finished=True)
    bg_launch(t, "toolu_1", "bg-1", "./a.sh", done=True)
    rc, out = run(env, "rr4b", "--idle", "2", "--timeout", "6")
    assert rc == 0, (rc, out)


R5_SYSTEMCTL = """#!/usr/bin/env bash
end=$(cat "$STUB_DIR/end")
now=$(date +%s.%N)
echo "$now" >> "$STUB_DIR/polls.log"
if (( $(echo "$now < $end" | bc -l) )); then printf 'ActiveState=active\\nSubState=running\\n'
else echo "$now exited" >> "$STUB_DIR/exited.log"; printf 'ActiveState=active\\nSubState=exited\\n'; fi
"""


def test_r5_stall_comes_a_full_idle_after_the_unit_ended(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    stub = Path(env["STUB_DIR"])
    sc = stub / "bin" / "systemctl"
    sc.write_text(R5_SYSTEMCTL.replace("bc -l", "python3 -c 'import sys;sys.exit(0 if float(sys.argv[1])<float(sys.argv[2]) else 1)' $now $end >/dev/null; echo $?").replace(
        '(( $(echo "$now < $end" | python3 -c \'import sys;sys.exit(0 if float(sys.argv[1])<float(sys.argv[2]) else 1)\' $now $end >/dev/null; echo $?) ))', "python3 -c 'import sys;sys.exit(0 if float(sys.argv[1])<float(sys.argv[2]) else 1)' $now $end"))
    write_agent(tmp_path, "rr5", [bash("systemd-run --user --unit=build-z -- make")], finished=False)
    # The unit ends 2.5 s after arming, inside the 3 s before the old code
    # looked again: it then stalled ~0.5 s after the end, now ~2.5 s.
    end = time.time() + 2.5
    (stub / "end").write_text(f"{end}")
    rc, out = run(env, "rr5", "--idle", "3", "--timeout", "20")
    stalled_at = time.time()
    assert rc == 5, (rc, out)
    print("unit ended; stall after", stalled_at - end)
    # `date +%s` has one-second resolution, so "a full idle" is idle - 1 s at
    # worst; before the fix it came one poll interval early, after a 0.6 s unit.
    assert stalled_at - end >= 2, f"stall only {stalled_at - end:.2f}s after the unit ended"


def test_r6_agent_run_wait_for_unit_counts_as_launched_unit(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "rr6", [bash("~/.claude/scripts/wait-for unit build-q --user", bg=True)], finished=False)
    (Path(env["STUB_DIR"]) / "units" / "build-q").write_text("1000000")
    rc, out = run(env, "rr6", "--idle", "60", "--timeout", "2")
    assert rc == 124, (rc, out)


def _r7(tmp_path: Path, text: str) -> tuple[int, str]:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rr7", [], finished=True)
    append(t, launch_rows("toolu_f", text, bg=False))
    return run(env, "rr7", "--idle", "2", "--timeout", "6")


PENDING_X = "last: pending background `./x.sh`"
PENDING_A = "last: pending background `./a.sh`"


def test_r7a_auto_backgrounded_moved_text(tmp_path: Path) -> None:
    rc, out = _r7(tmp_path, "Command was moved to the background with ID: zz9. Output is being written to: /tmp/x/zz9.output")
    assert rc == 5, (rc, out)
    assert PENDING_X in out, out


def test_r7b_exact_launch_text_without_run_in_background(tmp_path: Path) -> None:
    rc, out = _r7(tmp_path, "Command running in background with ID: zz9. Output is being written to: /tmp/x/zz9.output")
    assert rc == 5, (rc, out)
    assert PENDING_X in out, out


def _r8(tmp_path: Path, rows: list[dict]) -> tuple[int, str]:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rr8", [], finished=True)
    bg_launch(t, "toolu_1", "bq1", "./a.sh", done=False)
    append(t, rows)
    return run(env, "rr8", "--idle", "2", "--timeout", "6")


def test_r8a_marker_in_assistant_text_does_not_clear_task(tmp_path: Path) -> None:
    rc, out = _r8(tmp_path, [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "the log has <task-id>bq1</task-id>"}]}}])
    assert rc == 5, (rc, out)
    assert PENDING_A in out, out


def test_r8b_marker_in_tool_result_does_not_clear_task(tmp_path: Path) -> None:
    rows = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_c", "name": "Bash",
         "input": {"command": "cat log"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_c",
         "content": "<task-id>bq1</task-id>"}]}},
    ]
    rc, out = _r8(tmp_path, rows)
    assert rc == 5, (rc, out)
    assert PENDING_A in out, out


def test_r9_deleted_transcript_does_not_hang_until_timeout(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rr9", [bash("ls")], finished=False, age=0)
    p = subprocess.Popen([str(WAIT_FOR), "agent", "rr9", "--idle", "2", "--timeout", "8"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.7)
    t.unlink()
    out, err = p.communicate(timeout=30)
    assert p.returncode == 3, (p.returncode, out, err)
    assert "no transcript" in out, out
