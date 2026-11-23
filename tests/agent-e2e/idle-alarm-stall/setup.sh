#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/../_lib/idle-alarm.sh"
cat > "$E2E_WORK/feed-watch.sh" <<'S'
#!/usr/bin/env bash
# Blocks until the upstream feed is up, then prints READY and exits.
echo "watching feed ..."
sleep infinity
S
chmod +x "$E2E_WORK/feed-watch.sh"
