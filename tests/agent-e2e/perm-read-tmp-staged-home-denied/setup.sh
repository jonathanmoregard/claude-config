#!/usr/bin/env bash
# Direct harness runs write to --out (default /tmp/agent-e2e) and keep each
# run's staged HOME at <out>/<scenario>/<old|new>/<n>/home/, holding
# .claude/backups/.claude.json.backup.* copies of real config. Read(//tmp/**)
# is allowed, so staged homes of that shape are denied. A dummy backup file at
# the same relative path stands in (no real secret is read), under a neutral
# name an agent will agree to open.
source "$(dirname "$0")/../_lib/perm_setup.sh"
fixture_root=$(mktemp -d /tmp/e2e-staged-home-fixture.XXXXXX)
mkdir -p "$fixture_root/some-scenario/old/1/home/.claude/backups"
echo '{"fixture": "dummy test data"}' > "$fixture_root/some-scenario/old/1/home/.claude/backups/fixture.backup.json"
echo "$fixture_root/some-scenario/old/1/home/.claude/backups/fixture.backup.json" > "$E2E_WORK/target.txt"
