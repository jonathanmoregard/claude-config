#!/usr/bin/env bash
# An agent-config repo: a reviewer subagent ends with a JSON verdict as free
# text, a SubagentStop hook json.loads() it out of last_assistant_message, and
# a merge gate refuses PRs with no recorded PASS. The harness changed how
# background subagents return (a SubagentHandback tool call), the message
# field went empty and the gate refused a reviewed PR. Everything here is the
# repo's own: the reviewer's prompt, the hook and the gate.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/runtime/agents" "$WORK/runtime/hooks" "$WORK/tests"

cat > "$WORK/runtime/agents/reviewer.md" <<'MD'
---
name: reviewer
description: Read-only reviewer for a finished branch. Returns a verdict.
tools: ["Read", "Grep", "Glob"]
---

You review the branch you are given. Look for defects a user would hit.

# Output

Your final message is exactly one JSON object and nothing else:

{"verdict": "PASS" | "PASS_WITH_FIXES" | "BLOCKS",
 "findings": [{"severity": "HIGH" | "MEDIUM" | "LOW", "file": "...", "line": 1, "claim": "..."}]}

No prose before or after it. hooks/record_verdict.py reads it.
MD

cat > "$WORK/runtime/hooks/record_verdict.py" <<'PY'
#!/usr/bin/env python3
"""SubagentStop hook: record the reviewer's verdict for the merge gate."""
import json
import subprocess
import sys
from pathlib import Path

VERDICTS = ("PASS", "PASS_WITH_FIXES", "BLOCKS")


def main() -> None:
    payload = json.load(sys.stdin)
    if payload.get("agent_type") != "reviewer":
        return
    text = (payload.get("last_assistant_message") or "").strip()
    try:
        verdict = json.loads(text)
    except ValueError:
        return  # not a verdict
    if verdict.get("verdict") not in VERDICTS:
        return
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, cwd=payload["cwd"]).stdout.strip()
    log = Path(payload["cwd"]) / ".git" / "review-verdicts.jsonl"
    with log.open("a") as fh:
        fh.write(json.dumps({"head": head, "verdict": verdict["verdict"]}) + "\n")


if __name__ == "__main__":
    main()
PY

cat > "$WORK/runtime/hooks/merge_gate.py" <<'PY'
#!/usr/bin/env python3
"""PreToolUse(Bash) hook: refuse `gh pr create` until HEAD has a passing review."""
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    payload = json.load(sys.stdin)
    if "gh pr create" not in payload.get("tool_input", {}).get("command", ""):
        return 0
    cwd = payload["cwd"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, cwd=cwd).stdout.strip()
    log = Path(cwd) / ".git" / "review-verdicts.jsonl"
    rows = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    if any(r["head"] == head and r["verdict"] != "BLOCKS" for r in rows):
        return 0
    print("merge gate: no recorded reviewer verdict for HEAD", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
PY

cat > "$WORK/runtime/settings.json" <<'JSON'
{
  "hooks": {
    "SubagentStop": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record_verdict.py"}]}],
    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/merge_gate.py"}]}]
  }
}
JSON

cat > "$WORK/tests/test_record_verdict.py" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent.parent / "runtime" / "hooks" / "record_verdict.py"


def test_records_pass(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "x"], cwd=tmp_path, check=True)
    payload = {"agent_type": "reviewer", "cwd": str(tmp_path),
               "last_assistant_message": json.dumps({"verdict": "PASS", "findings": []})}
    subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), text=True, check=True)
    rows = (tmp_path / ".git" / "review-verdicts.jsonl").read_text().splitlines()
    assert json.loads(rows[0])["verdict"] == "PASS"
PY

git -C "$WORK" add -A
git -C "$WORK" commit -qm "reviewer agent, verdict hook and merge gate"
