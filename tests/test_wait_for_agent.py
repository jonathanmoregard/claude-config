"""`wait-for agent` and the hook that asks the parent to arm it.

`wait-for agent` is a quiet-only alarm: it exits 5 when neither the subagent's
transcript nor its meta file has been touched for the idle limit, and never
reads the transcript's contents. A synthetic transcript under a temporary HOME
stands in for a real background agent; `sleep` is a stub, so every case
finishes in a few seconds at most, and `notify-send` is a logging stub so a
case can prove no desktop notification is sent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "runtime" if (REPO / "runtime").is_dir() else REPO
WAIT_FOR = RUNTIME / "scripts" / "wait-for"
HOOK = RUNTIME / "hooks" / "agent-idle-alarm.py"

SYSTEMCTL_ACTIVATING = """#!/usr/bin/env bash
printf 'LoadState=loaded\\nActiveState=activating\\nSubState=start\\nTransient=no\\nType=simple\\n'
"""


def make_env(tmp: Path) -> dict[str, str]:
    stub = tmp / "stub"
    (stub / "bin").mkdir(parents=True)
    for name, body in {
        "notify-send": '#!/bin/sh\necho "$*" >> "$STUB_DIR/notify.log"\n',
        "sleep": "#!/bin/sh\nexit 0\n",
    }.items():
        p = stub / "bin" / name
        p.write_text(body)
        p.chmod(0o755)
    return {
        **os.environ,
        "HOME": str(tmp / "home"),
        "STUB_DIR": str(stub),
        "PATH": f"{stub / 'bin'}:{os.environ['PATH']}",
    }


def write_agent(tmp: Path, agent: str, *, age: int = 600, meta_age: int | None = None,
                meta: dict | None = {"agentType": "worker-hard"}) -> Path:
    """A transcript `age` seconds old and, unless `meta` is None, its meta file."""
    d = tmp / "home" / ".claude" / "projects" / "p" / "sess" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    t = d / f"agent-{agent}.jsonl"
    t.write_text('{"type": "assistant"}\n{"partial')
    now = time.time()
    os.utime(t, (now - age, now - age))
    if meta is not None:
        m = d / f"agent-{agent}.meta.json"
        m.write_text(json.dumps(meta))
        mt = now - (age if meta_age is None else meta_age)
        os.utime(m, (mt, mt))
    return t


def run(env: dict[str, str], *args: str) -> tuple[int, str]:
    p = subprocess.run([str(WAIT_FOR), "agent", *args], env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout


def notified(env: dict[str, str]) -> str:
    log = Path(env["STUB_DIR"]) / "notify.log"
    return log.read_text() if log.exists() else ""


def test_quiet_transcript_and_meta_is_a_stall(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "bbb2")
    rc, out = run(env, "bbb2", "--idle", "60")
    assert rc == 5, out
    assert out.startswith("agent bbb2 stalled: no activity for 10m")
    assert "(idle limit 60s)" in out
    # The exit wakes the parent; a stall sends no desktop notification.
    assert notified(env) == ""


def test_fresh_transcript_is_not_a_stall(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "ccc3", age=0)
    rc, out = run(env, "ccc3", "--idle", "7200", "--timeout", "2")
    assert rc == 124, out
    assert notified(env) == ""


def test_fresh_meta_with_old_transcript_is_not_a_stall(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "ddd4", age=600, meta_age=0)
    rc, out = run(env, "ddd4", "--idle", "300", "--timeout", "2")
    assert rc == 124, out
    assert notified(env) == ""


def test_absent_meta_and_old_transcript_is_a_stall(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "eee5", meta=None)
    rc, out = run(env, "eee5", "--idle", "60")
    assert rc == 5, out
    assert "eee5 stalled" in out
    assert notified(env) == ""


def test_finished_meta_is_not_a_finish_signal(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "fff6", meta={"finished": True})
    rc, out = run(env, "fff6", "--idle", "60")
    assert rc == 5, out
    write_agent(tmp_path, "fff7", age=0, meta={"finished": True})
    rc, out = run(env, "fff7", "--idle", "7200", "--timeout", "2")
    assert rc == 124, out


def test_unknown_agent_is_exit_3_after_the_missing_limit(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rc, out = run(env, "ggg8", "--idle", "1")
    assert rc == 3 and "no transcript" in out


def test_deleted_transcript_is_exit_3(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    t = write_agent(tmp_path, "hhh9", age=0)
    # --idle 30, not 2: mtimes are whole seconds, so a short limit can call the
    # fresh transcript stalled before the unlink lands (sleep is stubbed).
    p = subprocess.Popen([str(WAIT_FOR), "agent", "hhh9", "--idle", "30", "--timeout", "8"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.7)
    t.unlink()
    out, err = p.communicate(timeout=30)
    assert p.returncode == 3, (p.returncode, out, err)
    assert "no transcript" in out


def test_missing_limit_is_120_seconds_of_naps(tmp_path: Path) -> None:
    """At the default step (30 s) exit 3 comes after four naps, not three."""
    env = make_env(tmp_path)
    (Path(env["STUB_DIR"]) / "bin" / "sleep").write_text(
        '#!/bin/sh\necho "$1" >> "$STUB_DIR/naps.log"\n')
    rc, out = run(env, "jjj1", "--idle", "7200")
    assert rc == 3 and "no transcript" in out
    naps = (Path(env["STUB_DIR"]) / "naps.log").read_text().split()
    assert naps == ["30"] * 4, naps


def test_transcript_seen_then_deleted_gets_the_full_missing_limit(tmp_path: Path) -> None:
    """The 120 s missing limit restarts once the transcript is found: missing
    for 2 naps, present for 1, then deleted -> 4 more naps, 7 in all."""
    env = make_env(tmp_path)
    d = tmp_path / "home" / ".claude" / "projects" / "p" / "sess" / "subagents"
    d.mkdir(parents=True)
    t = d / "agent-mmm3.jsonl"
    (Path(env["STUB_DIR"]) / "bin" / "sleep").write_text(
        '#!/bin/sh\necho "$1" >> "$STUB_DIR/naps.log"\n'
        'n=$(wc -l < "$STUB_DIR/naps.log")\n'
        f'[ "$n" -eq 2 ] && echo x > "{t}"\n'
        f'[ "$n" -eq 3 ] && rm -f "{t}"\n'
        'exit 0\n')
    rc, out = run(env, "mmm3", "--idle", "7200")
    assert rc == 3 and "no transcript" in out, out
    naps = (Path(env["STUB_DIR"]) / "naps.log").read_text().split()
    assert len(naps) == 7, naps


def test_default_timeout_outlasts_the_default_idle(tmp_path: Path) -> None:
    """Agent mode's default timeout (86400) must exceed the 2 h idle limit, or
    the waiter times out (124) before the alarm can ever fire. A fake clock
    advances 3700 s per `date +%s`; with a 3600 s timeout this exits 124."""
    env = make_env(tmp_path)
    real_date = shutil.which("date")
    assert real_date
    clock = Path(env["STUB_DIR"]) / "clock"
    clock.write_text(str(int(time.time())))
    date = Path(env["STUB_DIR"]) / "bin" / "date"
    date.write_text(
        '#!/bin/sh\n[ "$*" = "+%s" ] || exec ' + real_date + ' "$@"\n'
        't=$(cat "$STUB_DIR/clock"); echo "$t"; echo $((t + 3700)) > "$STUB_DIR/clock"\n')
    date.chmod(0o755)
    write_agent(tmp_path, "kkk2", age=0)
    env.pop("WAIT_FOR_AGENT_IDLE", None)
    rc, out = run(env, "kkk2")
    assert rc == 5, out


def test_bad_id_is_a_usage_error(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rc, _ = run(env, "a;b")
    assert rc == 64


def test_leading_zero_idle_is_eight_seconds(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "iii10")
    rc, out = run(env, "iii10", "--idle", "08")
    assert rc == 5, out
    assert "(idle limit 8s)" in out


def test_invalid_idle_is_a_usage_error_in_agent_mode_only(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    write_agent(tmp_path, "jjj11")
    rc, _ = run(env, "jjj11", "--idle", "2h")
    assert rc == 64
    rc, _ = run(env, "jjj11", "--idle", "0")
    assert rc == 64
    # The idle limit is read from WAIT_FOR_AGENT_IDLE too, but only by `agent`.
    rc, _ = run({**env, "WAIT_FOR_AGENT_IDLE": "2h"}, "jjj11")
    assert rc == 64
    (Path(env["STUB_DIR"]) / "bin" / "systemctl").write_text(SYSTEMCTL_ACTIVATING)
    (Path(env["STUB_DIR"]) / "bin" / "systemctl").chmod(0o755)
    cmd = [str(WAIT_FOR), "unit", "x", "--user", "--timeout", "2"]
    clean = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
    stray = subprocess.run(cmd, env={**env, "WAIT_FOR_AGENT_IDLE": "2h"},
                           capture_output=True, text=True, timeout=10)
    assert (stray.returncode, stray.stdout) == (clean.returncode, clean.stdout)
    assert stray.returncode != 64


def test_idle_is_agent_only(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    p = subprocess.run([str(WAIT_FOR), "unit", "x", "--idle", "5"], env=env,
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 64


def test_non_agent_mode_leading_zero_timeout_is_eight_seconds(tmp_path: Path) -> None:
    # `--timeout 08` was octal-parsed for every mode: "value too great for base".
    env = make_env(tmp_path)
    (Path(env["STUB_DIR"]) / "bin" / "systemctl").write_text(SYSTEMCTL_ACTIVATING)
    (Path(env["STUB_DIR"]) / "bin" / "systemctl").chmod(0o755)
    started = time.time()
    p = subprocess.run([str(WAIT_FOR), "unit", "x", "--user", "--timeout", "08"], env=env,
                       capture_output=True, text=True, timeout=40)
    took = time.time() - started
    assert "value too great" not in p.stderr, (p.returncode, p.stdout, p.stderr)
    assert p.returncode == 124 and "timed out after 8s" in p.stdout and took >= 6, \
        (p.returncode, p.stdout, p.stderr, took)


def hook(payload: object, idle: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "WAIT_FOR_AGENT_IDLE"}
    if idle is not None:
        env["WAIT_FOR_AGENT_IDLE"] = idle
    p = subprocess.run(["python3", "-S", "-E", str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10, env=env)
    assert p.returncode == 0
    return p.stdout


def test_hook_names_the_wait_for_background_dispatch() -> None:
    out = json.loads(hook({"tool_name": "Agent", "tool_response": {
        "status": "async_launched", "agentId": "a0eb7360ac337731d"}}))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    text = spec["additionalContext"]
    assert "`~/.claude/scripts/wait-for agent a0eb7360ac337731d`" in text
    assert "2 hours" in text and "TaskStop" in text
    assert "exits 5" in text and "124" in text
    assert "exits 0" not in text


def test_hook_silent_otherwise() -> None:
    launched = {"status": "async_launched", "agentId": "abc"}
    assert hook({"tool_name": "Agent", "tool_response": {"status": "completed", "agentId": "abc"}}) == ""
    assert hook({"tool_name": "Agent", "agent_id": "sub", "tool_response": launched}) == ""
    assert hook({"tool_name": "Bash", "tool_response": launched}) == ""
    assert hook({"tool_name": "Agent", "tool_response": {**launched, "agentId": "a b;c"}}) == ""
    assert hook({"tool_name": "Agent", "tool_response": {**launched, "agentId": "abc\n"}}) == ""


def test_hook_names_a_custom_idle_limit() -> None:
    launched = {"tool_name": "Agent", "tool_response": {"status": "async_launched", "agentId": "abc"}}
    text = lambda idle: json.loads(hook(launched, idle))["hookSpecificOutput"]["additionalContext"]
    assert "quiet for 900 seconds" in text("900")
    assert "quiet for 2 hours" in text("7200")
    assert "quiet for 2 hours" in text("0")
    assert "quiet for 2 hours" in text("x")
    assert "needs a new waiter" in text("900")
    p = subprocess.run(["python3", "-S", "-E", str(HOOK)], input="not json",
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0 and p.stdout == ""
