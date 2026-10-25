#!/usr/bin/env python3
"""Assert the agent runtime never executes code from a dev-only directory.

`~/.claude` is both the live harness config and this repository, so `tests/`,
`docs/`, `experiments/` and `research/` sit inside the directory the agent
reads. Claude Code offers no boundary there: Read/Edit deny rules cover the
built-in file tools and the Bash file commands it recognises, not arbitrary
subprocesses, and the sandbox covers shell commands only -- hooks, local MCP
servers and the file tools run outside it with full access. So if a
`settings.json` hook command or an `mcp-servers.json` server path ever
resolved under `tests/`, nothing in the harness would fence it.

This gate is the enforcement. It fails when runtime config reaches into a
dev-only tree:

  A  settings.json         a hook (or statusLine) command naming a dev-only path
  B  mcp-servers.json      a server command/args naming a dev-only path
  C  tracked *.py / *.sh   outside the dev-only trees, executing or importing
                           from one

Prose is not a violation. Check C ignores comments, and for Python it only
looks at strings passed to exec-ish calls, so the many legitimate "see
tests/test_x.py" docstring pointers stay legal.

    check-dev-path-isolation.py [--repo-root DIR] [--quiet]

Exit 0 clean, 1 violations (listed as `path:line: message`), 2 usage or
internal error.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

# Tracked trees that exist for development and must never be on the agent's
# execution path. `.githooks/` and `.github/` are deliberately absent: git and
# the CI runner execute those by design, which is a different lane from the
# agent runtime this gate protects.
DEV_ONLY = ("tests", "docs", "experiments", "research")

# Prefixes a hook or MCP command may use to name a file inside the config dir.
# Each is stripped to a repo-relative path before the dev-only test.
CONFIG_DIR_PREFIXES = (
    "$HOME/.claude/",
    "${HOME}/.claude/",
    "~/.claude/",
    "$CLAUDE_CONFIG_DIR/",
    "${CLAUDE_CONFIG_DIR}/",
)

# Python callables that run another program or load another module. A dev-only
# path reaching one of these is a violation; the same path in a docstring is not.
EXEC_CALLS = (
    "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.Popen",
    "os.system", "os.popen", "os.execv", "os.execve", "os.execvp",
    "os.execvpe", "os.execl", "os.execle", "os.execlp", "os.spawnv",
    "runpy.run_path", "runpy.run_module",
    "importlib.import_module", "import_module",
    "exec", "eval",
)

# Shell words that take a script path as their next argument.
SHELL_RUNNERS = (
    "source", ".", "bash", "sh", "zsh", "dash",
    "python", "python3", "perl", "ruby", "node", "env", "exec",
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
    """Strip a config-dir prefix, returning a repo-relative path or None.

    A token that names no config-dir path (an absolute system path, a bare
    flag) is not this gate's business and yields None.
    """
    token = token.strip().strip("\"'")
    token = _ABS_CONFIG_RE.sub("", token, count=1)
    for prefix in CONFIG_DIR_PREFIXES:
        if token.startswith(prefix):
            return token[len(prefix):]
    if token.startswith(("/", "~")) or "$" in token.split("/")[0]:
        return None
    return token.lstrip("./")


def is_dev_only(rel: str | None) -> bool:
    """True when `rel` reaches *into* a dev-only tree.

    A separator is required. The bare word is not enough and matching it is a
    live source of noise: `mcp-servers.json` passes `--tools docs docs_preview`
    (tool names), and `mcps/klaffat-app-runner/e2e_deps.py` builds
    `Path.cwd() / "tests" / "e2e"` against another repository entirely.

    The cost is a known blind spot: a path assembled piecewise, as
    `os.path.join("tests", "smoke.sh")`, reads as no path at all here. Evasion
    by that route is a review problem, not one a linter closes.
    """
    if not rel or "/" not in rel.rstrip("/"):
        return False
    return rel.split("/")[0] in DEV_ONLY


def dev_paths_in(text: str) -> list[str]:
    """Every dev-only path token in a command line or string literal."""
    out = []
    for token in _TOKEN_SPLIT.split(text):
        bare = token.strip().strip("\"'")
        if bare and is_dev_only(to_repo_relative(bare)):
            out.append(bare)
    return out


def strip_shell_comments(line: str) -> str:
    """Drop a trailing `#` comment, respecting single and double quotes."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    )
    return [p for p in proc.stdout.split("\0") if p]


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


# ----------------------------------------------------------------------------- C

def _callee_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_callee_name(node.value)}.{node.attr}"
    return ""


def check_python(rel_path: str, text: str) -> list[Violation]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [Violation(rel_path, exc.lineno or 1, f"syntax error: {exc.msg}")]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [a.name for a in node.names] + [module]
            for name in names:
                head = name.split(".")[0]
                if head in DEV_ONLY:
                    out.append(Violation(
                        rel_path, node.lineno,
                        f"imports from dev-only tree '{head}'",
                    ))
        elif isinstance(node, ast.Call):
            callee = _callee_name(node.func)
            if callee not in EXEC_CALLS and callee.split(".")[-1] not in (
                c.split(".")[-1] for c in EXEC_CALLS
            ):
                continue
            # Only what the call will execute. `cwd=`, `env=` and the stream
            # keywords name a context, not a program, and reading them turns
            # every harness that chdirs into a test tree into a violation.
            executed = list(node.args) + [
                kw.value for kw in node.keywords
                if kw.arg in ("args", "executable", "path", "module")
            ]
            for arg in executed:
                for literal in ast.walk(arg):
                    if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                        for hit in dev_paths_in(literal.value):
                            out.append(Violation(
                                rel_path, literal.lineno,
                                f"{callee}() executes dev-only path '{hit}'",
                            ))
    return out


def check_shell(rel_path: str, text: str) -> list[Violation]:
    out = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = strip_shell_comments(raw_line)
        if not line.strip():
            continue
        words = line.replace(";", " ").replace("|", " ").replace("&", " ").split()
        for index, word in enumerate(words):
            bare = word.strip("\"'")
            rel = to_repo_relative(bare)
            if not is_dev_only(rel):
                continue
            previous = words[index - 1].strip("\"'").rsplit("/", 1)[-1] if index else ""
            first_word = index == 0
            if first_word or previous in SHELL_RUNNERS:
                out.append(Violation(
                    rel_path, number,
                    f"executes dev-only path '{bare}'",
                ))
    return out


def check_executables(root: Path) -> list[Violation]:
    out = []
    for rel_path in tracked_files(root):
        if rel_path.split("/")[0] in DEV_ONLY:
            continue
        if not rel_path.endswith((".py", ".sh")):
            continue
        full = root / rel_path
        try:
            text = full.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if rel_path.endswith(".py"):
            out.extend(check_python(rel_path, text))
        else:
            out.extend(check_shell(rel_path, text))
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
            print("check-dev-path-isolation: not a git repository", file=sys.stderr)
            return 2
        root = Path(top)
    root = root.resolve()
    if not (root / ".git").exists() and not (root / "settings.json").exists():
        print(f"check-dev-path-isolation: {root} is not a config repo", file=sys.stderr)
        return 2

    violations = check_settings(root) + check_mcp_servers(root) + check_executables(root)

    if violations:
        print(
            f"check-dev-path-isolation: {len(violations)} violation(s) -- "
            f"runtime config must not execute {'/, '.join(DEV_ONLY)}/",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(
            "check-dev-path-isolation: clean -- no runtime path reaches "
            f"{', '.join(d + '/' for d in DEV_ONLY)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
