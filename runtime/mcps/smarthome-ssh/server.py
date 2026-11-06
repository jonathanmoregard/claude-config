#!/usr/bin/env python3
"""Host-pinned SSH broker for smarthome CI/CD work over stdio MCP.

The server exposes one ``ssh`` tool that runs a command on exactly one
configured host as exactly one configured user. The host key is checked
against a pinned known_hosts file, authentication goes only through the
user's existing SSH agent socket (no key file is ever opened), and every
forwarding, proxy, and multiplexing channel is disabled.

Protocol: MCP 2024-11-05 over stdio, JSON-RPC 2.0 line-delimited.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional


class _InFlight:
    """Per-request state for the threaded dispatcher.

    `cancelled` flips to True when `notifications/cancelled` arrives for this
    id; `proc` is the running ssh child so cancellation can signal its group.
    """
    __slots__ = ("cancelled", "proc")

    def __init__(self) -> None:
        self.cancelled = False
        self.proc: Optional[subprocess.Popen] = None


# Stdout writes from the main thread and worker threads MUST be serialized,
# else JSON-RPC frames interleave and corrupt the protocol.
_write_lock = threading.Lock()
_in_flight_lock = threading.Lock()
_in_flight: dict[Any, _InFlight] = {}

DEFAULT_TIMEOUT_MS = 120_000
MAX_COMMAND_CHARS = 16_384

HERE = Path(__file__).resolve().parent
CONFIG_PATH = str(HERE / "config.json")

_POLICY_KEYS = frozenset(
    {
        "version",
        "host",
        "user",
        "knownHostsFile",
        "sshExecutable",
        "identityAgent",
        "identityAgentRoot",
        "connectTimeoutSec",
        "maxTimeoutMs",
        "outputLimitChars",
    }
)
_POSITIVE_INTS = ("connectTimeoutSec", "maxTimeoutMs", "outputLimitChars")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _runtime_dir() -> Path:
    # From getuid(), never the environment: the policy stays non-injectable.
    return Path(f"/run/user/{os.getuid()}")


def _expand_runtime_dir(value):
    """Expand a leading "%t" path component (systemd's runtime-dir specifier)."""
    if isinstance(value, str) and (value == "%t" or value.startswith("%t/")):
        return str(_runtime_dir()) + value[2:]
    return value


def _require_executable(path: str, key: str) -> str:
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise RuntimeError(f"BROKEN safety boundary: {key} is not executable: {path}")
    return path


def _require_agent_socket(policy: dict) -> None:
    agent: Path = policy["identityAgent"]
    if not _path_within(agent, policy["identityAgentRoot"]):
        raise RuntimeError("BROKEN safety boundary: identity agent is outside its allowed root")
    if not agent.exists():
        raise RuntimeError(f"BROKEN safety boundary: identity agent socket is unavailable: {agent}")


def _known_hosts_pins(path: Path, host: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"BROKEN safety boundary: known_hosts is missing: {path} "
            "(machine-local, untracked; it must pin the host key of the configured host)"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"BROKEN safety boundary: known_hosts is unreadable: {path}") from exc
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and not line.lstrip().startswith("#"):
            if host in fields[0].split(","):
                return True
    return False


def _load_policy(config: dict, base_dir: Path = HERE) -> dict:
    raw = config.get("smarthomeSsh") if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError("policy must contain a smarthomeSsh object")
    missing = sorted(_POLICY_KEYS - raw.keys())
    if missing:
        raise RuntimeError(f"policy is missing keys: {', '.join(missing)}")
    unknown = sorted(raw.keys() - _POLICY_KEYS)
    if unknown:
        raise RuntimeError(f"policy has unknown keys: {', '.join(unknown)}")
    if raw["version"] != 1:
        raise RuntimeError("unsupported policy version")
    raw = dict(raw)
    for key in ("identityAgent", "identityAgentRoot"):
        raw[key] = _expand_runtime_dir(raw[key])
    for key in ("host", "user"):
        value = raw[key]
        if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
            raise RuntimeError(f"policy {key} must be a plain name")
    for key in _POSITIVE_INTS:
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"policy {key} must be a positive integer")

    ssh = raw["sshExecutable"]
    if not isinstance(ssh, str) or not os.path.isabs(ssh):
        raise RuntimeError("policy sshExecutable must be an absolute path")
    for key in ("knownHostsFile", "identityAgent", "identityAgentRoot"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise RuntimeError(f"policy {key} must be a non-empty path")
    for key in ("identityAgent", "identityAgentRoot"):
        if not os.path.isabs(raw[key]):
            raise RuntimeError(f"policy {key} must be an absolute path")

    policy = dict(raw)
    policy["knownHostsFile"] = (base_dir / raw["knownHostsFile"]).resolve()
    policy["identityAgent"] = Path(raw["identityAgent"]).resolve(strict=False)
    policy["identityAgentRoot"] = Path(raw["identityAgentRoot"]).resolve(strict=False)
    policy["sshExecutable"] = _require_executable(ssh, "sshExecutable")
    if not _known_hosts_pins(policy["knownHostsFile"], policy["host"]):
        raise RuntimeError(f"known_hosts does not pin host {policy['host']!r}")
    _require_agent_socket(policy)
    return policy


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"BROKEN safety boundary: broker config is unreadable: {exc}") from exc


def _validate_request(args: object, policy: dict) -> dict:
    if not isinstance(args, dict):
        raise RuntimeError("arguments must be an object")
    unexpected = sorted(set(args) - {"command", "timeout"})
    if unexpected:
        raise RuntimeError(f"unexpected fields: {', '.join(unexpected)}")
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RuntimeError("command must be a non-empty string")
    if "\x00" in command:
        raise RuntimeError("command must not contain NUL")
    if len(command) > MAX_COMMAND_CHARS:
        raise RuntimeError(f"command exceeds {MAX_COMMAND_CHARS} characters")
    timeout = args.get("timeout", DEFAULT_TIMEOUT_MS)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise RuntimeError("timeout must be an integer number of milliseconds")
    if timeout <= 0 or timeout > policy["maxTimeoutMs"]:
        raise RuntimeError(f"timeout must be between 1 and {policy['maxTimeoutMs']} ms")
    return {"command": command, "timeout": timeout}


def _build_ssh_argv(command: str, policy: dict) -> list[str]:
    _require_agent_socket(policy)
    options = [
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={policy['knownHostsFile']}",
        "GlobalKnownHostsFile=/dev/null",
        "UpdateHostKeys=no",
        f"IdentityAgent={policy['identityAgent']}",
        "IdentityFile=none",
        "ForwardAgent=no",
        "ForwardX11=no",
        "ClearAllForwardings=yes",
        "PermitLocalCommand=no",
        "ProxyCommand=none",
        "ProxyJump=none",
        "ControlMaster=no",
        "ControlPath=none",
        f"ConnectTimeout={policy['connectTimeoutSec']}",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
        "LogLevel=ERROR",
    ]
    argv = [policy["sshExecutable"], "-F", "/dev/null", "-T"]
    for option in options:
        argv.extend(("-o", option))
    argv.extend(("--", f"{policy['user']}@{policy['host']}", command))
    return argv


def _ssh_env(policy: dict) -> dict[str, str]:
    return {
        "PATH": "/run/current-system/sw/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
    }


def _run_argv(
    argv: list[str],
    env: dict[str, str],
    timeout_ms: int,
    rec: Optional[_InFlight] = None,
) -> tuple[int, str, str]:
    """Run a prevalidated argv directly; never invoke a local shell."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )

    def signal_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    if rec is not None:
        with _in_flight_lock:
            rec.proc = proc
            cancelled_already = rec.cancelled
        if cancelled_already:
            signal_group(signal.SIGTERM)

    try:
        stdout, stderr = proc.communicate(timeout=max(1, timeout_ms) / 1000.0)
        return proc.returncode, stdout or "", stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

    signal_group(signal.SIGTERM)
    try:
        out, err = proc.communicate(timeout=2.0)
        stdout, stderr = out or stdout, err or stderr
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        try:
            out, err = proc.communicate(timeout=2.0)
            stdout, stderr = out or stdout, err or stderr
        except subprocess.TimeoutExpired:
            pass
    warning = (
        f"[smarthome-ssh] command exceeded timeout of {timeout_ms} ms; the local ssh "
        "client was stopped, but the remote command may still be running."
    )
    return 124, stdout or "", ((stderr or "") + "\n" + warning).strip()


# Prefix-shaped secrets are replaced before output reaches the model.
_SECRET_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("anthropic-api", re.compile(r"sk-ant-(?:api|oat)\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("openai-project", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai-legacy", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"\bgh[psoure]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("slack-token", re.compile(r"\bxox[boaprs]-[A-Za-z0-9\-]{10,}\b")),
    ("aws-access", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("tavily-key", re.compile(r"\btvly-[A-Za-z0-9_\-]{20,}\b")),
    ("age-secret-key", re.compile(r"\bAGE-SECRET-KEY-1[0-9A-Z]{58}\b")),
]
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----"
    r"(?:.*?-----END (?:[A-Z0-9 ]*)PRIVATE KEY(?: BLOCK)?-----|.*\Z)",
    re.DOTALL,
)


def _redact_secrets(body: str) -> str:
    body = _PRIVATE_KEY_RE.sub(
        lambda m: f"[REDACTED:private-key:{len(m.group(0))}chars]", body
    )
    for label, pat in _SECRET_PATTERNS:
        body = pat.sub(
            lambda m, _label=label: f"[REDACTED:{_label}:{len(m.group(0))}chars]",
            body,
        )
    return body


def _compose_output(rc: int, stdout: str, stderr: str, limit: int) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip())
    if stderr:
        parts.append("--- stderr ---\n" + stderr.rstrip())
    parts.append(f"--- exit {rc} ---")
    combined = _redact_secrets("\n".join(parts))
    if len(combined) > limit:
        combined = combined[:limit] + "\n[smarthome-ssh] output truncated"
    return combined


def _tool_ssh(args: dict, rec: Optional[_InFlight] = None) -> str:
    policy = _load_policy(_load_config())
    request = _validate_request(args, policy)
    argv = _build_ssh_argv(request["command"], policy)
    rc, stdout, stderr = _run_argv(argv, _ssh_env(policy), request["timeout"], rec)
    return _compose_output(rc, stdout, stderr, policy["outputLimitChars"])


TOOLS = [
    {
        "name": "ssh",
        "description": (
            "Runs one command on the smarthome home-server over SSH as the pinned "
            "administrator account. The host key is pinned and authentication uses "
            "the existing SSH agent; forwarding and proxies are disabled. The command "
            "is interpreted by the remote login shell. Returns stdout, stderr, and the "
            "exit code. On timeout the local client stops but the remote command may "
            "keep running; use systemd-run or nohup remotely for long jobs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command line to run on home-server.",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in milliseconds (default {DEFAULT_TIMEOUT_MS}).",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    }
]

SERVER_INFO = {"name": "smarthome-ssh", "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}


def _write_msg(msg: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def _respond(msg_id, result=None, error=None) -> None:
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    _write_msg(out)


def _send_response(msg_id, result=None, error=None) -> None:
    """Send an in-flight response, or drop it if the request was cancelled.

    The cancelled check and the write share `_write_lock` and
    `_in_flight_lock` so a late cancel cannot race a spurious response.
    """
    with _write_lock:
        with _in_flight_lock:
            rec = _in_flight.pop(msg_id, None)
            if rec is not None and rec.cancelled:
                return
        out: dict = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def _on_cancelled(req_id) -> None:
    with _in_flight_lock:
        rec = _in_flight.get(req_id)
        if rec is None:
            return
        rec.cancelled = True
        proc = rec.proc
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass


def _dispatch_tools_call(msg_id, params, rec) -> None:
    try:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "ssh":
            _send_response(msg_id, error={"code": -32601, "message": f"Unknown tool: {name}"})
            return
        text = _tool_ssh(arguments, rec)
        _send_response(msg_id, result={"content": [{"type": "text", "text": text}]})
    except Exception as e:
        _send_response(
            msg_id,
            result={"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True},
        )


def _handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

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
        req_id = params.get("requestId")
        if req_id is not None:
            _on_cancelled(req_id)
        return
    if method == "tools/list":
        _respond(msg_id, result={"tools": TOOLS})
        return
    if method == "tools/call":
        if msg_id is None:
            sys.stderr.write("[smarthome-ssh] tools/call missing id; dropping\n")
            sys.stderr.flush()
            return
        with _in_flight_lock:
            already_in_flight = msg_id in _in_flight
            if not already_in_flight:
                rec = _InFlight()
                _in_flight[msg_id] = rec
        if already_in_flight:
            _respond(
                msg_id,
                error={"code": -32600, "message": f"Request id {msg_id} already in flight"},
            )
            return
        threading.Thread(
            target=_dispatch_tools_call,
            args=(msg_id, params, rec),
            daemon=True,
            name=f"smarthome-ssh-tool-{msg_id}",
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
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as e:
            sys.stderr.write(f"[smarthome-ssh] unhandled error: {e}\n")
            sys.stderr.flush()
    _drain_in_flight(timeout_s=30.0)


if __name__ == "__main__":
    main()
