"""Reproducers for review findings F2-F6 on `wait-for agent`. Each FAILS while the defect is present."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_wait_for_agent import WAIT_FOR, bash, make_env, run, write_agent  # noqa: E402


def test_f2_fresh_idle_period_after_launched_unit_ends(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "ddd4", [bash("systemd-run --user --unit=build-x -- make")], finished=False)
    (Path(env["STUB_DIR"]) / "units" / "build-x").write_text("3")
    rc, out = run(env, "ddd4", "--idle", "60", "--timeout", "2")
    # The unit just ended and the agent is about to be woken: not stalled yet.
    assert rc == 124, (rc, out)


def _unit_holds(tmp_path: Path, cmd: str) -> tuple[int, str]:
    env = make_env(tmp_path)
    write_agent(tmp_path, "unit1", [bash(cmd)], finished=False)
    (Path(env["STUB_DIR"]) / "units" / "build-x").write_text("1000000")
    return run(env, "unit1", "--idle", "60", "--timeout", "2")


def test_f3a_short_unit_flag_holds_alarm(tmp_path: Path) -> None:
    rc, out = _unit_holds(tmp_path, "systemd-run --user -u build-x -- make")
    assert rc == 124, (rc, out)


def test_f3b_backslash_newline_continuation_holds_alarm(tmp_path: Path) -> None:
    rc, out = _unit_holds(tmp_path, "systemd-run --user \\\n  --unit=build-x -- make")
    assert rc == 124, (rc, out)


def test_f3_control_long_flag_holds_alarm(tmp_path: Path) -> None:
    rc, out = _unit_holds(tmp_path, "systemd-run --user --unit=build-x -- make")
    assert rc == 124, (rc, out)


def test_f4_idle_env_does_not_break_unit_mode(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env["WAIT_FOR_AGENT_IDLE"] = "2h"
    (Path(env["STUB_DIR"]) / "units" / "x").write_text("0")
    p = subprocess.run([str(WAIT_FOR), "unit", "x", "--user", "--timeout", "2"], env=env,
                       capture_output=True, text=True, timeout=10)
    assert p.returncode != 64, (p.returncode, p.stdout, p.stderr)


def test_f5_read_result_with_launch_text_is_not_a_pending_task(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "rrr5", [], finished=True)
    rows = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_r",
         "name": "Read", "input": {"file_path": "/tmp/notes.txt"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_r",
         "content": "1\tCommand running in background with ID: zzz9. Output is being written to: /tmp/z"}]}},
    ]
    t.write_text(t.read_text().removesuffix('{"partial') + "".join(json.dumps(r) + "\n" for r in rows))
    old = time.time() - 600
    os.utime(t, (old, old))
    rc, out = run(env, "rrr5", "--idle", "60", "--timeout", "5")
    assert rc == 0, (rc, out)


def test_f6_failing_systemctl_stall_says_units_were_unreadable(tmp_path: Path) -> None:
    # Decided behaviour: an unreadable user bus counts as "not running" (a false
    # stall wakes the parent once; a false "running" is silence), and the stall
    # line says the unit state could not be read.
    env = make_env(tmp_path)
    sc = Path(env["STUB_DIR"]) / "bin" / "systemctl"
    sc.write_text("#!/bin/sh\necho 'Failed to connect to bus' >&2\nexit 1\n")
    write_agent(tmp_path, "sss6", [bash("systemd-run --user --unit=build-x -- make")], finished=False)
    rc, out = run(env, "sss6", "--idle", "60", "--timeout", "2")
    assert rc == 5, (rc, out)
    assert "systemctl could not read some" in out, out
