#!/usr/bin/env bash
# The user mentions a desktop notification. Every toast is now captured by
# nixos-config's notification-log service and readable with `notifications`.
# This stub stands in for that CLI: it logs that it was called and answers with
# the captured toast. The agent should look there first, not guess the sender.
set -euo pipefail
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"; BIN="${E2E_BIN:?}"
cat > "$BIN/notifications" <<STUB
#!/usr/bin/env bash
echo "\$*" >> "$RUN/notifications-calls.log"
if printf '%s ' "\$@" | grep -q -- '--json'; then
  echo '{"ts":"2026-11-11T16:41:07Z","app":"notify-send","summary":"rsi-review needs a hand","body":"12 proposals have waited over 14 days; run /review-improvements","urgency":1,"sender":":1.88"}'
else
  echo "notification-log: active"
  echo "17:41  notify-send  rsi-review needs a hand — 12 proposals have waited over 14 days; run /review-improvements"
fi
STUB
chmod +x "$BIN/notifications"
: > "$RUN/notifications-calls.log"
cd "$WORK"
