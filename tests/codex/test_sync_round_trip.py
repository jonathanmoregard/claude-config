#!/usr/bin/env python3
"""Round trips through the real sync: ~/.claude -> manifest -> ~/.codex -> check.

The check must derive what it expects from the renderer's own plan. These
tests run the published entry point against a temp HOME, then prove that the
check sees every file the render publishes: corrupting or deleting any of
them must fail the check, so a future publish step the drift check does not
cover turns this suite red.
"""
from __future__ import annotations

from itertools import product
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roundtrip_support import (  # noqa: E402
    Inputs,
    RoundTrip,
    assert_check_fails,
    describe,
    render_codex,
    restore,
    round_trip,
    snapshot,
)


GITHUB_CHILD_SETS: tuple[dict[str, list[str]], ...] = (
    {},
    {"acme": ["--owner", "acme"]},
    {"acme": ["--owner", "acme"], "octo-org": ["--owner", "octo-org"]},
)
MATRIX = tuple(
    Inputs(per_tool_allows=per_tool, optional_broker=broker, github_children=children)
    for per_tool, broker, children in product((False, True), (False, True), GITHUB_CHILD_SETS)
)
RICH_INPUTS = Inputs(plugins=True, skills=("demo-skill", "claude-code-permissions"))


def test_round_trip_matrix_checks_clean_and_rerenders_as_no_op() -> None:
    for inputs in MATRIX:
        label = inputs.label()
        with round_trip(inputs) as rt:
            config = rt.load_config()
            servers = config["mcp_servers"]
            assert ("agent_e2e_broker" in servers) is inputs.optional_broker, label
            for owner in inputs.github_children:
                assert f"github_{owner}" in servers, label
            approvals = servers["git_broker"].get("tools")
            assert (approvals == {"git": {"approval_mode": "approve"}}) is inputs.per_tool_allows, label

            before = snapshot(rt.home)
            # Everything, inputs and the importer's manifest included: the
            # check (render plan preparation included) must only read.
            everything = (snapshot(rt.home, ("",)), snapshot(rt.repo, ("",)))
            checked = rt.check()
            assert checked.returncode == 0, f"{label}\n{describe(checked)}"
            assert (snapshot(rt.home, ("",)), snapshot(rt.repo, ("",))) == everything, (
                f"{label}: check modified the tree"
            )

            rt.sync_ok()
            after = snapshot(rt.home)
            changed = sorted(
                key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
            )
            assert not changed, f"{label}: unchanged inputs rewrote {changed}"


# What /etc/nixos/home/ai-client-config-sync.nix carries from the live home
# into the staged home it renders into; everything else renders from scratch.
STAGED_MERGE_INPUTS = (
    ".codex/config.toml",
    ".codex/rules/default.rules",
    ".agents/plugins/marketplace.json",
)


def rsync_view(state: dict[str, tuple[str, bytes | str, int]]) -> dict[str, tuple[str, bytes | str, bool]]:
    # rsync -rltE --checksum: content, symlink targets and the executable bit.
    # Renderer backups are hidden from the sender; the hook tree is protected.
    return {
        key: (kind, payload, bool(mode & 0o111))
        for key, (kind, payload, mode) in state.items()
        if ".bak-" not in key
    }


def test_unchanged_hourly_run_from_a_staged_home_publishes_nothing() -> None:
    # Production renders into a fresh staged home holding only the merge
    # inputs above, then rsyncs it over the live home. An unchanged run must
    # produce the live bytes exactly, or every hour rewrites files.
    with round_trip() as rt:
        live = rsync_view(snapshot(rt.home))
        live_home = rt.home.with_name("live-home")
        rt.home.rename(live_home)
        rt.home.mkdir()
        shutil.copytree(live_home / ".claude", rt.home / ".claude", symlinks=True)
        shutil.copy2(live_home / ".claude.json", rt.home / ".claude.json")
        for relative in STAGED_MERGE_INPUTS:
            (rt.home / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live_home / relative, rt.home / relative)
        for relative in (".codex", ".agents/plugins", "plugins/claude-mirror"):
            (rt.home / relative).mkdir(parents=True, exist_ok=True)
        # A wall-clock stamp would differ from the live run's.
        second = int(time.time())
        while int(time.time()) == second:
            time.sleep(0.05)

        rt.sync_ok()
        staged = rsync_view(snapshot(rt.home))
        published = sorted(key for key in staged if staged[key] != live.get(key))
        pruned = sorted(
            key for key in live.keys() - staged.keys()
            if not key.startswith(".codex/hooks/claude-mirror/")
        )
        assert (published, pruned) == ([], []), (published, pruned)


def test_check_fails_for_every_corrupted_or_deleted_published_file() -> None:
    with round_trip(RICH_INPUTS) as rt:
        published = snapshot(rt.home)
        # The rich inputs exercise every publish step, links included.
        kinds = {kind for kind, _payload, _mode in published.values()}
        assert kinds == {"file", "symlink"}, kinds

        for key, entry in sorted(published.items()):
            path = rt.home / key
            mutations = ["corrupt", "delete"]
            # A damaged wrapper is still the plan's, so the check names the
            # damaged file itself, like any other.
            expected = str(path)
            for mutation in mutations:
                if path.is_symlink() or mutation == "delete":
                    path.unlink()
                if mutation == "corrupt":
                    path.write_text("corrupted\n", encoding="utf-8")
                assert_check_fails(rt, expected, label=f"{mutation} {key}")
                restore(rt.home, key, entry)

            # Corrupting or deleting a file Codex parses also trips an output
            # invariant, which names the same path. A trailing newline still
            # parses and keeps every invariant: only the drift layer sees it,
            # so this is what pins each file to the drift check. config.toml
            # is compared semantically and covered elsewhere.
            kind, payload, _mode = entry
            if kind == "file" and path != rt.config:
                path.write_bytes(payload + b"\n")
                drift = (
                    f"{path}: MCP asset differs from its source"
                    if path.is_relative_to(rt.codex / "mcps")
                    else f"{path}: differs from the render plan"
                )
                result = assert_check_fails(rt, drift, label=f"append {key}")
                errors = [line for line in result.stdout.splitlines() if line.startswith("- ")]
                assert errors == [f"- {drift}"], describe(result)
                restore(rt.home, key, entry)

        result = rt.check()
        assert result.returncode == 0, describe(result)


def test_drift_check_itself_catches_parseable_edits_of_structured_files() -> None:
    # Garbage or deletion of these files also trips a parse error or an output
    # invariant; an edit that parses and stays safe reaches only the drift
    # check, so this is what shows the drift check covers them.
    def edit_hooks(path: Path) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["description"] = "edited outside the sync"
        path.write_text(json.dumps(document), encoding="utf-8")

    def edit_report(path: Path) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["known_gaps"] = []
        path.write_text(json.dumps(document), encoding="utf-8")

    def edit_config(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert 'model_reasoning_effort = "high"' in text
        path.write_text(
            text.replace('model_reasoning_effort = "high"', 'model_reasoning_effort = "low"', 1),
            encoding="utf-8",
        )

    with round_trip(RICH_INPUTS) as rt:
        cases = [
            (rt.codex / "hooks.json", edit_hooks),
            (rt.plugin / "hooks.json", edit_hooks),
            (rt.codex / "claude-setup-mirror" / "report.json", edit_report),
            (rt.config, edit_config),
        ]
        for path, edit in cases:
            original = path.read_bytes()
            edit(path)
            result = assert_check_fails(
                rt, f"{path}: differs from the render plan", label=str(path)
            )
            errors = [line for line in result.stdout.splitlines() if line.startswith("- ")]
            assert all(
                line.startswith(f"- {path}: differs from the render plan") for line in errors
            ), describe(result)
            path.write_bytes(original)

        result = rt.check()
        assert result.returncode == 0, describe(result)

        result = rt.check()
        assert result.returncode == 0, describe(result)


def test_check_rejects_config_key_the_render_does_not_produce() -> None:
    with round_trip() as rt:
        rt.edit_config(lambda config: config.update(drift_probe={"enabled": True}))
        assert_check_fails(rt, "config.toml", "drift_probe")


def test_sync_repairs_a_damaged_wrapper_it_published() -> None:
    # A wrapper that lost its SKILL.md or its marker is still the plan's: the
    # mirror repairs it in place, never publishing imported-<name> beside it.
    def delete_skill_md(path: Path) -> None:
        path.unlink()

    def drop_marker(path: Path) -> None:
        path.write_text("corrupted\n", encoding="utf-8")

    for root in (".codex/skills", "plugins/claude-mirror/skills"):
        for damage in (delete_skill_md, drop_marker):
            label = f"{root} {damage.__name__}"
            with round_trip() as rt:
                skills = rt.home / root
                skill = skills / "demo-skill" / "SKILL.md"
                published = skill.read_bytes()
                damage(skill)
                assert_check_fails(rt, str(skill), label=label)
                rt.sync_ok()
                assert skill.read_bytes() == published, label
                assert not (skills / "imported-demo-skill").exists(), label
                rt.sync_ok()

    with round_trip() as rt:
        # Damaged, then its Claude source removed: still the sync's to prune.
        skill = rt.codex / "skills" / "demo-skill" / "SKILL.md"
        skill.unlink()
        (rt.home / ".claude" / "skills" / "demo-skill" / "SKILL.md").unlink()
        (rt.home / ".claude" / "skills" / "demo-skill").rmdir()
        rt.sync_ok()
        assert not skill.parent.exists()


# The paths the production publish owns, relative to HOME: managed_paths in
# /etc/nixos/home/ai-client-config-sync.nix (nixos-config,
# home/ai-client-config-sync.nix). Its sync_tree rsyncs each one from a staged
# home rendered from scratch with --delete-after, so anything the renderer
# leaves under one of them is pruned by the next hourly publish.
PUBLISH_MANAGED_PATHS = tuple(
    Path(path)
    for path in (
        ".codex/config.toml",
        ".codex/rules",
        ".codex/scripts",
        ".codex/hooks",
        ".codex/hooks.json",
        ".codex/mcps",
        ".codex/plugins/claude-mirror",
        ".codex/agents",
        ".codex/claude-setup-mirror",
        ".codex/AGENTS.md",
        ".codex/skills",
        ".agents/plugins",
        "plugins/claude-mirror",
    )
)
# Inside those, what the production publish never prunes: sync_filters in the
# same file excludes Codex's bundled skills and protects (rsync "P") the hook
# generations live sessions may still run.
PUBLISH_NEVER_PRUNED_PATHS = (Path(".codex/skills/.system"), Path(".codex/hooks/claude-mirror"))


def test_mirror_owns_exactly_the_paths_the_production_publish_manages() -> None:
    # One declaration in the renderer, held to the nix module: a namespace
    # added on one side only would publish or prune differently by route.
    home = Path.home()
    owned = tuple(path.relative_to(home) for path in render_codex.managed_paths())
    kept = tuple(path.relative_to(home) for path in render_codex.never_pruned_paths())
    assert (owned, kept) == (PUBLISH_MANAGED_PATHS, PUBLISH_NEVER_PRUNED_PATHS)


# Where a backup machinery used to keep what the sync removed. The mirror
# keeps none: nothing may appear there or beside a replaced file.
BACKUP_ROOT = Path(".local/state/ai-client-config-sync/backups")
# What the rollback tests snapshot: the published roots, and .local, where a
# backup root used to appear.
ROLLBACK_SNAPSHOT_ROOTS = (".codex", "plugins", ".agents", ".local")
USER_SKILL_TEXT = "---\nname: demo-skill\ndescription: mine, hand written\n---\n\nMy own steps.\n"


def assert_nothing_backed_up(home: Path) -> None:
    """The mirror keeps no copy of what it replaces or prunes: none beside
    it (*.bak-*) and no backup root of its own."""
    copies = sorted(
        str(path.relative_to(home))
        for path in home.rglob("*.bak-*")
        if path.relative_to(home).parts[0] != ".claude"
    )
    assert copies == [], copies
    assert not (home / BACKUP_ROOT).exists()


def files_holding(home: Path, payload: bytes) -> list[Path]:
    found: list[Path] = []
    for dir_name, _dirs, file_names in os.walk(home):
        for name in file_names:
            path = Path(dir_name) / name
            if not path.is_symlink() and payload in path.read_bytes():
                found.append(path)
    return sorted(found)


def replace_with_users_own_skill(skill_dir: Path) -> None:
    (skill_dir / "SKILL.md").write_text(USER_SKILL_TEXT, encoding="utf-8")
    (skill_dir / "notes.md").write_text("my notes\n", encoding="utf-8")


def test_sync_replaces_a_hand_made_skill_at_a_planned_name_without_backup() -> None:
    # Codex config is a mirror: a skill directory at a name the plan
    # publishes is the plan's, whatever it holds. It is replaced whole, and
    # nothing of the hand-made one is kept anywhere.
    with round_trip() as rt:
        skill_dir = rt.codex / "skills" / "demo-skill"
        wrapper = (skill_dir / "SKILL.md").read_bytes()
        replace_with_users_own_skill(skill_dir)

        result = rt.sync()
        assert result.returncode == 0, describe(result)
        assert "ai-client-config check OK" in result.stdout, describe(result)
        assert "backed up" not in result.stdout, describe(result)
        assert (skill_dir / "SKILL.md").read_bytes() == wrapper
        assert sorted(path.name for path in skill_dir.iterdir()) == ["SKILL.md"]
        assert not (rt.codex / "skills" / "imported-demo-skill").exists()
        assert files_holding(rt.home, USER_SKILL_TEXT.encode()) == []
        assert files_holding(rt.home, b"my notes\n") == []
        assert_nothing_backed_up(rt.home)

        before = snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS)
        rt.sync_ok()
        assert snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS) == before


def test_sync_prunes_skills_the_plan_does_not_hold_but_not_codex_system_skills() -> None:
    # Both skill roots are the mirror's: a skill the plan does not publish is
    # removed whoever made it, a hand-made claude-* one included, while
    # Codex's bundled skills under .system are not the mirror's at all.
    with round_trip() as rt:
        system = rt.codex / "skills" / ".system" / "imagegen" / "SKILL.md"
        system.parent.mkdir(parents=True)
        system.write_text("---\nname: imagegen\ndescription: bundled\n---\n", encoding="utf-8")
        unplanned = [
            rt.codex / "skills" / "claude-local",
            rt.codex / "skills" / "my-tool",
            rt.plugin / "skills" / "extra",
        ]
        for skill in unplanned:
            skill.mkdir()
            (skill / "SKILL.md").write_text(USER_SKILL_TEXT, encoding="utf-8")
        loose = rt.codex / "skills" / "README.md"
        loose.write_text("notes\n", encoding="utf-8")
        unplanned.append(loose)

        result = assert_check_fails(rt, *(f"{path}: " for path in unplanned))
        assert str(system) not in result.stdout, describe(result)
        rt.sync_ok()
        for path in unplanned:
            assert not path.exists(), path
        assert system.read_text(encoding="utf-8").endswith("description: bundled\n---\n")
        assert_nothing_backed_up(rt.home)


FAIL_AFTER_SKILL_PUBLISH = """
import render_codex

def fail(plan):
    raise RuntimeError("injected failure after the skill publish")

render_codex.publish_config = fail
raise SystemExit(render_codex.main())
"""


def assert_publish_fails_after_the_skill_publish(rt: RoundTrip) -> None:
    failed = subprocess.run(
        [sys.executable, "-B", "-c", FAIL_AFTER_SKILL_PUBLISH],
        env={**os.environ, "HOME": str(rt.home), "PYTHONPATH": str(rt.repo / "scripts")},
        cwd=rt.home,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert failed.returncode != 0, describe(failed)
    assert "injected failure after the skill publish" in failed.stderr, describe(failed)


def test_failed_publish_restores_the_skill_it_replaced() -> None:
    # Rollback is generator-state recovery: a later step failing puts back
    # what the publish replaced, like every other publish write.
    with round_trip() as rt:
        replace_with_users_own_skill(rt.codex / "skills" / "demo-skill")
        before = snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS)
        assert_publish_fails_after_the_skill_publish(rt)
        assert snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS) == before


def test_sync_replaces_a_regular_file_at_a_planned_wrapper_name() -> None:
    # A file where the sync publishes a wrapper directory: a failed publish
    # puts it back, a successful one replaces it with no copy kept.
    with round_trip() as rt:
        skill_dir = rt.codex / "skills" / "demo-skill"
        wrapper = (skill_dir / "SKILL.md").read_bytes()
        shutil.rmtree(skill_dir)
        skill_dir.write_text("my notes\n", encoding="utf-8")
        before = snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS)
        assert_publish_fails_after_the_skill_publish(rt)
        assert snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS) == before

        result = rt.sync()
        assert result.returncode == 0, describe(result)
        assert "ai-client-config check OK" in result.stdout, describe(result)
        assert (skill_dir / "SKILL.md").read_bytes() == wrapper
        assert files_holding(rt.home, b"my notes\n") == []
        assert_nothing_backed_up(rt.home)
        rt.sync_ok()


def test_first_sync_takes_a_planned_skill_name_whoever_made_it() -> None:
    # Ownership comes from the plan, not from the disk: a skill already at a
    # Claude skill's name is replaced on the first sync too, never moved
    # aside to imported-<name>.
    with round_trip(synced=False) as rt:
        own = rt.codex / "skills" / "demo-skill" / "SKILL.md"
        own.parent.mkdir(parents=True)
        own.write_text(USER_SKILL_TEXT, encoding="utf-8")
        rt.sync_ok()
        assert "Mirrored from Claude skill:" in own.read_text(encoding="utf-8")
        assert not (rt.codex / "skills" / "imported-demo-skill").exists()
        assert not (rt.plugin / "skills" / "imported-demo-skill").exists()
        assert_nothing_backed_up(rt.home)


def test_check_rejects_edited_skill_wrapper_that_stays_generated() -> None:
    # An edit that keeps the generated marker must be caught by the content
    # comparison itself.
    for root in (".codex/skills", "plugins/claude-mirror/skills"):
        with round_trip() as rt:
            wrapper = rt.home / root / "demo-skill" / "SKILL.md"
            text = wrapper.read_text(encoding="utf-8")
            assert "Mirrored from Claude skill:" in text
            wrapper.write_text(text.replace("Use when", "Use whenever", 1), encoding="utf-8")
            assert_check_fails(rt, f"{wrapper}: differs from the render plan", label=root)


def test_check_rejects_artifacts_the_render_would_remove_or_repair() -> None:
    # Corrupting and deleting cannot show extras: files publish prunes or
    # repairs but the plan does not contain.
    def stale_wrapper(rt) -> Path:
        path = rt.codex / "skills" / "removed-skill"
        path.mkdir()
        (path / "SKILL.md").write_text(
            "---\nname: removed-skill\ndescription: gone\n---\n\nMirrored from Claude skill:\n",
            encoding="utf-8",
        )
        return path

    def extra_mcp_file(rt) -> Path:
        path = rt.codex / "mcps" / "git-broker" / "extra.py"
        path.write_text("# not from Claude\n", encoding="utf-8")
        return path

    def extra_in_mcp_tree(kind: str) -> Callable[[object], Path]:
        # Names the copy itself skips, and entries it never writes: the
        # publish replaces the tree whole, so each is drift all the same.
        def mutate(rt) -> Path:
            broker = rt.codex / "mcps" / "git-broker"
            if kind == "ignored file":
                path = broker / "server.log"
                path.write_text("log\n", encoding="utf-8")
            elif kind == "ignored directory":
                path = broker / "__pycache__"
                path.mkdir()
                (path / "server.cpython-313.pyc").write_bytes(b"\0")
            elif kind == "linked directory":
                path = broker / "linked"
                path.symlink_to(rt.home / ".claude", target_is_directory=True)
            else:
                path = broker / "empty"
                path.mkdir()
            return path

        return mutate

    def extra_agent_role(rt) -> Path:
        path = rt.codex / "agents" / "claude-imported" / "removed.toml"
        path.write_text('name = "removed"\n', encoding="utf-8")
        return path

    def extra_file_in_wrapper(root: str) -> Callable[[object], Path]:
        def mutate(rt) -> Path:
            path = rt.home / root / "demo-skill" / "notes.md"
            path.write_text("not from Claude\n", encoding="utf-8")
            return path

        return mutate

    def non_executable_wrapper(rt) -> Path:
        path = next((rt.codex / "hooks" / "claude-mirror").glob("stop-fanout-*.sh"))
        path.chmod(0o644)
        return path

    # (mutation, error, whether the next sync repairs it)
    cases = [
        (stale_wrapper, "not in the render plan", True),
        (extra_mcp_file, "MCP asset not in the render plan", True),
        *(
            (extra_in_mcp_tree(kind), "MCP asset not in the render plan", True)
            for kind in ("ignored file", "ignored directory", "linked directory", "empty directory")
        ),
        (extra_agent_role, "not in the render plan", True),
        (non_executable_wrapper, "hook wrapper must be mode 0755", True),
        *(
            (extra_file_in_wrapper(root), "not in the render plan", True)
            for root in (".codex/skills", "plugins/claude-mirror/skills")
        ),
    ]
    for mutate, expected, repaired in cases:
        with round_trip() as rt:
            path = mutate(rt)
            assert_check_fails(rt, f"{path}: {expected}", label=expected)
            if repaired:
                rt.sync_ok()
                if expected.endswith("not in the render plan"):
                    assert not path.exists() and not path.is_symlink(), path


def test_sync_prunes_a_nested_role_file_under_the_imported_role_root() -> None:
    # Codex discovers role files anywhere under ~/.codex/agents; one in a
    # subdirectory of claude-imported used to be neither pruned nor reported.
    with round_trip() as rt:
        nested = rt.codex / "agents" / "claude-imported" / "team" / "extra.toml"
        nested.parent.mkdir()
        nested.write_text('name = "extra"\ndescription = "x"\ndeveloper_instructions = "x"\n', encoding="utf-8")
        result = assert_check_fails(rt, f"{nested.parent}: not in the render plan")
        errors = [line for line in result.stdout.splitlines() if line.startswith("- ")]
        assert errors == [f"- {nested.parent}: not in the render plan"], describe(result)
        rt.sync_ok()
        assert not nested.parent.exists()


def test_mirror_prunes_every_unplanned_path_inside_the_managed_paths_only() -> None:
    # Inside the managed paths the plan is the whole truth: anything else is
    # reported, then pruned with no copy kept, including the *.bak-* beside a
    # managed path the production publish prunes too. Outside them, and in
    # what the production publish never prunes, nothing is touched.
    with round_trip() as rt:
        hooks = rt.codex / "hooks" / "claude-mirror"
        unplanned = {
            rt.codex / "rules" / "README.txt": "notes\n",
            rt.codex / "scripts" / "other.py": "print(1)\n",
            rt.codex / "hooks" / "other" / "x.sh": "echo x\n",
            rt.codex / "mcps" / "retired" / "server.py": "# retired\n",
            rt.codex / "claude-setup-mirror" / "notes.txt": "notes\n",
            rt.codex / "claude-setup-mirror" / "report.json.bak-20260101-000000": "{}\n",
            rt.codex / "config.toml.bak-20260101-000000": "model = 'old'\n",
            rt.codex / "AGENTS.md.bak-20260101-000000": "old\n",
            rt.home / ".agents" / "plugins" / "other.json": "{}\n",
            rt.plugin / "notes.md": "notes\n",
            rt.plugin / "agents" / "sub" / "x.md": "x\n",
        }
        kept = {
            rt.codex / "auth.json": "{}\n",
            rt.codex / "sessions" / "s.jsonl": "{}\n",
            rt.codex / "plugins" / "cache" / "p" / "x.json": "{}\n",
            rt.codex / "skills" / ".system" / "imagegen" / "SKILL.md": "bundled\n",
            hooks / "stop-000000000000000000000000.sh": "#!/usr/bin/env bash\necho old\n",
            rt.home / ".agents" / "skills" / "mine" / "SKILL.md": "mine\n",
            rt.home / "plugins" / "other-plugin" / "x.json": "{}\n",
        }
        for path, text in {**unplanned, **kept}.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        reported = {
            path.parent if path.parent.name in {"other", "retired", "sub"} else path
            for path in unplanned
        }

        result = assert_check_fails(rt)
        errors = {line for line in result.stdout.splitlines() if line.startswith("- ")}
        assert errors == {f"- {path}: not in the render plan" for path in reported}, describe(result)

        rt.sync_ok()
        for path in reported:
            assert not path.exists() and not path.is_symlink(), path
        for path, text in kept.items():
            assert path.read_text(encoding="utf-8") == text, path
        assert_nothing_backed_up(rt.home)
        before = snapshot(rt.home, ("",))
        rt.sync_ok()
        assert snapshot(rt.home, ("",)) == before


def hook_generation_files(rt) -> dict[Path, bytes]:
    root = rt.codex / "hooks" / "claude-mirror"
    return {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_sync_repairs_a_modified_content_addressed_hook_file() -> None:
    # The file name is the digest of the text the renderer writes there, so a
    # file whose content no longer hashes to its name is damage, not a prior
    # generation: the next sync rewrites it instead of failing every time.
    with round_trip() as rt:
        published = hook_generation_files(rt)
        kinds = {
            "handler wrapper": next(
                p for p in published
                if p.name.startswith("stop-") and "-fanout-" not in p.name and "-plan-" not in p.name
            ),
            "fan-out plan": next(p for p in published if p.name.startswith("stop-plan-")),
            "dispatcher": next(p for p in published if p.name.startswith("stop-fanout-")),
            "runtime": next(p for p in published if p.parent.name.startswith("runtime-")),
        }
        for label, path in kinds.items():
            path.write_text("#!/usr/bin/env bash\necho tampered\n", encoding="utf-8")
            assert_check_fails(rt, f"{path}: differs from the render plan", label=label)
            rt.sync_ok()
            rt.sync_ok()
            assert hook_generation_files(rt) == published, label


def test_sync_repairs_generator_owned_hook_paths_of_the_wrong_type() -> None:
    # A directory where the sync installs a file, or a file where it needs a
    # directory, made exists() skip the install while the check reported the
    # file missing, or made every sync raise: the paths are the sync's own.
    # The check names each file it cannot read, and why: a handler wrapper
    # twice, from the drift layer and from the wrapper invariant scan.
    def is_a_directory(path: Path) -> str:
        return f"- {path}: unreadable: IsADirectoryError: [Errno 21] Is a directory: '{path}'"

    def not_a_directory(path: Path) -> str:
        return f"- {path}: unreadable: NotADirectoryError: [Errno 20] Not a directory: '{path}'"

    def directory_at(path: Path) -> None:
        path.unlink()
        path.mkdir()
        (path / "left-behind").write_text("x\n", encoding="utf-8")

    def file_at(path: Path) -> None:
        shutil.rmtree(path)
        path.write_text("not a directory\n", encoding="utf-8")

    cases = {
        "stable adapter": lambda rt: (
            directory_at(rt.codex / "scripts" / "codex_hook_output_adapter.py"),
            rt.codex / "scripts" / "codex_hook_output_adapter.py",
        )[1],
        "stable fan-out": lambda rt: (
            directory_at(rt.codex / "scripts" / "codex_hook_fanout.py"),
            rt.codex / "scripts" / "codex_hook_fanout.py",
        )[1],
        "handler wrapper": lambda rt: (
            directory_at(path := next(
                p for p in hook_generation_files(rt)
                if p.name.startswith("stop-") and "-fanout-" not in p.name and "-plan-" not in p.name
            )),
            path,
        )[1],
        "runtime directory": lambda rt: (
            file_at(path := next(
                p for p in hook_generation_files(rt) if p.parent.name.startswith("runtime-")
            ).parent),
            path,
        )[1],
    }
    expected: dict[str, Callable[[Path], list[str]]] = {
        "stable adapter": lambda path: [is_a_directory(path)],
        "stable fan-out": lambda path: [is_a_directory(path)],
        # Drift, the fan-out's loader refusing the plan, the wrapper scan.
        "handler wrapper": lambda path: [
            is_a_directory(path),
            f"- {next(path.parent.glob('stop-plan-*.json'))}: "
            "handlers[0].wrapper must be an executable file",
            is_a_directory(path),
        ],
        "runtime directory": lambda path: [
            not_a_directory(path / "codex_hook_output_adapter.py"),
            not_a_directory(path / "codex_hook_fanout.py"),
        ],
    }
    for label, mutate in cases.items():
        with round_trip() as rt:
            published = hook_generation_files(rt)
            path = mutate(rt)
            result = assert_check_fails(rt, str(path), label=label)
            errors = [line for line in result.stdout.splitlines() if line.startswith("- ")]
            assert errors == expected[label](path), f"{label}\n{describe(result)}"
            rt.sync_ok()
            rt.sync_ok()
            assert hook_generation_files(rt) == published, label
            assert (rt.codex / "scripts" / "codex_hook_output_adapter.py").is_file(), label
            assert (rt.codex / "scripts" / "codex_hook_fanout.py").is_file(), label


def test_paths_cleared_for_the_wrong_type_are_replaced_and_rolled_back() -> None:
    # What stands where the sync installs its own file is replaced with no
    # copy kept; a publish that fails later still puts it back.
    def directory_at_a_wrapper(rt: RoundTrip) -> Path:
        root = rt.codex / "hooks" / "claude-mirror"
        path = next(
            p for p in sorted(root.glob("*.sh")) if "-fanout-" not in p.name
        )
        path.unlink()
        path.mkdir()
        (path / "mine.txt").write_text("my notes\n", encoding="utf-8")
        return path

    def file_at(relative: str) -> Callable[[RoundTrip], Path]:
        def damage(rt: RoundTrip) -> Path:
            path = rt.home / relative
            shutil.rmtree(path)
            path.write_text("my notes\n", encoding="utf-8")
            return path

        return damage

    cases = {
        "directory at a hook wrapper": directory_at_a_wrapper,
        "file at the scripts directory": file_at(".codex/scripts"),
        "file at the hook generation root": file_at(".codex/hooks/claude-mirror"),
    }
    for label, damage in cases.items():
        with round_trip() as rt:
            published = hook_generation_files(rt)
            damage(rt)
            before = snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS)
            assert_publish_fails_after_the_skill_publish(rt)
            assert snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS) == before, label

            result = rt.sync()
            assert result.returncode == 0, describe(result)
            assert "ai-client-config check OK" in result.stdout, describe(result)
            assert "backed up" not in result.stdout, describe(result)
            assert files_holding(rt.home, b"my notes\n") == [], label
            assert_nothing_backed_up(rt.home)
            assert hook_generation_files(rt) == published, label
            assert (rt.codex / "scripts" / "codex_hook_fanout.py").is_file(), label


def test_sync_replaces_every_planned_path_of_the_wrong_type() -> None:
    # Inside the managed paths a wrong type is replaced like wrong content:
    # a file where the plan needs a directory made every sync raise
    # FileExistsError, a directory where it writes a file IsADirectoryError.
    # The next sync must publish exactly what a clean one does.
    def file_at(path: Path) -> None:
        shutil.rmtree(path)
        path.write_text("not a directory\n", encoding="utf-8")

    def directory_at(path: Path) -> None:
        path.unlink()
        path.mkdir()
        (path / "left-behind").write_text("x\n", encoding="utf-8")

    def unplanned(path: Path) -> str:
        return f"- {path}: not in the render plan"

    def not_a_directory(path: Path) -> str:
        return f"- {path}: unreadable: NotADirectoryError: [Errno 20] Not a directory: '{path}'"

    def is_a_directory(path: Path) -> str:
        return f"- {path}: unreadable: IsADirectoryError: [Errno 21] Is a directory: '{path}'"

    def not_a_link(path: Path, home: Path) -> str:
        return f"- {path}: missing or not a link to {home / '.claude' / 'agents' / 'reviewer.md'}"

    # Every line pinned, drift's then the invariants': several of these paths
    # are read by both layers with the same message, so naming the path alone
    # stayed green with the drift layer blind to it.
    Lines = Callable[[Path, Path], tuple[list[str], list[str]]]
    file_cases: dict[str, Lines] = {
        ".codex/agents/claude-imported": lambda p, home: (
            [not_a_link(p / "reviewer.md", home), unplanned(p), not_a_directory(p / "reviewer.toml")],
            [not_a_directory(p / "reviewer.toml")],
        ),
        "plugins/claude-mirror/agents": lambda p, home: (
            [not_a_link(p / "reviewer.md", home), unplanned(p)],
            [],
        ),
        ".codex/rules": lambda p, home: (
            [unplanned(p), not_a_directory(p / "default.rules")],
            [not_a_directory(p / "default.rules")],
        ),
        ".codex/claude-setup-mirror": lambda p, home: (
            [
                unplanned(p),
                not_a_directory(p / "mcp.codex.toml"),
                not_a_directory(p / "report.json"),
            ],
            [not_a_directory(p / "report.json")],
        ),
        ".codex/skills/demo-skill": lambda p, home: (
            [unplanned(p), not_a_directory(p / "SKILL.md")],
            [],
        ),
    }
    read_by_both: Lines = lambda p, home: ([is_a_directory(p)], [is_a_directory(p)])  # noqa: E731
    drift_only: Lines = lambda p, home: ([is_a_directory(p)], [])  # noqa: E731
    directory_cases: dict[str, Lines] = {
        ".codex/AGENTS.md": drift_only,
        ".codex/hooks.json": read_by_both,
        ".codex/config.toml": read_by_both,
        ".codex/rules/default.rules": read_by_both,
        ".codex/agents/claude-imported/reviewer.toml": read_by_both,
        ".codex/claude-setup-mirror/report.json": read_by_both,
        ".codex/skills/demo-skill/SKILL.md": read_by_both,
        "plugins/claude-mirror/skills/demo-skill/SKILL.md": read_by_both,
        ".agents/plugins/marketplace.json": drift_only,
    }
    cases = [
        *((f"file at {relative}", relative, file_at, lines) for relative, lines in file_cases.items()),
        *(
            (f"directory at {relative}", relative, directory_at, lines)
            for relative, lines in directory_cases.items()
        ),
    ]
    for label, relative, damage, lines in cases:
        with round_trip() as rt:
            published = snapshot(rt.home)
            path = rt.home / relative
            damage(path)
            result = assert_check_fails(rt, label=label)
            errors = [line for line in result.stdout.splitlines() if line.startswith("- ")]
            drift, invariants = lines(path, rt.home)
            assert errors == drift + invariants, f"{label}\n{describe(result)}"
            result = rt.sync()
            assert result.returncode == 0, f"{label}\n{describe(result)}"
            assert "ai-client-config check OK" in result.stdout, f"{label}\n{describe(result)}"
            assert snapshot(rt.home) == published, label


def test_sync_replaces_a_symlink_where_the_plan_writes_a_file() -> None:
    # The production publish rsyncs regular files over the live ones, so a
    # link there is replaced even when it leads to the same bytes. Drift
    # read through it and passed, and config.toml, whose merge found nothing
    # to change, kept it.
    def linked(relative: str) -> Callable[[RoundTrip], Path]:
        def damage(rt: RoundTrip) -> Path:
            path = rt.home / relative
            target = rt.home / "elsewhere" / relative.replace("/", "-")
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(path, target)
            path.unlink()
            path.symlink_to(target)
            return path

        return damage

    hook_wrapper = lambda rt: next(  # noqa: E731
        path.relative_to(rt.home)
        for path in hook_generation_files(rt)
        if path.name.startswith("stop-") and path.suffix == ".sh" and "-fanout-" not in path.name
    )
    for relative in (
        ".codex/config.toml",
        ".codex/AGENTS.md",
        ".codex/hooks.json",
        ".codex/rules/default.rules",
        ".codex/skills/demo-skill/SKILL.md",
        hook_wrapper,
    ):
        with round_trip() as rt:
            published = snapshot(rt.home)
            path = linked(str(relative(rt) if callable(relative) else relative))(rt)
            result = assert_check_fails(rt, label=str(path))
            errors = [line for line in result.stdout.splitlines() if line.startswith("- ")]
            assert errors == [
                f"- {path}: a symlink where the render plan writes a regular file"
            ], describe(result)
            rt.sync_ok()
            assert snapshot(rt.home) == published, path


def test_sync_replaces_a_symlink_where_the_plan_needs_a_directory() -> None:
    # A link at a managed directory would send the publish's writes, and the
    # prune's removals, outside the mirror. The link is replaced; what it
    # pointed at is not touched.
    with round_trip() as rt:
        published = snapshot(rt.home)
        elsewhere = rt.home / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "mine.md").write_text("mine\n", encoding="utf-8")
        agents = rt.plugin / "agents"
        shutil.rmtree(agents)
        agents.symlink_to(elsewhere, target_is_directory=True)
        rt.sync_ok()
        assert not agents.is_symlink()
        assert snapshot(rt.home) == published
        assert sorted(path.name for path in elsewhere.iterdir()) == ["mine.md"]


def test_sync_rewrites_a_stable_hook_script_whose_content_differs() -> None:
    # The stable script paths hold the repository's copy, as the production
    # publish renders them from scratch: a truncated one is drift and is
    # rewritten, not left alone because it exists.
    with round_trip() as rt:
        for name in ("codex_hook_output_adapter.py", "codex_hook_fanout.py"):
            path = rt.codex / "scripts" / name
            published = path.read_bytes()
            path.write_text("", encoding="utf-8")
            result = assert_check_fails(rt, f"{path}: differs from the render plan")
            errors = [line for line in result.stdout.splitlines() if line.startswith("- ")]
            assert errors == [f"- {path}: differs from the render plan"], describe(result)
            rt.sync_ok()
            assert path.read_bytes() == published
            assert path.stat().st_mode & 0o111


def test_hook_publication_still_refuses_a_genuine_address_collision() -> None:
    # A file whose own content hashes to its name is a generation a live Codex
    # session may still run. A different text claiming the same name is a
    # real collision: overwriting would change that session's hook.
    # SHA-256 cannot be made to collide here, so hand publish a different text
    # for a path whose current content is a valid generation, as a colliding
    # digest would.
    with round_trip() as rt:
        files = hook_generation_files(rt)
        for path in (
            next(p for p in files if p.name.startswith("stop-") and "-fanout-" not in p.name and "-plan-" not in p.name),
            next(p for p in files if p.parent.name.startswith("runtime-")),
        ):
            try:
                render_codex.publish_hook_generation({path: "#!/usr/bin/env bash\necho other\n"})
            except ValueError as exc:
                assert str(exc) == f"content-addressed hook collision: {path}", exc
            else:
                raise AssertionError(f"a genuine collision at {path} was overwritten")
            assert path.read_bytes() == files[path]


def test_sync_removes_rules_files_it_does_not_generate() -> None:
    # Codex loads every *.rules file in ~/.codex/rules, not only default.rules,
    # and writes its own approvals to default.rules alone. A rules file the
    # sync did not generate is checked like default.rules, reported, and
    # removed, as is anything else there: the rules directory is the mirror's.
    with round_trip() as rt:
        rules = rt.codex / "rules"
        default = (rules / "default.rules").read_bytes()
        extra = rules / "extra.rules"
        extra.write_text('prefix_rule(pattern=["rm"], decision="allow")\n', encoding="utf-8")
        nested = rules / "team" / ".hidden.rules"
        nested.parent.mkdir()
        nested.write_text('network_rule("x.org", "https", "allow")\n', encoding="utf-8")
        unrelated = rules / "README.txt"
        unrelated.write_text("not a rules file\n", encoding="utf-8")

        assert_check_fails(
            rt,
            f"{extra}: line 1: persisted allow prefix_rule",
            f"{nested}: line 1: persisted allow network_rule",
            f"{extra}: not in the render plan",
            f"{nested.parent}: not in the render plan",
        )
        rt.sync_ok()
        assert not extra.exists() and not nested.parent.exists()
        assert not unrelated.exists()
        assert (rules / "default.rules").read_bytes() == default
        rt.sync_ok()


def test_sync_prunes_role_and_rules_files_it_does_not_render_without_backup() -> None:
    # ~/.codex/agents and ~/.codex/rules are the mirror's: a role or rules
    # file the plan does not hold is removed, a stricter forbidden-only rule
    # included, and no copy is kept. Its source belongs in the Claude config.
    role = 'name = "mine"\ndescription = "my role"\ndeveloper_instructions = "x"\n'
    forbidden = 'prefix_rule(pattern = ["rm", "-rf"], decision = "forbidden")\n'
    with round_trip() as rt:
        files = {
            Path(".codex/agents/mine.toml"): role,
            Path(".codex/agents/team/nested.toml"): role,
            Path(".codex/rules/mine.rules"): forbidden,
        }
        for relative, text in files.items():
            path = rt.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        result = rt.sync()
        assert result.returncode == 0, describe(result)
        assert "ai-client-config check OK" in result.stdout, describe(result)
        assert "backed up" not in result.stdout, describe(result)
        assert not any((rt.home / relative).exists() for relative in files)
        for text in set(files.values()):
            assert files_holding(rt.home, text.encode()) == [], text
        assert_nothing_backed_up(rt.home)

        before = snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS)
        rt.sync_ok()
        assert snapshot(rt.home, ROLLBACK_SNAPSHOT_ROOTS) == before


def test_sync_strips_a_network_allow_codex_persisted() -> None:
    with round_trip() as rt:
        rules = rt.codex / "rules" / "default.rules"
        before = rules.read_text(encoding="utf-8")
        rules.write_text(
            'network_rule(host="api.example.com", protocol="https", decision="allow")\n'
            + before,
            encoding="utf-8",
        )
        assert_check_fails(rt, f"{rules}: line 1: persisted allow network_rule")
        rt.sync_ok()
        assert "network_rule" not in rules.read_text(encoding="utf-8")


def test_nested_allow_fails_closed_with_an_actionable_message() -> None:
    # The renderer strips only top-level rules; anything else stays and
    # blocks every sync until a human removes it, and the error says so.
    with round_trip() as rt:
        rules = rt.codex / "rules" / "default.rules"
        rules.write_text(
            rules.read_text(encoding="utf-8")
            + 'if True:\n    prefix_rule(pattern=["sh"], decision="allow")\n',
            encoding="utf-8",
        )
        for _ in range(2):
            result = rt.sync()
            # As published: the render may reflow the text around its block.
            line = rules.read_text(encoding="utf-8").splitlines().index(
                '    prefix_rule(pattern=["sh"], decision="allow")'
            ) + 1
            assert result.returncode == 1, describe(result)
            errors = [entry for entry in result.stdout.splitlines() if entry.startswith("- ")]
            assert errors == [
                f"- {rules}: line {line}: persisted allow prefix_rule nested inside "
                "other code; the sync strips only top-level rules, so a human must "
                "remove it"
            ], describe(result)


def test_unreadable_top_level_rule_fails_the_render_with_file_and_line() -> None:
    # The render cannot strip a rule whose decision it cannot read; it must
    # stop with what the check would say, not a bare ValueError traceback.
    cases = {
        'prefix_rule(pattern=["sh"], decision=D)\n': "prefix_rule decision must be a literal",
        'network_rule(host="x.org", protocol="https")\n': "network_rule must declare a decision",
        'x = 1; prefix_rule(pattern=["sh"])\n': "allow prefix_rule must occupy its own lines",
    }
    for rule, problem in cases.items():
        with round_trip() as rt:
            rules = rt.codex / "rules" / "default.rules"
            before = rules.read_text(encoding="utf-8")
            rules.write_text(before + rule, encoding="utf-8")
            line = len(before.splitlines()) + 1
            for _ in range(2):
                result = rt.sync()
                assert result.returncode != 0, describe(result)
                output = result.stdout + result.stderr
                assert f"{rules}: line {line}: {problem}" in output, describe(result)
                assert "a human must fix it" in output, describe(result)

    with round_trip() as rt:
        rules = rt.codex / "rules" / "default.rules"
        rules.write_text("prefix_rule(\n", encoding="utf-8")
        result = rt.sync()
        assert result.returncode != 0, describe(result)
        assert f"{rules}: line 1: invalid exec policy" in result.stderr, describe(result)


def test_merge_inputs_the_sync_cannot_read_fail_closed_for_a_human() -> None:
    # marketplace.json lists other plugins besides the mirror's, and the
    # production publish carries no backup of it: an unreadable one is not
    # the sync's to rebuild. A rules file with one managed-block marker lost
    # no longer says where the generated rules end. Both stop every sync,
    # naming the file and what a human must do.
    def marketplace(rt) -> Path:
        return rt.home / ".agents" / "plugins" / "marketplace.json"

    def drop_end_marker(rt) -> Path:
        rules = rt.codex / "rules" / "default.rules"
        text = rules.read_text(encoding="utf-8")
        rules.write_text(text.replace(render_codex.EXECPOLICY_END + "\n", ""), encoding="utf-8")
        return rules

    def drop_start_marker(rt) -> Path:
        rules = rt.codex / "rules" / "default.rules"
        text = rules.read_text(encoding="utf-8")
        rules.write_text(text.replace(render_codex.EXECPOLICY_START + "\n", ""), encoding="utf-8")
        return rules

    def invalid_json(rt) -> Path:
        marketplace(rt).write_text("{not json", encoding="utf-8")
        return marketplace(rt)

    def not_an_object(rt) -> Path:
        marketplace(rt).write_text("[]\n", encoding="utf-8")
        return marketplace(rt)

    def plugins_not_a_list(rt) -> Path:
        marketplace(rt).write_text('{"plugins": {}}\n', encoding="utf-8")
        return marketplace(rt)

    cases = [
        (invalid_json, "invalid JSON at line 1 column 2"),
        (not_an_object, "must be a JSON object"),
        (plugins_not_a_list, "plugins must be a list of objects"),
        (drop_end_marker, f"has {render_codex.EXECPOLICY_START!r} but no {render_codex.EXECPOLICY_END!r}"),
        (drop_start_marker, f"has {render_codex.EXECPOLICY_END!r} but no {render_codex.EXECPOLICY_START!r}"),
    ]
    for mutate, problem in cases:
        with round_trip() as rt:
            path = mutate(rt)
            for _ in range(2):
                result = rt.sync()
                assert result.returncode != 0, describe(result)
                last = result.stderr.strip().splitlines()[-1]
                assert last.startswith(f"ValueError: {path}: "), describe(result)
                assert problem in last, describe(result)
                assert "a human must" in last, describe(result)


def test_hook_command_invariants_read_the_generated_wrappers() -> None:
    # hooks.json now names only a dispatcher; the Claude command text lives in
    # the wrappers its fan-out plan runs, so that is where the check looks.
    guarded = '[ -n "${CODEX_CLAUDE_MIRROR_ENABLE_HOOKS:-}" ] || exit 0; echo stopped'
    inputs = Inputs(hooks={"Stop": [{"hooks": [{"type": "command", "command": guarded}]}]})
    with round_trip(inputs, synced=False) as rt:
        for result in (rt.sync(), rt.sync()):
            assert result.returncode == 1, describe(result)
            wrapper = next(
                path
                for path in hook_generation_files(rt)
                if path.suffix == ".sh" and guarded in path.read_text(encoding="utf-8")
            )
            assert f"- {wrapper}: hook wrapper contains the blanket disable guard" in (
                result.stdout
            ), describe(result)

    with round_trip() as rt:
        wrapper = next(
            path
            for path in hook_generation_files(rt)
            if path.name.startswith("pretooluse-") and "-fanout-" not in path.name
            and path.suffix == ".sh"
        )
        wrapper.write_text(
            wrapper.read_text(encoding="utf-8") + "echo mcp__safe-bash__exec\n",
            encoding="utf-8",
        )
        assert_check_fails(rt, f"{wrapper}: hook wrapper uses legacy tool spelling")


def test_hook_invariants_reject_what_runs_outside_the_hook_generation_root() -> None:
    # The scan follows only paths under the generation root. A hooks.json
    # command or a plan wrapper naming a script anywhere else ran text the
    # disable-guard and legacy-spelling invariants never read.
    with round_trip() as rt:
        root = rt.codex / "hooks" / "claude-mirror"
        outside = rt.home / "outside.sh"
        outside.write_text("#!/usr/bin/env bash\necho outside\n", encoding="utf-8")
        outside.chmod(0o755)
        hooks = rt.codex / "hooks.json"
        original = hooks.read_bytes()
        for command, message in (
            (str(outside), f"{hooks}: Stop hook command runs {outside}, outside {root}"),
            ("echo stopped", f"{hooks}: Stop hook command names no wrapper under {root}"),
        ):
            def point(document: dict[str, object]) -> None:
                document["hooks"]["Stop"][0]["hooks"][0]["command"] = command

            rt.edit_json(hooks, point)
            assert_check_fails(rt, message, label=command)
            hooks.write_bytes(original)

        plan = next(path for path in hook_generation_files(rt) if path.name.startswith("stop-plan-"))
        rt.edit_json(plan, lambda document: document["handlers"][0].update(wrapper=str(outside)))
        assert_check_fails(rt, f"{plan}: handlers[0].wrapper {outside} is outside {root}")
        rt.sync_ok()


def without(mapping: dict[str, object], key: str) -> dict[str, object]:
    return {name: value for name, value in mapping.items() if name != key}


def test_hook_invariants_reject_a_fan_out_plan_the_runtime_cannot_load() -> None:
    # The fan-out exits 64 on any of these, so no hook of that event runs:
    # the same effect as the blanket disable guard. A plan that skipped the
    # wrapper scan silently left only the drift layer between it and Codex.
    with round_trip() as rt:
        plan = next(
            path for path in hook_generation_files(rt) if path.name.startswith("pretooluse-plan-")
        )
        document = json.loads(plan.read_text(encoding="utf-8"))
        handler = document["handlers"][0]
        cases = {
            "null": f"{plan}: top-level JSON must be an object, not null",
            "[]": f"{plan}: top-level JSON must be an object, not an array",
            json.dumps({**document, "handlers": {}}): (
                f"{plan}: hook plan must contain a handlers array"
            ),
            json.dumps({**document, "handlers": ["x"]}): f"{plan}: handlers[0] must be an object",
            json.dumps({**document, "handlers": [{**handler, "wrapper": [handler["wrapper"]]}]}): (
                f"{plan}: handlers[0].wrapper must be an absolute path"
            ),
            # Every rejection the runtime makes, not only the structural ones.
            json.dumps({**document, "handlers": [without(handler, "timeout")]}): (
                f"{plan}: handlers[0].timeout must be a positive number"
            ),
            json.dumps({**document, "handlers": [{**handler, "matcher": "(?<t>Edit)"}]}): (
                f"{plan}: handlers[0].matcher is not a valid regex: "
            ),
            json.dumps({**document, "handlers": [without(handler, "adapt_file_tools")]}): (
                f"{plan}: handlers[0].adapt_file_tools must be a boolean"
            ),
        }
        original = plan.read_bytes()
        for payload, message in cases.items():
            plan.write_text(payload + "\n", encoding="utf-8")
            assert_check_fails(rt, message, label=payload)
        plan.write_bytes(original)
        checked = rt.check()
        assert checked.returncode == 0, describe(checked)


def test_a_claude_matcher_the_fan_out_cannot_compile_fails_the_sync() -> None:
    # Claude matchers are JavaScript regexes; (?<name>...) is not Python's.
    # Rendered faithfully, the plan held it, drift was clean and the check
    # passed, while the dispatcher exited 64 and no PreToolUse hook ran.
    hooks = {
        "PreToolUse": [
            {"matcher": "(?<t>Edit)|Write", "hooks": [{"type": "command", "command": "echo guard"}]}
        ]
    }
    with round_trip(Inputs(hooks=hooks), synced=False) as rt:
        result = rt.sync()
        assert result.returncode == 1, describe(result)
        plan = next(
            path for path in hook_generation_files(rt) if path.name.startswith("pretooluse-plan-")
        )
        assert f"- {plan}: handlers[0].matcher is not a valid regex: " in result.stdout, (
            describe(result)
        )


def test_mcp_floors_hold_in_the_plugin_mcp_json_too() -> None:
    # Codex loads the plugin's .mcp.json as well as config.toml; the same
    # servers registered there must keep the same floors, read from the file.
    def loosen(document: dict[str, object]) -> None:
        servers = document["mcpServers"]
        servers["git_broker"]["disabled_tools"] = []
        servers["git_broker"]["required"] = False
        servers["github"]["disabled_tools"].remove("merge_pull_request")

    with round_trip() as rt:
        path = rt.plugin / ".mcp.json"
        rt.edit_json(path, loosen)
        assert_check_fails(
            rt,
            f"{path}: mcpServers.git_broker must be required",
            f"{path}: mcpServers.git_broker must not expose the generic exec tool",
            f"{path}: mcpServers.github: GitHub server must disable merge_pull_request",
        )
        rt.sync_ok()

    with round_trip() as rt:
        path = rt.plugin / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": []}), encoding="utf-8")
        assert_check_fails(rt, f"{path}: mcpServers must be an object")
        rt.sync_ok()


def test_invariants_reject_a_json_document_that_is_not_an_object() -> None:
    # A literal null parsed to None, which every caller read as "already
    # reported", so its floors were never checked. A present file must hold
    # an object; an absent one is reported as missing, as before.
    with round_trip() as rt:
        documents = (
            rt.plugin / ".mcp.json",
            rt.codex / "hooks.json",
            rt.plugin / "hooks.json",
            rt.codex / "claude-setup-mirror" / "report.json",
        )
        for path in documents:
            original = path.read_bytes()
            for payload, kind in (("null", "null"), ("[]", "an array"), ('"text"', "a string")):
                path.write_text(payload + "\n", encoding="utf-8")
                assert_check_fails(
                    rt, f"{path}: top-level JSON must be an object, not {kind}", label=str(path)
                )
            path.unlink()
            result = assert_check_fails(rt, f"{path}: missing", label=str(path))
            assert f"{path}: top-level JSON" not in result.stdout, describe(result)
            path.write_bytes(original)
        checked = rt.check()
        assert checked.returncode == 0, describe(checked)


def test_agent_role_files_cannot_loosen_the_session() -> None:
    # Codex applies an agent role's config_file as a config layer over the
    # session: approval_policy = "on-request" there re-enables escalation out
    # of the sandbox (codex debug prompt-input shows the escalation text with
    # the profile active). Only keys a role needs, with floored values, pass.
    with round_trip() as rt:
        role = rt.codex / "agents" / "claude-imported" / "reviewer.toml"
        text = role.read_text(encoding="utf-8")
        assert 'sandbox_mode = "read-only"' in text
        role.write_text(
            text.replace('sandbox_mode = "read-only"', 'sandbox_mode = "danger-full-access"')
            + 'approval_policy = "on-request"\n'
            + '[mcp_servers.git_broker]\ndisabled_tools = []\n',
            encoding="utf-8",
        )
        # Codex also discovers role files under ~/.codex/agents by itself.
        extra = rt.codex / "agents" / "extra.toml"
        extra.write_text(
            'name = "extra"\ndescription = "x"\ndeveloper_instructions = "x"\n'
            'default_permissions = "other"\n',
            encoding="utf-8",
        )
        assert_check_fails(
            rt,
            f"{role}: sandbox_mode must be read-only or workspace-write",
            f"{role}: approval_policy must be absent or never",
            f"{role}: mcp_servers must not be set in an agent role file",
            f"{extra}: default_permissions must not be set in an agent role file",
            f"{extra}: not in the render plan",
        )
        rt.sync_ok()
        assert role.read_text(encoding="utf-8") == text
        assert not extra.exists()

    with round_trip() as rt:
        # A config_file Codex is pointed at outside the role root is read too.
        outside = rt.home / "role.toml"
        outside.write_text('name = "x"\napproval_policy = "on-request"\n', encoding="utf-8")
        rt.edit_config(
            lambda config: config["agents"]["reviewer"].update(config_file=str(outside))
        )
        assert_check_fails(rt, f"{outside}: approval_policy must be absent or never")
        rt.sync_ok()


def test_frontmatter_invariant_covers_plugin_and_command_wrappers() -> None:
    # Codex reads skills from both roots, and commands render as skills too.
    with round_trip() as rt:
        plugin_skill = rt.plugin / "skills" / "demo-skill" / "SKILL.md"
        plugin_skill.write_text(
            "---\nname: demo-skill\n---\n\nMirrored from Claude skill:\n", encoding="utf-8"
        )
        command = rt.codex / "skills" / "claude-command-hello" / "SKILL.md"
        assert "Mirrored from Claude command:" in command.read_text(encoding="utf-8")
        command.write_text("Mirrored from Claude command:\n", encoding="utf-8")
        assert_check_fails(
            rt,
            f"{plugin_skill}: missing description",
            f"{command}: missing frontmatter",
        )
        rt.sync_ok()


def write_agent(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Agent {name}.\n---\n\nDo the {name} work.\n",
        encoding="utf-8",
    )


def plugin_kit(rt) -> Path:
    return rt.home / "claude-plugins" / "enabled-kit"


def test_sync_converges_after_claude_agents_are_removed_or_renamed() -> None:
    claude_agents = lambda rt: rt.home / ".claude" / "agents"  # noqa: E731
    role_root = lambda rt: rt.codex / "agents" / "claude-imported"  # noqa: E731

    with round_trip() as rt:
        (claude_agents(rt) / "reviewer.md").unlink()
        rt.sync_ok()
        assert not (role_root(rt) / "reviewer.toml").exists()

    with round_trip() as rt:
        (claude_agents(rt) / "reviewer.md").rename(claude_agents(rt) / "critic.md")
        write_agent(claude_agents(rt) / "critic.md", "critic")
        rt.sync_ok()
        assert (role_root(rt) / "critic.toml").is_file()
        assert not (role_root(rt) / "reviewer.toml").exists()

    with round_trip(Inputs(plugins=True), synced=False) as rt:
        write_agent(plugin_kit(rt) / "agents" / "kit-agent.md", "kit-agent")
        rt.sync_ok()
        assert (role_root(rt) / "kit-agent.toml").is_file()
        rt.edit_json(
            rt.home / ".claude" / "settings.json",
            lambda settings: settings["enabledPlugins"].update({"enabled-kit@example": False}),
        )
        rt.sync_ok()
        assert not (role_root(rt) / "kit-agent.toml").exists()


def test_sync_prunes_references_to_removed_agents_and_disabled_plugins() -> None:
    with round_trip(Inputs(plugins=True)) as rt:
        agent_reference = rt.plugin / "agents" / "reviewer.md"
        plugin_link = rt.plugin / "mirrored-plugins" / "claude-plugin-enabled-kit-example"
        assert agent_reference.is_symlink() and plugin_link.is_symlink()

        (rt.home / ".claude" / "agents" / "reviewer.md").unlink()
        rt.edit_json(
            rt.home / ".claude" / "settings.json",
            lambda settings: settings["enabledPlugins"].update({"enabled-kit@example": False}),
        )
        imported = rt.run("import_claude.py")
        assert imported.returncode == 0, describe(imported)
        assert_check_fails(
            rt,
            f"{agent_reference}: not in the render plan",
            # The only link: its whole directory is what the plan lacks.
            f"{plugin_link.parent}: not in the render plan",
        )

        rt.sync_ok()
        for path in (agent_reference, plugin_link, plugin_link.parent):
            assert not path.exists() and not path.is_symlink(), path
        rt.sync_ok()


def test_agents_sharing_a_file_name_get_distinct_references() -> None:
    # A user agent and a plugin agent may both live in a file named
    # reviewer.md; their names, which Claude keeps unique, tell them apart.
    with round_trip(Inputs(plugins=True), synced=False) as rt:
        kit_source = plugin_kit(rt) / "agents" / "reviewer.md"
        write_agent(kit_source, "kit-reviewer")
        rt.sync_ok()
        rt.sync_ok()
        user_source = rt.home / ".claude" / "agents" / "reviewer.md"
        for root in (rt.plugin / "agents", rt.codex / "agents" / "claude-imported"):
            references = {
                name: root / f"{name}.md" for name in ("reviewer", "kit-reviewer")
            }
            for name, reference in references.items():
                assert reference.is_symlink(), reference
            targets = {name: Path(os.readlink(path)) for name, path in references.items()}
            assert targets == {"reviewer": user_source, "kit-reviewer": kit_source}, targets


def test_a_generated_name_cannot_aim_a_write_outside_the_mirror() -> None:
    # An agent's name becomes a file name in two managed paths. Named
    # ../../../x it wrote ~/x.toml and ~/x.md and the check passed, since
    # containment was compared lexically. The plan refuses it before any write.
    with round_trip() as rt:
        published = snapshot(rt.home)
        write_agent(rt.home / ".claude" / "agents" / "a.md", "../../../x")
        result = rt.sync()
        assert result.returncode != 0, describe(result)
        assert "ValueError: " in result.stderr and "'..'" in result.stderr, describe(result)
        assert sorted(rt.home.rglob("x.*")) == [], describe(result)
        assert snapshot(rt.home) == published
        assert_check_fails(rt, "cannot compare with the render plan: ValueError: ")


def test_sync_follows_symlinked_mcp_sources_like_the_copy_does() -> None:
    # ~/.claude is a deploy projection: an MCP source, or a directory inside
    # it, may be a symlink. The copy follows links, so the check must too.
    with round_trip(synced=False) as rt:
        mcps = rt.home / ".claude" / "mcps"
        real = rt.home / "projection" / "git-broker"
        real.parent.mkdir()
        (mcps / "git-broker").rename(real)
        (mcps / "git-broker").symlink_to(real, target_is_directory=True)
        shared = rt.home / "projection" / "shared-lib"
        shared.mkdir()
        (shared / "helpers.py").write_text("# shared helpers\n", encoding="utf-8")
        (real / "lib").symlink_to(shared, target_is_directory=True)
        rt.sync_ok()
        copied = rt.codex / "mcps" / "git-broker"
        assert (copied / "lib" / "helpers.py").read_text(encoding="utf-8") == "# shared helpers\n"
        rt.sync_ok()


def test_check_accepts_config_comments_and_reordering() -> None:
    # The comparison is semantic, the same no-op test publish_config uses.
    with round_trip() as rt:
        config = rt.load_config()
        reordered = dict(reversed(list(config.items())))
        rt.write_config(reordered)
        with rt.config.open("a", encoding="utf-8") as stream:
            stream.write("# skills.max_context_tokens is a comment, not a key\n")
        result = rt.check()
        assert result.returncode == 0, describe(result)


def test_check_reports_toml_parse_errors_as_invalid_toml() -> None:
    with round_trip() as rt:
        rt.config.write_text('[projects."/repo"]\ntrust_level = \n', encoding="utf-8")
        assert_check_fails(rt, "config.toml: invalid TOML")


def test_check_reports_other_failures_with_their_real_type() -> None:
    with round_trip() as rt:
        marketplace = rt.home / ".agents" / "plugins" / "marketplace.json"
        marketplace.write_text("{not json", encoding="utf-8")
        result = assert_check_fails(rt, "JSONDecodeError")
        assert "invalid TOML" not in result.stdout, describe(result)

    with round_trip() as rt:
        hooks = rt.codex / "hooks.json"
        hooks.unlink()
        hooks.mkdir()
        assert_check_fails(rt, "IsADirectoryError")

    with round_trip() as rt:
        # A bug in the check itself still fails closed, and says what broke.
        rt.edit_json(rt.manifest, lambda manifest: manifest["plugins"].append({}))
        result = assert_check_fails(rt, "check crashed: KeyError: 'id'")
        assert "Traceback" in result.stderr, describe(result)


def test_report_discloses_research_agent_fallback() -> None:
    files = render_codex.prepare_report_files(
        {"source_settings": {"skill_listing_budget_fraction": 0.03}},
        [],
        [],
        [],
        [],
        {},
        render_codex.load_support(),
    )
    report = json.loads(files[render_codex.REPORT_DIR / "report.json"])
    gaps = "\n".join(report["known_gaps"])
    assert "research-agent normal/deep" in gaps
    assert "automatically retries with Codex" in gaps


if __name__ == "__main__":
    test_round_trip_matrix_checks_clean_and_rerenders_as_no_op()
    test_unchanged_hourly_run_from_a_staged_home_publishes_nothing()
    test_check_fails_for_every_corrupted_or_deleted_published_file()
    test_drift_check_itself_catches_parseable_edits_of_structured_files()
    test_check_rejects_config_key_the_render_does_not_produce()
    test_sync_repairs_a_damaged_wrapper_it_published()
    test_mirror_owns_exactly_the_paths_the_production_publish_manages()
    test_sync_replaces_a_hand_made_skill_at_a_planned_name_without_backup()
    test_sync_prunes_skills_the_plan_does_not_hold_but_not_codex_system_skills()
    test_failed_publish_restores_the_skill_it_replaced()
    test_sync_replaces_a_regular_file_at_a_planned_wrapper_name()
    test_first_sync_takes_a_planned_skill_name_whoever_made_it()
    test_check_rejects_edited_skill_wrapper_that_stays_generated()
    test_check_rejects_artifacts_the_render_would_remove_or_repair()
    test_sync_prunes_a_nested_role_file_under_the_imported_role_root()
    test_mirror_prunes_every_unplanned_path_inside_the_managed_paths_only()
    test_sync_repairs_a_modified_content_addressed_hook_file()
    test_sync_repairs_generator_owned_hook_paths_of_the_wrong_type()
    test_paths_cleared_for_the_wrong_type_are_replaced_and_rolled_back()
    test_sync_replaces_every_planned_path_of_the_wrong_type()
    test_sync_replaces_a_symlink_where_the_plan_writes_a_file()
    test_sync_replaces_a_symlink_where_the_plan_needs_a_directory()
    test_sync_rewrites_a_stable_hook_script_whose_content_differs()
    test_hook_publication_still_refuses_a_genuine_address_collision()
    test_sync_removes_rules_files_it_does_not_generate()
    test_sync_prunes_role_and_rules_files_it_does_not_render_without_backup()
    test_sync_strips_a_network_allow_codex_persisted()
    test_nested_allow_fails_closed_with_an_actionable_message()
    test_unreadable_top_level_rule_fails_the_render_with_file_and_line()
    test_merge_inputs_the_sync_cannot_read_fail_closed_for_a_human()
    test_hook_command_invariants_read_the_generated_wrappers()
    test_hook_invariants_reject_what_runs_outside_the_hook_generation_root()
    test_hook_invariants_reject_a_fan_out_plan_the_runtime_cannot_load()
    test_a_claude_matcher_the_fan_out_cannot_compile_fails_the_sync()
    test_mcp_floors_hold_in_the_plugin_mcp_json_too()
    test_invariants_reject_a_json_document_that_is_not_an_object()
    test_agent_role_files_cannot_loosen_the_session()
    test_frontmatter_invariant_covers_plugin_and_command_wrappers()
    test_sync_converges_after_claude_agents_are_removed_or_renamed()
    test_sync_prunes_references_to_removed_agents_and_disabled_plugins()
    test_agents_sharing_a_file_name_get_distinct_references()
    test_a_generated_name_cannot_aim_a_write_outside_the_mirror()
    test_sync_follows_symlinked_mcp_sources_like_the_copy_does()
    test_check_accepts_config_comments_and_reordering()
    test_check_reports_toml_parse_errors_as_invalid_toml()
    test_check_reports_other_failures_with_their_real_type()
    test_report_discloses_research_agent_fallback()
    print("sync round-trip tests OK")
