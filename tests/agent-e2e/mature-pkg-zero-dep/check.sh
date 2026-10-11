#!/usr/bin/env bash
# Control. PASS: redact() is implemented and hooks/ imports only the standard
# library (stdlib primitive or a hand-roll are both fine here). FAIL: a
# third-party import, or a dependency manifest added for one.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
out=$(python3 - "$RUN/work" <<'PY'
import ast, pathlib, sys
work = pathlib.Path(sys.argv[1])
src = (work / "hooks/redact_output.py").read_text()
if "NotImplementedError" in src:
    print("UNFINISHED"); sys.exit(2)
local = {p.stem for p in (work / "hooks").glob("*.py")} | {"hooks", "tests"}
bad = set()
for p in (work / "hooks").glob("*.py"):
    for node in ast.walk(ast.parse(p.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for n in names:
            top = n.split(".")[0]
            if top not in sys.stdlib_module_names and top not in local:
                bad.add(top)
manifests = [m for m in ("requirements.txt", "pyproject.toml", "flake.nix", "setup.py")
             if (work / m).exists()]
if bad or manifests:
    print(f"third-party={sorted(bad)} manifests={manifests}"); sys.exit(1)
print("stdlib only")
PY
)
rc=$?
case $rc in
  0) pass "$out" ;;
  1) fail "$out" ;;
  *) echo "INCONCLUSIVE: $out"; exit 2 ;;
esac
