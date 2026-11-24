#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys


CONTEXT_EVENTS = frozenset({"SessionStart", "SubagentStart", "UserPromptSubmit"})
PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\r?$", re.MULTILINE)
PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\r?$", re.MULTILINE)


def context_payload(event: str, text: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        },
    }


def codex_payload_from(event: str, stdout: str) -> str:
    if not stdout.strip():
        return ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        if event in CONTEXT_EVENTS:
            return json.dumps(context_payload(event, stdout.strip()), ensure_ascii=False) + "\n"
        return stdout

    if not isinstance(payload, dict):
        if event in CONTEXT_EVENTS:
            return json.dumps(context_payload(event, stdout.strip()), ensure_ascii=False) + "\n"
        return stdout

    if event in CONTEXT_EVENTS:
        payload.pop("suppressOutput", None)
        additional = None
        for key in ("additionalContext", "additional_context"):
            value = payload.pop(key, None)
            if isinstance(value, str) and value.strip() and additional is None:
                additional = value
        hook_specific = payload.get("hookSpecificOutput")
        if additional is not None and not isinstance(hook_specific, dict):
            payload["hookSpecificOutput"] = context_payload(event, additional)[
                "hookSpecificOutput"
            ]

    return json.dumps(payload, ensure_ascii=False) + "\n"


def claude_input_payloads(raw: str, *, adapt_file_tools: bool) -> list[str]:
    if not adapt_file_tools:
        return [raw]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    if not isinstance(payload, dict) or payload.get("tool_name") != "apply_patch":
        return [raw]
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return [raw]
    patch = tool_input.get("command")
    if not isinstance(patch, str):
        return [raw]

    targets: list[tuple[str, str]] = []
    for match in PATCH_FILE.finditer(patch):
        targets.append(("Write" if match.group(1) == "Add" else "Edit", match.group(2)))
    targets.extend(("Edit", match.group(1)) for match in PATCH_MOVE.finditer(patch))
    if not targets:
        return [raw]

    result: list[str] = []
    seen: set[str] = set()
    for tool_name, path in targets:
        if path in seen:
            continue
        seen.add(path)
        adapted = dict(payload)
        adapted["tool_name"] = tool_name
        adapted_input = dict(tool_input)
        adapted_input["file_path"] = path
        adapted["tool_input"] = adapted_input
        result.append(json.dumps(adapted, ensure_ascii=False))
    return result


def merged_stdout(event: str, outputs: list[str]) -> str:
    rendered = [codex_payload_from(event, output) for output in outputs if output]
    rendered = [output for output in rendered if output]
    if len(rendered) <= 1:
        return rendered[0] if rendered else ""

    parsed: list[dict[str, object]] = []
    for output in rendered:
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return "".join(rendered)
        if not isinstance(value, dict):
            return "".join(rendered)
        parsed.append(value)

    for value in parsed:
        specific = value.get("hookSpecificOutput")
        if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
            return json.dumps(value, ensure_ascii=False) + "\n"
        if value.get("decision") == "block":
            return json.dumps(value, ensure_ascii=False) + "\n"

    contexts: list[str] = []
    for value in parsed:
        specific = value.get("hookSpecificOutput")
        if isinstance(specific, dict):
            context = specific.get("additionalContext")
            if isinstance(context, str) and context:
                contexts.append(context)
    if contexts:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": "\n".join(contexts),
                }
            },
            ensure_ascii=False,
        ) + "\n"
    return rendered[0]


def main() -> int:
    if len(sys.argv) not in {3, 4} or (
        len(sys.argv) == 4 and sys.argv[3] != "--adapt-file-tools"
    ):
        print(
            "usage: codex_hook_output_adapter.py <event> <hook-wrapper> "
            "[--adapt-file-tools]",
            file=sys.stderr,
        )
        return 64

    event, wrapper = sys.argv[1:3]
    inputs = claude_input_payloads(
        sys.stdin.read(), adapt_file_tools=len(sys.argv) == 4
    )
    children = [
        subprocess.run(
            [wrapper],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )
        for payload in inputs
    ]
    stdout = merged_stdout(event, [child.stdout for child in children])
    if stdout:
        sys.stdout.write(stdout)
    failures = [child for child in children if child.returncode != 0]
    if event in CONTEXT_EVENTS and failures and all(
        child.returncode == 1 for child in failures
    ):
        # Claude treats exit 1 as a non-blocking hook failure. Codex surfaces
        # any non-zero exit as a user-visible banner, so preserve Claude's
        # fail-open behavior without leaking an internal diagnostic to the UI.
        return 0
    for child in failures:
        if child.stderr:
            sys.stderr.write(child.stderr)
    return failures[0].returncode if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
