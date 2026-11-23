"""night-proceed: answer a night-time question once, only while the screen is locked."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "runtime" if (REPO / "runtime").is_dir() else REPO
HOOK = RUNTIME / "hooks" / "night-proceed.py"
PYTHON = shutil.which("python3") or "python3"

FAKE_CLAUDE = """#!/bin/sh
# Records each classifier call (args, stdin) and answers with $FAKE_REPLY.
n=$(ls "$FAKE_DIR"/call.* 2>/dev/null | wc -l)
printf '%s\\n' "$@" > "$FAKE_DIR/call.$n"
cat > "$FAKE_DIR/stdin.$n"
pwd > "$FAKE_DIR/cwd.$n"
printf '%s\\n' "${FAKE_REPLY-QUESTION}"
exit "${FAKE_RC:-0}"
"""


def fake_bin(tmp: Path) -> Path:
    """A bin dir with the fake `claude`; calls land in tmp/claude-calls."""
    bindir = tmp / "fakebin"
    if not bindir.exists():
        bindir.mkdir()
        (tmp / "claude-calls").mkdir()
        stub = bindir / "claude"
        stub.write_text(FAKE_CLAUDE)
        stub.chmod(0o755)
    return bindir


def calls(tmp: Path) -> list[list[str]]:
    d = tmp / "claude-calls"
    return [d.joinpath(f"call.{i}").read_text().splitlines()
            for i in range(len(list(d.glob("call.*"))))] if d.exists() else []


def run(tmp: Path, payload: object, *, now: str | None = "02:00", lock: str | None = "locked",
        reply: str = "QUESTION", extra: dict[str, str] | None = None,
        raw: str | None = None) -> dict | None:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_NIGHT_PROCEED") and k != "CLAUDE_CODE_ENTRYPOINT"}
    env.update(HOME=str(tmp), CLAUDE_CODE_ENTRYPOINT="cli", FAKE_DIR=str(tmp / "claude-calls"),
               FAKE_REPLY=reply, PATH=f"{fake_bin(tmp)}:{os.environ['PATH']}")
    if now is not None:
        env["CLAUDE_NIGHT_PROCEED_TEST_NOW"] = now
    if lock is not None:
        env["CLAUDE_NIGHT_PROCEED_TEST_LOCK"] = lock
    env.update(extra or {})
    p = subprocess.run([PYTHON, "-S", "-E", str(HOOK)], env=env, text=True,
                       input=raw if raw is not None else json.dumps(payload),
                       capture_output=True, timeout=15)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def stop(msg: str, **kw: object) -> dict:
    return {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": msg, **kw}


def ask(question: str, **kw: object) -> dict:
    return {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": question}]}, **kw}


AB = "Two ways to do it:\n\nA) keep the cache (recommended)\nB) drop it\n\nWhich one?"


def test_night_locked_question_blocks_the_stop(tmp_path: Path) -> None:
    out = run(tmp_path, stop(AB))
    assert out == {"decision": "block", "reason": out["reason"]}
    assert out["reason"].startswith("The user is likely asleep")
    assert "force pushes" in out["reason"] and "break a directive the user gave" in out["reason"]


def test_classifier_is_sonnet_without_tools_hooks_or_session(tmp_path: Path) -> None:
    run(tmp_path, stop(AB))
    (args,) = calls(tmp_path)
    assert args[:3] == ["-p", "--model", "sonnet"]
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    sent = (tmp_path / "claude-calls" / "stdin.0").read_text()
    assert sent.startswith("<message>\n") and AB in sent and sent.rstrip().endswith("</message>")


def test_long_message_sends_its_end(tmp_path: Path) -> None:
    run(tmp_path, stop("x" * 20000 + "\n\nShall I go on?"))
    sent = (tmp_path / "claude-calls" / "stdin.0").read_text()
    assert "Shall I go on?" in sent and len(sent) < 7000


def test_classifier_done_or_failing_means_no_reply(tmp_path: Path) -> None:
    assert run(tmp_path, stop("Done 1. Tests pass."), reply="DONE") is None
    assert run(tmp_path, stop("Done 2?"), reply="QUESTION, probably") is None
    assert run(tmp_path, stop("Done 3?"), reply="") is None
    assert run(tmp_path, stop("Done 4?"), extra={"FAKE_RC": "1"}) is None
    assert run(tmp_path, stop("Done 5?"), extra={"PATH": "/nonexistent"}) is None
    # Trailing punctuation or markup around the one word is still the word.
    assert run(tmp_path, stop("Q 6?"), reply="**Question.**")


def test_no_classifier_call_outside_the_window(tmp_path: Path) -> None:
    assert run(tmp_path, stop(AB), now="14:00") is None
    assert run(tmp_path, stop(AB), now="09:00") is None
    assert run(tmp_path, stop(AB), lock="unlocked") is None
    assert run(tmp_path, stop(AB), lock="unreadable") is None
    assert run(tmp_path, stop(AB), extra={"CLAUDE_NIGHT_PROCEED": "0"}) is None
    assert run(tmp_path, stop(AB, stop_hook_active=True)) is None
    assert run(tmp_path, stop(AB, agent_id="a1")) is None
    assert run(tmp_path, stop("   ")) is None
    assert calls(tmp_path) == []


def test_window_edges(tmp_path: Path) -> None:
    assert run(tmp_path, stop("Edge 1?"), now="22:00")
    assert run(tmp_path, stop("Edge 2?"), now="23:59")
    assert run(tmp_path, stop("Edge 3?"), now="08:59")
    assert run(tmp_path, stop("Edge 4?"), now="21:59") is None


def test_same_question_twice_is_let_through_without_a_second_call(tmp_path: Path) -> None:
    assert run(tmp_path, stop(AB))
    assert run(tmp_path, stop(AB)) is None
    assert len(calls(tmp_path)) == 1
    assert run(tmp_path, stop("A different question?"))


def test_ask_user_question_denied_once_without_classifier(tmp_path: Path) -> None:
    out = run(tmp_path, ask("Which cache policy?"))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert spec["permissionDecisionReason"].startswith("The user is likely asleep")
    assert run(tmp_path, ask("Which cache policy?")) is None
    assert calls(tmp_path) == []


def test_one_reply_per_turn_across_ask_and_stop(tmp_path: Path) -> None:
    # Denied ask, then the session ends that turn on the same question as text:
    # it already had PROMPT this turn, so the stop is let through.
    assert run(tmp_path, ask("Which license?", prompt_id="p1"))
    assert run(tmp_path, stop("Which license?", prompt_id="p1")) is None
    # A later turn asking again is a new turn.
    assert run(tmp_path, stop("Which license, MIT or Apache?", prompt_id="p2"))


def test_one_reply_per_turn_after_a_blocked_stop(tmp_path: Path) -> None:
    # Review round 3: a blocked stop, then an AskUserQuestion (or a reworded
    # one) in the same turn got a second and third reply.
    assert run(tmp_path, stop("Which license?", prompt_id="p1"))
    assert run(tmp_path, ask("Which license?", prompt_id="p1")) is None
    assert run(tmp_path, ask("License: MIT or Apache?", prompt_id="p1")) is None
    assert run(tmp_path, ask("License: MIT or Apache?", prompt_id="p2"))


def test_other_tools_and_garbage(tmp_path: Path) -> None:
    assert run(tmp_path, {**ask("x?"), "tool_name": "Bash"}) is None
    assert run(tmp_path, stop(AB, session_id="../etc")) is None
    assert run(tmp_path, None, raw="not json") is None


def fake_gdbus(tmp: Path, reply: str, rc: int = 0) -> str:
    """A PATH whose gdbus prints `reply` and exits `rc`, ahead of the fake claude."""
    bindir = tmp / f"gdbus-{abs(hash((reply, rc)))}"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gdbus"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{reply}'\nexit {rc}\n")
    stub.chmod(0o755)
    return f"{bindir}:{fake_bin(tmp)}:{os.environ['PATH']}"


def test_desktop_lock_query_without_seam(tmp_path: Path) -> None:
    # The real query path, with gdbus faked: locked blocks, everything else
    # (unlocked, a bus error, no gdbus at all) means present.
    assert run(tmp_path, stop("Lock 1?"), lock=None, extra={"PATH": fake_gdbus(tmp_path, "(true,)")})
    assert run(tmp_path, stop("Lock 2?"), lock=None, extra={"PATH": fake_gdbus(tmp_path, "(false,)")}) is None
    assert run(tmp_path, stop("Lock 3?"), lock=None,
               extra={"PATH": fake_gdbus(tmp_path, "Error: no bus", rc=1)}) is None
    assert run(tmp_path, stop("Lock 4?"), lock=None, extra={"PATH": f"{fake_bin(tmp_path)}"}) is None


def test_classifier_ignores_the_session_settings_and_cwd(tmp_path: Path) -> None:
    run(tmp_path, stop(AB))
    (args,) = calls(tmp_path)
    assert args[args.index("--setting-sources") + 1] == ""
    cwd = (tmp_path / "claude-calls" / "cwd.0").read_text().strip()
    assert Path(cwd).resolve() == Path(tempfile.gettempdir()).resolve(), cwd


def test_classifier_runs_unlocked_and_result_is_rechecked(tmp_path: Path) -> None:
    """The stub finds the session lock free, then records this turn (an ask
    answered mid-classify) and says QUESTION: the Stop must not reply twice."""
    state = tmp_path / ".claude" / "tmp" / "night-proceed"
    bindir = tmp_path / "lockbin"
    bindir.mkdir()
    (bindir / "claude").write_text(f"""#!{PYTHON}
import fcntl, json, pathlib, sys
sys.stdin.read()
with open("{state}/s1.lock", "a") as f:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); got = "free"
    except OSError:
        got = "held"
    pathlib.Path("{state}/s1.json").write_text(json.dumps(["turn:p1"]))
pathlib.Path("{tmp_path}/lock-state").write_text(got)
print("QUESTION")
""")
    (bindir / "claude").chmod(0o755)
    assert run(tmp_path, stop(AB, prompt_id="p1"),
               extra={"PATH": f"{bindir}:{os.environ['PATH']}"}) is None
    assert (tmp_path / "lock-state").read_text() == "free"


def load_hook(tmp: Path, monkeypatch) -> object:
    """The hook as a module, HOME at tmp, so main() can run in-process."""
    monkeypatch.setenv("HOME", str(tmp))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_NIGHT_PROCEED_TEST_NOW", "02:00")
    monkeypatch.setenv("CLAUDE_NIGHT_PROCEED_TEST_LOCK", "locked")
    monkeypatch.delenv("CLAUDE_NIGHT_PROCEED", raising=False)
    spec = importlib.util.spec_from_file_location("night_proceed_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main_out(mod: object, payload: object, monkeypatch, capsys) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert mod.main() == 0
    return capsys.readouterr().out


def test_hanging_classifier_fails_open(tmp_path: Path, monkeypatch, capsys) -> None:
    bindir = tmp_path / "hangbin"
    bindir.mkdir()
    # Answers QUESTION, but only after 30 s: without the timeout this denies.
    (bindir / "claude").write_text(
        f'#!/bin/sh\ntouch "{tmp_path}/classifier-ran"\n'
        f'exec {PYTHON} -c "import time; time.sleep(30); print(\'QUESTION\')"\n')
    (bindir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    mod = load_hook(tmp_path, monkeypatch)
    mod.CLASSIFY_TIMEOUT = 2
    started = time.monotonic()
    assert main_out(mod, stop(AB), monkeypatch, capsys) == ""
    assert time.monotonic() - started < 5
    assert (tmp_path / "classifier-ran").exists()


def test_missing_or_broken_config_fails_open(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PATH", f"{fake_bin(tmp_path)}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path / "claude-calls"))
    mod = load_hook(tmp_path, monkeypatch)
    mod.CONFIG = tmp_path / "absent.json"
    assert main_out(mod, stop("Missing?"), monkeypatch, capsys) == ""
    mod.CONFIG = tmp_path / "broken.json"
    mod.CONFIG.write_text('{"night_proceed": {"start": 22}}')
    assert main_out(mod, stop("Broken?"), monkeypatch, capsys) == ""


def test_stop_timeout_outlasts_classifier_and_gdbus() -> None:
    """settings.json kills the hook at its timeout; the classifier (plus the 3 s
    gdbus query) must finish well inside it, or the nested claude is orphaned."""
    settings = json.loads((RUNTIME / "settings.json").read_text())
    text = (RUNTIME / "hooks" / "night-proceed.py").read_text()
    classify = int(re.search(r"^CLASSIFY_TIMEOUT = (\d+)", text, re.M).group(1))
    def timeouts(event: str) -> list[int]:
        return [h.get("timeout", 60) for entry in settings["hooks"][event]
                for h in entry.get("hooks", []) if "night-proceed.py" in h.get("command", "")]
    assert timeouts("Stop") and all(t >= classify + 3 + 5 for t in timeouts("Stop")), \
        (timeouts("Stop"), classify)
    # AskUserQuestion runs no classifier, only the 3 s gdbus query.
    assert timeouts("PreToolUse") and all(t >= 3 + 5 for t in timeouts("PreToolUse"))
