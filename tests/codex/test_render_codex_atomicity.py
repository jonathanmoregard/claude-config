#!/usr/bin/env python3
from __future__ import annotations

from contextlib import ExitStack, contextmanager, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Iterator
from unittest.mock import patch


RENDERER = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(RENDERER / "scripts"))

import common  # noqa: E402
import render_codex  # noqa: E402
from mcp_test_support import shared_broker_registrations  # noqa: E402


def budget_support() -> dict[str, object]:
    return {
        "defaults": {"skills": "shared", "plugin_skills": "shared"},
        "settings": {
            "skillListingBudgetFraction": {
                "status": "degraded",
                "codex_native_fraction": 0.02,
                "codex_version_verified": "0.146.0",
                "strategy": "compact-trigger-descriptions",
            }
        },
    }


def write_fixture(root: Path) -> tuple[dict[str, object], Path, Path, Path]:
    codex = root / ".codex"
    plugin = root / "plugin"
    claude = root / ".claude"
    claude.mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("fixture instructions\n", encoding="utf-8")
    git_broker = claude / "mcps" / "git-broker"
    git_broker.mkdir(parents=True)
    (git_broker / "server.py").write_text("# structured broker\n", encoding="utf-8")
    (git_broker / "config.json").write_text(
        '{"gitBroker": {"repositoryRoots": ["/repos"]}}\n', encoding="utf-8"
    )
    source_md = root / "source" / "new-skill" / "SKILL.md"
    source_md.parent.mkdir(parents=True)
    source_md.write_text(
        "---\nname: new-skill\ndescription: Use when testing atomic render failures.\n---\n",
        encoding="utf-8",
    )

    for skills_root, label in (
        (codex / "skills", "codex"),
        (plugin / "skills", "plugin"),
    ):
        generated = skills_root / "old-skill"
        generated.mkdir(parents=True)
        (generated / "SKILL.md").write_text(
            "Mirrored from Claude skill:\n" + label + " old wrapper\n",
            encoding="utf-8",
        )
        (generated / "retained.bin").write_bytes(b"\x00old-" + label.encode())
        (generated / "retained-link").symlink_to("retained.bin")
        local = skills_root / "local-skill"
        local.mkdir(parents=True)
        (local / "SKILL.md").write_text(label + " user owned\n", encoding="utf-8")

    manifest: dict[str, object] = {
        "source_settings": {"skill_listing_budget_fraction": 0.03},
        "skills": [
            {
                "name": "new-skill",
                "path": str(source_md.parent),
                "skill_md": str(source_md),
                "description": "Use when testing atomic render failures.",
            }
        ],
        "plugin_skills": [],
        "plugins": [],
        "hooks": {},
        "mcp_servers": shared_broker_registrations(claude),
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = codex / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[features]\nhooks = true\n", encoding="utf-8")
    return manifest, manifest_path, codex, plugin


def tree_state(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    """Every entry under root: a link by target, a file by bytes and mode,
    a directory by mode, so a change of mode alone is a difference too."""
    if not root.exists() and not root.is_symlink():
        return {}
    state: dict[str, tuple[str, bytes | str, int]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else str(path.relative_to(root))
        if path.is_symlink():
            state[relative] = ("symlink", os.readlink(path), 0)
        elif path.is_dir():
            state[relative] = ("directory", b"", path.stat().st_mode & 0o7777)
        else:
            state[relative] = ("file", path.read_bytes(), path.stat().st_mode & 0o7777)
    return state


@contextmanager
def isolated_renderer(
    root: Path,
    manifest_path: Path,
    codex: Path,
    plugin: Path,
    support: dict[str, object],
) -> Iterator[None]:
    with (
        patch.object(render_codex, "MANIFEST", manifest_path),
        patch.object(render_codex, "CLAUDE", root / ".claude"),
        patch.object(render_codex, "CODEX", codex),
        patch.object(render_codex, "PLUGIN_ROOT", plugin),
        patch.object(
            render_codex,
            "LEGACY_PLUGIN_LINK",
            codex / "plugins" / "claude-mirror",
        ),
        patch.object(
            render_codex,
            "HOOK_OUTPUT_ADAPTER",
            codex / "scripts" / "codex_hook_output_adapter.py",
        ),
        patch.object(
            render_codex,
            "HOOK_FANOUT",
            codex / "scripts" / "codex_hook_fanout.py",
        ),
        patch.object(
            render_codex,
            "HOOK_WRAPPER_ROOT",
            codex / "hooks" / "claude-mirror",
        ),
        patch.object(render_codex, "AGENTS_HOME", root / ".agents"),
        patch.object(render_codex, "REPORT_DIR", codex / "report"),
        patch.object(render_codex, "RESERVED_SKILL_NAMES", frozenset()),
        patch.object(render_codex, "load_support", return_value=support),
        # The fixture root, which holds every output path patched above,
        # stands for the stage publish_render_plan writes only into.
        common.staged_output(root),
    ):
        yield


def assert_wrapper_trees_unchanged(
    codex: Path,
    plugin: Path,
    before_codex: dict[str, tuple[str, bytes | str, int]],
    before_plugin: dict[str, tuple[str, bytes | str, int]],
) -> None:
    # By tree_state, not bytes alone: a failed render that changed a
    # wrapper's mode, a directory or a link target changed the tree too.
    assert tree_state(codex / "skills") == before_codex
    assert tree_state(plugin / "skills") == before_plugin
    assert not (codex / "skills" / "new-skill").exists()
    assert not (plugin / "skills" / "new-skill").exists()


def live_state(
    codex: Path, plugin: Path, agents: Path
) -> dict[str, dict[str, tuple[str, bytes | str, int]]]:
    return {
        "codex": tree_state(codex),
        "plugin": tree_state(plugin),
        "agents": tree_state(agents),
    }


def test_invalid_live_config_preserves_both_wrapper_trees() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        config = codex / "config.toml"
        config.write_text('[projects."/repo"]\ntrust_level = \n', encoding="utf-8")
        original_config = config.read_bytes()
        before_codex = tree_state(codex / "skills")
        before_plugin = tree_state(plugin / "skills")

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except ValueError as exc:
                assert "invalid config.toml" in str(exc)
            else:
                raise AssertionError("invalid live config was accepted")

        assert config.read_bytes() == original_config
        assert_wrapper_trees_unchanged(
            codex, plugin, before_codex, before_plugin
        )


def test_runtime_state_written_after_prepare_survives() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        config = codex / "config.toml"

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            plan = render_codex.prepare_render_plan(
                manifest, render_codex.load_support()
            )
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                '[projects."/new"]\ntrust_level = "trusted"\n', encoding="utf-8"
            )
            render_codex.publish_render_plan(plan)

        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        assert parsed["projects"] == {"/new": {"trust_level": "trusted"}}


def test_sync_names_replaced_values_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        config = codex / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[shell_environment_policy]\ninherit = "core"\n', encoding="utf-8")
        err = StringIO()

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ), redirect_stderr(err):
            render_codex.main()

        assert err.getvalue().count("shell_environment_policy") == 1, err.getvalue()


def test_value_identical_reformat_is_not_rewritten() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        config = codex / "config.toml"

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            render_codex.main()
            rendered = config.read_text(encoding="utf-8")
            assert "[tui]" not in rendered and "[notice]" not in rendered
            # Kept tables render notice-first; this file has them the other
            # way round, with comments and blank lines stripped.
            reformatted = "\n".join(
                line for line in rendered.splitlines()
                if line.strip() and not line.startswith("#")
            ) + "\n[tui]\nx = 1\n[notice]\ny = 2\n"
            config.write_text(reformatted, encoding="utf-8")
            files_before = sorted(codex.glob("config.toml*"))
            render_codex.main()

        assert config.read_text(encoding="utf-8") == reformatted
        assert sorted(codex.glob("config.toml*")) == files_before


def test_enabled_plugin_missing_id_fails_before_any_live_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        agents = root / ".agents"
        manifest["plugins"] = [{"enabled": True}]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = live_state(codex, plugin, agents)

        failure: Exception | None = None
        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except Exception as exc:
                failure = exc

        assert live_state(codex, plugin, agents) == before
        assert isinstance(failure, ValueError), failure
        assert "plugin" in str(failure).lower()
        assert "id" in str(failure).lower()


def test_file_at_desired_skill_path_is_replaced_by_the_wrapper() -> None:
    # Codex config is a mirror: a skill path the plan publishes is the plan's,
    # so a regular file there is replaced, not refused, and no copy is kept.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        desired = codex / "skills" / "new-skill"
        desired.write_bytes(b"user-owned regular file\n")

        with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
            render_codex.main()

        assert sorted(path.name for path in desired.iterdir()) == ["SKILL.md"]
        assert "Mirrored from Claude skill:" in (desired / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert not list(codex.rglob("*.bak-*"))


def test_malformed_config_inputs_fail_before_any_live_mutation() -> None:
    # (override, exception type, message). The permissions case has no
    # validation of its own: extending the writable roots with a non-list
    # raises, and that is what stops it before publication.
    cases = {
        "settings": (
            {"settings": {"sandbox": "invalid"}},
            TypeError,
            "Claude sandbox settings must be an object",
        ),
        # arftl-allow: permission-widened (fixture: malformed input the render must reject)
        "permissions": (
            {
                # arftl-allow: permission-widened (fixture, as above)
                "permissions": {
                    "defaultMode": "acceptEdits",
                    "additionalDirectories": 7,
                }
            },
            TypeError,
            "'int' object is not iterable",
        ),
    }
    for name, (override, expected_type, expected_message) in cases.items():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, manifest_path, codex, plugin = write_fixture(root)
            manifest.update(override)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            agents = root / ".agents"
            before = live_state(codex, plugin, agents)

            with isolated_renderer(
                root, manifest_path, codex, plugin, budget_support()
            ):
                try:
                    render_codex.main()
                except Exception as exc:
                    assert type(exc) is expected_type, (name, exc)
                    assert str(exc) == expected_message, (name, exc)
                else:
                    raise AssertionError(f"malformed {name} was accepted")

            assert live_state(codex, plugin, agents) == before, name


def test_invalid_marketplace_json_fails_before_any_live_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        agents = root / ".agents"
        marketplace = agents / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        marketplace.write_text("{invalid", encoding="utf-8")
        before = live_state(codex, plugin, agents)

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except ValueError as exc:
                assert str(marketplace) in str(exc), exc
            else:
                raise AssertionError("invalid marketplace JSON was accepted")

        assert live_state(codex, plugin, agents) == before


def test_invalid_reference_source_fails_before_any_live_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        invalid_agent = root / "source" / "not-an-agent-file"
        invalid_agent.mkdir()
        manifest["agents"] = [
            {"name": "invalid-agent", "path": str(invalid_agent)}
        ]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        agents = root / ".agents"
        before = live_state(codex, plugin, agents)

        failure: Exception | None = None
        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except Exception as exc:
                failure = exc

        assert live_state(codex, plugin, agents) == before
        assert isinstance(failure, ValueError), failure
        assert "agent" in str(failure).lower()


def test_invalid_mcp_asset_config_fails_before_any_live_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        git_broker = root / ".claude" / "mcps" / "git-broker"
        (git_broker / "config.json").write_text(
            '{"notGitBroker": {}}\n', encoding="utf-8"
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        agents = root / ".agents"
        before = live_state(codex, plugin, agents)

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except ValueError as exc:
                assert str(exc) == (
                    "Codex Git broker policy must contain a non-empty gitBroker object"
                ), exc
            else:
                raise AssertionError("invalid MCP asset config was accepted")

        assert live_state(codex, plugin, agents) == before


def test_missing_mcp_asset_fails_before_any_live_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        shutil.rmtree(root / ".claude" / "mcps" / "git-broker")
        agents = root / ".agents"
        before = live_state(codex, plugin, agents)

        failure: Exception | None = None
        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except (FileNotFoundError, ValueError) as exc:
                failure = exc
            else:
                raise AssertionError("missing MCP asset was accepted")

        assert failure is not None
        assert live_state(codex, plugin, agents) == before


STOP_HOOKS = {"Stop": [{"hooks": [{"type": "command", "command": "echo stopped"}]}]}


def test_serialization_failure_occurs_before_any_live_mutation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        agents = root / ".agents"
        before = live_state(codex, plugin, agents)

        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(
                render_codex,
                "dump_json",
                side_effect=TypeError("injected serialization failure"),
            ),
        ):
            try:
                render_codex.main()
            except TypeError as exc:
                assert "serialization" in str(exc)
            else:
                raise AssertionError("serialization failure was not raised")

        assert live_state(codex, plugin, agents) == before


def test_publication_never_serializes_so_it_cannot_fail_on_it() -> None:
    # The failure above fires at the first dump_json call, in prepare. Make it
    # fire only once publish_render_plan has started: every file text must
    # already be in the plan, so publication serializes nothing.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        manifest["hooks"] = STOP_HOOKS
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        agents = root / ".agents"
        publishing = False
        serialized_while_publishing: list[str] = []
        real_dump_json = render_codex.dump_json
        real_publish = render_codex.publish_render_plan

        def dump_json(value: object) -> str:
            if publishing:
                serialized_while_publishing.append(type(value).__name__)
                raise TypeError("injected serialization failure during publication")
            return real_dump_json(value)

        def publish(plan: render_codex.RenderPlan, **kwargs: bool) -> None:
            nonlocal publishing
            publishing = True
            return real_publish(plan, **kwargs)

        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(render_codex, "dump_json", side_effect=dump_json),
            patch.object(render_codex, "publish_render_plan", side_effect=publish),
        ):
            before = live_state(codex, plugin, agents)
            try:
                render_codex.main()
            except TypeError:
                assert live_state(codex, plugin, agents) == before
                raise
        assert publishing
        assert serialized_while_publishing == []
        assert any((codex / "hooks" / "claude-mirror").glob("stop-*.sh"))


@contextmanager
def watching_for_missing(paths: list[Path]) -> Iterator[list[str]]:
    """Record each of paths found missing at any copy, link, replace or
    removal made inside the block. A directory swapped in by two renames is
    missing only between them, which this does not observe."""
    missing: list[str] = []

    def observing(name: str, real):
        def call(*args: object, **kwargs: object) -> object:
            missing.extend(
                f"{path} before {name}"
                for path in paths
                if not path.exists() and not path.is_symlink()
            )
            return real(*args, **kwargs)

        return call

    with ExitStack() as observers:
        for owner, name in (
            (shutil, "copy2"),
            (shutil, "copytree"),
            (shutil, "rmtree"),
            (os, "replace"),
            (os, "unlink"),
            (Path, "symlink_to"),
        ):
            observers.enter_context(
                patch.object(owner, name, observing(name, getattr(owner, name)))
            )
        yield missing


def test_a_no_op_publish_leaves_mcp_assets_in_place() -> None:
    # Each publish removed ~/.codex/mcps/git-broker and copied it back, so a
    # Codex session starting meanwhile found the required broker missing,
    # even when nothing had changed.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        broker = codex / "mcps" / "git-broker"
        with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
            render_codex.main()
            before = (broker.stat().st_ino, (broker / "server.py").stat().st_ino)
            with watching_for_missing([broker / "server.py"]) as missing:
                render_codex.main()
        assert missing == [], missing
        assert (broker.stat().st_ino, (broker / "server.py").stat().st_ino) == before


def test_a_changed_mcp_asset_replaces_the_old_one_whole_never_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        broker = codex / "mcps" / "git-broker"
        with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
            render_codex.main()
            (broker / "stale.txt").write_text("from an earlier sync\n", encoding="utf-8")
            (root / ".claude" / "mcps" / "git-broker" / "server.py").write_text(
                "# structured broker, changed\n", encoding="utf-8"
            )
            with watching_for_missing([broker / "server.py"]) as missing:
                render_codex.main()
        assert missing == [], missing
        assert (broker / "server.py").read_text(encoding="utf-8") == (
            "# structured broker, changed\n"
        )
        assert not (broker / "stale.txt").exists()
        assert not [entry.name for entry in broker.parent.iterdir() if entry.name.startswith(".")]


def test_a_retargeted_reference_link_is_never_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old, new = root / "old.md", root / "new.md"
        old.write_text("old\n", encoding="utf-8")
        new.write_text("new\n", encoding="utf-8")
        link = root / "refs" / "agent.md"
        link.parent.mkdir()
        link.symlink_to(old)
        with watching_for_missing([link]) as missing:
            common.link_or_copy(new, link)
        assert missing == [], missing
        assert os.readlink(link) == str(new)
        assert [entry.name for entry in link.parent.iterdir()] == ["agent.md"]


def test_an_interrupt_between_the_two_renames_leaves_what_stood_at_dst() -> None:
    # Replacing a directory takes two renames: what stands at dst moves into
    # the holder, then the new entry moves in. A Ctrl-C between them unwound
    # through `finally: rmtree(holder)`, deleting the moved-aside original
    # and leaving dst missing.
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "AGENTS.md"
        dst.mkdir()
        (dst / "hand-made").write_text("original\n", encoding="utf-8")
        before = tree_state(Path(td))
        real = os.rename
        renames = 0

        def interrupted_after_the_first(src: object, target: object) -> None:
            nonlocal renames
            renames += 1
            real(src, target)
            if renames == 1:
                raise KeyboardInterrupt

        try:
            with patch.object(os, "rename", interrupted_after_the_first):
                common.replace_entry(dst, lambda entry: entry.write_text("new\n", encoding="utf-8"))
        except KeyboardInterrupt:
            # arftl-allow: swallowed-exception (the injected interrupt; the state check below is the test)
            pass
        else:
            raise AssertionError("the injected interrupt was not raised")
        assert tree_state(Path(td)) == before


def test_unclassified_hook_preserves_both_wrapper_trees() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        manifest["hooks"] = {"FutureHookEvent": []}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before_codex = tree_state(codex / "skills")
        before_plugin = tree_state(plugin / "skills")

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except ValueError as exc:
                assert "unclassified hook event" in str(exc)
            else:
                raise AssertionError("unclassified hook was accepted")

        assert_wrapper_trees_unchanged(
            codex, plugin, before_codex, before_plugin
        )


def test_invalid_or_missing_budget_mapping_preserves_both_wrapper_trees() -> None:
    cases = ("missing support", "invalid source")
    for case in cases:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, manifest_path, codex, plugin = write_fixture(root)
            support = budget_support()
            if case == "missing support":
                support.pop("settings")
            else:
                manifest["source_settings"] = {
                    "skill_listing_budget_fraction": 0
                }
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            before_codex = tree_state(codex / "skills")
            before_plugin = tree_state(plugin / "skills")

            with isolated_renderer(root, manifest_path, codex, plugin, support):
                try:
                    render_codex.main()
                except ValueError as exc:
                    assert "skillListingBudgetFraction" in str(exc), case
                else:
                    raise AssertionError(f"{case} budget mapping was accepted")

            assert_wrapper_trees_unchanged(
                codex, plugin, before_codex, before_plugin
            )


def test_second_wrapper_preparation_failure_publishes_neither_copy() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        before_codex = tree_state(codex / "skills")
        before_plugin = tree_state(plugin / "skills")
        calls = 0

        def fail_second_copy(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected plugin wrapper preparation failure")
            return "---\nname: new-skill\ndescription: prepared\n---\n"

        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(
                render_codex,
                "wrapper_skill_text",
                side_effect=fail_second_copy,
                create=True,
            ),
        ):
            try:
                render_codex.main()
            except RuntimeError as exc:
                assert "plugin wrapper preparation" in str(exc)
            else:
                raise AssertionError("injected preparation failure was not raised")

        assert calls == 2
        assert_wrapper_trees_unchanged(
            codex, plugin, before_codex, before_plugin
        )


def test_missing_source_fails_before_wrapper_publication() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        # arftl-allow: suppression-marker (fixture manifest is an untyped dict)
        Path(manifest["skills"][0]["skill_md"]).unlink()  # type: ignore[index]
        before_codex = tree_state(codex / "skills")
        before_plugin = tree_state(plugin / "skills")

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            try:
                render_codex.main()
            except FileNotFoundError:
                # arftl-allow: swallowed-exception (the expected rejection; the else branch fails the test)
                pass
            else:
                raise AssertionError("missing source skill was not rejected")

        assert_wrapper_trees_unchanged(
            codex, plugin, before_codex, before_plugin
        )


def test_replacing_a_generated_file_keeps_no_copy_beside_it() -> None:
    # The mirror keeps no backup of content it replaces: the production
    # publish prunes such copies anyway, so a direct run must not make them.
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "AGENTS.md"
        target.write_text("edited by hand\n", encoding="utf-8")
        common.write_file(target, "rendered\n")
        assert sorted(path.name for path in Path(td).iterdir()) == ["AGENTS.md"]
        assert target.read_text(encoding="utf-8") == "rendered\n"


def test_generated_files_replace_existing_content_atomically() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "hooks.json"
        target.write_text("old generation\n", encoding="utf-8")
        observed: list[tuple[str, str, Path]] = []
        replace = os.replace

        def observe_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            observed.append(
                (
                    destination_path.read_text(encoding="utf-8"),
                    source_path.read_text(encoding="utf-8"),
                    source_path.parent,
                )
            )
            replace(source, destination)

        with patch.object(common.os, "replace", side_effect=observe_replace):
            common.write_file(target, "new generation\n")

        assert observed == [("old generation\n", "new generation\n", target.parent)]
        assert target.read_text(encoding="utf-8") == "new generation\n"


def test_generated_file_mode_is_durable_before_replace() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "dispatcher.sh"
        observed_modes: list[int] = []
        fsync = os.fsync

        def observe_fsync(fd: int) -> None:
            observed_modes.append(os.fstat(fd).st_mode & 0o777)
            fsync(fd)

        with patch.object(common.os, "fsync", side_effect=observe_fsync):
            common.write_file(target, "#!/bin/sh\n", mode=0o755)

        assert observed_modes[0] == 0o755


def test_new_generated_files_honor_restrictive_umask() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "private.json"
        previous_umask = os.umask(0o077)
        try:
            common.write_file(target, "private\n")
        finally:
            os.umask(previous_umask)

        assert target.stat().st_mode & 0o777 == 0o600


def test_hook_publication_retains_previous_generation_for_live_sessions() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        manifest["hooks"] = {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "printf old-generation"}]}
            ]
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
            first = render_codex.prepare_render_plan(manifest, budget_support())
            render_codex.publish_render_plan(first)
            first_hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            first_command = first_hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0][
                "command"
            ]
            first_dispatcher = Path(shlex.split(first_command)[-1])
            first_generation = set(
                (codex / "hooks" / "claude-mirror").iterdir()
            )

            manifest["hooks"] = {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {"type": "command", "command": "printf new-generation"}
                        ]
                    }
                ]
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            second = render_codex.prepare_render_plan(manifest, budget_support())
            render_codex.publish_render_plan(second)

        assert first_generation
        assert all(path.exists() for path in first_generation)
        result = subprocess.run(
            [first_dispatcher],
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "probe",
                    "source": "user",
                }
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["additionalContext"] == "old-generation"


def test_old_hook_generation_pins_its_runtime() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        manifest["hooks"] = {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "printf pinned-runtime"}]}
            ]
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
            plan = render_codex.prepare_render_plan(manifest, budget_support())
            render_codex.publish_render_plan(plan)
            hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            registered_command = shlex.split(
                hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            )
            common.write_file(
                codex / "scripts" / "codex_hook_fanout.py",
                "#!/usr/bin/env python3\nraise SystemExit(97)\n",
                mode=0o755,
            )

        result = subprocess.run(
            registered_command,
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "probe",
                    "source": "user",
                }
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["additionalContext"] == "pinned-runtime"


def test_hook_registry_switch_is_publication_commit_point() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        manifest["hooks"] = {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "printf generation"}]}
            ]
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        writes: list[Path] = []

        with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
            plan = render_codex.prepare_render_plan(manifest, budget_support())
            write_file = render_codex.write_file

            def observe_write(path: Path, text: str, mode: int | None = None) -> bool:
                writes.append(path)
                return write_file(path, text, mode)

            with patch.object(render_codex, "write_file", side_effect=observe_write):
                render_codex.publish_render_plan(plan)

        assert writes[-1] == codex / "hooks.json"


if __name__ == "__main__":
    test_invalid_live_config_preserves_both_wrapper_trees()
    test_runtime_state_written_after_prepare_survives()
    test_value_identical_reformat_is_not_rewritten()
    test_sync_names_replaced_values_once()
    test_enabled_plugin_missing_id_fails_before_any_live_mutation()
    test_file_at_desired_skill_path_is_replaced_by_the_wrapper()
    test_malformed_config_inputs_fail_before_any_live_mutation()
    test_invalid_marketplace_json_fails_before_any_live_mutation()
    test_invalid_reference_source_fails_before_any_live_mutation()
    test_invalid_mcp_asset_config_fails_before_any_live_mutation()
    test_missing_mcp_asset_fails_before_any_live_mutation()
    test_serialization_failure_occurs_before_any_live_mutation()
    test_publication_never_serializes_so_it_cannot_fail_on_it()
    test_a_no_op_publish_leaves_mcp_assets_in_place()
    test_a_changed_mcp_asset_replaces_the_old_one_whole_never_missing()
    test_a_retargeted_reference_link_is_never_missing()
    test_an_interrupt_between_the_two_renames_leaves_what_stood_at_dst()
    test_unclassified_hook_preserves_both_wrapper_trees()
    test_invalid_or_missing_budget_mapping_preserves_both_wrapper_trees()
    test_second_wrapper_preparation_failure_publishes_neither_copy()
    test_missing_source_fails_before_wrapper_publication()
    test_replacing_a_generated_file_keeps_no_copy_beside_it()
    test_generated_files_replace_existing_content_atomically()
    test_generated_file_mode_is_durable_before_replace()
    test_new_generated_files_honor_restrictive_umask()
    test_hook_publication_retains_previous_generation_for_live_sessions()
    test_old_hook_generation_pins_its_runtime()
    test_hook_registry_switch_is_publication_commit_point()
    print("Codex render atomicity tests OK")
