#!/usr/bin/env python3
"""Print real user messages from a transcript for eval.

Usage: print.py <jsonl_path> [max_chars_per_msg]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude"))
from importlib import import_module
cls = import_module("step-back-classifier")

path = sys.argv[1]
maxc = int(sys.argv[2]) if len(sys.argv) > 2 else 400

i = 0
with open(path, errors="replace") as f:
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
        if not cls.is_real_user_entry(obj):
            continue
        content = obj.get("message", {}).get("content")
        text = cls.extract_text(content)
        text = cls.clean_text(text)
        if not text:
            continue
        i += 1
        ts = obj.get("timestamp", "")[:16].replace("T", " ")
        print(f"--- U{i} [{ts}] ---")
        print(text[:maxc])
