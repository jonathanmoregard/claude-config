#!/usr/bin/env python3
"""ai-router over stdio MCP, for sessions whose Bash tool runs in the sandbox.

Inside the sandbox ai-router cannot create its job directory, reach the user
systemd manager, or let Codex write ~/.codex. This server runs outside it and
offers the ai-router subcommands skills use as typed tools. It always runs the
configured ai-router entry point with an argv it builds itself, an environment
built from config.json, and paths confined to the configured roots.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

TAG = "[ai-router-broker]"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"

TIERS = ("strongest", "standard", "fast")
PROVIDERS = ("codex", "claude")
SANDBOXES = ("read-only", "workspace-write")
NEEDS = ("code", "vision", "write", "long-context")
MAX_JOB_TIMEOUT_S = 86400
MAX_LOG_TAIL = 1000
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_EFFORT_RE = re.compile(r"[a-z]{1,16}")
_JOB_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")

_POLICY_KEYS = frozenset({"version", "aiRouter", "roots", "path", "host", "schemaDir",
                          "callTimeoutSec", "maxWaitSec", "maxPromptBytes", "outputLimitChars"})
_POSITIVE_INTS = ("callTimeoutSec", "maxWaitSec", "maxPromptBytes", "outputLimitChars")


class Refused(ValueError):
    pass


@dataclass(frozen=True)
class Call:
    argv: list[str]
    stdin: bytes | None
    timeout: float


# --- policy ---------------------------------------------------------------

def _abs(value: object, key: str) -> str:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise RuntimeError(f"policy {key} must be an absolute path")
    return value


def load_policy(config: object) -> dict:
    raw = config.get("aiRouterBroker") if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError("config must contain an aiRouterBroker object")
    if raw.keys() != _POLICY_KEYS:
        raise RuntimeError("config keys must be exactly: " + ", ".join(sorted(_POLICY_KEYS)))
    if raw["version"] != 1:
        raise RuntimeError("unsupported config version")
    for key in _POSITIVE_INTS:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"policy {key} must be a positive integer")
    if raw["host"] not in PROVIDERS:
        raise RuntimeError(f"policy host must be one of: {', '.join(PROVIDERS)}")
    for key in ("roots", "path"):
        if not isinstance(raw[key], list) or not raw[key]:
            raise RuntimeError(f"policy {key} must be a non-empty list")
    path = [_abs(p, "path") for p in raw["path"]]
    if any(":" in p for p in path):
        raise RuntimeError("policy path entries must not contain ':'")
    return {**raw, "aiRouter": _abs(raw["aiRouter"], "aiRouter"), "path": path,
            "schemaDir": _abs(raw["schemaDir"], "schemaDir"),
            "roots": [os.path.realpath(_abs(r, "roots")) for r in raw["roots"]]}


def load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"broker config {path} is unreadable: {exc}") from exc


# --- paths ---------------------------------------------------------------

def _open_under_roots(label: str, path: object, policy: dict, flags: int) -> tuple[int, str]:
    """Open PATH refusing a symlink as its last component; judge the file actually opened."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise Refused(f"{label} must be an absolute path")
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        # O_DIRECTORY on a symlink fails ENOTDIR before O_NOFOLLOW's ELOOP.
        if exc.errno == errno.ELOOP or os.path.islink(path):
            raise Refused(f"{label} is a symlink: {path}") from None
        if exc.errno == errno.ENOTDIR:
            raise Refused(f"{label} is not a directory: {path}") from None
        raise Refused(f"{label} cannot be opened: {path}: {exc.strerror}") from None
    real = os.readlink(f"/proc/self/fd/{fd}")
    if not any(real == root or real.startswith(root + "/") for root in policy["roots"]):
        os.close(fd)
        raise Refused(f"{label} is outside {', '.join(policy['roots'])}: {real}")
    return fd, real


def _read_file(label: str, path: object, policy: dict) -> bytes:
    fd, _ = _open_under_roots(label, path, policy, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"{label} is not a regular file: {path}")
        limit = policy["maxPromptBytes"]
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > limit:
        raise Refused(f"{label} exceeds {limit} bytes")
    return data


def _prompt_bytes(label: str, data: bytes, policy: dict) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{label} is not UTF-8 text") from None
    if not text.strip():
        raise Refused(f"{label} is empty")
    if len(data) > policy["maxPromptBytes"]:
        raise Refused(f"{label} exceeds {policy['maxPromptBytes']} bytes")
    return data


def _cwd(name: str, value: object, policy: dict) -> list[str]:
    fd, real = _open_under_roots(name, value, policy, os.O_RDONLY | os.O_DIRECTORY)
    os.close(fd)
    return [f"--cwd={real}"]


def _schema_file(name: str, value: object, policy: dict) -> list[str]:
    data = _read_file(name, value, policy)
    try:
        json.loads(data)
    except ValueError:
        raise Refused(f"{name} is not valid JSON: {value}") from None
    # ai-router hands the schema path to the job, which may read it after this
    # call returns; a copy the caller cannot write keeps it what was checked.
    folder = Path(policy["schemaDir"])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    copy = folder / f"{hashlib.sha256(data).hexdigest()}.json"
    if not copy.exists():
        scratch = folder / f".{copy.name}.{os.getpid()}.{threading.get_ident()}"
        scratch.write_bytes(data)
        os.replace(scratch, copy)
    return [f"--schema={copy}"]


# --- options ---------------------------------------------------------------

def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _choice(values: tuple[str, ...], desc: str):
    def render(name: str, value: object, policy: dict) -> list[str]:
        if value not in values:
            raise Refused(f"{name} must be one of: {', '.join(values)}")
        return [f"{_flag(name)}={value}"]
    return {"type": "string", "enum": list(values), "description": desc}, render


def _pattern(rx: re.Pattern, desc: str):
    def render(name: str, value: object, policy: dict) -> list[str]:
        if not isinstance(value, str) or not rx.fullmatch(value):
            raise Refused(f"{name} must match {rx.pattern}")
        return [f"{_flag(name)}={value}"]
    return {"type": "string", "description": desc}, render


def _switch(desc: str):
    def render(name: str, value: object, policy: dict) -> list[str]:
        if not isinstance(value, bool):
            raise Refused(f"{name} must be a boolean")
        return [_flag(name)] if value else []
    return {"type": "boolean", "description": desc}, render


def _bounded(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Refused(f"{name} must be an integer between {low} and {high}")
    return value


def _int_range(low: int, high: int, desc: str):
    def render(name: str, value: object, policy: dict) -> list[str]:
        return [f"{_flag(name)}={_bounded(name, value, low, high)}"]
    return {"type": "integer", "minimum": low, "description": desc}, render


def _needs(name: str, value: object, policy: dict) -> list[str]:
    if not isinstance(value, list) or not all(v in NEEDS for v in value):
        raise Refused(f"{name} must be a list of: {', '.join(NEEDS)}")
    return [f"--need={v}" for v in value]


def _path_schema(desc: str) -> dict:
    return {"type": "string", "description": desc}


OPTIONS: dict[str, tuple[dict, Callable[[str, object, dict], list[str]] | None]] = {
    "tier": _choice(TIERS, "Pick by ranking and quota headroom within this tier."),
    "model": _pattern(_NAME_RE, "Model id or alias (e.g. luna, terra, sol); still subject "
                                "to quota eligibility."),
    "prompt": ({"type": "string", "description": "The whole task as text."}, None),
    "prompt_file": (_path_schema("Absolute path of a regular file under ~/Repos or ~/worktrees "
                                 "holding the whole task; read by the broker."), None),
    "cwd": (_path_schema("Absolute working directory of the job, under ~/Repos or ~/worktrees; "
                         "its last component may not be a symlink."), _cwd),
    "sandbox": _choice(SANDBOXES, "Job sandbox (ai-router default read-only)."),
    "schema_file": (_path_schema("Absolute path of a JSON schema file under ~/Repos or "
                                 "~/worktrees forcing structured output (result_json)."),
                    _schema_file),
    "effort": _pattern(_EFFORT_RE, "Reasoning effort (low|medium|high|xhigh|max; Codex also ultra)."),
    "timeout_s": _int_range(1, MAX_JOB_TIMEOUT_S, "Seconds. delegate/send: the job's timeout "
                            "(ai-router default 3600). wait: how long to block (default and "
                            "cap: the broker's maximum)."),
    "provider": _choice(PROVIDERS, "Constrain to one provider."),
    "host": _choice(PROVIDERS, "Host CLI for the native/cross-provider decision "
                               "(default: this broker's host)."),
    "name": _pattern(_NAME_RE, "Label recorded on the job."),
    "need": ({"type": "array", "items": {"type": "string", "enum": list(NEEDS)},
              "description": "Required capabilities."}, _needs),
    "allow_downgrade": _switch("Allow a lower tier when nothing in the tier is eligible."),
    "fresh": _switch("Bypass the quota cache."),
    "job_id": ({"type": "string", "description": "Job id as returned by delegate or list."}, None),
    "tail": _int_range(1, MAX_LOG_TAIL, "Number of trailing log lines (default 50)."),
    "all": _switch("Every job, not only the newest 20."),
}


@dataclass(frozen=True)
class Tool:
    description: str
    options: tuple[str, ...]
    required: tuple[str, ...] = ()
    one_of: tuple[tuple[str, str], ...] = field(default_factory=tuple)


TOOLS: dict[str, Tool] = {
    "delegate": Tool(
        "Start a detached headless ai-router job (Codex or Claude) and return at once with its "
        "job_id. Follow it with status/peek/log, block with wait, read it with result. "
        "Give exactly one of tier/model and one of prompt/prompt_file.",
        ("tier", "model", "prompt", "prompt_file", "cwd", "sandbox", "schema_file", "effort",
         "timeout_s", "provider", "host", "name", "need", "allow_downgrade", "fresh"),
        required=("cwd",), one_of=(("tier", "model"), ("prompt", "prompt_file"))),
    "wait": Tool("Block until the job (following an automatic quota retry) is terminal, then "
                 "return its result. timeout_s defaults to and is capped at this broker's maximum.",
                 ("job_id", "timeout_s"), required=("job_id",)),
    "status": Tool("Status of one job, or of every job when job_id is omitted.", ("job_id",)),
    "result": Tool("Final result of a finished job: result_text, result_json, usage, error_class.",
                   ("job_id",), required=("job_id",)),
    "peek": Tool("Newest agent message of a job (sanitised).", ("job_id",), required=("job_id",)),
    "log": Tool("Summarised events and stderr of a job.", ("job_id", "tail"), required=("job_id",)),
    "send": Tool("Resume a finished job's thread with a follow-up prompt, as a new job.",
                 ("job_id", "prompt", "prompt_file", "timeout_s"), required=("job_id",),
                 one_of=(("prompt", "prompt_file"),)),
    "cancel": Tool("Stop a running job.", ("job_id",), required=("job_id",)),
    "list": Tool("Recent jobs, newest first.", ("all",)),
    "quota": Tool("Usage-cap snapshots per provider: buckets, headroom, rejected_until.",
                  ("fresh", "provider")),
    "pick": Tool("Choose a model for a tier; native true means use your own subagents.",
                 ("tier", "need", "provider", "host", "allow_downgrade", "fresh"),
                 required=("tier",)),
}


def _input_schema(tool: Tool) -> dict:
    props = {name: OPTIONS[name][0] for name in tool.options}
    return {"type": "object", "properties": props, "required": list(tool.required),
            "additionalProperties": False}


def tool_list() -> list[dict]:
    return [{"name": name, "description": tool.description, "inputSchema": _input_schema(tool)}
            for name, tool in TOOLS.items()]


def build_call(name: str, arguments: object, policy: dict) -> Call:
    tool = TOOLS.get(name)
    if tool is None:
        raise Refused(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise Refused("arguments must be an object")
    args = {k: v for k, v in arguments.items() if v is not None}
    unexpected = sorted(set(args) - set(tool.options))
    if unexpected:
        raise Refused(f"unexpected fields: {', '.join(unexpected)}")
    missing = [k for k in tool.required if k not in args]
    if missing:
        raise Refused(f"missing fields: {', '.join(missing)}")
    for pair in tool.one_of:
        if sum(k in args for k in pair) != 1:
            raise Refused(f"give exactly one of {', '.join(pair)}")
    if "host" in tool.options:
        args.setdefault("host", policy["host"])

    argv = [policy["aiRouter"], name, "--json"]
    timeout = float(policy["callTimeoutSec"])
    if name == "wait":
        wait_s = _bounded("timeout_s", args.pop("timeout_s", policy["maxWaitSec"]), 1,
                          policy["maxWaitSec"])
        argv.append(f"--timeout-s={wait_s}")
        timeout += wait_s
    stdin = None
    if "prompt" in args:
        prompt = args.pop("prompt")
        if not isinstance(prompt, str):
            raise Refused("prompt must be a string")
        stdin = _prompt_bytes("prompt", prompt.encode("utf-8"), policy)
    elif "prompt_file" in args:
        path = args.pop("prompt_file")
        stdin = _prompt_bytes("prompt_file", _read_file("prompt_file", path, policy), policy)
    if stdin is not None:
        argv.append("--prompt-file=/dev/stdin")
    job_id = args.pop("job_id", None)
    for key, value in args.items():
        argv += OPTIONS[key][1](key, value, policy)
    if job_id is not None:
        if not isinstance(job_id, str) or not _JOB_RE.fullmatch(job_id):
            raise Refused(f"job_id must match {_JOB_RE.pattern}")
        argv.append(job_id)
    return Call(argv=argv, stdin=stdin, timeout=timeout)


# --- running ---------------------------------------------------------------

def child_env(policy: dict) -> dict[str, str]:
    user = pwd.getpwuid(os.getuid())
    return {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name,
            "LANG": "C.UTF-8", "PATH": ":".join(policy["path"]),
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def run_call(call: Call, policy: dict) -> tuple[bool, str]:
    """→ (is_error, text)."""
    proc = subprocess.Popen(call.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=child_env(policy), cwd="/",
                            start_new_session=True)
    try:
        out, err = proc.communicate(call.stdin or b"", timeout=call.timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return True, f"{TAG} ai-router {call.argv[1]} timed out after {call.timeout:g}s; stopped"
    text = out.decode("utf-8", "replace").rstrip()
    if proc.returncode != 0:
        stderr = err.decode("utf-8", "replace").strip()
        text += f"\n{TAG} ai-router exit {proc.returncode}"
        if stderr:
            text += "\n--- stderr ---\n" + stderr[-2000:]
    limit = policy["outputLimitChars"]
    if len(text) > limit:
        text = text[:limit] + f"\n{TAG} output truncated at {limit} chars"
    return proc.returncode != 0, text


# --- MCP ---------------------------------------------------------------

class Broker:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.write_lock = threading.Lock()

    def respond(self, msg_id: Any, result: Any = None, error: Any = None) -> None:
        out: dict = {"jsonrpc": "2.0", "id": msg_id}
        out["error" if error is not None else "result"] = error if error is not None else result
        with self.write_lock:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()

    def call_tool(self, msg_id: Any, params: dict) -> None:
        try:
            policy = load_policy(load_config(self.config_path))
            is_error, text = run_call(build_call(params.get("name"), params.get("arguments") or {},
                                                 policy), policy)
        except Refused as exc:
            is_error, text = True, f"REFUSED: {exc}"
        except Exception as exc:
            is_error, text = True, f"ERROR: {exc}"
        self.respond(msg_id, result={"content": [{"type": "text", "text": text}],
                                     "isError": is_error})

    def handle(self, msg: dict) -> None:
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            self.respond(msg_id, result={"protocolVersion": "2024-11-05",
                                         "capabilities": {"tools": {"listChanged": False}},
                                         "serverInfo": {"name": "ai-router-broker",
                                                        "version": "1.0.0"}})
        elif method == "tools/list":
            self.respond(msg_id, result={"tools": tool_list()})
        elif method == "tools/call" and msg_id is not None:
            threading.Thread(target=self.call_tool, args=(msg_id, msg.get("params") or {}),
                             name=f"ai-router-broker-{msg_id}").start()
        elif msg_id is not None and not str(method).startswith("notifications/"):
            self.respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})

    def serve(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self.handle(msg)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ai-router-broker")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    Broker(parser.parse_args(argv).config).serve()


if __name__ == "__main__":
    main()
