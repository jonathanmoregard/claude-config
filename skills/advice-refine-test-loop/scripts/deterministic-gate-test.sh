#!/usr/bin/env bash
# Fixture harness for deterministic-gate.sh
set -uo pipefail
GATE="$1"

# Fixture repos must not run the INVOKING USER's global git config or hooks.
# On this host core.hooksPath points at a gitleaks pre-commit hook, which
# REFUSES a commit carrying a secret — so the secret-lane fixtures below could
# never commit their fixture credential, silently left it staged, and turned
# the committed-range arm into a duplicate of the staged arm that passed
# whether or not the gate scanned the range at all. Same reasoning, and the
# same two variables, as tests/smoke.sh.
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
F=$(mktemp -d)
cd "$F" || exit 2
git init -q .
# Identity is set per-repo because GIT_CONFIG_GLOBAL=/dev/null above takes the
# user's global one away with the hooks it came for.
git config user.email t@example.invalid
git config user.name t
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
C=$(mktemp -d); cd "$C"; git init -q .
git config user.email t@example.invalid; git config user.name t
git commit -q --allow-empty -m base
echo 'print("hello")' > ok.py; git add -A
bash "$GATE" --repo "$C"; echo "rc=$?"

echo
echo "=== secret lane scans the RANGE it reports on ==="
# The pattern rules scan `origin/main...HEAD`; the secret lane must scan the
# same commits. It used to scan the INDEX (`gitleaks git --staged`) instead,
# and the loop commits at every round's step 7 — so by the time any round
# after the first ran the gate, the index was empty, the lane scanned nothing,
# and the run reported `gitleaks_clean: true` over a committed secret.
#
# Fixture token: GitHub PAT shape, body drawn from urandom so no literal
# credential is ever written into this file and the value is certainly invalid.
SECRET_FIXTURE="ghp_$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 36)"

mk_range_repo() {   # mk_range_repo <dir>; leaves it on feat/x with an origin
  local d="$1"
  git init -q -b main "$d"
  git -C "$d" config user.email t@example.invalid
  git -C "$d" config user.name t
  echo base > "$d/base.txt"
  git -C "$d" add -A
  git -C "$d" commit -q -m base
  git clone -q "$d" "$d.origin" >/dev/null 2>&1
  git -C "$d" remote add origin "$d.origin"
  git -C "$d" fetch -q origin
  git -C "$d" checkout -q -b feat/x
}

# (a) secret COMMITTED in range, index clean — the shape every round after the
#     first presents. Must be caught.
LEAKED=$(mktemp -d)/repo; mk_range_repo "$LEAKED"
printf 'token = "%s"\n' "$SECRET_FIXTURE" > "$LEAKED/creds.txt"
git -C "$LEAKED" add creds.txt
git -C "$LEAKED" commit -q -m "add fixture credential"
echo "  (a) committed secret, $(git -C "$LEAKED" diff --cached --name-only | wc -l) file(s) staged"
out_a=$(bash "$GATE" --repo "$LEAKED" --range origin/main...HEAD --json); rc_a=$?
echo "      gate: $out_a"
echo "      rc=$rc_a"
printf '      caught? (must be True)  %s\n' \
  "$(printf '%s' "$out_a" | python3 -c 'import json,sys; print(json.load(sys.stdin)["gitleaks_clean"] is False)')"
printf '      exit 1? (must be True)  %s\n' "$([[ $rc_a -eq 1 ]] && echo True || echo False)"

# (b) same range, no secret — must stay clean, or the lane is just noise.
CLEAN=$(mktemp -d)/repo; mk_range_repo "$CLEAN"
echo 'ordinary = "value"' > "$CLEAN/ok.txt"
git -C "$CLEAN" add ok.txt
git -C "$CLEAN" commit -q -m "add ordinary file"
out_b=$(bash "$GATE" --repo "$CLEAN" --range origin/main...HEAD --json); rc_b=$?
echo "  (b) clean committed range"
echo "      gate: $out_b"
printf '      clean? (must be True)   %s\n' \
  "$(printf '%s' "$out_b" | python3 -c 'import json,sys; print(json.load(sys.stdin)["gitleaks_clean"] is True)')"
printf '      exit 0? (must be True)  %s\n' "$([[ $rc_b -eq 0 ]] && echo True || echo False)"

# (c) secret STAGED but uncommitted — the case the old lane did cover, kept so
#     the fix cannot trade one blind spot for the other.
STAGED=$(mktemp -d)/repo; mk_range_repo "$STAGED"
printf 'token = "%s"\n' "$SECRET_FIXTURE" > "$STAGED/creds.txt"
git -C "$STAGED" add creds.txt
out_c=$(bash "$GATE" --repo "$STAGED" --range origin/main...HEAD --json); rc_c=$?
echo "  (c) staged-but-uncommitted secret"
echo "      gate: $out_c"
printf '      caught? (must be True)  %s\n' \
  "$(printf '%s' "$out_c" | python3 -c 'import json,sys; print(json.load(sys.stdin)["gitleaks_clean"] is False)')"

echo
echo "=== a waiver reaches ONE line, not the whole diff ==="
# The documented escape is `arftl-allow: <rule-id>` "on the matched line or the
# line directly above it". `prevtext` implemented the second half with no
# adjacency test and was never reset between hunks or files, so the tag on the
# LAST added line of one hunk silently waived a BLOCKER on the FIRST added line
# of the next — measured: `rm -rf /tmp/victim` 23 lines and one hunk away from
# an `arftl-allow: destructive-rm` was reported as waived, with no finding.
# Silent suppression of a BLOCKER is the worst failure this gate has, since the
# whole point of the rules layer is that it cannot be talked out of a verdict.
WV=$(mktemp -d)/repo; mk_range_repo "$WV"
{ echo "line1"; for i in $(seq 2 40); do echo "filler$i"; done; } > "$WV/a.sh"
echo "base" > "$WV/b.sh"
git -C "$WV" add -A; git -C "$WV" commit -q -m fillers
python3 - "$WV/a.sh" <<'PY'
import sys
lines = open(sys.argv[1]).read().splitlines()
lines.insert(2, 'echo safe  # arftl-allow: destructive-rm')   # hunk 1, waived
lines.insert(25, 'rm -rf /tmp/gate-waiver-victim')            # hunk 2, MUST flag
open(sys.argv[1], 'w').write("\n".join(lines) + "\n")
PY
# Same shape again, but with the waiver as the last added line of the PREVIOUS
# FILE, so the reset is asserted across the +++ boundary too.
printf 'echo tail  # arftl-allow: destructive-rm\n' >> "$WV/a.sh"
printf 'rm -fr /tmp/gate-waiver-victim-2\n' >> "$WV/b.sh"
git -C "$WV" add -A; git -C "$WV" commit -q -m "waiver scope fixture"
out_w=$(bash "$GATE" --repo "$WV" --range HEAD~1...HEAD --json)
echo "      gate: $out_w"
printf '%s' "$out_w" | python3 -c '
import json, sys
d = json.load(sys.stdin)
rm = [f for f in d["findings"] if f["rule"] == "destructive-rm"]
files = {f["file"] for f in rm}
print("      cross-HUNK match flagged? (must be True) ", "a.sh" in files)
print("      cross-FILE match flagged? (must be True) ", "b.sh" in files)
print("      both, i.e. no silent waive? (must be True) ", len(rm) == 2)
'

echo
echo "=== ...and the documented adjacent waiver still works ==="
# The fix must not buy correctness by breaking the escape hatch: a tag on the
# matched line, and on the line physically above it, both still waive.
AD=$(mktemp -d)/repo; mk_range_repo "$AD"
printf 'rm -rf /tmp/on-the-line  # arftl-allow: destructive-rm\n' > "$AD/same.sh"
printf '# arftl-allow: destructive-rm\nrm -rf /tmp/line-above\n' > "$AD/above.sh"
git -C "$AD" add -A; git -C "$AD" commit -q -m "adjacent waivers"
out_ad=$(bash "$GATE" --repo "$AD" --range origin/main...HEAD --json); rc_ad=$?
echo "      gate: $out_ad"
printf '%s' "$out_ad" | python3 -c '
import json, sys
d = json.load(sys.stdin)
rm = [f for f in d["findings"] if f["rule"] == "destructive-rm"]
print("      same-line waiver honoured? (must be True)  ", not any(f["file"].endswith("same.sh") for f in rm))
print("      line-above waiver honoured? (must be True) ", not any(f["file"].endswith("above.sh") for f in rm))
print("      waived count == 2? (must be True)          ", d.get("waived") == 2)
'
printf '      exit 0? (must be True)  %s\n' "$([[ $rc_ad -eq 0 ]] && echo True || echo False)"
