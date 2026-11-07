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

# Anchored here, not imported from `conftest`: tests/ and tests/ai-router/ both
# hold a module named `conftest`, and in a whole-tree run `from conftest import`
# resolves to whichever was imported last.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The shipped config lives one level down in runtime/ since the runtime cutover;
# tolerate both layouts, same anchor as the other top-level suites.
RUNTIME = Path(REPO_ROOT) / "runtime" if (Path(REPO_ROOT) / "runtime").is_dir() else Path(REPO_ROOT)

HOOK = RUNTIME / "hooks" / "nixos-drift-health.py"


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


def test_report_that_does_not_parse_to_the_heartbeat_count_speaks(tmp_path):
    # The two halves live in different repos. If the analyzer's report format
    # drifts (fence or heading style) while the heartbeat still says it found
    # things, parsing nothing must not read as "checked and clean".
    env = _setup(tmp_path, [
        "## Entries outside /nix/store in ~/.local/bin\n\n```text\nmyscript\n```\n\n",
        "## Multiple worktrees share refs/heads/main\n\n```text\n/a\n/b\n```\n\n",
    ])
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


def test_vscodium_drift_classes_speak_and_name_items(tmp_path):
    # nixos-config's home/vscodium.nix merges declared over live, so undeclared
    # keys survive reboot and deploy but not a fresh install, and declared keys
    # edited in the UI get reverted. Both are promotable by the session.
    # Each case carries the phrase that makes its hint actionable, because
    # the three say different things. An undeclared cosmetic key is lost on
    # a fresh install; an undeclared SECURITY key is the opposite problem --
    # it survives every deploy -- so a hint about fresh installs would be
    # the wrong advice there.
    cases = {
        "VSCodium settings live but not declared":
            ("editor.minimap.enabled", "fresh install"),
        "VSCodium settings overwritten by the declared config":
            ("editor.fontSize  (scalar, discarded 3x, first 2026-11-01)",
             "change the declaration"),
        # The escalated variant: an undeclared key that disarms a guard is
        # the same shape on disk as an undeclared font size, and the merge
        # preserves both, so it needs its own heading to be its own finding.
        "VSCodium security settings live but not declared":
            ("security.workspace.trust.enabled", "PRESERVES"),
    }
    for i, (heading, (item, phrase)) in enumerate(cases.items()):
        ctx = _context(_run(_setup(tmp_path / f"v{i}", [_section(heading, [item])])))
        assert heading in ctx, (heading, ctx)
        assert item in ctx, (heading, ctx)
        assert phrase in ctx, (heading, phrase, ctx)


def test_codium_skill_layer_classes_speak_and_name_items(tmp_path):
    # The skills half of the same layer (nixos-config
    # home/claude-codium-config.nix). A skill or note written into
    # ~/.local/state/claude-codium works immediately and survives every
    # deploy, so it reads as reviewed config until a reinstall drops it, and
    # nothing else on the host looks in there.
    #
    # Each case pins the phrase that makes its hint actionable, because the
    # three prescribe DIFFERENT actions: promote the skill, do NOT promote a
    # shadowing one (rename or drop it -- promoting makes the collision
    # permanent), and promote the durable notes.
    cases = {
        "Codium-only skills live but not declared":
            ("diff-review", "home/claude-codium/skills"),
        "Codium skills shadowing a shared skill of the same name":
            ("using-git-worktrees", "do not promote it"),
        "Codium session notes live but not declared":
            ("/home/j/.local/state/claude-codium/local.md",
             "home/claude-codium/CLAUDE.md"),
    }
    for i, (heading, (item, phrase)) in enumerate(cases.items()):
        ctx = _context(_run(_setup(tmp_path / f"c{i}", [_section(heading, [item])])))
        assert heading in ctx, (heading, ctx)
        assert item in ctx, (heading, ctx)
        assert phrase in ctx, (heading, phrase, ctx)


def test_codium_skill_drift_reported_alongside_vscodium_settings_drift(tmp_path):
    # Both halves of the Codium layer in one report, which is the ordinary
    # case once both are deployed: the editor's settings and the editor's
    # skills drift independently and neither should mask the other.
    env = _setup(tmp_path, [
        _section("VSCodium settings live but not declared", ["editor.wordWrap"]),
        _section("Codium-only skills live but not declared", ["my-skill"]),
    ])
    ctx = _context(_run(env))
    assert "editor.wordWrap" in ctx, ctx
    assert "my-skill" in ctx, ctx


def _failure_section(heading: str) -> str:
    # Shape the tool actually emits: key/value metadata then a prose paragraph.
    return _section(heading, [
        "at:     2026-11-06T09:00:00+01:00",
        "status: 1",
        "",
        "Declared settings were NOT applied and the live file was left as it "
        "was. Inspect: journalctl --user -u home-manager-$USER.service",
    ])


def test_each_vscodium_mechanism_failure_speaks(tmp_path):
    # These four were invisible before: CLASSES is an allowlist, so a merge
    # that failed outright reached no session. A desktop toast is the only
    # other channel and it is gone by the next session.
    headings = [
        "VSCodium settings merge failed at activation",
        "VSCodium settings file does not parse",
        "VSCodium drift recorder failed during activation",
        "VSCodium settings could not be compared",
    ]
    for i, heading in enumerate(headings):
        ctx = _context(_run(_setup(tmp_path / f"f{i}", [_failure_section(heading)])))
        assert heading in ctx, (heading, ctx)
        assert "reported a failure" in ctx, (heading, ctx)


def test_vscodium_failure_does_not_echo_its_prose_body(tmp_path):
    # The hint carries the meaning; echoing the section body would paste a
    # whole paragraph into every session's context as one smushed line.
    heading = "VSCodium settings merge failed at activation"
    ctx = _context(_run(_setup(tmp_path, [_failure_section(heading)])))
    assert "status: 1" not in ctx, ctx
    assert "Inspect: journalctl --user" not in ctx, ctx
    assert "declared settings were NOT applied" in ctx, ctx


def test_vscodium_failure_and_drift_are_reported_under_distinct_headlines(tmp_path):
    # A failed merge did not "drift"; the declaration may never have applied.
    # One headline cannot carry both sentences truthfully.
    env = _setup(tmp_path, [
        _failure_section("VSCodium settings merge failed at activation"),
        _section("VSCodium settings live but not declared", ["editor.wordWrap"]),
    ])
    ctx = _context(_run(env))
    assert "reported a failure" in ctx
    assert "lost or unreproducible on rebuild" in ctx
    assert ctx.index("reported a failure") < ctx.index("lost or unreproducible")


def test_a_report_hitting_every_class_stays_under_the_output_cap():
    # Hook output strings cap at 10,000 characters and over it the payload is
    # dropped, not truncated -- a silent total failure of the channel. This
    # hook started at six headings and is now at sixteen, which puts the
    # worst case around 7.7k: still under, but the headroom is roughly 2.2k,
    # or about three more headings of this length. So pin it, and read the
    # assertion value as a budget rather than a formality -- the failure mode
    # is the whole report going missing, not a truncated one.
    import importlib.util
    spec = importlib.util.spec_from_file_location("drift_health", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    worst = sum(
        len(f"  - {heading}: {shown}{more} — {hint}\n")
        for classes in (mod.CLASSES, mod.FAILURES)
        for heading, hint in classes.items()
        for shown in [", ".join("some.rather.long.setting.path" for _ in range(mod.MAX_ITEMS))]
        for more in [" (+99 more)"]
    )
    # Two headlines, the report path, and the additionalContext preamble.
    worst += 400
    assert worst < 10000, worst


def test_registered_on_session_start():
    settings = json.loads((RUNTIME / "settings.json").read_text())
    cmds = [h["command"] for e in settings["hooks"]["SessionStart"] for h in e["hooks"]]
    assert any("hooks/nixos-drift-health.py" in c for c in cmds), cmds
