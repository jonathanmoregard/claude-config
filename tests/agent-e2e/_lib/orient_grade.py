"""Grade a reporting-state run: python3 orient_grade.py MODE RUN

MODE `orient`: the reply to a general "where are we" is an orientation summary,
not a per-PR rundown. PASS needs all of:
  - at most 150 words;
  - at most 2 distinct PRs named in prose (URL-only lines excluded);
  - no merge-state jargon (MERGEABLE, CLEAN, mergeStateStatus, N/N checks);
  - PR URLs only for PRs that need the user's click (#41);
  - stale handoff caught: #339 (MERGED live, "awaiting" in mission.md) is not
    presented as awaiting, and #41 (OPEN) still is;
  - the agent itself queried the live state of #41 and #339;
  - the "needed from you" part asks for an action (merge/click/approve); the
    #41 URL alone is not an ask.
MODE `merged`: "did #41 merge?" with #41 MERGED live. PASS needs the agent to
have queried #41's state, the reply saying it merged, and the #41 URL at the
bottom (only blank lines, other URLs or the state marker after it).

"Queried" reads the agent's own commands (commands.txt), not gh.log: gh.log
also logs the pr-state-guard Stop hook, which queries every PR the final reply
links whether or not the agent ever looked.

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
cmds_file = run / "commands.txt"
commands = cmds_file.read_text(encoding="utf-8").splitlines() if cmds_file.exists() else []
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


def agent_queried(n, repo):
    """Did one of the agent's commands read PR n (in repo) live?"""
    for c in commands:
        if re.search(r"\bgh\s+(pr\s+(view|checks)|api)\b", c) and re.search(rf"(?<![\w/.-]){n}\b|pulls?/{n}\b", c):
            return True
        if re.search(r"\bgh\s+pr\s+list\b", c) and re.search(r"(--state|-s)[ =]all\b", c):
            if repo in c or (repo == "aggregator" and not re.search(r"\s(-R|--repo)\b", c)):
                return True
    return False


needed = {"orient": [(41, "aggregator"), (339, "nixos-config")], "merged": [(41, "aggregator")]}
queried = "yes" if all(agent_queried(n, r) for n, r in needed.get(mode, [])) else "no"


def asks_for_action():
    """A merge/click/approve line inside the "needed from you" part."""
    start = next((i for i, l in enumerate(lines) if re.search(r"from you|needs? you", l, re.I)), None)
    if start is None:
        return False
    for l in lines[start:]:
        if l is not lines[start] and (re.match(r"\W*next\b", l, re.I) or URL.search(l)):
            break
        if re.search(r"\b(merge|click|approve)\b", l, re.I):
            return True
    return False


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
    if queried != "yes":
        verdict(False, "did not query live state of #41 and #339")
    ok, why = prstate("339:finished", "41:awaiting")
    if not ok:
        verdict(False, f"PR state wrong ({why})")
    if not asks_for_action():
        verdict(False, "no merge/click ask for #41 under needed-from-you")
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
