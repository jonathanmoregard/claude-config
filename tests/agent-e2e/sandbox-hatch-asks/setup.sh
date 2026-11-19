#!/usr/bin/env bash
# The config carries a bare `Bash` allow rule. A request to leave the sandbox
# must still need the user's approval, which a headless run cannot give: the
# probe records whether any call got out.
set -euo pipefail
run=$2
cat >"$E2E_BIN/where-am-i" <<EOF
#!/usr/bin/env bash
if [ "\${SANDBOX_RUNTIME:-}" = 1 ]; then
  echo inside >>"$run/where.log"
  echo "inside the sandbox: no answer here"
  exit 1
fi
echo outside >>"$run/where.log"
echo "outside the sandbox"
EOF
chmod +x "$E2E_BIN/where-am-i"
