#!/usr/bin/env bash
# Registers the arm's own artl_verdict MCP server (runtime/mcps/artl-verdict),
# through which the advisor submits its verdict, in $E2E_RUN/mcp.json -- as a
# live session has it (nixos-config registers it on the host). Source from a
# scenario's setup.sh:
#     source "$(dirname "$0")/../_lib/artl-verdict-mcp.sh"
# The harness loads only RUN/mcp.json (--strict-mcp-config), and the dev-MCP
# dispatcher runs only a script under E2E_RUN, so the arm's server, validator
# and schema are copied there in their relative layout. An arm without the
# server (an older config) registers nothing: its advisor replies in text.
artl_verdict_src="$HOME/.claude"
if [ -f "$artl_verdict_src/mcps/artl-verdict/server.py" ]; then
  artl_verdict_dst="${E2E_RUN:?}/artl-verdict-runtime"
  mkdir -p "$artl_verdict_dst/mcps/artl-verdict" "$artl_verdict_dst/hooks" \
    "$artl_verdict_dst/skills/advice-refine-test-loop"
  cp "$artl_verdict_src/mcps/artl-verdict/server.py" "$artl_verdict_dst/mcps/artl-verdict/"
  cp "$artl_verdict_src/hooks/artl_verdict.py" "$artl_verdict_dst/hooks/"
  cp "$artl_verdict_src/skills/advice-refine-test-loop/verdict-schema.json" \
    "$artl_verdict_dst/skills/advice-refine-test-loop/"
  python3 "$(dirname "${BASH_SOURCE[0]}")/mcp_stub.py" artl_verdict \
    "$artl_verdict_dst/mcps/artl-verdict/server.py"
fi
