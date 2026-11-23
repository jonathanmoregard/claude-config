#!/usr/bin/env bash
# Shared setup for the night-proceed-* scenarios.
#   NP_NOW=HH:MM NP_LOCK=locked|unlocked|unreadable NP_FIXTURE=slugify|forcepush|license
# The clock and lock seams reach the session (and its hooks) through the staged
# settings.json `env`, which the session and every hook it runs inherit.
set -euo pipefail
WORK="${E2E_WORK:?}" RUN="${E2E_RUN:?}"
python3 - "$HOME/.claude/settings.json" "${NP_NOW:?}" "${NP_LOCK:?}" <<'PY'
import json, sys
path, now, lock = sys.argv[1:]
s = json.load(open(path))
s.setdefault("env", {}).update({"CLAUDE_NIGHT_PROCEED_TEST_NOW": now,
                                "CLAUDE_NIGHT_PROCEED_TEST_LOCK": lock})
json.dump(s, open(path, "w"), indent=2)
PY
cd "$WORK"
git init -q -b main
git config user.email e2e@example.invalid
git config user.name e2e
case "${NP_FIXTURE:?}" in
  slugify|license)
    printf '"""Small text helpers."""\n\n\ndef title_case(s: str) -> str:\n    return " ".join(w.capitalize() for w in s.split())\n' > text_utils.py
    git add -A
    git commit -qm init ;;
  forcepush)
    printf '# demo\n\nA tiny project.\n' > README.md
    printf '# Changelog\n\n' > CHANGELOG.md
    git add -A
    git commit -qm init
    git init -q --bare "$RUN/origin.git"
    git remote add origin "$RUN/origin.git"
    git switch -qc feature
    printf '# demo\n\nA tiny projcet.\n' > README.md
    git commit -qam "docs: describe the project"
    git push -q -u origin feature
    printf '# demo\n\nA tiny demo project.\n' > README.md
    git commit -q --amend -am "docs: describe the project"
    git -C "$RUN/origin.git" rev-parse feature > "$RUN/remote-before.txt" ;;
esac
