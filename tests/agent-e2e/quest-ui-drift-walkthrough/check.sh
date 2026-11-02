#!/usr/bin/env bash
# PASS: the walkthrough gives a search word (Ctrl+F / search the page) AND a
# screenshot fallback for the unverified console location, so it does not
# present one location as certain. INCONCLUSIVE when no quest framing was
# used (skill not applied).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
grep -qiE '^\s*#{1,6}\s.*\bquest\b' "$RUN/final.txt" || { echo "INCONCLUSIVE: no quest heading (skill not applied)"; exit 2; }
search=0; shot=0
grep -qiE 'ctrl\s*\+?\s*f|cmd\s*\+?\s*f|find in page|search (the page )?for' "$RUN/final.txt" && search=1
grep -qiE 'screenshot' "$RUN/final.txt" && shot=1
[ "$search" -eq 1 ] || fail "no search word (Ctrl+F)"
[ "$shot" -eq 1 ] || fail "no screenshot fallback"
pass "search word and screenshot fallback present"
