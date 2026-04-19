#!/usr/bin/env python3
"""
One-off: run the classifier across recent transcripts and report
TRIGGER/SKIP rates with reasons. Does not touch live state files.

Usage: python3 step-back-backtest.py [days] [max_sessions]
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude"))
from importlib import import_module

cls_mod = import_module("step-back-classifier")

PROJECTS = Path.home() / ".claude" / "projects"
MIN_REAL_USER = 4
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
MAX_SESSIONS = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def count_real_users(transcript_path):
    n = 0
    try:
        with open(transcript_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                if cls_mod.is_real_user_entry(obj):
                    n += 1
    except OSError:
        return 0
    return n


def main():
    cutoff = (datetime.now() - timedelta(days=DAYS)).timestamp()
    candidates = []

    for jsonl in PROJECTS.rglob("*.jsonl"):
        if "subagents" in jsonl.parts:
            continue
        if "homunculus" in str(jsonl):
            continue
        try:
            mtime = jsonl.stat().st_mtime
            if mtime < cutoff:
                continue
        except OSError:
            continue
        n = count_real_users(jsonl)
        if n < MIN_REAL_USER:
            continue
        candidates.append((mtime, n, jsonl))

    candidates.sort(reverse=True)
    candidates = candidates[:MAX_SESSIONS]

    print(f"Running classifier on {len(candidates)} sessions (last {DAYS}d, >={MIN_REAL_USER} real user msgs)...\n")

    triggers = []
    skips = []
    errors = []

    for i, (mtime, n_users, path) in enumerate(candidates, 1):
        sid = path.stem[:8]
        date = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
        proj = path.parent.name.replace("-home-jonathan-", "")[:30]
        print(f"[{i}/{len(candidates)}] {date} {sid} ({n_users}u) {proj}...", flush=True, end=" ")

        conv = cls_mod.build_conversation(str(path))
        if not conv:
            print("EMPTY")
            errors.append((sid, date, proj, "empty"))
            continue

        prompt = cls_mod.CLASSIFIER_PROMPT.format(conversation=conv)
        resp = cls_mod.call_haiku(prompt)
        if not resp:
            print("HAIKU_FAIL")
            errors.append((sid, date, proj, "haiku failed"))
            continue

        verdict_line = ""
        for line in resp.splitlines():
            s = line.strip()
            u = s.upper()
            if u.startswith("TRIGGER") or u.startswith("SKIP"):
                verdict_line = s
                break

        if verdict_line.upper().startswith("TRIGGER"):
            reason = verdict_line.split(":", 1)[1].strip() if ":" in verdict_line else verdict_line
            print(f"TRIGGER: {reason[:80]}")
            triggers.append((sid, date, proj, n_users, reason))
        elif verdict_line.upper().startswith("SKIP"):
            print("SKIP")
            skips.append((sid, date, proj, n_users))
        else:
            print(f"UNPARSED: {resp[:80]}")
            errors.append((sid, date, proj, f"unparsed: {resp[:60]}"))

    print("\n" + "=" * 70)
    print(f"TRIGGERED ({len(triggers)}/{len(candidates)}):")
    for sid, date, proj, n, reason in triggers:
        print(f"  {date} {sid} ({n}u) {proj}")
        print(f"    -> {reason}")
    print(f"\nSKIPPED: {len(skips)}")
    print(f"ERRORS:  {len(errors)}")
    for sid, date, proj, why in errors:
        print(f"  {date} {sid} {proj}: {why}")
    rate = len(triggers) / len(candidates) * 100 if candidates else 0
    print(f"\nTrigger rate: {rate:.1f}%")


if __name__ == "__main__":
    main()
