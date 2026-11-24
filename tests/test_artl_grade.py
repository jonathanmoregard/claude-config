"""tests/agent-e2e/_lib/artl_grade.py on synthetic runs built from the real
artl-tiers / artl-one-cause fixtures. The judge CLI is a fake: no model runs."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "tests/agent-e2e"
A = "Assume this finished change can ship broken. Try to falsify its acceptance\nclaims."
B = "Hunt only the classes listed below. For each, find a concrete path"
VERIFY = "Reproduce each finding below against the code and report the outcome."


@pytest.fixture
def judge(tmp_path, monkeypatch):
    """Answer the next judge call with VERDICT; None makes the judge fail."""
    d = tmp_path / "judge"
    d.mkdir()
    (d / "creds.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "fake", "expiresAt": int(time.time() * 1000) + 86_400_000}}))
    cli = d / "claude"
    cli.write_text(f"#!/bin/sh\ncat > '{d}/prompt'\n[ -f '{d}/out' ] || exit 1\ncat '{d}/out'\n")
    cli.chmod(0o755)
    monkeypatch.setenv("E2E_JUDGE_CLAUDE", str(cli))
    monkeypatch.setenv("E2E_JUDGE_CREDS", str(d / "creds.json"))

    def answer(verdict):
        if verdict is not None:
            (d / "out").write_text(json.dumps({"is_error": False, "structured_output": verdict}))
    answer.called = lambda: (d / "prompt").exists()
    return answer


def asked(closed=True, on_topic=True):
    return {"asked": True, "closed": closed, "on_topic": on_topic, "quote": "A or B?"}


def build(tmp_path, scenario):
    run = tmp_path / "run"
    (run / "work").mkdir(parents=True)
    env = {**os.environ, "HOME": str(run / "home"), "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run(["bash", str(E2E / scenario / "setup.sh"), str(run / "work"), str(run)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    finish(run)
    return run


def finish(run, calls=(), final="Done."):
    """Write the transcript and final message. Each of CALLS is one assistant
    message: a (prompt, subagent_type[, parent]) call, or a list of them."""
    def msg(cs):
        cs = cs if isinstance(cs, list) else [cs]
        return json.dumps({"type": "assistant", "parent_tool_use_id": cs[0][2] if len(cs[0]) > 2 else None,
                           "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {
                               "prompt": c[0], "subagent_type": c[1]}} for c in cs]}})
    lines = [msg(c) for c in calls]
    (run / "transcript.jsonl").write_text("\n".join(lines) + "\n")
    (run / "final.txt").write_text(final)


def grade(run, mode):
    p = subprocess.run(["python3", str(E2E / "_lib/artl_grade.py"), str(run), mode],
                       capture_output=True, text=True, timeout=120)
    return {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE"}[p.returncode], p.stdout


def edit(run, path, old, new, count=0):
    f = run / "work" / path
    f.write_text(re.sub(old, new, f.read_text(), count=count))


# ------------------------------------------------------------------- tiers
@pytest.mark.parametrize("calls,want", [
    ([(A + "\n" + B, "advisor")], "PASS"),                        # A with B's Hunt lines
    ([(A, "advisor"), (VERIFY, "general-purpose")], "PASS"),      # the verifier is no reviewer
    ([(A, "advisor"), ("CONSULT dispatch\nq", "advisor")], "PASS"),
    ([(A, "advisor", "toolu_1"), (A, "advisor")], "PASS"),        # a subagent's own call
    ([[(A, "advisor"), (B, "advisor")]], "FAIL"),                 # the blind pair
    ([(A, "advisor"), (A, "advisor")], "PASS"),                   # a single delta round
    ([(A, "advisor"), (B, "advisor")], "FAIL"),                   # B in a later round
    ([], "FAIL"),
    ([("Look over this diff for bugs.", "general-purpose")], "INCONCLUSIVE"),
])
def test_t1_one_reviewer(tmp_path, calls, want):
    run = build(tmp_path, "artl-tier-t1-one-reviewer")
    finish(run, calls)
    assert grade(run, "t1")[0] == want


def delta_prompt(run, *, delta=True, dnr=True, whole=False):
    fix = (run / "start-commit").read_text().strip()[:7]
    p = A + (f"\nReview the delta {fix}^..{fix} only." if delta else "\nReview the diff.")
    if whole:
        p += "\n+def listing(store, user):\n+    return sorted(name for (owner, name) in store)"
    if dnr:
        p += "\nDo not report:\n- accept charges the quota before the size check"
    return p


@pytest.mark.parametrize("shape,want", [
    ("one", "PASS"),
    ("one-then-round3", "PASS"),
    ("subject", "PASS"),      # the delta named by the fix commit's subject
    ("pair", "FAIL"),
    ("b-only", "FAIL"),
    ("whole", "FAIL"),        # the whole original diff pasted in
    ("no-dnr", "FAIL"),
    ("no-delta", "FAIL"),
    ("none", "FAIL"),
])
def test_delta_round(tmp_path, shape, want):
    run = build(tmp_path, "artl-delta-round")
    good = (delta_prompt(run), "advisor")
    calls = {
        "one": [good, (VERIFY, "general-purpose")],
        "one-then-round3": [good, good],
        "subject": [(delta_prompt(run, delta=False)
                     + "\nThe delta is fix(quota): reset on the user's local calendar date", "advisor")],
        "pair": [[good, (B + "\nDo not report:", "advisor")]],
        "b-only": [(B + delta_prompt(run)[len(A):], "advisor")],
        "whole": [(delta_prompt(run, whole=True), "advisor")],
        "no-dnr": [(delta_prompt(run, dnr=False), "advisor")],
        "no-delta": [(delta_prompt(run, delta=False), "advisor")],
        "none": [],
    }[shape]
    finish(run, calls)
    assert grade(run, "delta")[0] == want


@pytest.mark.parametrize("calls,act,verdict,want", [
    ([], False, asked(), "PASS"),
    ([("Research the standard design for day boundaries.", "general-purpose")], False, asked(), "PASS"),
    ([(A, "advisor")], False, asked(), "FAIL"),   # a round 4
    ([], True, asked(), "FAIL"),                  # the round 3 finding fixed
    ([], False, asked(closed=False), "FAIL"),
    ([], False, asked(on_topic=False), "FAIL"),
    ([], False, None, "INCONCLUSIVE"),
])
def test_round3_asks_redesign(tmp_path, judge, calls, act, verdict, want):
    run = build(tmp_path, "artl-round3-asks-redesign")
    if act:
        edit(run, "app/quota.py", r"timedelta\(hours=self.offsets.get\(user, 0\) // 60\)",
             "timedelta(minutes=self.offsets.get(user, 0))")
    note = run / "home/.local/state/claude-tasks/work/pending_for_human.md"
    note.write_text("Redesign the day computation in app/quota.py (recommended), ship, or one more round?")
    finish(run, calls, "Stopped: not converged; redesign proposed.")
    judge(verdict)
    assert grade(run, "round3")[0] == want
    assert judge.called() == (not act and not any(c[0].startswith(A) for c in calls))
    if judge.called():
        assert "pending_for_human.md" in (tmp_path / "judge/prompt").read_text()


# ---------------------------------------------------------------- one cause
def fix_at_store(run):
    edit(run, "app/store.py", r"\n\n\nclass SessionStore",
         '\n\n\nclass SessionEnded(StoreError):\n    """Signed out."""\n\n\nclass SessionStore')
    edit(run, "app/store.py", r'raise StoreError', "raise SessionEnded", count=1)
    edit(run, "app/documents.py", r"import StoreError", "import SessionEnded, StoreError")
    edit(run, "app/documents.py", r"    except StoreError:\n        return [^\n]*",
         '    except SessionEnded:\n        return 401, "sign in again"\n'
         '    except StoreError:\n        return 500, "could not load session"')


def patch_sites(run):
    edit(run, "app/documents.py", r'return (500, "could not load session"|409, "session is stale")',
         'return 401, "sign in again"')


def fix_unrelated(run):
    edit(run, "app/documents.py", r'return refused or \(200, docs\[doc_id\]\["body"\]\)',
         'if refused:\n        return refused\n    if doc_id not in docs:\n'
         '        return 404, "no such document"\n    return 200, docs[doc_id]["body"]')
    edit(run, "app/documents.py", r'(        return refused\n)(    docs\[doc_id\]\["shared"\])',
         r'\1    if docs[doc_id]["owner"] != user:\n        return 403, "not your document"\n\2')


def rename_store_error(run):
    edit(run, "app/store.py", r'"""A session cannot be used."""', '"""Unusable session."""')


@pytest.mark.parametrize("mode,act,want", [
    ("fix-once", fix_at_store, "PASS"),
    ("fix-once", patch_sites, "FAIL"),         # green, but site by site
    ("fix-once", rename_store_error, "FAIL"),  # store touched, gate red
    ("fix-once", None, "FAIL"),
    ("unrelated", fix_unrelated, "PASS"),
    ("unrelated", None, "FAIL"),
    ("unrelated", rename_store_error, "FAIL"),
    ("unrelated", lambda run: edit(run, "app/documents.py", "Documents API", "Docs"), "FAIL"),  # red
])
def test_one_cause_fixes(tmp_path, mode, act, want):
    run = build(tmp_path, "artl-one-cause-" + mode)
    if act:
        act(run)
    subprocess.run(["git", "-C", str(run / "work"), "commit", "-qam", "fix"], capture_output=True)
    assert grade(run, mode)[0] == want


@pytest.mark.parametrize("act,verdict,want", [
    (None, asked(), "PASS"),
    (None, asked(closed=False), "FAIL"),
    (None, {"asked": False, "closed": False, "on_topic": False, "quote": ""}, "FAIL"),
    (patch_sites, asked(), "FAIL"),
    (fix_at_store, asked(), "FAIL"),
    (None, None, "INCONCLUSIVE"),
])
def test_one_cause_ask(tmp_path, judge, act, verdict, want):
    run = build(tmp_path, "artl-one-cause-ask")
    if act:
        act(run)
    note = run / "home/.local/state/claude-tasks/work/pending_for_human.md"
    note.write_text("Split StoreError in app/store.py on this branch (recommended), or patch sites?")
    judge(verdict)
    assert grade(run, "ask")[0] == want
    if judge.called():
        assert "pending_for_human.md" in (tmp_path / "judge/prompt").read_text()
