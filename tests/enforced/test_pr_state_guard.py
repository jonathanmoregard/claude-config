"""hooks/pr-state-guard.py: a reply never presents a merged/closed PR as awaiting.

Every case runs the real hook as a subprocess with a stub `gh` first on PATH
and a throwaway HOME. No network.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "runtime/hooks/pr-state-guard.py"
NIX = "https://github.com/jonathanmoregard/nixos-config/pull"
CLA = "https://github.com/jonathanmoregard/.claude/pull"

STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
here = pathlib.Path(__file__).parent
with open(here / "gh.log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
cfg = json.loads((here / "states.json").read_text())
if cfg.get("fail"):
    print("HTTP 401: Bad credentials", file=sys.stderr); sys.exit(1)
if cfg.get("sleep"):
    time.sleep(cfg["sleep"])
a = sys.argv[1:]
if a[:2] != ["pr", "view"]:
    sys.exit(2)
key = f"{a[a.index('-R') + 1]}#{a[2]}".lower()
st = cfg["prs"].get(key)
if st is None:
    print("GraphQL: Could not resolve to a PullRequest", file=sys.stderr); sys.exit(1)
print(json.dumps({"state": st,
                  "mergedAt": "2026-10-12T08:07:39Z" if st == "MERGED" else None,
                  "closedAt": "2026-10-12T08:07:39Z" if st != "OPEN" else None}))
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(STUB)
    gh.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    class Env:
        def set(self, prs=None, **extra):
            (bindir / "states.json").write_text(json.dumps({"prs": prs or {}, **extra}))

        def calls(self):
            log = bindir / "gh.log"
            return log.read_text().splitlines() if log.exists() else []

        def run(self, message, session="s1", stop_hook_active=False, path=None, raw=None):
            stdin = raw if raw is not None else json.dumps({
                "session_id": session, "hook_event_name": "Stop",
                "stop_hook_active": stop_hook_active,
                "last_assistant_message": message})
            p = subprocess.run(
                [sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True,
                env={"PATH": path if path is not None else f"{bindir}:{os.environ['PATH']}",
                     "HOME": str(home)}, timeout=30)
            assert p.returncode == 0, p.stderr
            return json.loads(p.stdout) if p.stdout.strip() else None

    e = Env()
    e.set()
    return e


MERGED_339 = {"jonathanmoregard/nixos-config#339": "MERGED"}


def test_merged_pr_listed_as_awaiting_merge_click_blocks(env):
    env.set(MERGED_339)
    out = env.run(f"Awaiting your merge click: {NIX}/339")
    assert out["decision"] == "block"
    assert f"{NIX}/339 is MERGED" in out["reason"]


def test_link_on_its_own_line_under_awaiting_header_blocks(env):
    """The incident's shape: status words on one line, the URL alone below."""
    env.set({**MERGED_339, "jonathanmoregard/.claude#152": "OPEN"})
    msg = ("Done with the refactor.\n\nAwaiting your merge click:\n"
           f"1. {NIX}/339\n2. {CLA}/152\n")
    out = env.run(msg)
    assert out["decision"] == "block"
    assert "/339 is MERGED" in out["reason"]
    assert "/152" not in out["reason"]


def test_list_items_under_awaiting_header_with_links_at_bottom_block(env):
    """Status on the lead-in line, bare-number list items, links at the bottom."""
    env.set({**MERGED_339, "jonathanmoregard/.claude#152": "OPEN"})
    msg = ("Awaiting your merge click:\n1. nixos-config #339\n2. .claude #152\n\n"
           f"{NIX}/339\n{CLA}/152\n")
    out = env.run(msg)
    assert out["decision"] == "block" and "/339 is MERGED" in out["reason"]


def test_short_form_list_items_take_status_from_lead_in(env):
    env.set({**MERGED_339, "jonathanmoregard/.claude#152": "OPEN"})
    msg = ("Awaiting your merge click:\n- jonathanmoregard/nixos-config#339\n"
           "- jonathanmoregard/.claude#152\n")
    assert env.run(msg)["decision"] == "block"


def test_bottom_links_skip_unnumbered_list_to_reach_lead_in(env):
    env.set({**MERGED_339, "jonathanmoregard/.claude#152": "OPEN"})
    msg = ("Awaiting your merge click:\n1. dictation deploy-safety fix\n2. stop hook\n\n"
           f"{NIX}/339\n{CLA}/152\n")
    assert env.run(msg)["decision"] == "block"


def test_list_item_with_own_merged_status_passes(env):
    env.set({**MERGED_339, "jonathanmoregard/.claude#152": "OPEN"})
    msg = ("Status:\n1. nixos-config #339 (merged 10:07)\n2. .claude #152 awaits your click\n\n"
           f"{NIX}/339\n{CLA}/152\n")
    assert env.run(msg) is None


def test_bare_number_line_with_link_at_bottom_blocks(env):
    env.set({**MERGED_339, "jonathanmoregard/.claude#152": "OPEN"})
    msg = ("1. nixos-config #339 - ready for your merge\n"
           "2. .claude #152 - CI running\n\n"
           f"{NIX}/339\n{CLA}/152")
    assert env.run(msg)["decision"] == "block"


def test_closed_pr_presented_as_open_blocks(env):
    env.set({"jonathanmoregard/nixos-config#340": "CLOSED"})
    out = env.run(f"Still open and waiting on review: {NIX}/340")
    assert out["decision"] == "block"
    assert "CLOSED" in out["reason"]


def test_short_form_reference_blocks(env):
    env.set(MERGED_339)
    assert env.run("jonathanmoregard/nixos-config#339 needs your click")["decision"] == "block"


def test_merged_pr_reported_as_merged_passes(env):
    env.set(MERGED_339)
    assert env.run(f"Merged 10:07 CEST, deploy ran:\n{NIX}/339") is None
    assert env.run(f"nixos-config #339 - MERGED, nothing awaiting you.\n{NIX}/339") is None
    assert env.run(f"nixos-config #339 no longer needs your click.\n{NIX}/339") is None


def test_open_pr_awaiting_click_passes(env):
    """No false block: an OPEN PR listed as awaiting a click is correct."""
    env.set({"jonathanmoregard/.claude#152": "OPEN"})
    assert env.run(f"Awaiting your merge click:\n{CLA}/152") is None


def test_header_naming_another_pr_is_not_borrowed(env):
    """A link-only line does not inherit a status line about a different PR."""
    env.set({**MERGED_339, "jonathanmoregard/.claude#152": "OPEN"})
    msg = f".claude #152 awaits your click\n{CLA}/152\n{NIX}/339"
    assert env.run(msg) is None


def test_no_pr_reference_makes_no_gh_call(env):
    assert env.run("All tests pass. Nothing is awaiting your click.") is None
    assert env.calls() == []


def test_gh_failure_never_blocks_and_says_unverified(env):
    env.set(MERGED_339, fail=True)
    out = env.run(f"Awaiting your merge click: {NIX}/339")
    assert "decision" not in out
    assert "unverified" in out["systemMessage"] and "/339" in out["systemMessage"]


def test_gh_missing_from_path_never_blocks(env, tmp_path):
    out = env.run(f"Awaiting your merge click: {NIX}/339", path=str(tmp_path / "empty"))
    assert "decision" not in out and "gh is not on PATH" in out["systemMessage"]


def test_stop_hook_active_never_blocks_again(env):
    """Loop guard: a second Stop in the same chain gets a notice, not a block."""
    env.set(MERGED_339)
    out = env.run(f"Awaiting your merge click: {NIX}/339", stop_hook_active=True)
    assert "decision" not in out and "/339 is MERGED" in out["systemMessage"]


def test_merged_state_is_cached_per_session(env):
    env.set(MERGED_339)
    env.run(f"Awaiting your merge click: {NIX}/339", session="sess-a")
    env.set({}, fail=True)  # GitHub unreachable now
    out = env.run(f"Awaiting your merge click: {NIX}/339", session="sess-a")
    assert out["decision"] == "block"
    assert len(env.calls()) == 1


def test_open_state_is_requeried(env):
    env.set({"jonathanmoregard/.claude#152": "OPEN"})
    env.run(f"Awaiting your click: {CLA}/152", session="sess-b")
    env.set({"jonathanmoregard/.claude#152": "MERGED"})
    assert env.run(f"Awaiting your click: {CLA}/152", session="sess-b")["decision"] == "block"


@pytest.mark.parametrize("raw", ["", "not json", "[1,2]", '{"last_assistant_message": 5}'])
def test_unparseable_input_is_silent(env, raw):
    assert env.run("", raw=raw) is None


def test_slow_gh_is_bounded_and_parallel(env):
    """Three PRs whose gh calls each take 1s finish well under 3s in total."""
    env.set({f"jonathanmoregard/.claude#{n}": "OPEN" for n in (1, 2, 3)}, sleep=1)
    t0 = time.monotonic()
    assert env.run(f"Awaiting: {CLA}/1 {CLA}/2 {CLA}/3") is None
    assert time.monotonic() - t0 < 2.5


# --- invariant B: a merge ask carries the link ----------------------------
# The quoted replies are real: they are the messages this session actually
# sent, which is what makes them worth pinning. The lapse was measured at
# 2/37 merge-ask references, so a synthetic-only suite would be testing a
# shape nobody writes.


def test_the_real_lapse_blocks(env):
    """Verbatim from 2026-11-02: #395 linked, #41 and #394 named bare."""
    msg = ("PR #41: `failed=0`, mergeable, awaiting your review. #394 and #395 "
           "have CI running. The merge clicks are all yours.")
    out = env.run(msg)
    assert out["decision"] == "block"
    assert "#41" in out["reason"] and "#394" in out["reason"]
    assert env.calls() == []  # pure text: invariant B never calls gh


def test_bare_numbers_under_an_ask_lead_in_block(env):
    msg = "Both are green, the merge clicks are yours:\n1. #37\n2. #41\n"
    out = env.run(msg)
    assert out["decision"] == "block"
    assert "#37" in out["reason"] and "#41" in out["reason"]


def test_same_list_with_urls_at_the_bottom_passes(env):
    """The shape `finishing-up` and `reporting-pr-state` prescribe."""
    env.set({"jonathanmoregard/.claude#37": "OPEN", "jonathanmoregard/.claude#41": "OPEN"})
    msg = (f"Both are green, the merge clicks are yours:\n1. #37\n2. #41\n\n"
           f"{CLA}/37\n{CLA}/41\n")
    assert env.run(msg) is None


def test_pr_word_form_without_a_url_blocks(env):
    assert env.run("PR 41 is ready for your merge.")["decision"] == "block"


def test_short_form_is_not_a_clickable_link(env):
    """`owner/repo#41` resolves for invariant A but is not something to click."""
    env.set({"jonathanmoregard/.claude#41": "OPEN"})
    out = env.run("jonathanmoregard/.claude#41 is ready for your review.")
    assert out["decision"] == "block" and "#41" in out["reason"]


def test_ask_that_links_nothing_at_all_only_notices(env):
    """42% precision on real traffic: a notice, never a block. See the hook."""
    out = env.run("All four lanes are green and it is ready for your merge.")
    assert "decision" not in out
    assert "links no PR at all" in out["systemMessage"]
    assert env.calls() == []


def test_negated_ask_passes(env):
    assert env.run("All green. Nothing is awaiting your merge click.") is None
    assert env.run("Don't merge yet: CI is still running.") is None


def test_pr_number_inside_a_code_fence_is_not_an_ask(env):
    env.set({"jonathanmoregard/.claude#41": "OPEN"})
    msg = ("Commit message, ready for your review:\n\n```\n"
           "fix(dcg): the remedy names a unit\n\nFixes #389\n```\n\n"
           f"{CLA}/41\n")
    assert env.run(msg) is None


def test_quoted_user_words_are_not_this_reply_asking(env):
    msg = "> merge #41 please\n\nDone — merged at 10:07 and deployed."
    assert env.run(msg) is None
    assert env.calls() == []


def test_issue_number_with_its_issue_url_is_not_a_pr(env):
    env.set({"jonathanmoregard/.claude#41": "OPEN"})
    msg = ("Ready for your review; it closes #389.\n\n"
           f"{CLA}/41\nhttps://github.com/jonathanmoregard/.claude/issues/389\n")
    assert env.run(msg) is None


def test_link_gap_never_blocks_twice_in_one_chain(env):
    out = env.run("PR 41 is ready for your merge.", stop_hook_active=True)
    assert "decision" not in out and "#41" in out["systemMessage"]


def test_both_invariants_broken_names_both(env):
    env.set(MERGED_339)
    msg = f"Awaiting your merge click:\n- {NIX}/339\n- #41\n"
    out = env.run(msg)
    assert out["decision"] == "block"
    assert "/339 is MERGED" in out["reason"] and "#41" in out["reason"]


def test_known_limit_a_list_index_on_an_ask_line_reads_as_a_pr(env):
    """Documented false positive, pinned so a change to it is deliberate.

    Costs one rewrite, and only a notice on the second Stop of the chain.
    """
    assert env.run("Merge #1 first, then the rest.")["decision"] == "block"


# Each of these pins a false positive measured on real transcripts; the
# comment is the text that produced it.


def test_a_count_is_not_an_imperative_ask(env):
    """'| 1b | dcg marker | ~30-50k | review 1 PR |' -- 1 is a count."""
    assert env.run("Item 1b: dcg marker, ~30-50k tokens, review 1 PR. #391 tracks it.") is None


def test_a_table_row_is_not_an_ask(env):
    msg = ("| # | Attempt | Outcome |\n"
           "| 1 | Prose in finishing-up | Same lapse: \"merge clicks: #37, #38\" |\n")
    assert env.run(msg) is None


def test_inline_json_is_not_an_ask(env):
    msg = '{"obstacles_to_autonomy":["Blocked on a merge click for PR 45 and #398"]}'
    assert env.run(msg) is None


def test_a_finished_pr_on_an_ask_line_is_not_asked_about(env):
    """'Merge clicks - <links>. Order matters only in that #35 already landed.'"""
    env.set({"jonathanmoregard/.claude#37": "OPEN"})
    msg = (f"Merge clicks — {CLA}/37. Order matters only in that #35 already landed.")
    assert env.run(msg) is None


def test_a_sentence_about_merge_clicks_is_not_an_ask(env):
    """'item 4 is blocked on a merge click no amount of iteration produces'."""
    msg = ("I didn't arm Ralph: it assumes one mission in one worktree, and "
           "item 4 is blocked on a merge click no amount of iteration produces.")
    assert env.run(msg) is None


def test_an_arrow_plan_naming_prs_to_merge_blocks(env):
    """'verify five ro mounts -> merge #381 -> merge #383 -> drop the brake'."""
    out = env.run("Then: verify five `ro` mounts → merge #381 → merge #383 → drop the brake.")
    assert out["decision"] == "block"
    assert "#381" in out["reason"] and "#383" in out["reason"]


def test_handing_the_list_back_is_an_ask(env):
    """Verbatim from the e2e: the reply answers, it never asks, and the four
    numbers are bare. The first vocabulary missed it and the e2e caught that."""
    msg = ("Mergeable (open, clean, checks green):\n\n1. widget #37\n2. widget #41\n"
           "3. infra #393\n4. infra #394\n\n#38 is already merged.")
    out = env.run(msg)
    assert out["decision"] == "block"
    for n in ("#37", "#41", "#393", "#394"):
        assert n in out["reason"]


def test_handing_the_list_back_with_urls_passes(env):
    env.set({"jonathanmoregard/.claude#37": "OPEN", "jonathanmoregard/.claude#41": "OPEN"})
    msg = (f"Mergeable (open, clean, checks green):\n\n1. #37 — {CLA}/37\n"
           f"2. #41 — {CLA}/41\n")
    assert env.run(msg) is None


def test_ready_to_merge_without_a_link_blocks(env):
    assert env.run("Ready to merge now, if you want to start there: #375.")["decision"] == "block"


def test_a_pr_that_was_not_ready_is_not_an_ask(env):
    """'#381 wasn't ready to merge' -- past tense, and a negation."""
    assert env.run("As of this morning #381 wasn't ready to merge.") is None


def test_mergeable_unknown_is_a_field_not_a_handback(env):
    """'`#393`/`#394` report `mergeable=UNKNOWN` -- GitHub computes it lazily'."""
    msg = "#393 and #394 report `mergeable=UNKNOWN` — GitHub computes that lazily."
    assert env.run(msg) is None
