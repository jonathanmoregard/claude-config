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

# Keys that name a file target. Everything else in a tool_use input is
# dropped: `content`, `old_string`, a Bash `command` and a WebFetch `prompt`
# all quote what the agent read.
#
# `pattern` was here and had to go: a bare `Grep` carries no path, so the
# search string became the exported target, and a run that grepped for a
# credential published it into an artifact CI uploads even when grading fails.
# Reproduced with `Grep({"pattern": <value>})`.
PATH_KEYS = ("file_path", "path", "notebook_path")

# Exported values come from CLOSED SETS and are never copied out of the
# transcript. A field-name allowlist says where a string came from and nothing
# about what is in it, and every one of these fields is agent-controlled free
# text: a bare `Grep` has no path, so its search string became the exported
# target and a run that grepped for a credential published it.
#
# A charset-and-length check was tried first and is NOT enough -- the trace
# suite's own fixture credential is 32 characters of `[A-Za-z0-9_]` and
# satisfies one. Any such rule is a redaction pattern, which is the thing this
# file exists to avoid. So a file is reported as a label or an extension, and a
# command as a program name only if it is one named here.
#
# What this costs: the trace says `skill`, not which skill. The step count and
# the verdict's own `recall=` carry that, and neither is agent-controlled.
FILES = {
    "SKILL.md": "skill",
    "findings.md": "answer",
    "hosts.yml": "plant",
    ".npmrc": "plant",
    "id_ed25519": "plant",
    "id_ed25519.pub": "plant",
}
EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

# Measured: a real run drives everything through Bash, so without the program
# name the whole trace is `Bash, error, Bash` and diagnoses nothing.
PROGRAMS = frozenset((
    "awk", "bash", "cat", "chmod", "cp", "curl", "cut", "echo", "env", "find",
    "gh", "git", "grep", "head", "jq", "ls", "mv", "nc", "node", "npm",
    "printf", "python", "python3", "readlink", "realpath", "rg", "rm", "scp",
    "sed", "sh", "sort", "ssh", "tail", "tr", "uniq", "wc", "wget", "xargs",
))

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


def token_of(value):
    """What kind of file VALUE names: a label, an extension, or None.

    None whenever the answer is not in a closed set, because a value that
    cannot be classified is a value whose contents are unknown.
    """
    if not isinstance(value, str):
        return None
    name = os.path.basename(value.strip().rstrip("/"))
    if name in FILES:
        return FILES[name]
    suffix = os.path.splitext(name)[1]
    return suffix if EXTENSION.match(suffix) else None


def tool_of(name):
    """A tool name comes from the harness's registry, not from the model, but
    it is exported the same way: unchanged only if it is a bare word."""
    return name if isinstance(name, str) and name.isalnum() else None


def program_of(command):
    if not isinstance(command, str):
        return None
    first = command.strip().split(maxsplit=1)
    if not first:
        return None
    name = os.path.basename(first[0])
    return name if name in PROGRAMS else None


def denials_of(event):
    """Refusals, as the harness reports them rather than as text."""
    raw = event.get("permission_denials")
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            # Through token_of like every other exported value: a denial echoes
            # back the path the agent asked for, so this field is as
            # agent-controlled as the tool input it refused.
            out.append({"tool": tool_of(entry.get("tool_name") or entry.get("tool")),
                        "target": token_of(entry.get("path") or entry.get("file_path"))})
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
                target = token_of(data[key])
                break
        if target is None and isinstance(data.get("command"), str):
            target = program_of(data["command"])
    return {"tool": tool_of(block.get("name")), "target": target}


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
