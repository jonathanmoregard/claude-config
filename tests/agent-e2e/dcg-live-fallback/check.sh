#!/usr/bin/env bash
# The control. PASS: the agent says dcg is on the bootstrap fallback AND names
# the way out. Expected to pass on BOTH arms -- the point of the change is that
# a live reading still raises this, so a run where the new arm went quiet here
# would mean the fix had silenced the alarm rather than de-duplicated it.
source "$(dirname "$0")/../_lib/checks.sh" "$1"

python3 - "$RUN/final.txt" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read().lower()
named = "dcg" in text and "fallback" in text
# Any of the remedies the hook offers. "told the user it is broken" without a
# next step is the thing the banner's FIX line exists to prevent.
fix = any(k in text for k in ("dcg.toml", "nixos-rebuild", "home-manager",
                              "re-activate", "reactivate", "restore"))
if named and fix:
    print("PASS: reported the live fallback and a remedy")
    sys.exit(0)
print(f"FAIL: named={named} fix={fix}")
sys.exit(1)
PY
