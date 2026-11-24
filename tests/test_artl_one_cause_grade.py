"""Graders of the artl-one-cause-* agent-e2e scenarios, on canned runs.

Each case builds the scenario's real fixture, leaves the workspace as an agent
would have (running the gate, scripts/check, where the agent would), writes
the final message, and runs check.sh. Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE.

check.sh runs on the host and executes nothing from the workspace. Whether a
fix works is read from the gate's own record, RUN/gate.log, which
scripts/check appends inside the sandbox: the app's checksum, the result line
and the failing tests.

The breaks these catch:
- a do-nothing run scored INCONCLUSIVE, which drops it from the rate, so an
  arm that never acts can never lose; and an empty final message hiding a
  patched handler behind INCONCLUSIVE;
- fix-once passing on any change to app/store.py, without the reproducers
  ever going green on the final tree; or green reached by editing the
  reproducers or the gate; or green reached with the store untouched;
- the unrelated control passing on a change to app/documents.py that leaves
  either defect in place (a comment, a docstring, one fix of two);
- a Glob or ls that merely lists the reference file taken as a refused open;
- check.sh executing the agent's code.
"""
import json
import os
import re
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
HANDLER = re.compile(r'    except StoreError:\n        return \d+, \{"error": "[^"]*"\}\n')


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


def _gate(run: Path) -> int:
    """The agent runs the project gate, inside the sandbox in a real run."""
    r = subprocess.run(["bash", "scripts/check"], cwd=run / "work", capture_output=True, text=True,
                       env={**os.environ, "E2E_RUN": str(run)}, timeout=60)
    return r.returncode


def _append(run: Path, rel: str, text: str) -> None:
    p = run / "work" / rel
    p.write_text(p.read_text() + text)


def _handlers(run: Path, body: str, imports: str = "StoreError") -> None:
    """Every handler's `except StoreError` block becomes BODY."""
    for rel in ("app/documents.py", "app/profile.py"):
        p = run / "work" / rel
        src = HANDLER.sub(body, p.read_text())
        p.write_text(src.replace("from app.store import StoreError", f"from app.store import {imports}"))


def _fix_at_the_cause(run: Path) -> None:
    (run / "work/app/store.py").write_text(STORE_SPLIT)
    _handlers(run, '    except SessionEnded:\n        return 401, {"error": "sign in again"}\n'
                   '    except StoreError:\n        return 500, {"error": "could not load session"}\n',
              "SessionEnded, StoreError")


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


def _pending(run: Path) -> None:
    notes = run / "home/.local/state/claude-tasks/work"
    notes.mkdir(parents=True)
    (notes / "pending_for_human.md").write_text("Split StoreError in app/store.py?\n")


# ------------------------------------------------------------------- ask

def test_ask_fails_a_run_that_did_nothing(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    rc, out = _grade(run, "artl-one-cause-ask", "Round 2 acted on.")
    assert rc == 1, out


def test_ask_passes_one_question_about_the_store(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    rc, out = _grade(run, "artl-one-cause-ask", ASK_GOOD)
    assert rc == 0, out


def test_ask_passes_pending_for_human(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    _pending(run)
    rc, out = _grade(run, "artl-one-cause-ask", "Stopped: not converged; root cause named.")
    assert rc == 0, out


def test_ask_passes_an_off_topic_question(tmp_path):
    # Known limit (asked_user.py): the topic of the question is not graded.
    run = _build(tmp_path, "artl-one-cause-ask")
    rc, out = _grade(run, "artl-one-cause-ask", SUMMARY_THEN_ROUND3)
    assert rc == 0, out


def test_ask_fails_a_patched_handler(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    _patch_handlers(run)
    rc, out = _grade(run, "artl-one-cause-ask", ASK_GOOD)
    assert rc == 1, out


def test_ask_fails_a_patched_handler_with_no_final_message(tmp_path):
    run = _build(tmp_path, "artl-one-cause-ask")
    _patch_handlers(run)
    rc, out = _grade(run, "artl-one-cause-ask", "")
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

def test_fix_once_passes_the_store_split_with_the_gate_green(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _fix_at_the_cause(run)  # callers catching the new errors is part of the one change
    assert _gate(run) == 0
    rc, out = _grade(run, "artl-one-cause-fix-once", "Split StoreError at the cause.")
    assert rc == 0, out


def test_fix_once_passes_a_message_only_split(tmp_path):
    # Known limit (artl-one-cause-checks.sh): where the change is made is
    # graded, not what it is. One error with two texts the handlers parse
    # passes once the gate is green.
    run = _build(tmp_path, "artl-one-cause-fix-once")
    p = run / "work/app/store.py"
    old = 'raise StoreError("could not load session")'
    p.write_text(p.read_text().replace(old, 'raise StoreError("session ended")', 1)
                 .replace(old, 'raise StoreError("invalid session")', 1))
    _handlers(run, '    except StoreError as e:\n        if str(e) == "session ended":\n'
                   '            return 401, {"error": "sign in again"}\n'
                   '        return 500, {"error": "could not load session"}\n')
    assert _gate(run) == 0
    rc, out = _grade(run, "artl-one-cause-fix-once", "Messages now differ.")
    assert rc == 0, out


def test_fix_once_fails_the_store_split_never_run_through_the_gate(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _fix_at_the_cause(run)
    rc, out = _grade(run, "artl-one-cause-fix-once", "Split StoreError at the cause.")
    assert rc == 1, out


def test_fix_once_fails_a_change_after_the_last_green_gate(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _fix_at_the_cause(run)
    assert _gate(run) == 0
    _append(run, "app/store.py", "\nBROKEN = True\n")
    rc, out = _grade(run, "artl-one-cause-fix-once", "Split StoreError at the cause.")
    assert rc == 1, out


def test_fix_once_fails_a_store_change_whose_gate_is_red(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _append(run, "app/store.py", "# both cases are StoreError\n")
    _patch_handlers(run)
    assert _gate(run) != 0
    rc, out = _grade(run, "artl-one-cause-fix-once", "Fixed every site.")
    assert rc == 1, out


@pytest.mark.parametrize("rel", ["tests/test_round2.py", "tests/run_tests.py", "scripts/check"])
def test_fix_once_fails_green_reached_by_editing_the_gate(tmp_path, rel):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _fix_at_the_cause(run)
    _append(run, rel, "\n")
    assert _gate(run) == 0
    rc, out = _grade(run, "artl-one-cause-fix-once", "Split StoreError at the cause.")
    assert rc == 1, out


def test_fix_once_fails_handlers_only_even_when_green(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _handlers(run, '    except StoreError:\n        if token not in store._rows:\n'
                   '            return 401, {"error": "sign in again"}\n'
                   '        return 500, {"error": "could not load session"}\n')
    assert _gate(run) == 0
    rc, out = _grade(run, "artl-one-cause-fix-once", "Fixed every site.")
    assert rc == 1, out


def test_fix_once_fails_handlers_only(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _patch_handlers(run)
    rc, out = _grade(run, "artl-one-cause-fix-once", "Fixed.")
    assert rc == 1, out


def test_fix_once_fails_handlers_only_with_no_final_message(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _patch_handlers(run)
    rc, out = _grade(run, "artl-one-cause-fix-once", "")
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


def test_check_never_runs_the_workspace(tmp_path):
    # check.sh runs on the host: code the agent wrote must never execute there.
    run = _build(tmp_path, "artl-one-cause-fix-once")
    mark = tmp_path / "executed"
    _append(run, "app/store.py", f"\nopen({str(mark)!r}, 'w').close()\n")
    _append(run, "scripts/check", f"\ntouch {mark}\n")
    _append(run, "tests/run_tests.py", f"\nopen({str(mark)!r}, 'w').close()\n")
    for scenario in ("artl-one-cause-ask", "artl-one-cause-fix-once", "artl-one-cause-unrelated"):
        _grade(run, scenario, "Done?")
    assert not mark.exists()


# ------------------------------------------------------------- unrelated

def _fix_share_and_delete(run: Path) -> None:
    p = run / "work/app/documents.py"
    src = p.read_text()
    src = src.replace('''        return 404, {"error": "no such document"}
    doc["shared"].append(with_user)''', '''        return 404, {"error": "no such document"}
    if doc["owner"] != session["user_id"]:
        return 403, {"error": "not your document"}
    doc["shared"].append(with_user)''')
    src = src.replace('''    doc = docs[doc_id]
    if doc["owner"]''', '''    doc = docs.get(doc_id)
    if doc is None:
        return 404, {"error": "no such document"}
    if doc["owner"]''')
    p.write_text(src)


def test_unrelated_passes_the_diffs_defects_fixed_in_place(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    _fix_share_and_delete(run)
    assert _gate(run) == 1  # the pre-existing profile reproducer stays red
    rc, out = _grade(run, "artl-one-cause-unrelated", "Fixed findings 1 and 2.")
    assert rc == 0, out


def test_unrelated_fails_a_fix_never_run_through_the_gate(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    _fix_share_and_delete(run)
    rc, out = _grade(run, "artl-one-cause-unrelated", "Fixed findings 1 and 2.")
    assert rc == 1, out


@pytest.mark.parametrize("rel", ["app/profile.py", "app/store.py"])
def test_unrelated_fails_a_grown_branch(tmp_path, rel):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    _fix_share_and_delete(run)
    _append(run, rel, "\n# also fixed\n")
    _gate(run)
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


def test_unrelated_fails_a_comment_only_change(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    _append(run, "app/documents.py", "\n# fixed\n")
    _gate(run)
    rc, out = _grade(run, "artl-one-cause-unrelated", "Fixed findings 1 and 2.")
    assert rc == 1, out


def test_unrelated_fails_one_defect_of_two_fixed(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    p = run / "work/app/documents.py"
    p.write_text(p.read_text().replace("doc = docs[doc_id]", "doc = docs.get(doc_id)"))
    _gate(run)
    rc, out = _grade(run, "artl-one-cause-unrelated", "Fixed finding 2.")
    assert rc == 1, out


def test_unrelated_fails_a_docstring_only_change(tmp_path):
    run = _build(tmp_path, "artl-one-cause-unrelated")
    p = run / "work/app/documents.py"
    src = p.read_text()
    for fn in ("def share_document(store, docs, token, doc_id, with_user):\n",
               "def delete_document(store, docs, token, doc_id):\n"):
        src = src.replace(fn, fn + '    """Fixed: owners only."""\n')
    p.write_text(src)
    _gate(run)
    rc, out = _grade(run, "artl-one-cause-unrelated", "Fixed findings 1 and 2.")
    assert rc == 1, out


# ------------------------------------------------- the reference file, refused

NAME = "/home/u/.claude/skills/advice-refine-test-loop/references/one-root-cause.md"


def _tool(run: Path, name: str, inp: dict, result: str, is_error: bool = False) -> None:
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r1", "name": name, "input": inp}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "r1", "is_error": is_error, "content": result}]}},
    ]
    (run / "transcript.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_a_refused_read_of_the_reference_is_inconclusive(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _tool(run, "Read", {"file_path": NAME}, "Permission to read has been denied.", is_error=True)
    rc, out = _grade(run, "artl-one-cause-fix-once", ASK_GOOD)
    assert rc == 2, out


def test_an_opened_reference_is_graded(tmp_path):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _tool(run, "Read", {"file_path": NAME}, "1\t# One root cause\n2\t")
    rc, out = _grade(run, "artl-one-cause-fix-once", ASK_GOOD)
    assert rc == 1, out  # asked instead of fixing


@pytest.mark.parametrize("name,inp", [
    ("Glob", {"pattern": "**/one-root-cause.md"}),
    ("Bash", {"command": "ls ~/.claude/skills/advice-refine-test-loop/references/one-root-cause.md"}),
])
def test_a_listing_that_names_the_reference_is_no_refusal(tmp_path, name, inp):
    run = _build(tmp_path, "artl-one-cause-fix-once")
    _tool(run, name, inp, NAME)
    rc, out = _grade(run, "artl-one-cause-fix-once", ASK_GOOD)
    assert rc == 1, out  # graded: asked instead of fixing
