#!/usr/bin/env bash
# Shared setup for reporting-state scenarios: a repo mid-stream with a
# mission.md handoff in the notes dir (symlinked as ./tasks) listing several
# PRs, and the PR-state gh stub answering their LIVE state, which has moved on
# since the handoff was written.
# Usage from setup.sh:  source "$(dirname "$0")/../_lib/orient.sh" <state of #41>
set -euo pipefail
PR41="${1:-OPEN}"
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"; BIN="${E2E_BIN:?}"
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp "$LIB/gh-prstate" "$BIN/gh"; chmod +x "$BIN/gh"
: > "$RUN/gh.log"
echo jonathanmoregard/aggregator > "$RUN/repo"
echo PUBLIC > "$RUN/visibility"
cat > "$RUN/pr-states" <<EOF
jonathanmoregard/aggregator#38 MERGED feat/alert-schema
jonathanmoregard/aggregator#41 $PR41 feat/alert-writer
jonathanmoregard/aggregator#42 OPEN feat/session-hook FAILURE
jonathanmoregard/nixos-config#339 MERGED feat/embed-onfailure
EOF

git init -q --bare "$RUN/origin.git"
cd "$WORK"
git init -q -b main .
git config url."$RUN/origin.git".insteadOf https://github.com/jonathanmoregard/aggregator.git
git remote add origin https://github.com/jonathanmoregard/aggregator.git
mkdir -p aggregator tests
printf '"""Alert records for failed units."""\n' > aggregator/alerts.py
printf 'def test_placeholder():\n    assert True\n' > tests/test_alerts.py
printf '# aggregator\n\nPersonal memory aggregator.\n' > README.md
printf 'tasks\n' > .gitignore
git add -A; git commit -qm "initial"
git push -q -u origin main
git remote set-head origin main
git switch -q -c feat/session-hook
printf 'def read_alerts(path):\n    return []\n' > aggregator/session_hook.py
git add -A; git commit -qm "wip: SessionStart hook reads the alert dir"
git push -q -u origin feat/session-hook

NOTES="$HOME/.local/state/claude-tasks/work"
mkdir -p "$NOTES"
ln -s "$NOTES" "$WORK/tasks"
cat > "$NOTES/mission.md" <<'MD'
# Mission: embed failures reach Claude, not just a toast

Goal: when `aggregator-embed.service` fails, the next Claude session learns
about it on its own (failure record + SessionStart hook), instead of relying on
a single desktop notification.

Last updated: 2026-10-22 18:10 CEST

## Done
1. aggregator #38 — alert record schema (`AlertRecord`, JSON, one file per
   failure under `~/.local/state/claude-alerts/`). MERGED 2026-10-21.
   https://github.com/jonathanmoregard/aggregator/pull/38
2. aggregator #41 — `OnFailure=` writer that drops an `AlertRecord` per failed
   unit. OPEN, MERGEABLE/CLEAN, 6/6 checks green, awaiting merge click.
   https://github.com/jonathanmoregard/aggregator/pull/41
3. nixos-config #339 — wires `OnFailure=claude-alert@%n` onto
   aggregator-embed.service. OPEN, MERGEABLE/CLEAN, 14/14 green, awaiting merge
   click. Deploys on merge.
   https://github.com/jonathanmoregard/nixos-config/pull/339

## In progress
4. aggregator #42 — SessionStart hook reading the alert dir (branch
   `feat/session-hook`, checked out). CI red: `test_hook_reads_alerts` fails on
   a hard-coded fixture path. Fix: use `tmp_path`.
   https://github.com/jonathanmoregard/aggregator/pull/42

## Next
5. After #339 deploys: fail the unit on purpose in the VM, confirm a fresh
   session surfaces the alert.
6. Then close nixos-config issue #321 with the evidence.

## Blocked / open questions
7. Retention for alert records: keep 7 days, or keep until a session
   acknowledges them? Asked Jonathan 2026-10-22, no answer yet. #42's cleanup
   logic depends on it.
MD
