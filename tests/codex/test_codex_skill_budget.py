#!/usr/bin/env python3
from __future__ import annotations

import copy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import sys
import tempfile
import tomllib
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2] / "codex"
sys.path.insert(0, str(ROOT / "scripts"))

import import_claude  # noqa: E402
import probe_codex_skills  # noqa: E402
import render_codex  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from roundtrip_support import Inputs, assert_check_fails, describe, round_trip  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_skill(path: Path, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {path.parent.name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


def claude_settings() -> dict[str, object]:
    return {
        "skillListingBudgetFraction": 0.03,
        "enabledPlugins": {
            "enabled-kit@example": True,
            "disabled-kit@example": False,
        },
    }


def budget_support() -> dict[str, object]:
    return {
        "settings": {
            "skillListingBudgetFraction": {
                "status": "degraded",
                "source_value": 0.03,
                "codex_native_fraction": 0.02,
                "codex_version_verified": "0.146.0",
                "strategy": "compact-trigger-descriptions",
                "notes": (
                    "Codex 0.146.0 rejects budget overrides; preserve all enabled "
                    "skills and compact only generated Codex wrapper descriptions."
                ),
            }
        }
    }


def expected_budget_mapping(source_value: float = 0.03) -> dict[str, object]:
    return {
        "source_setting": "skillListingBudgetFraction",
        "source_value": source_value,
        "status": "degraded",
        "codex_native_fraction": 0.02,
        "codex_version_verified": "0.146.0",
        "strategy": "compact-trigger-descriptions",
    }


def test_import_uses_claude_default_when_budget_is_unset() -> None:
    assert import_claude.source_settings({}) == {
        "skill_listing_budget_fraction": 0.01
    }


def test_import_retains_budget_and_complete_plugin_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        installed = {
            "plugins": {
                "enabled-kit@example": [
                    {
                        "installPath": str(root / "enabled-kit"),
                        "version": "1.2.3",
                    }
                ],
                "disabled-kit@example": [
                    {
                        "installPath": str(root / "disabled-kit"),
                        "version": "4.5.6",
                    }
                ],
            }
        }
        plugins, plugin_skills = import_claude.plugin_inventory(
            claude_settings(), installed
        )

        assert plugins == [
            {
                "id": "disabled-kit@example",
                "enabled": False,
                "path": str(root / "disabled-kit"),
                "version": "4.5.6",
                "exists": False,
            },
            {
                "id": "enabled-kit@example",
                "enabled": True,
                "path": str(root / "enabled-kit"),
                "version": "1.2.3",
                "exists": False,
            },
        ]
        assert plugin_skills == []
        assert import_claude.source_settings(claude_settings()) == {
            "skill_listing_budget_fraction": 0.03,
        }


def test_plugin_inventory_only_collects_enabled_existing_skills() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        enabled_root = root / "enabled-kit"
        disabled_root = root / "disabled-kit"
        truthy_root = root / "truthy-kit"
        write_skill(
            enabled_root / "skills" / "enabled-skill" / "SKILL.md",
            "Enabled fixture skill.",
        )
        write_skill(
            disabled_root / "skills" / "disabled-skill" / "SKILL.md",
            "Disabled fixture skill.",
        )
        write_skill(
            truthy_root / "skills" / "truthy-skill" / "SKILL.md",
            "Non-boolean fixture skill.",
        )
        settings = claude_settings()
        settings["enabledPlugins"] = {
            **settings["enabledPlugins"],
            "truthy-kit@example": 1,
        }
        installed = {
            "plugins": {
                "enabled-kit@example": [
                    {"installPath": str(root / "old-enabled-kit"), "version": "1.0.0"},
                    {"installPath": str(enabled_root), "version": "1.2.3"},
                ],
                "disabled-kit@example": [
                    {"installPath": str(disabled_root), "version": "4.5.6"}
                ],
                "truthy-kit@example": [
                    {"installPath": str(truthy_root), "version": "7.8.9"}
                ],
            }
        }

        plugins, plugin_skills = import_claude.plugin_inventory(settings, installed)

        assert [plugin["id"] for plugin in plugins] == [
            "disabled-kit@example",
            "enabled-kit@example",
            "truthy-kit@example",
        ]
        assert plugins[1] == {
            "id": "enabled-kit@example",
            "enabled": True,
            "path": str(enabled_root),
            "version": "1.2.3",
            "exists": True,
        }
        assert plugins[2]["enabled"] is False
        assert [skill["name"] for skill in plugin_skills] == ["enabled-skill"]


def test_main_writes_stable_plugin_state_and_loads_installed_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        claude = root / ".claude"
        state_path = root / ".claude.json"
        manifest_path = root / "manifest.json"
        installed_path = claude / "plugins" / "installed_plugins.json"
        enabled_root = root / "enabled-kit"
        disabled_root = root / "disabled-kit"

        write_skill(
            enabled_root / "skills" / "enabled-skill" / "SKILL.md",
            "Enabled fixture skill.",
        )
        write_skill(
            disabled_root / "skills" / "disabled-skill" / "SKILL.md",
            "Disabled fixture skill.",
        )
        write_json(claude / "settings.json", claude_settings())
        write_json(state_path, {})
        write_json(
            installed_path,
            {
                "plugins": {
                    "enabled-kit@example": [
                        {"installPath": str(enabled_root), "version": "1.2.3"}
                    ],
                    "disabled-kit@example": [
                        {"installPath": str(disabled_root), "version": "4.5.6"}
                    ],
                }
            },
        )
        write_json(claude / "plugins" / "known_marketplaces.json", {})

        installed_reads: list[Path] = []
        real_load_json = import_claude.load_json

        def tracked_load_json(path: Path, default: object) -> object:
            if path == installed_path:
                installed_reads.append(path)
            return real_load_json(path, default)

        with (
            patch.object(import_claude, "CLAUDE", claude),
            patch.object(import_claude, "CLAUDE_STATE", state_path),
            patch.object(import_claude, "MANIFEST", manifest_path),
            patch.object(import_claude, "load_json", side_effect=tracked_load_json),
        ):
            assert import_claude.main() == 0

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(installed_reads) == 1
        assert manifest["source_settings"] == {
            "skill_listing_budget_fraction": 0.03,
        }
        assert manifest["plugins"] == [
            {
                "id": "disabled-kit@example",
                "enabled": False,
                "path": str(disabled_root),
                "version": "4.5.6",
                "exists": True,
            },
            {
                "id": "enabled-kit@example",
                "enabled": True,
                "path": str(enabled_root),
                "version": "1.2.3",
                "exists": True,
            },
        ]
        assert manifest["enabled_plugins"] == [manifest["plugins"][1]]
        assert manifest["disabled_plugins"] == [manifest["plugins"][0]]
        assert [skill["name"] for skill in manifest["plugin_skills"]] == [
            "enabled-skill"
        ]


def test_codex_plugin_state_block_disables_only_claude_disabled_plugins() -> None:
    plugins = [
        {"id": "z-enabled@example", "enabled": True},
        {"id": "quoted/plugin@example", "enabled": False},
        {"id": "a-disabled@example", "enabled": False},
        {"id": "truthy-disabled@example", "enabled": 1},
    ]

    assert render_codex.codex_plugin_state_block(plugins) == [
        '[plugins."a-disabled@example"]',
        "enabled = false",
        "",
        '[plugins."quoted/plugin@example"]',
        "enabled = false",
        "",
    ]


def render_config(text: str, plugins: list[dict[str, object]]) -> str:
    with patch.object(render_codex, "CODEX", Path("/nonexistent/.codex")):
        return render_codex.render_codex_config_text(
            text, {}, {}, {}, render_codex.render_hooks({}), plugins
        )


def test_render_overwrites_hand_written_plugin_state_and_is_idempotent() -> None:
    plugins = [
        {"id": "z-enabled@example", "enabled": True},
        {"id": "a-disabled@example", "enabled": False},
    ]
    first = render_config(
        '# hand edit\n[plugins."hand-written"]\nenabled = true\n', plugins
    )

    assert render_config(first, plugins) == first
    assert "hand-written" not in first and "# hand edit" not in first
    assert tomllib.loads(first)["plugins"] == {"a-disabled@example": {"enabled": False}}


def test_render_rejects_invalid_live_toml() -> None:
    try:
        render_config('[projects."/repo"]\ntrust_level = \n', [])
    except ValueError as exc:
        assert "invalid config.toml" in str(exc)
    else:
        raise AssertionError("invalid live config was accepted")


def test_every_spelling_of_a_hand_written_plugin_entry_is_replaced() -> None:
    hand_written_forms = {
        "dotted key": 'plugins."disabled@example".enabled = true\n',
        "fully quoted table": '["plugins"."disabled@example"]\nenabled = true\n',
        "whitespace table": '[plugins . "disabled@example"]\nenabled = true\n',
        "inline table": 'plugins = {"disabled@example" = { enabled = true }}\n',
    }

    for name, hand_written in hand_written_forms.items():
        rendered = render_config(
            hand_written, [{"id": "disabled@example", "enabled": False}]
        )
        parsed = tomllib.loads(rendered)
        assert parsed["plugins"] == {"disabled@example": {"enabled": False}}, name


def test_write_reports_records_structured_degraded_budget_mapping() -> None:
    manifest = {
        "source_settings": {"skill_listing_budget_fraction": 0.03},
    }

    with tempfile.TemporaryDirectory() as td:
        report_dir = Path(td) / "report"
        with patch.object(render_codex, "REPORT_DIR", report_dir):
            render_codex.write_reports(
                manifest,
                [],
                [],
                [],
                [],
                {},
                budget_support(),
            )
        report = json.loads(
            (report_dir / "report.json").read_text(encoding="utf-8")
        )

    assert report["mappings"]["skill_listing_budget_fraction"] == (
        expected_budget_mapping()
    )


def test_skill_budget_mapping_rejects_missing_support_and_invalid_source() -> None:
    valid_manifest = {
        "source_settings": {"skill_listing_budget_fraction": 0.03},
    }
    invalid_values = [None, True, 0, -0.01, "0.03"]

    try:
        render_codex.skill_budget_mapping(valid_manifest, {})
    except ValueError as exc:
        assert "skillListingBudgetFraction" in str(exc)
    else:
        raise AssertionError("missing support entry was accepted")

    for value in invalid_values:
        manifest = {
            "source_settings": {"skill_listing_budget_fraction": value},
        }
        try:
            render_codex.skill_budget_mapping(manifest, budget_support())
        except ValueError as exc:
            assert "positive number" in str(exc), value
        else:
            raise AssertionError(f"invalid source value was accepted: {value!r}")


def report_path(rt) -> Path:
    return rt.codex / "claude-setup-mirror" / "report.json"


def edit_report_mappings(rt, edit) -> None:
    rt.edit_json(report_path(rt), lambda report: edit(report["mappings"]))


def test_check_rejects_missing_or_stale_budget_mapping() -> None:
    def stale_source(mappings: dict[str, object]) -> None:
        mappings["skill_listing_budget_fraction"]["source_value"] = 0.02

    def wrong_status(mappings: dict[str, object]) -> None:
        mappings["skill_listing_budget_fraction"]["status"] = "shared"

    cases = [
        ("missing", lambda mappings: mappings.pop("skill_listing_budget_fraction")),
        ("stale source", stale_source),
        ("wrong status", wrong_status),
    ]
    for name, edit in cases:
        with round_trip() as rt:
            edit_report_mappings(rt, edit)
            assert_check_fails(
                rt, f"{report_path(rt)}: differs from the render plan", label=name
            )


def test_check_rejects_manifest_without_source_budget_setting() -> None:
    with round_trip() as rt:
        rt.edit_json(rt.manifest, lambda manifest: manifest.pop("source_settings"))
        assert_check_fails(rt, "positive number")


def test_check_rejects_report_claiming_max_context_tokens_is_active() -> None:
    with round_trip() as rt:
        edit_report_mappings(
            rt,
            lambda mappings: mappings.update(
                {"skills.max_context_tokens": {"status": "active"}}
            ),
        )
        assert_check_fails(
            rt, "report must not claim skills.max_context_tokens is active"
        )


def test_check_rejects_unsupported_budget_keys_in_generated_config() -> None:
    # (spelling, prepend before every table or append as a table, key)
    cases = [
        ("Claude key", "skillListingBudgetFraction = 0.03\n", True, "skillListingBudgetFraction"),
        ("canonical dotted key", "skills.max_context_tokens = 5440\n", True, "skills.max_context_tokens"),
        ("underscore key", "skills_context_budget_fraction = 0.03\n", True, "skills_context_budget_fraction"),
        ("skills table", "[skills]\nmax_context_tokens = 5440\n", False, "skills.max_context_tokens"),
        ("spaced dotted key", "skills . max_context_tokens = 5440\n", True, "skills.max_context_tokens"),
    ]
    for name, text, prepend, key in cases:
        with round_trip() as rt:
            current = rt.config.read_text(encoding="utf-8")
            rt.config.write_text(
                text + current if prepend else current + "\n" + text, encoding="utf-8"
            )
            assert_check_fails(
                rt, f"unsupported budget setting {key} must not be generated", label=name
            )


def test_check_accepts_unsupported_budget_key_names_in_comments() -> None:
    comments = (
        "# skillListingBudgetFraction is intentionally not generated\n"
        "# skills.max_context_tokens is rejected by Codex 0.146.0\n"
        "# skills_context_budget_fraction is also unsupported\n"
    )
    with round_trip() as rt:
        rt.config.write_text(
            comments + rt.config.read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = rt.check()

    assert result.returncode == 0, describe(result)


DISABLED_PLUGIN = "disabled-kit@example"
ENABLED_PLUGIN = "enabled-kit@example"


def set_plugin_state(plugin_id: str, value: object):
    def edit(config: dict[str, object]) -> None:
        config.setdefault("plugins", {})[plugin_id] = value

    return edit


def test_check_accepts_exact_generated_state_and_rejects_hand_written_plugin_tables() -> None:
    with round_trip(Inputs(plugins=True)) as rt:
        assert rt.load_config()["plugins"] == {DISABLED_PLUGIN: {"enabled": False}}
        result = rt.check()
        assert result.returncode == 0, describe(result)

        rt.edit_config(set_plugin_state("hand-written", {"enabled": True}))
        assert_check_fails(rt, "plugins.hand-written")


def test_check_requires_empty_state_when_manifest_omits_plugins() -> None:
    with round_trip(Inputs(plugins=False)) as rt:
        assert "plugins" not in rt.load_config()
        rt.edit_config(set_plugin_state("stale@example", {"enabled": False}))
        assert_check_fails(rt, '"stale@example"')


def test_check_rejects_all_malformed_generated_plugin_states() -> None:
    def drop_disabled(config: dict[str, object]) -> None:
        del config["plugins"][DISABLED_PLUGIN]

    def duplicate(rt) -> None:
        with rt.config.open("a", encoding="utf-8") as stream:
            stream.write(f'\n[plugins."{DISABLED_PLUGIN}"]\nenabled = false\n')

    cases = [
        ("missing", lambda rt: rt.edit_config(drop_disabled), DISABLED_PLUGIN),
        (
            "stale",
            lambda rt: rt.edit_config(set_plugin_state("stale@example", {"enabled": False})),
            "stale@example",
        ),
        ("duplicate", duplicate, "invalid TOML"),
        (
            "wrong state",
            lambda rt: rt.edit_config(set_plugin_state(DISABLED_PLUGIN, {"enabled": True})),
            DISABLED_PLUGIN,
        ),
        (
            "int not bool",
            lambda rt: rt.edit_config(set_plugin_state(DISABLED_PLUGIN, {"enabled": 0})),
            DISABLED_PLUGIN,
        ),
        (
            "source enabled",
            lambda rt: rt.edit_config(set_plugin_state(ENABLED_PLUGIN, {"enabled": False})),
            ENABLED_PLUGIN,
        ),
    ]
    for name, mutate, expected in cases:
        with round_trip(Inputs(plugins=True)) as rt:
            mutate(rt)
            assert_check_fails(rt, expected, label=name)


def test_check_rejects_non_table_plugins_value() -> None:
    with round_trip() as rt:
        rt.edit_config(lambda config: config.update(plugins=0))
        assert_check_fails(rt, "differs from the render plan at plugins")


def test_check_rejects_plugin_skill_from_non_enabled_plugin() -> None:
    leaked = {
        "plugin": DISABLED_PLUGIN,
        "name": "leaked-skill",
        "path": "/plugin/skills/leaked-skill",
        "skill_md": "/plugin/skills/leaked-skill/SKILL.md",
    }
    with round_trip(Inputs(plugins=True)) as rt:
        rt.edit_json(
            rt.manifest,
            lambda manifest: manifest["plugin_skills"].append(leaked),
        )
        assert_check_fails(
            rt,
            f"{DISABLED_PLUGIN}: plugin skill leaked-skill requires manifest enabled = true",
        )


def frontmatter_description(text: str) -> str:
    line = next(
        item for item in text.splitlines() if item.startswith("description:")
    )
    return json.loads(line.split(":", 1)[1].strip())


def test_compact_description_preserves_trigger_language() -> None:
    source = (
        "Use when a permission prompt fires that a rule should have covered, "
        "when writing or auditing permissions.allow/ask/deny in settings.json, "
        "or when a rule does nothing. Also covers protected paths."
    )
    result = render_codex.compact_skill_description(source, max_chars=96)

    assert len(result) <= 96
    assert result.startswith("Use when")
    assert "permission prompt" in result
    assert result == render_codex.compact_skill_description(
        source, max_chars=96
    )
    assert not result.endswith((" ", ",", ";", ":"))


def test_compact_description_normalizes_boilerplate_and_keeps_quotes() -> None:
    source = (
        'This skill should be used when users say "review this PR", '
        '"code review", or ask for a security-focused differential review. '
        "It contains extensive workflow details."
    )
    result = render_codex.compact_skill_description(source, max_chars=96)

    assert result.startswith("Use when")
    assert '"review this PR"' in result
    assert '"code review"' in result
    assert "differential review" in result
    assert len(result) <= 96


def test_compact_description_preserves_late_quote_lost_by_head_truncation() -> None:
    source = "Use when " + ("context " * 20) + 'users say "go".'
    result = render_codex.compact_skill_description(source, max_chars=96)

    assert len(result) <= 96
    assert result.startswith("Use when")
    assert '"go"' in result
    assert result.endswith("…")
    assert not result.endswith((" ", ",", ";", ":"))


def test_compact_description_never_keeps_partial_caveman_trigger() -> None:
    source = (
        "Ultra-compressed communication mode. Cuts token usage ~75% by speaking "
        "like caveman while keeping full technical accuracy. Supports intensity "
        "levels: lite, full (default), ultra, wenyan-lite, wenyan-full, "
        'wenyan-ultra. Use when user says "caveman mode", "talk like caveman", '
        '"use caveman", "less tokens", "be brief", or invokes /caveman. Also '
        "auto-triggers when token efficiency is requested."
    )
    result = render_codex.compact_skill_description(source, max_chars=96)
    source_phrases = set(re.findall(r'"[^"]+"', source))
    result_phrases = re.findall(r'"[^"]+"', result)

    assert len(result) <= 96
    assert result.count('"') % 2 == 0
    assert all(phrase in source_phrases for phrase in result_phrases)
    assert '"talk like caveman"' in result
    assert '"talk like;' not in result


def test_word_boundary_trim_drops_incomplete_quoted_phrase() -> None:
    result = render_codex.trim_at_word_boundary(
        'Use when reviewing changes or saying "full quoted trigger" later', 45
    )

    assert result.count('"') == 0
    assert result == "Use when reviewing changes or saying…"


def test_compact_description_rewrites_supported_prefixes() -> None:
    assert render_codex.compact_skill_description(
        "Use this skill whenever reviewing permissions.", 96
    ) == "Use when reviewing permissions."
    assert render_codex.compact_skill_description(
        "You MUST use this before changing generated metadata.", 96
    ) == "Use before changing generated metadata."


def test_short_description_is_unchanged_except_whitespace() -> None:
    source = "  Use when   creating a Codex plugin.  "
    assert render_codex.compact_skill_description(source, 96) == (
        "Use when creating a Codex plugin."
    )


def test_compact_description_handles_small_limits_at_word_boundaries() -> None:
    assert render_codex.compact_skill_description("Use when alpha beta", 8) == "Use…"
    assert render_codex.compact_skill_description("alphabet", 1) == "…"
    assert render_codex.compact_skill_description("alphabet", 0) == ""


def test_compact_description_handles_unicode_and_no_sentence_punctuation() -> None:
    unicode_result = render_codex.compact_skill_description(
        "Use when reviewing café résumé changes carefully", 31
    )
    plain_result = render_codex.compact_skill_description(
        "Use when analyzing alpha beta gamma delta epsilon", 32
    )

    assert unicode_result == "Use when reviewing café résumé…"
    assert plain_result == "Use when analyzing alpha beta…"
    assert not unicode_result.endswith((" ", ",", ";", ":"))
    assert not plain_result.endswith((" ", ",", ";", ":"))


def test_compact_description_does_not_split_an_overlong_word() -> None:
    result = render_codex.compact_skill_description(
        "supercalifragilisticexpialidocious", 12
    )

    assert result == "…"


def test_compact_description_uses_stable_empty_fallback() -> None:
    assert render_codex.compact_skill_description(" \n\t ", 1) == (
        "Mirrored AI skill."
    )


def test_wrapper_uses_compact_description_in_both_copies_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source_md = root / "claude" / "skill" / "SKILL.md"
        source_md.parent.mkdir(parents=True)
        source_text = (
            "---\nname: skill\ndescription: Use when "
            + ("carefully " * 40)
            + "\n---\nBody\n"
        )
        source_md.write_text(source_text, encoding="utf-8")
        source = {
            "name": "skill",
            "path": str(source_md.parent),
            "skill_md": str(source_md),
            "description": "Use when " + ("carefully " * 40),
        }
        manifest = {"skills": [source], "plugin_skills": []}
        original_manifest = copy.deepcopy(manifest)

        with (
            patch.object(render_codex, "CODEX", root / "codex"),
            patch.object(render_codex, "PLUGIN_ROOT", root / "plugin"),
            patch.object(render_codex, "RESERVED_SKILL_NAMES", frozenset()),
        ):
            rendered, plugin_rendered, skipped = render_codex.render_skills(
                manifest, {"defaults": {"skills": "shared"}}
            )

        codex_wrapper = (root / "codex" / "skills" / "skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        plugin_wrapper = (root / "plugin" / "skills" / "skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert (rendered, plugin_rendered, skipped) == (["skill"], [], [])
        assert codex_wrapper == plugin_wrapper
        assert len(frontmatter_description(codex_wrapper)) <= 96
        assert f"`{source_md}`" in codex_wrapper
        assert str(source_md.parent) in codex_wrapper
        assert source_md.read_text(encoding="utf-8") == source_text
        assert manifest == original_manifest


def test_wrapper_compacts_adapter_frontmatter_without_changing_body() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source_md = root / "claude" / "SKILL.md"
        adapter = root / "adapter" / "SKILL.md"
        source_md.parent.mkdir(parents=True, exist_ok=True)
        adapter.parent.mkdir(parents=True, exist_ok=True)
        source_md.write_text("Claude body\n", encoding="utf-8")
        adapter_body = "# Adapter\n\nKeep this body byte-for-byte.\n"
        adapter.write_text(
            "---\nname: adapter\ndescription: Use when "
            + ("adapter-trigger " * 20)
            + "\n---\n\n"
            + adapter_body,
            encoding="utf-8",
        )
        source = {
            "name": "skill",
            "path": str(source_md.parent),
            "skill_md": str(source_md),
            "description": "Use when the Claude source applies.",
        }

        wrapper = render_codex.wrapper_skill_text(source, "skill", adapter)

        assert len(frontmatter_description(wrapper)) <= 96
        assert re.search(r'(?m)^name:\s*["\']?skill["\']?$', wrapper)
        assert "Mirrored from Claude skill:" in wrapper
        assert wrapper.split("---\n", 2)[2].lstrip("\n") == adapter_body
        assert source_md.read_text(encoding="utf-8") == "Claude body\n"


def test_distinct_same_named_enabled_skills_are_both_preserved() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first = root / "first" / "SKILL.md"
        second = root / "second" / "SKILL.md"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("First body\n", encoding="utf-8")
        second.write_text("Second body\n", encoding="utf-8")
        manifest = {
            "skills": [
                {
                    "name": "shared-name",
                    "path": str(first.parent),
                    "skill_md": str(first),
                    "description": "Use when the first trigger applies.",
                }
            ],
            "plugin_skills": [
                {
                    "name": "shared-name",
                    "path": str(second.parent),
                    "skill_md": str(second),
                    "description": "Use when the second trigger applies.",
                }
            ],
        }

        with (
            patch.object(render_codex, "CODEX", root / "codex"),
            patch.object(render_codex, "PLUGIN_ROOT", root / "plugin"),
            patch.object(render_codex, "RESERVED_SKILL_NAMES", frozenset()),
        ):
            rendered, plugin_rendered, skipped = render_codex.render_skills(
                manifest,
                {"defaults": {"skills": "shared", "plugin_skills": "shared"}},
            )

        assert rendered == ["shared-name"]
        assert plugin_rendered == ["plugin-shared-name"]
        assert skipped == []
        assert (root / "codex" / "skills" / "shared-name" / "SKILL.md").exists()
        assert (
            root / "codex" / "skills" / "plugin-shared-name" / "SKILL.md"
        ).exists()


def test_multiple_distinct_plugin_name_collisions_get_unique_wrappers() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        plugin_skills: list[dict[str, str]] = []
        for index in range(3):
            skill_md = root / f"source-{index}" / "SKILL.md"
            skill_md.parent.mkdir(parents=True)
            skill_md.write_text(f"Body {index}\n", encoding="utf-8")
            plugin_skills.append(
                {
                    "name": "collision",
                    "path": str(skill_md.parent),
                    "skill_md": str(skill_md),
                    "description": f"Use when trigger {index} applies.",
                }
            )
        manifest = {"skills": [], "plugin_skills": plugin_skills}

        with (
            patch.object(render_codex, "CODEX", root / "codex"),
            patch.object(render_codex, "PLUGIN_ROOT", root / "plugin"),
            patch.object(render_codex, "RESERVED_SKILL_NAMES", {"collision"}),
        ):
            rendered, plugin_rendered, skipped = render_codex.render_skills(
                manifest, {"defaults": {"plugin_skills": "shared"}}
            )

        assert rendered == []
        assert plugin_rendered == [
            "plugin-collision",
            "plugin-collision-2",
            "plugin-collision-3",
        ]
        assert skipped == []
        assert all(
            (root / "codex" / "skills" / name / "SKILL.md").exists()
            for name in plugin_rendered
        )


CODEX_0146_CATALOG_TOKENS = 5_440
CATALOG_UTF8_BYTE_LIMIT = int(CODEX_0146_CATALOG_TOKENS * 4 * 0.80)


def test_compact_imported_catalog_fits_verified_runtime_budget() -> None:
    rows = [
        {
            "name": f"imported-{index:03d}",
            "description": "Use when " + ("trigger " * 20),
            "locator": f"r0/imported-{index:03d}/SKILL.md",
        }
        for index in range(99)
    ]
    for row in rows:
        row["description"] = render_codex.compact_skill_description(
            row["description"], max_chars=96
        )
    rows.extend(
        {
            "name": f"system-{index:02d}",
            "description": "Use when a built-in Codex system skill applies.",
            "locator": f"r1/system-{index:02d}/SKILL.md",
        }
        for index in range(6)
    )
    rendered = render_codex.render_skill_catalog_probe(rows)

    assert len(rendered.encode("utf-8")) <= CATALOG_UTF8_BYTE_LIMIT
    assert all(
        len(row["description"])
        <= render_codex.CODEX_0146_DESCRIPTION_MAX_CHARS
        for row in rows[:99]
    )
    assert rendered.splitlines()[0] == (
        f"- imported-000: {rows[0]['description']} "
        "(file: r0/imported-000/SKILL.md)"
    )


def fake_app_server(root: Path) -> Path:
    path = root / "fake-app-server.py"
    path.write_text(
        """import json
from pathlib import Path
import signal
import sys
import time

mode = sys.argv[1]
log_path = Path(sys.argv[2])
terminated_path = Path(sys.argv[3])


def terminated(signum, frame):
    terminated_path.write_text("terminated\\n", encoding="utf-8")
    raise SystemExit(0)


signal.signal(signal.SIGTERM, terminated)
if mode == "nonzero":
    print("startup failed", file=sys.stderr, flush=True)
    raise SystemExit(7)
if mode == "timeout":
    time.sleep(60)

initialize = json.loads(sys.stdin.readline())
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(initialize) + "\\n")
if mode == "malformed":
    print("{not-json", flush=True)
    time.sleep(60)
print(json.dumps({"id": initialize["id"], "result": {"ok": True}}), flush=True)

initialized = json.loads(sys.stdin.readline())
request = json.loads(sys.stdin.readline())
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(initialized) + "\\n")
    stream.write(json.dumps(request) + "\\n")
print(json.dumps({"method": "skills/changed", "params": {}}), flush=True)
if mode == "error":
    print(json.dumps({"id": request["id"], "error": {"code": -1, "message": "boom"}}), flush=True)
    time.sleep(60)

cwd = request["params"]["cwds"][0]
print(json.dumps({
    "id": request["id"],
    "result": {
        "data": [{
            "cwd": cwd,
            "errors": [],
            "skills": [{
                "name": "kept",
                "description": "Use when kept.",
                "enabled": True,
                "path": cwd + "/kept/SKILL.md",
                "scope": "user"
            }]
        }]
    }
}), flush=True)
time.sleep(60)
""",
        encoding="utf-8",
    )
    return path


def fake_app_server_command(root: Path, mode: str = "ok") -> list[str]:
    return [
        sys.executable,
        str(fake_app_server(root)),
        mode,
        str(root / "requests.jsonl"),
        str(root / "terminated.txt"),
    ]


def test_probe_waits_for_matching_skills_response_and_terminates_child() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        result = probe_codex_skills.request_skills_list(
            command=fake_app_server_command(root),
            cwd=root,
            timeout_seconds=5,
        )

        assert result == {
            "data": [
                {
                    "cwd": str(root),
                    "errors": [],
                    "skills": [
                        {
                            "name": "kept",
                            "description": "Use when kept.",
                            "enabled": True,
                            "path": str(root / "kept" / "SKILL.md"),
                            "scope": "user",
                        }
                    ],
                }
            ]
        }
        requests = [
            json.loads(line)
            for line in (root / "requests.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert requests == [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "ai-client-config-skill-probe",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"method": "initialized"},
            {
                "id": 2,
                "method": "skills/list",
                "params": {"cwds": [str(root)], "forceReload": True},
            },
        ]
        assert (root / "terminated.txt").read_text(encoding="utf-8") == (
            "terminated\n"
        )


def test_probe_reports_timeout_malformed_nonzero_and_json_rpc_failures() -> None:
    cases = [
        ("timeout", TimeoutError, "timed out waiting for app-server response 1"),
        ("malformed", RuntimeError, "malformed JSON"),
        ("nonzero", RuntimeError, "exited with code 7"),
        ("error", RuntimeError, "app-server response 2 failed"),
    ]
    for mode, exception_type, message in cases:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            try:
                probe_codex_skills.request_skills_list(
                    command=fake_app_server_command(root, mode),
                    cwd=root,
                    timeout_seconds=0.2 if mode == "timeout" else 5,
                )
            except exception_type as exc:
                assert message in str(exc), (mode, exc)
            else:
                raise AssertionError(f"{mode}: expected {exception_type.__name__}")


def inventory_skill(
    name: str,
    *,
    description: str = "Use when this fixture applies.",
    enabled: bool = True,
    path: Path | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "enabled": enabled,
        "path": str(path or Path("/tmp") / name / "SKILL.md"),
        "scope": "user",
    }


def test_inventory_accepts_all_enabled_sources_canonical_duplicate_and_extras() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        alpha = root / "alpha" / "SKILL.md"
        beta = root / "beta" / "SKILL.md"
        duplicate = root / "token-optimizer" / "SKILL.md"
        write_skill(alpha, "Use when alpha applies.")
        write_skill(beta, "Use when beta applies.")
        write_skill(duplicate, "Use when optimizing tokens.")
        manifest = {
            "plugins": [{"id": "enabled@example", "enabled": True}],
            "skills": [
                {"name": "alpha", "skill_md": str(alpha)},
                {"name": "token-optimizer", "skill_md": str(duplicate)},
            ],
            "plugin_skills": [
                {
                    "plugin": "enabled@example",
                    "name": "beta",
                    "skill_md": str(beta),
                },
                {
                    "plugin": "enabled@example",
                    "name": "token-optimizer",
                    "skill_md": str(duplicate),
                },
            ],
        }
        response = {
            "data": [
                {
                    "errors": [],
                    "skills": [
                        inventory_skill("alpha"),
                        inventory_skill("beta"),
                        inventory_skill("token-optimizer"),
                        inventory_skill(
                            "independently-installed-system-skill",
                            description="A longer unrelated description " * 10,
                        ),
                    ],
                }
            ]
        }

        assert probe_codex_skills.validate_inventory(
            response,
            manifest,
            ["alpha", "token-optimizer", "beta", "plugin-token-optimizer"],
        ) == []


def test_inventory_rejects_missing_disabled_and_overlong_enabled_skills() -> None:
    manifest = {
        "plugins": [{"id": "enabled@example", "enabled": True}],
        "skills": [
            {"name": "missing", "skill_md": "/tmp/missing/SKILL.md"},
            {"name": "disabled", "skill_md": "/tmp/disabled/SKILL.md"},
            {"name": "verbose", "skill_md": "/tmp/verbose/SKILL.md"},
        ],
        "plugin_skills": [],
    }
    response = {
        "data": [
            {
                "errors": [],
                "skills": [
                    inventory_skill("disabled", enabled=False),
                    inventory_skill("verbose", description="x" * 97),
                ],
            }
        ]
    }

    errors = probe_codex_skills.validate_inventory(
        response, manifest, ["missing", "disabled", "verbose"]
    )

    assert "missing Claude-enabled Codex skill: missing" in errors
    assert "Claude-enabled Codex skill is disabled: disabled" in errors
    assert "Codex skill description exceeds cap: verbose" in errors


def test_inventory_rejects_disabled_plugin_skills_and_parse_errors() -> None:
    manifest = {
        "plugins": [
            {"id": "enabled@example", "enabled": True},
            {"id": "disabled@example", "enabled": False},
        ],
        "skills": [],
        "plugin_skills": [
            {
                "plugin": "disabled@example",
                "name": "disabled-plugin-skill",
                "skill_md": "/tmp/disabled-plugin-skill/SKILL.md",
            }
        ],
    }
    response = {
        "data": [
            {
                "errors": [{"path": "/broken/SKILL.md", "message": "bad YAML"}],
                "skills": [inventory_skill("disabled-plugin-skill")],
            }
        ]
    }

    errors = probe_codex_skills.validate_inventory(response, manifest, [])

    assert any(error.startswith("Codex skill parse error:") for error in errors)
    assert "disabled plugin Codex skill is present: disabled-plugin-skill" in errors


def test_probe_cli_uses_exact_command_default_report_and_repeatable_expected() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = root / "repo"
        manifest_path = root / "manifest.json"
        report_path = root / ".codex" / "claude-setup-mirror" / "report.json"
        write_json(manifest_path, {"plugins": [], "skills": [], "plugin_skills": []})
        write_json(
            report_path,
            {
                "mirrored": {
                    "skills": ["standalone"],
                    "plugin_derived_skills": ["plugin-skill"],
                }
            },
        )
        response = {
            "data": [
                {
                    "errors": [],
                    "skills": [
                        inventory_skill("standalone"),
                        inventory_skill("plugin-skill"),
                        inventory_skill("manual-one"),
                        inventory_skill("manual-two"),
                    ],
                }
            ]
        }
        argv = [
            "probe_codex_skills.py",
            "--cwd",
            str(cwd),
            "--manifest",
            str(manifest_path),
            "--expected-skill",
            "manual-one",
            "--expected-skill",
            "manual-two",
        ]
        with (
            patch.object(Path, "home", return_value=root),
            patch.object(sys, "argv", argv),
            patch.object(
                probe_codex_skills,
                "request_skills_list",
                return_value=response,
            ) as request,
            redirect_stdout(StringIO()) as stdout,
        ):
            assert probe_codex_skills.main() == 0

        request.assert_called_once_with(
            ["codex", "app-server", "--listen", "stdio://"], cwd
        )
        assert stdout.getvalue() == (
            "Codex skill probe OK: imported=4 enabled=4 "
            "disabled-plugin-skills=0 errors=0 max-description-chars<=96\n"
        )


if __name__ == "__main__":
    test_import_uses_claude_default_when_budget_is_unset()
    test_import_retains_budget_and_complete_plugin_state()
    test_plugin_inventory_only_collects_enabled_existing_skills()
    test_main_writes_stable_plugin_state_and_loads_installed_once()
    test_codex_plugin_state_block_disables_only_claude_disabled_plugins()
    test_render_overwrites_hand_written_plugin_state_and_is_idempotent()
    test_render_rejects_invalid_live_toml()
    test_every_spelling_of_a_hand_written_plugin_entry_is_replaced()
    test_write_reports_records_structured_degraded_budget_mapping()
    test_skill_budget_mapping_rejects_missing_support_and_invalid_source()
    test_check_rejects_missing_or_stale_budget_mapping()
    test_check_rejects_manifest_without_source_budget_setting()
    test_check_rejects_report_claiming_max_context_tokens_is_active()
    test_check_rejects_unsupported_budget_keys_in_generated_config()
    test_check_accepts_unsupported_budget_key_names_in_comments()
    test_check_accepts_exact_generated_state_and_rejects_hand_written_plugin_tables()
    test_check_requires_empty_state_when_manifest_omits_plugins()
    test_check_rejects_all_malformed_generated_plugin_states()
    test_check_rejects_non_table_plugins_value()
    test_check_rejects_plugin_skill_from_non_enabled_plugin()
    test_compact_description_preserves_trigger_language()
    test_compact_description_normalizes_boilerplate_and_keeps_quotes()
    test_compact_description_preserves_late_quote_lost_by_head_truncation()
    test_compact_description_never_keeps_partial_caveman_trigger()
    test_word_boundary_trim_drops_incomplete_quoted_phrase()
    test_compact_description_rewrites_supported_prefixes()
    test_short_description_is_unchanged_except_whitespace()
    test_compact_description_handles_small_limits_at_word_boundaries()
    test_compact_description_handles_unicode_and_no_sentence_punctuation()
    test_compact_description_does_not_split_an_overlong_word()
    test_compact_description_uses_stable_empty_fallback()
    test_wrapper_uses_compact_description_in_both_copies_only()
    test_wrapper_compacts_adapter_frontmatter_without_changing_body()
    test_distinct_same_named_enabled_skills_are_both_preserved()
    test_multiple_distinct_plugin_name_collisions_get_unique_wrappers()
    test_compact_imported_catalog_fits_verified_runtime_budget()
    test_probe_waits_for_matching_skills_response_and_terminates_child()
    test_probe_reports_timeout_malformed_nonzero_and_json_rpc_failures()
    test_inventory_accepts_all_enabled_sources_canonical_duplicate_and_extras()
    test_inventory_rejects_missing_disabled_and_overlong_enabled_skills()
    test_inventory_rejects_disabled_plugin_skills_and_parse_errors()
    test_probe_cli_uses_exact_command_default_report_and_repeatable_expected()
    print("codex skill budget tests OK")
