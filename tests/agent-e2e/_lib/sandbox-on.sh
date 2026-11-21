#!/usr/bin/env bash
# Turns the Bash sandbox on in the staged config. Sourced by setup.sh.
python3 - "$HOME/.claude/settings.json" <<'PY'
import json, sys
path = sys.argv[1]
settings = json.load(open(path, encoding="utf-8"))
settings.setdefault("sandbox", {})["enabled"] = True
open(path, "w", encoding="utf-8").write(json.dumps(settings, indent=2) + "\n")
PY
