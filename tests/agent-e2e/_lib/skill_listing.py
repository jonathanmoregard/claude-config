"""Graders for the skill-listing scenarios: read transcript.jsonl (stream-json)."""
import json, sys


def events(path):
    for line in open(path, encoding="utf-8"):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def blocks(ev):
    c = (ev.get("message") or {}).get("content")
    return c if isinstance(c, list) else []


def main_first_context(path):
    """Input tokens of the main session's first API call (the always-on layer)."""
    for ev in events(path):
        if ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
            u = (ev.get("message") or {}).get("usage") or {}
            return sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return None


def subagent_first_context(path):
    """Input tokens of a subagent's first API call."""
    for ev in events(path):
        if ev.get("type") == "assistant" and ev.get("parent_tool_use_id"):
            u = (ev.get("message") or {}).get("usage") or {}
            return sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return None


def tool_uses(path, sub=None):
    """(name, input) of tool_use blocks; sub=True only subagent, False only main."""
    for ev in events(path):
        if ev.get("type") != "assistant":
            continue
        is_sub = bool(ev.get("parent_tool_use_id"))
        if sub is not None and is_sub != sub:
            continue
        for b in blocks(ev):
            if b.get("type") == "tool_use":
                yield b.get("name"), b.get("input") or {}
