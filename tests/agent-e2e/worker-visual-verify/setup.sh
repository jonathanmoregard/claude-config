#!/usr/bin/env bash
# A rendered-view template. Writing it fires the visual-verify PostToolUse
# nudge inside the worker, which must still reach the skill's instructions.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/templates"
cat > "$WORK/templates/orders.html" <<'H'
<!doctype html>
<html><head><title>Shop</title></head>
<body><main><h1>Orders</h1><p>No orders yet.</p></main></body></html>
H
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
