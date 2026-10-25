#!/usr/bin/env python3
"""Host-side ai-router broker over stdio MCP.

The server exposes one structured tool and delegates every operation to the
real ai-router CLI. It never accepts raw argv, shell text, force/downgrade
flags, environment overrides, or caller-selected executables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


AI_ROUTER = Path("/home/jonathan/.claude/bin/ai-router")
ALLOWED_CWD_ROOTS = (
    Path("/home/jonathan/Repos"),
    Path("/home/jonathan/worktrees"),
    Path("/tmp"),
)
MAX_PROMPT_CHARS = 1_000_000
MAX_OUTPUT_CHARS = 60_000
MAX_LOG_LINES = 500
MAX_WAIT_S = 600
MAX_JOB_TIMEOUT_S = 86_400
CLI_TIMEOUT_S = 30

_OPERATIONS = frozenset({"delegate", "status", "wait", "result", "log"})
_TIERS = frozenset({"strongest", "standard", "fast"})
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
_SANDBOXES = frozenset({"read-only", "workspace-write"})
_JOB_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")


class _InFlight:
    __slots__ = ("cancelled", "proc")

    def __init__(self) -> None:
        self.cancelled = False
        self.proc: subprocess.Popen | None = None


_write_lock = threading.Lock()
_in_flight_lock = threading.Lock()
_in_flight: dict[Any, _InFlight] = {}


def _validate_cwd(raw: object) -> Path:
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or len(raw) > 4096
        or not Path(raw).is_absolute()
    ):
        raise RuntimeError("cwd must be a bounded absolute path")
    lexical = Path(os.path.normpath(raw))
    try:
        resolved = lexical.resolve(strict=True)
        info = lexical.lstat()
    except OSError as exc:
        raise RuntimeError("cwd does not exist") from exc
    if resolved != lexical or lexical.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("cwd must be a canonical non-symlink directory")
    if not any(resolved == root or root in resolved.parents for root in ALLOWED_CWD_ROOTS):
        raise RuntimeError("cwd must resolve under /home/jonathan/Repos, /home/jonathan/worktrees, or /tmp")
    return resolved


def _validate_schema(raw: object, cwd: Path) -> Path:
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or len(raw) > 4096
        or not Path(raw).is_absolute()
    ):
        raise RuntimeError("schema must be an absolute path inside cwd")
    lexical = Path(os.path.normpath(raw))
    try:
        resolved = lexical.resolve(strict=True)
        info = lexical.lstat()
    except OSError as exc:
        raise RuntimeError("schema does not exist") from exc
    if (
        resolved != lexical
        or lexical.is_symlink()
        or cwd not in resolved.parents
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_size > 1_048_576
    ):
        raise RuntimeError("schema must be an owned canonical regular file inside cwd")
    try:
        schema = json.loads(lexical.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("schema must contain valid JSON") from exc
    if not isinstance(schema, dict):
        raise RuntimeError("schema JSON must be an object")
    return resolved


def _strict_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RuntimeError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _validate_request(args: object) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise RuntimeError("request arguments must be an object")
    operation = args.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise RuntimeError("operation must be delegate, status, wait, result, or log")
    allowed = {
        "delegate": {
            "operation",
            "prompt",
            "model",
            "tier",
            "effort",
            "sandbox",
            "cwd",
            "name",
            "schema",
            "timeout_s",
            "dev_gates",
        },
        "status": {"operation", "job_id"},
        "wait": {"operation", "job_id", "timeout_s"},
        "result": {"operation", "job_id"},
        "log": {"operation", "job_id", "tail_lines"},
    }[operation]
    unexpected = sorted(set(args) - allowed)
    if unexpected:
        raise RuntimeError(f"unexpected fields for {operation}: " + ", ".join(unexpected))

    request: dict[str, Any] = {"operation": operation}
    if operation != "delegate":
        job_id = args.get("job_id")
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise RuntimeError("job_id is missing or malformed")
        request["job_id"] = job_id
        if operation == "wait":
            request["timeout_s"] = _strict_int(
                args.get("timeout_s", MAX_WAIT_S), "timeout_s", 0, MAX_WAIT_S
            )
        if operation == "log":
            request["tail_lines"] = _strict_int(
                args.get("tail_lines", 100), "tail_lines", 1, MAX_LOG_LINES
            )
        return request

    prompt = args.get("prompt")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or "\x00" in prompt
        or len(prompt) > MAX_PROMPT_CHARS
    ):
        raise RuntimeError("prompt must be a non-empty string within the size limit")
    has_model = "model" in args
    has_tier = "tier" in args
    if has_model == has_tier:
        raise RuntimeError("delegate requires exactly one of model or tier")
    if has_model:
        model = args["model"]
        if not isinstance(model, str) or _MODEL.fullmatch(model) is None:
            raise RuntimeError("model is malformed")
        request["model"] = model
    else:
        tier = args["tier"]
        if not isinstance(tier, str) or tier not in _TIERS:
            raise RuntimeError("tier must be strongest, standard, or fast")
        request["tier"] = tier
    effort = args.get("effort")
    if effort is not None and (not isinstance(effort, str) or effort not in _EFFORTS):
        raise RuntimeError("effort is unsupported")
    sandbox = args.get("sandbox", "read-only")
    if not isinstance(sandbox, str) or sandbox not in _SANDBOXES:
        raise RuntimeError("sandbox must be read-only or workspace-write")
    cwd = _validate_cwd(args.get("cwd"))
    dev_gates = args.get("dev_gates", False)
    if not isinstance(dev_gates, bool):
        raise RuntimeError("dev_gates must be a boolean")
    request.update(
        prompt=prompt,
        effort=effort,
        sandbox=sandbox,
        cwd=cwd,
        dev_gates=dev_gates,
    )
    if "timeout_s" in args:
        request["timeout_s"] = _strict_int(
            args["timeout_s"], "timeout_s", 1, MAX_JOB_TIMEOUT_S
        )
    if "name" in args:
        name = args["name"]
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise RuntimeError("name is malformed")
        request["name"] = name
    if "schema" in args:
        request["schema"] = _validate_schema(args["schema"], cwd)
    return request


def _host_env() -> dict[str, str]:
    """Keep the host login environment but remove sandbox inheritance markers."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEX_SANDBOX")
    }


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()


def _run_cli(
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    rec: _InFlight | None = None,
) -> dict[str, Any]:
    try:
        info = AI_ROUTER.lstat()
    except OSError as exc:
        raise RuntimeError("the fixed ai-router executable is unavailable") from exc
    if AI_ROUTER.is_symlink() or not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR:
        raise RuntimeError("the fixed ai-router path is not an executable regular file")
    proc = subprocess.Popen(
        [str(AI_ROUTER), *argv, "--json"],
        cwd=cwd,
        env=_host_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if rec is not None:
        rec.proc = proc
        if rec.cancelled:
            _terminate(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        raise RuntimeError("ai-router operation timed out")
    finally:
        if rec is not None:
            rec.proc = None
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        message = stderr.strip()[:1000] or "no diagnostic"
        raise RuntimeError(f"ai-router returned malformed JSON: {message}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("ai-router returned a non-object result")
    return result


def _bound_result(result: dict[str, Any]) -> str:
    text = json.dumps(result, sort_keys=True, ensure_ascii=False)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    bounded = dict(result)
    if isinstance(bounded.get("lines"), list):
        lines = list(bounded["lines"])
        while lines and len(json.dumps({**bounded, "lines": lines}, ensure_ascii=False)) > MAX_OUTPUT_CHARS:
            lines.pop(0)
        bounded["lines"] = lines
        bounded["truncated"] = True
    if isinstance(bounded.get("result_text"), str):
        bounded["result_text"] = bounded["result_text"][: MAX_OUTPUT_CHARS // 2] + "…"
        bounded["truncated"] = True
    if bounded.get("result_json") is not None:
        bounded["result_json"] = {"truncated": True}
        bounded["truncated"] = True
    text = json.dumps(bounded, sort_keys=True, ensure_ascii=False)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return json.dumps(
        {
            "ok": bounded.get("ok", False),
            "job_id": bounded.get("job_id"),
            "status": bounded.get("status"),
            "truncated": True,
        },
        sort_keys=True,
    )


def _delegate(request: dict[str, Any], rec: _InFlight | None) -> dict[str, Any]:
    fd, prompt_path_raw = tempfile.mkstemp(prefix="ai-router-mcp-", suffix=".md", dir="/tmp")
    prompt_path = Path(prompt_path_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(request["prompt"])
        argv = ["delegate"]
        if "model" in request:
            argv += ["--model", request["model"]]
        else:
            argv += ["--tier", request["tier"]]
        argv += [
            "--prompt-file",
            str(prompt_path),
            "--cwd",
            str(request["cwd"]),
            "--sandbox",
            request["sandbox"],
            "--host",
            "codex",
            "--provider",
            "codex",
        ]
        if request.get("timeout_s") is not None:
            argv += ["--timeout-s", str(request["timeout_s"])]
        if request.get("effort"):
            argv += ["--effort", request["effort"]]
        if request.get("name"):
            argv += ["--name", request["name"]]
        if request.get("schema"):
            argv += ["--schema", str(request["schema"])]
        if request["dev_gates"]:
            argv.append("--dev-gates")
        return _run_cli(argv, cwd=request["cwd"], timeout_s=CLI_TIMEOUT_S, rec=rec)
    finally:
        prompt_path.unlink(missing_ok=True)


def _tool_ai_router(args: object, rec: _InFlight | None = None) -> str:
    request = _validate_request(args)
    operation = request["operation"]
    if operation == "delegate":
        result = _delegate(request, rec)
    else:
        argv = [operation, request["job_id"]]
        timeout_s = CLI_TIMEOUT_S
        if operation == "wait":
            argv += ["--timeout-s", str(request["timeout_s"])]
            timeout_s = request["timeout_s"] + 10
        elif operation == "log":
            argv += ["--tail", str(request["tail_lines"])]
        result = _run_cli(argv, cwd=Path("/tmp"), timeout_s=timeout_s, rec=rec)
    return _bound_result(result)


def _write_msg(message: dict[str, Any]) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _respond(msg_id: Any, result: object = None, error: object = None) -> None:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    _write_msg(message)


def _send_response(msg_id: Any, result: object = None, error: object = None) -> None:
    with _write_lock:
        with _in_flight_lock:
            rec = _in_flight.pop(msg_id, None)
            if rec is not None and rec.cancelled:
                return
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _on_cancelled(request_id: Any) -> None:
    with _in_flight_lock:
        rec = _in_flight.get(request_id)
        if rec is None:
            return
        rec.cancelled = True
        proc = rec.proc
    if proc is not None:
        _terminate(proc)


def _dispatch_tools_call(msg_id: Any, params: dict[str, Any], rec: _InFlight) -> None:
    try:
        name = params.get("name")
        if name != "ai_router":
            _send_response(msg_id, error={"code": -32601, "message": f"Unknown tool: {name}"})
            return
        output = _tool_ai_router(params.get("arguments") or {}, rec)
        _send_response(msg_id, result={"content": [{"type": "text", "text": output}]})
    except Exception as exc:
        _send_response(
            msg_id,
            result={
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "isError": True,
            },
        )


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        _respond(
            msg_id,
            result={
                "protocolVersion": "2024-11-05",
                "capabilities": CAPABILITIES,
                "serverInfo": SERVER_INFO,
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "notifications/cancelled":
        if params.get("requestId") is not None:
            _on_cancelled(params["requestId"])
        return
    if method == "tools/list":
        _respond(msg_id, result={"tools": TOOLS})
        return
    if method == "tools/call":
        if msg_id is None:
            return
        with _in_flight_lock:
            collision = msg_id in _in_flight
            if not collision:
                rec = _InFlight()
                _in_flight[msg_id] = rec
        if collision:
            _respond(msg_id, error={"code": -32600, "message": f"Request id {msg_id} already in flight"})
            return
        threading.Thread(
            target=_dispatch_tools_call,
            args=(msg_id, params, rec),
            daemon=False,
            name=f"ai-router-mcp-{msg_id}",
        ).start()
        return
    if msg_id is not None:
        _respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def _drain_in_flight(timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with _in_flight_lock:
            if not _in_flight:
                return
        time.sleep(0.05)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(message)
        except Exception as exc:
            print(f"[ai-router-mcp] unhandled error: {exc}", file=sys.stderr, flush=True)
    _drain_in_flight()


SERVER_INFO = {"name": "ai-router", "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}
TOOLS = [
    {
        "name": "ai_router",
        "description": (
            "Delegate through the host ai-router from a sandboxed Codex session. "
            "delegate writes prompt text to a private temporary file and invokes the "
            "real CLI without --force or --allow-downgrade, returning a job_id at "
            "once. Use status, bounded wait (at most 600 seconds), result, and bounded "
            "log to follow it. cwd is restricted to /home/jonathan/Repos, "
            "/home/jonathan/worktrees, or /tmp. dev_gates is retained for API "
            "compatibility but the CLI rejects it under managed permission profiles; "
            "use klaffat-app-runner check mode for Klaffat gates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["delegate", "status", "wait", "result", "log"],
                    "description": "Structured ai-router operation.",
                },
                "prompt": {"type": "string", "description": "Required only for delegate; full task text."},
                "model": {"type": "string", "description": "Delegate with this exact configured model; mutually exclusive with tier."},
                "tier": {"type": "string", "enum": ["strongest", "standard", "fast"], "description": "Delegate through this routing tier; mutually exclusive with model."},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "ultra"], "description": "Optional delegate reasoning effort."},
                "sandbox": {"type": "string", "enum": ["read-only", "workspace-write"], "description": "Requested delegate access; read-only selects the managed repos_readonly profile."},
                "cwd": {"type": "string", "description": "Required delegate working directory under an approved root."},
                "name": {"type": "string", "description": "Optional bounded job label."},
                "schema": {"type": "string", "description": "Optional absolute path to an owned JSON schema inside cwd."},
                "timeout_s": {"type": "integer", "minimum": 0, "maximum": MAX_JOB_TIMEOUT_S, "description": "Delegate job timeout (1..86400), or bounded wait timeout (0..600)."},
                "dev_gates": {"type": "boolean", "description": "Unsupported under managed profiles; use klaffat-app-runner check."},
                "job_id": {"type": "string", "description": "Required for status, wait, result, and log."},
                "tail_lines": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_LINES, "description": "Optional only for log."},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    }
]


if __name__ == "__main__":
    main()
