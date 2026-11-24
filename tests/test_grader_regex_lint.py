"""grader-regex-lint hook: warns on regex/grep written into agent-e2e graders."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "runtime" / "hooks" / "grader-regex-lint.py"
E2E = "/w/repo/tests/agent-e2e/demo/check.sh"


def run(tool_input: dict) -> str:
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": tool_input}
    r = subprocess.run([sys.executable, "-S", "-E", str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout


def context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_warns_on_grep_in_written_grader():
    out = run({"file_path": E2E, "content": 'grep -q done "$RUN/final.txt" && pass ok\n'})
    assert "structured_judge" in context(out) and "regex-ok:" in context(out)
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_warns_on_python_regex_in_edit():
    out = run({"file_path": "/w/repo/tests/agent-e2e/_lib/g.py",
               "old_string": "x", "new_string": "if re.search(r'done', final):\n"})
    assert "1 regex/grep line" in context(out)


def test_python_heredoc_inside_shell_grader_is_scanned():
    text = "python3 - <<'PY'\nimport re\nif re.search(r'pushed', final): ok()\nPY\n"
    assert "re.search" in context(run({"file_path": E2E, "content": text}))


def test_multiedit_scans_every_new_string():
    out = run({"file_path": E2E, "edits": [{"new_string": "echo hi"},
                                           {"new_string": "egrep -c x f"}]})
    assert "egrep" in context(out)


def test_silenced_comment_and_clean_lines_stay_quiet():
    text = ('x=$(grep -c push "$RUN/commands.txt")  # regex-ok: actions\n'
            "# grep in a comment does not count\n"
            'python3 "$LIB/structured_judge.py" --schema s.json\n')
    assert run({"file_path": E2E, "content": text}) == ""


def test_out_of_scope_paths_stay_quiet():
    assert run({"file_path": "/w/repo/tests/other/check.sh", "content": "grep x y\n"}) == ""
    assert run({"file_path": "/w/repo/tests/agent-e2e/demo/prompt.md", "content": "grep x\n"}) == ""


def test_malformed_input_exits_zero_silently():
    r = subprocess.run([sys.executable, "-S", "-E", str(HOOK)], input="not json",
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and r.stdout == ""
