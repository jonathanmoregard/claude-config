"""Grade a reporting-state run: python3 orient_grade.py MODE RUN

MODE `orient`: the reply to a general "where are we" is an orientation summary,
not a per-PR rundown. PASS needs all of:
  - at most 150 words;
  - at most 2 distinct PRs named in prose (URL-only lines excluded);
  - no merge-state jargon (MERGEABLE, CLEAN, mergeStateStatus, N/N checks);
  - PR URLs only for PRs that need the user's click (#41);
  - stale handoff caught: #339 (MERGED live, "awaiting" in mission.md) is not
    presented as awaiting, and #41 (OPEN) still is.
MODE `merged`: "did #41 merge?" with #41 MERGED live. PASS needs a gh call
that could read #41's state, the reply saying it merged, and the #41 URL at
the bottom (only blank lines, other URLs or the state marker after it).

The last stdout line is the verdict reason plus metrics (words, prs, urls,
marker, queried, skills) for the PR-body table.
"""
import json
import pathlib
import re
import subprocess
import sys

mode, run = sys.argv[1], pathlib.Path(sys.argv[2])
final = (run / "final.txt").read_text(encoding="utf-8")
lines = [l.rstrip() for l in final.splitlines()]
gh_log = (run / "gh.log").read_text(encoding="utf-8") if (run / "gh.log").exists() else ""
MARKERS = {"DONE", "QUESTION", "BLOCKED", "RUNNING", "FAILED"}
URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")


def bare(l):
    return re.sub(r"[*`_\s]", "", l)


nonblank = [l for l in lines if l.strip()]
marker = bare(nonblank[-1]) if nonblank and bare(nonblank[-1]) in MARKERS else "-"
words = len(final.split())
urls = sorted({int(n) for n in URL.findall(final)})
prose = [l for l in lines if not re.fullmatch(r"\s*[-*\d.]*\s*<?" + URL.pattern + r">?\s*", l)]
named = sorted({int(n) for l in prose for n in re.findall(r"(?:#|pull/)(\d+)\b", URL.sub("", l))}
               | {int(n) for l in prose for n in URL.findall(l)})
queried = "yes" if re.search(r"^(pr (view|list|checks)|api)\b", gh_log, re.M) else "no"
skills = []
for raw in (run / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
    try:
        ev = json.loads(raw)
    except ValueError:
        continue
    for c in (ev.get("message") or {}).get("content") or []:
        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Skill":
            skills.append(str((c.get("input") or {}).get("skill")))
        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Read":
            m = re.search(r"skills/([\w-]+)/SKILL\.md", str((c.get("input") or {}).get("file_path")))
            if m:
                skills.append(m.group(1))
metrics = (f"words={words} prs={named} urls={urls} marker={marker} "
           f"queried={queried} skills={','.join(sorted(set(skills))) or '-'}")


def verdict(ok, why):
    print(f"{'PASS' if ok else 'FAIL'}: {why} | {metrics}")
    sys.exit(0 if ok else 1)


def prstate(*specs):
    grade = pathlib.Path(__file__).with_name("prstate_grade.py")
    r = subprocess.run([sys.executable, str(grade), str(run / "final.txt"), *specs],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


if mode == "orient":
    ok, why = prstate("339:finished", "41:awaiting")
    if not ok:
        verdict(False, f"PR state wrong ({why})")
    if words > 150:
        verdict(False, "over 150 words")
    if len(named) > 2:
        verdict(False, "per-PR rundown")
    if re.search(r"MERGEABLE|\bCLEAN\b|mergeStateStatus|\b\d+/\d+\s*(checks|green)", final):
        verdict(False, "merge-state jargon")
    if set(urls) - {41}:
        verdict(False, "URL for a PR that needs no click")
    verdict(True, "orientation summary")

if mode == "merged":
    if queried != "yes":
        verdict(False, "never queried PR state")
    mention = [i for i, l in enumerate(lines) if re.search(r"(#|pull/)41\b", l)]
    if not any(re.search(r"merged", " ".join(lines[max(0, i - 1): i + 2]), re.I) for i in mention):
        verdict(False, "did not say #41 merged")
    at = [i for i, l in enumerate(lines) if "/pull/41" in l]
    if not at:
        verdict(False, "no #41 URL")
    tail = lines[at[-1] + 1:]
    if any(l.strip() and not URL.search(l) and bare(l) not in MARKERS for l in tail):
        verdict(False, "#41 URL not at the bottom")
    verdict(True, "merged, queried, URL at bottom")

sys.exit(f"unknown mode {mode}")
