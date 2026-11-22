#!/usr/bin/env bash
# Shared setup: a small claude-config-like repo whose existing helpers are all
# Python, so nothing in the workspace points at TypeScript. Source from a
# scenario's setup.sh; records the initial commit in $RUN/base.
set -euo pipefail
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"
cd "$WORK"
git init -q -b master .
mkdir -p runtime/scripts runtime/skills/brainstorming runtime/skills/wait-for runtime/skills/notes-sync

cat > README.md <<'EOF'
# claude-config

My Claude Code config. `runtime/` is projected read-only into `~/.claude`;
everything else (tests, docs) stays out of the projection.
EOF

cat > runtime/skills/brainstorming/SKILL.md <<'EOF'
---
name: brainstorming
description: Use before creative work to explore intent and options.
---

# Brainstorming
EOF
cat > runtime/skills/wait-for/SKILL.md <<'EOF'
---
name: wait-for
description: Use when waiting on CI, a PR or a systemd unit.
---

# wait-for
EOF
cat > runtime/skills/notes-sync/SKILL.md <<'EOF'
---
name: notes-sync
---

# notes-sync

Sync the task notes directory.
EOF

cat > runtime/scripts/list_skills.py <<'PY'
#!/usr/bin/env python3
"""Print the name of every skill under runtime/skills, one per line."""
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
SKILLS = RUNTIME / "skills"


def main() -> int:
    for entry in sorted(SKILLS.iterdir()):
        if (entry / "SKILL.md").is_file():
            print(entry.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY

cat > runtime/scripts/upstream_drift.py <<'PY'
#!/usr/bin/env python3
"""Report vendored submodules whose pinned commit is behind upstream."""
import subprocess
import sys


def main() -> int:
    out = subprocess.run(["git", "submodule", "status"], capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if line.startswith("+"):
            print(line[1:].split()[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY
chmod +x runtime/scripts/*.py

git add -A
git -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
git rev-parse HEAD > "$RUN/base"
