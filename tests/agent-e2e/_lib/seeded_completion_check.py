#!/usr/bin/env python3
"""Project UserPromptSubmit hook for agent-e2e: run the ARM's own completion-check
against a seeded transcript, so a one-turn `claude -p` sees what a long session
would see after an earlier `git push`.

    seeded_completion_check.py SEED_TRANSCRIPT OUT_FILE

A headless run has no prior tool calls, so the real hook (registered by the
staged settings.json) stays silent. This wrapper hands the staged
$HOME/.claude/hooks/completion-check.py the same payload with transcript_path
pointing at SEED_TRANSCRIPT, prints its output for Claude Code, and copies it to
OUT_FILE so check.sh can tell whether the hook actually spoke.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

seed, out_file = sys.argv[1], sys.argv[2]
payload = json.loads(sys.stdin.read() or "{}")
payload["transcript_path"] = seed
hook = pathlib.Path(os.environ["HOME"], ".claude/hooks/completion-check.py")
p = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload),
                   capture_output=True, text=True, timeout=55)
with open(out_file, "a", encoding="utf-8") as f:  # append: one block per invocation
    f.write(f"=== {payload.get('hook_event_name')} {time.strftime('%H:%M:%S')} rc={p.returncode}\n")
    f.write(p.stdout + ("\n[stderr]\n" + p.stderr if p.stderr else "") + "\n")
sys.stdout.write(p.stdout)
sys.exit(0)
