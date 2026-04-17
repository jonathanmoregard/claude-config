#!/bin/bash
# Bootstraps token-optimizer plugin registration + cron entry.
# Idempotent: safe to re-run.
#
# Preconditions:
#   - ~/Repos/token-optimizer/.claude-plugin/plugin.json exists (clone fork first)
#   - jq installed
#   - ~/.claude/settings.json already references the marketplace + plugin
#     (committed in this repo)
set -euo pipefail

REPO="$HOME/Repos/token-optimizer"
DATA_DIR="$HOME/.claude/token-optimizer"
PLUGINS_DIR="$HOME/.claude/plugins"
MARKETPLACE="alexgreensh-token-optimizer"
PLUGIN_KEY="token-optimizer@${MARKETPLACE}"
WORKER="$HOME/.claude/workers/token-optimizer-updater/run.sh"

if [ ! -d "$REPO/.claude-plugin" ]; then
  echo "ERROR: $REPO/.claude-plugin missing. Clone the fork first:" >&2
  echo "  git clone https://github.com/jonathanmoregard/token-optimizer.git $REPO" >&2
  echo "  git -C $REPO remote add upstream https://github.com/alexgreensh/token-optimizer.git" >&2
  echo "  git -C $REPO fetch upstream" >&2
  exit 1
fi

command -v jq >/dev/null || { echo "ERROR: jq required" >&2; exit 1; }

mkdir -p "$DATA_DIR" "$PLUGINS_DIR"

# --- known_marketplaces.json ---
KM="$PLUGINS_DIR/known_marketplaces.json"
[ -f "$KM" ] || echo '{}' > "$KM"
NOW=$(date -u +%FT%TZ)
jq --arg name "$MARKETPLACE" \
   --arg path "$REPO/.claude-plugin" \
   --arg ts "$NOW" \
   '.[$name] = {
     "source": {"source":"directory","path":$path},
     "installLocation": $path,
     "lastUpdated": $ts
   }' "$KM" > "$KM.tmp" && mv "$KM.tmp" "$KM"

# --- installed_plugins.json ---
IP="$PLUGINS_DIR/installed_plugins.json"
[ -f "$IP" ] || echo '{"version":1,"plugins":{}}' > "$IP"
VERSION=$(jq -r '.version // "unknown"' "$REPO/.claude-plugin/plugin.json" 2>/dev/null || echo "unknown")
SHA=$(git -C "$REPO" rev-parse HEAD)
jq --arg key "$PLUGIN_KEY" \
   --arg path "$REPO" \
   --arg ver "$VERSION" \
   --arg sha "$SHA" \
   --arg ts "$NOW" \
   '.plugins[$key] = [{
     "scope":"user",
     "installPath":$path,
     "version":$ver,
     "installedAt":$ts,
     "lastUpdated":$ts,
     "gitCommitSha":$sha
   }]' "$IP" > "$IP.tmp" && mv "$IP.tmp" "$IP"

# --- cron entry ---
if ! crontab -l 2>/dev/null | grep -q 'token-optimizer-auto-update'; then
  (crontab -l 2>/dev/null; echo "0 */6 * * * $WORKER # token-optimizer-auto-update") | crontab -
  echo "Installed cron entry."
fi

echo "Registration complete:"
echo "  marketplace: $MARKETPLACE -> $REPO/.claude-plugin"
echo "  plugin: $PLUGIN_KEY @ $SHA"
echo "  data dir: $DATA_DIR"
