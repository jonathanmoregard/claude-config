#!/usr/bin/env python3
"""Stop hook: a reply never misreports a PR's state, and never asks for a click
without the link to click.

Two invariants, checked independently so that losing one does not cost the
other:

  A. STATE. A reply this hook lets through never presents a GitHub PR as open
     or awaiting action while GitHub reports that PR MERGED or CLOSED, as far
     as the classifier below can see. Needs `gh`.
  B. LINK. A reply that asks the user to merge or review carries the full
     `https://github.com/<owner>/<repo>/pull/<n>` URL of every PR it asks
     about. A bare `#41`, a `owner/repo#41`, a title or a markdown label is
     not something a terminal reader can click. Pure text analysis: no network,
     so it still holds when `gh` is missing and invariant A has gone blind.

Invariant B exists because A cannot cover it: A resolves a PR only from a URL
or `owner/repo#n`, so the reply that names a PR as a bare `#41` and nothing
else -- the exact shape of the lapse -- was invisible to it. That gap was
documented under *Known limits* here while the omission went on recurring.

How it decides invariant A, in order:
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

How it decides invariant B:
  1. Strip fenced code blocks and quoted lines. A commit message with
     `Fixes #389` in it, or the user's own words quoted back, is not this
     reply asking for anything.
  2. Find lines carrying merge/review-ask language aimed at the user
     (MERGE_ASK) -- "merge click", "awaiting your review", "the merge clicks
     are all yours" -- or an imperative that names a number (IMPERATIVE_ASK),
     "merge #37 first". Deliberately NOT the broad AWAITING vocabulary that
     invariant A uses: "to merge" alone matches "I need to merge main into the
     branch", and "open" matches an issue. A segment under negation
     ("nothing is awaiting your click") is not an ask.
  3. Each ask line brings the contiguous run of list items and link-only lines
     that follows it, which is where a lead-in's PRs get named.
  4. In that scope, every PR-shaped reference (`PR 41`, `#41`, `owner/repo#41`)
     whose number has no `/pull/<n>` URL anywhere in the reply is unlinked,
     unless its own segment reports the PR finished ("#35 already landed").
  5. An ask that names no PR at all, in a reply that links none either, is a
     NOTICE and never a block: measured, that shape is right less than half
     the time.

Measured on 4965 real assistant replies (one week, every project): the named-PR
finding fires on 0.89% of replies, and hand classification found no false
positive among them -- every hit was a merge or review ask carrying a PR number
with no URL. Five earlier false-positive classes were each fixed rather than
tolerated: "review 1 PR" as a count, a findings table quoting an old lapse, a
reflection record's JSON, "#35 already landed" on an ask line, and "a merge
click no amount of iteration produces".

Outcomes:
  - either invariant broken, first Stop of the chain -> {"decision": "block"}
    naming each PR, its live state (A) and the numbers missing a link (B).
  - broken, stop_hook_active already set -> systemMessage only; the hook never
    blocks twice in one Stop chain, so it cannot loop.
  - gh missing, failing or timing out -> systemMessage saying which PRs went
    unverified; never a block. Invariant B is unaffected.
  - anything unparseable -> exit 0, no output.

Known limits (deliberate, to keep false blocks rare):
  - A segment that says "merged" about one PR excuses every PR on it, and a
    sentence about the old list ("it wrongly lists #339 as waiting") reads as
    awaiting (invariant A).
  - Any PR number on an ask line is asked about, including one mentioned in
    passing ("kept both #372's assertions"). Deliberate: the alternative is to
    read only the asking segment, which loses "#394 and #395 have CI running.
    The merge clicks are all yours" -- the measured lapse itself.
  - A bare `#41` carries no repository, so matching is by number alone.
  - An ask written only inside a table cell, a JSON blob or a quoted block is
    not flagged.
Each is at most one needless block, and only a notice once stop_hook_active is
set -- which is also why the remedy is always quotable: the block names the
numbers, so the rewrite is a paste, not an investigation.

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

# --- invariant B ---------------------------------------------------------
# Asks aimed at the user, not status words. Every alternative here names the
# user or their gesture, so "I still need to merge main into the branch" and
# "the PR is open" -- both of which AWAITING matches -- cannot reach it. The
# cost of a wrong hit is a wasted turn, and this is the regex that decides how
# often that happens.
MERGE_ASK = re.compile(
    r"(?:\bmerge click|\byour click\b|\bplease (?:merge|review)\b"
    r"|\bawaiting (?:your )?(?:merge|review|click)"
    r"|\bawaiting the user|\bneeds? your (?:merge|review|click)"
    r"|\bready for (?:your )?(?:merge|review)\b"
    r"|\bfor you to (?:merge|review)\b|\byours to (?:merge|click)\b"
    r"|\b(?:merge|click|review)s? (?:is|are) (?:all )?yours\b"
    r"|\ball yours\b)",
    re.I,
)
# "Merge #37 first" is an ask too, but the bare verb cannot be: "I need to
# merge main into the branch" is the same words about my own work. So an
# imperative counts only when it names a number in the same breath, which is
# also the only form that can leave the user hunting for a link.
# The number must wear a `#` or follow the word PR. Measured: "review 1 PR"
# in a planning table matched the looser form, where the digit is a count.
IMPERATIVE_ASK = re.compile(
    r"\b(?:merge|review)\s+(?:(?:PRs?|pull requests?)\s+#?\d+|#\d+)", re.I
)
# Second person, required before the vaguest finding of all -- an ask that
# names no PR and links none. Measured: "item 4 is blocked on a merge click no
# amount of iteration produces" is a sentence about merge clicks, not an ask
# for one, and only the missing "you" tells them apart.
DIRECTED = re.compile(r"\b(?:you|your|yours)\b", re.I)
# Lines that are data rather than the reply addressing the user: a markdown
# table row, an inline JSON blob, a quoted block. Measured, each from a real
# false positive: a planning estimate ("review 1 PR") and a findings table
# quoting an earlier lapse back; a session-reflection record whose
# `obstacles_to_autonomy` array says "Blocked on a merge click for ... PR 45";
# a quoted decisions list. An ask written only inside one of these goes
# unflagged -- the URLs-at-the-bottom rule is not written in table cells.
DATA_LINE_RE = re.compile(r"^\s*[|\[{\"]")
# "Nothing is awaiting your click" is the report that there is nothing to do,
# and it carries an ask phrase verbatim. Negation is tested per segment, not
# per line, so "Don't merge #41 yet; #37 is ready for your merge" still has an
# ask in it.
NOT_ASKING = re.compile(
    r"\b(?:nothing|none|nobody|no longer|not|never|isn't|aren't|won't|don't)\b",
    re.I,
)
SEGMENT_RE = re.compile(r"[;.]")
# `PR 41` / `pull request #41`: the shape that carries no `#` at all.
PR_WORD_RE = re.compile(r"\b(?:PRs?|pull requests?)\s+#?(\d+)\b", re.I)
ISSUE_URL_RE = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/(\d+)"
)
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
QUOTE_RE = re.compile(r"^\s*>")

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


def speakable_lines(text: str) -> list[str]:
    """`text` with fenced code and quoted lines blanked out.

    Blanked rather than dropped, so line order and the run-following in
    `ask_scope` still describe the reply the user reads. A commit message
    holding `Fixes #389`, or the user's own words quoted back, is not this
    reply asking for a click.
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence or QUOTE_RE.match(line) else line)
    return out


URL_TOKEN = re.compile(r"\x00U(\d+)\x00")
SHORT_TOKEN = re.compile(r"\x00S(\d+)\x00")


def segments(line: str) -> list[str]:
    """Sentence-ish pieces of `line`, with PR references masked first.

    Masked because splitting on `.` would cut `jonathanmoregard/.claude#152`
    in half and lose the reference. A URL becomes \\x00U<n>\\x00 and an
    `owner/repo#n` becomes \\x00S<n>\\x00, so a segment still knows which
    numbers it holds and in which shape.
    """
    masked = URL_RE.sub(lambda m: f" \x00U{m.group(3)}\x00 ", line)
    masked = SHORT_RE.sub(lambda m: f" \x00S{m.group(3)}\x00 ", masked)
    return SEGMENT_RE.split(masked)


def asking_segments(line: str) -> list[str]:
    """Segments of `line` that ask the user to merge or review."""
    if DATA_LINE_RE.match(line):
        return []
    return [seg for seg in segments(line)
            if (MERGE_ASK.search(seg) or IMPERATIVE_ASK.search(seg))
            and not NOT_ASKING.search(seg)]


def is_ask(line: str) -> bool:
    """True when some segment of `line` asks the user to merge or review."""
    return bool(asking_segments(line))


def ask_scope(lines: list[str]) -> list[str]:
    """Lines belonging to a merge/review ask: the ask line, plus what it leads.

    An ask is often a lead-in ("The merge clicks are all yours:") whose PRs are
    named in the list under it, or linked in a block of bare URLs after a blank
    line. Both runs are followed; a blank line otherwise ends the ask, so an
    unrelated bullet list further down the reply is not read as part of it.
    """
    scope: list[str] = []
    for i, line in enumerate(lines):
        if not is_ask(line):
            continue
        scope.append(line)
        j = i + 1
        while j < len(lines):
            if not lines[j].strip():
                k = j
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and LINK_ONLY_RE.match(lines[k]):
                    j = k
                    continue
                break
            if LIST_ITEM_RE.match(lines[j]) or LINK_ONLY_RE.match(lines[j]):
                scope.append(lines[j])
                j += 1
                continue
            break
    return scope


def link_gap(text: str) -> tuple[list[str], str | None]:
    """Invariant B: (PR numbers asked about without their URL, linkless ask).

    The second element is set only when an ask names no PR at all and the reply
    holds no PR URL either ("all four are green, merge away") -- the same lapse
    with nothing to enumerate. Matching is by number alone, because a bare `#41`
    carries no repository: a reply that links claude-config#41 while asking
    about nixos-config#41 passes. Narrow, and unfixable without inventing the
    repository the text never gave.
    """
    lines = speakable_lines(text)
    scope = ask_scope(lines)
    if not scope:
        return [], None
    body = "\n".join(lines)
    linked = {m.group(3) for m in URL_RE.finditer(body)}
    issues = {m.group(1) for m in ISSUE_URL_RE.finditer(body)}
    missing: list[str] = []
    for line in scope:
        for seg in segments(line):
            # "Order matters only in that #35 already landed" sits on an ask
            # line and asks for nothing: a segment that reports a PR finished
            # is not asking the user to click it. Measured as a false positive.
            if TERMINAL.search(seg):
                continue
            nums = SHORT_TOKEN.findall(seg)
            nums += [m.group(1) for m in PR_WORD_RE.finditer(seg)]
            nums += BARE_NUM_RE.findall(URL_TOKEN.sub(" ", seg))
            for n in nums:
                if n not in linked and n not in issues and n not in missing:
                    missing.append(n)
    if not missing and not linked:
        # Nothing named and nothing linked: only a lapse if the reply is
        # addressing the user's own gesture.
        directed = next((seg for line in scope for seg in asking_segments(line)
                         if DIRECTED.search(seg)), None)
        return [], directed.strip() if directed else None
    return missing, None


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

    # Invariant B first: it is pure text, makes no call, and must still be
    # enforced for the reply that names no resolvable PR at all -- which is the
    # case the state check below returns early on.
    gaps, linkless = link_gap(text)
    missing_links = []
    if gaps:
        missing_links.append(
            "- this reply asks for a merge or review but gives no clickable URL for "
            + ", ".join("#" + n for n in gaps))
    # An ask that names no PR is a NOTICE, never a block. Measured over 4943
    # real replies: of the hits on that shape, 5 were the lapse and 7 were
    # sentences that merely sound like it ("Where your click lands. You see a
    # title and a diff and press Merge"). 42% precision does not earn the
    # power to send a turn back; the named-PR finding above was 100%.
    link_notice = None
    if linkless and not gaps:
        link_notice = ('PR guard: this reply asks for a merge or review ("'
                       + linkless[:120] + '") and links no PR at all. If a PR is '
                       "waiting on the user, its full URL belongs in the reply.")

    prs = extract_prs(text)
    if not prs:
        return link_only_output(missing_links, link_notice, payload)

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
    notices = [link_notice] if link_notice else []
    if unverified:
        why = "gh is not on PATH" if gh is None else "gh pr view failed or timed out"
        notices.append("PR-state guard: live state unverified (" + why + ") for "
                       + ", ".join(unverified)
                       + ". The PR states in this reply are unchecked.")
    notice = "\n".join(notices) or None

    if stale or missing_links:
        head = []
        if stale:
            head.append("presents PRs as open or awaiting action that GitHub "
                        "reports as finished")
        if missing_links:
            head.append("asks for a click without the link to click")
        facts = ("PR guard: this reply " + ", and ".join(head) + ":\n"
                 + "\n".join(stale + missing_links))
        if payload.get("stop_hook_active"):
            return {"systemMessage": facts + (("\n" + notice) if notice else "")}
        out: dict = {"decision": "block", "reason": facts + remedy(stale, missing_links)}
        if notice:
            out["systemMessage"] = notice
        return out
    if notice:
        return {"systemMessage": notice}
    return None


def remedy(stale: list[str], missing_links: list[str]) -> str:
    """What to write instead. Quotable, so a block costs one rewrite, not a hunt."""
    out = ""
    if stale:
        out += ("\nA merged PR is reported as merged (with its time), a closed one as "
                "closed, and neither is listed as needing a click. Rewrite the reply "
                "from live `gh pr view` state.")
    if missing_links:
        out += ("\nEvery PR a merge or review ask names carries its full "
                "https://github.com/<owner>/<repo>/pull/<n> URL, one per line at the "
                "bottom of the reply. Not the number, not `owner/repo#n`, not the "
                "title: the thing the user can click.")
    return out


def link_only_output(missing_links: list[str], link_notice: str | None,
                     payload: dict) -> dict | None:
    """Invariant B's verdict when the reply names no resolvable PR at all."""
    if not missing_links:
        return {"systemMessage": link_notice} if link_notice else None
    facts = ("PR guard: this reply asks for a click without the link to click:\n"
             + "\n".join(missing_links))
    if payload.get("stop_hook_active"):
        return {"systemMessage": facts}
    return {"decision": "block", "reason": facts + remedy([], missing_links)}


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
