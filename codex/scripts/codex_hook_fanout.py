#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from codex_hook_output_adapter import claude_input_payloads, codex_payload_from


CONTEXT_EVENTS = frozenset({"SessionStart", "SubagentStart", "UserPromptSubmit"})
TOOL_ALIASES = {
    "apply_patch": ("Write", "Edit"),
    "spawn_agent": ("Agent",),
}


@dataclass(frozen=True)
class Handler:
    matcher: str | None
    wrapper: str
    timeout: float
    adapt_file_tools: bool


@dataclass(frozen=True)
class Invocation:
    index: int
    wrapper: str
    timeout: float
    payload: str


@dataclass(frozen=True)
class Outcome:
    index: int
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False


def load_handlers(path: Path, locate: Callable[[Path], Path] = Path) -> list[Handler]:
    """The plan's handlers, or ValueError naming what the fan-out rejects.

    locate maps a path the plan names to where it is on disk: the check
    passes common.located, which knows where the output is written.
    """
    try:
        plan = json.loads(locate(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read hook plan {path}: {exc}") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("handlers"), list):
        raise ValueError("hook plan must contain a handlers array")

    result: list[Handler] = []
    for index, value in enumerate(plan["handlers"]):
        prefix = f"handlers[{index}]"
        if not isinstance(value, dict):
            raise ValueError(f"{prefix} must be an object")
        matcher = value.get("matcher")
        if matcher is not None and not isinstance(matcher, str):
            raise ValueError(f"{prefix}.matcher must be a string or null")
        if matcher not in {None, "", "*"}:
            try:
                re.compile(matcher)
            except re.error as exc:
                raise ValueError(f"{prefix}.matcher is not a valid regex: {exc}") from exc
        wrapper = value.get("wrapper")
        if not isinstance(wrapper, str) or not Path(wrapper).is_absolute():
            raise ValueError(f"{prefix}.wrapper must be an absolute path")
        on_disk = locate(Path(wrapper))
        if not on_disk.is_file() or not os.access(on_disk, os.X_OK):
            raise ValueError(f"{prefix}.wrapper must be an executable file")
        timeout = value.get("timeout")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError(f"{prefix}.timeout must be a positive number")
        adapt_file_tools = value.get("adapt_file_tools")
        if not isinstance(adapt_file_tools, bool):
            raise ValueError(f"{prefix}.adapt_file_tools must be a boolean")
        result.append(
            Handler(matcher, wrapper, float(timeout), adapt_file_tools)
        )
    return result


def matcher_values(event: str, payload: object) -> tuple[str, ...]:
    if event in {"UserPromptSubmit", "Stop"}:
        return ()
    if not isinstance(payload, dict):
        return ()
    if event == "SessionStart":
        value = payload.get("source")
        return (value,) if isinstance(value, str) else ()
    if event == "PreCompact":
        value = payload.get("trigger")
        return (value,) if isinstance(value, str) else ()

    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str):
        return (tool_name, *TOOL_ALIASES.get(tool_name, ()))
    if event in {"SubagentStart", "SubagentStop"}:
        agent_type = payload.get("agent_type")
        return (agent_type,) if isinstance(agent_type, str) else ()
    return ()


def handler_matches(event: str, payload: object, matcher: str | None) -> bool:
    if event in {"UserPromptSubmit", "Stop"}:
        return True
    if matcher in {None, "", "*"}:
        return True
    try:
        pattern = re.compile(matcher)
    except re.error:
        return False
    return any(pattern.search(value) is not None for value in matcher_values(event, payload))


def build_invocations(
    event: str, handlers: list[Handler], raw: str
) -> list[Invocation]:
    invocations: list[Invocation] = []
    for handler in handlers:
        for adapted in claude_input_payloads(
            raw, adapt_file_tools=handler.adapt_file_tools
        ):
            try:
                payload: object = json.loads(adapted)
            except json.JSONDecodeError:
                payload = None
            if not handler_matches(event, payload, handler.matcher):
                continue
            invocations.append(
                Invocation(
                    len(invocations), handler.wrapper, handler.timeout, adapted
                )
            )
    return invocations


def decoded_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_invocation(invocation: Invocation) -> Outcome:
    try:
        child = subprocess.run(
            [invocation.wrapper],
            input=invocation.payload,
            text=True,
            capture_output=True,
            check=False,
            timeout=invocation.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = decoded_stream(exc.stderr)
        if stderr and not stderr.endswith("\n"):
            stderr += "\n"
        stderr += (
            f"hook wrapper timed out after {invocation.timeout:g} seconds: "
            f"{invocation.wrapper}\n"
        )
        return Outcome(
            invocation.index,
            decoded_stream(exc.stdout),
            stderr,
            None,
            timed_out=True,
        )
    except OSError as exc:
        return Outcome(
            invocation.index,
            "",
            f"cannot execute hook wrapper {invocation.wrapper}: {exc}\n",
            126,
        )
    return Outcome(
        invocation.index,
        child.stdout,
        child.stderr,
        child.returncode,
    )


def run_all(invocations: list[Invocation]) -> list[Outcome]:
    if not invocations:
        return []
    completed: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=len(invocations)) as executor:
        futures = [executor.submit(run_invocation, invocation) for invocation in invocations]
        for future in as_completed(futures):
            completed.append(future.result())
    return completed


def is_deny(value: dict[str, Any]) -> bool:
    specific = value.get("hookSpecificOutput")
    return isinstance(specific, dict) and specific.get("permissionDecision") == "deny"


def is_block(value: dict[str, Any]) -> bool:
    return value.get("decision") == "block"


def merge_mapping(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merge_mapping(current, value)
        else:
            target[key] = deepcopy(value)


def normalized_outputs(event: str, outputs: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for output in outputs:
        if not output.strip():
            continue
        rendered = codex_payload_from(event, output)
        try:
            value = json.loads(rendered)
        except json.JSONDecodeError:
            normalized.append({"systemMessage": rendered.strip()})
            continue
        if isinstance(value, dict):
            normalized.append(value)
        else:
            normalized.append({"systemMessage": rendered.strip()})
    return normalized


def merged_stdout(event: str, outputs: list[str]) -> str:
    values = normalized_outputs(event, outputs)
    if not values:
        return ""

    for predicate in (is_deny, is_block):
        for value in values:
            if predicate(value):
                return json.dumps(value, ensure_ascii=False) + "\n"

    base: dict[str, Any] = {}
    systems: list[str] = []
    contexts: list[str] = []
    for value in values:
        system_message = value.get("systemMessage")
        if isinstance(system_message, str) and system_message:
            systems.append(system_message)
        for key in ("additionalContext", "additional_context"):
            context = value.get(key)
            if isinstance(context, str) and context:
                contexts.append(context)
        specific = value.get("hookSpecificOutput")
        if isinstance(specific, dict):
            context = specific.get("additionalContext")
            if isinstance(context, str) and context:
                contexts.append(context)

        structured = deepcopy(value)
        structured.pop("systemMessage", None)
        structured.pop("additionalContext", None)
        structured.pop("additional_context", None)
        structured_specific = structured.get("hookSpecificOutput")
        if isinstance(structured_specific, dict):
            structured_specific.pop("additionalContext", None)
            if not structured_specific:
                structured.pop("hookSpecificOutput")
        merge_mapping(base, structured)

    if systems:
        base["systemMessage"] = "\n".join(systems)
    if contexts:
        specific = base.get("hookSpecificOutput")
        if not isinstance(specific, dict):
            specific = {}
            base["hookSpecificOutput"] = specific
        specific["hookEventName"] = event
        specific["additionalContext"] = "\n".join(contexts)
    if not base:
        base = values[0]
    return json.dumps(base, ensure_ascii=False) + "\n"


def aggregate_exit_code(event: str, outcomes: list[Outcome]) -> int:
    if any(outcome.returncode == 2 for outcome in outcomes):
        return 2
    if any(outcome.timed_out for outcome in outcomes):
        return 124
    failures = sorted(
        (outcome for outcome in outcomes if outcome.returncode not in {None, 0}),
        key=lambda outcome: outcome.index,
    )
    if not failures:
        return 0
    if event in CONTEXT_EVENTS and all(outcome.returncode == 1 for outcome in failures):
        return 0
    return failures[0].returncode or 1


def merged_stderr(event: str, outcomes: list[Outcome]) -> str:
    failures = [
        outcome
        for outcome in outcomes
        if outcome.timed_out or outcome.returncode not in {None, 0}
    ]
    if (
        event in CONTEXT_EVENTS
        and failures
        and all(not outcome.timed_out and outcome.returncode == 1 for outcome in failures)
    ):
        return ""
    chunks: list[str] = []
    for outcome in sorted(failures, key=lambda item: item.index):
        if not outcome.stderr:
            continue
        chunks.append(
            outcome.stderr
            if outcome.stderr.endswith("\n")
            else outcome.stderr + "\n"
        )
    return "".join(chunks)


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: codex_hook_fanout.py <event> <plan.json>",
            file=sys.stderr,
        )
        return 64
    event = sys.argv[1]
    try:
        handlers = load_handlers(Path(sys.argv[2]))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 64

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid hook payload: {exc}", file=sys.stderr)
        return 65
    if not isinstance(payload, dict):
        print("invalid hook payload: expected a JSON object", file=sys.stderr)
        return 65
    invocations = build_invocations(event, handlers, raw)
    outcomes = run_all(invocations)
    stdout = merged_stdout(event, [outcome.stdout for outcome in outcomes])
    if stdout:
        sys.stdout.write(stdout)
    stderr = merged_stderr(event, outcomes)
    if stderr:
        sys.stderr.write(stderr)
    return aggregate_exit_code(event, outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
