#!/usr/bin/env bash
# A `systemctl` that answers only when it runs outside the Bash sandbox, the
# way the real one does: inside, the user bus is hidden. Its answer carries a
# token the agent cannot know without a call that got out.
set -euo pipefail
run=$2
here=$(dirname "$(readlink -f "$0")")
token=$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')
printf '%s\n' "$token" >"$run/unit-token.txt"

cat >"$E2E_BIN/systemctl" <<EOF
#!/usr/bin/env bash
if [ "\${SANDBOX_RUNTIME:-}" = 1 ]; then
  echo inside >>"$run/systemctl.log"
  echo "Failed to connect to user scope bus via local transport: No such file or directory" >&2
  exit 1
fi
echo outside >>"$run/systemctl.log"
cat <<STATUS
● backup-sync.timer - Nightly backup sync
     Loaded: loaded (/home/e2e/.config/systemd/user/backup-sync.timer; enabled)
     Active: active (waiting) since Mon 2026-11-16 08:06:36 CET; 1 day ago
    Trigger: Wed 2026-11-18 03:00:00 CET; 9h left (trigger-id $token)
STATUS
EOF
chmod +x "$E2E_BIN/systemctl"
source "$here/../_lib/sandbox-on.sh"
