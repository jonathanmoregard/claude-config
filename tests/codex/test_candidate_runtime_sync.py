#!/usr/bin/env python3
"""CI guard: the candidate runtime/ in this checkout syncs to Codex cleanly.

runtime/ is what ~/.claude serves, and the Codex config is a pure mirror of
it, rendered by codex/scripts/sync_codex.py (import, render, check). These
tests run that sync on the candidate tree itself -- whatever this commit or
pull request carries -- so a Claude config change the renderer cannot map, or
one its check rejects, fails here instead of in the next production publish.

The candidate is staged into a throwaway HOME:

- `.claude` holds runtime/'s tracked entries, which is what a CI checkout has.
  Symlinks are copied as what they resolve to; one that does not resolve is
  skipped. The vendor submodule skills are such links in CI, which never
  fetches submodules.
- In settings.json and mcp-servers.json the author's home prefix becomes the
  temp HOME, in the copy only, so every path the renderer resolves from them
  lands inside the throwaway home. The prefix is read from
  runtime/scripts/runtime-layout.py, which declares it for the projection.
- `.claude.json` is an empty stub: Claude's global state is not config.

The renderer runs from a copy of codex/ (roundtrip_support.copy_repo): its
importer writes a manifest beside the scripts, and the checkout must never
receive a test manifest.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roundtrip_support import RoundTrip, copy_repo, describe, render_codex, snapshot  # noqa: E402

import check  # noqa: E402  (roundtrip_support put codex/scripts on sys.path)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = REPO_ROOT / "runtime"
HOME_REWRITTEN = ("settings.json", "mcp-servers.json")
SUMMARY = re.compile(r"^rendered Codex: skills=(\d+) .* mcp=(\d+)$", re.MULTILINE)


def author_home() -> str:
    spec = importlib.util.spec_from_file_location(
        "runtime_layout", RUNTIME / "scripts" / "runtime-layout.py"
    )
    assert spec is not None and spec.loader is not None
    layout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout)
    return str(layout.HOME).rstrip("/")


def tracked_runtime() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "runtime"],
        check=True,
        capture_output=True,
    ).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def stage_candidate(
    root: Path, edit_settings: Callable[[dict[str, object]], None] | None = None
) -> RoundTrip:
    home = root / "home"
    claude = home / ".claude"
    entries = tracked_runtime()
    assert entries, "git lists no tracked file under runtime/"
    for name in entries:
        source = REPO_ROOT / name
        if not source.exists():  # a dangling symlink, or deleted in the tree
            continue
        target = claude / Path(name).relative_to("runtime")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore_dangling_symlinks=True)
        else:
            shutil.copy2(source, target)

    prefix = author_home() + "/"
    for name in HOME_REWRITTEN:
        path = claude / name
        path.write_text(
            path.read_text(encoding="utf-8").replace(prefix, f"{home}/"), encoding="utf-8"
        )
    if edit_settings is not None:
        settings_path = claude / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        edit_settings(settings)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    (home / ".claude.json").write_text("{}\n", encoding="utf-8")
    return RoundTrip(repo=copy_repo(root / "repo"), home=home)


def test_candidate_runtime_syncs_clean_and_resyncs_as_a_no_op() -> None:
    with tempfile.TemporaryDirectory(prefix="claude-config-candidate-") as td:
        rt = stage_candidate(Path(td))
        first = rt.sync()
        assert first.returncode == 0, describe(first)
        assert "ai-client-config check OK" in first.stdout, describe(first)
        # A staging that silently lost the candidate (a layout move, say)
        # would still check OK; the summary says whether anything rendered.
        summary = SUMMARY.search(first.stdout)
        assert summary, describe(first)
        assert int(summary.group(1)) > 0 and int(summary.group(2)) > 0, describe(first)

        published = snapshot(rt.home)
        second = rt.sync()
        assert second.returncode == 0, describe(second)
        assert "ai-client-config check OK" in second.stdout, describe(second)
        resynced = snapshot(rt.home)
        changed = sorted(
            key
            for key in published.keys() | resynced.keys()
            if published.get(key) != resynced.get(key)
        )
        assert not changed, f"an unchanged candidate rewrote {changed}"


def guard_first_edit_guard_hook(settings: dict[str, object]) -> None:
    """Make the candidate's first file-edit PreToolUse hook inert under Codex.

    A hook that exits early unless the blanket disable guard is set still
    reads as configured, but the edit gate it carries never runs in Codex.
    The renderer passes the command through verbatim; only the check sees it.
    A file-edit hook is picked because the renderer mirrors those, where it
    leaves Claude-shell-only (`Bash`) hooks out of Codex by design.
    """
    hooks = settings["hooks"]
    assert isinstance(hooks, dict)
    entry = next(
        (
            entry
            for entry in hooks.get("PreToolUse", [])
            if render_codex.matcher_targets_file_tools(entry.get("matcher"))
        ),
        None,
    )
    assert entry is not None, "the candidate has no file-edit PreToolUse hook to corrupt"
    action = entry["hooks"][0]
    assert action["type"] == "command", action
    action["command"] = (
        f'[ -n "${{{check.HOOK_DISABLE_GUARD}:-}}" ] || exit 0; {action["command"]}'
    )


def test_a_candidate_hook_disabled_under_codex_fails_the_sync() -> None:
    with tempfile.TemporaryDirectory(prefix="claude-config-candidate-") as td:
        rt = stage_candidate(Path(td), guard_first_edit_guard_hook)
        result = rt.sync()
        assert result.returncode == 1, describe(result)
        assert "ai-client-config check FAILED" in result.stdout, describe(result)
        suffix = ": hook wrapper contains the blanket disable guard"
        wrappers = sorted(
            Path(line[2 : -len(suffix)])
            for line in result.stdout.splitlines()
            if line.startswith("- ") and line.endswith(suffix)
        )
        assert wrappers, f"check named no guarded wrapper\n{describe(result)}"
        hooks_root = rt.codex / "hooks"
        for wrapper in wrappers:
            assert wrapper.is_relative_to(hooks_root), f"{wrapper} outside {hooks_root}"
            # The failed check rolled the run back, so the guarded wrapper it
            # rendered must not be left published.
            assert not wrapper.exists(), f"{wrapper} still published after rollback"
        assert not any(
            check.HOOK_DISABLE_GUARD in path.read_text(encoding="utf-8")
            for path in hooks_root.rglob("*.sh")
        ), f"a guarded wrapper survived the rollback\n{describe(result)}"


if __name__ == "__main__":
    test_candidate_runtime_syncs_clean_and_resyncs_as_a_no_op()
    test_a_candidate_hook_disabled_under_codex_fails_the_sync()
