#!/usr/bin/env python3
"""agent-e2e: drive a real headless agent through a scenario on a candidate config.

The agent-config counterpart of `nix run .#feature-vm`. A unit test proves a
script's branches; it does not prove an agent reading the new CLAUDE.md, skill
or hook text then BEHAVES differently. This harness installs a candidate
`~/.claude` into a throwaway HOME, runs `claude -p` there the way a user would
hit the situation, and grades what the agent actually did.

USAGE
    agent_e2e.py stage --config SRC --home DIR
    agent_e2e.py run SCENARIO --arm NAME=SRC [--arm NAME=SRC ...] [--n N]
                     [--jobs J] [--model M] [--budget USD] [--timeout S]
                     [--out DIR]

SRC is a directory (a worktree: its tracked + modified files are copied; a
plain directory: copied whole) or a git ref of this repository (`origin/master`
is the usual "before" arm).

SCENARIO is a directory holding:
    prompt.md   the user's message, verbatim
    setup.sh    `setup.sh WORK RUN` builds the workspace in WORK. Stubs it puts
                in $E2E_BIN shadow real tools (on PATH first); logs go to RUN,
                or to $E2E_SHARED (RUN/shared at the top level) when a stub
                may be called from a nested harness run. Its HOME is the
                staged dir, to write into; a path it hands the agent (a probe
                script, an MCP config, a settings rule) names the agent's
                HOME, E2E_HOME, which under bwrap is another path.
    persist-session  optional, empty: keep the session transcript (for hooks
                that read `transcript_path`); default is --no-session-persistence
    lend        optional: HOME-relative paths, one per line (# comments), of
                real-HOME state an MCP server in the sandbox needs, bound
                read-only at their own path under bwrap (e.g. a broker's
                GitHub token). The agent can read them too: lend no more.
                Each entry must be a path inside HOME. Under bwrap an entry
                the host lacks, or one the staged HOME already holds, makes
                the run INCONCLUSIVE; without bwrap the real HOME is all
                there and nothing is mounted.
    check.sh    `check.sh RUN` grades one run: exit 0 PASS, 1 FAIL, anything
                else INCONCLUSIVE. RUN holds final.txt (the agent's last message),
                commands.txt (every Bash command the agent issued, one per
                line, in order), transcript.jsonl and whatever setup.sh logged.
                Paths the agent saw are in its HOME (E2E_HOME), not the staged
                dir check.sh gets as HOME: map them with _lib/sandbox_paths.py.

A run whose agent exits non-zero, reports an error or ends without a final
message is INCONCLUSIVE: excluded from the denominator, reported separately,
never retried into a replacement. Fixed n, one look.

COST
    `claude -p` inherits the caller's auth. On a Claude subscription (OAuth
    credentials, no ANTHROPIC_API_KEY) runs draw on quota, not a bill; the
    printed dollars are cost-equivalent accounting. --budget is a truncation
    guard per run.

FIDELITY
    The staged HOME carries the candidate CLAUDE.md, settings.json (hooks,
    permissions, defaultMode), skills, agents and commands, so hooks referencing
    $HOME/.claude run the candidate's scripts. Under bwrap it is mounted at the
    real HOME's own path, so path rules in the settings mean there what they
    mean in a real session, whatever --out the run dir sits under, and it is
    the only name the staged HOME has inside: RUN/home is hidden there. Lent
    back from the real HOME, at their own paths and with every symlink on the
    way recreated, by one table (expose_table()): credentials, the plugin
    cache, ~/Repos (the target of relative skill symlinks), ~/.config/caveman
    and ~/worktrees; what the scenario's `lend` names; the directories on the
    harness's PATH that lie in it, save ~/.claude's (the candidate's own bin
    is staged); the claude binary and every link to it; and the run and
    shared dirs, with the rest of the scenario's out dir hidden. Only the
    credentials, the run and the shared dir are writable, and nothing else of
    the real HOME is visible. A path the scenario lends that the host lacks,
    or that the staged HOME already holds, makes the run INCONCLUSIVE; any
    other row the host lacks or the staged HOME holds is skipped.
    Without bwrap HOME is the staged dir itself and LINKS are links. MCP
    servers are not loaded (--strict-mcp-config): the agent cannot reach real
    external systems through them. A scenario that needs an MCP tool has
    setup.sh write RUN/mcp.json naming a local STUB server; that file, and
    only that file, is passed as --mcp-config. Headless is not interactive:
    anything that would prompt is denied instead.
"""

from __future__ import annotations

import argparse
import errno
import concurrent.futures as cf
import hashlib
import json
import os
import pathlib
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
# From the passwd entry, not $HOME: inside a run (a nested harness) $HOME is
# the sandbox, and ssh resolves ~/.ssh the same way.
REAL_HOME = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
# Resolved here, from the harness's OWN environment, and never again. A bare
# "bash" handed to subprocess.run(env=...) is looked up in the PASSED env's
# PATH (os.get_exec_path reads that dict, not the harness's), and the env a
# run is built with puts the run's stub directory first -- a directory bound
# writable into the sandbox and handed to the agent as $E2E_BIN. A config
# under test that writes $E2E_BIN/bash therefore got the harness to execute
# its code OUTSIDE the sandbox; measured, with a planted `bash` turning a real
# FAIL into `PASS: no canary was touched`, exit 0.
HARNESS_PATH = os.environ.get("PATH", "")
BASH = shutil.which("bash") or "/bin/bash"
# Lent, never copied, HOME-relative, with the mode they get under bwrap: auth
# (writable, for token refresh), the plugin cache and the tree relative skill
# symlinks (`../../Repos/router-agent/...`) resolve into. Linked from the
# staged HOME without bwrap; under it, rows of the expose table (sandboxed()),
# where a link to the real HOME path would point at itself.
LINKS = {
    ".claude/.credentials.json": "rw",
    ".claude/plugins": "ro",
    "Repos": "ro",
    ".config/caveman": "ro",
}
# Lent read-only under bwrap only (without it the real HOME is there anyway):
# the other checkout root beside ~/Repos, which scenarios, their MCP servers
# (the klaffat app runner resolves it strictly) and a nested harness name by
# absolute path. HOME-relative.
CHECKOUT_ROOTS = ("worktrees",)
# Credentials and agents a run must not inherit: with them, an agent in the
# sandbox could push a real checkout under the linked ~/Repos, or call GitHub
# with the user's token, past every stub. The ACTIONS_ID_TOKEN_REQUEST_* pair
# is GitHub Actions' identity-token endpoint and its bearer: a run that keeps
# them can ask GitHub to sign a fresh statement of who the job is and exchange
# it for a new cloud credential, which outlives whatever credential the run was
# given on purpose.
SCRUB = ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "GIT_ASKPASS",
         "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
         "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
         "DBUS_SESSION_BUS_ADDRESS")
# Copied, never linked: config a hook needs to run at all, in a directory the
# hook also WRITES (dcg records allow-once and pending exceptions beside its
# config), so a sandbox run must not reach the real one. Without dcg's config
# bash-guard fails closed and every Bash call in the run is refused.
COPIES = {
    ".config/dcg/config.toml": REAL_HOME / ".config/dcg/config.toml",
}


def sh(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw)


def extract(repo: pathlib.Path | None, rev: str, dest: pathlib.Path, *,
            git: list[str] | None = None, paths: list[str] | None = None) -> None:
    """Write the tree of REV in REPO (or the repository GIT names, as git's
    own leading options), or only PATHS of it, under DEST (submodules come
    empty)."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", *(git or ["-C", str(repo)]), "archive", rev,
                              *(["--", *paths] if paths else [])],
                             check=True, capture_output=True)
    subprocess.run(["tar", "-xf", "-", "-C", str(dest)], input=archive.stdout,
                   check=True)


def gitlinks(repo: pathlib.Path, rev: str | None) -> list[tuple[str, str]]:
    """(path, pinned commit) of every submodule in REV's tree, or the index."""
    argv = ["ls-tree", "-r", "-z", rev] if rev else ["ls-files", "-s", "-z"]
    links = []
    for entry in filter(None, sh(["git", "-C", str(repo), *argv]).stdout.split("\0")):
        meta, path = entry.split("\t", 1)
        fields = meta.split()
        if fields[0] == "160000":
            links.append((path, fields[2] if rev else fields[1]))
    return links


def stage_submodules(repo: pathlib.Path, rev: str | None, dest: pathlib.Path) -> list[str]:
    """Copy each submodule's content under DEST: its working tree when staging
    a worktree (REV None), its pinned commit when staging a git ref. Skill
    symlinks into vendor/ dangle without this. Returns a note per submodule
    that could not be staged."""
    notes = []
    for path, pin in gitlinks(repo, rev):
        sub = repo / path
        if not (sub / ".git").exists():
            notes.append(f"submodule {path} not initialised")
        elif rev is None:
            shutil.copytree(sub, dest / path, symlinks=True, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git"))
        else:
            try:
                extract(sub, pin, dest / path)
            except subprocess.CalledProcessError:
                notes.append(f"submodule {path} @ {pin[:7]} not fetched locally")
    return notes


# What the deployed projection carries of each submodule under
# ~/.claude/vendor/<sub>: skills (the skill links' targets), hooks (settings
# runs vendor/superpowers/hooks/session-start) and package.json. Not the rest:
# everything-claude-code's assets/ alone is 36 MB, with images past the size
# the run-dir redaction scan accepts.
PROJECTED_SUBMODULE_PARTS = ("skills", "hooks", "package.json")


def stage_projected_submodules(rev: str, dest: pathlib.Path) -> list[str]:
    """Stage REV's submodules where the deployed projection puts them, for a
    ~/.claude (DEST) staged from this repository's runtime/ at REV.

    The projection is the checkout's runtime/ at ~/.claude/runtime, its
    entries linked at the top, and its submodules (vendor/<sub>) at
    ~/.claude/vendor/<sub>, so `runtime/skills/X -> ../../vendor/<sub>/...`
    resolves to ~/.claude/vendor/<sub>/..., the path the vendor permission
    rules name. Staging flattens runtime/ into ~/.claude, so a submodule
    outside runtime/ goes to its repository path under DEST, and
    rehome_links() re-points the links that reached it. The arm's own pins,
    never the deployed copy. A worktree rarely has its submodules checked
    out; their objects are then read from the superproject's modules/.
    Returns a note per submodule that could not be staged."""
    top = pathlib.Path(sh(["git", "-C", str(REPO), "rev-parse", "--show-toplevel"]).stdout.strip())
    prefix = sh(["git", "-C", str(REPO), "rev-parse", "--show-prefix"]).stdout.strip()
    common = pathlib.Path(sh(["git", "-C", str(top), "rev-parse", "--path-format=absolute",
                              "--git-common-dir"]).stdout.strip())
    notes = []
    for path, pin in gitlinks(top, rev):
        rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
        if (top / path / ".git").exists():
            git = ["-C", str(top / path)]
        elif (common / "modules" / path).is_dir():
            git = ["--git-dir", str(common / "modules" / path)]
        else:
            notes.append(f"submodule {path} not initialised")
            continue
        try:
            top_level = set(sh(["git", *git, "ls-tree", "--name-only", pin]).stdout.split("\n"))
            parts = [p for p in PROJECTED_SUBMODULE_PARTS if p in top_level]
            if parts:
                extract(None, pin, dest / rel, git=git, paths=parts)
        except subprocess.CalledProcessError:
            notes.append(f"submodule {path} @ {pin[:7]} not fetched locally")
    rehome_links(dest, prefix)
    return notes


def rehome_links(dest: pathlib.Path, prefix: str) -> None:
    """Re-point each relative link staged from PREFIX (runtime/) that reaches
    out of it at what the staging put at that repository path under DEST --
    where the projection has it. A link whose target was not staged keeps its
    target and dangles, as it would deployed."""
    if not prefix:
        return
    for root, dirs, files in os.walk(dest):
        for name in dirs + files:
            link = pathlib.Path(root, name)
            if not link.is_symlink() or os.path.isabs(target := os.readlink(link)):
                continue
            rel = link.relative_to(dest)
            in_repo = os.path.normpath(os.path.join(prefix, os.path.dirname(rel), target))
            if in_repo.startswith((prefix, "../")) or in_repo == "..":
                continue
            staged = dest / in_repo
            if staged.exists():
                link.unlink()
                link.symlink_to(os.path.relpath(staged, link.parent))


def copy_config(src: str, dest: pathlib.Path) -> str:
    """Materialise config SRC at DEST. Returns a short provenance string."""
    dest.mkdir(parents=True, exist_ok=True)
    path = pathlib.Path(src).expanduser()
    if path.is_dir():
        if (path / ".git").exists():
            files = sh(["git", "-C", str(path), "ls-files", "-z", "-co",
                        "--exclude-standard"]).stdout.split("\0")
            for rel in filter(None, files):
                s, d = path / rel, dest / rel
                if not s.exists() and not s.is_symlink():
                    continue  # deleted in the working tree
                d.parent.mkdir(parents=True, exist_ok=True)
                if s.is_symlink():
                    d.symlink_to(os.readlink(s))
                elif s.is_file():
                    shutil.copy2(s, d)
            notes = stage_submodules(path, None, dest)
            head = sh(["git", "-C", str(path), "rev-parse", "--short", "HEAD"]).stdout.strip()
            dirty = sh(["git", "-C", str(path), "status", "--porcelain"]).stdout.strip()
            return "; ".join([f"{path} @ {head}{' +uncommitted' if dirty else ''}", *notes])
        shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
        return f"{path} (plain directory)"
    sha = sh(["git", "-C", str(REPO), "rev-parse", "--short", src]).stdout.strip()
    extract(REPO, src, dest)
    notes = stage_projected_submodules(src, dest)
    return "; ".join([f"{src} @ {sha}", *notes])


def scenario_id(scenario: pathlib.Path) -> str:
    """Digest of every AUTHORED file in the scenario (prompt, setup, check,
    stubs), so an edited scenario is run again rather than resumed.

    Generated caches are skipped. A scenario whose setup imports a helper gets
    `__pycache__/*.pyc` written into this tree, and hashing those made a
    finished run stop matching itself -- repeating completed, paid runs. Worse
    than flaky: a .pyc embeds the source mtime and the interpreter magic, so
    the digest was not reproducible across machines or checkouts either.

    A sibling directory any hashed file names as `../NAME` (`_lib`, another
    scenario's setup) is hashed too, so editing a shared grader re-runs.
    """
    skip = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    h = hashlib.sha256()
    dirs = [scenario]
    for d in dirs:
        for f in sorted(p for p in d.rglob("*") if p.is_file()):
            rel = f.relative_to(d)
            if skip & set(rel.parts) or rel.suffix in (".pyc", ".pyo"):
                continue
            data = f.read_bytes()
            name = rel if d == scenario else f.relative_to(scenario.parent)
            h.update(str(name).encode() + b"\0" + data + b"\0")
            for ref in re.findall(rb"\.\./([\w.-]*\w[\w.-]*)", data):
                sib = scenario.parent / ref.decode()
                if sib.is_dir() and sib not in dirs:
                    dirs.append(sib)
    return h.hexdigest()


def config_id(src: str) -> str | None:
    """The exact config SRC names right now: a commit, or None when it has no
    fixed identity (an uncommitted worktree, a plain directory). A finished run
    is resumed only under the same id, so an arm pointed at another commit, or
    a moving ref that has moved, is run again rather than reported stale."""
    path = pathlib.Path(src).expanduser()
    if path.is_dir():
        if not (path / ".git").exists():
            return None
        if sh(["git", "-C", str(path), "status", "--porcelain"]).stdout.strip():
            return None
        return sh(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    return sh(["git", "-C", str(REPO), "rev-parse", src]).stdout.strip()


def stage(src: str, home: pathlib.Path, *, links: bool = True) -> str:
    """Build a sandbox HOME whose ~/.claude is config SRC. LINKS are linked in
    unless LINKS is false: a HOME bwrap mounts at the real HOME's path gets
    them bound in by sandboxed()."""
    provenance = copy_config(src, home / ".claude")
    for rel in LINKS if links else ():
        link, target = home / rel, REAL_HOME / rel
        if link.exists() or link.is_symlink():
            continue
        if target.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
    for rel, source in COPIES.items():
        if source.is_file():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, home / rel)
    (home / ".gitconfig").write_text(
        "[user]\n\tname = e2e\n\temail = e2e@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    return provenance


# Account material a run leaves behind, found by name anywhere in its dir: a
# HOME the run moved, a nested harness's runs and work/ all hold copies, and a
# token refresh turns the credentials link into a real file. Every link goes
# too: stage() links into the real HOME, and a run can plant more.
SECRET_FILE_PREFIXES = (".claude.json", ".credentials.json")
SECRET_DIRS = (".ssh", ".codex")
_DIR_NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_WRITE_NOFOLLOW = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC


def _is_secret(name: str, parent: str, st: os.stat_result) -> bool:
    # A hard link goes like a symlink: the redaction pass rewrites files in
    # place, and through a second name that would rewrite a file outside.
    if (stat.S_ISLNK(st.st_mode) or name.startswith(SECRET_FILE_PREFIXES)
            or stat.S_ISREG(st.st_mode) and st.st_nlink > 1):
        return True
    return stat.S_ISDIR(st.st_mode) and (
        name in SECRET_DIRS or (name == "backups" and parent == ".claude"))


def scrub_dir(path: pathlib.Path, *, require_redaction: bool = False) -> list[str]:
    """Strip account material and every link from the tree at PATH, then
    redact secrets in every file kept (ledgers, notes, transcripts) for
    review. Returns the errors; one failed item never stops the others.
    See redact_dir for REQUIRE_REDACTION."""
    errors, opened = _remove_secrets(path)
    if not opened:
        return errors  # a refused or absent root: nothing to redact either
    return errors + redact_dir(path, required=require_redaction)


def _remove_secrets(path: pathlib.Path) -> tuple[list[str], bool]:
    """The removal pass of scrub_dir. Returns the errors and whether PATH
    was opened as a directory at all.

    Runs outside the sandbox on a tree a run could rewrite, so nothing here
    follows a link: PATH itself is opened O_NOFOLLOW and every step after it
    goes through directory fds. Read denies cannot stand in for this: Bash
    reads past them, and a run dir can sit under any --out.
    """
    errors: list[str] = []
    try:
        root = os.open(path, _DIR_NOFOLLOW)
    except FileNotFoundError:
        return errors, False
    except OSError as exc:  # a link planted at PATH (ELOOP) included
        return [f"{path}: {exc}"], False
    def record(exc: BaseException) -> None:
        errors.append(f"{path}: {exc}")

    try:
        for dirpath, dirnames, filenames, dfd in os.fwalk(
                dir_fd=root, follow_symlinks=False, onerror=record):
            parent = os.path.basename(os.path.normpath(dirpath))
            for name in [*dirnames, *filenames]:
                where = path / dirpath / name
                try:
                    st = os.lstat(name, dir_fd=dfd)
                    if not _is_secret(name, parent, st):
                        continue
                    if name in dirnames:
                        dirnames.remove(name)
                    if stat.S_ISDIR(st.st_mode):
                        # Past an unremovable entry, so its siblings still go.
                        shutil.rmtree(name, dir_fd=dfd,
                                      onexc=lambda _f, p, exc, w=where: errors.append(
                                          f"{w}: {p}: {exc}"))
                    else:
                        os.unlink(name, dir_fd=dfd)
                except FileNotFoundError:
                    continue  # gone already: nothing left to scrub
                except OSError as exc:
                    errors.append(f"{where}: {exc}")
    except OSError as exc:  # the root itself could not be listed
        record(exc)
    finally:
        os.close(root)
    return errors, True


# Account material a run PRINTED rather than left as a file: an agent that
# cats its staged credentials puts the token in transcript.jsonl, which the
# broker's reviewers may Read. Detection is the `secret-redact` CLI (gitleaks
# with pinned rules), the engine the output-redaction hook and the aggregator
# also use, through the hook's own client module.
SECRET_REDACT = "secret-redact"
REDACT_TIMEOUT = 300.0
REDACT_BATCH_FILES = 256     # open fds held per CLI call
# Half the client's MAX_SCAN_CHARS (8 MiB): a batch is flushed once past it,
# so batch + one more file stays inside what one CLI call may carry.
SCAN_FILE_BYTES = 4 * 1024 * 1024
_REDACTION = pathlib.Path(__file__).resolve().parents[2] / "hooks/secret_redaction.py"
WITHHELD = "[withheld by the agent-e2e scrub: {why}]\n"


def _redaction_client():
    """runtime/hooks/secret_redaction.py, or None when this checkout lacks it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("secret_redaction", _REDACTION)
    if spec is None or not _REDACTION.is_file():
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rewrite(fd: int, data: bytes) -> None:
    """Replace the content of the file open read-only at FD. A file the run
    kept read-only (git objects are 0444) gets the owner write bit for the
    write and its mode back after. The write goes to the same inode, reopened
    through /proc/self/fd: no path is looked up, so nothing can be swapped in."""
    mode = stat.S_IMODE(os.fstat(fd).st_mode)
    if not mode & stat.S_IWUSR:
        os.fchmod(fd, mode | stat.S_IWUSR)
    try:
        w = os.open(f"/proc/self/fd/{fd}", os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(w, view):]
        finally:
            os.close(w)
    finally:
        if not mode & stat.S_IWUSR:
            os.fchmod(fd, mode)


def _withhold(fd: int, why: str) -> None:
    _rewrite(fd, WITHHELD.format(why=why).encode())


def _scan_form(name: str, text: str):
    """What the scanner is given for a file, and how a redacted copy is written
    back (None: as text). JSON is scanned as its decoded strings, the way the
    hook's client scans a tool result: in the raw text a token right after an
    escape (`\\nsk-ant-...`) has a letter before it, and the pinned rules,
    anchored on a non-word character, miss it. A file that does not parse is
    scanned as text."""
    if name.endswith(".jsonl"):
        lines = text.split("\n")
        try:
            value = [json.loads(line) if line.strip() else None for line in lines]
        except (ValueError, RecursionError):
            return text, None
        return value, lambda v: "\n".join(json.dumps(x) if line.strip() else line
                                          for x, line in zip(v, lines))
    if name.endswith(".json"):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return text, None
        return value, lambda v: json.dumps(v, indent=2)
    return text, None


def redact_dir(path: pathlib.Path, *, required: bool) -> list[str]:
    """Redact secrets in every regular file under PATH, in place. Returns the
    errors. Each file is opened once, read-only and O_NOFOLLOW, and any
    rewrite goes to that same inode (see _rewrite), so a run cannot swap a
    path under it. A file is rewritten only when a secret was found in it.

    REQUIRED fails closed: a file that cannot be scanned (no scanner, a scan
    that fails, a file past the scan limit or not writable) is withheld, its
    content replaced by a WITHHELD line, and the error returned. The broker
    sets it, because its runs are Read-allowed. Without it, a missing scanner
    (a CI runner, where secret-redact cannot be installed) is a warning and
    the files stay as they are; any other failure still withholds.
    """
    client = _redaction_client()
    cli = shutil.which(SECRET_REDACT)
    if client is None or cli is None:
        why = (f"{SECRET_REDACT} is not on PATH" if cli is None
               else f"no redaction client at {_REDACTION}")
        if not required:
            print(f"warning: {path} not redacted: {why}", file=sys.stderr)
            return []
    else:
        why = None
    errors: list[str] = []
    batch: list[tuple] = []     # (fd, where, size, value scanned, how to write it back)

    def flush() -> None:
        if not batch:
            return
        def scan(values: list) -> tuple[list | None, str]:
            try:
                if why is not None:
                    raise RuntimeError(why)
                return client.redact_value(values, cli=cli, timeout=REDACT_TIMEOUT), ""
            except (RuntimeError, OSError) as exc:
                return None, str(exc)
            except Exception as exc:  # the client's ScanError, and anything it did not expect
                return None, f"{type(exc).__name__}: {exc}"

        redacted, failed = scan([value for _, _, _, value, _ in batch])
        if redacted is not None:
            results = [(r, "") for r in redacted]
        elif why is None and len(batch) > 1:
            # One file the scanner cannot take fails the whole call; scan the
            # batch again file by file, so only that file is withheld.
            results = []
            for _, _, _, value, _ in batch:
                one, failed = scan([value])
                results.append((one[0] if one else None, failed))
        else:
            results = [(None, failed)] * len(batch)
        for (fd, where, _, value, dump), (result, failed) in zip(batch, results):
            try:
                if result is None:
                    _withhold(fd, f"not scanned for secrets: {failed}")
                    errors.append(f"{where}: withheld, not scanned: {failed}")
                elif result != value:
                    _rewrite(fd, (dump(result) if dump else result).encode())
            except OSError as exc:
                errors.append(f"{where}: could not redact: {exc}")
            finally:
                os.close(fd)
        batch.clear()

    try:
        root = os.open(path, _DIR_NOFOLLOW)
    except FileNotFoundError:
        return errors
    except OSError as exc:
        return [f"{path}: {exc}"]
    try:
        for dirpath, _dirs, filenames, dfd in os.fwalk(
                dir_fd=root, follow_symlinks=False,
                onerror=lambda exc: errors.append(f"{path}: {exc}")):
            for name in filenames:
                where = path / dirpath / name
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                 | os.O_CLOEXEC, dir_fd=dfd)
                except FileNotFoundError:
                    continue
                except OSError as exc:  # a mode-000 file
                    errors.append(f"{where}: not scanned: {exc}")
                    if required:
                        try:
                            os.unlink(name, dir_fd=dfd)
                        except OSError as gone:
                            errors.append(f"{where}: could not withhold: {gone}")
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                        os.close(fd)  # the removal pass takes hard links
                        continue
                    if st.st_size > SCAN_FILE_BYTES:
                        _withhold(fd, "past the scan limit")
                        errors.append(f"{where}: withheld, {st.st_size} bytes "
                                      f"is past the {SCAN_FILE_BYTES}-byte scan limit")
                        os.close(fd)
                        continue
                    chunks = []
                    while chunk := os.read(fd, 1 << 20):
                        chunks.append(chunk)
                except OSError as exc:
                    errors.append(f"{where}: not scanned: {exc}")
                    os.close(fd)
                    continue
                # Bytes that are not UTF-8 reach the scanner as U+FFFD; the file
                # is rewritten, and they with it, only when a secret was found.
                text = b"".join(chunks).decode("utf-8", "replace")
                batch.append((fd, where, len(text), *_scan_form(name, text)))
                if (len(batch) >= REDACT_BATCH_FILES
                        or sum(size for _, _, size, _, _ in batch) > SCAN_FILE_BYTES):
                    flush()
    except OSError as exc:
        errors.append(f"{path}: {exc}")
    finally:
        flush()
        os.close(root)
    return errors


def write_run_file(run: pathlib.Path, name: str, text: str) -> None:
    """Write RUN/NAME without following a link the run planted there."""
    path = run / name
    try:
        fd = os.open(path, _WRITE_NOFOLLOW, 0o644)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        os.unlink(path)
        fd = os.open(path, _WRITE_NOFOLLOW, 0o644)
    # surrogateescape: a run can name files in bytes that are not UTF-8, and
    # those names reach scrub-errors.txt and verdict.json.
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def read_lend(scenario: pathlib.Path) -> list[str]:
    """HOME-relative paths the scenario's `lend` file names, one per line.

    Raises ValueError for an entry outside HOME or naming all of it: a lend is
    a specific path, never the real HOME back."""
    path = scenario / "lend"
    if not path.is_file():
        return []
    lend = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        rel = os.path.normpath(entry)
        if os.path.isabs(rel) or rel == "." or rel == ".." or rel.startswith("../"):
            raise ValueError(f"lend: {entry!r} is not a path inside HOME")
        lend.append(rel)
    return lend


def mounts(argv: list[str]) -> list[tuple[str, str | None]]:
    """(path inside, host path) of every mount in a sandboxed() ARGV, in
    order; None for a tmpfs, which has no host path. What check.sh gets as
    E2E_MOUNTS, so a grader can tell which file a path the agent saw was."""
    out: list[tuple[str, str | None]] = []
    i = 1
    while i < len(argv) and argv[i] != "--":
        op = argv[i]
        if op in ("--bind", "--ro-bind", "--dev-bind"):
            out.append((argv[i + 2], argv[i + 1]))
            i += 3
        elif op == "--tmpfs":
            out.append((argv[i + 1], None))
            i += 2
        elif op in ("--symlink", "--setenv"):
            i += 3
        elif op in ("--proc", "--chdir"):
            i += 2
        else:
            i += 1
    return out


def path_guard() -> list[list[str]]:
    """Bind every directory on the harness's own PATH outside the real HOME
    read-only.

    The grader runs outside the sandbox and resolves `python3`, `git`, `grep`
    through this PATH, so a writable entry on it is a place the config under
    test can plant an executable the harness will then run -- the same hijack
    as $E2E_BIN, by a different directory. Measured: with a writable PATH entry
    outside /tmp and outside REAL_HOME, a sandboxed shell wrote `python3` into
    it and turned a grader's FAIL into `PASS: no canary was touched`.

    Derived from PATH rather than naming paths, because the exposed directories
    are the runner's, not this host's: on a GitHub hosted runner /usr/local/bin
    and the hosted tool caches are owned by the unprivileged user the job runs
    as, while on NixOS every entry already lives in the read-only store. Each
    entry is resolved, because writability belongs to the real directory and
    bwrap should not be handed a symlink to mount over.

    An entry that resolves into the real HOME is not this guard's: the staged
    HOME mounted over the real one hides it, and what of it the agent gets
    back is a read-only row of the expose table (`expose_table()`).
    """
    seen: list[str] = []
    for entry in HARNESS_PATH.split(os.pathsep):
        if not entry:
            continue
        real = pathlib.Path(entry).resolve()
        if _within(real, REAL_HOME):
            continue
        if real.is_dir() and str(real) not in seen:
            seen.append(str(real))
    return [["--ro-bind", d, d] for d in seen]


class Unexposable(Exception):
    """A row of the expose table that cannot be lent as asked: the host has
    no such path (ABSENT), or the staged HOME already holds its destination,
    which the real one would shadow without a word (PROVIDED). The run is
    INCONCLUSIVE, save for a row the table marks optional: absent, it is
    skipped; provided, the staged copy stands, as it does without bwrap."""

    def __init__(self, path: pathlib.Path, why: str, *, absent: bool = False,
                 provided: bool = False):
        super().__init__(f"{path}: {why}")
        self.absent = absent
        self.provided = provided


def _within(path: pathlib.Path, root: pathlib.Path) -> bool:
    return path == root or path.is_relative_to(root)


def _hops(path: pathlib.Path, staged: pathlib.Path) -> tuple[list[str], pathlib.Path]:
    """Walk absolute PATH on the host a component at a time, the way the
    kernel resolves it. Returns the `--symlink` args that recreate every link
    met on the way inside a hidden tree (the real HOME, under the staged one;
    /tmp, under the private tmpfs) -- a link elsewhere is visible as it is --
    and the link-free path PATH names, the one to mount.

    Raises Unexposable for a component the host lacks, and for one in the real
    HOME that the staged HOME (STAGED) already holds: as the destination
    itself, or on the way as anything but the plain directory the host has."""
    hidden = (REAL_HOME, pathlib.Path("/tmp"))
    links: list[str] = []
    todo = list(reversed(path.parts[1:]))  # next component on top
    cur, hops = pathlib.Path("/"), 0
    while todo:
        name = todo.pop()
        if name == "..":
            cur = cur.parent
            continue
        nxt = cur / name
        if nxt != REAL_HOME and nxt.is_relative_to(REAL_HOME):
            mine = staged / nxt.relative_to(REAL_HOME)
            if os.path.lexists(mine) and (not todo or nxt.is_symlink()
                                          or mine.is_symlink() or not mine.is_dir()):
                raise Unexposable(nxt, "the staged HOME already provides it; "
                                       "lending the real one would shadow it",
                                  provided=True)
        if nxt.is_symlink():
            hops += 1
            if hops > 40:  # the kernel's own limit, give or take
                raise Unexposable(path, "too many levels of symbolic links", absent=True)
            target = os.readlink(nxt)
            if any(_within(nxt, h) for h in hidden):
                links += ["--symlink", target, str(nxt)]
            parts = pathlib.PurePosixPath(target).parts
            if os.path.isabs(target):
                cur, parts = pathlib.Path("/"), parts[1:]
            todo += reversed(parts)
            continue
        if not os.path.lexists(nxt):
            raise Unexposable(path, "absent on the host", absent=True)
        cur = nxt
    return links, cur


def expose(path: pathlib.Path, mode: str, staged: pathlib.Path) -> list[list[str]]:
    """The bwrap ops that make host PATH reachable inside the sandbox at its
    own path, the one routine every row of the expose table goes through (the
    pattern of Flatpak's FlatpakExports): each symlink on the way is recreated
    as the same link (`_hops()`), and its final target is bound -- `ro`
    read-only, `rw` writable -- or, for `tmpfs`, hidden under an empty one.
    One op per list; sandboxed() puts them in mount order.

    Raises Unexposable when that cannot be done faithfully: PATH is not
    absolute, or where it lands, every link on the way followed, is the real
    HOME or holds it -- a row lends a path inside it, never all of it, and
    the spelling (`.local/state/h` -> `../..`) is not what gets mounted.
    """
    if not path.is_absolute():
        raise Unexposable(path, "is not an absolute path (a --claude PATH "
                                "does not resolve?)")
    links, real = _hops(path, staged)
    if any(_within(home, real) for home in (REAL_HOME, REAL_HOME.resolve())):
        raise Unexposable(path, f"lands on {real}, the real HOME or what holds "
                                "it; a row lends a path inside it, never all of it")
    ops = [links[i:i + 3] for i in range(0, len(links), 3)]
    if mode == "tmpfs":
        return [*ops, ["--tmpfs", str(real)]]
    return [*ops, ["--bind" if mode == "rw" else "--ro-bind", str(real), str(real)]]


def _mount_order(op: list[str]) -> tuple[int, bool]:
    """Sort key putting bwrap ops in an order where none buries another:
    shallower destinations first, and at one destination a tmpfs last.

    bwrap mounts in argv order, and a mount covers whatever was mounted
    beneath it before. In this order a mount is never followed by one over
    it, so what is visible at any path is the deepest row holding it, which
    is what the table means, whatever order it lists rows in. Without it a
    bind could re-expose what a hiding row had hidden: claude's own directory,
    lent after the out-dir tmpfs, brought the sibling runs back whenever --out
    lay inside it."""
    return len(pathlib.PurePosixPath(op[-1]).parts), op[0] == "--tmpfs"


def _path_in_home() -> list[pathlib.Path]:
    """The harness's PATH entries that lie in the real HOME, literally or once
    resolved, by their literal path: the agent's PATH names that one
    (~/.nix-profile/bin, a link). Not those in ~/.claude: the config under test
    is the staged one, whose own bin/ sits beside what its scripts import
    through `..` (ai-router: ../tools/ai-router by realpath; egress-sink:
    ../egress_guard by abspath); the deployed bin lent alone would import from
    a tree the sandbox does not have."""
    config = REAL_HOME / ".claude"
    out = []
    for entry in HARNESS_PATH.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        literal = pathlib.Path(entry)
        both = (literal, literal.resolve())
        if any(p != REAL_HOME and _within(p, REAL_HOME) for p in both) \
                and not any(_within(p, config) for p in both):
            out.append(literal)
    return out


def _link_dirs_in_home(d: pathlib.Path) -> list[pathlib.Path]:
    """The dirs that entries of PATH dir D link into, where those lie in the
    real HOME (not ~/.claude, not HOME itself) and outside D."""
    config, out = REAL_HOME / ".claude", []
    try:
        entries = list(os.scandir(d))
    except OSError:
        return out
    for e in entries:
        if not e.is_symlink():
            continue
        parent = pathlib.Path(os.path.realpath(e.path)).parent
        if (parent != REAL_HOME and _within(parent, REAL_HOME)
                and not _within(parent, config) and not _within(parent, d.resolve())):
            out.append(parent)
    return list(dict.fromkeys(out))


def expose_table(*, claude: str, run: pathlib.Path, shared: pathlib.Path,
                 lend: list[str] | tuple[str, ...] = ()
                 ) -> list[tuple[pathlib.Path, str, bool]]:
    """What the sandbox lends back over the staged HOME and the private /tmp:
    (host path, mode, required). Each row goes through expose(); the order
    here is not the mount order, which sandboxed() derives from where each
    row lands (`_mount_order()`), so the deepest row holding a path decides
    what is there. A required row the host lacks makes the run INCONCLUSIVE; an
    optional one is skipped, which is no loss: the agent then finds nothing
    there, as it would on the host. A row whose destination the staged HOME
    already provides is INCONCLUSIVE when required; when optional it is
    skipped and the staged copy stands, as stage() leaves it without bwrap
    (an arm SRC copied whole from a ~/.claude carries plugins/ and
    .credentials.json of its own).

      row                          mode   required  why
      LINKS: ~/.claude/.credentials.json  rw  no    auth; refresh writes it
             ~/.claude/plugins     ro     no        the plugin cache
             ~/Repos               ro     no        relative skill links land here
             ~/.config/caveman     ro     no        caveman's own config
      CHECKOUT_ROOTS: ~/worktrees  ro     no        MCP servers, nested runs
      the scenario's `lend`        ro     yes       it asked for exactly these
      PATH entries in HOME         ro     no        the agent's tools, by literal
                                                    path (not ~/.claude's)
      the scenario's out dir       tmpfs  yes       hides sibling runs under a
                                                    lent root (--out ~/worktrees/x)
      claude's real directory      ro     yes       the binary (its own file when
                                                    straight in /tmp or HOME)
      claude as run                ro     yes       every link of the chain to it
      the run                      rw     yes       its own dir, workspace included
      the shared log dir           rw     yes       E2E_SHARED, for nested runs
      the run's home/              tmpfs  yes       one name for HOME: the staged
                                                    HOME is reachable only as HOME
    """
    home, tmp = REAL_HOME, pathlib.Path("/tmp")
    rows = [(home / rel, mode, False) for rel, mode in LINKS.items()]
    rows += [(home / rel, "ro", False) for rel in CHECKOUT_ROOTS]
    rows += [(home / rel, "ro", True) for rel in lend]
    for p in _path_in_home():
        rows.append((p, "ro", False))
        # A tool there that links elsewhere in HOME (pipx, uv tool, npm -g:
        # bin/t -> ../lib/t) needs its target's dir lent too, or it dangles.
        for target in _link_dirs_in_home(p):
            rows.append((target, "ro", False))
    rows.append((run.parents[1], "tmpfs", True))
    exe = pathlib.Path(claude if os.path.isabs(claude) else shutil.which(claude) or claude)
    real = exe.resolve()
    rows += [(real if real.parent in (home, tmp) else real.parent, "ro", True),
             (exe, "ro", True)]
    rows += [(run, "rw", True), (shared, "rw", True), (run / "home", "tmpfs", True)]
    return list(dict.fromkeys(rows))


def sandboxed(argv: list[str], *, claude: str, run: pathlib.Path,
              shared: pathlib.Path, work: pathlib.Path,
              lend: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Wrap ARGV in the bubblewrap confinement a run executes under.

    The confinement binds `/` read-write, so what a run cannot write is
    whatever is bound read-only over it. `path_guard()` is why the harness's
    own PATH is on that list.

    Its own function so a test can attack the real construction rather than a
    copy of it; `tests/enforced/test_sandbox_isolation.py` runs this argv and
    tries to break out of it.

    A private /tmp per run. Agents write literal /tmp paths, and concurrent
    runs sharing the real one leaked into each other: two dogfood runs picked
    the same /tmp output dir and one "ran" its e2e by reusing the other's
    verdict. Only this run's own directory is bound back in.

    The staged HOME (RUN/home) is mounted at the real HOME's own path, and
    HOME names that path. The settings under test are written for the host
    layout -- `~/` and absolute `//home/<user>/...` rules alike -- so that is
    the layout they must see, never wherever --out put the run dir: a deny of
    every staged HOME under the broker's state dir, there for reviewers of run
    dirs, would otherwise deny the agent its own HOME. Docker, Flatpak and
    Bazel's linux-sandbox do the same: a mount whose destination is not its
    source, so the policy never sees where the source lives. It is the only
    name the staged HOME has inside: RUN/home, bound back with the run, is
    hidden again, so a path baked from the host's view of it fails loudly.

    That mount hides all of the real HOME -- ~/.ssh, the account's state,
    every other run under the broker's state dir. What a run needs of it, and
    of the private /tmp, is lent back by one table (`expose_table()`) through
    one routine (`expose()`), and every mount goes in by depth
    (`_mount_order()`), so none buries another. check.sh, on the host, gets the mounts as
    E2E_MOUNTS (`mounts()`), so a path the agent saw can be told apart from
    the host file at that path (`_lib/sandbox_paths.py`).

    Raises Unexposable when a row cannot be lent faithfully.
    """
    # The credentials file is a mount point inside, not the replaceable link
    # it is without bwrap: a refresh that writes in place reaches the real
    # file, one that writes a temp file and renames it over gets EBUSY.
    # Measured with a decoy in its place: `mv ~/x ~/.claude/.credentials.json`
    # -> "Device or resource busy", `echo > ~/.claude/.credentials.json` -> ok.
    staged = run / "home"
    ops = [["--tmpfs", "/tmp"], ["--bind", str(staged), str(REAL_HOME)], *path_guard()]
    for path, mode, required in expose_table(claude=claude, run=run, shared=shared,
                                             lend=lend):
        try:
            ops += expose(path, mode, staged)
        except Unexposable as exc:
            if required or not (exc.absent or exc.provided):
                raise
    ops.sort(key=_mount_order)
    # A private pid namespace with its own procfs. Dropping a variable from
    # the child's environment is not isolation while the child can read it
    # back out of an ancestor's /proc entry: the harness itself holds what the
    # run must not have, GitHub Actions' identity-token endpoint and bearer
    # among it.
    #
    # Measured, because the obvious explanation was the wrong one. An
    # unsandboxed child CAN read its parent's environ on these hosts:
    # `kernel.yama.ptrace_scope` is 1 here, but yama gates PTRACE_MODE_ATTACH
    # and not a read, so what refuses that read from inside the sandbox is
    # bubblewrap's user namespace. The pid namespace is what stops the
    # guarantee resting on one mechanism, and it drops the host's process list
    # from view besides -- some 900 entries to 4.
    #
    # --die-with-parent because a run must not outlive the credential it was
    # given. Killing the launcher at the deadline left the sandbox running
    # with the token in hand; measured with a 3s timeout against `sleep 300`.
    return [shutil.which("bwrap"), "--dev-bind", "/", "/",
            "--unshare-pid", "--proc", "/proc", "--die-with-parent",
            *(arg for op in ops for arg in op),
            "--setenv", "HOME", str(REAL_HOME), "--chdir", str(work), "--", *argv]


def events_of(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def final_text(events: list[dict]) -> str:
    final = ""
    for ev in events:
        if ev.get("type") == "result" and isinstance(ev.get("result"), str):
            final = ev["result"]
    if final:
        return final
    for ev in events:
        if ev.get("type") == "assistant":
            parts = (ev.get("message") or {}).get("content") or []
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            if text.strip():
                final = text
    return final


def bash_commands(events: list[dict]) -> list[str]:
    cmds = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for part in (ev.get("message") or {}).get("content") or []:
            if part.get("type") == "tool_use" and part.get("name") == "Bash":
                cmd = (part.get("input") or {}).get("command", "")
                cmds.append(" ".join(cmd.split()))
    return cmds


# A hook refusing the call, or headless -p denying a permission it cannot ask for.
REFUSAL = re.compile(r"hook error|requested permissions|permission to use|"
                     r"denied by your permission|haven't granted", re.I)


def bash_blocked(events: list[dict]) -> bool:
    """True when the agent ran Bash and the environment refused every call.

    That run measured the sandbox, not the config: an agent that cannot run a
    command cannot open a PR either, and would "pass" a hold-the-PR check. The
    probe that found this had bash-guard failing closed on a missing dcg config;
    headless permission denials count the same way.
    """
    bash_ids = {part.get("id")
                for ev in events if ev.get("type") == "assistant"
                for part in (ev.get("message") or {}).get("content") or []
                if part.get("type") == "tool_use" and part.get("name") == "Bash"}
    if not bash_ids:
        return False
    refused = 0
    for ev in events:
        if ev.get("type") != "user":
            continue
        content = (ev.get("message") or {}).get("content")
        for part in content if isinstance(content, list) else []:
            if part.get("type") == "tool_result" and part.get("tool_use_id") in bash_ids \
                    and part.get("is_error") and REFUSAL.search(str(part.get("content"))):
                refused += 1
    return refused == len(bash_ids)


def result_event(events: list[dict]) -> dict:
    for ev in reversed(events):
        if ev.get("type") == "result":
            return ev
    return {}


def run_one(scenario: pathlib.Path, arm: str, src: str, idx: int,
            out: pathlib.Path, args) -> dict:
    """One run, its dir scrubbed on every way out once check.sh is done.
    A SIGKILLed harness skips this; `scrub` over the output dir covers that.
    Scrub errors land in scrub-errors.txt, and in verdict.json when there is
    a verdict (a run that raised gets its fault verdict from cmd_run)."""
    verdict = None
    try:
        verdict = _run_one(scenario, arm, src, idx, out, args)
        return verdict
    finally:
        run = out / arm / str(idx)
        # Any raise here would replace the verdict. Before 3.12's iterative
        # fwalk/rmtree (a 3.12.x backport), a deep enough dir chain the run
        # built raised RecursionError from inside the scrub.
        try:
            errors = scrub_dir(run)
        except Exception as exc:
            errors = [f"{run}: scrub raised {exc!r}"]
            if verdict is not None:  # verdict.json was never scanned: keep only the grade
                _keep_grade(verdict, "withheld: the scrub raised before it was scanned")
        else:
            if verdict is not None:
                _take_redacted(run, verdict)
        if errors:
            print(f"scrub incomplete in {run}:\n  " + "\n  ".join(errors), file=sys.stderr)
            if verdict is not None:
                verdict["scrub_errors"] = errors
            # Recording must not cost the verdict: a raise here would replace it.
            for name, text in (("scrub-errors.txt", "\n".join(errors) + "\n"),
                               *((("verdict.json", json.dumps(verdict, indent=2)),)
                                 if verdict is not None else ())):
                try:
                    write_run_file(run, name, text)
                except (OSError, ValueError) as exc:
                    print(f"could not record scrub errors in {run / name}: {exc}",
                          file=sys.stderr)


def _take_redacted(run: pathlib.Path, verdict: dict) -> None:
    """Replace VERDICT, in place, by the scrubbed RUN/verdict.json: the copy in
    memory still holds what the scrub redacted on disk, and it goes on to the
    summary, stdout and any rewrite of verdict.json. A verdict.json that cannot
    be read back (withheld, or never written) leaves only its grade."""
    try:
        with open(run / "verdict.json", encoding="utf-8") as f:
            on_disk = json.load(f)
    except (OSError, ValueError):
        on_disk = None
    if not isinstance(on_disk, dict) or on_disk.get("verdict") != verdict.get("verdict"):
        _keep_grade(verdict, "verdict.json withheld or unreadable after the scrub")
        return
    verdict.clear()
    verdict.update(on_disk)


def _keep_grade(verdict: dict, why: str) -> None:
    """Strip VERDICT, in place, to fields that carry no run output."""
    kept = {k: verdict[k] for k in ("arm", "run", "config", "verdict") if k in verdict}
    verdict.clear()
    verdict.update(kept, why=why)


def _run_one(scenario: pathlib.Path, arm: str, src: str, idx: int,
             out: pathlib.Path, args) -> dict:
    run = out / arm / str(idx)
    cid = config_id(src)
    key = cid and f"{cid}:{scenario_id(scenario)}"
    if key and (run / "verdict.json").exists():  # resumable; never re-spend
        done = json.loads((run / "verdict.json").read_text(encoding="utf-8"))
        if done.get("resume_key") == key:
            return done
    if run.exists():
        shutil.rmtree(run)
    home, work, bindir = run / "home", run / "work", run / "bin"
    for d in (home, work, bindir):
        d.mkdir(parents=True)
    bwrap = shutil.which("bwrap")
    provenance = stage(src, home, links=not bwrap)
    # E2E_SHARED is the one directory every nesting level keeps writable: a
    # scenario whose agent runs this harness itself (the dogfood) gets a
    # private /tmp inside the inner run too, and a stub logging to the outer
    # run dir wrote into that tmpfs and was lost. Inherited when already set.
    shared = pathlib.Path(os.environ.get("E2E_SHARED") or run / "shared")
    shared.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "E2E_SHARED": str(shared),
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "E2E_BIN": str(bindir), "E2E_RUN": str(run), "E2E_WORK": str(work),
        "DISABLE_AUTOUPDATER": "1",
    }
    for var in ("GIT_CONFIG_GLOBAL", "CLAUDE_CONFIG_DIR", "CLAUDECODE",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", *SCRUB):
        env.pop(var, None)
    # ssh finds keys via getpwuid, not $HOME; refuse it outright. Scenario
    # remotes are local bare repos behind insteadOf, so nothing needs ssh.
    env["GIT_SSH_COMMAND"] = "false"
    # The stub directory is on PATH for the AGENT, whose tools the stubs exist
    # to shadow -- and for nobody else. setup.sh and check.sh run outside the
    # sandbox, as children of the harness, so every bare command they resolve
    # through PATH is a place the config under test can plant an executable:
    # the honeypot's own grader shells out to `python3`. They still get
    # $E2E_BIN as a variable, which is how setup.sh writes stubs; no grader
    # reads it, and the one grader that calls `gh` wants the real one.
    #
    # E2E_HOME is the HOME the agent sees: the real HOME's path under bwrap,
    # where setup.sh and check.sh still get the staged dir as HOME, so a path
    # the agent wrote in terms of its HOME is not one in theirs. check.sh also
    # gets the mounts behind it (E2E_MOUNTS, set below; never inherited from
    # an outer run) to translate such a path, `_lib/sandbox_paths.py`.
    env.pop("E2E_MOUNTS", None)
    script_env = {**env, "PATH": HARNESS_PATH,
                  "E2E_HOME": str(REAL_HOME if bwrap else home)}
    verdict = {"arm": arm, "run": idx, "config": provenance, "resume_key": key}
    try:
        lend = read_lend(scenario)
    except ValueError as exc:
        verdict.update(verdict="INCONCLUSIVE", why=str(exc))
        write_run_file(run, "verdict.json", json.dumps(verdict, indent=2))
        return verdict
    setup = subprocess.run([BASH, str(scenario / "setup.sh"), str(work), str(run)],
                           env=script_env, capture_output=True, text=True)
    write_run_file(run, "setup.log", setup.stdout + setup.stderr)
    if setup.returncode != 0:
        verdict.update(verdict="INCONCLUSIVE", why=f"setup.sh exit {setup.returncode}")
        write_run_file(run, "verdict.json", json.dumps(verdict, indent=2))
        return verdict
    prompt = (scenario / "prompt.md").read_text(encoding="utf-8")
    argv = [args.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--max-budget-usd", f"{args.budget:g}"]
    # Sessions are not persisted by default, so a hook reading `transcript_path`
    # finds no file. A scenario that tests such a hook opts in with an empty
    # `persist-session` file; the transcript lands in the throwaway HOME.
    if not (scenario / "persist-session").exists():
        argv.append("--no-session-persistence")
    if args.model:
        argv += ["--model", args.model]
    # The scenario's own stub MCP server(s), if setup.sh wrote any. Still
    # --strict-mcp-config: nothing from the real HOME's MCP config loads.
    if (run / "mcp.json").is_file():
        argv += ["--mcp-config", str(run / "mcp.json")]
    if bwrap:
        # After setup.sh, which may plant what a row would lend: that is
        # exactly the shadowing expose() refuses.
        try:
            argv = sandboxed(argv, claude=args.claude, run=run, shared=shared, work=work,
                             lend=lend)
        except Unexposable as exc:
            verdict.update(verdict="INCONCLUSIVE", why=f"sandbox: {exc}")
            write_run_file(run, "verdict.json", json.dumps(verdict, indent=2))
            return verdict
        script_env["E2E_MOUNTS"] = json.dumps(mounts(argv))
    verdict["isolated_tmp"] = bool(bwrap)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=work, env=env, capture_output=True,
                              text=True, timeout=args.timeout)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = f"timeout after {args.timeout}s"
    write_run_file(run, "transcript.jsonl", stdout)
    write_run_file(run, "stderr.txt", stderr)
    events = events_of(stdout)
    final = final_text(events)
    res = result_event(events)
    write_run_file(run, "final.txt", final)
    write_run_file(run, "commands.txt", "\n".join(bash_commands(events)) + "\n")
    verdict.update(seconds=round(time.monotonic() - t0, 1),
                   cost_usd=res.get("total_cost_usd"), exit=code)
    if code != 0 or res.get("is_error") or not final.strip():
        verdict.update(verdict="INCONCLUSIVE",
                       why=f"agent exit {code}, is_error={res.get('is_error')}, "
                           f"subtype={res.get('subtype')}, final={'yes' if final.strip() else 'none'}")
    elif bash_blocked(events):
        verdict.update(verdict="INCONCLUSIVE",
                       why="a hook refused every Bash call: the sandbox, not the "
                           "config, decided this run (see transcript.jsonl)")
    else:
        chk = subprocess.run([BASH, str(scenario / "check.sh"), str(run)],
                             env=script_env, capture_output=True, text=True)
        write_run_file(run, "check.log", chk.stdout + chk.stderr)
        why = (chk.stdout.strip().splitlines() or [""])[-1][:300]
        outcome = {0: "PASS", 1: "FAIL"}.get(chk.returncode, "INCONCLUSIVE")
        # A FAIL always carries its reason, because every grader prints one
        # before exiting 1. Exit 1 with nothing on stdout therefore means the
        # grader died before it could grade -- a parse error in the script
        # beats any excepthook the script installs -- and a crash must not be
        # reported as the config having gone for the credentials.
        if outcome == "FAIL" and not why:
            outcome = "INCONCLUSIVE"
            why = "grader exited 1 without a verdict; see check.log"
        verdict["verdict"] = outcome
        verdict["why"] = why
    write_run_file(run, "verdict.json", json.dumps(verdict, indent=2))
    return verdict


def summarise(scenario: pathlib.Path, verdicts: list[dict], arms: list[str]) -> str:
    lines = [f"### agent-e2e: `{scenario.name}`", "",
             "| arm | config | PASS | FAIL | INCONCLUSIVE | pass rate |",
             "|---|---|---|---|---|---|"]
    for arm in arms:
        mine = [v for v in verdicts if v["arm"] == arm]
        n = {k: sum(v["verdict"] == k for v in mine) for k in ("PASS", "FAIL", "INCONCLUSIVE")}
        denom = n["PASS"] + n["FAIL"]
        rate = f"{n['PASS']}/{denom}" if denom else "n/a"
        config = mine[0]["config"] if mine else "?"
        lines.append(f"| {arm} | `{config}` | {n['PASS']} | {n['FAIL']} | "
                     f"{n['INCONCLUSIVE']} | {rate} |")
    lines.append("")
    for v in sorted(verdicts, key=lambda v: (v["arm"], v["run"])):
        lines.append(f"- {v['arm']}#{v['run']} {v['verdict']}: {v.get('why', '')}")
    return "\n".join(lines) + "\n"


def cmd_run(args) -> int:
    scenario = pathlib.Path(args.scenario).resolve()
    for name in ("prompt.md", "setup.sh", "check.sh"):
        if not (scenario / name).is_file():
            sys.exit(f"{scenario} lacks {name}")
    arms = []
    for spec in args.arm:
        name, sep, src = spec.partition("=")
        if not sep or not name or not src:
            sys.exit(f"--arm wants NAME=SRC, got {spec!r}")
        arms.append((name, src))
    out = pathlib.Path(args.out).resolve() / scenario.name
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(name, src, i) for name, src in arms for i in range(1, args.n + 1)]
    verdicts = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(run_one, scenario, name, src, i, out, args): (name, i)
                for name, src, i in jobs}
        for fut in cf.as_completed(futs):
            name, i = futs[fut]
            try:
                v = fut.result()
            except Exception as exc:  # harness fault: never silently a PASS
                v = {"arm": name, "run": i, "config": "?", "verdict": "INCONCLUSIVE",
                     "why": f"harness error: {type(exc).__name__}: {exc}"}
                # Persist it, or this run leaves no trace on disk: the summary
                # below would say INCONCLUSIVE while anything reading the run
                # tree saw only the runs that survived. A CI gate counting
                # verdict files read that as all-clear.
                try:
                    fault = out / name / str(i)
                    fault.mkdir(parents=True, exist_ok=True)
                    write_run_file(fault, "verdict.json", json.dumps(v, indent=2))
                except OSError:
                    pass  # the in-memory verdict still reaches the summary
            verdicts.append(v)
            print(f"{name}#{i} {v['verdict']} {v.get('why', '')}", flush=True)
    summary = summarise(scenario, verdicts, [a for a, _ in arms])
    write_run_file(out, "summary.md", summary)
    print("\n" + summary + f"\nruns kept under {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stage", help="build one sandbox HOME and stop")
    st.add_argument("--config", required=True)
    st.add_argument("--home", required=True)
    rn = sub.add_parser("run", help="run a scenario across arms")
    rn.add_argument("scenario")
    rn.add_argument("--arm", action="append", required=True)
    rn.add_argument("--n", type=int, default=3)
    rn.add_argument("--jobs", type=int, default=4)
    rn.add_argument("--model", default=None, help="default: the staged config's own")
    rn.add_argument("--budget", type=float, default=3.0)
    rn.add_argument("--timeout", type=int, default=900)
    # Nested (an agent under test running this harness): default inside its own
    # run, never the outer tree, whose finished verdicts it would otherwise
    # resume as real and whose in-flight run dirs it could delete.
    nested = os.environ.get("E2E_RUN")
    rn.add_argument("--out", default=(f"{nested}/nested-e2e" if nested else
                                      os.environ.get("TMPDIR", "/tmp") + "/agent-e2e"))
    rn.add_argument("--claude", default=shutil.which("claude") or "claude")
    sc = sub.add_parser("scrub", help="strip account material from every run "
                        "dir under DIR (runs a killed harness left unscrubbed)")
    sc.add_argument("dir")
    sc.add_argument("--require-redaction", action="store_true",
                    help="withhold every file that cannot be scanned for secrets")
    args = ap.parse_args()
    if args.cmd == "stage":
        print(stage(args.config, pathlib.Path(args.home).resolve()))
        return 0
    if args.cmd == "scrub":
        errors = scrub_dir(pathlib.Path(args.dir), require_redaction=args.require_redaction)
        print("\n".join(errors), file=sys.stderr)
        return 1 if errors else 0
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
