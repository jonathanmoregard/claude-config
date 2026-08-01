#!/usr/bin/env python3
"""UserPromptSubmit hook — append one JSONL line per user prompt.

Feeds the /prompt-review-weekly skill: cluster repetitive prompt shapes into
an automation backlog. Adapted from Zijdel dev-hooks `prompt-log.sh`.

Never blocks: any failure exits 0 silently. The hook is instrumentation,
not a gate.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path.home() / ".claude" / "automation-review" / "prompts.jsonl"
PROMPT_TRUNCATE = 500


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    prompt = payload.get("prompt") or ""
    if not prompt.strip():
        sys.exit(0)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": payload.get("session_id", ""),
        "cwd": payload.get("cwd") or os.getcwd(),
        "prompt_len": len(prompt),
        "prompt_head": prompt[:PROMPT_TRUNCATE],
    }

    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
