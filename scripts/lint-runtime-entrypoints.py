#!/usr/bin/env python3
"""Lint the declared runtime entrypoints for paths into a dev-only tree.

`~/.claude` is both the live harness config and this repository, so `tests/`,
`docs/`, `experiments/` and `research/` sit inside the directory the agent
reads. Claude Code offers no boundary there: Read/Edit deny rules cover the
built-in file tools and the Bash file commands it recognises, not arbitrary
subprocesses, and the sandbox covers shell commands only -- hooks, local MCP
servers and the file tools run outside it with full access.

WHAT THIS IS. A lint over the command-bearing fields of two tracked files:

  A  settings.json      `hooks.*[].hooks[].command`, `statusLine.command`,
                        `apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`,
                        `otelHeadersHelper`, every `env` value, and
                        `extraKnownMarketplaces.*.source.path`
  B  mcp-servers.json   a server `command`, `args` and `env`

Those are not the only places a path can become an execution path, and saying
otherwise was an earlier version's mistake. NOT read here: skill and agent
frontmatter `hooks:`, a plugin's own `hooks.json`, `settings.local.json`, and
`~/.claude.json` -- which is where the harness actually keeps the registered
MCP servers (all nine live ones), making tracked `mcp-servers.json` the copy
that gets synced in rather than the source of truth. Check B is therefore a
check on the delivery path, not on the running configuration.

Both are tractable because the path is a declared field of a structured
document rather than something to be inferred from code. A violation is the
realistic accident: an entrypoint edited to point at a harness under `tests/`
would be fenced by nothing in the platform.

It is a **conservative lint over path mentions**, not an execution analysis,
and the distinction is not pedantry. A `command` is a shell line, and shell
semantics are not modelled here: quoting, substitutions and which word is the
program all go unexamined, so `echo tests/x.sh` is reported exactly like
`bash tests/x.sh`. That asymmetry is deliberate. A declared entrypoint has no
business naming a dev-only path at all, so a false report costs one line of
review while a miss is silent -- which is why the diagnostics say a path is
*named*, never that it is executed.

WHAT THIS IS NOT. It is not an isolation boundary, and it must not be read as
one. It inspects text; the property that matters is a filesystem property.
Anything that reaches a dev-only path without naming it in these two files --
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
answer is a kernel one.

The boundary is **the dev trees not being in the agent's mounted view at all**.
Read-only is a separate property that stops writes; what stops access is the
exclusion -- `tests/`, `docs/`, `experiments/` and `research/` are simply not
among the paths projected into `~/.claude`, so no subprocess, hook or MCP
server can open them however it spells the path. This lint only catches the
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

# How a token says "inside the config dir". Matching the marker wherever it
# appears, rather than testing a list of prefixes, is what makes the resolver
# robust: an earlier version carried five literal prefixes plus an
# absolute-path regex, and every one of these ordinary spellings walked
# straight past it --
#
#   "$HOME"/.claude/tests/x.sh                      quotes inside the token
#   ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/tests/x.sh  a default-value expansion
#   CLAUDE_PLUGIN_ROOT=$HOME/.claude/tests/y        an assignment prefix, which
#                                                   is the shape settings.json
#                                                   already uses for vendor/
#   --config=$HOME/.claude/tests/x.sh               a path glued to a flag
#   /home/u/.claude//tests/x.sh                     a doubled separator
#
# so the first path segment was compared against `tests` while holding
# something like `$HOME"` or `X=$HOME`.
# `\}*` so a closing brace between the marker and the separator does not hide
# it, as in `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/tests/x.sh`.
_CONFIG_DIR_RE = re.compile(r"(?:\.claude|\$\{?CLAUDE_CONFIG_DIR\}?)\}*/+")

# A command line is split into candidate path tokens before each is tested.
# Matching with a regex over the whole string is the trap here: a dev-only
# segment is almost always embedded in a longer path (`$HOME/.claude/tests/x`),
# so any pattern anchored on a non-path character before `tests/` silently
# matches nothing at all. `=` and `,` are separators too, so an assignment or a
# comma-joined list yields the path on its own.
#
# `:` is a separator for the same reason, and its absence was a real hole: an
# ordinary `PYTHONPATH` of `$HOME/.claude/tests:/usr/lib/python3` arrived as a
# single token, so the segment compared against `tests` was `tests:` and the
# lint stayed quiet about a dev-only entry on the import path. A path list is
# the common shape of exactly the field most worth reading.
_TOKEN_SPLIT = re.compile(r"[\s;|&()<>=,:]+")


class Violation:
    def __init__(self, path: str, line: int, message: str) -> None:
        self.path, self.line, self.message = path, line, message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _inside_config_dir(rest: str) -> str | None:
    """Normalise the part of a token that follows a config-dir marker.

    Normalisation is load-bearing, not tidiness: `hooks/../tests/x.sh`,
    `./tests/x.sh` and `tests//x.sh` all name the file `tests/x.sh` names, and
    a check comparing the first path segment literally sees four unrelated
    strings.

    `None` when the remainder climbs back out of the config dir, because it
    then names some other tree.
    """
    if not rest:
        return None
    normalised = posixpath.normpath(rest)
    if normalised == ".." or normalised.startswith("../"):
        return None
    return normalised


def to_repo_relative(token: str) -> list[tuple[str, bool]]:
    """Every way this token can be read as a path inside the config dir.

    Each element is `(repo-relative path, named the config dir)`. The second
    field is what lets `$HOME/.claude/tests` count while a bare `tests` does
    not: the first can only mean the directory, the second is usually a word
    (`--tools docs`).

    A list rather than one answer because a token may name the config dir more
    than once. Resolving only the last marker reads the token as its author's
    final word, which is right for a single path but wrong for anything that
    concatenates two of them -- the earlier path is then invisible. Reporting
    on any reading is the conservative direction: the cost of a surplus reading
    is a line of review, the cost of a missed one is silence.

    Empty for anything that is not a path inside the config dir: an absolute
    system path, a variable-rooted path pointing elsewhere, or a `..` that
    climbs out.
    """
    token = token.strip()
    for ch in "\"'\\":
        token = token.replace(ch, "")

    markers = list(_CONFIG_DIR_RE.finditer(token))
    if markers:
        readings = [_inside_config_dir(token[m.end():]) for m in markers]
        return [(rel, True) for rel in readings if rel is not None]
    if token.startswith(("/", "~")) or "$" in token:
        return []
    rel = _inside_config_dir(token)
    return [(rel, False)] if rel is not None else []


def is_dev_only(rel: str | None, anchored: bool = False) -> bool:
    """True when `rel` names a dev-only tree, or something inside one."""
    if not rel:
        return False
    if rel.split("/")[0] not in DEV_ONLY:
        return False
    if anchored:
        # `$HOME/.claude/tests` is the directory, separator or not --
        # `cd $HOME/.claude/tests && bash x.sh` is the shape this catches.
        return True
    # Unanchored, a separator is required. The bare word is a live source of
    # noise: `mcp-servers.json` passes `--tools docs docs_preview`, tool names.
    return "/" in rel.rstrip("/")


def dev_paths_in(text: str) -> list[str]:
    """Every dev-only path token in a declared command line."""
    out = []
    for token in _TOKEN_SPLIT.split(text):
        bare = token.strip()
        if not bare:
            continue
        if any(is_dev_only(rel, anchored) for rel, anchored in to_repo_relative(bare)):
            out.append(bare)
    return out


def line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    return 1


# --------------------------------------------------------------------------- A/B

# Top-level settings fields the harness runs as a command. Each is present in
# the installed harness's own settings schema (checked against claude-code
# 2.1.288); hooks and statusLine are not the only two, and an earlier revision
# of this lint read only those.
HELPER_COMMAND_FIELDS = (
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "otelHeadersHelper",
)


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
    for field in HELPER_COMMAND_FIELDS:
        value = data.get(field)
        if isinstance(value, str):
            found.append((field, value))
    # `env` is not run directly, and reaches every hook anyway: BASH_ENV is
    # sourced by each non-interactive bash, PYTHONPATH decides what `import`
    # resolves to. A dev-only path here is an execution path.
    for name, value in (data.get("env") or {}).items():
        if isinstance(value, str):
            found.append((f"env.{name}", value))
    # A marketplace source path is loaded as plugin code.
    for name, entry in (data.get("extraKnownMarketplaces") or {}).items():
        source = entry.get("source") if isinstance(entry, dict) else None
        path = source.get("path") if isinstance(source, dict) else None
        if isinstance(path, str):
            found.append((f"extraKnownMarketplaces.{name}.source.path", path))
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
                f"{label} command names dev-only path '{hit}' -- "
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
        # Same reasoning as settings `env`: the server's environment decides
        # what its interpreter loads.
        words += [str(v) for v in (spec.get("env") or {}).values()]
        for word in words:
            for hit in dev_paths_in(str(word)):
                out.append(Violation(
                    "mcp-servers.json", line_of(raw, hit),
                    f"server '{name}' names dev-only path '{hit}' -- "
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
