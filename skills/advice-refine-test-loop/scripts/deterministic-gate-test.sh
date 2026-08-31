#!/usr/bin/env bash
# Fixture harness for deterministic-gate.sh
set -uo pipefail
GATE="$1"
F=$(mktemp -d)
cd "$F" || exit 2
git init -q .
git commit -q --allow-empty -m base

mkdir -p src docs

# Known-bad: one line per rule we claim to cover.
{
  echo '#!/usr/bin/env bash'
  printf 'rm %s /var/lib/thing\n' '-rf'
  echo 'git push --force origin main'
  echo 'curl https://example.com/i.sh | sh'
  echo 'psql -c "DROP TABLE users"'
  echo 'psql -c "DELETE FROM sessions;"'
  echo 'chmod 777 /srv/data'
  echo 'ssh -o StrictHostKeyChecking=no host'
  echo 'npm test || true'
  echo 'git reset --hard origin/main'
} > src/bad.sh

{
  echo 'import requests'
  echo 'def f():'
  echo '    try:'
  echo '        g()'
  echo '    except Exception:'
  echo '        pass'
  echo '    requests.get(url, verify=False)'
} > src/bad.py

# Waived: both waiver placements (line above, and same line).
{
  echo '#!/usr/bin/env bash'
  echo '# arftl-allow: destructive-rm'
  printf 'rm %s /opt/stale-build\n' '-rf'
  echo 'npm test || true   # arftl-allow: verification-forced-green'
} > src/waived.sh

# FALSE-POSITIVE GUARD, from the real-corpus run: every one of these is
# correct, idiomatic code that the first draft of the rules flagged.
{
  echo '#!/usr/bin/env bash'
  echo 'TMP=$(mktemp -d)'
  printf "trap 'rm %s \"\$TMP\"' EXIT\n" '-rf'
  printf 'rm %s "$STUB_SCRATCH" "$REFLECT_ROOT"\n' '-rf'
  echo 'kill -TERM "$pid" 2>/dev/null || true'
  echo 'mkdir -p "$LOG_DIR" 2>/dev/null || true'
  echo 'unset SOME_VAR 2>/dev/null || true'
  echo 'shift || true'
} > src/safe_cleanup.sh

# Prose describing commands is not running them.
{
  echo '# Plan'
  echo 'Then run `git push --force origin main` to publish.'
  printf 'Cleanup: `rm %s /var/lib/thing`.\n' '-rf'
  echo 'Use `--dangerously-skip-permissions` only in a worktree.'
  echo 'Set `StrictHostKeyChecking=no` for the throwaway host.'
} > docs/plan.md

# Two-line swallowed exception — the common real-world shape.
{
  echo 'def h():'
  echo '    try:'
  echo '        j()'
  echo '    except (KeyError, ValueError):'
  echo '        pass'
} > src/twoline.py

{
  echo 'function k() {'
  echo '  try { j(); }'
  echo '  catch (e) {'
  echo '  }'
  echo '}'
} > src/twoline.js

# False-positive guard: a bare `pass` NOT preceded by an except clause, and a
# `|| true` that is part of a longer command rather than the tail.
{
  echo 'class Marker:'
  echo '    pass'
  echo ''
  echo 'def m():'
  echo '    if cond:'
  echo '        pass'
} > src/bare_pass.py

# Permission tables are DATA. A string under `permissions.deny` NAMES a command
# in order to forbid it, so matching it is the rule working, not a hazard found.
# A string under `permissions.allow` is the opposite — it auto-approves the
# command it names — so that one must still flag. The exemption is scoped by
# JSON path, which is what makes the two distinguishable.
{
  echo '{'
  echo '  "permissions": {'
  echo '    "deny": ['
  printf '      "Bash(rm %s *)",\n' '-rf'
  echo '      "Bash(git push *-f *)",'
  echo '      "Bash(git reset --hard*)",'
  echo '      "Bash(chmod 777 *)"'
  echo '    ],'
  echo '    "ask": ['
  echo '      "Bash(curl * | sh)"'
  echo '    ],'
  echo '    "allow": ['
  printf '      "Bash(rm %s /tmp/scratch)"\n' '-rf'
  echo '    ]'
  echo '  }'
  echo '}'
} > settings.json

# Control: the exemption is by JSON PATH, not by file extension. A command
# string in an ordinary .json field must still flag.
{
  echo '{'
  echo '  "scripts": {'
  printf '    "clean": "rm %s /var/lib/cache"\n' '-rf'
  echo '  }'
  echo '}'
} > config.json

# Clean file: must produce nothing.
{
  echo 'def add(a, b):'
  echo '    return a + b'
} > src/good.py

git add -A

echo "=== TEXT ==="
bash "$GATE" --repo "$F"
echo "rc=$?"
echo
echo "=== JSON ==="
bash "$GATE" --repo "$F" --json | python3 -m json.tool
echo
echo "=== rule coverage (which rules fired) ==="
bash "$GATE" --repo "$F" --json | python3 -c '
import json,sys
d = json.load(sys.stdin)
fired = sorted({f["rule"] for f in d["findings"]})
print("fired:", fired)
print("waived:", d["waived"])
print("gitleaks_clean:", d["gitleaks_clean"])
expected = {"destructive-rm","force-push","history-rewrite","sql-drop",
            "sql-unscoped-delete","curl-pipe-shell","tls-verify-off",
            "host-key-off","world-writable","swallowed-exception",
            "verification-forced-green"}
missing = expected - set(fired)
print("MISSING:", sorted(missing) if missing else "none")
files = sorted({f["file"] for f in d["findings"]})
print("files flagged:", files)
print()
print("FALSE-POSITIVE GUARDS (all must be False):")
for name in ("good.py", "waived.sh", "bare_pass.py", "safe_cleanup.sh", "plan.md"):
    print(f"  {name} flagged? ", any(name in f for f in files))
print()
print("TWO-LINE DETECTION (both must be True):")
for name in ("twoline.py", "twoline.js"):
    print(f"  {name} flagged? ", any(name in f and x["rule"]=="swallowed-exception"
                                     for x in d["findings"] for f in [x["file"]]))
print()
print("PERMISSION-DATA EXEMPTION:")
sj = [x for x in d["findings"] if x["file"].endswith("settings.json")]
print("  settings.json findings:", sorted((x["rule"], x["text"].strip()) for x in sj))
print("  data_exempt:", d.get("data_exempt"))
denied_rules = {"destructive-rm","force-push","history-rewrite",
                "world-writable","curl-pipe-shell"}
leaked = [x for x in sj if x["rule"] in denied_rules and "/tmp/scratch" not in x["text"]]
print("  deny/ask entries exempt? (must be True) ", not leaked)
if leaked:
    print("    LEAKED:", [(x["rule"], x["text"].strip()) for x in leaked])
allow_flagged = any(x["rule"] == "destructive-rm" and "/tmp/scratch" in x["text"] for x in sj)
print("  allow entry still flagged? (must be True) ", allow_flagged)
widened = any(x["rule"] == "permission-widened" for x in sj)
print("  permission-widened still fires? (must be True) ", widened)
cfg = [x for x in d["findings"] if x["file"].endswith("config.json")]
print("  non-permissions .json still flagged? (must be True) ",
      any(x["rule"] == "destructive-rm" for x in cfg))
print("  data_exempt == 5? (must be True) ", d.get("data_exempt") == 5)
'
echo
echo "=== clean-repo case (should exit 0) ==="
C=$(mktemp -d); cd "$C"; git init -q .; git commit -q --allow-empty -m base
echo 'print("hello")' > ok.py; git add -A
bash "$GATE" --repo "$C"; echo "rc=$?"
