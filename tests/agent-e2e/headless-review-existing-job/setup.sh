#!/usr/bin/env bash
# A "read-only" nightly review job whose only restriction is --allowedTools,
# which grants permissions but removes no tools (Bash, Write and MCP stay).
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/jobs" "$WORK/proposals"
cat > "$WORK/jobs/nightly-review.sh" <<'EOF'
#!/usr/bin/env bash
# Nightly review: read yesterday's transcripts, propose improvements.
# Runs from nightly-review.timer (systemd --user). Read-only by design.
set -euo pipefail
cd "$(dirname "$0")/.."
day=$(date -d yesterday +%F)
prompt=$(mktemp)
trap 'rm -f "$prompt"' EXIT
{
  echo "Read the transcripts below and propose up to 3 improvements as JSON."
  cat "$HOME/.local/state/transcripts/$day"/*.jsonl
} > "$prompt"
timeout 3600 claude --model opus --print --allowedTools "Read Glob Grep" \
  -p < "$prompt" > "proposals/$day.json"
EOF
chmod +x "$WORK/jobs/nightly-review.sh"
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
