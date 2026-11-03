"""PreToolUse guard (`hooks/bash-guard.py`) — the foreground poll layers.

Invariant: a FOREGROUND Bash call that sleeps and then takes a one-shot CI
status snapshot is denied, with the blocking primitive named; every other
shape — a single status look, `--watch`, the same thing in the background,
a sleep next to unrelated work, the words merely quoted in text — passes.

Same invariant for LOCAL state (layer 5b): a foreground sleep inside a wait
loop, or a long sleep next to a read of a log/file/process, is denied with
"background it and end your turn"; short settles, bounded retries, rate-limit
pauses in iteration, quoted text and the background form all pass.

Run:  python3 tests/test_bash_guard_ci_poll.py
Exit: 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

# This module is a SCRIPT SUITE: every check below runs at import time and the
# verdict is this file's exit status. Importing it under pytest would run the
# whole body just to collect it, so the module stops here with a visible skip
# naming the real invocation. `tests/conftest.py` turns this marker back into
# one real test that runs the file as a script, so CI needs no list of these.
if __name__ != "__main__":
    import pytest

    pytest.skip("script suite: run `python3 tests/test_bash_guard_ci_poll.py` "
                "(exit 0 = all green); pytest cannot import it",
                allow_module_level=True)

HERE = os.path.dirname(os.path.abspath(__file__))
# `runtime/` once the layout migration has landed, the repository root before it;
# only the shipped subset moved, tests/ stays at the root.
REPO_ROOT = os.path.dirname(HERE)
RUNTIME = os.path.join(REPO_ROOT, "runtime") if os.path.isdir(os.path.join(REPO_ROOT, "runtime")) else REPO_ROOT
GUARD = os.path.join(RUNTIME, "hooks", "bash-guard.py")
SETTINGS = os.path.join(RUNTIME, "settings.json")

spec = importlib.util.spec_from_file_location("bash_guard", GUARD)
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)  # type: ignore[union-attr]

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)


# Real commands lifted from session transcripts (2026-09-12 .. 2026-10-02).
DENY = [
    "sleep 150; gh pr checks 287 -R jonathanmoregard/nixos-config 2>&1 | grep -v -E '\\spass\\s' | cut -c1-110",
    "sleep 20; gh pr checks 92 -R jonathanmoregard/.claude 2>&1 | cut -c1-90",
    "sleep 40; gh pr checks 18 -R jonathanmoregard/voquill 2>&1 | cut -c1-100; python3 x.py refresh abc",
    "sleep 45 && gh pr checks 12 --json name,state,bucket",
    "sleep 60; gh run view 123456 --json status,conclusion",
    "sleep 30; gh run list --branch fix/x --limit 3",
    "while true; do gh pr checks 5; sleep 30; done",
    "until gh pr checks 5 | grep -q pending; do sleep 10; done",
    "sleep 90\ngh pr view 7 --json statusCheckRollup",
    "for i in $(seq 1 19); do OUT=$(gh pr checks 97 2>&1); sleep 30; done",
    "for i in $(seq 1 55); do S=$(gh run view 3639 --json status -q .status); [ \"$S\" = completed ] && break; sleep 10; done",
    # a heredoc note written first does not hide the real poll after it
    "cat >> /tmp/progress.md <<EOF\n- pushed; PR #15 opened\nEOF\nsleep 240; gh pr checks 15 2>&1 | head -6",
    # backticks inside double quotes DO execute
    "sleep 30; echo \"state: `gh pr checks 5 | grep -c pending`\"",
    # 2026-10-03, this session: waiting out GitHub's lazy UNKNOWN mergeability
    "sleep 15; for n in 96 104; do gh pr view $n -R jonathanmoregard/.claude --json number,state,mergeable,mergeStateStatus --jq '.state'; done",
    "sleep 20; gh pr view 302 -R jonathanmoregard/nixos-config --json state,mergeStateStatus",
    # waiting on a deploy / unit by re-reading its status
    "sleep 30; systemctl status nixos-deploy.service --no-pager | head -5",
    "while systemctl --user is-active -q foo.service; do sleep 5; done",
    "sleep 60; systemctl --user show aggregator-ingest.service -p ActiveState -p Result -p ExecMainStatus",
    "sleep 330; systemctl --user is-active vmbase-proposals-push 2>&1; tail -8 /tmp/vm-base-merged.log",
]

ALLOW = [
    # a single look, no wait
    "gh pr checks 287 -R jonathanmoregard/nixos-config",
    # the blocking primitive, with or without a registration delay
    "gh pr checks 287 --watch --fail-fast",
    "sleep 20; gh pr checks 287 --watch --fail-fast",
    "sleep 45; gh run watch 99 --exit-status",
    # watch first, then print the settled result
    "sleep 20; gh pr checks 11 --watch --interval 30 >/dev/null 2>&1; gh pr checks 11",
    # the wait-for helper is the blocking primitive for PRs, runs and units
    "~/.claude/scripts/wait-for pr 302 -R jonathanmoregard/nixos-config",
    "sleep 20; $HOME/.claude/scripts/wait-for unit nixos-deploy.service --since now",
    # single looks without a wait
    "gh pr view 302 --json state,mergeable",
    "systemctl --user status foo.service",
    # sleep next to a unit ACTION, not a status read
    "sleep 2; systemctl --user restart foo.service",
    # act, then look: a health check after starting a unit (real, 2026-09)
    "systemctl --user restart whisper-writer && sleep 2 && systemctl --user status whisper-writer --no-pager",
    "systemctl start --no-ask-password --no-block nixos-deploy.service; sleep 5; systemctl is-active nixos-deploy.service",
    # a short settle delay before one look
    "sleep 3 && systemctl --user status autodoro --no-pager | head -5",
    # sleep next to unrelated work
    "sleep 5; git status",
    "gh pr checks 3; git log --oneline -3",
    # the words only appear inside quoted text
    "git commit -m 'stop doing sleep 30; gh pr checks 4 loops'",
    "echo \"sleep 10; gh run list\"",
    # heredoc bodies are data: markdown backticks there are not substitutions
    # (2026-10-02, this PR's own SKILL.md edit was denied by this layer)
    "python3 - <<'EOF'\nnew='before re-running `gh pr checks` / `gh run view` / `sleep N; <status>`'\nEOF",
    "git commit -F - <<'EOF'\nfix: stop polling\n\nInstead of `sleep 30; gh pr checks 9` use the watch.\nEOF",
    "cat >> /tmp/notes.md <<EOF\nsleep 30\ngh pr checks 9\nEOF",
    # backticks inside single quotes are literal text
    "echo 'avoid `sleep 9` then `gh pr checks 3`'",
]


# Layer 5b: the same hand-rolled wait aimed at LOCAL state (a log, a marker
# file, a process). Live 2026-11-02: a worker-hard subagent ran
# `until grep -q DONE log; do sleep 15; done` in the foreground.
LOCAL_DENY = [
    # the incident shape and its loop spellings
    "until grep -q DONE /tmp/job.log; do sleep 15; done",
    "while ! test -f /tmp/out/done; do sleep 5; done",
    "while ! [ -f /tmp/out/done ]; do sleep 5; done; cat /tmp/out/result",
    "while true; do grep -q DONE job.log && break; sleep 10; done",
    "while :; do if [ -s result.json ]; then break; fi; sleep 2; done",
    "until [ -s /tmp/result ]; do sleep 1; done",
    "while pgrep -f 'cargo test' >/dev/null; do sleep 10; done",
    "while kill -0 $PID 2>/dev/null; do sleep 5; done; tail -3 run.log",
    "for i in $(seq 1 60); do grep -q DONE job.log && break; sleep 5; done",
    "for i in {1..40}; do sleep 15; tail -1 job.log; done",
    "for i in 1 2 3 4 5 6 7 8 9 10; do test -e /tmp/ready && break; sleep 30; done",
    # sleep, then look at local state
    "sleep 60; tail -20 /tmp/job.log",
    "sleep 90 && cat /tmp/out.txt",
    "sleep 45 && grep -c PASS build.log",
    "sleep 120; ls -la target/",
    "sleep 30; wc -l results.csv",
    "sleep 5m; stat out.json",
    "sleep 40\nhead -5 progress.txt",
    "sleep 25; pgrep -fa playwright",
    "sleep 20 && test -f /tmp/done && echo finished",
    "sleep 30; [ -f /tmp/done ] && echo finished",
    "sleep 60 && tail -5 /tmp/a.log | grep -E 'ok|FAIL'",
    # a long bare timer: the wait before a Read of the output file
    "sleep 60",
    "sleep 300 && echo waited",
    # a note written first does not turn the timer after it into work
    "echo '- CI green on PR #126' >> ~/.local/state/claude-tasks/k/progress.md; sleep 300; echo waited",
    "cat >> notes.md <<EOF\n- pushed at $(date +%H:%M)\nEOF\nsleep 240; date '+%H:%M'",
    # wrapped in a sub-shell string or a timeout
    "timeout 600 bash -c 'until grep -q DONE job.log; do sleep 5; done'",
    "bash -c \"while ! test -f done.flag; do sleep 3; done\"",
    "S=$(sleep 30; cat status.txt); echo $S",
    # replayed from real transcripts (2026-09/10), each re-issued 10-400 times
    "for i in $(seq 1 19); do s=$(~/.claude/bin/ai-router status 20260915-192049-93e130 2>&1); case \"$s\" in *running*) sleep 30;; *) break;; esac; done; echo \"$s\"",
    "for i in 1 2 3 4 5 6 7 8; do\n  sleep 60\n  echo \"tick $i  $(date +%H:%M:%S)\"\ndone\n",
    "sleep 580; git log --oneline -1",
    "sleep 115; cd ~/worktrees/claude-x && git -C . status --short",
    "sleep 118; ~/.claude/bin/ai-router status 20260917-124854-ac8148",
    # launching the job in the same command does not make a long wait a settle
    "systemd-run --user --unit=gates bash gates.sh; for i in $(seq 1 55); do sleep 10; grep -q GATES_FINISHED gates.log && break; done",
    "./job.sh > job.log 2>&1 & until grep -q DONE job.log; do sleep 5; done",
    "./start.sh & sleep 30; tail -3 server.log",
]

LOCAL_ALLOW = [
    # a single look, no wait
    "tail -20 /tmp/job.log",
    "grep -q DONE /tmp/job.log && echo done",
    "test -f /tmp/out/done && cat /tmp/out/result",
    # a short settle before one look (same threshold as unit status reads)
    "sleep 3; tail -5 /tmp/server.log",
    "sleep 2 && cat /tmp/out",
    "./start.sh & sleep 5; curl -s localhost:8080/health; tail -3 server.log",
    # a sleep next to work that is not a status read
    "sleep 20; npm run build",
    "sleep 1; git status",
    # a write is not a look: wait, then act on what was written
    "cat > cfg.json <<'EOF'\n{}\nEOF\nsleep 15; ./server --config cfg.json",
    # loops without a sleep
    "for f in a.txt b.txt; do cat $f; done",
    "while read -r l; do echo \"$l\"; done < list.txt",
    # iteration with a rate-limit pause is not a wait
    "for pr in 11 12 13; do gh api repos/o/r/pulls/$pr/merge -X PUT; sleep 2; done",
    "while read -r f; do curl -s -T \"$f\" https://up.example; sleep 1; done < files.txt",
    # a short, bounded retry
    "for i in 1 2 3; do curl -sf http://localhost:3000 && break; sleep 2; done",
    "for i in $(seq 1 5); do test -S /tmp/pg/.s.PGSQL.5432 && break; sleep 1; done",
    # A bounded measurement script samples state; it does not wait for a
    # completion condition and must be allowed even when the sample interval
    # exceeds the short-retry budget.
    "for sample in $(seq 1 6); do sleep 10; systemctl --user show probe.service -p ActiveState -p Result; done",
    # a readiness settle right after starting the target, bounded by the
    # command itself to the foreground timeout (real, 2026-10)
    "systemctl --user restart llm-guard-daemon.service && sleep 2 && for i in $(seq 1 60); do\n  if curl -fsS --max-time 2 http://127.0.0.1:8787/health >/dev/null 2>&1; then echo ok; break; fi\n  sleep 1\ndone; tail -3 ~/.claude/logs/llm-guard-daemon.log",
    "(python3 -m http.server 8765 >/dev/null 2>&1 &) ; for i in $(seq 1 20); do curl -sf localhost:8765 && break; sleep 0.5; done",
    # The readiness loop taught by detached-long-commands probes an SSH
    # banner after launch. It is not a completion poll.
    "systemd-run --user --unit=probe-vm vm-launch; for i in $(seq 1 60); do timeout 5 ssh-keyscan -p 2222 -T 5 127.0.0.1 | head -1 && break; sleep 1; done",
    # a sleep keeping a pipe's stdin open is not a wait (real, MCP smoke tests)
    "(printf '{\"jsonrpc\":\"2.0\",\"id\":1}\\n'; sleep 2) | timeout 5 uv run python -m srv.main 2>&1 | tail -5",
    # the shape quoted as data
    "git commit -m 'replace until grep -q DONE log; do sleep 5; done with a background wait'",
    "echo \"sleep 60; tail log\"",
    "cat > wait.sh <<'EOF'\nuntil grep -q DONE job.log; do sleep 5; done\nEOF",
    "python3 - <<'EOF'\nprint('sleep 60; tail -f log')\nEOF",
    "echo 'run `sleep 60; cat out` in the background'",
]


def main() -> int:
    print("== foreground sleep + local status read / sleep loop is denied ==")
    for cmd in LOCAL_DENY:
        reason = bg._check_local_poll(cmd, background=False)
        check(f"deny: {cmd[:70]!r}", reason is not None)
        if reason is not None:
            check("  reason names durable job primitive", "wait-for job <slug> -- <cmd>" in reason)
            check("  reason ranks background wait first", "run_in_background" in reason)

    print("== local: legitimate shapes pass ==")
    for cmd in LOCAL_ALLOW:
        check(f"allow: {cmd[:70]!r}", bg._check_local_poll(cmd, background=False) is None)
    measurement = next(cmd for cmd in LOCAL_ALLOW if "probe.service" in cmd)
    check("systemd measurement also passes the earlier CI/unit layer",
          bg._check_ci_poll(measurement, background=False) is None)

    print("== local: the sanctioned wait is the same loop in the background ==")
    for cmd in LOCAL_DENY:
        check(f"bg allow: {cmd[:60]!r}", bg._check_local_poll(cmd, background=True) is None)

    print("== local: who the layer applies to ==")
    applies = bg._local_poll_applies
    check("main session, interactive", applies({}, "cli"))
    check("main session, headless (re-invoked by its own bg command)", applies({}, "sdk-cli"))
    check("subagent, interactive (re-invoked by its own bg command)",
          applies({"agent_id": "a1", "agent_type": "worker-hard"}, "cli"))
    check("subagent, entrypoint unset counts as interactive", applies({"agent_id": "a1"}, None))
    check("subagent, headless: durable systemd job replaces the exemption",
          applies({"agent_id": "a1", "agent_type": "worker-hard"}, "sdk-cli"))

    print("== local: Monitor cannot bypass the same wait gate ==")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Monitor",
        "tool_input": {"command": LOCAL_DENY[0]},
    }
    proc = subprocess.run(
        [sys.executable, "-S", "-E", GUARD], input=json.dumps(payload),
        capture_output=True, text=True, timeout=10,
    )
    check("Monitor loop is denied", '"permissionDecision": "deny"' in proc.stdout)
    configured = json.load(open(SETTINGS, encoding="utf-8"))
    guard_entries = [
        entry for entry in configured["hooks"]["PreToolUse"]
        if any("bash-guard.py" in hook.get("command", "") for hook in entry.get("hooks", []))
    ]
    check("settings offers Bash and Monitor to bash-guard",
          len(guard_entries) == 1 and guard_entries[0].get("matcher") == "^(Bash|Monitor)$")

    print("== foreground sleep + one-shot CI snapshot is denied ==")
    for cmd in DENY:
        reason = bg._check_ci_poll(cmd, background=False)
        check(f"deny: {cmd[:70]!r}", reason is not None)
        if reason is not None:
            check("  reason names the blocking primitive", "--watch" in reason)
            check("  reason names wait-for", "wait-for" in reason)

    print("== legitimate shapes pass ==")
    for cmd in ALLOW:
        check(f"allow: {cmd[:70]!r}", bg._check_ci_poll(cmd, background=False) is None)

    print("== the same poll in the background is not this layer's business ==")
    for cmd in DENY[:3]:
        check(f"bg allow: {cmd[:60]!r}", bg._check_ci_poll(cmd, background=True) is None)

    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
