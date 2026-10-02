"""SessionStart hook that tells a session about imperative-install drift.

WHAT THIS GUARDS. Four times in four months something scheduled on this
host was installed with a stateful command (`crontab -e`, a hand-written
unit under ~/.config/systemd/user, a copy into ~/.local/bin) instead of a
nix declaration, and stopped silently on the next rebuild. The RSI daily
reviewer was dead for four months that way. nixos-config's
``nixos-drift-analyzer`` already detects these classes hourly, but writes
them to a markdown file no session reads; its failure path is a desktop
toast. This hook is the channel to Claude.

Contract with the producer (home/drift-analyzer.nix in nixos-config):
``$XDG_STATE_HOME/nixos-drift-analyzer/last-success`` holds
``timestamp=<iso>\\nfindings=<n>`` after every successful run, and
``$XDG_DATA_HOME/nixos-drift-analyzer/latest.md`` holds ``## <heading>``
sections each followed by a ``~~~text`` block.

Invariants tested: silent when there is nothing to act on; speaks, naming
the items, when an imperative-install class is present; never silent when
it could not tell (stale or missing heartbeat on a host that runs the
analyzer); never breaks startup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import REPO_ROOT

HOOK = Path(REPO_ROOT) / "hooks" / "nixos-drift-health.py"


def _section(heading: str, items: list[str]) -> str:
    body = "\n".join(items)
    return f"## {heading}\n\n~~~text\n{body}\n~~~\n\n"


def _setup(tmp_path: Path, sections: list[str] | None, *, age_h: float = 1,
           heartbeat: str | None = None) -> dict:
    data = tmp_path / "data" / "nixos-drift-analyzer"
    state = tmp_path / "state" / "nixos-drift-analyzer"
    data.mkdir(parents=True)
    state.mkdir(parents=True)
    if sections is not None:
        n = len(sections)
        head = "## No drift detected\n" if n == 0 else f"{n} finding(s) need review.\n\n"
        (data / "latest.md").write_text(
            "# NixOS drift report\n\nGenerated: x\n\n" + head + "".join(sections)
        )
        ts = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
        if heartbeat is None:
            heartbeat = f"timestamp={ts}\nfindings={n}\n"
    if heartbeat is not None:
        (state / "last-success").write_text(heartbeat)
    return {"XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state")}


def _run(env_extra: dict, stdin: str = "{}") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env,
                          capture_output=True, text=True, timeout=10)


def _context(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return ""
    doc = json.loads(proc.stdout)
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = doc["hookSpecificOutput"]["additionalContext"]
    assert doc["systemMessage"]
    return ctx


def test_clean_report_is_silent(tmp_path):
    assert _context(_run(_setup(tmp_path, []))) == ""


def test_non_install_findings_stay_silent(tmp_path):
    # Live on tuxedo 2026-10-02: duplicate-main worktrees, harmless since
    # nixos-config-fetch became fetch-only. Not this hook's class; the report
    # file still carries it.
    env = _setup(tmp_path, [
        _section("Multiple worktrees share refs/heads/main", ["/a", "/b"]),
        _section("Deployed NixOS checkout has changes", [" M x.nix"]),
    ])
    assert _context(_run(env)) == ""


def test_each_install_class_speaks_and_names_items(tmp_path):
    cases = {
        "Unmanaged entries in ~/.config/systemd/user": "hand-made.timer",
        "Entries outside /nix/store in ~/.local/bin": "myscript",
        "Installed crontab differs from declaration": "declared: /h/.config/crontab",
        "Imperative nix-env packages": "hello-2.12",
    }
    for i, (heading, item) in enumerate(cases.items()):
        sub = tmp_path / str(i)
        ctx = _context(_run(_setup(sub, [_section(heading, [item])])))
        assert heading in ctx, (heading, ctx)
        assert item in ctx, (heading, ctx)


def test_install_class_reported_alongside_ignored_ones(tmp_path):
    env = _setup(tmp_path, [
        _section("Multiple worktrees share refs/heads/main", ["/a", "/b"]),
        _section("Installed crontab differs from declaration", ["declared: d"]),
    ])
    ctx = _context(_run(env))
    assert "crontab" in ctx
    assert "refs/heads/main" not in ctx


def test_stale_heartbeat_speaks_even_with_clean_report(tmp_path):
    ctx = _context(_run(_setup(tmp_path, [], age_h=30)))
    assert "UNVERIFIED" in ctx


def test_missing_heartbeat_with_analyzer_state_dir_speaks(tmp_path):
    env = _setup(tmp_path, None)
    assert "UNVERIFIED" in _context(_run(env))


def test_garbage_heartbeat_speaks(tmp_path):
    env = _setup(tmp_path, [], heartbeat="not a heartbeat\n")
    assert "UNVERIFIED" in _context(_run(env))


def test_host_without_analyzer_is_silent(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "d"), "XDG_STATE_HOME": str(tmp_path / "s")}
    assert _context(_run(env)) == ""


def test_long_item_lists_are_capped(tmp_path):
    items = [f"unit-{i}.service" for i in range(200)]
    ctx = _context(_run(_setup(tmp_path, [
        _section("Unmanaged entries in ~/.config/systemd/user", items)])))
    assert "unit-0.service" in ctx
    assert "unit-199.service" not in ctx
    assert "more" in ctx
    assert len(ctx) < 4000


def test_unreadable_report_does_not_break_startup(tmp_path):
    env = _setup(tmp_path, [_section("Imperative nix-env packages", ["x"])])
    report = tmp_path / "data" / "nixos-drift-analyzer" / "latest.md"
    report.chmod(0)
    try:
        proc = _run(env)
    finally:
        report.chmod(0o644)
    assert proc.returncode == 0
    if os.geteuid() != 0:
        assert "UNVERIFIED" in _context(proc)


def test_registered_on_session_start():
    settings = json.loads((Path(REPO_ROOT) / "settings.json").read_text())
    cmds = [h["command"] for e in settings["hooks"]["SessionStart"] for h in e["hooks"]]
    assert any("hooks/nixos-drift-health.py" in c for c in cmds), cmds
