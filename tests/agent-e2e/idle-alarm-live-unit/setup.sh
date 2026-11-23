#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/../_lib/idle-alarm.sh"
cat > "$E2E_WORK/refresh.sh" <<'S'
#!/usr/bin/env bash
# Data refresh: about three minutes. Prints DONE at the end.
sleep 180
echo DONE
S
chmod +x "$E2E_WORK/refresh.sh"
printf 'idle-alarm-e2e-%s-%s\n' "$RANDOM" "$$" > "$E2E_WORK/job-slug.txt"
