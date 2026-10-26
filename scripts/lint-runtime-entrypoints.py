#!/usr/bin/env python3
"""Lint the declared runtime entrypoints for paths into a dev-only tree.

`~/.claude` is both the live harness config and this repository, so `tests/`,
`docs/`, `experiments/` and `research/` sit inside the directory the agent
reads. Claude Code offers no boundary there: Read/Edit deny rules cover the
built-in file tools and the Bash file commands it recognises, not arbitrary
subprocesses, and the sandbox covers shell commands only -- hooks, local MCP
servers and the file tools run outside it with full access.

WHAT THIS IS. A lint over the two files that *declare* what the harness will
execute on its own initiative:

  A  settings.json      a hook or statusLine `command`
  B  mcp-servers.json   a server `command` / `args`

Both are well posed. The path is a declared field of a structured document, so
answering "does this name a dev-only tree?" needs string normalisation and
nothing else -- no dataflow, no call graph, no guessing. A violation here is
the realistic accident: an entrypoint edited to point at a harness under
`tests/` would be fenced by nothing in the platform.

WHAT THIS IS NOT. It is not an isolation boundary, and it must not be read as
one. It inspects text; the property that matters is a filesystem property.
Anything that executes a dev-only path without naming it in these two files --
a runtime script that shells out, a path assembled from pieces, an `importlib`
module name, a symlink -- passes this lint untouched and always will.

An earlier revision tried to close that by scanning every tracked `*.py` and
`*.sh` for exec-ish calls. It was deleted rather than patched, because the ways
around it are ordinary rather than adversarial: `bash -e tests/x.sh` (an option
before the path), `importlib.import_module("tests.helper")` (a module name, not
a path), and `p = "tests/x.sh"` before `subprocess.run(["bash", p])` (one hop
of indirection) all scanned clean. No maintained tool answers that question
either: Semgrep's taint mode is intra-procedural and Python-only, ShellCheck
has no dataflow, and CodeQL is a heavy instrument for a question whose real
answer is a kernel one. The boundary is the read-only bind mount that keeps
the dev trees out of the agent's namespace; this lint only catches the
entrypoint edit that would otherwise go unnoticed in review.

    lint-runtime-entrypoints.py [--repo-root DIR] [--quiet]

Exit 0 clean, 1 violations (listed as `path:line: message`), 2 usage or
internal error.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

# Tracked trees that exist for development and must never be named by a
# declared entrypoint. `.githooks/` and `.github/` are deliberately absent: git
# and the CI runner execute those by design, which is a different lane from the
# agent runtime.
DEV_ONLY = ("tests", "docs", "experiments", "research")

# Prefixes an entrypoint may use to name a file inside the config dir. Each is
# stripped to a repo-relative path before the dev-only test.
CONFIG_DIR_PREFIXES = (
    "$HOME/.claude/",
    "${HOME}/.claude/",
    "~/.claude/",
    "$CLAUDE_CONFIG_DIR/",
    "${CLAUDE_CONFIG_DIR}/",
)

# A command line is split into candidate path tokens before each is tested.
# Matching with a regex over the whole string is the trap here: a dev-only
# segment is almost always embedded in a longer path (`$HOME/.claude/tests/x`),
# so any pattern anchored on a non-path character before `tests/` silently
# matches nothing at all.
_TOKEN_SPLIT = re.compile(r"[\s;|&()<>]+")
_ABS_CONFIG_RE = re.compile(r"/home/[^/\s]+/\.claude/")


class Violation:
    def __init__(self, path: str, line: int, message: str) -> None:
        self.path, self.line, self.message = path, line, message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def to_repo_relative(token: str) -> str | None:
    """Strip a config-dir prefix, returning a normalised repo-relative path.

    Normalisation is load-bearing, not tidiness: `hooks/../tests/x.sh` and
    `./tests/x.sh` name exactly the file that `tests/x.sh` names, and a check
    that compares the first path segment literally sees three different things.
    A token that names no config-dir path -- an absolute system path, a bare
    flag, or a `..` that climbs out of the config dir -- is not this lint's
    business and yields None.
    """
    token = token.strip().strip("\"'")
    token = _ABS_CONFIG_RE.sub("", token, count=1)
    for prefix in CONFIG_DIR_PREFIXES:
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    else:
        if token.startswith(("/", "~")) or "$" in token.split("/")[0]:
            return None
    if not token:
        return None
    normalised = posixpath.normpath(token)
    if normalised == ".." or normalised.startswith("../"):
        return None
    return normalised


def is_dev_only(rel: str | None) -> bool:
    """True when `rel` reaches *into* a dev-only tree.

    A separator is required. The bare word is not enough and matching it is a
    live source of noise: `mcp-servers.json` passes `--tools docs docs_preview`,
    which are tool names.
    """
    if not rel or "/" not in rel.rstrip("/"):
        return False
    return rel.split("/")[0] in DEV_ONLY


def dev_paths_in(text: str) -> list[str]:
    """Every dev-only path token in a declared command line."""
    out = []
    for token in _TOKEN_SPLIT.split(text):
        bare = token.strip().strip("\"'")
        if bare and is_dev_only(to_repo_relative(bare)):
            out.append(bare)
    return out


def line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    return 1


# --------------------------------------------------------------------------- A/B

def command_strings_in_settings(data: dict) -> list[tuple[str, str]]:
    """(label, command) for every field a harness treats as a command line."""
    found: list[tuple[str, str]] = []
    for event, matchers in (data.get("hooks") or {}).items():
        for matcher in matchers if isinstance(matchers, list) else []:
            for hook in matcher.get("hooks", []) if isinstance(matcher, dict) else []:
                command = hook.get("command") if isinstance(hook, dict) else None
                if isinstance(command, str):
                    found.append((f"hooks.{event}", command))
    status = data.get("statusLine")
    if isinstance(status, dict) and isinstance(status.get("command"), str):
        found.append(("statusLine", status["command"]))
    return found


def check_settings(root: Path) -> list[Violation]:
    path = root / "settings.json"
    if not path.exists():
        return []
    raw = path.read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [Violation("settings.json", exc.lineno, f"invalid JSON: {exc.msg}")]
    out = []
    for label, command in command_strings_in_settings(data):
        for hit in dev_paths_in(command):
            out.append(Violation(
                "settings.json", line_of(raw, hit),
                f"{label} command executes dev-only path '{hit}' -- "
                "hooks run outside the sandbox and outside deny rules",
            ))
    return out


def check_mcp_servers(root: Path) -> list[Violation]:
    path = root / "mcp-servers.json"
    if not path.exists():
        return []
    raw = path.read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [Violation("mcp-servers.json", exc.lineno, f"invalid JSON: {exc.msg}")]
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data
    out = []
    for name, spec in (servers or {}).items():
        if not isinstance(spec, dict):
            continue
        words = [spec.get("command", "")] + [str(a) for a in spec.get("args", []) or []]
        for word in words:
            for hit in dev_paths_in(str(word)):
                out.append(Violation(
                    "mcp-servers.json", line_of(raw, hit),
                    f"server '{name}' runs dev-only path '{hit}' -- "
                    "MCP servers run with full user access",
                ))
    return out


# -------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = args.repo_root
    if root is None:
        try:
            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("lint-runtime-entrypoints: not a git repository", file=sys.stderr)
            return 2
        root = Path(top)
    root = root.resolve()
    if not (root / ".git").exists() and not (root / "settings.json").exists():
        print(f"lint-runtime-entrypoints: {root} is not a config repo", file=sys.stderr)
        return 2

    violations = check_settings(root) + check_mcp_servers(root)

    if violations:
        print(
            f"lint-runtime-entrypoints: {len(violations)} violation(s) -- "
            f"a declared entrypoint names {'/, '.join(DEV_ONLY)}/",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(
            "lint-runtime-entrypoints: clean -- no declared entrypoint names "
            f"{', '.join(d + '/' for d in DEV_ONLY)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
