#!/usr/bin/env python3
from __future__ import annotations

from contextlib import ExitStack, contextmanager, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
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


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def tree_state(root: Path) -> dict[str, tuple[str, bytes | str, int]]:
    """Every entry under root: a link by target, a file by bytes and mode,
    a directory by mode, so a rollback that restores another mode differs."""
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
    ):
        yield


def assert_wrapper_trees_unchanged(
    codex: Path,
    plugin: Path,
    before_codex: dict[str, bytes],
    before_plugin: dict[str, bytes],
) -> None:
    assert tree_bytes(codex / "skills") == before_codex
    assert tree_bytes(plugin / "skills") == before_plugin
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
        before_codex = tree_bytes(codex / "skills")
        before_plugin = tree_bytes(plugin / "skills")

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


def test_runtime_state_written_during_failed_publish_survives_rollback() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        config = codex / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('model = "m"\n', encoding="utf-8")

        def codex_writes_then_step_fails(*args: object, **kwargs: object) -> None:
            config.write_text(
                'model = "m"\n[projects."/new"]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            raise OSError("late step failed")

        with isolated_renderer(
            root, manifest_path, codex, plugin, budget_support()
        ):
            plan = render_codex.prepare_render_plan(
                manifest, render_codex.load_support()
            )
            with patch.object(render_codex, "render_plugin", codex_writes_then_step_fails):
                try:
                    render_codex.publish_render_plan(plan)
                except OSError as exc:
                    assert str(exc) == "late step failed"
                else:
                    raise AssertionError("failing publish step was swallowed")

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

        def publish(plan: render_codex.RenderPlan) -> list[tuple[Path, Path]]:
            nonlocal publishing
            publishing = True
            return real_publish(plan)

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


def test_late_publication_failure_rolls_back_all_live_paths() -> None:
    # With real hooks, so the failure lands after hook generation files, a
    # runtime directory among them, have been written.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        manifest["hooks"] = STOP_HOOKS
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        # A generation an older session may still run must survive rollback.
        prior = codex / "hooks" / "claude-mirror" / "stop-000000000000000000000000.sh"
        prior.parent.mkdir(parents=True)
        prior.write_text("#!/usr/bin/env bash\necho prior\n", encoding="utf-8")
        agents = root / ".agents"
        before = live_state(codex, plugin, agents)
        written: list[Path] = []

        def fail_after_hooks(*args: object, **kwargs: object) -> None:
            written.extend(
                path for path in (codex / "hooks" / "claude-mirror").rglob("*")
                if path.is_file() and path != prior
            )
            raise OSError("injected late publication failure")

        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(render_codex, "render_plugin", side_effect=fail_after_hooks),
        ):
            try:
                render_codex.main()
            except OSError as exc:
                assert "late publication" in str(exc)
            else:
                raise AssertionError("late publication failure was not raised")

        assert any(path.parent.name.startswith("runtime-") for path in written), written
        assert any(path.name.startswith("stop-fanout-") for path in written), written
        assert live_state(codex, plugin, agents) == before


def test_interrupted_publication_rolls_back_like_a_failed_one() -> None:
    # Ctrl-C (or SystemExit) is not an Exception. It skipped the rollback and
    # dropped the snapshot: interrupted in the MCP copy, ~/.codex/mcps was left
    # without the required git broker; later, with a half-published mirror.
    for target in ("copy_tree_clean", "render_plugin"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, manifest_path, codex, plugin = write_fixture(root)
            manifest["hooks"] = STOP_HOOKS
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            agents = root / ".agents"
            with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
                render_codex.main()
                # Something the next publish changes before either target runs.
                server = codex / "mcps" / "git-broker" / "server.py"
                server.write_text("# from an earlier sync\n", encoding="utf-8")
                before = live_state(codex, plugin, agents)
                with patch.object(
                    render_codex, target, side_effect=KeyboardInterrupt("injected")
                ):
                    try:
                        render_codex.main()
                    except KeyboardInterrupt as exc:
                        assert str(exc) == "injected", exc
                    else:
                        raise AssertionError(f"{target}: interrupt was not raised")
            assert live_state(codex, plugin, agents) == before, target


def test_rollback_that_cannot_restore_keeps_its_snapshot_and_names_it() -> None:
    # A rollback that fails part way must not lose what it restores from:
    # the snapshot stays, with a map of what each entry was, and the error
    # names it, chained to the publish failure that started the rollback.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        instructions = codex / "AGENTS.md"
        instructions.write_text("instructions from an earlier sync\n", encoding="utf-8")
        rolling_back = False
        real_copy2 = shutil.copy2

        def fail_late(*args: object, **kwargs: object) -> None:
            nonlocal rolling_back
            rolling_back = True
            raise OSError("injected late publication failure")

        def copy2(source: object, destination: object, *args: object, **kwargs: object) -> object:
            if rolling_back:
                raise OSError("injected restore failure")
            return real_copy2(source, destination, *args, **kwargs)

        failure: BaseException | None = None
        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(render_codex, "render_plugin", side_effect=fail_late),
            patch.object(render_codex.shutil, "copy2", side_effect=copy2),
        ):
            try:
                render_codex.main()
            except Exception as exc:
                failure = exc

        assert isinstance(failure, RuntimeError), failure
        message = str(failure)
        assert "injected restore failure" in message, message
        assert isinstance(failure.__cause__, OSError), failure.__cause__
        assert str(failure.__cause__) == "injected late publication failure"
        kept = Path(message.split("snapshot is kept at ", 1)[1].split()[0].rstrip(";,."))
        try:
            assert kept.is_dir(), kept
            entries = json.loads((kept / "snapshot.json").read_text(encoding="utf-8"))
            by_path = {entry["path"]: entry for entry in entries}
            saved = by_path[str(instructions)]
            assert saved["kind"] == "file"
            assert (kept / saved["snapshot"]).read_text(encoding="utf-8") == (
                "instructions from an earlier sync\n"
            )
        finally:
            shutil.rmtree(kept, ignore_errors=True)


def test_a_stop_signal_cannot_hide_a_failed_rollbacks_snapshot() -> None:
    # A stop signal held off during a rollback must not then replace the
    # error that names the kept snapshot: the run ends either way, and only
    # that error says where the pre-publish state is.
    def stop(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "AGENTS.md"
        target.write_text("before\n", encoding="utf-8")
        rolling_back = False
        real_copy2 = shutil.copy2

        def copy2(source: object, destination: object, *args: object, **kwargs: object) -> object:
            if rolling_back:
                os.kill(os.getpid(), signal.SIGTERM)
                raise OSError("injected restore failure")
            return real_copy2(source, destination, *args, **kwargs)

        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        previous = signal.signal(signal.SIGTERM, stop)
        failure: BaseException | None = None
        try:
            with patch.object(render_codex.shutil, "copy2", side_effect=copy2):
                try:
                    with render_codex.rollback_paths_on_error([target]):
                        target.write_text("after\n", encoding="utf-8")
                        rolling_back = True
                        raise OSError("injected publication failure")
                except BaseException as exc:
                    failure = exc
            # Nor may it leave the stop signals held: the mask is process
            # state every later child inherits.
            left_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
            left_pending = signal.sigpending()
        finally:
            # Ignoring a signal drops it if still pending; then put back both.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            signal.signal(signal.SIGTERM, previous)

        assert isinstance(failure, RuntimeError), repr(failure)
        assert left_mask == mask, left_mask
        assert signal.SIGTERM not in left_pending, left_pending
        message = str(failure)
        kept = Path(message.split("snapshot is kept at ", 1)[1].split()[0].rstrip(";,."))
        try:
            entries = json.loads((kept / "snapshot.json").read_text(encoding="utf-8"))
            assert [entry["path"] for entry in entries] == [str(target)], entries
        finally:
            shutil.rmtree(kept, ignore_errors=True)


def rollback_with_a_refilled_parent(
    signum: signal.Signals | None,
) -> tuple[BaseException | None, BaseException]:
    """Roll back a publish into a parent it created, which something refills
    between the rollback's empty check and its rmdir; signum, if any, arrives
    meanwhile. Asserts the signal mask is back, the output gone and the
    parent named on stderr; returns what the rollback raised and the publish
    failure."""

    def stop(number: int, frame: object) -> None:
        raise SystemExit(128 + number)

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "new" / "deeper" / "AGENTS.md"
        real_rmdir = Path.rmdir

        def refilled(self: Path) -> None:
            if signum is not None:
                os.kill(os.getpid(), signum)
            (self / "refill").write_text("written meanwhile\n", encoding="utf-8")
            real_rmdir(self)

        failure = OSError("injected publication failure")
        stderr = StringIO()
        caught: BaseException | None = None
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        previous = {number: signal.signal(number, stop) for number in render_codex.STOP_SIGNALS}
        try:
            with patch.object(Path, "rmdir", refilled), redirect_stderr(stderr):
                try:
                    with render_codex.rollback_paths_on_error([target]):
                        target.parent.mkdir(parents=True)
                        target.write_text("published\n", encoding="utf-8")
                        raise failure
                except BaseException as exc:
                    caught = exc
            left_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        finally:
            # Ignoring a signal drops it if still pending; then put back both.
            for number in render_codex.STOP_SIGNALS:
                signal.signal(number, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            for number, handler in previous.items():
                signal.signal(number, handler)
        assert left_mask == mask, left_mask
        assert not target.exists(), target
        assert str(target.parent) in stderr.getvalue(), stderr.getvalue()
        return caught, failure


def test_a_parent_the_rollback_cannot_remove_never_replaces_the_failure() -> None:
    # The empty-parent cleanup ran unguarded after the restore: a parent
    # refilled before its rmdir raised OSError over the publish failure
    # (sync_codex's CheckFailed escaped as a traceback) and left the stop
    # signals blocked for good. It is reported; the failure still ends the run.
    caught, failure = rollback_with_a_refilled_parent(None)
    assert caught is failure, repr(caught)


def test_a_parent_the_rollback_cannot_remove_still_delivers_a_held_stop() -> None:
    # A stop held off during the rollback is delivered once the restore has
    # finished, a cleanup that cannot finish notwithstanding.
    caught, _failure = rollback_with_a_refilled_parent(signal.SIGTERM)
    assert isinstance(caught, SystemExit), repr(caught)
    assert caught.code == 128 + signal.SIGTERM, caught.code


def test_rollback_never_leaves_a_restored_path_missing() -> None:
    # A rollback removed every target before restoring any: a Codex session
    # starting meanwhile found no config.toml or hooks.json. Each one is
    # replaced in place instead. Checked at every copy, replace and removal
    # the restore makes; a directory swaps in by two renames, which these do
    # not observe, so it is missing at most between them.
    with tempfile.TemporaryDirectory() as td:
        codex = Path(td) / ".codex"
        (codex / "skills" / "a").mkdir(parents=True)
        (codex / "skills" / "a" / "SKILL.md").write_text("old skill\n", encoding="utf-8")
        (codex / "config.toml").write_text("old config\n", encoding="utf-8")
        (codex / "hooks.json").write_text("{}\n", encoding="utf-8")
        (codex / "AGENTS.md").write_text("old instructions\n", encoding="utf-8")
        (codex / "link").symlink_to(codex / "skills")
        existing = ["config.toml", "hooks.json", "AGENTS.md", "link", "skills"]
        targets = [codex / name for name in [*existing, "new"]]
        before = tree_state(codex)
        rolling_back = False
        observed: list[str] = []
        missing: list[str] = []

        def observing(name: str, real):
            def call(*args: object, **kwargs: object) -> object:
                if rolling_back:
                    observed.append(name)
                    missing.extend(
                        f"{path} before {name}"
                        for path in existing
                        if not (codex / path).exists() and not (codex / path).is_symlink()
                    )
                return real(*args, **kwargs)

            return call

        failure = OSError("injected publication failure")
        with ExitStack() as observers:
            for owner, name in (
                (render_codex.shutil, "copy2"),
                (render_codex.shutil, "copytree"),
                (render_codex.os, "replace"),
                (render_codex, "remove_path"),
            ):
                observers.enter_context(
                    patch.object(owner, name, observing(name, getattr(owner, name)))
                )
            try:
                with render_codex.rollback_paths_on_error(targets):
                    (codex / "config.toml").write_text("new config\n", encoding="utf-8")
                    (codex / "AGENTS.md").unlink()
                    (codex / "AGENTS.md").mkdir()
                    (codex / "link").unlink()
                    (codex / "link").write_text("not a link\n", encoding="utf-8")
                    (codex / "skills" / "a" / "SKILL.md").write_text("new\n", encoding="utf-8")
                    (codex / "new").write_text("published\n", encoding="utf-8")
                    (codex / "hooks.json").write_text('{"new": true}\n', encoding="utf-8")
                    rolling_back = True
                    raise failure
            except OSError as exc:
                assert exc is failure, exc
        assert observed, "the restore made no observed call"
        assert missing == [], missing
        assert tree_state(codex) == before


def test_rollback_leaves_no_snapshot_behind() -> None:
    # The snapshot is the rollback's own scratch: removed after a publish that
    # stands and after a rollback that restored everything.
    def snapshots() -> set[Path]:
        return set(Path(tempfile.gettempdir()).glob("codex-render-rollback-*"))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        before = snapshots()
        with isolated_renderer(root, manifest_path, codex, plugin, budget_support()):
            render_codex.main()
            with patch.object(render_codex, "render_plugin", side_effect=OSError("late")):
                try:
                    render_codex.main()
                except OSError as exc:
                    assert str(exc) == "late", exc
                else:
                    raise AssertionError("injected failure was not raised")
        assert snapshots() == before


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


def test_unclassified_hook_preserves_both_wrapper_trees() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest, manifest_path, codex, plugin = write_fixture(root)
        manifest["hooks"] = {"FutureHookEvent": []}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before_codex = tree_bytes(codex / "skills")
        before_plugin = tree_bytes(plugin / "skills")

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
            before_codex = tree_bytes(codex / "skills")
            before_plugin = tree_bytes(plugin / "skills")

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
        before_codex = tree_bytes(codex / "skills")
        before_plugin = tree_bytes(plugin / "skills")
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
        before_codex = tree_bytes(codex / "skills")
        before_plugin = tree_bytes(plugin / "skills")

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


def test_second_wrapper_publication_failure_rolls_back_both_trees() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        before_codex = tree_bytes(codex / "skills")
        before_plugin = tree_bytes(plugin / "skills")
        before_codex_structure = tree_state(codex / "skills")
        before_plugin_structure = tree_state(plugin / "skills")
        publish_wrapper_skill = render_codex.publish_wrapper_skill
        calls = 0

        def fail_second_publish(dst: Path, text: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected plugin wrapper publication failure")
            publish_wrapper_skill(dst, text)

        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(
                render_codex,
                "publish_wrapper_skill",
                side_effect=fail_second_publish,
            ),
        ):
            try:
                render_codex.main()
            except OSError as exc:
                assert "plugin wrapper publication" in str(exc)
            else:
                raise AssertionError("injected publication failure was not raised")

        assert calls == 2
        assert_wrapper_trees_unchanged(
            codex, plugin, before_codex, before_plugin
        )
        assert tree_state(codex / "skills") == before_codex_structure
        assert tree_state(plugin / "skills") == before_plugin_structure


def test_failed_wrapper_publication_removes_originally_absent_skill_roots() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        shutil.rmtree(codex / "skills")
        shutil.rmtree(plugin / "skills")
        publish_wrapper_skill = render_codex.publish_wrapper_skill
        calls = 0

        def fail_after_second_publish(dst: Path, text: str) -> None:
            nonlocal calls
            calls += 1
            publish_wrapper_skill(dst, text)
            if calls == 2:
                raise OSError("injected failure after both wrapper roots were created")

        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(
                render_codex,
                "publish_wrapper_skill",
                side_effect=fail_after_second_publish,
            ),
        ):
            try:
                render_codex.main()
            except OSError as exc:
                assert "both wrapper roots" in str(exc)
            else:
                raise AssertionError("injected publication failure was not raised")

        assert calls == 2
        assert not (codex / "skills").exists()
        assert not (plugin / "skills").exists()


def test_failure_after_config_write_rolls_back_all_but_config() -> None:
    # The last publish step fails, after publish_config wrote config.toml.
    # publish_config documents config.toml as outside the rollback set, since
    # Codex writes runtime state into it at any time: it keeps the new render.
    # Every rollback path is restored, and the publish leaves no copy of what
    # it replaced: a *.bak-* from an earlier renderer stays as it was.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _, manifest_path, codex, plugin = write_fixture(root)
        agents = root / ".agents"
        config = codex / "config.toml"
        instructions = codex / "AGENTS.md"
        instructions.write_text("instructions from an earlier sync\n", encoding="utf-8")
        original_config = config.read_bytes()
        existing = {
            path: path.with_name(f"{path.name}.bak-20260908-154512")
            for path in (config, instructions)
        }
        for backup in existing.values():
            backup.write_bytes(b"pre-existing backup\n")
        before = live_state(codex, plugin, agents)

        written: dict[Path, str] = {}
        write_file = render_codex.write_file

        def fail_at_hook_registry(path: Path, text: str, mode: int | None = None) -> bool:
            if path == codex / "hooks.json":
                raise OSError("injected failure after config write")
            written[path] = text
            return write_file(path, text, mode)

        with (
            isolated_renderer(root, manifest_path, codex, plugin, budget_support()),
            patch.object(render_codex, "write_file", side_effect=fail_at_hook_registry),
        ):
            try:
                render_codex.main()
            except OSError as exc:
                assert str(exc) == "injected failure after config write"
            else:
                raise AssertionError("injected publication failure was not raised")

        assert config in written and instructions in written
        assert config.read_text(encoding="utf-8") == written[config]
        assert config.read_bytes() != original_config
        assert sorted(codex.glob("config.toml.bak-*")) == [existing[config]]
        assert sorted(codex.glob("AGENTS.md.bak-*")) == [existing[instructions]]
        assert instructions.read_text(encoding="utf-8") == "instructions from an earlier sync\n"

        after = live_state(codex, plugin, agents)
        for state in (before, after):
            state["codex"] = {
                key: value
                for key, value in state["codex"].items()
                if key != "config.toml"
            }
        changed = {
            tree: sorted(
                key
                for key in before[tree].keys() | after[tree].keys()
                if before[tree].get(key) != after[tree].get(key)
            )
            for tree in before
        }
        assert after == before, changed


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
    test_runtime_state_written_during_failed_publish_survives_rollback()
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
    test_late_publication_failure_rolls_back_all_live_paths()
    test_interrupted_publication_rolls_back_like_a_failed_one()
    test_rollback_that_cannot_restore_keeps_its_snapshot_and_names_it()
    test_a_stop_signal_cannot_hide_a_failed_rollbacks_snapshot()
    test_a_parent_the_rollback_cannot_remove_never_replaces_the_failure()
    test_a_parent_the_rollback_cannot_remove_still_delivers_a_held_stop()
    test_rollback_never_leaves_a_restored_path_missing()
    test_rollback_leaves_no_snapshot_behind()
    test_a_no_op_publish_leaves_mcp_assets_in_place()
    test_a_changed_mcp_asset_replaces_the_old_one_whole_never_missing()
    test_a_retargeted_reference_link_is_never_missing()
    test_unclassified_hook_preserves_both_wrapper_trees()
    test_invalid_or_missing_budget_mapping_preserves_both_wrapper_trees()
    test_second_wrapper_preparation_failure_publishes_neither_copy()
    test_missing_source_fails_before_wrapper_publication()
    test_second_wrapper_publication_failure_rolls_back_both_trees()
    test_failed_wrapper_publication_removes_originally_absent_skill_roots()
    test_failure_after_config_write_rolls_back_all_but_config()
    test_replacing_a_generated_file_keeps_no_copy_beside_it()
    test_generated_files_replace_existing_content_atomically()
    test_generated_file_mode_is_durable_before_replace()
    test_new_generated_files_honor_restrictive_umask()
    test_hook_publication_retains_previous_generation_for_live_sessions()
    test_old_hook_generation_pins_its_runtime()
    test_hook_registry_switch_is_publication_commit_point()
    print("Codex render atomicity tests OK")
