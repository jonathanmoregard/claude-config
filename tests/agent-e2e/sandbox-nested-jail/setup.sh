#!/usr/bin/env bash
# A script that needs a sandbox of its own, the way the agent-e2e harness and a
# nested `claude` do. Claude's Bash sandbox refuses nested namespaces, so the
# only prompt-free way to run it is through scripts/confined.
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
work=$1
token=$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')
printf '%s\n' "$token" >"$2/nested-token.txt"
cat >"$work/nested.sh" <<EOF
#!/usr/bin/env bash
bwrap --ro-bind / / --dev /dev true || exit 1
echo "nested=ok confined=\${SANDBOX_RUNTIME:-0}" >>"\$(dirname "\$0")/nested.log"
echo "inner sandbox started, run id $token"
EOF
chmod +x "$work/nested.sh"
source "$here/../_lib/sandbox-on.sh"
