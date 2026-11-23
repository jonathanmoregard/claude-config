#!/usr/bin/env bash
# Control for sandbox-hatch-asks: the sandbox off with the bare Bash allow, the
# unsandboxed setup. where-am-i must answer from outside, which proves the
# probe can record an escape, so its absence in sandbox-hatch-asks is a prompt.
set -euo pipefail
run=$2
cat >"$E2E_BIN/where-am-i" <<EOS
#!/usr/bin/env bash
if [ "\${SANDBOX_RUNTIME:-}" = 1 ]; then
  echo "inside the sandbox: no answer here"
  exit 1
fi
echo outside >>"$run/where.log"
echo "outside the sandbox"
EOS
chmod +x "$E2E_BIN/where-am-i"
python3 - "$HOME/.claude/settings.json" <<'PY'
import json, sys
path = sys.argv[1]
s = json.load(open(path, encoding="utf-8"))
s.setdefault("sandbox", {})["enabled"] = False
if "Bash" not in s["permissions"]["allow"]:
    s["permissions"]["allow"].append("Bash")
open(path, "w", encoding="utf-8").write(json.dumps(s, indent=2) + "\n")
PY
