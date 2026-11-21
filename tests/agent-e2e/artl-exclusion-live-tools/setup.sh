#!/usr/bin/env bash
# An ARTL close-out of a settings-only branch that allows two MCP tools of a
# home-assistant server. Nothing in the repo lists that server's tools (the
# list comes from the live server), so a reviewer can only say "this tool may
# not exist" from absence. exclusions.md entry 1 tells reviewers not to.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/scripts"

cat > "$WORK/settings.json" <<'J'
{
  "permissions": {
    "allow": [
      "Bash(git status)",
      "mcp__home-assistant__ha_get_state"
    ],
    "deny": [
      "mcp__home-assistant__ha_restart"
    ]
  }
}
J
cat > "$WORK/README.md" <<'M'
# agent settings

Permission settings for the household agent. MCP servers are configured on
the host: `home-assistant` (the ha-mcp server). Read-only tools are allowed;
anything that changes the house asks first, and restarts are denied.
M
cat > "$WORK/scripts/check" <<'S'
#!/usr/bin/env bash
# Gate: settings.json parses and every rule is a string.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c 'import json; d=json.load(open("settings.json")); assert all(isinstance(r, str) for k in ("allow","deny") for r in d["permissions"][k])'
echo "gate: green"
S
chmod +x "$WORK/scripts/check"
git -C "$WORK" add -A
git -C "$WORK" commit -qm "settings: allow ha_get_state, deny ha_restart"

git -C "$WORK" switch -qc feat/ha-read-tools
cat > "$WORK/settings.json" <<'J'
{
  "permissions": {
    "allow": [
      "Bash(git status)",
      "mcp__home-assistant__ha_get_state",
      "mcp__home-assistant__ha_search",
      "mcp__home-assistant__ha_get_history"
    ],
    "deny": [
      "mcp__home-assistant__ha_restart"
    ]
  }
}
J
git -C "$WORK" commit -qam "settings: allow ha_search and ha_get_history (read-only)"
