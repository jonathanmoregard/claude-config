"""The SessionStart hook that tells a session its memory is dead.

WHAT THIS GUARDS. Between 2026-08-27 and 2026-08-30 the aggregator's MCP
reader required cache schema 6 while the packaged writer built 5, so
``aggregator_search_memory`` returned ``{"ok": false}`` on every call. No
session ever said so. Each one asked the index a question, got a refusal, and
quietly fell back to grepping ``~/.claude/projects/*.jsonl`` — a strict subset
of what the index covers — so every answer came back thinner with no way to
tell. Worse, ``aggregator-recall-hook.py`` was actively steering recall-shaped
prompts at the dead tool the whole time.

The hook under test is the announcement that was missing. Its two failure
modes are opposites and both are silent, which is why each gets a test here:

  * speaking when healthy — burns the one budget it has. Seven other
    SessionStart entries already compete for attention, and a check that fires
    every session is one nobody reads on the session it matters.
  * staying silent when it could not tell — the failure this whole incident
    was made of. Silence has to mean "checked and healthy", never "could not
    tell", or it stops carrying any information at all.

Every test below was run RED first, against a hook that did not exist and then
against variants that always spoke and always stayed quiet.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT

HOOK = Path(REPO_ROOT) / "hooks" / "aggregator-recall-health.py"

# The probe the hook drives. It ships in the aggregator repo, beside the schema
# it describes, so a schema bump and the check that understands it move in one
# commit. These tests stub it rather than importing it: the hook's contract is
# "run this file, read its JSON", and stubbing is the only way to exercise the
# branches where that file is missing, hangs, or answers rubbish — which are
# three of the hook's five paths and the three that used to be untested.
PROBE_STUB = """\
import json, sys
print(json.dumps(%s))
sys.exit(%d)
"""


def _write_probe(tmp_path: Path, payload: dict, exit_code: int) -> Path:
    p = tmp_path / "probe_stub.py"
    p.write_text(PROBE_STUB % (json.dumps(payload), exit_code), encoding="utf-8")
    return p


def _run(probe: Path | str, *, stdin: str = "{}") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AGGREGATOR_SCHEMA_PROBE"] = str(probe)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _payload(state: str, severity: str, summary: str = "because reasons") -> dict:
    return {"state": state, "severity": severity, "summary": summary, "findings": []}


def test_healthy_is_completely_silent(tmp_path):
    """The earned silence: no stdout at all, not even an empty JSON object.

    An empty object would still render as a hook that ran and had nothing to
    say, and the point is that a healthy check is indistinguishable from no
    check at all until the day it fires.
    """
    proc = _run(_write_probe(tmp_path, _payload("fine", "silent"), 0))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"the hook spoke on a healthy machine: {proc.stdout!r}"


def test_dead_recall_reaches_both_the_user_and_the_agent(tmp_path):
    """Two readers, two channels, and the message has to land in both.

    ``systemMessage`` is for the human at the terminal; ``additionalContext``
    is for the model, and it is the one that actually matters here, because
    the degraded party is the agent. A version that emitted only the first
    would look right in a terminal and change nothing about how the session
    reasoned.
    """
    proc = _run(
        _write_probe(tmp_path, _payload("dead", "down", "cache 5, reader 6"), 20)
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert "cache 5, reader 6" in doc["systemMessage"]
    hso = doc["hookSpecificOutput"]
    # Exactly "SessionStart": a mismatched or missing hookEventName makes the
    # harness drop the whole payload silently, which would reproduce the bug
    # this hook exists to fix, one layer up.
    assert hso["hookEventName"] == "SessionStart"
    assert "cache 5, reader 6" in hso["additionalContext"]
    assert "REFUSING" in doc["systemMessage"] or "DOWN" in doc["systemMessage"]


def test_will_rot_is_announced_but_not_as_an_outage(tmp_path):
    """Recall works this minute; the writer re-stamps it down on the next tick.

    Distinct wording from the DOWN case on purpose. Telling the operator that
    recall is refusing when it is not trains them to discount the message, and
    a discounted health check is a disabled one.
    """
    proc = _run(_write_probe(tmp_path, _payload("will-rot", "reduced"), 10))
    doc = json.loads(proc.stdout)
    assert "next ingest tick" in doc["systemMessage"]
    assert "REFUSING" not in doc["systemMessage"].upper()


@pytest.mark.parametrize(
    "probe_factory,expected",
    [
        pytest.param(lambda t: t / "does-not-exist.py", "MISSING", id="probe-absent"),
        pytest.param(
            lambda t: _write_bad(t, 'print("definitely not json")'),
            "cannot read",
            id="probe-answers-rubbish",
        ),
        pytest.param(
            lambda t: _write_bad(t, "import sys; sys.exit(3)"),
            "cannot read",
            id="probe-answers-nothing",
        ),
        pytest.param(
            lambda t: _write_bad(t, 'import json; print(json.dumps({"state": "fine"}))'),
            "cannot read",
            id="probe-answers-half-a-verdict",
        ),
    ],
)
def test_a_probe_it_cannot_use_warns_rather_than_going_quiet(
    tmp_path, probe_factory, expected
):
    """UNKNOWN means warn. Every one of these used to be a plausible ``return 0``.

    The tempting shape for all four is "the probe didn't work, so say nothing
    and don't bother anyone". That is precisely the reasoning that let a dead
    index run for three days: nothing was reported, so nothing looked wrong.
    A check that goes quiet when its own machinery breaks is indistinguishable
    from one reporting good news.

    The last case is the subtle one — well-formed JSON that is missing
    ``summary``. A hook that reached for ``verdict["state"]`` alone would sail
    past it and then crash or print a bare headline with no content.
    """
    proc = _run(probe_factory(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "the hook went silent on a probe it could not use"
    doc = json.loads(proc.stdout)
    assert "UNVERIFIED" in doc["systemMessage"]
    assert expected in doc["systemMessage"]
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def _write_bad(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "bad_probe.py"
    p.write_text(body + "\n", encoding="utf-8")
    return p


def test_a_hanging_probe_is_reported_not_waited_on(tmp_path):
    """A stalled check is not a check.

    A hook that outruns the harness timeout has its output DISCARDED, which
    for a health check is the same as never noticing — so the hook bounds the
    probe itself and reports the timeout rather than letting the harness eat
    the whole thing. Uses a 1 s override so the test does not pay the real
    5 s bound.
    """
    p = tmp_path / "slow_probe.py"
    p.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    env = dict(os.environ)
    env["AGGREGATOR_SCHEMA_PROBE"] = str(p)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy,sys;"
            "sys.argv=['hook'];"
            "import importlib.util as u;"
            f"spec=u.spec_from_file_location('h', {str(HOOK)!r});"
            "m=u.module_from_spec(spec);spec.loader.exec_module(m);"
            "m.PROBE_TIMEOUT_S=1.0;"
            "sys.exit(m.main())",
        ],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "UNVERIFIED" in proc.stdout
    assert "did not answer" in proc.stdout


def test_malformed_stdin_does_not_break_startup(tmp_path):
    """The harness payload is read and discarded; garbage must not be fatal.

    This check depends on the machine's state, never on what the session says,
    so unparseable stdin is irrelevant to its verdict — and a health check
    that crashes the session it was meant to inform is worse than no check.
    """
    proc = _run(
        _write_probe(tmp_path, _payload("fine", "silent"), 0),
        stdin="this is not json {{{",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_hook_is_registered_and_enforcing():
    """Shipped ON. Not behind an env var, not shadow, not commented out.

    A gate that does nothing unless a variable is set is not an
    implementation, and the registration is the half most likely to be lost in
    a merge — the hook file would survive review while the thing that runs it
    quietly did not.
    """
    settings = json.loads((Path(REPO_ROOT) / "settings.json").read_text())
    commands = [
        h.get("command", "")
        for entry in settings["hooks"]["SessionStart"]
        for h in entry.get("hooks", [])
    ]
    mine = [c for c in commands if "aggregator-recall-health.py" in c]
    assert mine, (
        "the recall-health hook is not registered in settings.json "
        f"SessionStart; entries present: {commands}"
    )
    assert len(mine) == 1, f"registered more than once: {mine}"
    # No entrypoint guard and no env-var gate in front of it. Both are the
    # shapes that turn an enforcing check into a decorative one, and both look
    # fine in review.
    assert "||" not in mine[0] and "exit 0" not in mine[0], (
        f"the hook is registered behind a guard clause: {mine[0]!r}"
    )


def test_hook_is_stdlib_only_and_never_runs_the_aggregator_cli():
    """Two prohibitions that are one prohibition.

    ``cli.py`` calls ``migrate()`` on every subcommand but ``embed``, and
    ``migrate()`` WRITES ``PRAGMA user_version`` — so any hook that shelled out
    to ``aggregator status`` to check the cache would re-stamp it at the
    writer's old version, i.e. perform the damage it was checking for, on
    every session start. And importing the aggregator package would drag in
    torch on a ten-second budget.
    """
    source = HOOK.read_text(encoding="utf-8")
    assert "import aggregator" not in source and "from aggregator" not in source
    for forbidden in ("aggregator status", "aggregator ingest", "aggregator query"):
        # Prose in the docstring names these deliberately; what must not appear
        # is one inside a subprocess argument list.
        assert f'"{forbidden}"' not in source and f"'{forbidden}'" not in source, (
            f"the hook appears to invoke `{forbidden}`, which migrates the cache"
        )


def test_end_to_end_against_a_real_probe_and_a_real_cache(tmp_path):
    """The one test that wires the real probe to a real SQLite file.

    Everything above stubs the probe, which is right for the hook's own
    branches but leaves the seam between them untested — and a seam is where
    the last incident lived. This forges a schema-5 cache against a schema-6
    reader, the exact live shape, and asserts the announcement comes out the
    far end. Skips rather than fails when the aggregator checkout is absent,
    because that is an environment fact, not a regression in this repo.
    """
    probe = _locate_real_probe()
    if probe is None:
        pytest.skip("no aggregator checkout with health/schema_probe.py available")

    cache = tmp_path / "cache.db"
    con = sqlite3.connect(cache)
    con.execute("PRAGMA user_version = 5")
    con.commit()
    con.close()

    core = tmp_path / "reader" / "aggregator" / "core"
    core.mkdir(parents=True)
    (core / "store.py").write_text("SCHEMA_VERSION = 6\n", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "AGGREGATOR_SCHEMA_PROBE": str(probe),
            "AGGREGATOR_CACHE_DB": str(cache),
            "AGGREGATOR_READER_DIR": str(tmp_path / "reader"),
            "AGGREGATOR_WRITER_BIN": str(tmp_path / "no-writer"),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "the real probe reported a dead cache and the hook said nothing"
    doc = json.loads(proc.stdout)
    assert "5" in doc["systemMessage"] and "6" in doc["systemMessage"]


def _locate_real_probe() -> Path | None:
    for base in (
        os.environ.get("AGGREGATOR_READER_DIR"),
        str(Path.home() / "Repos" / "aggregator"),
    ):
        if not base:
            continue
        p = Path(base) / "aggregator" / "health" / "schema_probe.py"
        if p.is_file():
            return p
    return None
