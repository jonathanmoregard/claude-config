"""Shared logic for the ARTL PR-open gate.

Imported by hooks/pr-open-gate.py (PreToolUse, PermissionRequest,
UserPromptSubmit) and hooks/artl-evidence.py (PostToolUse on Agent, Read and
SendMessage; SubagentStop). Stdlib only; every git call is bounded.

ENFORCEMENT MODEL
  settings.json `permissions.ask` covers every PR-open route the gate knows
  of: `gh pr create` / `gh pr new` however spaced or quoted, any `gh api` call
  that can POST, GraphQL `createPullRequest`, `gh alias`, and the GitHub MCP
  create tool. `Bash` is on the allow list, so that ask rule -- not this code
  -- is what stops an unreviewed PR open: a parser miss, a hook timeout, a
  crash, or a hook that is not installed all leave the prompt standing.
  This module's one privilege is to ANSWER that prompt "allow", through a
  one-shot grant bound to the exact call, after judging that call. It may
  also deny with a remedy, but nothing depends on the deny firing.
  So every judgement here is built to be wrong only toward a prompt: when the
  gate cannot be sure what a command opens, or against what, it refuses to
  grant (and says why).

WHERE THINGS LIVE -- all under ~/.local/state/claude-pr-open-gate/, which
settings.json denies to the Edit and Write tools and to any Bash command
naming it (a directory, not file names, so a renamed file moved in is caught
by the same rule):
  records/<name>/artl-evidence.jsonl    one line per advisor verdict
  records/<name>/artl-exemptions.jsonl  every PR open let through WITHOUT a
                                        passing review: computed exemptions,
                                        the user's skip-artl, nixos `n/a`
  pending/   advisor dispatch halves, and the files each advisor Read
  grants/    one-shot answers for the permissions.ask prompt
  override/  a user-typed skip-artl, per session, until consumed

<name> follows CLAUDE.md's <notes> naming: the basename of the session's
primary working directory ($CLAUDE_PROJECT_DIR), a leading dot rewritten to
`dot-`. The writer uses exactly that. The gate also reads the records named
after the checkout it inspects and after that checkout's primary clone, since
a review in one session may be followed by a PR opened from another. Records
carry the repository's git-common-dir, so reading more directories never
mixes repositories up.

Records are tamper-EVIDENT, not tamper-proof: anything a hook can write, the
same uid can write. The deny rules stop the agent's file tools and any shell
command that names the directory; a command that builds the path at runtime
is not stopped.
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
LS_REMOTE_TIMEOUT_S = 6
SNAPSHOT_TIMEOUT_S = 10
DRIFT_LIMIT = 20          # lines changed since the reviewed tree that still count
SMALL_LIMIT = 15          # a change this small, outside SENSITIVE paths, is exempt
GRANT_TTL_S = 120
OVERRIDE_TTL_S = 24 * 3600
PENDING_TTL_S = 24 * 3600
MAX_READ_BYTES = 4_000_000
MAX_PROMPT_PATHS = 200
MAX_READS = 500
MAX_QUOTE_FILES = 150
MAX_QUOTE_BYTES = 3_000_000

EVIDENCE_FILE = "artl-evidence.jsonl"
EXEMPTIONS_FILE = "artl-exemptions.jsonl"
VERDICTS = ("PASS", "PASS_WITH_FIXES", "BLOCKS")
PASSING = ("PASS", "PASS_WITH_FIXES")
NOT_REVIEWS = ("CONSULT dispatch", "VALIDATION dispatch")

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SHA_RE = re.compile(r"[0-9a-f]{40,64}")


# --- locations ---------------------------------------------------------------

def state_dir() -> Path:
    override = os.environ.get("CLAUDE_PR_OPEN_GATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "claude-pr-open-gate"


def records_dir(name: str) -> Path:
    return state_dir() / "records" / name


def notes_name(path: Path) -> str:
    base = path.name
    return "dot-" + base[1:] if base.startswith(".") else base


def session_records(payload: dict) -> Path:
    """This session's records directory (named like CLAUDE.md's <notes>)."""
    project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    return records_dir(notes_name(Path(project)))


def safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and ID_RE.match(value) else None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# --- git ---------------------------------------------------------------------

def git(cwd: str | Path, *args: str, env: dict | None = None,
        timeout: float = GIT_TIMEOUT_S) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                              text=True, errors="replace", timeout=timeout,
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


def read_records_dirs(payload: dict, info: dict) -> list[Path]:
    names = [session_records(payload).name, notes_name(Path(info["top"])),
             notes_name(primary_dir(info["repo"]))]
    out: list[Path] = []
    for name in names:
        d = records_dir(name)
        if d not in out:
            out.append(d)
    return out


def remote_base(top: str) -> tuple[str | None, str]:
    """(commit, problem): the default branch's tip AS THE REMOTE REPORTS IT.

    No local ref decides the base. refs/remotes/origin/HEAD, origin/main and
    every branch are one `git symbolic-ref` / `git update-ref` away from
    pointing at the PR's own tip, which turns any diff into an empty,
    exempt one (finding 8). The remote's answer costs a network round trip;
    when it cannot be had, there is no grant -- the ask prompt stands."""
    out = git(top, "ls-remote", "--symref", "origin", "HEAD", "refs/heads/main",
              "refs/heads/master", timeout=LS_REMOTE_TIMEOUT_S)
    if out is None:
        return None, (f"could not ask the remote `origin` of {top} for its default "
                      f"branch (git ls-remote failed), and a local ref is not trusted "
                      f"as the base")
    shas: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and SHA_RE.fullmatch(parts[0]):
            shas[parts[1]] = parts[0]
    sha = shas.get("HEAD") or shas.get("refs/heads/main") or shas.get("refs/heads/master")
    if not sha:
        return None, "the remote `origin` reports no default branch"
    if git(top, "cat-file", "-e", sha + "^{commit}") is None:
        return None, (f"the remote default branch is at {sha[:12]}, which this checkout "
                      f"does not have; run `git fetch origin` and retry")
    return sha, ""


def local_base_ref(top: str) -> str | None:
    """A LOCAL guess at the base. Untrusted: used only to pick which file
    versions count as quotable code in a dispatch prompt (suspect_prompt)."""
    head = git(top, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    for ref in ([head] if head else []) + ["origin/main", "origin/master", "main", "master"]:
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


def snapshot_tree(top: str, untracked: bool = True) -> str | None:
    """The tree of the working state: HEAD plus staged and unstaged changes,
    and untracked (not ignored) files when `untracked`. Written through a
    throwaway index, so the real index and the stash are never touched.
    None when the tree cannot be built: a caller must not mistake HEAD for
    the working state."""
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
            return None
        if git(top, "add", "-A" if untracked else "-u", env=env,
               timeout=SNAPSHOT_TIMEOUT_S) is None:
            return None
        return git(top, "write-tree", env=env)


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
#
# The scanner's job is narrower than it looks. A miss costs a prompt (the ask
# rule still matches), so it does not have to find every spelling. What it
# must never do is mint a grant for a command whose PR open it misjudged: the
# wrong branch, the wrong repository, a second open it did not see, or a
# commit/checkout earlier in the same command it did not account for. So it
# finds a `gh` word ANYWHERE in a segment (wrappers, keywords, xargs), keeps
# `#` literal (the shell only comments at word start; shlex does not know
# that), and reports a `problem` for anything it cannot follow.

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;", "\n"}
PIPES = {"|", "|&"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
            "case", "esac", "{", "}", "!", "[[", "]]", "time"}
WRAPPERS = {"command", "exec", "nohup", "builtin", "sudo", "doas", "setsid", "nice",
            "ionice", "stdbuf", "chronic", "unbuffer", "env", "timeout", "xargs"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
# Before the PR open, in the same command, only these may run: anything else
# could move the branch, edit what a same-command commit captures, or
# retarget gh, and the gate (which runs first) would judge the wrong thing.
PRE_SAFE = {"cd", "git", "gh", "echo", "printf", "true", "false", ":", "sleep",
            "test", "[", "pwd", "ls", "cat", "set", "export", "unset", "wait"}
GIT_READ = {"status", "log", "diff", "show", "fetch", "rev-parse", "ls-files",
            "ls-remote", "describe", "shortlog", "grep", "blame", "cat-file",
            "merge-base", "tag", "push", "branch", "remote", "config", "version"}
GIT_STAGE = {"add", "stage", "rm", "mv", "commit"}
GH_READ = {("auth", "status"), ("pr", "view"), ("pr", "list"), ("pr", "status"),
           ("pr", "checks"), ("pr", "diff"), ("repo", "view"), ("run", "list"),
           ("run", "view")}
API_VALUE_FLAGS = {"-H", "--header", "-q", "--jq", "-t", "--template", "-p",
                   "--preview", "--cache", "--hostname"}
REST_PULLS = re.compile(r"^repos/([^/]+)/([^/]+)/pulls/?$")
HEREDOC = re.compile(r"(?<!<)<<-?[ \t]*(\\?)(['\"]?)([A-Za-z_][\w-]*)\2")
ARITH = re.compile(r"\(\(.*?\)\)")
GH_ENV = re.compile(r"^(GH_REPO|GH_HOST|GH_ENTERPRISE_TOKEN|GITHUB_TOKEN|GH_TOKEN)=(.*)$",
                    re.S)


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies that are pure data: a quoted delimiter (no
    expansion at all), or an unquoted one whose body holds no command
    substitution. A body is dropped only when its terminator line exists;
    `$((1<<n))` is arithmetic, not a heredoc."""
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(ARITH.sub(lambda a: " " * len(a.group(0)), line)):
            quoted = bool(m.group(1) or m.group(2))
            delim = m.group(3)
            end = next((j for j in range(i, len(lines)) if lines[j].strip() == delim), None)
            if end is None:
                continue
            body = "\n".join(lines[i:end])
            if quoted or ("$(" not in body and "`" not in body):
                i = end + 1         # body and terminator are data
                out.append("")      # keep a line break where they were
    return "\n".join(out)


def _tokens(cmd: str) -> list[str]:
    lex = shlex.shlex(strip_heredocs(cmd), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _segments(tokens: list[str]) -> list[dict]:
    """Simple commands, each {words, depth, before, after, writes}: the paren
    depth it starts at, the separators around it, output-redirect targets."""
    segs: list[dict] = []
    cur = {"words": [], "depth": 0, "before": None, "writes": []}
    depth = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in SEPARATORS or (tok and set(tok) <= set(";&|()\n")):
            cur["after"] = tok
            segs.append(cur)
            depth += tok.count("(") - tok.count(")")
            cur = {"words": [], "depth": depth, "before": tok, "writes": []}
        elif tok and set(tok) <= set("<>&"):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if ">" in tok and not (tok.endswith("&") and re.fullmatch(r"\d+|-", target)) \
                    and target not in ("/dev/null", "/dev/stdout", "/dev/stderr"):
                cur["writes"].append(target)
            i += 1                   # the next token is the redirect's target
        else:
            cur["words"].append(tok)
        i += 1
    cur["after"] = None
    segs.append(cur)
    return [s for s in segs if s["words"]]


def _program(words: list[str]) -> tuple[str | None, list[str]]:
    """The program a simple command runs, past assignments, keywords and
    known wrappers (with their own options), and its arguments."""
    i = 0
    while i < len(words):
        w = words[i]
        b = os.path.basename(w)
        if w.startswith("#"):
            return None, []          # a comment line: the shell runs nothing
        if ASSIGNMENT.match(w) or w in KEYWORDS:
            i += 1
        elif b in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or ASSIGNMENT.match(words[i])
                                      or (b == "timeout" and re.fullmatch(r"[\d.]+[smhd]?", words[i]))
                                      or (b == "nice" and re.fullmatch(r"-?\d+", words[i]))):
                i += 1
        else:
            return b, words[i + 1:]
    return None, []


def _opt_value(args: list[str], short: str | None, long: str) -> str | None:
    for i, a in enumerate(args):
        if a == long or (short and a == short):
            return args[i + 1] if i + 1 < len(args) else ""
        if a.startswith(long + "="):
            return a.split("=", 1)[1]
        if short and a.startswith(short) and len(a) > len(short) and not a.startswith("--"):
            return a[len(short):]
    return None


def _gh_api(args: list[str]) -> dict | None:
    """The PR open a `gh api` call makes, or None when it makes none."""
    method = None
    fields: dict[str, str | None] = {}
    inputs: list[str] = []
    endpoint = None
    host = None
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a in ("-X", "--method"):
            method = (nxt or "").upper()
            i += 2
            continue
        if a in ("-f", "-F", "--field", "--raw-field"):
            _field(fields, a, nxt)
            i += 2
            continue
        if a == "--input":
            inputs.append(nxt or "")
            i += 2
            continue
        if a == "--hostname":
            host = nxt
            i += 2
            continue
        if a in API_VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a.startswith(("--field=", "--raw-field=")):
            flag, _, kv = a.partition("=")
            _field(fields, flag, kv)
        elif a.startswith("--input="):
            inputs.append(a.split("=", 1)[1])
        elif re.match(r"^-[fF].", a):
            _field(fields, a[:2], a[2:])
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
        elif not a.startswith("-") and endpoint is None:
            endpoint = a
        i += 1
    if endpoint is None:
        return None
    ep = re.sub(r"^https?://[^/]+/", "", endpoint)
    ep = re.sub(r"^api/v3/", "", ep.lstrip("/")).split("?", 1)[0].split("#", 1)[0]
    posts = (method or ("POST" if fields or inputs else "GET")) == "POST"
    if not posts:
        return None
    if ep == "graphql":
        if not any("createPullRequest" in a for a in args):
            return None
        return {"kind": "gh-api-graphql", "problem": (
            "a GraphQL createPullRequest names its repository by node id, which the "
            "gate cannot check against this checkout; open the PR with `gh pr create`")}
    m = REST_PULLS.match(ep)
    if not m:
        return None
    owner, repo = m.groups()
    call = {"kind": "gh-api-pulls", "repo": None, "head": None, "input": None,
            "problem": None}
    if not ({owner, repo} & {"{owner}", "{repo}", ":owner", ":repo"}):
        call["repo"] = f"{owner}/{repo}"
    if host and host.lower() not in ("github.com", "api.github.com"):
        call["problem"] = f"the call names another GitHub host ({host})"
    head = fields.get("head", "")
    if head is None:
        call["problem"] = "the head field is read from a file (-F head=@...)"
    elif head:
        call["head"] = head
    elif inputs:
        if inputs[-1] in ("", "-"):
            call["problem"] = "the request body is read from stdin, so its head is unknown"
        else:
            call["input"] = inputs[-1]
    else:
        call["problem"] = "the call names no head branch"
    return call


def _field(fields: dict, flag: str, kv: str | None) -> None:
    if kv is None:
        return
    key, _, val = kv.partition("=")
    fields[key] = None if flag in ("-F", "--field") and val.startswith("@") else val


def _gh_pr_create(args: list[str]) -> dict:
    head = _opt_value(args, "-H", "--head")
    repo = _opt_value(args, "-R", "--repo")
    return {"kind": "gh-pr-create", "head": head or None, "repo": repo or None,
            "input": None, "problem": None}


def _pre_unsafe(seg: dict) -> tuple[str | None, bool, bool]:
    """(why this earlier segment makes the PR open unjudgeable, commits, adds)."""
    words = seg["words"]
    prog, args = _program(words)
    for w in words:
        m = GH_ENV.match(w)
        if m and m.group(1) != "GH_REPO":      # GH_REPO is read as the target repo
            return f"it sets {m.group(1)} before opening the PR", False, False
    if prog is None:
        return None, False, False
    if prog not in PRE_SAFE:
        return f"it runs `{prog}` before opening the PR", False, False
    if prog == "cd":
        return None, False, False
    if prog == "gh":
        if tuple(a for a in args if not a.startswith("-"))[:2] in GH_READ:
            return None, False, False
        return f"it runs `gh {' '.join(args[:2])}` before opening the PR", False, False
    if prog != "git":
        return None, False, False
    j = 0
    while j < len(args) and args[j].startswith("-"):
        if args[j] in ("--git-dir", "--work-tree") or args[j].startswith(("--git-dir=", "--work-tree=")):
            return "it points git at another directory before opening the PR", False, False
        j += 2 if args[j] in ("-C", "-c") else 1
    sub, rest = (args[j], args[j + 1:]) if j < len(args) else (None, [])
    if sub in GIT_STAGE:
        return None, sub == "commit", sub in ("add", "stage")
    if sub not in GIT_READ:
        return f"it runs `git {sub}` before opening the PR", False, False
    positional = [a for a in rest if not a.startswith("-")]
    if sub == "push" and any(":" in r and r.split(":", 1)[0].lstrip("+") not in ("HEAD", r.split(":", 1)[1])
                             for r in positional[1:]):
        return "it pushes another ref to the PR branch before opening", False, False
    if sub == "branch" and positional:
        return "it runs `git branch` with arguments before opening the PR", False, False
    if sub == "remote" and positional and positional[0] not in ("show", "get-url"):
        return f"it runs `git remote {positional[0]}` before opening the PR", False, False
    if sub == "config" and not any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l")
                                   for a in rest):
        return "it writes git config before opening the PR", False, False
    return None, False, False


def _scan(cmd: str, depth: int, cwd: str | None) -> list[dict]:
    try:
        tokens = _tokens(cmd)
    except ValueError:
        if re.search(r"gh\W+(?:pr\W+(?:create|new)|api)|createPullRequest", cmd):
            return [{"kind": "unparsed", "cwd": cwd, "head": None, "repo": None,
                     "input": None, "commits": False, "adds": False, "writes": [],
                     "problem": "the command could not be parsed (unbalanced quoting)"}]
        return []
    opens: list[dict] = []
    commits = adds = False
    writes: list[str] = []
    unsafe: str | None = None
    cwd_known = True
    env_repo = None
    for seg in _segments(tokens):
        words = seg["words"]
        found: list[dict] = []
        for k, w in enumerate(words):
            b = os.path.basename(w)
            rest = words[k + 1:]
            if b == "gh" and rest[:1] == ["api"]:
                hit = _gh_api(rest[1:])
                if hit:
                    found.append(hit)
            elif b == "gh" and len(rest) > 1 and rest[0] == "pr" and rest[1] in ("create", "new"):
                found.append(_gh_pr_create(rest[2:]))
            elif b in SHELLS and "-c" in rest and depth < 3:
                j = rest.index("-c")
                if j + 1 < len(rest):
                    found.extend(_scan(rest[j + 1], depth + 1, cwd))
            elif b == "eval" and rest and depth < 3:
                found.extend(_scan(" ".join(rest), depth + 1, cwd))
            elif ("$(" in w or "`" in w) and depth < 3:
                inner = w.replace("$(", " ( ").replace("`", " ; ")
                found.extend(_scan(inner, depth + 1, cwd))
            m = GH_ENV.match(w)
            if m and m.group(1) == "GH_REPO" and m.group(2):
                env_repo = m.group(2)
        for hit in found:
            hit.setdefault("cwd", cwd)
            hit.setdefault("commits", False)
            hit.setdefault("adds", False)
            hit.setdefault("writes", [])
            hit["commits"] = hit["commits"] or commits
            hit["adds"] = hit["adds"] or adds
            hit["writes"] = writes + hit["writes"]
            if hit["cwd"] is None:
                hit["cwd"] = cwd
            if env_repo and not hit.get("repo"):
                hit["repo"] = env_repo
            if not hit.get("problem"):
                if unsafe:
                    hit["problem"] = (f"this command cannot be judged as written: {unsafe}; "
                                      "run that step on its own first")
                elif not cwd_known:
                    hit["problem"] = ("a `cd` in this command runs in a subshell, pipeline or "
                                      "background job, so the checkout it opens from is unknown")
            opens.append(hit)
        if found:
            continue
        # Not a PR open: account for what it does before a later one.
        prog, args = _program(words)
        if prog == "cd":
            plain = seg["depth"] == 0 and seg["before"] not in PIPES | {"&"} \
                and seg.get("after") not in PIPES | {"&"}
            if not plain or not args or args[0] == "-":
                cwd_known = False
            else:
                cwd = args[0]
        elif prog in ("pushd", "popd"):
            cwd_known = False
        why, c, a = _pre_unsafe(seg)
        unsafe = unsafe or why
        commits = commits or c
        adds = adds or a
        writes.extend(seg["writes"])
    return opens


def pr_open_call(tool_name: str, tool_input: object) -> dict | None:
    """The PR open this tool call makes, or None.

    {"kind", "cwd", "head", "repo", "input", "commits", "adds", "writes",
     "count", "problem"}. `commits`/`adds`: the same command runs `git commit`
    / `git add` before the open, so the gate (which runs first) must judge
    what that commit will add. `problem`: why the call cannot be judged at
    all -- the gate denies it and mints no grant. A command with more than
    one PR open is a problem: one grant must cover one judged open."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "mcp__github__create_pull_request":
        head = tool_input.get("head")
        owner, repo = tool_input.get("owner"), tool_input.get("repo")
        return {"kind": "mcp", "cwd": None, "head": head if isinstance(head, str) else None,
                "repo": f"{owner}/{repo}" if isinstance(owner, str) and isinstance(repo, str)
                else None, "input": None, "commits": False, "adds": False, "writes": [],
                "count": 1, "problem": None if isinstance(head, str) and head
                else "the MCP call names no head branch"}
    if tool_name != "Bash":
        return None
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or "gh" not in cmd and "createPullRequest" not in cmd:
        return None
    opens = _scan(cmd, 0, None)
    if not opens:
        return None
    call = dict(opens[0])
    call["count"] = len(opens)
    if len(opens) > 1:
        call["problem"] = (f"this command opens {len(opens)} PRs; one grant covers one "
                           "judged PR open, so run each on its own")
    return call


def call_key(tool_name: str, tool_input: dict) -> str:
    """What a grant is bound to: the exact command, or the exact MCP input."""
    if tool_name == "Bash":
        body = str(tool_input.get("command", ""))
    else:
        body = json.dumps(tool_input, sort_keys=True)
    return sha256(tool_name + "\0" + body)


GITHUB_URL = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)


def checkout_repos(top: str) -> set[str]:
    """owner/repo (lower case) of every GitHub remote of the checkout."""
    urls = git(top, "config", "--get-regexp", r"^remote\..*\.url$") or ""
    out = set()
    for line in urls.splitlines():
        m = GITHUB_URL.search(line.split(" ", 1)[-1])
        if m:
            out.add(f"{m.group(1)}/{m.group(2)}".lower())
    return out


def target_problem(top: str, repo: str | None, head: str | None) -> str | None:
    """An explicit target repository, or an `owner:` head, that is not this
    checkout's: the gate would be judging a different repository's branch."""
    mine = checkout_repos(top)
    if repo and repo.lower() not in mine:
        return (f"the PR open targets {repo}, but the checkout at {top} has no remote "
                f"for it ({', '.join(sorted(mine)) or 'no GitHub remote'})")
    if head and ":" in head:
        owner = head.split(":", 1)[0].lower()
        owners = {r.split("/", 1)[0] for r in ([repo.lower()] if repo else mine)}
        if owner not in owners:
            return f"the head {head!r} is a branch in another owner's fork"
    return None


# --- evidence ------------------------------------------------------------------

PATH_TOKEN = re.compile(r"(?<![\w.~$-])(~?/[^\s'\"`,;()<>|*]+)")
SUSPECT = (
    re.compile(r"\b(?:return|respond|reply|answer|output|emit|give|say|mark|grade|"
               r"rate|approve)\b[^.\n]{0,40}\bPASS(?:_WITH_FIXES)?\b", re.I),
    re.compile(r"\bverdict\b[^.\n]{0,15}\b(?:should|must|will)\s+be\b[^.\n]{0,10}\bPASS",
               re.I),
    re.compile(r"[\"']verdict[\"']\s*:\s*[\"']PASS", re.I),
)


def suspect_prompt(prompt: str, quoted: set[str] | None = None) -> bool:
    """Does any line of the prompt ask for a PASS?

    Every line is screened, whatever its shape: a bullet, a `+`/`-` line or a
    fenced block is as easy to write as prose (finding 10). A matching line is
    excused only when it is verbatim code from the change under review --
    `quoted` holds the stripped lines of the changed files at the reviewed
    tree and at the base -- because reviewing verdict-handling code puts
    "return ... PASS" in an honest capsule."""
    for line in prompt.splitlines():
        if not any(rx.search(line) for rx in SUSPECT):
            continue
        keys = {line.strip()}
        if line[:1] in "+- ":
            keys.add(line[1:].strip())
        if quoted and keys & quoted:
            continue
        return True
    return False


def quotable_lines(top: str, tree: str) -> set[str]:
    """Stripped lines of every file the branch changes, at `tree` and at the
    (local, untrusted) base: what an honest prompt may quote verbatim."""
    base = local_base_ref(top)
    mb = git(top, "merge-base", base, "HEAD") if base else None
    if not mb:
        return set()
    rows = numstat(top, mb, tree) or []
    out: set[str] = set()
    budget = MAX_QUOTE_BYTES
    for path, _ in rows[:MAX_QUOTE_FILES]:
        for rev in (tree, mb):
            text = git(top, "cat-file", "blob", f"{rev}:{path}")
            if not text or budget <= 0:
                continue
            budget -= len(text)
            out.update(s for s in (l.strip() for l in text.splitlines()) if s)
    return out


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


def prompt_paths(prompt: str) -> list[str]:
    out = []
    for m in PATH_TOKEN.finditer(prompt):
        p = os.path.expanduser(m.group(1).rstrip(".,:;!?'\""))
        if p not in out:
            out.append(p)
        if len(out) >= MAX_PROMPT_PATHS:
            break
    return out


def repos_of(paths: list[str], cwd: str | None) -> list[dict]:
    """Every checkout holding one of `paths`; the cwd's if none."""
    seen_dirs: dict[str, dict | None] = {}
    repos: dict[str, dict] = {}
    for p in paths:
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


def snapshot(info: dict) -> dict | None:
    """What a reviewer dispatched now is looking at, in one checkout."""
    top = info["top"]
    head = git(top, "rev-parse", "HEAD")
    tree = snapshot_tree(top)
    if not head or not tree:
        return None
    return {"repo": info["repo"], "top": top, "branch": current_branch(top),
            "head": head, "tree": tree}


def covered_files(snap: dict, reads: list[str]) -> list[str]:
    """The files the advisor itself Read inside this checkout, repo-relative.
    Coverage comes from the advisor's own tool calls, never from the dispatch
    prompt the gated agent wrote (finding 6)."""
    top = os.path.realpath(snap["top"])
    out = set()
    for p in reads:
        try:
            rel = os.path.relpath(os.path.realpath(p), top)
        except ValueError:
            continue
        if rel != "." and not rel.startswith(".."):
            out.add(rel)
    return sorted(out)


# --- the decision ---------------------------------------------------------------

DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")
# Text an agent or a tool executes as behaviour. In a config repo a skill,
# an agent definition or a CLAUDE.md is code; `requirements.txt` and
# `CMakeLists.txt` are build inputs (finding 12).
INSTRUCTION_NAMES = {"claude.md", "claude.local.md", "agents.md", "gemini.md", "skill.md",
                     "copilot-instructions.md", ".cursorrules", "settings.json"}
BEHAVIOUR_DIRS = {"hooks", "scripts", "skills", "agents", "commands", "prompts", "rules",
                  "output-styles", "templates", "runtime", ".claude", ".github", ".codex",
                  ".cursor", "workflows", "bin"}


def is_behaviour(path: str) -> bool:
    parts = path.split("/")
    return parts[-1].lower() in INSTRUCTION_NAMES or \
        any(p.lower() in BEHAVIOUR_DIRS for p in parts[:-1])


def is_doc(path: str) -> bool:
    """Prose nothing executes: a doc suffix, outside every behaviour path."""
    return path.lower().endswith(DOC_SUFFIXES) and not is_behaviour(path)


def is_sensitive(path: str) -> bool:
    return is_behaviour(path)


def computed_exemption(changes: list[tuple[str, int | None]]) -> str | None:
    """Why this diff needs no ARTL review, or None. Computed from the diff
    alone; nothing the agent says enters into it."""
    if not changes:
        return "empty diff"
    if all(is_doc(p) for p, _ in changes):
        return f"docs-only ({len(changes)} file(s))"
    total = line_total(changes, SMALL_LIMIT + 1)
    if total <= SMALL_LIMIT and not any(is_sensitive(p) for p, _ in changes):
        return (f"{total} changed line(s), none in a behaviour path "
                f"(hooks/, scripts/, skills/, agents/, settings.json, CLAUDE.md, ...)")
    return None


def tree_rows(top: str, tree: object, tip: str, cache: dict) -> dict[str, int] | None:
    """{path: lines changed} from a reviewed tree to the tip; None if unknown."""
    if not isinstance(tree, str) or not SHA_RE.fullmatch(tree):
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


def _shown(paths: list[str]) -> str:
    return ", ".join(paths[:15]) + (" …" if len(paths) > 15 else "")


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
    # Coverage is per file, judged by the LATEST trustworthy review that read
    # it: that review must pass (a later BLOCKS supersedes an earlier PASS, and
    # a BLOCKS never covers -- finding 5), and the file must have changed at
    # most DRIFT_LIMIT lines since. Measured in e2e: after a round of fixes the
    # follow-up review reads only the files it touched, so a file an earlier
    # passing review read and nobody changed since stays covered.
    trusted = sorted((r for r in mine if not r.get("suspect")), key=lambda r: r.get("ts", 0))
    unread, blocked, changed = [], [], []
    for path in sorted(pr_paths):
        last = None
        for r in trusted:
            if path in (r.get("files") or []):
                last = r
        if last is None:
            unread.append(path)
        elif last.get("verdict") not in PASSING:
            blocked.append(path)
        else:
            rows = tree_rows(info["top"], last.get("tree"), tip, cache)
            if rows is None or rows.get(path, 0) > DRIFT_LIMIT:
                changed.append(path)
    problems = []
    if unread:
        problems.append(f"{len(unread)} changed file(s) no advisor has read: {_shown(unread)}")
    if blocked:
        problems.append(f"{len(blocked)} file(s) whose latest review was BLOCKS: "
                        f"{_shown(blocked)}")
    if changed:
        problems.append(f"{len(changed)} file(s) changed more than {DRIFT_LIMIT} lines "
                        f"since an advisor read them: {_shown(changed)}")
    return "; ".join(problems) or None


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
