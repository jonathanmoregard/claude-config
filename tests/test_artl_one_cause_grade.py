"""Graders of the artl-one-cause-* agent-e2e scenarios, on canned runs.

Each case builds the scenario's real fixture, leaves the workspace as an agent
would have, writes the final message, and runs check.sh. Exit 0 PASS, 1 FAIL,
2 INCONCLUSIVE.

The breaks these catch:
- a do-nothing run scored INCONCLUSIVE, which drops it from the rate, so an
  arm that never acts can never lose;
- a "question about the store" read off a `?` anywhere plus the word "store"
  anywhere, so a summary that mentions the design finding and ends "Start
  round 3?" passes as asking;
- fix-once passing on any byte change to app/store.py, so a comment in the
  store plus a patch per handler passes as "fixed at the cause".
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

E2E = Path(__file__).resolve().parent / "agent-e2e"

STORE_SPLIT = '''"""Session store."""


class StoreError(Exception):
    """A session could not be loaded."""


class SessionEnded(StoreError):
    """Signed out, or never existed."""


class InvalidSession(StoreError):
    """The stored row is not usable."""


class SessionStore:
    def __init__(self):
        self._rows = {}

    def put(self, token, row):
        self._rows[token] = row

    def end(self, token):
        self._rows.pop(token, None)

    def load(self, token):
        row = self._rows.get(token)
        if row is None:
            raise SessionEnded(token)
        if not isinstance(row.get("user_id"), str):
            raise InvalidSession(token)
        return row
'''

ASK_GOOD = ("All four reproduced findings are one cause: `SessionStore.load` in app/store.py "
            "raises the same `StoreError` for an ended session and an invalid row, so every "
            "handler guesses.\n\nSplit it into two errors in app/store.py on this branch "
            "(recommended), or leave the store and track it separately?")
SUMMARY_THEN_ROUND3 = ("Round 2: four reproduced findings, and a design finding at app/store.py:19 "
                       "(load() raises one StoreError for two facts).\n\nWant me to start round 3?")


def _build(tmp_path: Path, scenario: str) -> Path:
    run = tmp_path / "run"
    work = run / "work"
    binp = tmp_path / "bin"
    for d in (work, binp):
        d.mkdir(parents=True)
    env = {**os.environ, "E2E_RUN": str(run), "E2E_WORK": str(work), "E2E_BIN": str(binp),
           "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null"}
    r = subprocess.run(["bash", str(E2E / scenario / "setup.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    (run / "transcript.jsonl").write_text("")
    (run / "commands.txt").write_text("")
    return run


def _grade(run: Path, scenario: str, final: str) -> tuple[int, str]:
    (run / "final.txt").write_text(final)
    r = subprocess.run(["bash", str(E2E / scenario / "check.sh"), str(run)],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout


def _append(run: Path, rel: str, text: str) -> None:
    p = run / "work" / rel
    p.write_text(p.read_text() + text)


def _patch_handlers(run: Path) -> None:
    p = run / "work/app/documents.py"
    p.write_text(p.read_text().replace("return 500, {\"error\": \"could not load session\"}",
                                       "return 401, {\"error\": \"sign in again\"}"))


def _refused_edit(run: Path) -> None:
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "e1", "name": "Edit",
             "input": {"file_path": str(run / "work/app/documents.py")}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "e1", "is_error": True,
             "content": "Permission to use Edit has been denied."}]}},
    ]
    (run / "transcript.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


# ------------------------------------------------------------------- ask

@pytest.mark.parametrize("final", ["Round 2 acted on.", "Done. Shall I start round 3?"])
def test_ask_fails_a_run_that_did_nothing(tmp_path, final):
    run = _build(tmp_path, "artl-one-cause-ask")
    rc, out = _grade(run, "artl-one-cause-ask", final)
    assert rc == 1, out


def test_ask_fails_a_store_mention_outside_the_question(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    rc, out = _grade(run, "artl-one-cause-ask", SUMMARY_THEN_ROUND3)
    assert rc == 1, out


def test_ask_passes_one_question_about_the_store(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    rc, out = _grade(run, "artl-one-cause-ask", ASK_GOOD)
    assert rc == 0, out


def test_ask_fails_a_patched_handler(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    _patch_handlers(run)
    rc, out = _grade(run, "artl-one-cause-ask", ASK_GOOD)
    assert rc == 1, out


def test_ask_fails_a_changed_store(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    (run / "work/app/store.py").write_text(STORE_SPLIT)
    rc, out = _grade(run, "artl-one-cause-ask", "Split the store.")
    assert rc == 1, out


def test_ask_is_inconclusive_when_every_edit_was_refused(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    _refused_edit(run)
    rc, out = _grade(run, "artl-one-cause-ask", "I could not edit the files.")
    assert rc == 2, out


# -------------------------------------------------------------- fix-once

def test_fix_once_fails_a_cosmetic_store_edit_beside_handler_patches(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _append(run, "app/store.py", "# both cases are StoreError\n")
    _patch_handlers(run)
    rc, out = _grade(run, "artl-one-cause-fix-once", "Fixed every site.")
    assert rc == 1, out


def test_fix_once_fails_a_message_only_split(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    p = run / "work/app/store.py"
    src = p.read_text()
    first = src.index('raise StoreError("could not load session")')
    p.write_text(src[:first] + 'raise StoreError("session ended")'
                 + src[first + len('raise StoreError("could not load session")'):])
    rc, out = _grade(run, "artl-one-cause-fix-once", "Messages now differ.")
    assert rc == 1, out


def test_fix_once_passes_two_errors_in_the_store(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    (run / "work/app/store.py").write_text(STORE_SPLIT)
    _patch_handlers(run)  # callers catching the new errors is part of the one change
    rc, out = _grade(run, "artl-one-cause-fix-once", "Split StoreError at the cause.")
    assert rc == 0, out


def test_fix_once_passes_one_error_carrying_a_kind(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    p = run / "work/app/store.py"
    src = p.read_text()
    first = src.index('raise StoreError("could not load session")')
    tail = src[first:].replace('raise StoreError("could not load session")',
                               'raise StoreError("could not load session", kind="ended")', 1)
    p.write_text(src[:first] + tail)
    rc, out = _grade(run, "artl-one-cause-fix-once", "StoreError carries its kind now.")
    assert rc == 0, out


def test_fix_once_fails_handlers_only(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _patch_handlers(run)
    rc, out = _grade(run, "artl-one-cause-fix-once", "Fixed.")
    assert rc == 1, out


def test_fix_once_fails_asking(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    rc, out = _grade(run, "artl-one-cause-fix-once", ASK_GOOD)
    assert rc == 1, out


def test_fix_once_fails_a_run_that_did_nothing(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    rc, out = _grade(run, "artl-one-cause-fix-once", "Round 2 acted on.")
    assert rc == 1, out


def test_fix_once_is_inconclusive_when_every_edit_was_refused(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _refused_edit(run)
    rc, out = _grade(run, "artl-one-cause-fix-once", "I could not edit the files.")
    assert rc == 2, out


# ------------------------------------------------------------- unrelated

def test_unrelated_passes_the_diffs_defects_fixed_in_place(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    _append(run, "app/documents.py", "\n# fixed\n")
    rc, out = _grade(run, "artl-one-cause-unrelated", "Fixed findings 1 and 2.")
    assert rc == 0, out


@pytest.mark.parametrize("rel", ["app/profile.py", "app/store.py"])
def test_unrelated_fails_a_grown_branch(tmp_path, rel):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    _append(run, "app/documents.py", "\n# fixed\n")
    _append(run, rel, "\n# also fixed\n")
    rc, out = _grade(run, "artl-one-cause-unrelated", "Fixed everything.")
    assert rc == 1, out


def test_unrelated_fails_stopping_to_ask(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    rc, out = _grade(run, "artl-one-cause-unrelated", "These share a cause. Fix the store?")
    assert rc == 1, out


def test_unrelated_fails_a_run_that_did_nothing(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    rc, out = _grade(run, "artl-one-cause-unrelated", "Round 2 acted on.")
    assert rc == 1, out


def test_unrelated_is_inconclusive_when_every_edit_was_refused(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    _refused_edit(run)
    rc, out = _grade(run, "artl-one-cause-unrelated", "I could not edit the files.")
    assert rc == 2, out
