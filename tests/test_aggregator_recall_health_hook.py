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

import importlib.util
import json
import os
import sqlite3
import stat
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


# ---------------------------------------------------------------------------
# WHICH probe: the packaged sibling, or a developer checkout
# ---------------------------------------------------------------------------
# THE DEFECT THIS FIXES (2026-09-05). ``_reader_dir`` understood exactly one
# shape of ``mcpServers.aggregator`` — ``args: [..., "--directory", <dir>, ...]``
# — and the live entry has been ``{"command": ".../bin/aggregator-mcp",
# "args": []}`` for weeks. So the hook fell through to its ``~/Repos/aggregator``
# fallback and measured a WORKING TREE: a tree sitting on a feature branch at
# schema 7 while the packaged reader, the writer and the cache all agreed on 6.
# The hook announced "AGGREGATOR RECALL IS DOWN" at session start about a
# reader that Claude Code does not run and never ran.
#
# A false alarm is not a smaller version of a missed alarm — it is the thing
# that turns the next true one into noise, and this check's entire budget is
# its silence. So the hook now prefers the PRODUCTION artifact: the
# ``aggregator-schema-probe`` console script installed next to the very
# ``aggregator-mcp`` binary named by ``~/.claude.json``. The checkout logic
# stays for developer configs that really do launch the reader with
# ``--directory``.
#
# Every test below monkeypatches ``HOME`` before importing the hook, because
# ``DEFAULT_READER_DIR`` is computed at import time; a test that imported first
# would silently measure the operator's real checkout.

PROBE_STUB_SH = """\
#!/bin/sh
printf '%s\\n' '{"state": "dead", "severity": "down", "summary": "cache 5, reader 6"}'
exit 20
"""


def _load_hook():
    """Import the hook as a module so its resolution can be asserted directly.

    The argv is the thing under test — ``[<sibling>, "--json"]`` versus
    ``[sys.executable, <file>, "--json"]`` — and a subprocess cannot show it.
    """
    spec = importlib.util.spec_from_file_location("recall_health_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_exec(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_home(tmp_path: Path, entry: dict | None) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    doc = {"mcpServers": {"aggregator": entry}} if entry is not None else {}
    (home / ".claude.json").write_text(json.dumps(doc), encoding="utf-8")
    return home


def _isolate(monkeypatch, home: Path, path_dir: Path) -> None:
    """A hermetic environment: no real HOME, no real PATH, no ambient overrides.

    ``PATH`` matters as much as ``HOME`` here. Once the packaged probe ships,
    it lands in the same profile ``bin/`` as ``uv``, so a test that left the
    host PATH in place would start resolving a real sibling and stop testing
    what it says it tests.
    """
    path_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.delenv("AGGREGATOR_SCHEMA_PROBE", raising=False)
    monkeypatch.delenv("AGGREGATOR_READER_DIR", raising=False)


def test_the_packaged_probe_beside_the_reader_binary_wins(tmp_path, monkeypatch):
    """The live shape: an absolute ``command``, empty ``args``, a real sibling.

    This is the case that was reported as an outage every session. There is no
    ``--directory`` anywhere in the config, and the right answer is not a
    checkout at all.
    """
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    sibling = _make_exec(tmp_path / "bin" / "aggregator-schema-probe")
    _isolate(monkeypatch, _fake_home(tmp_path, {"command": str(mcp), "args": []}),
             tmp_path / "empty-bin")

    probe = _load_hook()._probe_invocation()

    assert probe.path == sibling
    assert probe.argv == [str(sibling), "--json"], (
        "the packaged probe is a console script, not a .py file: running it "
        "through sys.executable executes the wrong interpreter against a "
        "wrapper it cannot parse"
    )
    assert sys.executable not in probe.argv


def test_the_packaged_probe_is_run_as_a_program_not_fed_to_python(tmp_path):
    """End to end, with a sibling that is deliberately not valid Python.

    ``[sys.executable, <sibling>, "--json"]`` would die on the shebang line and
    surface as "answered in a shape this hook cannot read" — the same
    UNVERIFIED noise, one layer down. The only way to pass is to exec it.
    """
    _make_exec(tmp_path / "bin" / "aggregator-mcp")
    _make_exec(tmp_path / "bin" / "aggregator-schema-probe", PROBE_STUB_SH)
    home = _fake_home(tmp_path, {"command": str(tmp_path / "bin" / "aggregator-mcp"),
                                 "args": []})

    env = dict(os.environ)
    env.pop("AGGREGATOR_SCHEMA_PROBE", None)
    env.pop("AGGREGATOR_READER_DIR", None)
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True,
        env=env, timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert "cache 5, reader 6" in doc["systemMessage"]


@pytest.mark.parametrize("sibling_beside_the_symlink", [True, False], ids=["link-dir", "target-dir"])
def test_a_symlinked_command_is_searched_both_sides_of_the_link(
    tmp_path, monkeypatch, sibling_beside_the_symlink
):
    """``/etc/profiles/per-user/<u>/bin/aggregator-mcp`` is a symlink into the store.

    The console script is installed beside the LINK, in the profile — resolving
    the command first and looking only in the store directory would miss it.
    The store directory is still searched second, because a config may name the
    store path directly.
    """
    store = tmp_path / "store" / "bin"
    profile = tmp_path / "profile" / "bin"
    target = _make_exec(store / "aggregator-mcp")
    profile.mkdir(parents=True)
    link = profile / "aggregator-mcp"
    link.symlink_to(target)
    sibling = _make_exec(
        (profile if sibling_beside_the_symlink else store) / "aggregator-schema-probe"
    )
    _isolate(monkeypatch, _fake_home(tmp_path, {"command": str(link), "args": []}),
             tmp_path / "empty-bin")

    assert _load_hook()._probe_invocation().path == sibling


def test_a_bare_command_name_is_resolved_on_the_path(tmp_path, monkeypatch):
    """``command`` need not be absolute; Claude Code looks it up on PATH.

    So does the hook, or a perfectly healthy bare-name config would be reported
    as unverifiable.
    """
    bindir = tmp_path / "bin"
    _make_exec(bindir / "aggregator-mcp")
    sibling = _make_exec(bindir / "aggregator-schema-probe")
    _isolate(monkeypatch, _fake_home(tmp_path, {"command": "aggregator-mcp"}), bindir)

    probe = _load_hook()._probe_invocation()

    assert probe.path == sibling
    assert probe.argv == [str(sibling), "--json"]


def test_a_command_with_no_packaged_sibling_falls_back_to_the_checkout(
    tmp_path, monkeypatch
):
    """The state of this host until the packaged probe actually deploys.

    A reader binary with no ``aggregator-schema-probe`` next to it is not an
    error — it is every install predating the console script. The hook must
    keep working exactly as it does today rather than start announcing that it
    cannot find anything.
    """
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    home = _fake_home(tmp_path, {"command": str(mcp), "args": []})
    _isolate(monkeypatch, home, tmp_path / "empty-bin")

    probe = _load_hook()._probe_invocation()

    expected = home / "Repos" / "aggregator" / "aggregator" / "health" / "schema_probe.py"
    assert probe.path == expected
    assert probe.argv == [sys.executable, str(expected), "--json"]


def test_a_directory_argument_outranks_a_packaged_sibling_on_the_path(
    tmp_path, monkeypatch
):
    """Today's developer shape, and the sibling rule must not eat it.

    ``uv run --directory <tree> aggregator-mcp`` states which tree Claude Code
    is launching. But ``command`` there is ``uv``, which resolves to the Nix
    profile — and once the console script deploys, ``aggregator-schema-probe``
    is that binary's SIBLING. Preferring the packaged probe on the strength of
    that adjacency would silently stop measuring the tree the config named, on
    every developer config, the day the deploy landed.

    So the fixture puts a real ``uv`` and a real sibling on PATH: an empty PATH
    made the earlier version of this test pass while proving nothing about the
    host it was written for.
    """
    tree = tmp_path / "checkout"
    bindir = tmp_path / "bin"
    _make_exec(bindir / "uv")
    _make_exec(bindir / "aggregator-schema-probe")
    entry = {"command": "uv",
             "args": ["run", "--directory", str(tree), "aggregator-mcp"]}
    _isolate(monkeypatch, _fake_home(tmp_path, entry), bindir)

    probe = _load_hook()._probe_invocation()

    expected = tree / "aggregator" / "health" / "schema_probe.py"
    assert probe.path == expected, (
        "a packaged probe that merely sits next to `uv` displaced the tree the "
        "config explicitly named"
    )
    assert probe.argv == [sys.executable, str(expected), "--json"]


def test_the_reader_dir_override_outranks_a_packaged_sibling(tmp_path, monkeypatch):
    """``AGGREGATOR_READER_DIR`` is an instruction, not a hint.

    It exists so an operator can aim the check at a specific tree. Left below
    the packaged tier it would keep looking like a working knob while doing
    nothing at all from the deploy onward — the worst state for a control to be
    in, because nothing reports that it stopped mattering.
    """
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    _make_exec(tmp_path / "bin" / "aggregator-schema-probe")
    tree = tmp_path / "under-test"
    _isolate(monkeypatch, _fake_home(tmp_path, {"command": str(mcp), "args": []}),
             tmp_path / "empty-bin")
    monkeypatch.setenv("AGGREGATOR_READER_DIR", str(tree))

    probe = _load_hook()._probe_invocation()

    expected = tree / "aggregator" / "health" / "schema_probe.py"
    assert probe.path == expected
    assert probe.argv == [sys.executable, str(expected), "--json"]


def test_the_env_override_outranks_a_packaged_sibling(tmp_path, monkeypatch):
    """``AGGREGATOR_SCHEMA_PROBE`` is the escape hatch and stays on top.

    It is how this suite drives the hook at all, and how an operator points it
    at a probe under development. Discovery must never quietly win over an
    explicit instruction.
    """
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    _make_exec(tmp_path / "bin" / "aggregator-schema-probe")
    _isolate(monkeypatch, _fake_home(tmp_path, {"command": str(mcp), "args": []}),
             tmp_path / "empty-bin")
    override = tmp_path / "my_probe.py"
    override.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGGREGATOR_SCHEMA_PROBE", str(override))

    probe = _load_hook()._probe_invocation()

    assert probe.path == override
    assert probe.argv == [sys.executable, str(override), "--json"]


def test_the_missing_message_names_both_places_it_looked(tmp_path):
    """"MISSING" has to be actionable, and there are now two candidate homes.

    A message naming only the checkout sends the reader to fix the wrong thing
    on a host where the packaged probe is the one that should have been there,
    and vice versa. Both paths, in full, or the announcement costs its budget
    and buys nothing.
    """
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    home = _fake_home(tmp_path, {"command": str(mcp), "args": []})

    env = dict(os.environ)
    env.pop("AGGREGATOR_SCHEMA_PROBE", None)
    env.pop("AGGREGATOR_READER_DIR", None)
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True,
        env=env, timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    message = json.loads(proc.stdout)["systemMessage"]
    assert "UNVERIFIED" in message and "MISSING" in message
    assert str(tmp_path / "bin" / "aggregator-schema-probe") in message
    assert str(
        home / "Repos" / "aggregator" / "aggregator" / "health" / "schema_probe.py"
    ) in message


def test_a_command_that_does_not_resolve_says_so_instead_of_denying_it_exists(
    tmp_path, monkeypatch
):
    """"names no command" is a lie when the config names one that is missing.

    The two conditions need opposite fixes — write the entry versus repair the
    install or the PATH — and a reader who is told the entry is empty will go
    and look at an entry that is not. The name it failed to resolve is the
    whole content of the message.
    """
    _isolate(monkeypatch, _fake_home(tmp_path, {"command": "aggregator-mcp"}),
             tmp_path / "empty-bin")

    looked = _load_hook()._probe_invocation().looked

    assert "aggregator-mcp" in looked and "did not resolve on PATH" in looked
    assert "names no command" not in looked


def test_an_absent_mcp_entry_still_says_the_config_names_no_command(
    tmp_path, monkeypatch
):
    """The other half of the pair, so the two messages cannot collapse into one."""
    _isolate(monkeypatch, _fake_home(tmp_path, None), tmp_path / "empty-bin")

    looked = _load_hook()._probe_invocation().looked

    assert "names no command" in looked
    assert "did not resolve" not in looked


def test_a_sibling_that_is_not_executable_is_named_not_silently_skipped(
    tmp_path, monkeypatch
):
    """A present-but-chmod-644 console script is a broken install, not an absent one.

    Falling through to the checkout without a word turns a one-line fix into an
    afternoon: the operator can see the file sitting right there and has been
    told the probe is MISSING.
    """
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    dud = tmp_path / "bin" / "aggregator-schema-probe"
    dud.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dud.chmod(0o644)
    home = _fake_home(tmp_path, {"command": str(mcp), "args": []})
    _isolate(monkeypatch, home, tmp_path / "empty-bin")

    probe = _load_hook()._probe_invocation()

    assert probe.path == (
        home / "Repos" / "aggregator" / "aggregator" / "health" / "schema_probe.py"
    )
    assert str(dud) in probe.looked and "not executable" in probe.looked


def test_a_probe_that_only_speaks_on_stderr_has_its_words_relayed(tmp_path):
    """A Nix wrapper that cannot start exits non-zero with an empty stdout.

    Everything it knows — the missing library, the broken interpreter path — is
    on stderr, and the announcement used to quote only ``stdout[:120]``, i.e.
    ``''``. That reads as "the probe said something unparseable" when the probe
    said something perfectly clear and this hook threw it away, which is a
    strictly worse outcome than not checking.
    """
    p = _write_bad(
        tmp_path,
        "import sys\n"
        "sys.stderr.write('libtorch_cpu.so: cannot open shared object file')\n"
        "sys.exit(127)\n",
    )

    doc = json.loads(_run(p).stdout)

    assert "cannot open shared object file" in doc["systemMessage"]
    assert "UNVERIFIED" in doc["systemMessage"]


def test_a_bin_directory_it_cannot_read_is_named_with_the_reason(tmp_path):
    """"I looked and it was not there" versus "I could not look" need one word.

    An unreadable profile ``bin/`` and an empty one produce the same
    not-found, and the repairs are opposite: chmod versus reinstall. The
    operator gets a MISSING announcement either way, so the reason has to
    travel with it or the announcement sends them to the wrong end of the
    problem.

    Note the fixture is a permission error, not a missing file: ``Path.is_file``
    turns EVERY OSError into ``False``, and CPython 3.13 widened that swallow
    to cover EACCES, which 3.12 still re-raised. A check whose diagnosis
    depends on the interpreter's minor version is not a diagnosis, so the hook
    stats the candidate itself.
    """
    if os.geteuid() == 0:
        pytest.skip("root traverses a 0o000 directory, so nothing raises")

    bindir = tmp_path / "bin"
    mcp = _make_exec(bindir / "aggregator-mcp")
    home = _fake_home(tmp_path, {"command": str(mcp), "args": []})
    env = dict(os.environ)
    env.pop("AGGREGATOR_SCHEMA_PROBE", None)
    env.pop("AGGREGATOR_READER_DIR", None)
    env["HOME"] = str(home)

    bindir.chmod(0o000)
    try:
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input="{}", capture_output=True,
            text=True, env=env, timeout=30,
        )
    finally:
        bindir.chmod(0o755)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "the hook went silent on a directory it could not read"
    message = json.loads(proc.stdout)["systemMessage"]
    assert "UNVERIFIED" in message and "MISSING" in message
    assert str(bindir / "aggregator-schema-probe") in message
    assert "could not be resolved" in message
    assert "PermissionError" in message


# ---------------------------------------------------------------------------
# Provenance: which probe spoke, and how much the answer is worth
# ---------------------------------------------------------------------------
# THE DEFECT THIS FIXES. The tiers are not equally trustworthy — the packaged
# sibling IS the reader under test, while ``~/Repos/aggregator`` is a guess
# about a tree nobody named — and until now only the MISSING announcement said
# where it had been. A DEAD verdict looked identical whichever tier produced
# it, so the 2026-09-05 false alarm, read off a guessed checkout sitting on a
# feature branch, arrived with the same authority as a measurement of the real
# thing. The module docstring even claimed the guess tier "says so"; it did
# not.

DEAD_VERDICT = {
    "state": "dead", "severity": "down", "summary": "cache 5, reader 6",
    "findings": [],
}


def _plant_py_probe(path: Path, payload: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        PROBE_STUB % (json.dumps(payload or DEAD_VERDICT), 20), encoding="utf-8"
    )
    return path


def _plant_sh_probe(path: Path, payload: dict | None = None) -> Path:
    return _make_exec(
        path,
        "#!/bin/sh\n"
        "printf '%s\\n' '" + json.dumps(payload or DEAD_VERDICT) + "'\nexit 20\n",
    )


FINE_VERDICT = {
    "state": "fine", "severity": "silent", "summary": "all three agree at 7",
    "findings": [],
}


def _run_hook(home: Path, path_dir: Path, **extra_env: str):
    env = dict(os.environ)
    env.pop("AGGREGATOR_SCHEMA_PROBE", None)
    env.pop("AGGREGATOR_READER_DIR", None)
    env["HOME"] = str(home)
    env["PATH"] = str(path_dir)
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK)], input="{}", capture_output=True, text=True,
        env=env, timeout=30,
    )


def _tier_override(tmp_path):
    probe = _plant_py_probe(tmp_path / "explicit" / "probe.py")
    home = _fake_home(tmp_path, {"command": "aggregator-mcp"})
    return home, {"AGGREGATOR_SCHEMA_PROBE": str(probe)}, probe, "override"


def _tier_packaged(tmp_path):
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    probe = _plant_sh_probe(tmp_path / "bin" / "aggregator-schema-probe")
    home = _fake_home(tmp_path, {"command": str(mcp), "args": []})
    return home, {}, probe, "packaged sibling"


def _tier_directory(tmp_path):
    tree = tmp_path / "checkout"
    probe = _plant_py_probe(tree / "aggregator" / "health" / "schema_probe.py")
    home = _fake_home(tmp_path, {
        "command": "uv",
        "args": ["run", "--directory", str(tree), "aggregator-mcp"],
    })
    return home, {}, probe, "--directory"


def _tier_reader_dir(tmp_path):
    tree = tmp_path / "aimed-at"
    probe = _plant_py_probe(tree / "aggregator" / "health" / "schema_probe.py")
    home = _fake_home(tmp_path, {"command": "aggregator-mcp"})
    return home, {"AGGREGATOR_READER_DIR": str(tree)}, probe, "AGGREGATOR_READER_DIR"


def _tier_guess(tmp_path):
    home = _fake_home(tmp_path, {})
    probe = _plant_py_probe(
        home / "Repos" / "aggregator" / "aggregator" / "health" / "schema_probe.py"
    )
    return home, {}, probe, "GUESS"


@pytest.mark.parametrize(
    "fixture",
    [_tier_override, _tier_packaged, _tier_directory, _tier_reader_dir, _tier_guess],
    ids=["override", "packaged", "directory", "reader-dir", "guess"],
)
def test_every_verdict_names_the_probe_that_produced_it(tmp_path, fixture):
    """A verdict without its provenance is an assertion without a source.

    All five tiers are made to return the same DEAD verdict, so the only thing
    that can distinguish the announcements is the line naming what was actually
    run. Without it the operator cannot tell a measurement of the shipped
    reader from a reading of whatever branch a checkout happens to be on.
    """
    home, extra_env, probe, label = fixture(tmp_path)

    proc = _run_hook(home, tmp_path / "empty-bin", **extra_env)

    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    message = doc["systemMessage"]
    assert "cache 5, reader 6" in message
    assert str(probe) in message, f"the {label} tier did not name its probe"
    assert label in message, f"the {label} tier did not name itself"


def test_a_verdict_read_off_the_guessed_checkout_says_it_may_be_wrong(tmp_path):
    """The 2026-09-05 false alarm, and the sentence that would have defused it.

    Nothing in ~/.claude.json named ``~/Repos/aggregator``. The hook went there
    because it had nowhere else to go, read a feature branch, and announced an
    outage in the same words it would use for a measured one. Every other tier
    is a statement by somebody; this one is the hook's own guess, and it has to
    admit that in the announcement rather than only in the source.
    """
    home, extra_env, probe, _ = _tier_guess(tmp_path)

    doc = json.loads(_run_hook(home, tmp_path / "empty-bin", **extra_env).stdout)

    for reader in (doc["systemMessage"], doc["hookSpecificOutput"]["additionalContext"]):
        assert "GUESS" in reader
        assert "may not describe the reader" in reader


def test_a_probe_it_cannot_stat_is_not_reported_as_absent(tmp_path):
    """The sibling fix, applied to the probe the hook finally settled on.

    ``main`` still gated the chosen probe with ``Path.is_file()``, which maps
    every OSError to ``False`` — so an unreadable path was announced as MISSING
    and the operator was sent to install a file that is already there. Same
    misdiagnosis, same fix, one tier down.

    The trigger is an unreadable PARENT, not a ``0o000`` probe file: ``stat``
    needs no permission on the file itself, so a mode-000 probe is found
    correctly and fails later, loudly, when it is run.
    """
    if os.geteuid() == 0:
        pytest.skip("root traverses a 0o000 directory, so nothing raises")

    holder = tmp_path / "holder"
    probe = _plant_py_probe(holder / "schema_probe.py")
    home = _fake_home(tmp_path, {})

    holder.chmod(0o000)
    try:
        proc = _run_hook(home, tmp_path / "empty-bin",
                         AGGREGATOR_SCHEMA_PROBE=str(probe))
    finally:
        holder.chmod(0o755)

    assert proc.returncode == 0, proc.stderr
    message = json.loads(proc.stdout)["systemMessage"]
    assert "MISSING" not in message, (
        "an unreadable probe was reported as absent, which is the wrong repair"
    )
    assert "could not be resolved" in message
    assert "PermissionError" in message


def test_a_probe_file_with_no_permissions_is_still_found_and_reported_on(tmp_path):
    """The neighbouring case, pinned so the fix above cannot over-reach.

    A ``0o000`` probe file is present and stat-able. It must NOT be called
    missing or unresolvable — the interpreter refuses it at exec time, and that
    lands in the unreadable-verdict announcement, which since the stderr fix
    relays the reason verbatim.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file, so nothing is refused")

    probe = _plant_py_probe(tmp_path / "holder" / "schema_probe.py")
    probe.chmod(0o000)
    home = _fake_home(tmp_path, {})

    proc = _run_hook(home, tmp_path / "empty-bin",
                     AGGREGATOR_SCHEMA_PROBE=str(probe))

    assert proc.returncode == 0, proc.stderr
    message = json.loads(proc.stdout)["systemMessage"]
    assert "MISSING" not in message and "could not be resolved" not in message
    assert "Permission denied" in message


def test_an_unparseable_claude_json_is_reported_not_read_as_empty(tmp_path):
    """A config the hook could not parse is not a config that says nothing.

    ``_server_entry`` collapsed both into ``{}``, so a truncated or
    half-written ``~/.claude.json`` produced "mcpServers.aggregator names no
    command" — a claim about a file the hook never managed to look inside. The
    repairs differ completely: write the entry, versus repair the file. And
    the second silently demotes the check to its guess tier, which is the
    failure mode this whole branch exists to make loud.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"mcpServers": {"aggregator": ', encoding="utf-8")

    message = json.loads(_run_hook(home, tmp_path / "empty-bin").stdout)["systemMessage"]

    assert str(home / ".claude.json") in message
    assert "JSONDecodeError" in message
    assert "names no command" not in message


def test_an_unreadable_claude_json_names_the_permission_error(tmp_path):
    """Same distinction, the other cause: present, parseable, not readable."""
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file, so nothing is refused")

    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".claude.json"
    cfg.write_text("{}", encoding="utf-8")
    cfg.chmod(0o000)
    try:
        message = json.loads(
            _run_hook(home, tmp_path / "empty-bin").stdout
        )["systemMessage"]
    finally:
        cfg.chmod(0o644)

    assert str(cfg) in message
    assert "PermissionError" in message


def test_an_absent_claude_json_is_not_reported_as_a_broken_one(tmp_path):
    """The control. No file is the ordinary case on a fresh machine.

    It must not acquire an error message, or the distinction above is worth
    nothing — a warning that fires for everybody says nothing about anybody.
    """
    home = tmp_path / "home"
    home.mkdir()

    message = json.loads(_run_hook(home, tmp_path / "empty-bin").stdout)["systemMessage"]

    assert "could not be read" not in message
    assert "JSONDecodeError" not in message and "PermissionError" not in message


def test_a_probe_that_could_not_measure_is_not_called_an_outage(tmp_path):
    """``unknown`` carries severity ``down``. It does not mean recall is down.

    The probe maps UNKNOWN to the DOWN severity on purpose — its rule is
    "unknown means warn, never fine", and severity is how loudly to speak. The
    hook then keyed its HEADLINE off severity and told the operator
    "aggregator_search_memory is refusing every call this session" about a
    probe that had just said it could not read one of its three quantities.

    That is a fabricated fact in the loudest sentence on the screen, and it
    burns the same credibility the false-alarm fixes on this branch were spent
    buying back. The verdict still gets announced; it just says what it knows.
    """
    proc = _run(_write_probe(
        tmp_path,
        {"state": "unknown", "severity": "down",
         "summary": "the writer binary could not be resolved", "findings": []},
        20,
    ))

    message = json.loads(proc.stdout)["systemMessage"]
    assert "the writer binary could not be resolved" in message
    assert "REFUSING" not in message.upper(), (
        "a probe that could not measure was announced as a live outage"
    )
    assert "UNVERIFIED" in message


def test_a_state_this_hook_does_not_know_still_speaks(tmp_path):
    """Forward compatibility, in the direction that matters.

    A state string added to the probe later must not fall through to silence.
    Silence means "checked and healthy" here, and a hook that goes quiet on a
    verdict it does not recognise reproduces the original incident exactly.
    """
    proc = _run(_write_probe(
        tmp_path,
        {"state": "corrupt", "severity": "down", "summary": "cache is not a db",
         "findings": []},
        20,
    ))

    assert proc.stdout, "the hook went silent on a state it did not recognise"
    assert "cache is not a db" in json.loads(proc.stdout)["systemMessage"]


# ---------------------------------------------------------------------------
# H2: silence is a claim, and only some tiers have standing to make it
# ---------------------------------------------------------------------------
# Silence means "checked and healthy" — that is the contract this hook is built
# on and the reason it competes for a SessionStart slot at all. But `fine`
# returned silently at ANY tier, so a guessed checkout that happened to agree
# with the cache, or a run that never managed to read ~/.claude.json, produced
# the identical nothing. That is the original incident with the sign flipped:
# recall could be dead while a tree nobody named said everything agreed, and
# the session would be told exactly as much as if the check had passed.


def test_a_healthy_verdict_from_the_guessed_checkout_is_not_silence(tmp_path):
    """The guess tier has no standing to say "checked and healthy".

    It measured a tree nobody named. Agreement there is evidence about that
    tree and nothing else, so it gets one calm line — not the outage headline,
    which would be its own false alarm.
    """
    home = _fake_home(tmp_path, {})
    _plant_py_probe(
        home / "Repos" / "aggregator" / "aggregator" / "health" / "schema_probe.py",
        FINE_VERDICT,
    )

    proc = _run_hook(home, tmp_path / "empty-bin")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "a healthy-looking guess was reported as a verified pass"
    message = json.loads(proc.stdout)["systemMessage"]
    assert "not verified" in message
    assert "GUESS" in message
    assert "REFUSING" not in message.upper() and "IS DOWN" not in message


def test_a_healthy_verdict_after_an_unreadable_config_is_not_silence(tmp_path):
    """Same standing problem, different cause, and a NAMED tier this time.

    AGGREGATOR_READER_DIR still resolves when ~/.claude.json cannot be parsed,
    so the tier looks authoritative while the hook has no idea what Claude Code
    is actually launching. The config fault has to survive into the verdict
    rather than being reported only when something else also goes wrong.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"mcpServers":', encoding="utf-8")
    tree = tmp_path / "aimed-at"
    _plant_py_probe(tree / "aggregator" / "health" / "schema_probe.py", FINE_VERDICT)

    proc = _run_hook(home, tmp_path / "empty-bin", AGGREGATOR_READER_DIR=str(tree))

    assert proc.stdout, "a healthy verdict hid a config the hook could not read"
    message = json.loads(proc.stdout)["systemMessage"]
    assert "not verified" in message
    assert "could not be read" in message and "JSONDecodeError" in message


def test_a_healthy_verdict_from_the_packaged_sibling_stays_silent(tmp_path):
    """The other half, and the one that keeps the budget worth having.

    The sibling IS the reader under test. When it says everything agrees, the
    check has done its job and must print nothing at all — a hook that speaks
    on a good day is one nobody reads on a bad one.
    """
    mcp = _make_exec(tmp_path / "bin" / "aggregator-mcp")
    _plant_sh_probe(tmp_path / "bin" / "aggregator-schema-probe", FINE_VERDICT)
    home = _fake_home(tmp_path, {"command": str(mcp), "args": []})

    proc = _run_hook(home, tmp_path / "empty-bin")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"the hook spoke on a verified pass: {proc.stdout!r}"


def test_a_healthy_verdict_from_a_named_tree_stays_silent(tmp_path):
    """A tree the config named is a statement about the reader, so it counts."""
    tree = tmp_path / "checkout"
    _plant_py_probe(tree / "aggregator" / "health" / "schema_probe.py", FINE_VERDICT)
    home = _fake_home(tmp_path, {
        "command": "uv",
        "args": ["run", "--directory", str(tree), "aggregator-mcp"],
    })

    proc = _run_hook(home, tmp_path / "empty-bin")

    assert proc.stdout == "", f"the hook spoke on a verified pass: {proc.stdout!r}"
