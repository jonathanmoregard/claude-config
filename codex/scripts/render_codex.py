#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
import ast
import datetime as dt
from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tomllib
from typing import Any, Callable

import common
from common import (
    AGENTS_HOME,
    CLAUDE,
    CODEX,
    HOME,
    LEGACY_PLUGIN_LINK,
    MANIFEST,
    PLUGIN_NAME,
    PLUGIN_ROOT,
    REPO,
    REPORT_DIR,
    RESERVED_SKILL_NAMES,
    SECRET_KEY_PARTS,
    copy_tree_clean,
    dump_json,
    ignore_generated,
    link_or_copy,
    load_json,
    located,
    mkdir,
    remove_path,
    toml_array,
    toml_scalar,
    toml_string,
    write_file,
    yaml_string,
)


def load_support() -> dict[str, Any]:
    return tomllib.loads((REPO / "client-support.toml").read_text(encoding="utf-8"))


def skill_budget_mapping(
    manifest: dict[str, Any], support: dict[str, Any]
) -> dict[str, Any]:
    source_value = (manifest.get("source_settings") or {}).get(
        "skill_listing_budget_fraction"
    )
    if (
        isinstance(source_value, bool)
        or not isinstance(source_value, (int, float))
        or source_value <= 0
    ):
        raise ValueError(
            "skillListingBudgetFraction source value must be a positive number"
        )

    support_entry = (support.get("settings") or {}).get(
        "skillListingBudgetFraction"
    )
    if not isinstance(support_entry, dict):
        raise ValueError(
            "client support is missing skillListingBudgetFraction"
        )
    target_keys = (
        "status",
        "codex_native_fraction",
        "codex_version_verified",
        "strategy",
    )
    missing = [key for key in target_keys if key not in support_entry]
    if missing:
        raise ValueError(
            "skillListingBudgetFraction support entry is missing: "
            + ", ".join(missing)
        )

    return {
        "source_setting": "skillListingBudgetFraction",
        "source_value": source_value,
        **{key: support_entry[key] for key in target_keys},
    }


def normalize_permission_path(raw: str) -> str:
    if raw.startswith("//"):
        raw = raw[1:]
        return os.path.normpath(raw)
    if raw == "~" or raw.startswith("~/"):
        return os.path.normpath(str(Path(raw).expanduser()))
    if raw.startswith("/"):
        return os.path.normpath(str(CLAUDE / raw[1:]))
    return os.path.normpath(raw)


def normalize_candidate_path(raw: str) -> str | None:
    if raw.startswith("//"):
        raw = raw[1:]
    path = Path(raw).expanduser()
    if not path.is_absolute():
        # Claude anchors these to each session's cwd; a global Codex root cannot.
        return None
    return os.path.normpath(str(path))


def has_path_glob(path: str) -> bool:
    return any(char in path for char in "*?[")


def permission_path(entry: str, kind: str) -> str | None:
    prefix = f"{kind}("
    if not entry.startswith(prefix) or not entry.endswith(")"):
        return None
    raw = entry[len(prefix) : -1]
    if raw.endswith("/**"):
        raw = raw[:-3]
    return normalize_permission_path(raw)


def permission_glob_matches(path: Path, pattern: Path) -> bool:
    path_parts = path.parts
    pattern_parts = pattern.parts
    memo: dict[tuple[int, int], bool] = {}

    def matches(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = matches(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and matches(pattern_index, path_index + 1)
            )
        else:
            result = path_index < len(path_parts) and fnmatchcase(
                path_parts[path_index], pattern_parts[pattern_index]
            ) and matches(pattern_index + 1, path_index + 1)
        memo[key] = result
        return result

    return matches(0, 0)


def edit_denied(path: str, permissions: dict[str, Any]) -> bool:
    lexical = Path(normalize_candidate_path(path) or os.path.normpath(path))
    resolved = lexical.resolve(strict=False)
    for entry in permissions.get("deny") or []:
        denied = permission_path(entry, "Read") or permission_path(entry, "Edit")
        if not denied:
            continue
        denied_path = Path(denied)
        if not denied_path.is_absolute():
            denied_path = Path("**") / denied_path
        if has_path_glob(str(denied_path)):
            roots = (lexical, *lexical.parents, resolved, *resolved.parents)
            if any(permission_glob_matches(root, denied_path) for root in roots):
                return True
        elif any(root == denied_path or denied_path in root.parents for root in (lexical, resolved)):
            return True
    return False


def path_covered_by_roots(path: Path, roots: set[str]) -> bool:
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


# Job state written by ~/.claude/bin/ai-router when Codex is the host that
# delegates. The launcher runs inside Codex's workspace-write sandbox, so the
# directory must be a writable root or sandboxed hosts cannot delegate.
# Kept as a module constant for the legacy permission-mapping report.
AI_ROUTER_STATE = Path.home() / ".local" / "state" / "ai-router"
CODEX_PERMISSION_PROFILE = "repos_dev"
CODEX_APPROVAL_POLICY = "never"


def codex_writable_roots(permissions: dict[str, Any]) -> list[str]:
    # REPO is deliberately absent: the sync unit renders from a throwaway
    # clone bind-mounted at /tmp/ai-client-config-repo, and baking that path
    # in made bwrap fail on every sandboxed Codex command once the mount was
    # gone (2026-09-14). The renderer's own location is not a permission
    # source; edit rights on any checkout come from the mirrored Claude rules.
    # Renderer and sync service own ~/.codex, ~/.agents, and generated plugin
    # state. Sandboxed agent commands must not be able to rewrite their own
    # configuration, hooks, rules, or plugin code. ai-router state is the one
    # operational root commands legitimately need.
    candidates = [str(AI_ROUTER_STATE)]
    if permissions.get("defaultMode") == "acceptEdits":
        candidates.extend(permissions.get("additionalDirectories") or [])
    for entry in permissions.get("allow") or []:
        path = permission_path(entry, "Edit")
        if path and Path(path).is_absolute() and not has_path_glob(path):
            candidates.append(path)

    lexical_roots = {normalized for path in candidates if (normalized := normalize_candidate_path(path))}
    operational_roots = {str(AI_ROUTER_STATE)}
    protected_roots = {
        AGENTS_HOME,
        CODEX,
        PLUGIN_ROOT,
        CLAUDE / "hooks",
        CLAUDE / "mcps",
        CLAUDE / "skills",
    }
    roots = {
        root
        for root in lexical_roots
        if not any(
            Path(root) == protected
            or Path(root) in protected.parents
            or protected in Path(root).parents
            for protected in protected_roots
        )
        and (
            root in operational_roots
            or (resolved := Path(root).resolve(strict=False)) == Path(root)
            or path_covered_by_roots(resolved, lexical_roots)
        )
    }
    return sorted(path for path in roots if not edit_denied(path, permissions))


def generated_block(text: str, start: str, end: str, replacement: str = "") -> str:
    if start not in text or end not in text:
        return text
    before = text.split(start, 1)[0].rstrip()
    after = text.split(end, 1)[1].lstrip()
    separator = "\n\n" if before else ""
    middle = separator + replacement.strip() if replacement.strip() else ""
    suffix = f"\n\n{after}" if after else "\n"
    return before + middle + suffix


def skill_status(support: dict[str, Any], name: str, plugin: bool = False) -> str:
    skills = support.get("skills", {})
    if name in skills:
        return skills[name].get("status", "shared")
    return support.get("defaults", {}).get("plugin_skills" if plugin else "skills", "shared")


def ensure_legacy_plugin_link() -> None:
    if LEGACY_PLUGIN_LINK == PLUGIN_ROOT:
        return
    mkdir(LEGACY_PLUGIN_LINK.parent)
    # The link names PLUGIN_ROOT under HOME, wherever it is written.
    link = located(LEGACY_PLUGIN_LINK)
    if link.exists() or link.is_symlink():
        if link.is_symlink() and Path(os.readlink(link)) == PLUGIN_ROOT:
            return
        remove_path(link)
    link.symlink_to(PLUGIN_ROOT, target_is_directory=True)


CODEX_0146_DESCRIPTION_MAX_CHARS = 96

DESCRIPTION_PREFIX_REWRITES = (
    ("This skill should be used when ", "Use when "),
    ("Use this skill whenever ", "Use when "),
    ("You MUST use this before ", "Use before "),
)

DESCRIPTION_COMPACTION_REWRITES = (
    ("Use when users say ", "Use when "),
)


def trim_at_word_boundary(value: str, max_chars: int) -> str:
    value = value.rstrip(" ,;:")
    if len(value) <= max_chars:
        return value
    if max_chars <= 0:
        return ""
    if max_chars == 1:
        return "…"

    prefix = value[: max_chars - 1].rstrip()
    if prefix and len(prefix) < len(value) and not value[len(prefix)].isspace():
        prefix = prefix.rsplit(" ", 1)[0] if " " in prefix else ""
    open_quote: int | None = None
    for index, character in enumerate(prefix):
        if open_quote is None and character in {'"', "“"}:
            open_quote = index
        elif open_quote is not None and character in {'"', "”"}:
            open_quote = None
    if open_quote is not None:
        prefix = prefix[:open_quote]
    prefix = prefix.rstrip(" ,;:")
    return f"{prefix}…" if prefix else "…"


def compact_skill_description(description: str, max_chars: int) -> str:
    value = " ".join(description.split())
    if not value:
        return "Mirrored AI skill."

    for source, replacement in DESCRIPTION_PREFIX_REWRITES:
        if value.startswith(source):
            value = replacement + value[len(source) :]
            break
    value = value.rstrip(" ,;:")
    if len(value) <= max_chars:
        return value

    candidate = re.split(r"(?<=[.!?])\s+", value, maxsplit=1)[0]
    for source, replacement in DESCRIPTION_COMPACTION_REWRITES:
        if source in candidate:
            candidate = candidate.replace(source, replacement, 1)

    quoted = re.findall(r'(["“][^"”]{2,40}["”])', value)
    for phrase in quoted:
        if phrase in candidate:
            continue
        expanded = f"{candidate.rstrip('.')}; {phrase}"
        if len(expanded) + 1 > max_chars:
            break
        candidate = expanded

    truncated = trim_at_word_boundary(candidate, max_chars)
    visible_quotes = [phrase for phrase in quoted if phrase in truncated]
    missing_quotes = [phrase for phrase in quoted if phrase not in truncated]
    protected_quotes: list[str] = []
    protected_candidate = truncated
    for phrase in missing_quotes:
        suffix = "; " + "; ".join([*protected_quotes, phrase]) + "…"
        head_budget = max_chars - len(suffix)
        if head_budget <= 0:
            continue
        head = trim_at_word_boundary(candidate, head_budget)
        if head.endswith("…"):
            head = head[:-1].rstrip(" ,;:")
        if not head or any(phrase not in head for phrase in visible_quotes):
            continue
        protected_quotes.append(phrase)
        protected_candidate = head + suffix
    if protected_quotes:
        return protected_candidate

    if candidate != value and len(candidate.rstrip(". ,;:")) + 1 <= max_chars:
        return candidate.rstrip(". ,;:") + "…"
    return truncated


def render_skill_catalog_probe(rows: list[dict[str, str]]) -> str:
    return "\n".join(
        f"- {row['name']}: {row['description']} (file: {row['locator']})"
        for row in rows
    )


def replace_frontmatter_description(text: str, description: str) -> str:
    frontmatter = re.match(
        r"\A---(?P<newline>\r?\n)(?P<header>.*?)(?P<closing>\r?\n---(?:\r?\n|$))",
        text,
        flags=re.DOTALL,
    )
    if frontmatter is None:
        return text
    header, replacements = re.subn(
        r"(?m)^description:.*$",
        lambda _: f"description: {yaml_string(description)}",
        frontmatter.group("header"),
        count=1,
    )
    if replacements == 0:
        header += (
            frontmatter.group("newline")
            + f"description: {yaml_string(description)}"
        )
    return (
        text[: frontmatter.start("header")]
        + header
        + text[frontmatter.end("header") :]
    )


def replace_frontmatter_name(text: str, name: str) -> str:
    frontmatter = re.match(
        r"\A---(?P<newline>\r?\n)(?P<header>.*?)(?P<closing>\r?\n---(?:\r?\n|$))",
        text,
        flags=re.DOTALL,
    )
    if frontmatter is None:
        return text
    header, replacements = re.subn(
        r"(?m)^name:.*$",
        lambda _: f"name: {yaml_string(name)}",
        frontmatter.group("header"),
        count=1,
    )
    if replacements == 0:
        header += frontmatter.group("newline") + f"name: {yaml_string(name)}"
    return (
        text[: frontmatter.start("header")]
        + header
        + text[frontmatter.end("header") :]
    )


def wrapper_skill_text(
    source: dict[str, Any], name: str, adapter: Path | None = None
) -> str:
    source_skill = Path(source["skill_md"])
    source_skill.read_text(encoding="utf-8")
    if adapter is not None:
        text = adapter.read_text(encoding="utf-8")
        adapter_description = "Mirrored AI skill."
        description_line = re.search(r"(?m)^description:\s*(.+)$", text)
        if description_line is not None:
            encoded = description_line.group(1).strip()
            try:
                adapter_description = json.loads(encoded)
            except json.JSONDecodeError:
                adapter_description = encoded.strip("\"'")
        text = replace_frontmatter_description(
            text,
            compact_skill_description(
                adapter_description, CODEX_0146_DESCRIPTION_MAX_CHARS
            ),
        )
        text = replace_frontmatter_name(text, name)
        text = text.replace(
            "\n---",
            "\n# Mirrored from Claude skill:\n---",
            1,
        )
    else:
        source_root = source["path"]
        description = compact_skill_description(
            source.get("description")
            or f"Mirrored Claude skill from {source['name']}.",
            CODEX_0146_DESCRIPTION_MAX_CHARS,
        )
        text = f"""---
name: {yaml_string(name)}
description: {yaml_string(description)}
---

# {name}

Mirrored from Claude skill:

`{source_skill}`

When this skill applies, read that source `SKILL.md` and follow it. Resolve
relative paths against `{source_root}`. This wrapper keeps Codex startup
context small while preserving the original Claude setup.
"""
    return text


def command_template_supported(text: str) -> bool:
    return not (
        "$ARGUMENTS" in text
        or re.search(r"\$[0-9]", text)
        or ("{{" in text and "}}" in text)
        or "!`" in text
        or "! `" in text
    )


def command_skill_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return f"claude-command-{slug or 'command'}"[:64].rstrip("-")


def command_skill_text(command: dict[str, Any], name: str) -> str:
    source = Path(command["path"])
    body = markdown_body(source)
    plugin_root = command.get("plugin_root")
    if isinstance(plugin_root, str) and plugin_root:
        body = body.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)
        body = body.replace("$CLAUDE_PLUGIN_ROOT", plugin_root)
    body = body.replace("CLAUDE.md", "AGENTS.md").replace("Claude Code", "Codex")
    description = compact_skill_description(
        command.get("description") or f"Migrated Claude command {command['name']}.",
        CODEX_0146_DESCRIPTION_MAX_CHARS,
    )
    return f"""---
name: {yaml_string(name)}
description: {yaml_string(description)}
---

# {name}

Mirrored from Claude command:

`{source}`

Use this skill when the user invokes or asks for the migrated Claude command
`{command['name']}`.

{body}
"""


def publish_wrapper_skill(dst: Path, text: str) -> None:
    # Anything else in the directory is pruned with the rest of the
    # unplanned paths at the end of the publish.
    put_file(dst / "SKILL.md", text)


def allocate_skill_name(name: str, used: set[str], collision_prefix: str) -> str:
    candidate = name if name not in used else f"{collision_prefix}{name}"
    base = candidate
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def prepare_skills(
    manifest: dict[str, Any], support: dict[str, Any]
) -> tuple[
    list[tuple[Path, str]],
    list[str],
    list[str],
    list[dict[str, str]],
]:
    prepared: list[tuple[Path, str]] = []
    rendered: list[str] = []
    plugin_rendered: list[str] = []
    skipped: list[dict[str, str]] = []
    used = set(RESERVED_SKILL_NAMES)
    adapter = REPO / support.get("skills", {}).get("orchestrate", {}).get("codex_adapter", "")
    for skill in manifest.get("skills", []):
        status = skill_status(support, skill["name"])
        if status in {"claude-only", "unsupported", "reference-only"}:
            skipped.append({"name": skill["name"], "status": status})
            continue
        name = allocate_skill_name(skill["name"], used, "imported-")
        skill_adapter = adapter if skill["name"] == "orchestrate" and adapter.exists() else None
        prepared.append(
            (
                CODEX / "skills" / name,
                wrapper_skill_text(skill, name, skill_adapter),
            )
        )
        prepared.append(
            (
                PLUGIN_ROOT / "skills" / name,
                wrapper_skill_text(skill, name, skill_adapter),
            )
        )
        rendered.append(name)
    for skill in manifest.get("plugin_skills", []):
        status = skill_status(support, skill["name"], plugin=True)
        if status in {"claude-only", "unsupported", "reference-only"}:
            skipped.append({"name": skill["name"], "status": status, "plugin": skill.get("plugin", "")})
            continue
        name = allocate_skill_name(skill["name"], used, "plugin-")
        prepared.append(
            (CODEX / "skills" / name, wrapper_skill_text(skill, name))
        )
        prepared.append(
            (PLUGIN_ROOT / "skills" / name, wrapper_skill_text(skill, name))
        )
        plugin_rendered.append(name)
    for command in manifest.get("commands", []):
        source = Path(command["path"])
        template = markdown_body(source)
        if not command_template_supported(template):
            skipped.append(
                {
                    "name": command["name"],
                    "status": "unsupported-template",
                    "plugin": command.get("plugin", ""),
                }
            )
            continue
        name = allocate_skill_name(
            command_skill_name(command["name"]), used, "imported-"
        )
        text = command_skill_text(command, name)
        prepared.append((CODEX / "skills" / name, text))
        prepared.append((PLUGIN_ROOT / "skills" / name, text))
        rendered.append(name)
    return prepared, rendered, plugin_rendered, skipped


def publish_prepared_skills(prepared: tuple[tuple[Path, str], ...]) -> None:
    for dst, text in prepared:
        publish_wrapper_skill(dst, text)


def render_skills(
    manifest: dict[str, Any], support: dict[str, Any]
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    prepared, rendered, plugin_rendered, skipped = prepare_skills(manifest, support)
    publish_prepared_skills(tuple(prepared))
    return rendered, plugin_rendered, skipped


CODEX_SUPPORTED_HOOK_EVENTS = frozenset(
    {
        "PermissionRequest",
        "PostCompact",
        "PostToolUse",
        "PreCompact",
        "PreToolUse",
        "SessionEnd",
        "SessionStart",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "UserPromptSubmit",
    }
)
CODEX_UNSUPPORTED_HOOK_EVENTS = frozenset(
    {
        "ConfigChange",
        "CwdChanged",
        "InstructionsLoaded",
        "MessageDisplay",
        "PermissionDenied",
        "PostToolUseFailure",
        "StopFailure",
    }
)
LEGACY_SAFE_BASH_TOOL = "mcp__safe-bash__exec"
CODEX_SAFE_BASH_TOOL = "mcp__safe_bash__exec"
SAFE_BASH_TOOL_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_-]){re.escape(LEGACY_SAFE_BASH_TOOL)}(?![A-Za-z0-9_-])"
)
CODEX_CONTEXT_HOOK_EVENTS = frozenset({"SessionStart", "SubagentStart", "UserPromptSubmit"})
HOOK_OUTPUT_ADAPTER = CODEX / "scripts" / "codex_hook_output_adapter.py"
HOOK_FANOUT = CODEX / "scripts" / "codex_hook_fanout.py"
HOOK_WRAPPER_ROOT = CODEX / "hooks" / "claude-mirror"
EXECPOLICY_START = "# BEGIN claude-mirror generated Bash permissions"
EXECPOLICY_END = "# END claude-mirror generated Bash permissions"
# The built-in GitHub connector bypasses the Git broker and GitHub MCP policy.
BUILTIN_GITHUB_CONNECTOR_APP_ID = "connector_76869538009648d5b282a4bb21c3d157"
CODEX_AGENTS_PREAMBLE = """# Codex Instructions Mirrored From Claude

This file is generated from `~/.claude/CLAUDE.md`; edit the Claude source and
run the mirror/importer. Claude-specific names remain as historical context.
Codex-native developer instructions, permissions, and user instructions take
precedence.

Codex mappings:
- `CLAUDE.md` global instructions map to this `AGENTS.md`.
- Skills, commands, agents, plugins, MCP servers, and compatible hooks are
  rendered by the renderer under `codex/` in the claude-config repo.

Git boundary:
- Native read-only Git is allowed only with `GIT_OPTIONAL_LOCKS=0`.
- Every Git mutation must use the structured `mcp__git_broker__git` broker.
- Never fall back to native mutating Git. No `mcp__git_broker__exec` exists.

"""

BASH_DENY_PREFIXES = {
    "Bash(sudo)": ("sudo",),
    "Bash(sudo *)": ("sudo",),
    # arftl-allow: destructive-rm (deny-rule table: data, not a command)
    "Bash(rm -rf *)": ("rm", "-rf"),
    # arftl-allow: destructive-rm (deny-rule table, as above)
    "Bash(rm -fr *)": ("rm", "-fr"),
    "Bash(mkfs *)": ("mkfs",),
    "Bash(dd *)": ("dd",),
    "Bash(git push --force*)": ("git", "push", "--force"),
    "Bash(git push -f*)": ("git", "push", "-f"),
    # arftl-allow: history-rewrite (deny-rule table, as above)
    "Bash(git reset --hard*)": ("git", "reset", "--hard"),
    "Bash(gh pr merge)": ("gh", "pr", "merge"),
    "Bash(gh pr merge *)": ("gh", "pr", "merge"),
    "Bash(secret-tool*)": ("secret-tool",),
}

# These names come from github-mcp-server's published tool schemas. The server's
# exclusion policy takes precedence over toolsets; disabled_tools repeats the
# same boundary in Codex so the tools are unavailable at both layers.
GITHUB_MCP_EXCLUDED_TOOLS = (
    "merge_pull_request",
    "pull_request_review_write",
    "delete_file",
    "create_repository",
    "fork_repository",
    "create_branch",
    "create_or_update_file",
    "push_files",
    "update_pull_request_branch",
    "actions_run_trigger",
)
GITHUB_MCP_NAME = "github"
# What Claude must register. Codex's own GitHub servers are generated per
# installation from the mux config, so they are discovered with
# `rendered_github_names`, never listed here -- a dynamic set needs dynamic
# validation, or a deleted child stops being checked for drift.
GITHUB_MCP_NAMES = (GITHUB_MCP_NAME,)
# A Codex `[mcp_servers.<name>]` key, and a TOML bare key: the owner half comes
# from the mux config, so it is checked rather than trusted.
CODEX_MCP_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
PER_USER_PROFILE_BIN = (
    Path("/etc/profiles/per-user") / Path.home().name / "bin"
)


def normalize_hook_tool_name(value: str) -> str:
    return SAFE_BASH_TOOL_PATTERN.sub(CODEX_SAFE_BASH_TOOL, value)


def matcher_targets_only_claude_shell(value: object) -> bool:
    """Return true only for simple matchers confined to retired shell names.

    Codex code-mode shell calls arrive as ``exec`` with a different payload
    and result schema. Re-labeling them ``Bash`` would make Claude-specific
    guards look active without preserving their semantics. Mixed matchers are
    kept unless every alternative is one of the known Claude/safe-bash names.
    """
    if not isinstance(value, str) or not value:
        return False
    parts = {part.strip().lstrip("^").rstrip("$") for part in value.split("|")}
    return bool(parts) and parts <= {
        "Bash",
        LEGACY_SAFE_BASH_TOOL,
        CODEX_SAFE_BASH_TOOL,
    }


HOOK_RUNTIME_PREFIX = "runtime-"
HOOK_RUNTIME_ADAPTER_NAME = "codex_hook_output_adapter.py"
HOOK_RUNTIME_FANOUT_NAME = "codex_hook_fanout.py"


def hook_content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:24]


def hook_runtime_digest(adapter: bytes, fanout: bytes) -> str:
    return hook_content_digest(adapter + b"\0" + fanout)


def content_addressed_hook_path(label: str, suffix: str, text: str) -> Path:
    digest = hook_content_digest(text.encode("utf-8"))
    safe_label = re.sub(r"[^A-Za-z0-9_-]", "-", label).lower()
    return HOOK_WRAPPER_ROOT / f"{safe_label}-{digest}{suffix}"


def hook_wrapper_path(event: str, wrapper_text: str) -> Path:
    return content_addressed_hook_path(event, ".sh", wrapper_text)


def hook_wrapper_text(command: str, asynchronous: bool = False) -> str:
    invocation = (
        f"({command}) </dev/null >/dev/null 2>&1 &"
        if asynchronous
        else command
    )
    return f"#!/usr/bin/env bash\nset -o pipefail\n{invocation}\n"


def hook_runtime_paths(adapter_text: str, fanout_text: str) -> tuple[Path, Path]:
    digest = hook_runtime_digest(
        adapter_text.encode("utf-8"), fanout_text.encode("utf-8")
    )
    runtime_root = HOOK_WRAPPER_ROOT / f"{HOOK_RUNTIME_PREFIX}{digest}"
    return (
        runtime_root / HOOK_RUNTIME_ADAPTER_NAME,
        runtime_root / HOOK_RUNTIME_FANOUT_NAME,
    )


def hook_fanout_paths(
    event: str, plan: dict[str, Any], fanout_path: Path
) -> tuple[Path, Path]:
    plan_text = dump_json(plan)
    plan_path = content_addressed_hook_path(f"{event}-plan", ".json", plan_text)
    dispatcher_text = hook_fanout_wrapper_text(event, plan_path, fanout_path)
    dispatcher_path = content_addressed_hook_path(
        f"{event}-fanout", ".sh", dispatcher_text
    )
    return plan_path, dispatcher_path


def hook_fanout_wrapper_text(event: str, plan_path: Path, fanout_path: Path) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -o pipefail\n"
        f"exec python3 {shlex.quote(str(fanout_path))} "
        f"{shlex.quote(event)} {shlex.quote(str(plan_path))}\n"
    )


def hook_file_matches_its_address(path: Path) -> bool:
    """Whether the file on disk is the text its content-addressed name names.

    A runtime directory is addressed by its adapter and fan-out pair; every
    other hook file by its own bytes, the digest being the last "-" field of
    its name. Read from the path alone, independent of HOOK_WRAPPER_ROOT.
    """
    try:
        if path.parent.name.startswith(HOOK_RUNTIME_PREFIX):
            digest = path.parent.name[len(HOOK_RUNTIME_PREFIX) :]
            return digest == hook_runtime_digest(
                (path.parent / HOOK_RUNTIME_ADAPTER_NAME).read_bytes(),
                (path.parent / HOOK_RUNTIME_FANOUT_NAME).read_bytes(),
            )
        digest = path.name.removesuffix(path.suffix).rpartition("-")[2]
        return digest == hook_content_digest(path.read_bytes())
    except OSError:
        return False


def managed_root(path: Path) -> Path:
    """The managed path that holds path. Every publish write goes through
    this, so one aimed outside the paths the mirror owns (which the
    production publish would never carry over) fails instead.

    Containment is lexical, so a path must be absolute with no "..": a
    generated name such as an agent's "../../x" would otherwise pass while
    the write lands outside. A link on the way is wrong_type_path's to find.
    """
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{path}: not an absolute path without '..'; the mirror refuses it")
    for root in managed_paths():
        if path == root or root in path.parents:
            return root
    raise ValueError(f"{path}: outside every path the mirror owns")


def wrong_type_path(path: Path, *, directory: bool = False) -> Path | None:
    """What stands where a publish step puts a file, or a directory, at path.

    Anything but a real directory at an ancestor from its managed root down
    (a link there would send the write outside the mirror), else at path
    itself a directory where a file goes or a non-directory where a
    directory goes. Below a non-directory nothing exists: at most one.
    """
    root = managed_root(path)
    for ancestor in reversed(path.parents):
        if ancestor == root or root in ancestor.parents:
            on_disk = located(ancestor)
            if on_disk.is_symlink() or (on_disk.exists() and not on_disk.is_dir()):
                return ancestor
    on_disk = located(path)
    real_dir = on_disk.is_dir() and not on_disk.is_symlink()
    if directory:
        return path if (on_disk.exists() or on_disk.is_symlink()) and not real_dir else None
    return path if real_dir else None


def make_room(path: Path, *, directory: bool = False) -> None:
    """Remove what stands in the way of putting path. Codex config is a
    mirror: a wrong type is replaced like wrong content, with no copy kept."""
    blocking = wrong_type_path(path, directory=directory)
    if blocking is not None:
        remove_path(blocking)


def put_file(path: Path, text: str, mode: int | None = None) -> None:
    """The one way a publish writes a file: room made, then an atomic write
    that leaves a file already holding text untouched."""
    make_room(path)
    write_file(path, text, mode)


def put_dir(path: Path) -> None:
    make_room(path, directory=True)
    mkdir(path)


def put_tree(src: Path, dst: Path) -> None:
    """put_file for a copied tree: room made, then dst swapped in whole, or
    left untouched when it already is that copy."""
    make_room(dst, directory=True)
    copy_tree_clean(src, dst)


def publish_hook_generation(files: dict[Path, str]) -> None:
    # Active Codex sessions can retain dispatcher paths from an older hooks.json.
    # These files are content-addressed: add deltas, never prune prior
    # generations. A sync renders into a fresh stage holding none of them;
    # sync_codex.publish_stage, which sees the home's, refuses a digest
    # collision with one.
    for path, text in files.items():
        put_file(path, text, mode=0o755 if path.suffix == ".sh" else None)


def matcher_targets_file_tools(matcher: object) -> bool:
    if not isinstance(matcher, str):
        return False
    try:
        pattern = re.compile(matcher)
    except re.error:
        return False
    return any(pattern.search(name) is not None for name in ("Write", "Edit"))


def adapt_hook_command(
    event: str,
    wrapper: Path,
    *,
    output_adapter_path: Path,
    adapt_file_tools: bool = False,
) -> str:
    if event not in CODEX_CONTEXT_HOOK_EVENTS and not adapt_file_tools:
        return shlex.quote(str(wrapper))
    parts = [
        "python3",
        shlex.quote(str(output_adapter_path)),
        shlex.quote(event),
        shlex.quote(str(wrapper)),
    ]
    if adapt_file_tools:
        parts.append("--adapt-file-tools")
    return " ".join(parts)


def bash_rules(entries: object) -> list[str]:
    return [
        entry
        for entry in entries if isinstance(entries, list)
        if isinstance(entry, str) and (entry == "Bash" or entry.startswith("Bash("))
    ]


def codex_approval_policy(permissions: dict[str, Any]) -> str:
    # The managed native profile is the complete filesystem authority.
    # Never permit an approval flow or automatic reviewer to widen it.
    return CODEX_APPROVAL_POLICY


def codex_sandbox_mode(settings: dict[str, Any] | None = None) -> str:
    # Never inherit a disabled Claude sandbox into Codex. Codex has its own
    # native OS sandbox and MCP/exec surface, so this is a client-specific
    # safety floor rather than a literal translation.
    if settings is not None and not isinstance(settings, dict):
        raise TypeError("Claude settings must be an object")
    sandbox = (settings or {}).get("sandbox") or {}
    if not isinstance(sandbox, dict):
        raise TypeError("Claude sandbox settings must be an object")
    return "workspace-write"


def codex_reasoning_effort(settings: dict[str, Any] | None = None) -> str | None:
    value = (settings or {}).get("effortLevel")
    if value == "max":
        value = "xhigh"
    return value if value in {"none", "minimal", "low", "medium", "high", "xhigh"} else None


def codex_web_search_mode(permissions: dict[str, Any]) -> str | None:
    denied = set(permissions.get("deny") or [])
    return "disabled" if "WebSearch" in denied else None


def codex_network_access(
    permissions: dict[str, Any], settings: dict[str, Any] | None = None
) -> bool:
    """Keep generated Codex workspace-write jobs offline by default.

    Claude's Bash permission controls Claude's own executor, not the network
    scope of every Codex workspace-write process. Callers that need networking
    opt in per invocation, where the privilege is auditable.
    """
    return False


def codex_hook_trust_hash(hooks: dict[str, Any]) -> str:
    digest = hashlib.sha256(dump_json(hooks).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def codex_hook_states(hooks: dict[str, Any]) -> list[tuple[str, str]]:
    event_labels = {
        "PreToolUse": "pre_tool_use",
        "PermissionRequest": "permission_request",
        "PostToolUse": "post_tool_use",
        "PreCompact": "pre_compact",
        "PostCompact": "post_compact",
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "UserPromptSubmit": "user_prompt_submit",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
        "Stop": "stop",
    }
    result: list[tuple[str, str]] = []
    for event, groups in (hooks.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher") or None
            if event in {"UserPromptSubmit", "Stop"}:
                matcher = None
            for handler_index, action in enumerate(group.get("hooks") or []):
                if not isinstance(action, dict) or action.get("type") != "command":
                    continue
                timeout = action.get("timeout")
                if timeout is None:
                    timeout = 1 if event == "SessionEnd" else 600
                elif event == "SessionEnd":
                    timeout = min(max(timeout, 1), 3)
                else:
                    timeout = max(timeout, 1)
                handler: dict[str, Any] = {
                    "type": "command",
                    "command": action["command"],
                    "timeout": timeout,
                    "async": action.get("async", False),
                }
                for source, target in (
                    ("commandWindows", "commandWindows"),
                    ("statusMessage", "statusMessage"),
                    ("additionalContextLimit", "additionalContextLimit"),
                ):
                    if action.get(source) is not None:
                        handler[target] = action[source]
                identity: dict[str, Any] = {
                    "event_name": event_labels[event],
                    "hooks": [handler],
                }
                if matcher is not None:
                    identity["matcher"] = matcher
                serialized = json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                digest = "sha256:" + hashlib.sha256(serialized).hexdigest()
                key = (
                    f"{CODEX / 'hooks.json'}:{event_labels[event]}:"
                    f"{group_index}:{handler_index}"
                )
                result.append((key, digest))
    return result


def codex_bash_deny_rules(permissions: dict[str, Any]) -> tuple[list[str], list[str]]:
    prefixes: list[tuple[str, ...]] = []
    unsupported: list[str] = []
    for entry in bash_rules(permissions.get("deny") or []):
        prefix = BASH_DENY_PREFIXES.get(entry)
        if prefix is None:
            unsupported.append(entry)
        elif prefix not in prefixes:
            prefixes.append(prefix)
    rules = [
        "prefix_rule("
        f"pattern={json.dumps(list(prefix), separators=(',', ':')).replace(',', ', ')}, "
        'decision="forbidden", '
        'justification="Mirrored Claude Bash deny")'
        for prefix in prefixes
    ]
    return rules, unsupported


# The Codex execpolicy builtins that carry a decision, verified against Codex
# 0.161.0 (`codex execpolicy check`): name -> (positional index of decision,
# decision when omitted, decisions Codex accepts). An "allow" prefix_rule runs
# the command unsandboxed on the host; an "allow" network_rule opens the
# managed network proxy to that host. The third builtin, host_executable,
# only maps absolute program paths onto existing basename rules.
EXECPOLICY_DECISION_BUILTINS: dict[str, tuple[int, str | None, tuple[str, ...]]] = {
    "prefix_rule": (1, "allow", ("allow", "prompt", "forbidden")),
    "network_rule": (2, None, ("allow", "deny", "forbidden", "prompt")),
}


class ExecpolicyError(ValueError):
    """A rules file problem the sync cannot repair, at a 1-based line.

    str() is the problem alone; located() names the file and line the way the
    check words its findings, for the error that stops the render.
    """

    def __init__(self, message: str, line: int) -> None:
        super().__init__(message)
        self.line = line

    def located(self, path: Path) -> ValueError:
        return ValueError(f"{path}: line {self.line}: {self}; a human must fix it")


def _execpolicy_rule_decision(node: ast.stmt) -> tuple[str, str] | None:
    """(builtin, literal decision) of a top-level direct rule call, else None."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    call = node.value
    if (
        not isinstance(call.func, ast.Name)
        or call.func.id not in EXECPOLICY_DECISION_BUILTINS
    ):
        return None
    name = call.func.id
    index, default, accepted = EXECPOLICY_DECISION_BUILTINS[name]

    def fail(message: str) -> ExecpolicyError:
        return ExecpolicyError(message, node.lineno)

    if any(isinstance(argument, ast.Starred) for argument in call.args):
        raise fail(f"{name} must not use starred positional arguments")
    if any(keyword.arg is None for keyword in call.keywords):
        raise fail(f"{name} must not use **kwargs")
    decisions = [
        keyword.value for keyword in call.keywords if keyword.arg == "decision"
    ]
    if len(decisions) > 1:
        raise fail(f"{name} must declare decision at most once")
    positional_decision = call.args[index] if len(call.args) > index else None
    if positional_decision is not None and decisions:
        raise fail(f"{name} must not declare decision both positionally and by keyword")
    if positional_decision is None and not decisions:
        if default is None:
            raise fail(f"{name} must declare a decision")
        # Codex 0.161.0 execpolicy check confirms the omitted default is allow.
        return name, default
    decision = positional_decision or decisions[0]
    if not isinstance(decision, ast.Constant) or not isinstance(decision.value, str):
        raise fail(f"{name} decision must be a literal string")
    if decision.value not in accepted:
        raise fail(f"{name} decision must be {', '.join(accepted[:-1])}, or {accepted[-1]}")
    return name, decision.value


def strip_unmanaged_allow_rules(text: str) -> str:
    """Never persist allow rules: an allow prefix_rule executes commands
    unsandboxed on the host, an allow network_rule widens network access.

    Only top-level direct calls are stripped; the check fails closed on any
    other form, which a human must remove."""
    if not text.strip():
        return text
    try:
        document = ast.parse(text)
    except SyntaxError as exc:
        raise ExecpolicyError(
            f"invalid exec policy: existing Codex execpolicy is not valid "
            f"Starlark/Python syntax ({exc.msg})",
            exc.lineno or 1,
        ) from exc

    lines = text.splitlines(keepends=True)
    encoded_lines = [line.encode("utf-8") for line in lines]

    managed_span = managed_block_span(text, EXECPOLICY_START, EXECPOLICY_END)
    managed_lines = (
        (
            text[: managed_span[0]].count("\n"),
            text[: managed_span[1]].count("\n"),
        )
        if managed_span is not None
        else None
    )
    removals: list[tuple[int, int]] = []
    for node in document.body:
        rule = _execpolicy_rule_decision(node)
        if rule is None:
            continue
        name, decision = rule
        start_line = node.lineno - 1
        end_line = (node.end_lineno or node.lineno) - 1
        end_column = node.end_col_offset
        if end_column is None:
            raise ExecpolicyError(f"{name} has no source end position", node.lineno)
        if (
            managed_lines is not None
            and managed_lines[0] <= start_line <= managed_lines[1]
        ):
            continue
        if decision == "allow":
            before = encoded_lines[start_line][: node.col_offset]
            after = encoded_lines[end_line][end_column:]
            after_content = after.lstrip()
            if before.strip() or (
                after_content and not after_content.startswith(b"#")
            ):
                raise ExecpolicyError(f"allow {name} must occupy its own lines", node.lineno)
            removals.append((start_line, end_line + 1))
    for start_line, end_line in reversed(removals):
        del lines[start_line:end_line]
    return "".join(lines)


def render_execpolicy_text(text: str, permissions: dict[str, Any]) -> str:
    text = strip_unmanaged_allow_rules(text)
    deny_rules, _ = codex_bash_deny_rules(permissions)
    block = "\n".join([EXECPOLICY_START, *deny_rules, EXECPOLICY_END])
    if EXECPOLICY_START in text and EXECPOLICY_END in text:
        return generated_block(text, EXECPOLICY_START, EXECPOLICY_END, block)
    prefix = text.rstrip()
    return ((prefix + "\n\n") if prefix else "") + block + "\n"


def render_hooks(
    hooks: dict[str, Any],
    wrapper_files: dict[Path, str] | None = None,
    *,
    hook_adapter_text: str | None = None,
    hook_fanout_text: str | None = None,
) -> dict[str, Any]:
    unclassified = sorted(
        set(hooks) - CODEX_SUPPORTED_HOOK_EVENTS - CODEX_UNSUPPORTED_HOOK_EVENTS
    )
    if unclassified:
        raise ValueError(f"unclassified hook event {', '.join(unclassified)}")
    if hook_adapter_text is None:
        hook_adapter_text = (
            REPO / "scripts" / "codex_hook_output_adapter.py"
        ).read_text(encoding="utf-8")
    if hook_fanout_text is None:
        hook_fanout_text = (REPO / "scripts" / "codex_hook_fanout.py").read_text(
            encoding="utf-8"
        )
    runtime_adapter, runtime_fanout = hook_runtime_paths(
        hook_adapter_text, hook_fanout_text
    )
    rendered: dict[str, Any] = {}
    for event, source_entries in hooks.items():
        if event not in CODEX_SUPPORTED_HOOK_EVENTS:
            continue
        entries = json.loads(json.dumps(source_entries))
        if not isinstance(entries, list):
            rendered[event] = entries
            continue
        handlers: list[dict[str, Any]] = []
        aggregate_timeout: float = 0
        status_message: str | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher")
            if (
                event in {"PreToolUse", "PostToolUse"}
                and matcher_targets_only_claude_shell(matcher)
            ):
                continue
            if (
                event in {"UserPromptSubmit", "Stop"}
                or matcher is None
                or matcher == ""
                or matcher == "*"
            ):
                matcher = None
            elif isinstance(matcher, str):
                matcher = normalize_hook_tool_name(matcher)
            else:
                matcher = None
            for hook in entry.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                action_type = hook.get("type")
                if action_type != "command":
                    raise ValueError(f"unsupported hook action type {action_type}")
                command = hook.get("command")
                if not isinstance(command, str):
                    raise ValueError("command hook action requires a string command")
                command = normalize_hook_tool_name(command)
                asynchronous = hook.get("async", False) is True
                wrapper_text = hook_wrapper_text(command, asynchronous)
                wrapper = hook_wrapper_path(event, wrapper_text)
                if wrapper_files is not None:
                    wrapper_files[wrapper] = wrapper_text
                timeout = hook.get("timeout")
                if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                    timeout = 1 if event == "SessionEnd" else 600
                elif event == "SessionEnd":
                    timeout = min(max(timeout, 1), 3)
                else:
                    timeout = max(timeout, 1)
                aggregate_timeout = max(aggregate_timeout, timeout)
                if status_message is None and isinstance(hook.get("statusMessage"), str):
                    status_message = hook["statusMessage"]
                handlers.append(
                    {
                        "matcher": matcher,
                        "wrapper": str(wrapper),
                        "timeout": timeout,
                        "adapt_file_tools": (
                            event in {"PreToolUse", "PostToolUse"}
                            and matcher_targets_file_tools(matcher)
                        ),
                    }
                )
        if not handlers:
            continue
        plan = {"handlers": handlers}
        plan_path, dispatcher = hook_fanout_paths(event, plan, runtime_fanout)
        if wrapper_files is not None:
            wrapper_files[plan_path] = dump_json(plan)
            wrapper_files[dispatcher] = hook_fanout_wrapper_text(
                event, plan_path, runtime_fanout
            )
        action: dict[str, Any] = {
            "type": "command",
            "command": adapt_hook_command(
                event, dispatcher, output_adapter_path=runtime_adapter
            ),
            "timeout": aggregate_timeout,
        }
        if event in CODEX_CONTEXT_HOOK_EVENTS:
            action["additionalContextLimit"] = 0
        if status_message is not None:
            action["statusMessage"] = status_message
        rendered[event] = [{"hooks": [action]}]
    if wrapper_files is not None and rendered:
        wrapper_files[runtime_adapter] = hook_adapter_text
        wrapper_files[runtime_fanout] = hook_fanout_text
    return {
        "description": "Hooks mirrored from Claude Code by the codex/ renderer in claude-config.",
        "hooks": rendered,
    }


def codex_mcp_table(name: str, cfg: dict[str, Any]) -> list[str]:
    cfg = dict(cfg)
    if name == "aggregator":
        env = dict(cfg.get("env") or {})
        env.setdefault("FASTMCP_LOG_ENABLED", "false")
        cfg["env"] = env
    lines = [f"[mcp_servers.{json.dumps(name)[1:-1]}]"]
    ctype = cfg.get("type")
    if ctype in {"http", "streamable-http", "sse"}:
        if "url" in cfg:
            lines.append(f"url = {toml_scalar(cfg['url'])}")
        if ctype != "http":
            lines.append(f"type = {toml_scalar(ctype)}")
    else:
        if "command" in cfg:
            lines.append(f"command = {toml_scalar(cfg['command'])}")
        if cfg.get("args"):
            lines.append(f"args = {toml_array(cfg['args'])}")
    for key in ("required",):
        if key in cfg:
            if not isinstance(cfg[key], bool):
                raise ValueError(f"{name}: {key} must be a boolean")
            lines.append(f"{key} = {toml_scalar(cfg[key])}")
    for key in ("enabled_tools", "disabled_tools"):
        if key in cfg:
            value = cfg[key]
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(item, str) and item for item in value)
            ):
                raise ValueError(f"{name}: {key} must be a non-empty string list")
            lines.append(f"{key} = {toml_array(value)}")
    if "default_tools_approval_mode" in cfg:
        value = cfg["default_tools_approval_mode"]
        if value not in {"approve", "prompt"}:
            raise ValueError(
                f"{name}: default_tools_approval_mode must be approve or prompt"
            )
        lines.append(f"default_tools_approval_mode = {toml_scalar(value)}")
    for key in ("startup_timeout_sec", "tool_timeout_sec"):
        if key in cfg:
            value = cfg[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name}: {key} must be a positive number")
            lines.append(f"{key} = {toml_scalar(value)}")
    if "timeout" in cfg:
        timeout = cfg["timeout"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ValueError(f"{name}: timeout must be a positive number of milliseconds")
        seconds = timeout / 1000
        lines.append(
            f"tool_timeout_sec = {toml_scalar(int(seconds) if seconds.is_integer() else seconds)}"
        )
    env = cfg.get("env") or {}
    if isinstance(env, dict) and env:
        forward: list[str] = []
        literal: dict[str, Any] = {}
        for key, value in env.items():
            if key == "WORKSPACE_MCP_CREDENTIALS_DIR":
                literal[key] = value
            elif value in ("", None) or any(part in key.upper() for part in SECRET_KEY_PARTS):
                forward.append(key)
            else:
                literal[key] = value
        if forward:
            lines.append(f"env_vars = {toml_array(sorted(forward))}")
        if literal:
            lines.append("[mcp_servers.%s.env]" % json.dumps(name)[1:-1])
            for key in sorted(literal):
                lines.append(f"{key} = {toml_scalar(literal[key])}")
    tools = cfg.get("tools") or {}
    if not isinstance(tools, dict):
        raise ValueError(f"{name}: tools must be a table of per-tool settings")
    for tool in sorted(tools):
        settings = tools[tool]
        if not isinstance(tool, str) or not MCP_TOOL_NAME.fullmatch(tool):
            raise ValueError(f"{name}: unsupported MCP tool name {tool!r}")
        if (
            not isinstance(settings, dict)
            or set(settings) != {"approval_mode"}
            or settings["approval_mode"] not in {"approve", "prompt"}
        ):
            raise ValueError(
                f"{name}.{tool}: per-tool settings must be approval_mode approve or prompt"
            )
        lines.append(f"[mcp_servers.{json.dumps(name)[1:-1]}.tools.{tool}]")
        lines.append(f"approval_mode = {toml_scalar(settings['approval_mode'])}")
    return lines


# Codex keys per-tool tables by the bare MCP tool name.
MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+")


def codex_mcp_config(
    name: str, cfg: dict[str, Any], permissions: dict[str, Any]
) -> dict[str, Any]:
    rendered = dict(cfg)
    allow = permissions.get("allow") or []
    fully_allowed = any(
        rule in {f"mcp__{name}", f"mcp__{name}__*"}
        for rule in allow
        if isinstance(rule, str)
    )
    server_prefix = f"mcp__{name}"
    higher_priority_rules = [
        rule
        for bucket in (permissions.get("deny") or [], permissions.get("ask") or [])
        for rule in bucket
        if isinstance(rule, str)
        and (rule == server_prefix or rule.startswith(f"{server_prefix}__"))
    ]
    if fully_allowed and not higher_priority_rules:
        rendered.setdefault("default_tools_approval_mode", "approve")
        return rendered
    # Claude allows an exact `mcp__<server>__<tool>` rule without a prompt.
    # Codex's equivalent is a per-tool approval_mode. A server-wide or
    # same-tool ask/deny rule outranks the allow in Claude, so it does here.
    if any(rule in {server_prefix, f"{server_prefix}__*"} for rule in higher_priority_rules):
        return rendered
    tool_prefix = f"{server_prefix}__"
    approved = {
        rule[len(tool_prefix):]
        for rule in allow
        if isinstance(rule, str)
        and rule.startswith(tool_prefix)
        and MCP_TOOL_NAME.fullmatch(rule[len(tool_prefix):])
        and rule not in higher_priority_rules
    }
    if approved:
        tools = dict(rendered.get("tools") or {})
        for tool in sorted(approved):
            tools.setdefault(tool, {"approval_mode": "approve"})
        rendered["tools"] = tools
    return rendered


def validate_tree_readable(root: Path) -> None:
    for dir_name, dir_names, file_names in os.walk(root):
        ignored = ignore_generated(dir_name, [*dir_names, *file_names])
        dir_names[:] = [name for name in dir_names if name not in ignored]
        for name in file_names:
            if name not in ignored:
                (Path(dir_name) / name).read_bytes()


# (source dir under mcps/, Codex server key, label, policy section, table)
OPTIONAL_BROKERS: tuple[tuple[str, str, str, str, dict[str, Any]], ...] = (
    (
        "smarthome-ssh",
        "smarthome_ssh",
        "smarthome SSH broker",
        "smarthomeSsh",
        {
            "enabled_tools": ["ssh"],
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 10,
            "tool_timeout_sec": 600,
        },
    ),
    # Runs one old/new agent-config e2e campaign outside the no-network
    # sandbox. The fetch of origin needs the SSH agent; the harness and the
    # agent under test never receive it. Campaigns run up to an hour.
    (
        "agent-e2e-broker",
        "agent_e2e_broker",
        "agent-e2e broker",
        "agentE2eBroker",
        {
            "enabled_tools": ["run"],
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 10,
            "tool_timeout_sec": 3900,
            "env": {"SSH_AUTH_SOCK": ""},
        },
    ),
)

def rewrite_git_broker_server_arg(
    registration: dict[str, Any], source: Path, destination: Path
) -> list[Any]:
    """Point the shared broker registration at Codex's copied entrypoint."""
    args = registration.get("args")
    if not isinstance(args, list) or str(source) not in args:
        raise ValueError(
            "Claude MCP server 'git_broker' args must reference its shared "
            f"server entrypoint: {source}"
        )
    return [str(destination) if arg == str(source) else arg for arg in args]


def github_child_name(owner: str | None) -> str:
    """The Codex server key for one GitHub installation; None is the default."""
    return GITHUB_MCP_NAME if owner is None else f"{GITHUB_MCP_NAME}_{owner.lower()}"


def rendered_github_names(rendered: dict[str, Any]) -> list[str]:
    """Every GitHub server in a rendered mapping, default child first.

    The set is generated from the mux config, so a checker that hardcodes it
    stops noticing when a child is deleted or renamed. Ask the rendering.
    """
    prefix = f"{GITHUB_MCP_NAME}_"
    children = sorted(name for name in rendered if name.startswith(prefix))
    return [GITHUB_MCP_NAME, *children] if GITHUB_MCP_NAME in rendered else children


def github_children_from_mux(config: object) -> dict[str, dict[str, Any]]:
    """Codex's GitHub registrations, derived from Claude's mux config.

    Claude reaches both GitHub App installations through one multiplexer that
    spawns a child per installation. Codex cannot run that multiplexer: it is a
    `uv run --script` entry point, and uv insists on taking a lock inside
    ~/.cache/uv, which the managed `repos_dev` profile mounts read-only. Measured
    under srt with a fully warm cache and no network:

        error: Could not acquire lock
          Caused by: Read-only file system (os error 30)
                     at path "$HOME/.cache/uv/.tmp1wymo9"

    So Codex gets what the mux would have spawned: one direct child per
    installation, built from the same `command` and `sharedArgs` the mux uses, so
    the exclusion floor is the single source of truth for both agents. This is
    what Codex ran before the mux existed, which is why it is not a capability
    change for Codex -- only a change of where the arguments come from.

    What Codex does not get this way is the mux's own request-level gating, the
    pull-request base guard included. Routing Codex through the mux needs a
    uv-free launcher, which belongs in nixos-config.
    """
    if not isinstance(config, dict):
        raise ValueError("GitHub mux config must be a JSON object")
    command = config.get("command")
    shared = config.get("sharedArgs")
    children = config.get("ownerChildren")
    if not isinstance(command, str) or not command:
        raise ValueError("GitHub mux config: command must be a non-empty string")
    if not isinstance(shared, list) or not all(isinstance(arg, str) for arg in shared):
        raise ValueError("GitHub mux config: sharedArgs must be a list of strings")
    if not isinstance(children, dict):
        raise ValueError("GitHub mux config: ownerChildren must be an object")
    rendered: dict[str, dict[str, Any]] = {
        github_child_name(None): {"command": command, "args": list(shared)}
    }
    for owner, args in children.items():
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(
                f"GitHub mux config: ownerChildren[{owner}] must be a list of strings"
            )
        name = github_child_name(owner)
        if not CODEX_MCP_NAME_RE.match(name):
            raise ValueError(f"GitHub mux config: {owner!r} is not a usable child name")
        rendered[name] = {"command": command, "args": [*args, *shared]}
    return rendered


def validate_github_exclusion_floor(name: str, config: object) -> None:
    """Require Claude's server-side exclusions to cover Codex's security floor."""
    args = config.get("sharedArgs") if isinstance(config, dict) else None
    args = args if isinstance(args, list) else []
    if args.count("--exclude-tools") != 1:
        raise ValueError(
            f"Claude MCP server {name!r} must set exactly one --exclude-tools in "
            "~/.claude/mcps/github-mux/config.json sharedArgs"
        )
    try:
        excluded = set(args[args.index("--exclude-tools") + 1].split(","))
    except (AttributeError, ValueError, IndexError) as exc:
        raise ValueError(
            f"Claude MCP server {name!r} must set --exclude-tools in "
            "~/.claude/mcps/github-mux/config.json sharedArgs"
        ) from exc
    missing = [tool for tool in GITHUB_MCP_EXCLUDED_TOOLS if tool not in excluded]
    if missing:
        raise ValueError(
            f"Claude MCP server {name!r} --exclude-tools is below the security "
            f"floor; add: {', '.join(missing)}"
        )


def prepare_mcp_assets(
    mcp: dict[str, Any],
    *,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
) -> tuple[dict[str, Any], list[tuple[Path, Path]]]:
    """Overlay Codex policy onto required shared MCP registrations."""
    claude_mcps = (claude_root or CLAUDE) / "mcps"
    codex_mcps = (codex_root or CODEX) / "mcps"
    rendered = json.loads(json.dumps(mcp))
    for name in ("git_broker", GITHUB_MCP_NAME):
        if name not in rendered:
            raise ValueError(
                f"Claude MCP server {name!r} is missing; register it in "
                "~/.claude/mcp-servers.json"
            )
    git_registration = rendered["git_broker"]
    src = claude_mcps / "git-broker"
    dst = codex_mcps / "git-broker"
    if not src.is_dir():
        raise ValueError(f"Codex Git broker source must be a directory: {src}")
    validate_tree_readable(src)
    server = src / "server.py"
    policy = src / "config.json"
    if not server.is_file():
        raise ValueError(f"Codex Git broker server is missing: {server}")
    try:
        policy_data = json.loads(policy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Codex Git broker policy is unreadable or invalid: {policy}") from exc
    git_policy = policy_data.get("gitBroker") if isinstance(policy_data, dict) else None
    if not isinstance(git_policy, dict) or not git_policy:
        raise ValueError("Codex Git broker policy must contain a non-empty gitBroker object")

    git_env = dict(git_registration.get("env", {}))
    git_env.setdefault("DCG_PATH", str(PER_USER_PROFILE_BIN / "dcg"))
    # An empty value forwards the host agent; SSH remotes need it for push.
    git_env.setdefault("SSH_AUTH_SOCK", "")
    git_registration.pop("timeout", None)
    git_registration.update(
        {
            "args": rewrite_git_broker_server_arg(
                git_registration, server, dst / "server.py"
            ),
            "required": True,
            # The broker serves "git" and "github"; "exec" is the escape hatch
            # and stays off. Omitting "github" here left Codex with no way to
            # watch a PR or run, update a PR branch, rerun failed jobs or read
            # rulesets, since github-app-mcp-server excludes
            # update_pull_request_branch and actions_run_trigger.
            "enabled_tools": ["git", "github"],
            "disabled_tools": ["exec"],
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 10,
            "tool_timeout_sec": 600,
            "env": git_env,
        }
    )
    assets = [(src, dst)]

    github_src = claude_mcps / "github-mux"
    github_config = github_src / "config.json"
    if not github_src.is_dir():
        raise ValueError(f"Codex GitHub mux source must be a directory: {github_src}")
    validate_tree_readable(github_src)
    try:
        github_config_data = json.loads(github_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Codex GitHub mux config is unreadable or invalid: {github_config}"
        ) from exc
    validate_github_exclusion_floor(GITHUB_MCP_NAME, github_config_data)
    rendered.pop(GITHUB_MCP_NAME, None)
    for name, child in github_children_from_mux(github_config_data).items():
        rendered[name] = {
            **child,
            "required": False,
            "disabled_tools": list(GITHUB_MCP_EXCLUDED_TOOLS),
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 600,
        }

    # Optional brokers ship only when their source exists; Codex may start
    # without them.
    for source_name, key, label, section, table in OPTIONAL_BROKERS:
        src_dir = claude_mcps / source_name
        dst_dir = codex_mcps / source_name
        if not src_dir.exists():
            continue
        if not src_dir.is_dir():
            raise ValueError(f"Codex {label} source must be a directory: {src_dir}")
        validate_tree_readable(src_dir)
        if not (src_dir / "server.py").is_file():
            raise ValueError(f"Codex {label} server is missing: {src_dir / 'server.py'}")
        policy_path = src_dir / "config.json"
        try:
            data = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Codex {label} policy is unreadable or invalid: {policy_path}"
            ) from exc
        body = data.get(section) if isinstance(data, dict) else None
        if not isinstance(body, dict) or not body:
            raise ValueError(f"Codex {label} policy must contain a non-empty {section} object")
        rendered[key] = {
            "command": "python3",
            "args": [str(dst_dir / "server.py")],
            "required": False,
            **json.loads(json.dumps(table)),
        }
        assets.append((src_dir, dst_dir))
    return rendered, assets


def mirror_mcp_assets(mcp: dict[str, Any]) -> dict[str, Any]:
    rendered, assets = prepare_mcp_assets(mcp)
    if assets:
        publish_mcp_assets(assets)
    return rendered


def publish_mcp_assets(assets: list[tuple[Path, Path]]) -> None:
    """Replace each destination with a copy of its source. What else sits
    under the MCP root is unplanned, the walker's to prune."""
    for src, dst in assets:
        put_tree(src, dst)


def codex_permissions_block(
    permissions: dict[str, Any],
    settings: dict[str, Any] | None = None,
    hooks: dict[str, Any] | None = None,
) -> list[str]:
    # Keep source validation strict even where Codex uses a stronger,
    # client-specific profile instead of translating Claude fields literally.
    codex_sandbox_mode(settings)
    codex_writable_roots(permissions)
    lines = [
        f"default_permissions = {toml_scalar(CODEX_PERMISSION_PROFILE)}",
        f"approval_policy = {toml_scalar(codex_approval_policy(permissions))}",
    ]
    effort = codex_reasoning_effort(settings)
    if effort is not None:
        lines.append(f"model_reasoning_effort = {toml_scalar(effort)}")
    web_search = codex_web_search_mode(permissions)
    if web_search is not None:
        lines.append(f"web_search = {toml_scalar(web_search)}")
    lines.append("")
    if hooks is not None:
        lines.extend(["[hooks.state]", ""])
        for key, digest in codex_hook_states(hooks):
            lines.extend(
                [
                    f"[hooks.state.{toml_string(key)}]",
                    f"trusted_hash = {toml_scalar(digest)}",
                    "",
                ]
            )
    return lines


def codex_plugin_state_block(plugins: list[dict[str, Any]]) -> list[str]:
    plugin_ids: set[str] = set()
    for index, plugin in enumerate(plugins):
        plugin_id = plugin.get("id") if isinstance(plugin, dict) else None
        if not isinstance(plugin_id, str) or not plugin_id:
            raise ValueError(f"plugin state entry {index} must have a non-empty id")
        if plugin_id in plugin_ids:
            raise ValueError(f"duplicate plugin state id: {plugin_id}")
        plugin_ids.add(plugin_id)

    lines: list[str] = []
    for plugin in sorted(plugins, key=lambda item: item["id"]):
        if plugin.get("enabled") is not False:
            continue
        lines.extend(
            [
                f"[plugins.{toml_string(plugin['id'])}]",
                "enabled = false",
                "",
            ]
        )
    return lines


def codex_apps_block() -> str:
    return "\n".join(
        [
            f"[apps.{BUILTIN_GITHUB_CONNECTOR_APP_ID}]",
            "enabled = false",
        ]
    )


def managed_block_span(text: str, start: str, end: str) -> tuple[int, int] | None:
    start_matches = list(re.finditer(rf"(?m)^{re.escape(start)}\r?$", text))
    end_matches = list(re.finditer(rf"(?m)^{re.escape(end)}\r?$", text))
    if not start_matches and not end_matches:
        return None

    def line(match: re.Match[str]) -> int:
        return text.count("\n", 0, match.start()) + 1

    # Once a marker is lost or doubled, nothing says where the generated
    # rules end and the user's begin: guessing would either drop a user rule
    # (which may be a deny) or keep stale generated rules forever.
    if not end_matches:
        raise ExecpolicyError(
            f"managed block has {start!r} but no {end!r}; restore it after the "
            "generated rules, or delete the marker and the generated rules below it",
            line(start_matches[0]),
        )
    if not start_matches:
        raise ExecpolicyError(
            f"managed block has {end!r} but no {start!r}; restore it before the "
            "generated rules, or delete the marker and the generated rules above it",
            line(end_matches[0]),
        )
    for marker, matches in ((start, start_matches), (end, end_matches)):
        if len(matches) > 1:
            raise ExecpolicyError(
                f"managed block marker {marker!r} appears {len(matches)} times; keep one "
                "block of generated rules between one pair of markers",
                line(matches[1]),
            )
    start_match = start_matches[0]
    end_match = end_matches[0]
    if start_match.end() > end_match.start():
        raise ExecpolicyError(
            f"managed block marker {end!r} comes before {start!r}; put the generated "
            "rules between them in that order",
            line(end_match),
        )
    return start_match.start(), end_match.end()


def codex_privacy_block(settings: dict[str, Any]) -> str:
    env = settings.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("Claude env settings must be an object")
    lines: list[str] = []
    if str(env.get("DISABLE_TELEMETRY", "")) == "1":
        lines.extend(["[analytics]", "enabled = false", ""])
    if str(env.get("CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY", "")) == "1":
        lines.extend(["[feedback]", "enabled = false", ""])
    return "\n".join(lines)


def codex_agents_text(claude_text: str) -> str:
    return CODEX_AGENTS_PREAMBLE + claude_text


# Keys Codex itself writes into config.toml at runtime. Everything else in the
# file is regenerated from the Claude manifest on every sync. A generated value
# for one of these keys looks like runtime state once its source is removed and
# stays until Codex or the user changes it: nothing marks who wrote it.
CODEX_RUNTIME_KEYS = (
    "model",
    "personality",
    "service_tier",
    "model_reasoning_effort",
    "projects",
    "notice",
    "apps",
    "tui",
    "windows_wsl_setup_acknowledged",
)
CODEX_CONFIG_HEADER = (
    "# Generated by ai-client-config-codex-sync from the Claude config; hand edits\n"
    "# are overwritten. Kept from the previous file (written by Codex at runtime):\n"
    f"# {', '.join(CODEX_RUNTIME_KEYS)}.\n"
)
BARE_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")


def toml_key(key: str) -> str:
    return key if BARE_TOML_KEY.fullmatch(key) else toml_string(key)


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, dict):
        items = (f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items())
        return "{" + ", ".join(items) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return toml_scalar(value)


def toml_document(data: dict[str, Any]) -> str:
    lines: list[str] = []

    def table(path: list[str], node: dict[str, Any]) -> None:
        leaves = {k: v for k, v in node.items() if not isinstance(v, dict)}
        if path and (leaves or not node):
            lines.extend(["", "[" + ".".join(map(toml_key, path)) + "]"])
        lines.extend(f"{toml_key(k)} = {toml_value(v)}" for k, v in leaves.items())
        for key, value in node.items():
            if isinstance(value, dict):
                table([*path, key], value)

    table([], data)
    return "\n".join(lines).lstrip("\n") + "\n"


def fill_missing(target: dict[str, Any], source: dict[str, Any]) -> None:
    # One level deep only: a generated entry such as apps.<id> replaces the
    # live entry whole instead of absorbing its extra keys.
    for key, value in source.items():
        if key not in target:
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            for child, child_value in value.items():
                target[key].setdefault(child, child_value)


def overwritten_paths(
    live: dict[str, Any], config: dict[str, Any], prefix: str = ""
) -> list[str]:
    paths: list[str] = []
    for key, value in live.items():
        path = prefix + toml_key(key)
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            paths.extend(overwritten_paths(value, config[key], path + "."))
        elif key not in config or toml_value(config[key]) != toml_value(value):
            paths.append(path)
    return paths


def parse_live_config(text: str) -> dict[str, Any]:
    try:
        live = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            "cannot carry Codex runtime state over from invalid config.toml"
        ) from exc
    return live


def render_codex_config_text(
    text: str,
    mcp: dict[str, Any],
    permissions: dict[str, Any],
    settings: dict[str, Any],
    hooks: dict[str, Any],
    plugins: list[dict[str, Any]],
    agents: list[dict[str, Any]] | None = None,
    *,
    announce: bool = False,
) -> str:
    fragments = [
        "\n".join(codex_permissions_block(permissions, settings, hooks)),
        "[features]\nhooks = true",
        "\n".join(codex_plugin_state_block(plugins)),
        codex_agent_roles_block(agents or []),
        codex_privacy_block(settings),
        codex_apps_block(),
        *(
            "\n".join(
                codex_mcp_table(name, codex_mcp_config(name, mcp[name], permissions))
            )
            for name in sorted(mcp)
        ),
    ]
    config = tomllib.loads("\n\n".join(fragments))
    live = parse_live_config(text)
    fill_missing(config, {k: live[k] for k in CODEX_RUNTIME_KEYS if k in live})
    overwritten = overwritten_paths(live, config)
    if announce and overwritten:
        print(
            "config.toml: replacing live values the Claude config does not produce: "
            + ", ".join(overwritten),
            file=sys.stderr,
        )
    body = toml_document(config)
    rendered = CODEX_CONFIG_HEADER + "\n" + body
    # Compared as emitted text, not dicts: nan != nan.
    if toml_document(tomllib.loads(rendered)) != body:
        raise ValueError("generated Codex config does not round-trip through TOML")
    return rendered


def update_execpolicy(
    permissions: dict[str, Any], prepared_text: str | None = None
) -> None:
    path = CODEX / "rules" / "default.rules"
    text = merge_input_text(path) or ""
    put_file(
        path,
        prepared_text
        if prepared_text is not None
        else render_execpolicy_text(text, permissions),
    )


def stable_hook_script_files(plan: RenderPlan) -> dict[Path, str]:
    """The hook scripts at stable paths, for sessions started against an
    older hooks.json; rendered hooks run content-addressed runtime copies.
    They hold the repository's copy, as the production publish renders them
    from scratch: rewritten whenever that differs, not only when missing."""
    return {
        HOOK_OUTPUT_ADAPTER: plan.hook_adapter_text,
        HOOK_FANOUT: plan.hook_fanout_text,
    }


def merge_input_text(path: Path) -> str | None:
    """A merge input's text, or None when no regular file stands there:
    the production wrapper, and sync_codex.seed_stage, carry one into the
    stage only if it is a file, and anything else at the path is the
    mirror's to replace."""
    on_disk = located(path)
    return on_disk.read_text(encoding="utf-8") if on_disk.is_file() else None


def render_marketplace_text() -> str:
    """The marketplace with the mirror's own entry rebuilt, the rest kept.

    Other plugins are listed here too, and the production publish keeps no
    backup of this merge input, so a file the sync cannot read is a human's
    to fix: the sync rebuilds only its own entry, never the whole file.
    """
    path = AGENTS_HOME / "plugins" / "marketplace.json"
    advice = "a human must fix or remove it (the sync then re-adds its own entry)"
    text = merge_input_text(path)
    try:
        data = (
            json.loads(text)
            if text is not None
            else {"name": "local", "interface": {"displayName": "Local"}, "plugins": []}
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path}: invalid JSON at line {exc.lineno} column {exc.colno}: "
            f"{exc.msg}; {advice}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: must be a JSON object; {advice}")
    if not isinstance(data.setdefault("interface", {}), dict):
        raise ValueError(f"{path}: interface must be an object; {advice}")
    existing = data.get("plugins", [])
    if not isinstance(existing, list) or not all(isinstance(p, dict) for p in existing):
        raise ValueError(f"{path}: plugins must be a list of objects; {advice}")
    data.setdefault("name", "local")
    data["interface"].setdefault("displayName", "Local")
    plugins = [p for p in existing if p.get("name") != PLUGIN_NAME]
    plugins.append(
        {
            "name": PLUGIN_NAME,
            "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
            "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_USE"},
            "category": "Productivity",
        }
    )
    data["plugins"] = plugins
    return dump_json(data)


def update_marketplace(prepared_text: str | None = None) -> None:
    path = AGENTS_HOME / "plugins" / "marketplace.json"
    put_file(path, prepared_text if prepared_text is not None else render_marketplace_text())


def prepare_plugin_files(
    manifest: dict[str, Any], mcp: dict[str, Any], hooks: dict[str, Any]
) -> dict[Path, str]:
    return {
        PLUGIN_ROOT / ".codex-plugin" / "plugin.json": dump_json(
            {
                "name": PLUGIN_NAME,
                "version": "1.0.0",
                "description": "Codex mirror of behavior-bearing local Claude Code setup.",
                "author": {"name": "jonathan"},
                "skills": "./skills/",
                "hooks": "./hooks.json",
                "mcpServers": "./.mcp.json",
                "interface": {
                    "displayName": "Claude Mirror",
                    "shortDescription": "Mirrored Claude skills, hooks, agents, and MCP.",
                    "longDescription": "Local compatibility plugin generated by the codex/ renderer in claude-config without importing chat history, caches, auth, or task state.",
                    "developerName": "jonathan",
                    "category": "Productivity",
                    "capabilities": ["Skills", "MCP", "Hooks"],
                    "brandColor": "#3B82F6",
                },
            }
        ),
        PLUGIN_ROOT / ".mcp.json": dump_json({"mcpServers": mcp}),
        PLUGIN_ROOT / "hooks.json": dump_json(hooks),
    }


def render_plugin(
    manifest: dict[str, Any],
    mcp: dict[str, Any],
    hooks: dict[str, Any],
    prepared_files: dict[Path, str] | None = None,
) -> None:
    for path, text in (
        prepared_files
        if prepared_files is not None
        else prepare_plugin_files(manifest, mcp, hooks)
    ).items():
        put_file(path, text)


AGENT_MUTATING_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"})


def markdown_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    return text.strip()


def codex_agent_body(path: Path) -> str:
    return markdown_body(path).replace("CLAUDE.md", "AGENTS.md").replace(
        "Claude Code", "Codex"
    )


def codex_agent_sandbox(agent: dict[str, Any]) -> str | None:
    permission_mode = agent.get("permission_mode")
    tools = set(agent.get("tools") or [])
    if permission_mode == "readOnly" or (tools and tools.isdisjoint(AGENT_MUTATING_TOOLS)):
        return "read-only"
    if permission_mode == "acceptEdits":
        return "workspace-write"
    return None


def agent_role_root() -> Path:
    return CODEX / "agents" / "claude-imported"


MIRRORED_PLUGIN_PREFIX = "claude-plugin-"


def execpolicy_root() -> Path:
    return CODEX / "rules"


def mirrored_plugin_root() -> Path:
    return PLUGIN_ROOT / "mirrored-plugins"


def agent_role_path(name: str) -> Path:
    return agent_role_root() / f"{name}.toml"


def agent_reference_path(root: Path, name: str) -> Path:
    # Keyed by the agent name, which Claude keeps unique; file basenames are
    # not (a user agent and a plugin agent may both be reviewer.md).
    return root / f"{name}.md"


def prepare_agent_role_files(agents: list[dict[str, Any]]) -> dict[Path, str]:
    files: dict[Path, str] = {}
    for agent in agents:
        name = agent.get("name")
        source = Path(agent.get("path") or "")
        if not isinstance(name, str) or not name or not source.is_file():
            raise ValueError(f"invalid Claude agent source: {agent!r}")
        lines = [
            f"name = {toml_scalar(name)}",
            f"description = {toml_scalar(agent.get('description') or f'Mirrored Claude agent {name}.')}",
            f"developer_instructions = {toml_scalar(codex_agent_body(source))}",
        ]
        sandbox = codex_agent_sandbox(agent)
        if sandbox is not None:
            lines.append(f"sandbox_mode = {toml_scalar(sandbox)}")
        effort = agent.get("effort")
        if effort == "max":
            effort = "xhigh"
        if effort in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            lines.append(f"model_reasoning_effort = {toml_scalar(effort)}")
        rendered = "\n".join(lines) + "\n"
        tomllib.loads(rendered)
        files[agent_role_path(name)] = rendered
    return files


def codex_agent_roles_block(agents: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for agent in sorted(agents, key=lambda item: item["name"]):
        name = agent["name"]
        key = toml_key(name)
        lines.extend(
            [
                f"[agents.{key}]",
                f"description = {toml_scalar(agent.get('description') or f'Mirrored Claude agent {name}.')}",
                f"config_file = {toml_scalar(str(agent_role_path(name)))}",
                "",
            ]
        )
    return "\n".join(lines)


def prepare_references(
    manifest: dict[str, Any],
) -> tuple[list[str], list[tuple[Path, Path]]]:
    agents: list[str] = []
    links: list[tuple[Path, Path]] = []
    roots = [PLUGIN_ROOT / "agents", agent_role_root()]
    for agent in manifest.get("agents", []):
        src = Path(agent["path"])
        if src.exists():
            if not src.is_file():
                raise ValueError(f"agent source must be a file: {src}")
            src.read_bytes()
            links.extend(
                (src, agent_reference_path(root, agent["name"])) for root in roots
            )
            agents.append(agent["name"])
    for plugin in manifest.get("enabled_plugins", []):
        src = Path(plugin.get("path") or "")
        if src.exists():
            if not src.is_dir():
                raise ValueError(f"enabled plugin source must be a directory: {src}")
            validate_tree_readable(src)
            target = MIRRORED_PLUGIN_PREFIX + plugin["id"].replace("@", "-").replace("/", "-")
            links.append((src, mirrored_plugin_root() / target))
    return agents, links


def managed_paths() -> tuple[Path, ...]:
    """Every path the mirror owns, the production publish's managed_paths.

    /etc/nixos/home/ai-client-config-sync.nix renders into a home built from
    scratch and rsyncs each of these over the live one with --delete-after;
    sync_codex.publish_stage copies each from its stage the same way.
    Codex config is a mirror of the Claude config: inside these paths the
    render plan is the whole truth, so anything else there is pruned and
    anything of the wrong type or content replaced, with no backup. Outside
    them (Codex runtime state, installed plugins) nothing is touched.
    """
    return (
        CODEX / "config.toml",
        CODEX / "rules",
        CODEX / "scripts",
        CODEX / "hooks",
        CODEX / "hooks.json",
        CODEX / "mcps",
        LEGACY_PLUGIN_LINK,
        CODEX / "agents",
        REPORT_DIR,
        CODEX / "AGENTS.md",
        CODEX / "skills",
        AGENTS_HOME / "plugins",
        PLUGIN_ROOT,
    )


def never_pruned_paths() -> tuple[Path, ...]:
    """Inside the managed paths, what the production publish never prunes:
    Codex's bundled skills, and the hook generations live sessions may still
    run (the sync adds to them and never removes one)."""
    return (CODEX / "skills" / ".system", HOOK_WRAPPER_ROOT)


def planned_entries(plan: RenderPlan) -> tuple[set[Path], set[Path]]:
    """(leaves, directories) of the plan inside the managed paths.

    A leaf is written whole by one publish step, and what is inside it is
    that step's to compare (a link, a copied MCP tree). The directories are
    the ones the publish creates even when nothing is planned inside them.
    Hook generations sit under a never-pruned path and are not listed.
    """
    leaves = {
        *plan_text_files(plan),
        CODEX / "config.toml",
        LEGACY_PLUGIN_LINK,
        *(dst for _, dst in plan.reference_links),
        *(dst for _, dst in plan.mcp_assets),
        *(dst / "SKILL.md" for dst, _ in plan.skill_wrappers),
    }
    return leaves, {PLUGIN_ROOT / "agents", agent_role_root()}


def unplanned_paths(plan: RenderPlan) -> list[Path]:
    """Everything inside the managed paths the plan does not hold, outermost
    first: what publish_render_plan prunes and render_plan_drift reports.

    The walk never follows a link and never enters a leaf or a never-pruned
    path. What beside_managed_path names counts as inside it.
    """
    leaves, directories = planned_entries(plan)
    kept = set(never_pruned_paths())
    open_ = directories | {
        parent for path in leaves | directories | kept for parent in path.parents
    }
    found: list[Path] = []

    def visit(path: Path) -> None:
        if path in leaves or path in kept:
            return
        on_disk = located(path)
        if path in open_ and on_disk.is_dir() and not on_disk.is_symlink():
            for name in sorted(os.listdir(on_disk)):
                visit(path / name)
        elif on_disk.exists() or on_disk.is_symlink():
            found.append(path)

    for root in managed_paths():
        visit(root)
        found.extend(beside_managed_path(root))
    found.extend(left_in_hook_generations())
    return found


# The name a run killed inside replace_entry (.<name>.swap-*) or write_file
# (.<name>.tmp-*) leaves its holder or temporary file under: the prefix,
# then tempfile's eight random characters.
KILLED_RUN_LEFTOVER = re.compile(r"\..+\.(?:swap|tmp)-[a-z0-9_]{8}")


def left_in_hook_generations() -> list[Path]:
    """What a run killed inside replace_entry or write_file left among the
    hook generations: never pruned, yet the sync writes into them, so the
    walk in unplanned_paths and the publish's prune never reach it there. A
    generation's name never starts with a dot."""
    root = located(HOOK_WRAPPER_ROOT)
    found: list[Path] = []
    if not root.is_dir() or root.is_symlink():
        return found
    for dir_name, dir_names, file_names in os.walk(root):
        for name in [*dir_names, *file_names]:
            if KILLED_RUN_LEFTOVER.fullmatch(name):
                found.append(HOOK_WRAPPER_ROOT / Path(dir_name).relative_to(root) / name)
        dir_names[:] = [name for name in dir_names if not KILLED_RUN_LEFTOVER.fullmatch(name)]
    return found


def beside_managed_path(root: Path) -> list[Path]:
    """What sits beside a managed path but counts as inside it: a *.bak-*,
    as in the production publish, which prunes those too, and the holder a
    run killed inside replace_entry (.<name>.swap-*) or write_file
    (.<name>.tmp-*) left there, perhaps with the original moved aside: a
    publish copying a stage into the home, or a render in place."""
    parent = located(root.parent)
    if not parent.is_dir():
        return []
    prefixes = (f"{root.name}.bak-", f".{root.name}.swap-", f".{root.name}.tmp-")
    return [
        root.parent / name for name in sorted(os.listdir(parent)) if name.startswith(prefixes)
    ]


def publish_references(links: list[tuple[Path, Path]]) -> None:
    for src, dst in links:
        make_room(dst)
        link_or_copy(src, dst)


def render_references(manifest: dict[str, Any]) -> list[str]:
    agents, links = prepare_references(manifest)
    for root in [PLUGIN_ROOT / "agents", CODEX / "agents" / "claude-imported"]:
        mkdir(root)
    publish_references(links)
    return agents


def prepare_report_files(
    manifest: dict[str, Any],
    skills: list[str],
    plugin_skills: list[str],
    skipped: list[dict[str, str]],
    agents: list[str],
    mcp: dict[str, Any],
    support: dict[str, Any],
    budget_mapping: dict[str, Any] | None = None,
) -> dict[Path, str]:
    mcp_lines = ["# Codex MCP servers rendered by the codex/ renderer in claude-config", ""]
    for name in sorted(mcp):
        mcp_lines.extend(codex_mcp_table(name, mcp[name]))
        mcp_lines.append("")
    mcp_text = "\n".join(mcp_lines).rstrip() + "\n"
    # A pure function of the inputs, with no timestamp: production renders
    # into a staged home without the previous report, so a wall-clock stamp
    # made every unchanged hourly run publish a new report.json.
    report = {
        "source": manifest.get("source", {}),
        "source_signature": manifest.get("source_signature", {}),
        "mirrored": {
            "skills": skills,
            "plugin_derived_skills": plugin_skills,
            "total_exposed_skills": len(skills) + len(plugin_skills),
            "command_derived_skills": [
                name for name in skills if "command-" in name
            ],
            "source_commands": [
                command["name"] for command in manifest.get("commands", [])
            ],
            "agents": agents,
            "enabled_plugins": [p["id"] for p in manifest.get("enabled_plugins", [])],
            "mcp_servers": sorted(mcp),
            "hooks_events": sorted((manifest.get("hooks") or {}).keys()),
        },
        "skipped": skipped,
        "known_gaps": [
            "research-agent normal/deep uses Claude first and automatically retries with Codex when Claude's usage limit is exhausted; scanner/provider outages still fail closed.",
            "Claude ConfigChange, CwdChanged, InstructionsLoaded, MessageDisplay, PermissionDenied, PostToolUseFailure, and StopFailure hooks remain in the source manifest and report but are not rendered because Codex 0.146 does not support those events.",
            "Supported Claude commands render as Codex skills; templates that depend on Claude-only argument interpolation or stop-hook behavior remain reported but unexposed.",
            "Claude agents render as native Codex roles, but Claude model identifiers and exact per-tool allowlists have no direct Codex equivalent; read-only source agents are sandboxed read-only.",
            "Codex 0.146 renders hook-provided model context in the TUI; suppressOutput cannot make UserPromptSubmit or SessionStart context model-only.",
            "Claude Bash-only hooks are omitted from Codex: Codex code-mode uses exec with different input/output schemas, and hooks fail open on timeout or error. Codex native permission profiles, the structured Git broker, and execpolicy rules are the enforcement layers; Claude-only deny and output-redaction behavior that cannot be represented remains unsupported.",
        ],
        "permission_translation": {
            "bash_deny_native_rules": len(codex_bash_deny_rules(manifest.get("permissions") or {})[0]),
            "bash_deny_unsupported": codex_bash_deny_rules(manifest.get("permissions") or {})[1],
        },
        "mappings": {
            "skill_listing_budget_fraction": budget_mapping
            if budget_mapping is not None
            else skill_budget_mapping(manifest, support),
        },
    }
    return {
        REPORT_DIR / "mcp.codex.toml": mcp_text,
        REPORT_DIR / "report.json": dump_json(report),
    }


def write_reports(
    manifest: dict[str, Any],
    skills: list[str],
    plugin_skills: list[str],
    skipped: list[dict[str, str]],
    agents: list[str],
    mcp: dict[str, Any],
    support: dict[str, Any],
    budget_mapping: dict[str, Any] | None = None,
    prepared_files: dict[Path, str] | None = None,
) -> None:
    for path, text in (
        prepared_files
        if prepared_files is not None
        else prepare_report_files(
            manifest,
            skills,
            plugin_skills,
            skipped,
            agents,
            mcp,
            support,
            budget_mapping,
        )
    ).items():
        put_file(path, text)


@dataclass
class RenderPlan:
    manifest: dict[str, Any]
    hooks: dict[str, Any]
    mcp: dict[str, Any]
    mcp_assets: list[tuple[Path, Path]]
    skill_wrappers: tuple[tuple[Path, str], ...]
    skills: list[str]
    plugin_skills: list[str]
    skipped: list[dict[str, str]]
    agents: list[str]
    agent_role_files: dict[Path, str]
    reference_links: list[tuple[Path, Path]]
    # (live config text, announce=True) -> rendered config text
    render_config: Callable[..., str]
    execpolicy_text: str
    marketplace_text: str
    hook_adapter_text: str
    hook_fanout_text: str
    hook_wrapper_files: dict[Path, str]
    codex_hooks_text: str
    plugin_files: dict[Path, str]
    report_files: dict[Path, str]
    agents_text: str


def prepare_render_plan(
    manifest: dict[str, Any], support: dict[str, Any]
) -> RenderPlan:
    hook_adapter_text = (REPO / "scripts" / "codex_hook_output_adapter.py").read_text(
        encoding="utf-8"
    )
    hook_fanout_text = (REPO / "scripts" / "codex_hook_fanout.py").read_text(
        encoding="utf-8"
    )
    hook_wrapper_files: dict[Path, str] = {}
    hooks = render_hooks(
        manifest.get("hooks") or {},
        hook_wrapper_files,
        hook_adapter_text=hook_adapter_text,
        hook_fanout_text=hook_fanout_text,
    )
    budget_mapping = skill_budget_mapping(manifest, support)
    plugins = manifest.get("plugins") or []
    mcp, mcp_assets = prepare_mcp_assets(manifest.get("mcp_servers") or {})
    prepared_skills, skills, plugin_skills, skipped = prepare_skills(
        manifest, support
    )
    agents, reference_links = prepare_references(manifest)
    agent_role_files = prepare_agent_role_files(manifest.get("agents") or [])
    config_path = CODEX / "config.toml"
    existing_config = merge_input_text(config_path) or ""

    def render_config(text: str, announce: bool = True) -> str:
        return render_codex_config_text(
            text,
            mcp,
            manifest.get("permissions") or {},
            manifest.get("settings") or {},
            hooks,
            plugins,
            manifest.get("agents") or [],
            announce=announce,
        )

    # Fail before any write; publish_config renders again against the
    # file as it is then.
    render_config(existing_config, announce=False)
    rules_path = CODEX / "rules" / "default.rules"
    existing_rules = merge_input_text(rules_path) or ""
    try:
        execpolicy_text = render_execpolicy_text(
            existing_rules, manifest.get("permissions") or {}
        )
    except ExecpolicyError as exc:
        raise exc.located(rules_path) from exc
    marketplace_text = render_marketplace_text()
    codex_hooks_text = dump_json(hooks)
    plugin_files = prepare_plugin_files(manifest, mcp, hooks)
    report_files = prepare_report_files(
        manifest,
        skills,
        plugin_skills,
        skipped,
        agents,
        mcp,
        support,
        budget_mapping,
    )
    agents_text = codex_agents_text(
        (CLAUDE / "CLAUDE.md").read_text(encoding="utf-8")
    )
    plan = RenderPlan(
        manifest=manifest,
        hooks=hooks,
        mcp=mcp,
        mcp_assets=mcp_assets,
        skill_wrappers=tuple(prepared_skills),
        skills=skills,
        plugin_skills=plugin_skills,
        skipped=skipped,
        agents=agents,
        agent_role_files=agent_role_files,
        reference_links=reference_links,
        render_config=render_config,
        execpolicy_text=execpolicy_text,
        marketplace_text=marketplace_text,
        hook_adapter_text=hook_adapter_text,
        hook_fanout_text=hook_fanout_text,
        hook_wrapper_files=hook_wrapper_files,
        codex_hooks_text=codex_hooks_text,
        plugin_files=plugin_files,
        report_files=report_files,
        agents_text=agents_text,
    )
    # Names in the plan come from Claude config (agents, skills, plugins).
    # Each planned path must be one the mirror owns, checked here so a bad
    # one fails before any write, and the check, which runs this, too.
    leaves, directories = planned_entries(plan)
    for path in sorted(leaves | directories | set(hook_wrapper_files)):
        managed_root(path)
    return plan


def canonical(value: Any) -> str:
    # Order-insensitive and nan-safe: Codex may reorder tables when it saves.
    def sort(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: sort(node[key]) for key in sorted(node)}
        if isinstance(node, list):
            return [sort(item) for item in node]
        return node

    return toml_document(sort(value))


def publish_config(plan: RenderPlan) -> None:
    # Written last but one, and merged against the file as it is right now:
    # Codex writes runtime state into it at any time.
    path = CODEX / "config.toml"
    live = merge_input_text(path) or ""
    rendered = plan.render_config(live)
    # A link is replaced even when it leads to equal content, as the
    # production publish replaces it with the regular file it rsyncs.
    if (
        live
        and not located(path).is_symlink()
        and canonical(parse_live_config(live)) == canonical(tomllib.loads(rendered))
    ):
        return
    put_file(path, rendered)


def publish_render_plan(plan: RenderPlan) -> None:
    """Write the plan where common.located puts the output: into a stage
    for sync_codex, which checks it before anything reaches the home. With
    no stage set it refuses: it would write into the home unchecked."""
    if common.STAGE is None:
        raise RuntimeError(f"render_codex writes only into a stage; run {SYNC}")
    ensure_legacy_plugin_link()
    for path, text in stable_hook_script_files(plan).items():
        put_file(path, text, mode=0o755)
    publish_hook_generation(plan.hook_wrapper_files)
    if plan.mcp_assets:
        publish_mcp_assets(plan.mcp_assets)
    for root in sorted(planned_entries(plan)[1]):
        put_dir(root)
    publish_references(plan.reference_links)
    for path, text in plan.agent_role_files.items():
        put_file(path, text)
    put_file(CODEX / "rules" / "default.rules", plan.execpolicy_text)
    update_marketplace(plan.marketplace_text)
    render_plugin(plan.manifest, plan.mcp, plan.hooks, plan.plugin_files)
    for path, text in plan.report_files.items():
        put_file(path, text)
    put_file(CODEX / "AGENTS.md", plan.agents_text)
    publish_prepared_skills(plan.skill_wrappers)
    publish_config(plan)
    put_file(CODEX / "hooks.json", plan.codex_hooks_text)
    # Last, as the production publish prunes after its transfer: nothing
    # the new entry points name is gone before they are in place.
    for path in unplanned_paths(plan):
        remove_path(path)


# Drift is the inverse of publish_render_plan: each step there has its
# comparison here. A publish step added without one is caught by the round-trip
# test that corrupts and deletes every published file.


def plan_text_files(plan: RenderPlan) -> dict[Path, str]:
    """Files publish_render_plan writes verbatim from the plan."""
    return {
        **stable_hook_script_files(plan),
        **plan.agent_role_files,
        CODEX / "rules" / "default.rules": plan.execpolicy_text,
        AGENTS_HOME / "plugins" / "marketplace.json": plan.marketplace_text,
        **plan.plugin_files,
        **plan.report_files,
        CODEX / "AGENTS.md": plan.agents_text,
        CODEX / "hooks.json": plan.codex_hooks_text,
    }


TREE_DIRECTORY = "directory"
TREE_LINK = "symlink"


def comparable_tree(root: Path, *, source: bool = False) -> dict[str, bytes | str]:
    """Every entry under root by relative path: a file as its bytes, a
    directory as TREE_DIRECTORY, a link as TREE_LINK.

    A source is read the way copytree(root, ..., ignore=ignore_generated)
    reads it: ignored names skipped, symlinks followed at the top and inside,
    since ~/.claude is a deploy projection that may link them. A published
    tree is read whole without following: the copy writes neither a link nor
    an ignored name, so either one there is drift the publish replaces.
    """
    if not root.is_dir() or (root.is_symlink() and not source):
        return {}
    result: dict[str, bytes | str] = {}
    for dir_name, dir_names, file_names in os.walk(root, followlinks=source):
        names = [*dir_names, *file_names]
        ignored = ignore_generated(dir_name, names) if source else set()
        dir_names[:] = [name for name in dir_names if name not in ignored]
        for name in names:
            if name in ignored:
                continue
            path = Path(dir_name) / name
            relative = str(path.relative_to(root))
            if path.is_symlink() and not source:
                result[relative] = TREE_LINK
            elif path.is_dir():
                result[relative] = TREE_DIRECTORY
            else:
                result[relative] = path.read_bytes()
    return result


SYMLINK_FOR_FILE = "a symlink where the render plan writes a regular file"


def file_drift(path: Path, text: str) -> list[str]:
    # put_file replaces a link with a regular file whatever it leads to.
    on_disk = located(path)
    if on_disk.is_symlink():
        return [f"{path}: {SYMLINK_FOR_FILE}"]
    try:
        actual = on_disk.read_bytes()
    except FileNotFoundError:
        return [f"{path}: missing"]
    except OSError as exc:
        return [f"{path}: unreadable: {type(exc).__name__}: {exc}"]
    if actual != text.encode("utf-8"):
        return [f"{path}: differs from the render plan"]
    return []


def config_drift(plan: RenderPlan) -> list[str]:
    # The same no-op test publish_config uses: semantic, order-insensitive.
    path = CODEX / "config.toml"
    on_disk = located(path)
    if on_disk.is_symlink():
        return [f"{path}: {SYMLINK_FOR_FILE}"]
    try:
        live = on_disk.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"{path}: missing"]
    except OSError as exc:
        return [f"{path}: unreadable: {type(exc).__name__}: {exc}"]
    live_config = parse_live_config(live)
    rendered = tomllib.loads(plan.render_config(live, announce=False))
    if canonical(live_config) == canonical(rendered):
        return []
    differing = config_differences(live_config, rendered)
    return [f"{path}: differs from the render plan at {', '.join(differing)}"]


def config_differences(
    left: dict[str, Any], right: dict[str, Any], prefix: str = ""
) -> list[str]:
    """Leaf key paths where two parsed configs differ, for the drift report."""
    paths: list[str] = []
    for key in sorted(left.keys() | right.keys()):
        path = prefix + toml_key(key)
        a, b = left.get(key), right.get(key)
        if isinstance(a, dict) or isinstance(b, dict):
            if (a is None or isinstance(a, dict)) and (b is None or isinstance(b, dict)):
                nested = config_differences(a or {}, b or {}, path + ".")
                # An empty table present on one side only differs as a whole.
                paths.extend(nested or ([path] if a is None or b is None else []))
                continue
        if key not in left or key not in right or toml_value(a) != toml_value(b):
            paths.append(path)
    return paths


def link_drift(src: Path, dst: Path) -> list[str]:
    on_disk = located(dst)
    if on_disk.is_symlink():
        if Path(os.readlink(on_disk)) == src:
            return []
    elif src.is_dir() and on_disk.is_dir():
        # link_or_copy falls back to a copy where symlinks are unavailable.
        if comparable_tree(on_disk) == comparable_tree(src, source=True):
            return []
    elif src.is_file() and on_disk.is_file():
        if on_disk.read_bytes() == src.read_bytes():
            return []
    return [f"{dst}: missing or not a link to {src}"]


def mcp_asset_drift(assets: list[tuple[Path, Path]]) -> list[str]:
    # Each destination is a copy of its source; what sits beside them under
    # the MCP root is the walker's to report (unplanned_paths).
    errors: list[str] = []
    for src, dst in assets:
        expected = comparable_tree(src, source=True)
        actual = comparable_tree(located(dst))
        for rel in sorted(expected.keys() | actual.keys()):
            path = dst / rel
            if rel not in actual:
                errors.append(f"{path}: MCP asset missing")
            elif rel not in expected:
                errors.append(f"{path}: MCP asset not in the render plan")
            elif actual[rel] != expected[rel]:
                errors.append(f"{path}: MCP asset differs from its source")
    return errors


def render_plan_drift(plan: RenderPlan) -> list[str]:
    """Every difference between what publish_render_plan writes and disk,
    read where common.located puts the output."""
    errors: list[str] = []
    link = located(LEGACY_PLUGIN_LINK)
    if LEGACY_PLUGIN_LINK != PLUGIN_ROOT and not (
        link.is_symlink() and Path(os.readlink(link)) == PLUGIN_ROOT
    ):
        errors.append(f"{LEGACY_PLUGIN_LINK}: missing or not a link to {PLUGIN_ROOT}")
    for path, text in plan.hook_wrapper_files.items():
        drift = file_drift(path, text)
        if not drift and path.suffix == ".sh" and located(path).stat().st_mode & 0o7777 != 0o755:
            drift = [f"{path}: hook wrapper must be mode 0755"]
        errors.extend(drift)
    errors.extend(mcp_asset_drift(plan.mcp_assets))
    for src, dst in plan.reference_links:
        errors.extend(link_drift(src, dst))
    errors.extend(f"{path}: not in the render plan" for path in unplanned_paths(plan))
    for path, text in plan_text_files(plan).items():
        errors.extend(file_drift(path, text))
    for dst, text in plan.skill_wrappers:
        errors.extend(file_drift(dst / "SKILL.md", text))
    errors.extend(config_drift(plan))
    return errors


# The one way to publish: import, render into a stage, check it, and copy
# into the home only what passed.
SYNC = REPO / "scripts" / "sync_codex.py"


def load_render_plan() -> RenderPlan:
    manifest = load_json(MANIFEST, {})
    if not manifest:
        # The checked sync, as check.py advises: import, then a render,
        # would publish unchecked.
        raise SystemExit(f"missing manifest; run {SYNC}")
    return prepare_render_plan(manifest, load_support())


def publish(plan: RenderPlan) -> None:
    publish_render_plan(plan)
    print(
        "rendered Codex: "
        f"skills={len(plan.skills)} "
        f"plugin_skills={len(plan.plugin_skills)} "
        f"agents={len(plan.agents)} "
        f"mcp={len(plan.mcp)}"
    )


def main() -> int:
    """Render the manifest where common.located puts the output: the stage
    sync_codex checks before it publishes."""
    publish(load_render_plan())
    return 0


if __name__ == "__main__":
    # Run alone, this rendered into the home unchecked.
    raise SystemExit(f"render_codex.py publishes nothing by itself; run {SYNC}")
