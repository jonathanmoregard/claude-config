#!/usr/bin/env python3
"""The one fixed command every dev MCP server is registered under.

A managed `allowedMcpServers` policy matches a server's exact argv, and dev
servers (agent-e2e stubs, a checkout's mock server) live at paths that vary
per run or per worktree, so their own argv cannot be allowlisted. This
script's argv can:

    /run/current-system/sw/bin/python3 \
        /var/lib/claude-runtime/checkout/runtime/scripts/dev-mcp/dispatch.py

The entry's `env`, which the policy does not compare, picks a row of PROFILES
with DEV_MCP_PROFILE; each row builds exactly one argv shape and execs it with
stdio untouched. A new argv shape is a new row, through a reviewed PR.
`e2e-stub` runs whatever stdlib-Python script the test names (git-broker's
scenario even runs the real broker that way): test code, reviewed with the
test, not by this row.

  e2e-stub  E2E_RUN, E2E_MCP_STUB (a script under E2E_RUN), optional
            E2E_MCP_STUB_ARGS (JSON list of strings) ->
            PYTHON <stub> <args...>
  llmux     DEV_MCP_REPO (GDOCS_REPO or a git worktree of it), DEV_MCP_TOOLS
            (names, space-separated or a JSON list) ->
            <repo or GDOCS_REPO>/.venv/bin/python <repo>/mockdocs/serve.py
                --transport stdio --single-user --tools <tools...>

The path rules keep intent legible; they are not a security boundary.
E2E_RUN is caller-chosen; the containment check catches mistakes, not intent.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from typing import Callable, Mapping

PYTHON = "/run/current-system/sw/bin/python3"
GDOCS_REPO = "/home/jonathan/Repos/gdocs-review-mcp"
TOOL_NAME = re.compile(r"[A-Za-z0-9_]+")


class Refused(Exception):
    pass


def need(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise Refused(f"{key} is not set")
    return value


def e2e_stub(env: Mapping[str, str]) -> list[str]:
    run_dir = pathlib.Path(need(env, "E2E_RUN")).resolve()
    target = need(env, "E2E_MCP_STUB")
    stub = pathlib.Path(target).resolve()
    if not stub.is_relative_to(run_dir):
        raise Refused(f"{target} is outside the run directory {run_dir}")
    if not stub.is_file():
        raise Refused(f"{target} is not a file")
    try:
        args = json.loads(env.get("E2E_MCP_STUB_ARGS") or "[]")
    except ValueError:
        args = None
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise Refused("E2E_MCP_STUB_ARGS must be a JSON list of strings")
    return [PYTHON, str(stub), *args]


def common_git_dir(repo: pathlib.Path) -> pathlib.Path | None:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git.resolve()
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir: "):
        return None
    admin = (repo / line[len("gitdir: "):]).resolve()
    try:
        common = (admin / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return (admin / common).resolve()


def tool_names(raw: str) -> list[str]:
    try:
        tools = json.loads(raw) if raw.lstrip().startswith("[") else raw.split()
    except ValueError:
        tools = None
    if (not isinstance(tools, list) or not tools
            or not all(isinstance(t, str) and TOOL_NAME.fullmatch(t) for t in tools)):
        raise Refused("DEV_MCP_TOOLS must name tools (letters, digits, _), "
                      "space-separated or as a JSON list")
    return tools


def llmux(env: Mapping[str, str]) -> list[str]:
    given = need(env, "DEV_MCP_REPO")
    repo = pathlib.Path(given).resolve()
    home = pathlib.Path(GDOCS_REPO).resolve()
    if repo != home and common_git_dir(repo) != home / ".git":
        raise Refused(f"DEV_MCP_REPO {given} is not {GDOCS_REPO} or a worktree of it")
    tools = tool_names(env.get("DEV_MCP_TOOLS") or "")
    serve = repo / "mockdocs/serve.py"
    # Worktrees carry no venv of their own; borrow the main checkout's.
    python = next((p for p in (repo / ".venv/bin/python", home / ".venv/bin/python")
                   if p.is_file()), repo / ".venv/bin/python")
    for path in (python, serve):
        if not path.is_file():
            raise Refused(f"{path} is not a file")
    return [str(python), str(serve), "--transport", "stdio", "--single-user",
            "--tools", *tools]


PROFILES: dict[str, Callable[[Mapping[str, str]], list[str]]] = {
    "e2e-stub": e2e_stub,
    "llmux": llmux,
}


def resolve(env: Mapping[str, str]) -> list[str]:
    name = need(env, "DEV_MCP_PROFILE")
    if name not in PROFILES:
        raise Refused(f"unknown profile {name!r}; known: {', '.join(sorted(PROFILES))}")
    return PROFILES[name](env)


def main() -> None:
    try:
        argv = resolve(os.environ)
    except Refused as exc:
        print(f"dev-mcp dispatch: {exc}", file=sys.stderr)
        raise SystemExit(2)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
