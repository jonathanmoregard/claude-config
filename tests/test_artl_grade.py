"""tests/agent-e2e/_lib/artl_grade.py on synthetic runs built from the real
artl-breaker / artl-one-cause fixtures. The judge CLI is a fake: no model runs."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "tests/agent-e2e"
CHECK = ROOT / "runtime/skills/advice-refine-test-loop/scripts/convergence-check.mts"
V = "VALIDATION dispatch\nCluster these findings into threads across rounds."
R = "Assume this finished change can ship broken."


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
    """Write the transcript (CALLS: (prompt, subagent_type[, parent])) and final message."""
    lines = [json.dumps({"type": "assistant", "parent_tool_use_id": c[2] if len(c) > 2 else None,
                         "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {
                             "prompt": c[0], "subagent_type": c[1]}}]}}) for c in calls]
    (run / "transcript.jsonl").write_text("\n".join(lines) + "\n")
    (run / "final.txt").write_text(final)


def grade(run, mode):
    p = subprocess.run(["python3", str(E2E / "_lib/artl_grade.py"), str(run), mode],
                       capture_output=True, text=True, timeout=120)
    return {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE"}[p.returncode], p.stdout


def edit(run, path, old, new, count=0):
    f = run / "work" / path
    f.write_text(re.sub(old, new, f.read_text(), count=count))


# ------------------------------------------------------------------ breaker
@pytest.mark.parametrize("mode,calls,want", [
    ("fires", [(V, "advisor"), (R, "advisor")], "PASS"),
    ("fires", [("CONSULT dispatch\nq", "advisor"), ("x", "worker-hard"), (V, "advisor")], "PASS"),
    ("fires", [(R, "Explore", "toolu_1"), (V, "advisor")], "PASS"),  # a subagent's own call
    ("fires", [("find the ledger", "Explore"), (V, "advisor")], "PASS"),  # not a reviewer
    ("fires", [(R, "advisor"), (V, "advisor")], "FAIL"),
    ("fires", [], "FAIL"),
    ("holds", [(R, "advisor"), (V, "advisor")], "PASS"),
    ("holds", [(V, "advisor"), (R, "advisor")], "FAIL"),
    ("holds", [("x", "worker-easy")], "FAIL"),
])
def test_breaker_dispatch_order(tmp_path, mode, calls, want):
    run = tmp_path / "run"
    run.mkdir()
    finish(run, calls)
    assert grade(run, mode)[0] == want


@pytest.mark.parametrize("mode,verdict", [("fires", "fire"), ("asks", "fire"), ("holds", "hold")])
def test_seeded_ledger_meets_the_convergence_check(tmp_path, mode, verdict):
    run = build(tmp_path, f"artl-breaker-{mode}")
    ledger = run / "home/.local/state/claude-tasks/work/review-ledger.tsv"
    tok = ledger.read_text().splitlines()[1].split("\t")[2].split("/")[0]
    r = subprocess.run(["node", str(CHECK), str(ledger), f"{tok}/r1", f"{tok}/r2"],
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.splitlines()[-1].split("\t")[0] == verdict, r.stdout + r.stderr


@pytest.mark.parametrize("calls,edits,verdict,want", [
    ([(V, "advisor")], False, asked(), "PASS"),
    ([], False, asked(closed=False), "FAIL"),
    ([], False, asked(on_topic=False), "FAIL"),
    ([(R, "advisor")], False, asked(), "FAIL"),
    ([], True, asked(), "FAIL"),
    ([], False, None, "INCONCLUSIVE"),
])
def test_breaker_asks(tmp_path, judge, calls, edits, verdict, want):
    run = build(tmp_path, "artl-breaker-asks")
    finish(run, calls, "Replace the regex grader with an LLM judge (billed), or document its limit?")
    if edits:
        edit(run, "src/asks.py", "no need to", "no need to|never")
    judge(verdict)
    assert grade(run, "asks")[0] == want
    assert judge.called() == (not edits and all(c[0] == V for c in calls))  # acting fails first


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
