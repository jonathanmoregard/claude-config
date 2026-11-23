#!/usr/bin/env bash
set -euo pipefail
bash "$(dirname "$0")/../_lib/idle-alarm.sh"
# The e2e sandbox has no user bus, so a real unit cannot run there. The fake
# systemd runs the job as a real process and reports it the way systemd
# reports a RemainAfterExit transient unit.
for name in systemd-run systemctl; do
  cp "$(dirname "$0")/../_lib/fake-systemd.sh" "$E2E_BIN/$name"
  chmod +x "$E2E_BIN/$name"
done
mkdir -p "$E2E_RUN/fake-systemd"
python3 - "$HOME/.claude/settings.json" "$E2E_RUN/fake-systemd" <<'PY'
import json, sys
path, d = sys.argv[1:]
s = json.load(open(path))
s.setdefault("env", {})["FAKE_SYSTEMD_DIR"] = d
json.dump(s, open(path, "w"), indent=2)
PY
cat > "$E2E_WORK/refresh.sh" <<'S'
#!/usr/bin/env bash
# Data refresh: about three minutes. Prints DONE at the end.
sleep 180
echo DONE
S
chmod +x "$E2E_WORK/refresh.sh"
printf 'idle-alarm-e2e-%s-%s\n' "$RANDOM" "$$" > "$E2E_WORK/job-slug.txt"
