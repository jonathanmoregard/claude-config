#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/../_lib/idle-alarm.sh"
cat > "$E2E_WORK/feed-watch.sh" <<'S'
#!/usr/bin/env bash
# Watches the upstream feed and prints READY once the feed is up.
echo "watching feed ..."
sleep infinity
S
chmod +x "$E2E_WORK/feed-watch.sh"
