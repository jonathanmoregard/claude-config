#!/usr/bin/env python3
"""Register a stub as an MCP server in $E2E_RUN/mcp.json, via the fixed dev-MCP dispatcher.

    mcp_stub.py NAME STUB [--env K=V]... [--timeout MS] [-- ARG...]

Other servers already in mcp.json are kept. The dispatcher is the served
checkout's (the allowlisted path) when it exists, else this checkout's own:
the served one only carries it once the dispatcher has merged.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib

PYTHON = "/run/current-system/sw/bin/python3"
SERVED = "/var/lib/claude-runtime/checkout/runtime/scripts/dev-mcp/dispatch.py"
POLICY = pathlib.Path("/etc/claude-code/managed-settings.d/50-mcp.json")
LOCAL = pathlib.Path(__file__).resolve().parents[3] / "runtime/scripts/dev-mcp/dispatch.py"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("stub")
    ap.add_argument("--env", action="append", default=[], metavar="K=V")
    ap.add_argument("--timeout", type=int)
    ap.add_argument("args", nargs="*")
    opts = ap.parse_args()

    run = pathlib.Path(os.environ["E2E_RUN"])
    served = pathlib.Path(os.environ.get("DEV_MCP_DISPATCH_SERVED", SERVED))
    env = dict(kv.split("=", 1) for kv in opts.env)
    env.update(DEV_MCP_PROFILE="e2e-stub", E2E_RUN=str(run), E2E_MCP_STUB=opts.stub)
    if opts.args:
        env["E2E_MCP_STUB_ARGS"] = json.dumps(opts.args)
    if not served.is_file() and POLICY.is_file():
        raise SystemExit(f"{POLICY} exists but the served dispatcher {served} does not; "
                         "the local one would never match the allowlist")
    entry = {"type": "stdio", "command": PYTHON,
             "args": [str(served if served.is_file() else LOCAL)], "env": env}
    if opts.timeout is not None:
        entry["timeout"] = opts.timeout

    path = run / "mcp.json"
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    cfg.setdefault("mcpServers", {})[opts.name] = entry
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
