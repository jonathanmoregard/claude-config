#!/usr/bin/env python3
"""Stop hook: refuse a reply that presents a merged or closed PR as open.

Invariant: a reply this hook lets through never presents a GitHub PR as open or
awaiting action while GitHub reports that PR MERGED or CLOSED, as far as the
classifier below can see.

How it decides, in order:
  1. Extract PR references from `last_assistant_message`: full URLs
     `https://github.com/<owner>/<repo>/pull/<n>` and `<owner>/<repo>#<n>`.
     No reference means no network call and no output.
  2. Query each PR's live state with `gh pr view` (in parallel, bounded).
  3. For every MERGED or CLOSED PR, collect the lines that mention it: lines
     holding its URL or `owner/repo#n`, and lines holding a bare `#n` when only
     one referenced PR has that number. A mention line that carries no status
     word of its own (a line of nothing but links, or a list item) also brings
     its lead-in: the nearest line above that is not blank, links-only or a
     list item (e.g. "Awaiting your merge click:"), unless that lead-in names
     a different PR.
  4. The PR is misreported when one of those lines (split on `;`) contains an
     awaiting word (AWAITING) and no terminal word (TERMINAL). A line that says
     "merged" or "closed" is taken at its word.

Outcomes:
  - misreport found, first Stop of the chain  -> {"decision": "block"} naming
    each PR and its live state.
  - misreport found, stop_hook_active already set -> systemMessage only; the
    hook never blocks twice in one Stop chain, so it cannot loop.
  - gh missing, failing or timing out -> systemMessage saying which PRs went
    unverified; never a block.
  - anything unparseable -> exit 0, no output.

Known limits (deliberate, to keep false blocks rare): a PR named only as a bare
`#n` with no URL anywhere in the reply is not resolved; a segment that says
"merged" about one PR excuses every PR on it; a sentence about the old list
("it wrongly lists #339 as waiting") reads as awaiting: at most one needless
block, and only a notice once stop_hook_active is set.

MERGED is terminal, so it is cached per session under
~/.local/state/claude-pr-state-guard/; OPEN and CLOSED (reopenable) are queried
fresh at every Stop.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

GH_TIMEOUT_S = 6
MAX_PRS = 20
CACHE_MAX_AGE_S = 7 * 24 * 3600

URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)"
)
SHORT_RE = re.compile(
    r"(?<![\w/.-])([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9_.-]+)#(\d+)\b"
)
BARE_NUM_RE = re.compile(r"(?<![\w/&])#(\d+)\b")
# A line that is only links (plus list markers / punctuation): its status words,
# if any, live on the line above it.
LINK_ONLY_RE = re.compile(
    r"^[\s>*\-+|#\d.)(\[\]<]*(?:" + URL_RE.pattern + r"[\s)>\]|.,]*)+$"
)

LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")

# Words that present a PR as still needing someone to act on it.
AWAITING = re.compile(
    r"\b(?:await\w*|waiting|wait(?:s)? on|pending|ready\b|needs? (?:your|a|the)\b"
    r"|your (?:merge|click|review|approval)|merge click|to merge\b|please merge"
    r"|for (?:your )?review|needs? review|open\b|still open|outstanding"
    r"|blocked on (?:you|your))",
    re.I,
)
# Words that already state the PR is finished.
TERMINAL = re.compile(r"\b(?:merged|closed|landed|abandoned|superseded|no longer)\b", re.I)

SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class PR(NamedTuple):
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}".lower()


def extract_prs(text: str) -> list[PR]:
    """Every distinct PR the text references, in first-mention order."""
    seen: dict[str, PR] = {}
    for rx in (URL_RE, SHORT_RE):
        for m in rx.finditer(text):
            pr = PR(m.group(1), m.group(2), int(m.group(3)))
            seen.setdefault(pr.key, pr)
    return list(seen.values())[:MAX_PRS]


def _mentions(line: str, pr: PR, bare_ok: bool) -> bool:
    for rx in (URL_RE, SHORT_RE):
        for m in rx.finditer(line):
            if PR(m.group(1), m.group(2), int(m.group(3))).key == pr.key:
                return True
    if bare_ok:
        stripped = SHORT_RE.sub(" ", URL_RE.sub(" ", line))
        return any(int(n) == pr.number for n in BARE_NUM_RE.findall(stripped))
    return False


def _names_other_pr(line: str, pr: PR, all_prs: list[PR]) -> bool:
    return any(o.key != pr.key and _mentions(line, o, bare_ok=True) for o in all_prs)


def context_lines(text: str, pr: PR, all_prs: list[PR]) -> list[str]:
    """Lines of `text` that speak about `pr` (see module docstring, step 3)."""
    lines = text.splitlines()
    bare_ok = sum(1 for o in all_prs if o.number == pr.number) == 1
    out: list[str] = []
    for i, line in enumerate(lines):
        if not _mentions(line, pr, bare_ok):
            continue
        out.append(line)
        has_status = AWAITING.search(line) or TERMINAL.search(line)
        if LINK_ONLY_RE.match(line) or (LIST_ITEM_RE.match(line) and not has_status):
            j = i - 1
            while j >= 0 and (not lines[j].strip() or LINK_ONLY_RE.match(lines[j])
                              or LIST_ITEM_RE.match(lines[j])):
                j -= 1
            if j >= 0 and not _names_other_pr(lines[j], pr, all_prs):
                out.append(lines[j])
    return out


def presented_as_awaiting(lines: list[str]) -> str | None:
    """The first segment presenting a PR as awaiting action, else None."""
    for line in lines:
        for seg in line.split(";"):
            if AWAITING.search(seg) and not TERMINAL.search(seg):
                return seg.strip()
    return None


def query_state(gh: str, pr: PR) -> dict | None:
    """Live {"state", "mergedAt", "closedAt"} for one PR, or None if unknowable."""
    try:
        p = subprocess.run(
            [gh, "pr", "view", str(pr.number), "-R", f"{pr.owner}/{pr.repo}",
             "--json", "state,mergedAt,closedAt"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("state") not in ("OPEN", "MERGED", "CLOSED"):
        return None
    return data


def cache_file(home: pathlib.Path, session_id: str) -> pathlib.Path | None:
    if not SESSION_RE.match(session_id or ""):
        return None
    return home / ".local/state/claude-pr-state-guard" / f"{session_id}.json"


def load_cache(path: pathlib.Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(path: pathlib.Path | None, cache: dict, now: float) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for old in path.parent.glob("*.json"):
            if now - old.stat().st_mtime > CACHE_MAX_AGE_S:
                old.unlink(missing_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(path)
    except OSError:
        pass


def live_states(prs: list[PR], gh: str | None, cache: dict) -> dict[str, dict | None]:
    """key -> live state dict (None = could not verify). MERGED comes from cache."""
    states: dict[str, dict | None] = {}
    todo = []
    for pr in prs:
        hit = cache.get(pr.key)
        if isinstance(hit, dict) and hit.get("state") == "MERGED":
            states[pr.key] = hit
        else:
            todo.append(pr)
    if todo and gh is None:
        states.update({pr.key: None for pr in todo})
    elif todo:
        # One worker per PR: worst case is one GH_TIMEOUT_S, inside the 15 s hook slot.
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            for pr, st in zip(todo, pool.map(lambda p: query_state(gh, p), todo)):
                states[pr.key] = st
    return states


def describe(state: dict) -> str:
    when = state.get("mergedAt") if state["state"] == "MERGED" else state.get("closedAt")
    return f"{state['state']}" + (f" at {when}" if when else "")


def decide(payload: dict, home: pathlib.Path, gh: str | None, now: float) -> dict | None:
    """The hook's JSON output for one Stop payload, or None for no output."""
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not text:
        return None
    prs = extract_prs(text)
    if not prs:
        return None

    cpath = cache_file(home, str(payload.get("session_id") or ""))
    cache = load_cache(cpath)
    states = live_states(prs, gh, cache)
    for pr in prs:
        st = states.get(pr.key)
        if st and st["state"] == "MERGED":
            cache[pr.key] = st
    save_cache(cpath, cache, now)

    stale = []
    for pr in prs:
        st = states.get(pr.key)
        if not st or st["state"] == "OPEN":
            continue
        quote = presented_as_awaiting(context_lines(text, pr, prs))
        if quote is not None:
            stale.append(f"- {pr.url} is {describe(st)} on GitHub; the reply says: \"{quote[:160]}\"")

    unverified = [pr.url for pr in prs if states.get(pr.key) is None]
    notice = None
    if unverified:
        why = "gh is not on PATH" if gh is None else "gh pr view failed or timed out"
        notice = ("PR-state guard: live state unverified (" + why + ") for "
                  + ", ".join(unverified) + ". The PR states in this reply are unchecked.")

    if stale:
        facts = ("PR-state guard: this reply presents PRs as open or awaiting action that "
                 "GitHub reports as finished:\n" + "\n".join(stale))
        if payload.get("stop_hook_active"):
            return {"systemMessage": facts + (("\n" + notice) if notice else "")}
        out: dict = {"decision": "block", "reason": facts + (
            "\nA merged PR is reported as merged (with its time), a closed one as "
            "closed, and neither is listed as needing a click. Rewrite the reply "
            "from live `gh pr view` state.")}
        if notice:
            out["systemMessage"] = notice
        return out
    if notice:
        return {"systemMessage": notice}
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        out = decide(payload, pathlib.Path.home(), shutil.which("gh"), time.time())
    except Exception:  # a guard that crashes must not wedge the turn
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
