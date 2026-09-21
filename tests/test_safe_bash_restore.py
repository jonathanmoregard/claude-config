#!/usr/bin/env python3
"""Contract for dormant safe-bash source plus Codex-only Git broker mode."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcps", "safe-bash", "server.py")
CONFIG = os.path.join(ROOT, "mcps", "safe-bash", "bash-config.json")
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "OK " if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" :: {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


settings = load_json(os.path.join(ROOT, "settings.json"))
servers = load_json(os.path.join(ROOT, "mcp-servers.json"))
safe_config = load_json(CONFIG)
ingress_config = load_json(os.path.join(ROOT, "hooks", "bash-guard-config.json"))
permissions = settings.get("permissions") or {}


def master_file(path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"origin/master:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout


print("\n== dormant Claude surface ==")
check(
    "Claude settings are byte-identical to origin/master",
    open(os.path.join(ROOT, "settings.json"), "rb").read() == master_file("settings.json"),
)
check(
    "Claude MCP registry is byte-identical to origin/master",
    open(os.path.join(ROOT, "mcp-servers.json"), "rb").read()
    == master_file("mcp-servers.json"),
)
check("native Bash remains available to Claude", "Bash" in (permissions.get("allow") or []))
check("native Bash is not blanket-denied", "Bash" not in (permissions.get("deny") or []))
check(
    "safe-bash MCP is not granted to Claude",
    "mcp__safe-bash__*" not in (permissions.get("allow") or []),
)

safe_registration = servers.get("safe-bash") or {}
check("safe-bash MCP is not registered for Claude", not safe_registration)

allowed_diff = {
    "mcps/safe-bash/bash-config.json",
    "mcps/safe-bash/server.py",
    "mcps/safe-bash/test_cancellation.py",
    "mcps/safe-bash/test_git_broker.py",
    "mcps/safe-bash/test_safe_bash.py",
    "tests/test_safe_bash_restore.py",
}
changed = set(
    subprocess.run(
        ["git", "diff", "--name-only", "origin/master", "--"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
)
check(
    "branch changes only dormant broker source and tests",
    changed <= allowed_diff,
    f"unexpected={sorted(changed - allowed_diff)}",
)

print("\n== policy parity ==")
expected_rules = [
    rule for rule in (permissions.get("deny") or []) if rule.startswith("Bash(")
]
actual_rules = ((safe_config.get("permissions") or {}).get("deny") or [])
check(
    "dormant safe-bash preserves every Claude Bash deny rule",
    set(expected_rules) <= set(actual_rules),
    f"missing={sorted(set(expected_rules) - set(actual_rules))}",
)
for key in ("untrusted_ingress_single", "untrusted_ingress_multi"):
    check(
        f"safe-bash and output guard share {key}",
        safe_config.get(key) == ingress_config.get(key),
    )


def call_with_dcg(marker: str, dcg_path: str) -> tuple[str, str]:
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "restore-test", "version": "1"},
        },
    }
    ready = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "exec",
            "arguments": {"command": f"printf executed > {marker}"},
        },
    }
    payload = "\n".join(json.dumps(item) for item in (init, ready, call)) + "\n"
    env = dict(os.environ)
    env["DCG_PATH"] = dcg_path
    proc = subprocess.run(
        [sys.executable, SERVER],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    response = ""
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 2:
            response = json.dumps(message)
            break
    return response, proc.stderr


print("\n== fail-closed execution ==")
with tempfile.TemporaryDirectory(prefix="safe-bash-restore-") as scratch:
    marker = os.path.join(scratch, "executed")
    response, stderr = call_with_dcg(marker, os.path.join(scratch, "missing-dcg"))
    check("missing validator returns a tool response", bool(response), stderr[:200])
    check("missing validator does not spawn command", not os.path.exists(marker), response[:240])
    check(
        "missing validator response names broken safety boundary",
        "BROKEN" in response or "broken" in response,
        response[:240],
    )

    dcg_cases = {
        "validator exits nonzero": "#!/bin/sh\nexit 7\n",
        "validator emits malformed output": "#!/bin/sh\nprintf 'not-json\\n'\n",
        "validator emits unknown decision": (
            "#!/bin/sh\nprintf '%s\\n' "
            "'{\"hookSpecificOutput\":{\"permissionDecision\":\"maybe\"}}'\n"
        ),
        "validator asks instead of clearing": (
            "#!/bin/sh\nprintf '%s\\n' "
            "'{\"hookSpecificOutput\":{\"permissionDecision\":\"ask\","
            "\"permissionDecisionReason\":\"indeterminate\"}}'\n"
        ),
        "validator config state is unverifiable": "#!/bin/sh\nexit 0\n",
    }
    for index, (label, program) in enumerate(dcg_cases.items()):
        stub = os.path.join(scratch, f"dcg-{index}")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(program)
        os.chmod(stub, 0o700)
        case_marker = os.path.join(scratch, f"executed-{index}")
        response, stderr = call_with_dcg(case_marker, stub)
        check(f"{label}: returns a tool response", bool(response), stderr[:200])
        check(f"{label}: does not spawn command", not os.path.exists(case_marker), response[:240])
        check(
            f"{label}: response reports refusal",
            "safe-bash" in response and "exit 0" not in response,
            response[:240],
        )

    spec = importlib.util.spec_from_file_location("safe_bash_restore_server", SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    original_config = module.CONFIG_PATH
    try:
        config_cases = (
            ("missing policy config", os.path.join(scratch, "missing-config.json")),
            ("malformed policy config", os.path.join(scratch, "malformed-config.json")),
            ("empty policy config", os.path.join(scratch, "empty-config.json")),
        )
        with open(config_cases[1][1], "w", encoding="utf-8") as handle:
            handle.write("{")
        with open(config_cases[2][1], "w", encoding="utf-8") as handle:
            handle.write("{}")
        for label, config_path in config_cases:
            module.CONFIG_PATH = config_path
            config_marker = os.path.join(scratch, label.replace(" ", "-"))
            try:
                result = module._tool_exec({"command": f"printf executed > {config_marker}"})
            except RuntimeError as exc:
                result = str(exc)
            check(f"{label}: does not spawn command", not os.path.exists(config_marker), result[:240])
            check(f"{label}: reports broken policy", "config" in result.lower(), result[:240])
    finally:
        module.CONFIG_PATH = original_config


print("\n== secret redaction parity ==")
private_key_token = "PRIVATE " + "KEY"
private_key_cases = {
    "complete private key": (
        f"prefix\n-----BEGIN OPENSSH {private_key_token}-----\nFAKEKEYBODY\n"
        f"-----END OPENSSH {private_key_token}-----\nsuffix",
        "FAKEKEYBODY",
    ),
    "truncated private key tail": (
        f"prefix\n-----BEGIN {private_key_token}-----\nFAKEORPHANBEGIN",
        "FAKEORPHANBEGIN",
    ),
    "truncated private key head": (
        f"FAKEORPHANEND\n-----END RSA {private_key_token}-----\nsuffix",
        "FAKEORPHANEND",
    ),
}
for label, (body, secret_part) in private_key_cases.items():
    redacted = module._redact_secrets(body)
    check(f"{label}: leaves private-key marker", "[REDACTED:private-key:" in redacted, redacted)
    check(f"{label}: key body is absent", secret_part not in redacted, redacted)


print("\n== Summary ==")
if FAILURES:
    print(f"FAIL: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
    raise SystemExit(1)
print("All checks passed.")
