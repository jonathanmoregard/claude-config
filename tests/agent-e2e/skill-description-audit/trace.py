#!/usr/bin/env python3
"""Why a run ended as it did, without publishing anything it read.

The run directories are not uploadable: they hold the planted credentials, and
the staged HOME links the real credential store, so a transcript can carry
either. This builds the diagnostic from an ALLOWLIST instead -- tool names,
tool paths, error booleans, turn and stop accounting -- so adding a field is a
deliberate act rather than something a redaction pattern has to catch. No
message text, no tool output, no tool input but the path.

Usage: trace.py <out-root> <scenario-dir>   (writes <scenario-dir>/*/trace.json)
"""

import json
import os
import re
import sys

# Keys that name a file or command target. Everything else in a tool_use input
# is dropped: `content`, `old_string`, a Bash `command` and a WebFetch `prompt`
# all quote what the agent read.
PATH_KEYS = ("file_path", "path", "notebook_path", "pattern")

# Measured: a real run drives everything through Bash, so without this the
# whole trace is `Bash, error, Bash` and diagnoses nothing. Only the program
# name is taken, and only when it looks like one -- anything else is dropped,
# so a command cannot smuggle its arguments through this field.
PROGRAM = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

# Refusals come from the result event's `permission_denials`, never from
# matching the error text. Tried and rejected: a regex over tool output called
# `grep` exiting 1 a denial, because the skill name `claude-code-permissions`
# was in the output it printed. Same failure as any pattern read against data
# instead of meaning.
RESULT_KEYS = ("subtype", "stop_reason", "terminal_reason", "num_turns",
               "duration_ms", "api_error_status", "is_error")


def blocks(event):
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def program_of(command):
    token = command.strip().split(maxsplit=1)
    if not token:
        return None
    name = os.path.basename(token[0])
    return name if PROGRAM.match(name) else None


def denials_of(event):
    """Refusals, as the harness reports them rather than as text."""
    raw = event.get("permission_denials")
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            out.append({"tool": entry.get("tool_name") or entry.get("tool"),
                        "target": entry.get("path") or entry.get("file_path")})
        else:
            out.append({"tool": None, "target": None})
    return out


def result_of(event):
    summary = {key: event.get(key) for key in RESULT_KEYS}
    summary["is_error"] = bool(event.get("is_error"))
    summary["permission_denials"] = denials_of(event)
    summary["safety_stops"] = len(event.get("safety_stops") or [])
    return summary


def tool_step(block):
    data = block.get("input")
    target = None
    if isinstance(data, dict):
        for key in PATH_KEYS:
            if isinstance(data.get(key), str):
                target = data[key]
                break
        if target is None and isinstance(data.get("command"), str):
            target = program_of(data["command"])
    return {"tool": block.get("name"), "target": target}


def trace_of(path):
    steps, result, turns = [], {}, 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "assistant":
                turns += 1
            if event.get("type") == "result":
                result = result_of(event)
            for block in blocks(event):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    steps.append(tool_step(block))
                elif block.get("type") == "tool_result" and block.get("is_error"):
                    steps.append({"error": True})
    return {"assistant_messages": turns, "result": result, "steps": steps}


def main(argv):
    out_root, scenario = argv[1], argv[2]
    written = 0
    # Run directories are nested <arm>/<run>, so this walks for transcripts
    # rather than listing one level.
    for current, _, files in os.walk(scenario):
        if "transcript.jsonl" not in files:
            continue
        trace = trace_of(os.path.join(current, "transcript.jsonl"))
        work = os.path.join(current, "work", "findings.md")
        trace["findings_md"] = {
            "exists": os.path.isfile(work),
            "bytes": os.path.getsize(work) if os.path.isfile(work) else 0,
        }
        target = os.path.join(out_root, os.path.relpath(current, scenario))
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "trace.json"), "w", encoding="utf-8") as fh:
            json.dump(trace, fh, indent=2, sort_keys=True)
            fh.write("\n")
        written += 1
    if not written:
        print(f"trace: no transcript under {scenario}", file=sys.stderr)
        return 1
    print(f"trace: wrote {written} trace(s) to {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
