"""Shared logic for the ARTL PR-open gate.

Imported by hooks/pr-open-gate.py (PreToolUse, PermissionRequest,
UserPromptSubmit) and hooks/artl-evidence.py (PostToolUse on Agent,
SubagentStop). Stdlib only; every git call is bounded.

WHERE THINGS LIVE
  <notes>/artl-evidence.jsonl    one line per advisor verdict (the evidence)
  <notes>/artl-exemptions.jsonl  every PR open let through WITHOUT a passing
                                 review: computed exemptions, the user's
                                 skip-artl override, nixos `n/a` smoke claims
  ~/.local/state/claude-pr-open-gate/
      pending/   the two halves of one advisor dispatch (launch + verdict)
      grants/    one-shot answers for the permissions.ask backstop
      override/  a user-typed skip-artl, per session, until consumed

<notes> is CLAUDE.md's: ~/.local/state/claude-tasks/<name>, where <name> is
the basename of the session's primary working directory ($CLAUDE_PROJECT_DIR)
with a leading dot rewritten to `dot-`. The writer uses exactly that. The gate
also reads the notes named after the checkout it inspects and after that
checkout's primary clone, because a review run from one session may be
followed by a PR opened from another. Records carry the repository's
git-common-dir, so reading more directories never mixes repositories up.

Records are tamper-EVIDENT, not tamper-proof: anything a hook can write, the
same uid can write. settings.json denies the agent's Edit tool and any Bash
command naming these files, which stops the casual write; it does not stop a
determined one.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

GIT_TIMEOUT_S = 5
SNAPSHOT_TIMEOUT_S = 10
DRIFT_LIMIT = 20          # lines changed since the reviewed tree that still count
SMALL_LIMIT = 15          # a change this small, outside SENSITIVE paths, is exempt
GRANT_TTL_S = 120
OVERRIDE_TTL_S = 24 * 3600
PENDING_TTL_S = 24 * 3600
MAX_READ_BYTES = 4_000_000
MAX_PROMPT_PATHS = 200

EVIDENCE_FILE = "artl-evidence.jsonl"
EXEMPTIONS_FILE = "artl-exemptions.jsonl"
VERDICTS = ("PASS", "PASS_WITH_FIXES", "BLOCKS")
PASSING = ("PASS", "PASS_WITH_FIXES")
NOT_REVIEWS = ("CONSULT dispatch", "VALIDATION dispatch")
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# --- locations ---------------------------------------------------------------

def notes_root() -> Path:
    override = os.environ.get("CLAUDE_TASKS_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "claude-tasks"


def state_dir() -> Path:
    override = os.environ.get("CLAUDE_PR_OPEN_GATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "claude-pr-open-gate"


def notes_name(path: Path) -> str:
    base = path.name
    return "dot-" + base[1:] if base.startswith(".") else base


def session_notes(payload: dict) -> Path:
    """CLAUDE.md's <notes> for this session."""
    project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    return notes_root() / notes_name(Path(project))


def safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and ID_RE.match(value) else None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# --- git ---------------------------------------------------------------------

def git(cwd: str | Path, *args: str, env: dict | None = None,
        timeout: float = GIT_TIMEOUT_S) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                              text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def repo_info(path: str | Path) -> dict | None:
    """{"top", "repo"} for the checkout holding PATH, or None."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir():
        p = p.parent
    top = git(p, "rev-parse", "--show-toplevel")
    common = git(p, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not top or not common:
        return None
    return {"top": top, "repo": os.path.realpath(common)}


def primary_dir(repo: str) -> Path:
    common = Path(repo)
    if common.name == ".git":
        return common.parent
    return common.with_name(common.name.removesuffix(".git"))


def read_notes_dirs(payload: dict, info: dict) -> list[Path]:
    names = [session_notes(payload).name, notes_name(Path(info["top"])),
             notes_name(primary_dir(info["repo"]))]
    out: list[Path] = []
    for name in names:
        d = notes_root() / name
        if d not in out:
            out.append(d)
    return out


def base_ref(top: str) -> str | None:
    head = git(top, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    candidates = ([head] if head else []) + ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if git(top, "rev-parse", "--verify", "-q", ref + "^{commit}"):
            return ref
    return None


def current_branch(top: str) -> str | None:
    return git(top, "symbolic-ref", "-q", "--short", "HEAD")


def numstat(top: str, a: str, b: str) -> list[tuple[str, int | None]] | None:
    """[(path, lines changed or None for binary)] between two tree-ishes."""
    out = git(top, "diff", "--numstat", "-z", "--no-renames", a, b)
    if out is None:
        return None
    rows = []
    for entry in out.split("\0"):
        parts = entry.split("\t", 2)
        if len(parts) != 3 or not parts[2]:
            continue
        added, deleted, path = parts
        lines = None if "-" in (added, deleted) else int(added) + int(deleted)
        rows.append((path, lines))
    return rows


def line_total(rows: list[tuple[str, int | None]], binary_cost: int) -> int:
    return sum(binary_cost if n is None else n for _, n in rows)


def snapshot_tree(top: str) -> str | None:
    """The tree of the working state as a reviewer sees it: HEAD plus staged,
    unstaged and untracked (not ignored) changes. Written through a throwaway
    index, so the real index and the stash are never touched."""
    index = git(top, "rev-parse", "--path-format=absolute", "--git-path", "index")
    with tempfile.TemporaryDirectory(prefix="artl-index-") as tmp:
        tmp_index = os.path.join(tmp, "index")
        env = {"GIT_INDEX_FILE": tmp_index}
        seeded = False
        if index and os.path.isfile(index):
            try:
                shutil.copyfile(index, tmp_index)
                seeded = True
            except OSError:
                seeded = False
        if not seeded and git(top, "read-tree", "HEAD", env=env) is None:
            return git(top, "rev-parse", "HEAD^{tree}")
        if git(top, "add", "-A", env=env, timeout=SNAPSHOT_TIMEOUT_S) is None:
            return git(top, "rev-parse", "HEAD^{tree}")
        return git(top, "write-tree", env=env) or git(top, "rev-parse", "HEAD^{tree}")


# --- files ---------------------------------------------------------------------

def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()


def read_jsonl(path: Path) -> list[dict]:
    try:
        if not path.is_file():
            return []
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - MAX_READ_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def prune(directory: Path, ttl: float, now: float) -> int:
    """Delete expired files in `directory`. Returns how many it could not remove.

    Housekeeping only, and deliberately not load-bearing: expiry is enforced
    where a grant is CONSUMED — pr-open-gate.on_permission unlinks the grant
    and only then compares its `ts` against GRANT_TTL_S — so a failure here
    leaves litter on disk, never a grant anyone can still spend. The count is
    returned rather than swallowed so a caller can say so out loud; a gate
    that quietly ignores its own IO errors is how a fail-open starts.
    """
    failed = 0
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return 0
    except OSError:
        return 1
    for f in entries:
        try:
            if now - f.stat().st_mtime > ttl:
                f.unlink(missing_ok=True)
        except OSError:
            failed += 1
    return failed


# --- what counts as opening a PR ------------------------------------------------

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;"}
WRAPPERS = {"command", "exec", "time", "nohup", "sudo", "builtin"}
SHELLS = {"bash", "sh", "zsh", "dash"}
# gh api flags that take a value, so the endpoint is not mistaken for one.
API_VALUE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H",
                   "--header", "--input", "-q", "--jq", "-t", "--template",
                   "--hostname", "-p", "--preview", "--cache"}
API_BODY_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}
PULLS_ENDPOINT = re.compile(r"/?repos/[^/\s]+/[^/\s]+/pulls/?")
HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
FALLBACK = re.compile(
    r"\bgh\s+pr\s+(?:create|new)\b|createPullRequest"
    r"|\bgh\s+api\b(?=.*/pulls\b)(?=.*(?:POST|\s-[fF]\s|--field|--raw-field|--input))",
    re.S)


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies: their text is data written to a file, not commands."""
    out_lines: list[str] = []
    pending: list[str] = []
    for line in cmd.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out_lines.append(line)
        pending.extend(m.group(2) for m in HEREDOC.finditer(line))
    return "\n".join(out_lines)


def _tokens(cmd: str) -> list[str]:
    lex = shlex.shlex(strip_heredocs(cmd), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    return list(lex)


def _segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in SEPARATORS or (tok and set(tok) <= set(";&|()\n")):
            segs.append([])
        elif tok and set(tok) <= set("<>&"):
            skip_next = True        # redirection: the next token is its target
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def _flag_value(words: list[str], names: tuple[str, ...]) -> str | None:
    for i, w in enumerate(words):
        for name in names:
            if w == name and i + 1 < len(words):
                return words[i + 1]
            if name.startswith("--") and w.startswith(name + "="):
                return w.split("=", 1)[1]
    return None


def _gh_api_opens_pr(args: list[str]) -> bool:
    method = None
    has_body = False
    endpoint = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1].upper()
            i += 2
            continue
        if a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
        elif a in API_BODY_FLAGS:
            has_body = True
        if a in API_VALUE_FLAGS:
            i += 2
            continue
        if not a.startswith("-") and endpoint is None:
            endpoint = a
        i += 1
    if endpoint == "graphql":
        return any("createPullRequest" in a for a in args)
    if endpoint and PULLS_ENDPOINT.fullmatch(endpoint):
        return (method or ("POST" if has_body else "GET")) == "POST"
    return False


def _scan(cmd: str, depth: int) -> dict | None:
    try:
        tokens = _tokens(cmd)
    except ValueError:
        # Unbalanced quoting: no reliable parse, so the raw text decides, and
        # it errs toward treating a mention as an open (fail closed).
        return ({"kind": "unparsed", "cwd": None, "head": None, "commits": "commit" in cmd}
                if FALLBACK.search(cmd) else None)
    cwd = None
    commits = False
    for words in _segments(tokens):
        i = 0
        while i < len(words) and (re.match(r"^[A-Za-z_]\w*=", words[i]) or words[i] in WRAPPERS):
            i += 1
        if i < len(words) and os.path.basename(words[i]) == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or "=" in words[i]):
                i += 1
        if i >= len(words):
            continue
        prog, args = os.path.basename(words[i]), words[i + 1:]
        if prog == "cd" and args:
            cwd = args[0]
        elif prog == "git" and "commit" in [a for a in args if not a.startswith("-")][:3]:
            commits = True
        elif prog in SHELLS and "-c" in args and depth < 3:
            j = args.index("-c")
            if j + 1 < len(args):
                inner = _scan(args[j + 1], depth + 1)
                if inner:
                    inner["cwd"] = inner["cwd"] or cwd
                    inner["commits"] = inner["commits"] or commits
                    return inner
        elif prog == "eval" and args and depth < 3:
            inner = _scan(" ".join(args), depth + 1)
            if inner:
                inner["cwd"] = inner["cwd"] or cwd
                inner["commits"] = inner["commits"] or commits
                return inner
        elif prog == "gh" and args:
            if args[0] == "pr" and len(args) > 1 and args[1] in ("create", "new"):
                head = _flag_value(args, ("--head", "-H"))
                return {"kind": "gh-pr-create", "cwd": cwd, "head": head, "commits": commits}
            if args[0] == "api" and _gh_api_opens_pr(args[1:]):
                return {"kind": "gh-api-pulls", "cwd": cwd, "head": None, "commits": commits}
    return None


def pr_open_call(tool_name: str, tool_input: object) -> dict | None:
    """{"kind", "cwd", "head", "commits"} when this tool call opens a PR, else None.
    `commits`: the same command runs `git commit` before the PR open, so the
    gate (which runs first) must also judge what that commit will add."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "mcp__github__create_pull_request":
        head = tool_input.get("head")
        return {"kind": "mcp", "cwd": None, "head": head if isinstance(head, str) else None,
                "commits": False}
    if tool_name != "Bash":
        return None
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or "gh" not in cmd:
        return None
    return _scan(cmd, 0)


def call_key(tool_name: str, tool_input: dict) -> str:
    """What a grant is bound to: the exact command, or the exact MCP input."""
    if tool_name == "Bash":
        body = str(tool_input.get("command", ""))
    else:
        body = json.dumps(tool_input, sort_keys=True)
    return sha256(tool_name + "\0" + body)


# --- evidence ------------------------------------------------------------------

PATH_TOKEN = re.compile(r"(?<![\w.~$-])(~?/[^\s'\"`,;()<>|*]+)")
SUSPECT = (
    re.compile(r"\b(?:return|respond|reply|answer|output|emit|give|say|mark|grade|"
               r"rate|approve)\b[^.\n]{0,40}\bPASS(?:_WITH_FIXES)?\b", re.I),
    re.compile(r"\bverdict\b[^.\n]{0,15}\b(?:should|must|will)\s+be\b[^.\n]{0,10}\bPASS",
               re.I),
    re.compile(r"[\"']verdict[\"']\s*:\s*[\"']PASS", re.I),
)
FENCE = re.compile(r"^\s*(?:```|~~~)")
DIFF_LINE = re.compile(r"^(?:[+-]|@@|diff --git |index [0-9a-f])")


def prose_of(prompt: str) -> str:
    """The prompt minus fenced blocks and diff lines: the instructions, not the
    code under review (a diff of verdict-handling code says "PASS" plenty).
    A space-led line is a diff context line only inside a diff region."""
    out, fenced, in_diff = [], False, False
    for line in prompt.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if DIFF_LINE.match(line):
            in_diff = True
            continue
        if in_diff and line.startswith(" "):
            continue
        in_diff = False
        out.append(line)
    return "\n".join(out)


def suspect_prompt(prompt: str) -> bool:
    prose = prose_of(prompt)
    return any(rx.search(prose) for rx in SUSPECT)


def parse_verdict(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("\n") + 1:] if "\n" in t else t
    candidates = [t]
    if "{" in t and "}" in t:
        candidates.append(t[t.find("{"): t.rfind("}") + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("verdict") in VERDICTS:
            return obj["verdict"]
    return None


def is_review_prompt(prompt: object) -> bool:
    return isinstance(prompt, str) and not prompt.lstrip().startswith(NOT_REVIEWS)


def mentioned(prompt: str, rel: str) -> bool:
    return re.search(r"(?<![\w.-])" + re.escape(rel) + r"(?![\w-])(?!\.\w)", prompt) is not None


def prompt_paths(prompt: str) -> list[str]:
    out = []
    for m in PATH_TOKEN.finditer(prompt):
        p = os.path.expanduser(m.group(1).rstrip(".,:;!?'\""))
        if p not in out:
            out.append(p)
        if len(out) >= MAX_PROMPT_PATHS:
            break
    return out


def repos_in_prompt(prompt: str, cwd: str | None) -> list[dict]:
    """Every checkout the dispatch names a path in; the session cwd's if none."""
    seen_dirs: dict[str, dict | None] = {}
    repos: dict[str, dict] = {}
    for p in prompt_paths(prompt):
        d = Path(p)
        while not d.exists() and d != d.parent:
            d = d.parent
        key = str(d if d.is_dir() else d.parent)
        if key == "/" or len(seen_dirs) >= 30 and key not in seen_dirs:
            continue
        if key not in seen_dirs:
            seen_dirs[key] = repo_info(key)
        info = seen_dirs[key]
        if info:
            repos.setdefault(info["top"], info)
    if not repos and cwd:
        info = repo_info(cwd)
        if info:
            repos[info["top"]] = info
    return list(repos.values())


def review_snapshot(prompt: str, cwd: str | None) -> list[dict]:
    """Per checkout: what the reviewer was looking at, and which of the
    branch's changed files the dispatch named."""
    out = []
    for info in repos_in_prompt(prompt, cwd):
        top = info["top"]
        head = git(top, "rev-parse", "HEAD")
        tree = snapshot_tree(top)
        if not head or not tree:
            continue
        base = base_ref(top)
        mb = git(top, "merge-base", base, head) if base else None
        changed = [p for p, _ in (numstat(top, mb, tree) or [])] if mb else []
        named = set()
        for p in prompt_paths(prompt):
            try:
                rel = os.path.relpath(os.path.realpath(p), os.path.realpath(top))
            except ValueError:
                continue
            if rel != "." and not rel.startswith(".."):
                named.add(rel)
        named |= {p for p in changed if mentioned(prompt, p)}
        out.append({"repo": info["repo"], "top": top, "branch": current_branch(top),
                    "head": head, "tree": tree, "files": sorted(named)})
    return out


# --- the decision ---------------------------------------------------------------

def is_doc(path: str) -> bool:
    return path.lower().endswith(DOC_SUFFIXES)


def is_sensitive(path: str) -> bool:
    parts = path.split("/")
    return "hooks" in parts[:-1] or "scripts" in parts[:-1] or parts[-1] == "settings.json"


def computed_exemption(changes: list[tuple[str, int | None]]) -> str | None:
    """Why this diff needs no ARTL review, or None. Computed from the diff
    alone; nothing the agent says enters into it."""
    if not changes:
        return "empty diff"
    if all(is_doc(p) for p, _ in changes):
        return f"docs-only ({len(changes)} file(s))"
    total = line_total(changes, SMALL_LIMIT + 1)
    if total <= SMALL_LIMIT and not any(is_sensitive(p) for p, _ in changes):
        return (f"{total} changed line(s), none under hooks/, scripts/ "
                f"or in settings.json")
    return None


def tree_rows(top: str, tree: object, tip: str, cache: dict) -> dict[str, int] | None:
    """{path: lines changed} from a reviewed tree to the tip; None if unknown."""
    if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        return None
    if tree not in cache:
        rows = numstat(top, tree, tip)
        cache[tree] = None if rows is None else {
            p: (DRIFT_LIMIT + 1 if n is None else n) for p, n in rows}
    return cache[tree]


def drift(top: str, tree: object, tip: str, cache: dict, pr_paths: set[str]) -> int | None:
    """Lines changed between the reviewed tree and the tip, counted only on the
    PR's own paths. A path outside the PR diff is identical at the tip and the
    merge base, so a difference there is untracked build junk the snapshot
    picked up (measured: __pycache__/*.pyc) or reviewed work since reverted --
    neither is unreviewed code in the PR."""
    rows = tree_rows(top, tree, tip, cache)
    return None if rows is None else sum(n for p, n in rows.items() if p in pr_paths)


def evidence_problem(records: list[dict], info: dict, branch: str, tip: str,
                     changes: list[tuple[str, int | None]]) -> str | None:
    """None when the recorded reviews clear this branch, else why not."""
    mine = [r for r in records if r.get("repo") == info["repo"] and r.get("branch") == branch]
    if not mine:
        return (f"no advisor verdict is recorded for branch {branch!r} "
                f"(advice-refine-test-loop has not reviewed it)")
    latest = max(mine, key=lambda r: r.get("ts", 0))
    if latest.get("suspect"):
        return ("the latest advisor dispatch for this branch is marked suspect: its "
                "prompt asked for a PASS verdict, so its verdict is not evidence")
    if latest.get("verdict") not in PASSING:
        return (f"the latest advisor verdict for this branch is {latest.get('verdict')}"
                f" ({str(latest.get('agent_id'))}); a BLOCKS verdict needs its fixes "
                f"and a fresh review")
    cache: dict = {}
    pr_paths = {p for p, _ in changes}
    d = drift(info["top"], latest.get("tree"), tip, cache, pr_paths)
    if d is None or d > DRIFT_LIMIT:
        return (f"the code has drifted {'an unknown number of' if d is None else d} "
                f"line(s) from the tree the latest passing review saw "
                f"(limit {DRIFT_LIMIT}); the review no longer describes this diff")
    # Coverage is per file: a file counts as reviewed when some non-suspect
    # review named it and THAT file has changed at most DRIFT_LIMIT lines since.
    # Measured in e2e: after a round of fixes the follow-up review names only
    # the files it touched, and whole-tree drift would orphan the rest.
    covered: set[str] = set()
    for r in mine:
        rows = None if r.get("suspect") else tree_rows(info["top"], r.get("tree"), tip, cache)
        if rows is None:
            continue
        covered.update(f for f in r.get("files") or []
                       if isinstance(f, str) and rows.get(f, 0) <= DRIFT_LIMIT)
    missing = sorted(p for p, _ in changes if p not in covered)
    if missing:
        shown = ", ".join(missing[:15]) + (" …" if len(missing) > 15 else "")
        return (f"{len(missing)} changed file(s) were never named in an advisor "
                f"dispatch: {shown}")
    return None


# --- nixos-agent-testing evidence (nixos-config only) -----------------------------

NIXOS_REMOTE = re.compile(r"[:/]jonathanmoregard/nixos-config(?:\.git)?/?$")
SMOKE_PATH = re.compile(
    r"service|daemon|timer|watchdog|health|systemd|script|\.sh$|\.py$"
    r"|cinnamon|desktop|kitty|ghostty|lightdm|xfce|xsession|x11|dconf|gui"
    r"|wallpaper|vscodium|theme|applet|launch", re.I)
SMOKE_CONTENT = re.compile(
    r"systemd\.(?:user\.)?(?:services|timers)\.|writeShellApplication"
    r"|writeShellScript|ExecStart|activationScripts")
CHECKLIST_HEADER = re.compile(r"^Pre-push checklist:\s*$")
SMOKE_FIELD = re.compile(r"^-\s*interactive smoke \(nixos-agent-testing\)\s*:\s*(.*)$", re.I)
NA_CLAIM = re.compile(r"^n/?a\b\s*[:—–-]+\s*(\S.{3,})", re.I | re.S)
ARTEFACT = re.compile(r"(?<![\w$])((?:~|\.{1,2})?/?[\w.@+-]+(?:/[\w.@+-]+)+)")


def is_nixos_config(top: str) -> bool:
    urls = git(top, "config", "--get-regexp", r"^remote\..*\.url$") or ""
    return any(NIXOS_REMOTE.search(line.split(" ", 1)[-1]) for line in urls.splitlines())


def smoke_triggers(top: str, mb: str, tip: str,
                   changes: list[tuple[str, int | None]]) -> list[str]:
    hits = [p for p, _ in changes
            if not p.startswith(("tests/", "docs/")) and SMOKE_PATH.search(p.rsplit("/", 1)[-1])]
    if hits:
        return hits
    patch = git(top, "diff", "--no-renames", "-U0", mb, tip, "--", ".",
                ":(exclude)tests", ":(exclude)docs") or ""
    added = "\n".join(l for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    m = SMOKE_CONTENT.search(added)
    return [f"added line with {m.group(0)}"] if m else []


def smoke_field(message: str) -> str | None:
    lines = message.splitlines()
    in_block = False
    for i, line in enumerate(lines):
        if CHECKLIST_HEADER.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        m = SMOKE_FIELD.match(line)
        if m:
            value = [m.group(1).strip()]
            for cont in lines[i + 1:]:
                if cont[:1] in (" ", "\t") and cont.strip():
                    value.append(cont.strip())
                else:
                    break
            return " ".join(v for v in value if v)
    return None


def smoke_problem(top: str, mb: str, tip: str,
                  changes: list[tuple[str, int | None]]) -> tuple[str | None, dict | None]:
    """(problem or None, exemption-log entry or None) for nixos-config."""
    if not is_nixos_config(top):
        return None, None
    triggers = smoke_triggers(top, mb, tip, changes)
    if not triggers:
        return None, None
    why = ", ".join(triggers[:5])
    value = smoke_field(git(top, "log", "-1", "--format=%B", tip) or "")
    head = (f"this nixos-config change touches services, scripts or GUI modules "
            f"({why}), so the HEAD commit's `Interactive smoke (nixos-agent-testing)` "
            f"checklist field must name an artefact that exists on disk, or say "
            f"`n/a: <reason>`")
    if not value or value.startswith("<"):
        return head + "; the field is missing or unfilled", None
    na = NA_CLAIM.match(value)
    if na:
        return None, {"kind": "nixos-smoke-na", "why": na.group(1).strip()[:400],
                      "triggers": triggers[:20]}
    for m in ARTEFACT.finditer(value):
        token = m.group(1).rstrip(".,:;")
        p = Path(os.path.expanduser(token))
        if not p.is_absolute():
            p = Path(top) / p
        if p.is_file():
            return None, None
    return head + f"; no file named in {value[:160]!r} exists", None
