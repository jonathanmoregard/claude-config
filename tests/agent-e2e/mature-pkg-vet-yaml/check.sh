#!/usr/bin/env bash
# PASS: Cargo.toml depends on a YAML crate that is neither deprecated nor
# flagged by RustSec. FAIL: it depends on serde_yaml (deprecated upstream),
# serde_yml (RUSTSEC-2025-0068, unsound + unmaintained) or yaml-rust
# (RUSTSEC-2024-0320, unmaintained).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
deps=$(python3 - "$RUN/work/Cargo.toml" <<'PY'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
deps = c.get("dependencies", {})
# a renamed dependency (`key = { package = "real" }`) counts as the real crate
print(" ".join(sorted(v.get("package", k) if isinstance(v, dict) else k
                      for k, v in deps.items())))
PY
) || { echo "INCONCLUSIVE: Cargo.toml unreadable"; exit 2; }
for bad in serde_yaml serde_yml yaml-rust; do
  [[ " $deps " == *" $bad "* ]] && fail "depends on $bad (deps: $deps)"
done
yaml=$(tr ' ' '\n' <<<"$deps" | grep -iE 'yaml|yml|saphyr|norway' | head -1)
[ -n "$yaml" ] && pass "YAML crate: $yaml"
echo "INCONCLUSIVE: no YAML crate in deps ($deps)"; exit 2
