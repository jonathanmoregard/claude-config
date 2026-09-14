"""Provider adapters (contract §Jobs: adapters.py).

One place knows how to talk to each headless CLI: how to find the binary,
build its argv, read its event stream, extract the final message / usage /
conversation id, and classify a failed run. The prompt never enters argv
here; ``PROMPT_PLACEHOLDER`` stands in until the runner ``materialize``s it,
so ``meta.json`` can record the argv without the prompt text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .sanitize import clean

PROMPT_PLACEHOLDER = "<prompt.md>"

# Sandbox-escaping flags are refused anywhere in argv, whatever the source.
DANGEROUS = re.compile(r"^--(dangerously|allow-dangerously)|^--full-auto$|^--yolo$")

# Error classes. The codex regexes are copied from
# ~/.claude/skills/advice-refine-test-loop/scripts/codex-advise.sh (measured
# 2026-08-27 against codex-cli 0.146.0): QUOTA is that script's quota grep,
# REJECT_PATTERN is verbatim and, as there, is consulted only on a non-zero
# exit so a successful run that merely mentions a 4xx is not misread.
CODEX_QUOTA = re.compile(r"usage limit|quota exceeded|rate limit|\b429\b", re.I)
REJECT_PATTERN = re.compile(
    r'"status"[[:space:]]*:[[:space:]]*4[0-9][0-9]|unexpected status 4[0-9][0-9]|\bHTTP 4[0-9][0-9]\b|'
    r"invalid_request_error|invalid_api_key|model_not_found|unsupported[_ ]model|not supported when using Codex|"
    r"is not supported (with|when|for)|no such model|unknown model|does not exist or you do not have access"
    .replace("[[:space:]]", r"\s"), re.I)
CLAUDE_QUOTA = re.compile(r"You've hit your|You've used|You're out of extra usage|rate_limit_error|rate limit|\b429\b", re.I)
OVERLOADED = re.compile(r"overloaded|\b529\b", re.I)

CLAUDE_TOOLS_READ_ONLY = "Read,Glob,Grep"
CLAUDE_TOOLS_WRITE = "Read,Glob,Grep,Edit,Write,MultiEdit,Bash"
CLAUDE_MAX_TURNS = "60"


class DangerousFlag(ValueError):
    """A sandbox-escaping flag was found in argv."""


class MissingBinary(FileNotFoundError):
    """The provider CLI could not be found."""


# --- binaries -----------------------------------------------------------------

def resolve_binary(provider: str, env: dict[str, str] | None = None) -> str | None:
    """Absolute path of the provider CLI or None.

    ``AI_ROUTER_CODEX_BIN`` / ``AI_ROUTER_CLAUDE_BIN`` override resolution (the
    unit suite points them at stub scripts). Otherwise claude is
    ``~/.local/bin/claude`` when executable (``claude`` on PATH may be a shell
    function) else ``shutil.which``; codex is ``shutil.which("codex")``.
    """
    e = os.environ if env is None else env
    override = e.get(f"AI_ROUTER_{provider.upper()}_BIN")
    if override:
        return override if os.access(override, os.X_OK) else None
    path = e.get("PATH", os.defpath)
    if provider == "claude":
        local = os.path.join(e.get("HOME", os.path.expanduser("~")), ".local", "bin", "claude")
        if os.access(local, os.X_OK):
            return local
    found = shutil.which(provider, path=path)
    return os.path.abspath(found) if found else None


# --- argv -----------------------------------------------------------------------

def refuse_dangerous(argv: list[str]) -> None:
    for a in argv[1:]:
        if a == "--":
            return  # everything after the separator is prompt text
        if DANGEROUS.match(a):
            raise DangerousFlag(f"refusing sandbox-escaping flag: {a}")


def _claude_common(model: str, effort: str | None, sandbox: str, schema: str | None, bare: bool) -> list[str]:
    argv = ["-p", "--output-format", "stream-json", "--verbose", "--model", model]
    if effort:
        argv += ["--effort", effort]
    if sandbox == "read-only":
        argv += ["--permission-mode", "default", "--tools", CLAUDE_TOOLS_READ_ONLY]
    else:
        argv += ["--permission-mode", "acceptEdits", "--allowedTools", CLAUDE_TOOLS_WRITE,
                 "--disallowedTools", "Agent"]
    if schema:
        argv += ["--json-schema", Path(schema).read_text(encoding="utf-8").strip()]
    if bare:
        # `--bare` never reads OAuth credentials (claude --help, 2.1.270) and this
        # machine authenticates via Max OAuth, so "bare" is spelled as: no
        # settings files (→ no hooks, no user permission rules) and no MCP.
        argv += ["--setting-sources", "", "--strict-mcp-config"]
    return argv


def build_argv(provider: str, binary: str, *, model: str, effort: str | None, sandbox: str, cwd: str,
               schema: str | None, bare: bool, job_dir: Path) -> list[str]:
    """argv for a fresh job; the prompt is PROMPT_PLACEHOLDER (contract §Jobs)."""
    if provider == "codex":
        argv = [binary, "exec", "--json", "-m", model, "-c", f"model_reasoning_effort={effort or 'medium'}",
                "--sandbox", sandbox, "-C", cwd, "--skip-git-repo-check", "-o", str(job_dir / "last_message.md")]
        if schema:
            argv += ["--output-schema", schema]
        # `--` is load-bearing: codex's variadic options otherwise swallow the
        # positional prompt (see codex-advise.sh, measured 2026-08-27).
        argv += ["--", PROMPT_PLACEHOLDER]
    else:
        argv = [binary] + _claude_common(model, effort, sandbox, schema, bare)
        # --max-turns immediately precedes the prompt so no variadic option can eat it.
        argv += ["--max-turns", CLAUDE_MAX_TURNS, PROMPT_PLACEHOLDER]
    refuse_dangerous(argv)
    return argv


def build_resume_argv(provider: str, binary: str, *, thread_id: str, model: str, effort: str | None,
                      sandbox: str, cwd: str, bare: bool) -> list[str]:
    """argv for `send`: codex `exec resume <thread>`, claude `-p --resume <session>`."""
    if provider == "codex":
        argv = [binary, "exec", "resume", thread_id, "--json", "-m", model,
                "-c", f"model_reasoning_effort={effort or 'medium'}", "--skip-git-repo-check", "--", PROMPT_PLACEHOLDER]
    else:
        argv = [binary] + _claude_common(model, effort, sandbox, None, bare)
        argv += ["--resume", thread_id, "--max-turns", CLAUDE_MAX_TURNS, PROMPT_PLACEHOLDER]
    refuse_dangerous(argv)
    return argv


def materialize(argv: list[str], prompt: str) -> list[str]:
    """Replace PROMPT_PLACEHOLDER with the prompt text (new list)."""
    return [prompt if a == PROMPT_PLACEHOLDER else a for a in argv]


# --- events -------------------------------------------------------------------

def parse_event_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def read_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [e for e in (parse_event_line(l) for l in lines) if e is not None]


def _claude_text_blocks(event: dict[str, Any]) -> list[str]:
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            out.append(str(block["text"]))
    return out


def agent_texts(provider: str, events: list[dict[str, Any]]) -> list[str]:
    """All agent-message texts in order (raw; callers sanitise)."""
    out: list[str] = []
    for e in events:
        if provider == "codex":
            item = e.get("item") or {}
            if e.get("type") == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                out.append(str(item["text"]))
        elif e.get("type") == "assistant":
            out.extend(_claude_text_blocks(e))
    return out


def latest_agent_text(provider: str, events: list[dict[str, Any]]) -> str | None:
    texts = agent_texts(provider, events)
    return clean(texts[-1]) if texts else None


def final_message(provider: str, events: list[dict[str, Any]], last_message_path: Path | None) -> str | None:
    """The run's final answer: codex `-o` file if written, claude `result`, else last agent text."""
    if provider == "codex" and last_message_path is not None:
        try:
            text = last_message_path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""  # -o file absent or unreadable: fall through to the event stream
        if text:
            return clean(text)
    if provider == "claude":
        for e in reversed(events):
            if e.get("type") == "result" and isinstance(e.get("result"), str) and e["result"]:
                return clean(e["result"])
    return latest_agent_text(provider, events)


def conversation_id(provider: str, events: list[dict[str, Any]]) -> str | None:
    """codex thread_id (thread.started) or claude session_id (system init / result)."""
    for e in events:
        if provider == "codex" and e.get("type") == "thread.started" and e.get("thread_id"):
            return str(e["thread_id"])
        if provider == "claude" and e.get("session_id"):
            return str(e["session_id"])
    return None


def usage(provider: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for e in reversed(events):
        if provider == "codex" and e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
            return dict(e["usage"])
        if provider == "claude" and e.get("type") == "result":
            u = dict(e.get("usage") or {})
            if e.get("total_cost_usd") is not None:
                u["total_cost_usd"] = e["total_cost_usd"]
            return u or None
    return None


def _short(text: Any, limit: int = 200) -> str:
    s = clean(str(text)).replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def summarize_event(provider: str, e: dict[str, Any]) -> str:
    """One sanitised human line per event (for `log`)."""
    t = str(e.get("type", "?"))
    if provider == "codex":
        if t == "thread.started":
            return f"thread.started {e.get('thread_id', '')}"
        if t.startswith("item."):
            item = e.get("item") or {}
            kind = item.get("type", "?")
            detail = item.get("text") or item.get("command") or item.get("message") or item.get("path") or ""
            return f"{t} {kind}" + (f": {_short(detail)}" if detail else "")
        if t == "turn.completed":
            return f"turn.completed usage={json.dumps(e.get('usage'), sort_keys=True)}"
        if t in ("turn.failed", "error"):
            err = e.get("error") if isinstance(e.get("error"), dict) else {"message": e.get("message")}
            return f"{t}: {_short(err.get('message', ''))}"
        return t
    if t == "system":
        return f"system {e.get('subtype', '')} session={e.get('session_id', '')} model={e.get('model', '')}"
    if t == "assistant":
        parts: list[str] = []
        for block in (e.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(f"text: {_short(block.get('text', ''))}")
            elif block.get("type") == "tool_use":
                parts.append(f"tool_use {block.get('name', '?')}")
        return "; ".join(parts) or "assistant"
    if t == "user":
        return "tool_result"
    if t == "result":
        return f"result {e.get('subtype', '')} cost={e.get('total_cost_usd')}" + \
            (f": {_short(e.get('result', ''))}" if e.get("result") else "")
    return t


# --- classification ---------------------------------------------------------------

def _diagnostics(provider: str, events: list[dict[str, Any]], stderr: str) -> str:
    parts = [stderr]
    for e in events:
        t = e.get("type")
        if t in ("error", "turn.failed"):
            parts.append(json.dumps(e))
        elif provider == "claude" and t == "result" and e.get("is_error"):
            parts.append(json.dumps(e))
        elif provider == "codex" and t and t.startswith("item.") and (e.get("item") or {}).get("type") == "error":
            parts.append(json.dumps(e))
    return "\n".join(parts)


def _finished_ok(provider: str, events: list[dict[str, Any]]) -> bool:
    if provider == "claude":
        return any(e.get("type") == "result" and not e.get("is_error") for e in events)
    return any(e.get("type") == "turn.completed" for e in events)


def classify(provider: str, exit_code: int, events: list[dict[str, Any]], stderr: str,
             last_message_path: Path | None = None) -> str:
    """done | quota | rejected | overloaded | unparseable | error (contract §Jobs)."""
    diag = _diagnostics(provider, events, stderr)
    quota_re = CODEX_QUOTA if provider == "codex" else CLAUDE_QUOTA
    if quota_re.search(diag):
        return "quota"
    if exit_code != 0:
        if OVERLOADED.search(diag):
            return "overloaded"
        if REJECT_PATTERN.search(diag):
            return "rejected"
        return "error"
    if provider == "claude" and any(e.get("type") == "result" and e.get("is_error") for e in events):
        return "error"
    if not _finished_ok(provider, events):
        return "unparseable"
    if final_message(provider, events, last_message_path) is None:
        return "unparseable"
    return "done"
