"""Stand-in for github-app-mcp-server in the github-mux tests.

A tiny stdio MCP server built on the same SDK. Every tool echoes which child
answered and what it was called with, so a test can see where the mux routed a
call. Tool `crash` kills the process mid-call; `--fail-start` exits before the
MCP handshake.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def build_server(
    label: str, tool_names: list[str], instructions: str, no_repo_search: bool = False
) -> Server:
    server: Server = Server(f"fake-{label}", instructions=instructions)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=name, description=f"fake {name}", inputSchema={"type": "object"})
            for name in tool_names
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        if name == "crash":
            os._exit(3)
        if name == "search_repositories" and no_repo_search:
            # What a repository the installation cannot see looks like.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="not found")], isError=True
            )
        if name == "search_repositories":
            # The mux asks for a repository's default branch this way. Echoing
            # the call back would leave it unresolvable, and the base rule would
            # then fail open in every test, so answer it in GitHub's shape.
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "total_count": 1,
                                "items": [{"full_name": "fake/repo", "default_branch": "main"}],
                            }
                        ),
                    )
                ]
            )
        payload = {"label": label, "tool": name, "arguments": arguments}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))]
        )

    return server


async def serve(server: Server) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="personal")
    parser.add_argument("--tools", required=True)
    parser.add_argument("--extra-tools", default="")
    parser.add_argument("--no-repo-search", action="store_true")
    parser.add_argument("--instructions", default="")
    parser.add_argument("--fail-start", action="store_true")
    parser.add_argument("--start-delay", type=float, default=0.0)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--exit-file", type=Path)
    args = parser.parse_args(argv)
    if args.pid_file:
        args.pid_file.write_text(str(os.getpid()), encoding="utf-8")
    if args.fail_start:
        print("fake child: refusing to start", file=sys.stderr)
        return 1
    # Stall before the MCP handshake, like a wrapper stuck minting a token.
    time.sleep(args.start_delay)
    names = [n for n in (args.tools + "," + args.extra_tools).split(",") if n]
    anyio.run(
        serve, build_server(args.label, names, args.instructions, args.no_repo_search)
    )
    # Reached only on a graceful MCP stdio shutdown (stdin closed), not a kill.
    if args.exit_file:
        args.exit_file.write_text("clean", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
