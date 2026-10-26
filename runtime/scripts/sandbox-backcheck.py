#!/usr/bin/env python3
"""Back-check a candidate Claude Code sandbox policy against real Bash history.

Reads the last N days of Bash tool calls from Claude Code transcripts (and
Codex exec calls when parseable), splits each call into simple commands with a
fail-closed shlex-based tokenizer, and classifies every call against the
candidate policy as ``ok``, ``breaks:<reason>`` or ``unknown:<why>``.

Output: a markdown report (redacted examples only) and optional JSON
aggregates. Stdlib only.

Classification is static and heuristic. It never executes the commands; it
only runs ``git -C <dir> remote -v`` / ``rev-parse`` (cached per directory) to
resolve remotes and git common dirs.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")

# ---------------------------------------------------------------------------
# Markers emitted by the pre-scanner. Control characters never appear in the
# commands we analyse, so they cannot be forged by quoting.
# ---------------------------------------------------------------------------
REDIR = "\x01"       # REDIR + op            -> redirection operator token
SUB = "\x02"         # SUB + n + SUB         -> command/process substitution n
VAR = "\x03"         # VAR + name + VAR_END  -> parameter expansion
VAR_END = "\x04"
HEREDOC = "\x05"     # HEREDOC + n + HEREDOC -> heredoc n feeds this command's stdin
DYN_CHARS = (SUB, VAR)


class Unparseable(Exception):
    """The command cannot be tokenized reliably. Always maps to unknown."""


class PolicyUnsupported(Exception):
    """The policy uses a restriction this tool cannot model. Refuse to measure.

    Reporting a break rate against a policy whose restrictions are silently
    ignored is worse than reporting nothing: the number looks like evidence
    that enabling the policy is safe.
    """


# ---------------------------------------------------------------------------
# Pre-scanner: quote-aware pass that strips heredoc bodies and comments,
# replaces substitutions/expansions with markers, normalises redirections,
# and splits on control operators. shlex then does word splitting and quote
# removal per segment.
# ---------------------------------------------------------------------------

_HD_OP = re.compile(r"""<<(-?)[ \t]*(?:(['"])(.*?)\2|\\?([A-Za-z0-9_.\-]+))""")


def _match_paren(s: str, i: int) -> int:
    """s[i-1] == '('; return index of the matching ')' (quote and heredoc aware)."""
    depth = 1
    q = None
    n = len(s)
    pending: list = []
    while i < n:
        c = s[i]
        if q is None and c == "\n" and pending:
            j = i + 1
            for delim, strip in pending:
                while j < n:
                    nl = s.find("\n", j)
                    line = s[j:] if nl == -1 else s[j:nl]
                    j = n if nl == -1 else nl + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            pending = []
            i = j
            continue
        if q is None and c == "<" and s.startswith("<<", i) and not s.startswith("<<<", i):
            m = _HD_OP.match(s, i)
            if m:
                pending.append((m.group(3) if m.group(2) else m.group(4), bool(m.group(1))))
                i = m.end()
                continue
        if q == "'":
            if c == "'":
                q = None
        elif q == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                q = None
            elif c == "$" and s[i + 1:i + 2] == "(":
                i = _match_paren(s, i + 2)
            elif c == "`":
                j = _match_backtick(s, i + 1)
                i = j
        else:
            if c == "\\":
                i += 1
            elif c in "'\"":
                q = c
            elif c == "`":
                i = _match_backtick(s, i + 1)
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
            elif c == "#" and (i == 0 or s[i - 1] in " \t\n;&|("):
                nl = s.find("\n", i)
                if nl == -1:
                    break
                i = nl
                continue
        i += 1
    raise Unparseable("unbalanced-substitution")


def _match_backtick(s: str, i: int) -> int:
    n = len(s)
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    raise Unparseable("unbalanced-backtick")


_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class Scan:
    segments: list  # list of (text, sep_before)
    funcs: set      # names of shell functions defined in this text
    subs: list      # list of (kind, inner_text)
    heredocs: list  # list of dict(body, quoted)


_SUB_START = re.compile(r"\$\(|`")
_MAX_SUBSTITUTION_SCAN_DEPTH = 32


def _collect_substitutions(text: str, subs: list, where: str, depth: int = 0) -> None:
    """Append every command substitution in `text` to `subs`.

    For places whose *value* the classifier does not model but whose embedded
    `$( )` and backticks still run: arithmetic expressions and unquoted heredoc
    bodies. Scanning that text as shell is the cheapest way to find them, and
    the result's own segments are thrown away.

    Anything that will not scan becomes unknown rather than ok: a substitution
    that cannot be found is not the same as there being none.
    """
    if not _SUB_START.search(text):
        return
    if depth >= _MAX_SUBSTITUTION_SCAN_DEPTH:
        raise Unparseable(f"{where}-substitution-too-deep")
    try:
        subs.extend(prescan(text, depth + 1).subs)
    except Unparseable as exc:
        raise Unparseable(f"{where}-with-unscannable-substitution:{exc}") from exc
    except RecursionError as exc:
        raise Unparseable(f"{where}-substitution-too-deep") from exc


def prescan(text: str, substitution_scan_depth: int = 0) -> Scan:
    segs: list = []
    subs: list = []
    heredocs: list = []
    pending: list = []
    funcs: set = set()
    cur: list = []
    sep_before = ""
    q = None
    i = 0
    n = len(text)

    def word_start() -> bool:
        if not cur:
            return True
        return cur[-1][-1:] in (" ", "\t")

    def flush(sep: str):
        nonlocal sep_before
        segs.append(("".join(cur), sep_before))
        cur.clear()
        sep_before = sep

    def add_sub(kind: str, inner: str):
        subs.append((kind, inner))
        cur.append(f"{SUB}{len(subs) - 1}{SUB}")

    def dollar(i: int) -> int:
        """text[i] == '$' outside single quotes. Returns new index."""
        nxt = text[i + 1:i + 2]
        if text.startswith("$((", i):
            j = _match_paren(text, i + 3)  # matches the inner '('
            if text[j + 1:j + 2] != ")":
                raise Unparseable("unbalanced-arithmetic")
            # The value is not interesting, but `$(( $(touch /x) + 1 ))` runs
            # that touch. Collapsing the whole expression to "0" dropped it.
            _collect_substitutions(
                text[i + 3:j], subs, "arithmetic", substitution_scan_depth
            )
            cur.append("0")
            return j + 2
        if nxt == "(":
            j = _match_paren(text, i + 2)
            add_sub("cmd", text[i + 2:j])
            return j + 1
        if nxt == "{":
            j = text.find("}", i + 2)
            if j == -1:
                raise Unparseable("unbalanced-brace-expansion")
            cur.append(f"{VAR}{text[i + 2:j]}{VAR_END}")
            return j + 1
        m = _NAME_RE.match(text, i + 1)
        if m:
            cur.append(f"{VAR}{m.group(0)}{VAR_END}")
            return m.end()
        if nxt and nxt in "@*#?$!-0123456789":
            cur.append(f"{VAR}{nxt}{VAR_END}")
            return i + 2
        if nxt == "'":  # $'...' ANSI-C string: keep as plain single quotes
            cur.append("$")
            return i + 1
        cur.append("$")
        return i + 1

    while i < n:
        c = text[i]
        if q == "'":
            cur.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if q == '"':
            if c == "\\" and i + 1 < n:
                cur.append(text[i:i + 2])
                i += 2
                continue
            if c == '"':
                q = None
                cur.append(c)
                i += 1
                continue
            if c == "$":
                i = dollar(i)
                continue
            if c == "`":
                j = _match_backtick(text, i + 1)
                add_sub("cmd", text[i + 1:j])
                i = j + 1
                continue
            cur.append(c)
            i += 1
            continue
        # ---- unquoted ----
        if c == "\\":
            if text[i + 1:i + 2] == "\n":
                i += 2
                continue
            cur.append(text[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            q = c
            cur.append(c)
            i += 1
            continue
        if c == "$":
            i = dollar(i)
            continue
        if c == "`":
            j = _match_backtick(text, i + 1)
            add_sub("cmd", text[i + 1:j])
            i = j + 1
            continue
        if c == "[" and text.startswith("[[", i) and word_start():
            end = text.find("]]", i + 2)
            if end != -1:
                inner = text[i + 2:end]
                if "$(" not in inner and "`" not in inner and "\n" not in inner:
                    inner = re.sub(r"&&|\|\||[<>()]", " ", inner)
                    text = text[:i + 2] + inner + text[end:]
                    n = len(text)
            cur.append("[[")
            i += 2
            continue
        if c == "#" and word_start():
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if c == "\n":
            if pending:
                i = _read_heredoc_bodies(text, i + 1, pending, heredocs)
                pending.clear()
            else:
                i += 1
            flush("\n")
            continue
        if c.isdigit() and word_start():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] in "<>":
                i = j
                continue
        if c == "<":
            if text.startswith("<<<", i):
                cur.append(f" {REDIR}<<< ")
                i += 3
                continue
            if text.startswith("<<", i):
                j = i + 2
                strip = False
                if text[j:j + 1] == "-":
                    strip = True
                    j += 1
                while j < n and text[j] in " \t":
                    j += 1
                m = re.match(r"""(['"])(.*?)\1|\\?([A-Za-z0-9_.\-]+)""", text[j:])
                if not m:
                    raise Unparseable("bad-heredoc-delimiter")
                delim = m.group(2) if m.group(1) else m.group(3)
                quoted = bool(m.group(1)) or text[j] == "\\"
                idx = len(heredocs)
                heredocs.append({"body": "", "quoted": quoted})
                pending.append((delim, strip, idx))
                cur.append(f" {HEREDOC}{idx}{HEREDOC} ")
                i = j + m.end()
                continue
            if text.startswith("<(", i):
                j = _match_paren(text, i + 2)
                add_sub("proc", text[i + 2:j])
                i = j + 1
                continue
            if re.match(r"<&(\d+|-)", text[i:]):
                i += len(re.match(r"<&(\d+|-)", text[i:]).group(0))
                continue
            cur.append(f" {REDIR}< ")
            i += 1
            continue
        if c == ">":
            if text.startswith(">(", i):
                j = _match_paren(text, i + 2)
                add_sub("proc", text[i + 2:j])
                i = j + 1
                continue
            m = re.match(r">&(\d+|-)", text[i:])
            if m:
                i += len(m.group(0))
                continue
            if text.startswith(">>", i):
                cur.append(f" {REDIR}>> ")
                i += 2
                continue
            if text.startswith(">|", i) or text.startswith(">&", i):
                cur.append(f" {REDIR}> ")
                i += 2
                continue
            cur.append(f" {REDIR}> ")
            i += 1
            continue
        if c == "&":
            if text.startswith("&>>", i):
                cur.append(f" {REDIR}>> ")
                i += 3
                continue
            if text.startswith("&>", i):
                cur.append(f" {REDIR}> ")
                i += 2
                continue
            if text.startswith("&&", i):
                flush("&&")
                i += 2
                continue
            flush("&")
            i += 1
            continue
        if c == "|":
            if text.startswith("||", i):
                flush("||")
                i += 2
                continue
            if text.startswith("|&", i):
                flush("|")
                i += 2
                continue
            flush("|")
            i += 1
            continue
        if c == ";":
            if text.startswith(";;", i):
                flush(";;")
                i += 2
            else:
                flush(";")
                i += 1
            continue
        if c == "(":
            if cur and cur[-1].endswith("="):  # array literal: name=(a b c)
                j = _match_paren(text, i + 1)
                cur.append("__array__")
                i = j + 1
                continue
            if text.startswith("((", i):
                j = _match_paren(text, i + 2)
                if text[j + 1:j + 2] != ")":
                    raise Unparseable("unbalanced-arithmetic")
                flush(";")
                i = j + 2
                continue
            m = re.match(r"\(\s*\)", text[i:])
            if m and "".join(cur).strip() and re.fullmatch(r"\s*(function\s+)?[\w.:-]+\s*", "".join(cur)):
                funcs.add("".join(cur).split()[-1])
                cur.clear()  # function definition header; body follows as a group
                i += len(m.group(0))
                continue
            flush("(")
            i += 1
            continue
        if c == ")":
            flush(")")
            i += 1
            continue
        cur.append(c)
        i += 1
    if q is not None:
        raise Unparseable("unbalanced-quote")
    flush("")
    for heredoc in heredocs:
        if not heredoc["quoted"]:
            _collect_substitutions(
                heredoc["body"], subs, "heredoc", substitution_scan_depth
            )
    return Scan(segs, funcs, subs, heredocs)


def _read_heredoc_bodies(text: str, i: int, pending: list, heredocs: list) -> int:
    for delim, strip, idx in pending:
        lines = []
        while True:
            if i >= len(text):
                break
            nl = text.find("\n", i)
            line = text[i:] if nl == -1 else text[i:nl]
            i = len(text) if nl == -1 else nl + 1
            cmp = line.lstrip("\t") if strip else line
            if cmp == delim:
                break
            lines.append(line)
        heredocs[idx]["body"] = "\n".join(lines)
    return i


def split_words(seg: str) -> list:
    try:
        lex = shlex.shlex(seg, posix=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError as e:
        raise Unparseable("unbalanced-quote") from e


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def _expand_user(p: str) -> str:
    if p == "~":
        return HOME
    if p.startswith("~/"):
        return HOME + p[1:]
    return p


def _glob_to_re(pat: str) -> str:
    out = []
    i = 0
    while i < len(pat):
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return "".join(out)


class Policy:
    def __init__(self, raw: dict, settings_deny: list):
        self.raw = raw
        bc = raw.get("_backcheck", {})
        self.bc = bc
        fs = raw.get("filesystem", {})
        self.allow_write = [os.path.normpath(_expand_user(p)) for p in fs.get("allowWrite", [])]
        self.deny_write = [os.path.normpath(_expand_user(p)) for p in fs.get("denyWrite", [])]
        self.always_writable = bc.get("always_writable", ["/dev/null"])
        net = raw.get("network", {})
        self.domains = [d.lower() for d in net.get("allowedDomains", [])]
        self.unix_sockets = bool(net.get("allowAllUnixSockets"))
        # Socket reachability is not modelled per call: the seccomp filter
        # blocks socket(AF_UNIX, ...) wholesale, which reaches the nix daemon,
        # the user bus, ssh-agent and anything else over a socket, and this
        # tool cannot tell from a command line which of those a program will
        # open. Refuse the policy rather than measure it wrong -- the policy
        # shape actually in use keeps sockets on and masks the ones it does not
        # want through denyRead, which IS modelled.
        if not self.unix_sockets:
            raise PolicyUnsupported(
                "network.allowAllUnixSockets is false: this back-check cannot model "
                "per-call socket use, and would report calls as ok that the sandbox "
                "would block. Express socket restrictions as filesystem.denyRead on "
                "the socket paths instead."
            )
        self.dbus_broken = bc.get("dbus_user_bus") == "broken"
        self.nix_dbus = bool(bc.get("nix_wrapper_uses_systemd_run_user")) and self.dbus_broken
        self.gh_broken = bc.get("gh_auth") == "broken"
        self.git_https_broken = bc.get("git_https_auth") == "broken"
        self.private_owners = {o.lower() for o in bc.get("private_github_owners", [])}
        self.ssh_ok = bool(bc.get("ssh_dir_readable", False))
        pats = []
        self.deny_labels = []
        ignore = set(bc.get("read_deny_not_in_sandbox", []))
        for rule in settings_deny + [f"Read({p})" for p in fs.get("denyRead", [])]:
            m = re.fullmatch(r"Read\((.*)\)", rule)
            if not m or m.group(1) in ignore:
                continue
            p = m.group(1)
            if p.startswith("//"):
                p = p[1:]
            elif p.startswith("~/"):
                p = HOME + p[1:]
            elif not p.startswith("/"):
                continue
            pats.append(_glob_to_re(p))
            self.deny_labels.append(m.group(1))
        self.deny_res = [re.compile(p) for p in pats]

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for d in self.domains:
            if d.startswith("*."):
                if host.endswith(d[1:]) or host == d[2:]:
                    return True
            elif host == d:
                return True
        return False

    def denied_read(self, path: str):
        for rx, label in zip(self.deny_res, self.deny_labels):
            if rx.fullmatch(path) or rx.fullmatch(path.rstrip("/") + "/x"):
                return label
        return None

    @staticmethod
    def _under(path: str, root: str) -> bool:
        return path == root or path.startswith(root.rstrip("/") + "/")

    def write_allowed(self, path: str, root: str | None) -> bool:
        # denyWrite wins over allowWrite and over the cwd: it exists to carve a
        # hole out of an allowed root, so checking it first is the whole point.
        for d in self.deny_write:
            if self._under(path, d):
                return False
        roots = list(self.allow_write)
        if root:
            roots.append(root)
        for a in self.always_writable:
            if self._under(path, a):
                return True
        for r in roots:
            if self._under(path, r):
                return True
        return False


def short_deny_label(label: str) -> str:
    label = label.replace("//", "/", 1) if label.startswith("//") else label
    return re.sub(r"/\*\*$", "", label)


# ---------------------------------------------------------------------------
# Git helpers (cached)
# ---------------------------------------------------------------------------

class GitInfo:
    def __init__(self):
        self.cache: dict = {}

    def _existing_repo_dir(self, d: str) -> str | None:
        if os.path.isdir(d):
            return d
        # Removed worktree: map ~/worktrees/<repo>-<slug> back to the primary checkout.
        wt = os.path.join(HOME, "worktrees") + "/"
        if d.startswith(wt):
            name = d[len(wt):].split("/")[0]
            if name.startswith("claude-"):
                return os.path.join(HOME, ".claude")
            parts = name.split("-")
            for k in range(len(parts) - 1, 0, -1):
                cand = os.path.join(HOME, "Repos", "-".join(parts[:k]))
                if os.path.isdir(cand):
                    return cand
        p = d
        while p and p != "/" and not os.path.isdir(p):
            p = os.path.dirname(p)
        if not p or p == "/":
            return None
        if p != d and not os.path.exists(os.path.join(p, ".git")):
            # parent of removed worktrees (e.g. ~/Repos/<x>-worktrees): any sibling shares the remotes
            try:
                for e in sorted(os.listdir(p)):
                    if os.path.exists(os.path.join(p, e, ".git")):
                        return os.path.join(p, e)
            except OSError:
                pass
        return p

    def info(self, d: str) -> dict:
        if d in self.cache:
            return self.cache[d]
        res = {"remotes": {}, "common_dir": None, "toplevel": None}
        rd = self._existing_repo_dir(d)
        if rd:
            try:
                out = subprocess.run(["git", "-C", rd, "remote", "-v"], capture_output=True,
                                     text=True, timeout=10).stdout
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and (len(parts) < 3 or parts[2] == "(push)" or parts[0] not in res["remotes"]):
                        res["remotes"][parts[0]] = parts[1]
                out = subprocess.run(["git", "-C", rd, "rev-parse", "--path-format=absolute",
                                      "--git-common-dir", "--show-toplevel"],
                                     capture_output=True, text=True, timeout=10)
                if out.returncode == 0:
                    lines = out.stdout.splitlines()
                    if lines:
                        res["common_dir"] = lines[0]
                    if len(lines) > 1:
                        res["toplevel"] = lines[1]
            except (OSError, subprocess.SubprocessError):
                pass
        self.cache[d] = res
        return res


def url_host(url: str):
    """Return (host, transport, owner) for a git/http URL, or None for local."""
    m = re.match(r"^([a-z+]+)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/?([^/]*)", url)
    if m:
        scheme = m.group(1)
        if scheme == "file":
            return None
        transport = "ssh" if "ssh" in scheme else "https" if scheme.startswith("http") else scheme
        return m.group(2).lower(), transport, m.group(3).lower()
    m = re.match(r"^(?:[^@/]+@)?([A-Za-z0-9.-]+):(?!//)([^/]*)", url)
    if m and "/" not in url.split(":")[0]:
        return m.group(1).lower(), "ssh", m.group(2).lower()
    return None


# ---------------------------------------------------------------------------
# Command knowledge
# ---------------------------------------------------------------------------

LOCAL_OK = set("""
ls cat head tail grep rg ugrep egrep fgrep sed awk gawk find fd wc sort uniq cut tr echo printf true false
test [ [[ ]] pwd date sleep which type file stat du df free ps pgrep pkill kill killall jq yq diff cmp comm
basename dirname realpath readlink printenv uname hostname id whoami nproc uptime lsblk column xxd od hexdump
base64 sha256sum sha1sum md5sum b2sum tree less more nl rev tac paste join fold fmt seq yes expr bc tput clear
export set unset local read shift alias wait trap exit return break continue declare typeset let shopt ulimit
umask history pytest shellcheck shfmt ruff black mypy pyright eslint prettier tsc rustc gcc cc clang ld
nixfmt nixpkgs-fmt alejandra statix deadnix tmux kitty wl-copy wl-paste xclip lsof ss ip strings
iconv numfmt timedatectl getent locale sum cksum split csplit zcat gzip gunzip bzip2 xz unxz zstd tar unzip zip
ffprobe identify exiftool pdftotext pdfinfo mediainfo sqlite3 psql pg_isready jless glow bat delta dust duf htop
btop top vmstat iostat sensors lscpu lspci lsusb dmesg nvidia-smi watch env getconf ldd nm objdump readelf
mktemp cp mv rm rmdir mkdir touch ln install chmod chown truncate tee dd rsync trash trash-put patch
git-lfs diffstat wdiff colordiff vim nvim nano emacs code python python3 node deno ruby perl lua bash sh zsh
dash fish source . cd pushd popd timeout nice nohup time command exec stdbuf ionice setsid xargs sudo doas
pkexec run0 gitleaks hyperfine tokei scc cloc ffmpeg convert magick pandoc docker kubectl column awk sponge
chronic unbuffer flock fc-list fc-match xdg-mime systemd-analyze systemd-escape loginctl
: print fuser lslocks rustfmt actionlint pdftoppm pdfimages pstree col man age ssh-keygen dcg openssl
cargo-fmt clippy-driver ruff-format taplo yamllint markdownlint hadolint shellharden jc gron xmllint
envsubst lsattr getfacl inotifywait timeout-wrapper pv mkfifo nproc lscpu w last journalctl
""".split())

NO_READ = {"echo", "printf", "export", "set", "unset", "alias", "trap", "which", "type", "basename", "dirname",
           "true", "false", "sleep", "date", "seq", "expr", "bc", "printenv", "kill", "pkill", "pgrep", "killall",
           "read", "local", "declare", "typeset", "let", "exit", "return", "wl-copy", "tput", "id", "uname"}
DBUS_ALWAYS = {"notify-send", "gdbus", "dbus-send", "secret-tool", "gio", "xdg-open", "gsettings",
               "dconf", "systemd-inhibit", "busctl", "kwallet-query", "gnome-keyring-daemon", "qdbus"}
SYSTEMD_USER_CMDS = {"systemctl", "journalctl", "systemd-run", "busctl", "systemd-analyze", "loginctl"}
SYSTEMCTL_READ = {"status", "show", "is-active", "is-enabled", "is-failed", "list-units", "list-timers",
                  "cat", "list-unit-files", "list-dependencies", "list-jobs", "list-sockets", "help"}
NIX_CMDS = {"nix", "nix-shell", "nix-build", "nix-store", "nix-instantiate", "nix-env", "nix-collect-garbage",
            "nix-prefetch-url", "nix-prefetch-git", "nix-channel", "nixos-rebuild", "home-manager",
            "nix-copy-closure", "nix-hash", "nix-locate", "nixos-option", "nix-tree", "nvd", "nh",
            "nixos-container", "nix-diff", "nix-update", "nix-fast-build", "nixos-generate-config"}
SHELLS = {"bash", "sh", "zsh", "dash", "fish"}
INTERPRETERS = {"python", "python3", "node", "deno", "ruby", "perl", "lua", "bun", "tsx", "ts-node"}
WRAPPERS = {"timeout", "nice", "nohup", "time", "command", "exec", "stdbuf", "ionice", "setsid", "chronic",
            "unbuffer", "env", "xargs", "flock", "builtin", "caffeinate", "systemd-inhibit"}
SUDO = {"sudo", "doas", "pkexec", "run0"}
SETUID = {"crontab", "passwd", "su", "newgrp", "fusermount", "fusermount3", "chsh", "mount", "umount"}
NETWORK_TOOLS = {"curl", "wget", "http", "https", "xh", "httpie", "aria2c", "ping", "dig", "nslookup", "host",
                 "nc", "ncat", "netcat", "socat", "telnet", "traceroute", "mtr", "whois"}
SSH_TOOLS = {"ssh", "scp", "sftp", "mosh", "ssh-keyscan", "ssh-copy-id", "sshfs"}
WRITE_ALL_ARGS = {"mkdir", "touch", "rm", "rmdir", "truncate", "trash", "trash-put", "shred", "unlink"}
WRITE_LAST_ARG = {"cp", "mv", "install", "ln", "rsync"}
LLM_CLIS = {"claude": "api.anthropic.com", "codex": "chatgpt.com", "ollama": "localhost", "gemini": "generativelanguage.googleapis.com"}
PKG_HOSTS = {
    "pip": ["pypi.org", "files.pythonhosted.org"],
    "uv": ["pypi.org", "files.pythonhosted.org"],
    "npm": ["registry.npmjs.org"],
    "cargo": ["index.crates.io", "static.crates.io"],
    "go": ["proxy.golang.org", "sum.golang.org"],
}
GIT_NET = {"push", "fetch", "pull", "clone", "ls-remote", "submodule"}
GIT_WRITE = {"commit", "add", "fetch", "pull", "push", "checkout", "switch", "merge", "rebase", "reset", "stash",
             "tag", "worktree", "cherry-pick", "revert", "am", "rm", "mv", "restore", "gc", "update-ref", "notes",
             "clean", "init", "apply", "prune", "repack", "maintenance", "commit-tree", "update-index", "sparse-checkout",
             "submodule", "bisect", "replace", "filter-branch", "pack-refs", "reflog", "lfs"}
GIT_WORKTREE_WRITE = {"checkout", "switch", "merge", "rebase", "reset", "stash", "rm", "mv", "restore", "clean",
                      "apply", "pull", "cherry-pick", "revert", "am", "sparse-checkout", "bisect"}
URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://(?:[^@/\s'\"]+@)?([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])", re.I)
SCRIPT_MARKERS = [
    (re.compile(r"(^|[\s;&|(`$])gh\s+(pr|api|run|repo|issue|auth|release|workflow|search|gist|label|secret)\b"
                r"|[\"']gh[\"']\s*,\s*[\"'](pr|api|run|repo|issue|release|workflow|search)[\"']", re.M), "gh-auth"),
    (re.compile(r"(systemctl|journalctl|systemd-run|busctl)\s+--user|notify-send|gdbus|dbus-send|secret-tool"), "dbus"),
    (re.compile(r"(^|[\s;&|(`$\"'])nix(-shell|-build|-store|-instantiate|-env)?\s+(build|shell|develop|run|eval|flake|store|path-info|search|profile|copy|-p|--run|-A|<)", re.M), "dbus"),
    (re.compile(r"(^|[\s;&|(])sudo\s", re.M), "sudo"),
    (re.compile(r"(^|[\s;&|(`$])git\s+(-C\s+\S+\s+)?(push|fetch|pull|clone|ls-remote)\b", re.M), "git-auth"),
]


@dataclass
class Ctx:
    cwd: str | None
    root: str | None
    policy: Policy
    git: GitInfo
    vars: dict = field(default_factory=dict)
    depth: int = 0
    script_cache: dict | None = None
    subs: list | None = None
    funcs: set = field(default_factory=set)
    files: dict = field(default_factory=dict)

    def child(self) -> "Ctx":
        return Ctx(self.cwd, self.root, self.policy, self.git, dict(self.vars), self.depth + 1,
                   self.script_cache, None, set(self.funcs), self.files)


@dataclass
class Simple:
    family: str
    reasons: list  # strings 'breaks:...' or 'unknown:...'


def is_dyn(w: str) -> bool:
    return any(ch in w for ch in DYN_CHARS)


def unmark(s: str, ctx) -> str:
    """Turn markers back into shell text so a nested shell string re-parses.
    Known variables are substituted; unknown ones become ${name} again and are
    treated as dynamic by the nested parse."""
    s = expand(s, ctx)
    s = re.sub(VAR + r"(.*?)" + VAR_END, lambda m: "${" + m.group(1) + "}", s)
    subs = ctx.subs or []
    return re.sub(SUB + r"(\d+)" + SUB,
                  lambda m: "$(" + subs[int(m.group(1))][1] + ")" if int(m.group(1)) < len(subs) else "${__SUB}", s)


KNOWN_ENV = {
    "HOME": HOME, "USER": os.environ.get("USER", "jonathan"), "TMPDIR": "/tmp", "TMP": "/tmp",
    "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", "XDG_CONFIG_HOME": HOME + "/.config",
    "XDG_CACHE_HOME": HOME + "/.cache", "XDG_STATE_HOME": HOME + "/.local/state",
    "XDG_DATA_HOME": HOME + "/.local/share",
}


def expand(word: str, ctx: Ctx) -> str:
    """Resolve known parameter expansions; leave unknown ones as markers."""
    def rep(m):
        mm = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?::?[-=](.*))?", m.group(1), re.S)
        if not mm:
            return m.group(0)
        name, default = mm.group(1), mm.group(2)
        if name == "PWD" and ctx.cwd:
            return ctx.cwd
        if name in ("CLAUDE_PROJECT_DIR",) and ctx.root:
            return ctx.root
        if name in ctx.vars:
            return ctx.vars[name]
        if name in KNOWN_ENV:
            return KNOWN_ENV[name]
        if default and not is_dyn(default):
            return default
        return m.group(0)
    w = re.sub(VAR + r"(.*?)" + VAR_END, rep, word)
    if w == "~" or w.startswith("~/"):
        w = HOME + w[1:]
    return w


def resolve_path(word: str, ctx: Ctx):
    """Return (path, exact). exact=False means the path has a dynamic tail and
    only its static directory prefix is returned (or None if nothing static)."""
    w = expand(word, ctx)
    cut = len(w)
    for ch in DYN_CHARS + ("*", "?", "["):
        k = w.find(ch)
        if k != -1:
            cut = min(cut, k)
    exact = cut == len(w)
    w = w[:cut]
    if not exact:
        w = w[: w.rfind("/") + 1] if "/" in w else ""
        if not w:
            if ctx.cwd and cut > 0:
                return ctx.cwd, False
            if cut == 0:
                return None, False
    if not w.startswith("/"):
        if ctx.cwd is None:
            return None, exact
        w = os.path.join(ctx.cwd, w)
    return os.path.normpath(w) if w else None, exact


def classify_text(text: str, ctx: Ctx) -> list:
    if ctx.depth > 8:
        return [Simple("(nesting)", ["unknown:nesting-too-deep"])]
    scan = prescan(text)
    results: list = []
    # Substitutions: classify their inner commands as part of this call.
    for kind, inner in scan.subs:
        sub_ctx = ctx.child()
        results.extend(classify_text(inner, sub_ctx))
    ctx.subs = scan.subs
    ctx.funcs |= scan.funcs
    segs = scan.segments
    in_case = 0
    for k, (seg, sep_before) in enumerate(segs):
        words = split_words(seg)
        if not words:
            continue
        nxt_sep = segs[k + 1][1] if k + 1 < len(segs) else ""
        while len(words) > 1 and words[0] in KEYWORDS_LEAD:
            words = words[1:]
        if words[0] == "case":
            in_case += 1  # 'case WORD in PATTERN' -- the first pattern shares this segment
            continue
        if in_case:
            if words[0] == "esac":
                in_case -= 1
                continue
            if nxt_sep == ")":  # a case pattern such as '*running*'
                continue
        results.extend(classify_words(words, ctx, scan, piped=(sep_before == "|")))
    return results


KEYWORDS_LEAD = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time", "function"}
KEYWORDS_ALONE = {"fi", "done", "esac", "}", "then", "else", "do"}


def classify_words(words: list, ctx: Ctx, scan: Scan, piped: bool) -> list:
    # strip control keywords
    while words and words[0] in KEYWORDS_LEAD:
        words = words[1:]
    while words and words[-1] in ("}",):
        words = words[:-1]
    if not words or (len(words) == 1 and words[0] in KEYWORDS_ALONE):
        return []
    if words[0] == "for" or words[0] == "select":
        if len(words) > 1 and _NAME_RE.fullmatch(words[1]):
            ctx.vars.pop(words[1], None)
        return []
    if words[0] == "in" or words[0].endswith(")") and len(words) == 1:
        return []
    # redirections and heredocs
    argv, redirs, hds = [], [], []
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith(REDIR):
            op = w[1:]
            if i + 1 >= len(words):
                raise Unparseable("dangling-redirection")
            redirs.append((op, words[i + 1]))
            i += 2
            continue
        if w.startswith(HEREDOC) and w.endswith(HEREDOC):
            hds.append(scan.heredocs[int(w.strip(HEREDOC))])
            i += 1
            continue
        argv.append(w)
        i += 1
    if not argv or (len(argv) == 1 and argv[0] in KEYWORDS_ALONE):
        argv = []
        if not redirs:
            return []
    return analyse(argv, redirs, hds, ctx, piped)


def _fam(*parts) -> str:
    return " ".join(p for p in parts if p)


def _first_non_opt(args: list, opts_with_value=()) -> tuple:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return (args[i + 1], i + 1) if i + 1 < len(args) else (None, None)
        if a.startswith("-") and a != "-":
            if a in opts_with_value:
                i += 2
                continue
            i += 1
            continue
        return a, i
    return None, None


def analyse(argv: list, redirs: list, hds: list, ctx: Ctx, piped: bool) -> list:
    out: list = []
    reasons: list = []
    pol = ctx.policy

    # redirections: writes and reads
    for op, target in redirs:
        if op in (">", ">>"):
            check_write(target, ctx, reasons)
            if hds and argv[:1] == ["cat"]:
                tp, exact = resolve_path(target, ctx)
                if exact and tp:
                    ctx.files[tp] = "\n".join(h["body"] for h in hds)
        elif op == "<":
            check_read(target, ctx, reasons)
        elif op == "<<<":
            pass

    # leading assignments
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        name, _, val = argv[0].partition("=")
        set_var(name, val, ctx)
        argv = argv[1:]
    if not argv:
        return [Simple("(assignment)", reasons)] if reasons else []

    name = argv[0]
    if is_dyn(name):
        resolved = expand(name, ctx)
        if is_dyn(resolved):
            return [Simple("(dynamic)", reasons + ["unknown:dynamic-command"])]
        name = resolved
        argv = [name] + argv[1:]
    base = os.path.basename(name) if "/" in name and not name.endswith("/") else name
    if name in ctx.funcs:
        return [Simple("(shell function)", reasons)]
    args = argv[1:]
    fam = base

    # --- wrappers ------------------------------------------------------
    if base in SUDO:
        reasons.append("breaks:sudo")
        inner, idx = _first_non_opt(args, ("-u", "-g", "-C", "-D", "-h", "-p", "-U", "-r", "-t"))
        out.append(Simple(base, reasons))
        if inner is not None:
            out.extend(analyse(args[idx:], [], hds, ctx, piped))
        return out
    if base in ("export", "readonly", "declare", "typeset", "local") and args:
        for a in args:
            if "=" in a and re.match(r"^[A-Za-z_]", a):
                n_, _, v = a.partition("=")
                set_var(n_, v, ctx)
        return [Simple(base, reasons)]
    if base == "eval":
        return [Simple("eval", reasons + ["unknown:eval"])]
    if base in WRAPPERS:
        inner_args = unwrap(base, args, ctx)
        if base == "systemd-inhibit":
            reasons.append("breaks:dbus")
        if base == "flock" and args:
            tgt = next((a for a in args if not a.startswith("-")), None)
            if tgt and not re.fullmatch(r"\d+", tgt):
                check_write(tgt, ctx, reasons)
        out.append(Simple(base, reasons))
        if inner_args:
            out.extend(analyse(inner_args, [], hds, ctx, piped))
        return out
    if base in ("cd", "pushd"):
        tgt, _ = _first_non_opt(args)
        if tgt is None:
            ctx.cwd = HOME
        elif tgt == "-":
            pass
        else:
            p, exact = resolve_path(tgt, ctx)
            ctx.cwd = p if exact else None
        return [Simple("cd", reasons)]
    if base == "popd":
        ctx.cwd = None
        return [Simple("cd", reasons)]

    # --- shells --------------------------------------------------------
    if base in SHELLS:
        cflag = next((k for k, a in enumerate(args) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a) or a == "-c"), None)
        if cflag is not None:
            script = next((a for a in args[cflag + 1:] if not a.startswith("-")), None)
            if script is None:
                return [Simple(base + " -c", reasons + ["unknown:shell-missing-script"])]
            script = unmark(script, ctx)
            sub_ctx = ctx.child()
            out.append(Simple(base + " -c", reasons))
            out.extend(classify_text(script, sub_ctx))
            return out
        if any(op == "<<<" for op, _ in redirs):
            return [Simple(base, reasons + ["unknown:here-string-to-shell"])]
        if "-n" in args:  # syntax check only, executes nothing
            return [Simple(base + " -n", reasons)]
        file_arg, _ = _first_non_opt(args, ("-o", "+o", "-O", "+O"))
        if file_arg:
            return [Simple(base + " <script>", reasons + scan_script(file_arg, ctx))]
        if hds:
            sub_ctx = ctx.child()
            out.append(Simple(base + " <heredoc>", reasons))
            for hd in hds:
                out.extend(classify_text(hd["body"], sub_ctx))
            return out
        if piped:
            return [Simple(base, reasons + ["unknown:pipe-to-shell"])]
        return [Simple(base, reasons + ["unknown:interactive-shell"])]

    # --- nix -------------------------------------------------------------
    if base in NIX_CMDS:
        sub = args[0] if args and base == "nix" and not args[0].startswith("-") else ""
        fam = _fam(base, sub)
        if pol.nix_dbus:
            reasons.append("breaks:dbus(nix-memory-run)")
        net_hosts = ["cache.nixos.org"]
        if any(a.startswith(("github:", "nixpkgs", "git+https://github.com", "https://github.com")) for a in args):
            net_hosts.append("github.com")
        for h in net_hosts:
            check_host(h, ctx, reasons)
        inner = None
        if base == "nix" and sub in ("shell", "develop"):
            for k, a in enumerate(args):
                if a in ("-c", "--command"):
                    inner = args[k + 1:]
                    break
        elif base == "nix-shell":
            for k, a in enumerate(args):
                if a in ("--run", "--command") and k + 1 < len(args):
                    s = args[k + 1]
                    out.append(Simple(fam, reasons))
                    out.extend(classify_text(unmark(s, ctx), ctx.child()))
                    return out
        if base == "nixos-rebuild" and not any(a in ("build", "dry-build", "dry-activate", "build-vm") for a in args):
            reasons.append("breaks:sudo")
        out.append(Simple(fam, reasons))
        if inner:
            out.extend(analyse(inner, [], hds, ctx, piped))
        return out

    # --- git -------------------------------------------------------------
    if base == "git":
        return [git_analyse(args, ctx, reasons)]

    if base == "gh":
        sub = next((a for a in args if not a.startswith("-")), "")
        fam = _fam("gh", sub)
        if sub in ("", "help", "completion", "version") or "--version" in args or "--help" in args:
            return [Simple(fam, reasons)]
        if pol.gh_broken:
            reasons.append("breaks:gh-auth")
        check_host("api.github.com", ctx, reasons)
        for k, a in enumerate(args):
            if a in ("-F", "--body-file", "--input") and k + 1 < len(args) and args[k + 1] != "-":
                v = args[k + 1].lstrip("@")
                if v.startswith(("/", "~", ".")):
                    check_read(v, ctx, reasons)
        return [Simple(fam, reasons)]

    # --- D-Bus ---------------------------------------------------------
    if base in SYSTEMD_USER_CMDS:
        user = "--user" in args
        sub = next((a for a in args if not a.startswith("-")), "")
        fam = _fam(base, "--user" if user else "")
        if user:
            if pol.dbus_broken:
                reasons.append("breaks:dbus")
        elif base == "systemctl":
            if sub in SYSTEMCTL_READ or sub == "":
                reasons.append("unknown:system-bus")
            else:
                reasons.append("breaks:sudo")
        elif base == "systemd-run":
            reasons.append("breaks:sudo")
        elif base in ("busctl", "loginctl"):
            reasons.append("unknown:system-bus")
        return [Simple(fam, reasons)]
    if base in DBUS_ALWAYS:
        if pol.dbus_broken:
            reasons.append("breaks:dbus")
        return [Simple(base, reasons)]

    # --- network tools -------------------------------------------------
    if base in NETWORK_TOOLS:
        hosts = set()
        for a in args:
            for m in URL_RE.finditer(expand(a, ctx)):
                hosts.add(m.group(1).lower())
        if not hosts and base in ("ping", "dig", "nslookup", "host", "nc", "ncat", "netcat", "telnet",
                                  "traceroute", "mtr", "whois", "socat"):
            k = 0
            while k < len(args):
                a = args[k]
                if a in ("-c", "-W", "-i", "-w", "-p", "-t", "-s", "-I", "-q", "-x", "-m", "-f") and base != "socat":
                    k += 2
                    continue
                cand = re.sub(r"^[A-Za-z0-9-]+:", "", a) if base == "socat" else a  # TCP:host:port
                cand = cand.split(":")[0].split(",")[0]
                if not a.startswith("-") and re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}|localhost|\d+\.\d+\.\d+\.\d+", cand):
                    hosts.add(cand.lower())
                    break
                if base == "socat" and re.match(r"(UNIX|unix|EXEC|STDIO|-)", a):
                    break
                k += 1
        if not hosts:
            for a in args:
                if not a.startswith("-") and re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?(/\S*)?", a) \
                        and not os.path.exists(a):
                    hosts.add(a.split("/")[0].split(":")[0].lower())
        if not hosts:
            reasons.append("unknown:network-host")
        for h in hosts:
            if base in ("dig", "nslookup", "host", "ping", "traceroute", "mtr", "whois"):
                reasons.append(f"breaks:network(raw-{base})")
            else:
                check_host(h, ctx, reasons)
        # outputs
        out_opts = {"curl": ("-o", "--output", "-D", "--dump-header", "-c", "--cookie-jar"),
                    "wget": ("-O", "--output-document", "-P", "--directory-prefix", "-o", "--output-file")}.get(base, ())
        for k, a in enumerate(args):
            if a in out_opts and k + 1 < len(args):
                check_write(args[k + 1], ctx, reasons)
            elif "=" in a and a.split("=", 1)[0] in out_opts:
                check_write(a.split("=", 1)[1], ctx, reasons)
        if (base == "curl" and any(a in ("-O", "--remote-name", "-J") for a in args)) or \
                (base == "wget" and not any(a in ("-O", "--output-document", "--spider") for a in args)):
            check_write(ctx.cwd or ".", ctx, reasons)
        return [Simple(base, reasons)]

    if base in SSH_TOOLS or (base == "rsync" and any(re.match(r"^([^/@]+@)?[A-Za-z0-9.-]+:", a) for a in args)):
        fam = base
        if not pol.ssh_ok:
            reasons.append("breaks:denied-read(~/.ssh)")
        host = None
        for a in args:
            m = re.match(r"^(?:[^@/]+@)?([A-Za-z0-9.-]+)(:|$)", expand(a, ctx))
            if not a.startswith("-") and m and (base in ("ssh", "mosh", "ssh-keyscan", "ssh-copy-id") or m.group(2) == ":"):
                host = m.group(1)
                break
        if host:
            check_host(host, ctx, reasons, raw=True)
        else:
            reasons.append("unknown:network-host")
        return [Simple(fam, reasons)]

    if base in SETUID:
        reasons.append("breaks:setuid")
        return [Simple(base, reasons)]
    if base == "nix-memory-run":
        if pol.nix_dbus:
            reasons.append("breaks:dbus(nix-memory-run)")
        out.append(Simple(base, reasons))
        inner = [a for a in args]
        while inner and inner[0].startswith("-"):
            inner = inner[1:]
        if inner and inner[0] == "--":
            inner = inner[1:]
        if inner:
            out.extend(analyse(inner, [], hds, ctx, piped))
        return out
    if base == "openssl" and args[:1] == ["s_client"]:
        m = next((args[k + 1] for k, a in enumerate(args) if a == "-connect" and k + 1 < len(args)), None)
        if m:
            check_host(m.split(":")[0], ctx, reasons)
        else:
            reasons.append("unknown:network-host")
        return [Simple("openssl s_client", reasons)]
    if base in LLM_CLIS:
        if not ("--version" in args or "--help" in args):
            check_host(LLM_CLIS[base], ctx, reasons)
        return [Simple(base, reasons)]

    # --- package managers ------------------------------------------------
    if base in ("pip", "pip3") or (base in ("uv",) and args[:1] in (["pip"], ["sync"], ["add"], ["lock"], ["tool"])):
        sub = args[0] if args else ""
        fam = _fam(base, sub)
        if base != "uv" and sub not in ("install", "download", "wheel", "index"):
            return [Simple(fam, reasons)]
        for h in PKG_HOSTS["pip"]:
            check_host(h, ctx, reasons)
        return [Simple(fam, reasons)]
    if base in ("npm", "npx", "pnpm", "yarn", "bun"):
        sub = args[0] if args and not args[0].startswith("-") else ""
        fam = _fam(base, sub)
        if base == "npm" and sub in ("run", "run-script", "test", "start"):
            reasons.append("unknown:opaque-script(npm run)")
            return [Simple(fam, reasons)]
        reasons.append("breaks:denied-read(~/.npm)")
        check_write(HOME + "/.npm", ctx, reasons)
        for h in PKG_HOSTS["npm"]:
            check_host(h, ctx, reasons)
        return [Simple(fam, reasons)]
    if base == "cargo":
        sub = args[0] if args and not args[0].startswith("-") else ""
        fam = _fam("cargo", sub)
        if sub in ("fetch", "update", "add", "install", "generate-lockfile", "search", "publish"):
            for h in PKG_HOSTS["cargo"]:
                check_host(h, ctx, reasons)
            check_write(HOME + "/.cargo", ctx, reasons)
        return [Simple(fam, reasons)]
    if base == "go":
        sub = args[0] if args else ""
        fam = _fam("go", sub)
        if sub in ("get", "install", "mod", "build", "test", "run"):
            check_write(HOME + "/go", ctx, reasons)
        if sub in ("get", "install", "mod"):
            for h in PKG_HOSTS["go"]:
                check_host(h, ctx, reasons)
        return [Simple(fam, reasons)]
    if base == "docker":
        sub = args[0] if args and not args[0].startswith("-") else ""
        return [Simple(_fam("docker", sub), reasons)]
    if base in ("make", "just", "task", "ninja"):
        return [Simple(base, reasons + ["unknown:opaque-script(" + base + ")"])]

    # --- interpreters ----------------------------------------------------
    perl_inplace = base == "perl" and any(re.fullmatch(r"-[a-zA-Z0-9]*i.*", a) for a in args)
    if (base in INTERPRETERS or re.fullmatch(r"python3(\.\d+)?", base)) and not perl_inplace:
        fam = "python3" if base.startswith("python") else base
        code = None
        for k, a in enumerate(args):
            if a == "-":
                code = "\n".join(h["body"] for h in hds)
                if not hds and piped:
                    reasons.append("unknown:interpreter-stdin")
                break
            if a in ("-c", "-e", "--eval", "-p") and k + 1 < len(args):
                code = args[k + 1]
                break
            if a == "-m" and k + 1 < len(args):
                mod = args[k + 1]
                fam = f"{fam} -m {mod}"
                if mod == "pip":
                    if any(x in args for x in ("install", "download")):
                        for h in PKG_HOSTS["pip"]:
                            check_host(h, ctx, reasons)
                elif mod not in ("pytest", "py_compile", "json.tool", "unittest", "venv", "compileall", "doctest",
                                 "tomllib", "ast", "tokenize", "dis", "pydoc", "this", "timeit", "cProfile",
                                 "http.server", "zipfile", "tarfile", "base64", "calendar", "site", "sysconfig"):
                    reasons.append("unknown:opaque-module")
                break
            if not a.startswith("-"):
                if a != "-":
                    fam = f"{fam} <script>"
                    reasons.extend(scan_script(a, ctx))
                else:
                    code = "\n".join(h["body"] for h in hds)
                break
        else:
            if hds:
                code = "\n".join(h["body"] for h in hds)
            elif piped:
                reasons.append("unknown:interpreter-stdin")
        if code is not None:
            reasons.extend(scan_code(code, ctx))
        return [Simple(fam, reasons)]

    # --- file writers ----------------------------------------------------
    if base in WRITE_ALL_ARGS or base in ("chmod", "chown", "chgrp"):
        targets = [a for a in args if not a.startswith("-")]
        if base in ("chmod", "chown", "chgrp"):
            targets = targets[1:]
        if base == "mkdir" or base == "rm":
            targets = [a for a in targets]
        if not targets and base not in ("chmod",):
            pass
        for t in targets:
            check_write(t, ctx, reasons)
        if any(is_dyn(a) for a in args) and not targets:
            reasons.append("unknown:dynamic-write-path")
        return [Simple(base, reasons)]
    if base in WRITE_LAST_ARG:
        nonopt = []
        tdir = None
        k = 0
        while k < len(args):
            a = args[k]
            if a in ("-t", "--target-directory") and k + 1 < len(args):
                tdir = args[k + 1]
                k += 2
                continue
            if a.startswith("--target-directory="):
                tdir = a.split("=", 1)[1]
            elif a in ("-m", "-o", "-g", "--mode", "--owner", "--group", "-S", "--suffix", "-e", "--exclude",
                       "--include", "--filter", "--rsh") and base in ("install", "rsync", "cp", "mv"):
                k += 2
                continue
            elif not a.startswith("-") or a == "-":
                nonopt.append(a)
            k += 1
        dest = tdir or (nonopt[-1] if len(nonopt) >= (1 if base == "ln" else 2) else None)
        if base == "ln" and len(nonopt) == 1 and not tdir:
            dest = ctx.cwd or "."
        for src in (nonopt[:-1] if not tdir else nonopt):
            check_read(src, ctx, reasons)
            if base == "mv":
                check_write(src, ctx, reasons)
        if dest is not None:
            check_write(dest, ctx, reasons)
        else:
            reasons.append("unknown:write-destination")
        return [Simple(base, reasons)]
    if base == "tee":
        for a in args:
            if not a.startswith("-"):
                check_write(a, ctx, reasons)
        return [Simple(base, reasons)]
    if base in ("sed", "perl") and any(re.fullmatch(r"-[a-zA-Z0-9]*i.*|--in-place.*", a) for a in args):
        files, has_script, k = [], False, 0
        while k < len(args):
            a = args[k]
            if a in ("-e", "-f", "--expression", "--file", "-E") and base == "perl" and a != "-E" or \
                    a in ("-e", "-f", "--expression", "--file") and base == "sed":
                has_script = True
                k += 2
                continue
            if re.fullmatch(r"-[a-zA-Z0-9]*e", a) and base == "perl":  # e.g. -pi -e / -0pe
                has_script = True
                k += 2
                continue
            if not a.startswith("-"):
                files.append(a)
            k += 1
        if not has_script:
            files = files[1:]
        for f in files:
            check_write(f, ctx, reasons)
        return [Simple(base + " -i", reasons)]
    if base == "dd":
        for a in args:
            if a.startswith("of="):
                check_write(a[3:], ctx, reasons)
            if a.startswith("if="):
                check_read(a[3:], ctx, reasons)
        return [Simple(base, reasons)]
    if base == "tar" and args and re.search(r"x|--extract|-x|c|--create", args[0]):
        cdir = None
        for k, a in enumerate(args):
            if a in ("-C", "--directory") and k + 1 < len(args):
                cdir = args[k + 1]
            if a in ("-f", "--file") and k + 1 < len(args) and re.search(r"c", args[0]):
                check_write(args[k + 1], ctx, reasons)
        if re.search(r"x", args[0]):
            check_write(cdir or (ctx.cwd or "."), ctx, reasons)
        return [Simple("tar", reasons)]
    if base == "unzip":
        d = None
        for k, a in enumerate(args):
            if a == "-d" and k + 1 < len(args):
                d = args[k + 1]
        if "-l" not in args:
            check_write(d or (ctx.cwd or "."), ctx, reasons)
        return [Simple("unzip", reasons)]
    if base == "find":
        roots = []
        for a in args:
            if a.startswith("-") or a in ("(", "!", ")"):
                break
            roots.append(a)
        if not roots:
            roots = ["."]
        for r in roots:
            check_read(r, ctx, reasons)
        if "-delete" in args:
            for r in roots:
                check_write(r, ctx, reasons)
        for k, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok"):
                end = next((j for j in range(k + 1, len(args)) if args[j] in (";", "+")), len(args))
                inner = [x.replace("{}", (roots[0].rstrip("/") + "/" + VAR + "F" + VAR_END)) for x in args[k + 1:end]]
                if inner:
                    out.extend(analyse(inner, [], [], ctx, False))
        out.insert(0, Simple("find", reasons))
        return out
    if base in ("ffmpeg", "convert", "magick", "pandoc"):
        for k, a in enumerate(args):
            if a in ("-o", "--output") and k + 1 < len(args):
                check_write(args[k + 1], ctx, reasons)
        if base in ("ffmpeg", "convert", "magick") and args and not args[-1].startswith("-"):
            check_write(args[-1], ctx, reasons)
        return [Simple(base, reasons)]
    if base == "sqlite3":
        db = next((a for a in args if not a.startswith("-")), None)
        if db and db != ":memory:" and "-readonly" not in args:
            check_write(db, ctx, reasons)
        return [Simple(base, reasons)]
    if base in ("source", "."):
        if args:
            reasons.extend(scan_script(args[0], ctx))
        return [Simple("source", reasons)]
    if base in ("kubectl", "psql") and any(a.startswith(("--server", "-h", "--host")) for a in args):
        reasons.append("unknown:network-host")
        return [Simple(base, reasons)]

    # --- generic: read checks on path-like args ----------------------------
    if base in LOCAL_OK:
        if base not in NO_READ:
            for a in args:
                v = a.split("=", 1)[1] if a.startswith("--") and "=" in a else a
                if v.startswith("-"):
                    continue
                check_read(v, ctx, reasons)
        return [Simple(base, reasons)]

    # path-invoked scripts or unknown PATH entries
    if "/" in name:
        return [Simple("script:" + base, reasons + scan_script(name, ctx))]
    found = shutil.which(name)
    if found:
        real = os.path.realpath(found)
        if real.startswith(HOME) or "/.claude/" in real or "/.local/bin/" in found:
            return [Simple("script:" + base, reasons + scan_script(found, ctx))]
    return [Simple(base, reasons + [f"unknown:unrecognized-command"])]


def unwrap(base: str, args: list, ctx: Ctx) -> list:
    i = 0
    if base == "timeout":
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in ("-s", "-k", "--signal", "--kill-after") else 1
        return args[i + 1:]
    if base == "env":
        while i < len(args):
            a = args[i]
            if a in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                if a in ("-C", "--chdir") and i + 1 < len(args):
                    p, exact = resolve_path(args[i + 1], ctx)
                    ctx.cwd = p if exact else None
                if a in ("-S", "--split-string") and i + 1 < len(args):
                    return split_words(args[i + 1]) + args[i + 2:]
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a):
                i += 1
                continue
            break
        return args[i:]
    if base == "xargs":
        while i < len(args) and args[i].startswith("-"):
            a = args[i]
            i += 2 if a in ("-I", "-n", "-P", "-d", "-L", "-s", "-a", "-E", "--max-args", "--max-procs",
                            "--delimiter", "--arg-file", "--replace") else 1
        inner = args[i:]
        return inner + [VAR + "XARGS" + VAR_END] if inner else ["echo"]
    if base == "nice":
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] == "-n" else 1
        return args[i:]
    if base == "ionice":
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in ("-c", "-n", "-p") else 1
        return args[i:]
    if base == "flock":
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in ("-w", "--timeout", "-E", "--conflict-exit-code") else 1
        rest = args[i + 1:]
        if rest[:1] in (["-c"], ["--command"]):
            return ["bash", "-c"] + rest[1:]
        return rest
    if base == "systemd-inhibit":
        while i < len(args) and args[i].startswith("-"):
            i += 1
        return args[i:]
    if base == "command" and args[:1] in (["-v"], ["-V"]):
        return []
    while i < len(args) and args[i].startswith("-"):
        i += 1
    return args[i:]


def set_var(name: str, val: str, ctx: Ctx):
    m = re.fullmatch(SUB + r"(\d+)" + SUB, val)
    if m and ctx.subs is not None and int(m.group(1)) < len(ctx.subs):
        inner = ctx.subs[int(m.group(1))][1].strip()
        if re.match(r"mktemp\b", inner) and not re.search(r"\s(-p|--tmpdir=)", inner):
            ctx.vars[name] = "/tmp/" + VAR + "MKTEMP" + VAR_END
            return
        if inner == "pwd" and ctx.cwd:
            ctx.vars[name] = ctx.cwd
            return
        if re.fullmatch(r"git rev-parse --show-toplevel", inner) and ctx.cwd:
            top = ctx.git.info(ctx.cwd)["toplevel"]
            if top:
                ctx.vars[name] = top
                return
    # Keep the expanded value even if partly dynamic: its static prefix still
    # decides write/read checks (e.g. OUT=/tmp/run-$(date +%s)).
    ctx.vars[name] = expand(val, ctx)


def check_host(host: str, ctx: Ctx, reasons: list, raw: bool = False):
    host = host.strip("[]").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.startswith("127."):
        if not ctx.policy.host_allowed(host):
            reasons.append("breaks:network(localhost)")
        return
    if is_dyn(host):
        reasons.append("unknown:network-host")
        return
    if not ctx.policy.host_allowed(host):
        reasons.append(f"breaks:network({host})")


def check_write(target: str, ctx: Ctx, reasons: list):
    if target in ("-", "/dev/null"):
        return
    p, exact = resolve_path(target, ctx)
    if p is None:
        reasons.append("unknown:dynamic-write-path" if is_dyn(target) else "unknown:dynamic-cwd")
        return
    if ctx.policy.write_allowed(p, ctx.root):
        return
    if not exact and ctx.policy.write_allowed(p.rstrip("/") + "/x", ctx.root):
        return
    reasons.append(f"breaks:write({p})")


def check_read(target: str, ctx: Ctx, reasons: list, quiet: bool = False):
    if "://" in target or not target:
        return
    p, exact = resolve_path(target, ctx)
    if p is None:
        return
    label = ctx.policy.denied_read(p)
    if label:
        reasons.append(f"breaks:denied-read({short_deny_label(label)})")


def scan_code(code: str, ctx: Ctx) -> list:
    reasons = []
    for m in URL_RE.finditer(code):
        check_host(m.group(1), ctx, reasons)
    for tok in re.findall(r"""['"]((?:~|/home/|/root/)[^'"\s]*)['"]""", code):
        check_read(tok, ctx, reasons)
    for rx, why in SCRIPT_MARKERS:
        if why in ("gh-auth", "dbus", "sudo") and rx.search(code):
            reasons.append(f"breaks:{why}[inline]")
    return sorted(set(reasons))


def _scan_markers(text: str, policy: Policy) -> list:
    hits = set()
    for rx, why in SCRIPT_MARKERS:
        if rx.search(text):
            hits.add(why)
    for m in URL_RE.finditer(text):
        h = m.group(1).lower()
        if not policy.host_allowed(h):
            hits.add("network(localhost)" if h in ("localhost", "127.0.0.1", "0.0.0.0") else f"network({h})")
    return sorted(hits)


def scan_script(path_word: str, ctx: Ctx) -> list:
    p, exact = resolve_path(path_word, ctx)
    if not exact or p is None:
        return ["unknown:dynamic-script-path"]
    cache = ctx.script_cache if ctx.script_cache is not None else {}
    if p in ctx.files:
        found = _scan_markers(ctx.files[p], ctx.policy)
    elif p in cache:
        found = cache[p]
    else:
        found = None
        try:
            with open(p, "rb") as f:
                data = f.read(262144)
            if b"\0" in data[:2048]:
                found = "binary"
            else:
                found = _scan_markers(data.decode("utf-8", "replace"), ctx.policy)
        except OSError:
            found = "missing"
        cache[p] = found
    if found == "missing":
        return ["unknown:opaque-script(missing)"]
    if found == "binary":
        return ["unknown:unrecognized-binary"]
    out = []
    for h in found:
        if h == "git-auth" and not ctx.policy.git_https_broken:
            continue
        if h == "gh-auth" and not ctx.policy.gh_broken:
            continue
        if h == "dbus" and not ctx.policy.dbus_broken:
            continue
        out.append(f"breaks:{h}[via-script]")
    return out


def git_analyse(args: list, ctx: Ctx, reasons: list) -> Simple:
    pol = ctx.policy
    repo = ctx.cwd
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            t = args[i + 1]
            if is_dyn(expand(t, ctx)):
                return Simple("git", reasons + ["unknown:dynamic-git-dir"])
            base = repo or ctx.cwd
            t = expand(t, ctx)
            repo = os.path.normpath(t if t.startswith("/") else os.path.join(base or "/", t)) if (base or t.startswith("/")) else None
            i += 2
            continue
        if a in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") and i + 1 < len(args):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    sub = args[i] if i < len(args) else ""
    rest = args[i + 1:]
    fam = _fam("git", sub)
    if sub in ("--version", "help", "version"):
        return Simple(fam, reasons)
    if repo is None:
        if sub in GIT_WRITE or sub in GIT_NET:
            return Simple(fam, reasons + ["unknown:dynamic-cwd"])
        return Simple(fam, reasons)
    info = ctx.git.info(repo)
    # writes into .git (common dir for worktrees)
    write_sub = sub in GIT_WRITE
    pos_rest = [a for a in rest if not a.startswith("-")]
    list_flags = {"--list", "-l", "--contains", "--merged", "--no-merged", "--points-at", "-a", "-r", "-v", "-vv",
                  "--show-current", "-n", "--sort", "--format"}
    if sub in ("branch", "tag"):
        mut = any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "-f", "--force", "-u",
                        "--set-upstream-to", "--unset-upstream", "-a", "-s", "--annotate") and not (sub == "branch" and a == "-a")
                  for a in rest)
        write_sub = mut or (bool(pos_rest) and not any(a.split("=")[0] in list_flags for a in rest))
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        write_sub = False
    if sub == "worktree" and rest[:1] == ["list"]:
        write_sub = False
    if sub == "config" and not any(a in ("--get", "--list", "-l", "--get-all", "--get-regexp", "--show-origin") for a in rest):
        write_sub = True
    if sub == "remote" and rest[:1] in (["add"], ["set-url"], ["remove"], ["rename"], ["prune"], ["update"]):
        write_sub = True
    if sub == "notes" and rest[:1] in (["list"], ["show"], []):
        write_sub = False
    if write_sub:
        targets = []
        if sub == "clone":
            pos = [a for a in rest if not a.startswith("-")]
            targets.append(pos[1] if len(pos) > 1 else (ctx.cwd or "."))
        else:
            if info["common_dir"]:
                targets.append(info["common_dir"])
            else:
                targets.append(os.path.join(repo, ".git"))
            if info["toplevel"] and sub in GIT_WORKTREE_WRITE:
                targets.append(info["toplevel"])
        if sub == "worktree" and rest[:1] == ["add"]:
            pos = [a for a in rest[1:] if not a.startswith("-")]
            if pos:
                targets.append(pos[0])
        for t in dict.fromkeys(targets):
            check_write(t, ctx, reasons)
    # network
    net = sub in ("push", "fetch", "pull", "clone", "ls-remote") or (sub == "remote" and rest[:1] == ["update"]) \
        or (sub == "submodule" and rest[:1] == ["update"])
    if net:
        urls = []
        pos = [a for a in rest if not a.startswith("-")]
        if sub == "clone":
            urls = pos[:1]
        elif "--all" in rest or sub in ("remote", "submodule"):
            urls = list(info["remotes"].values())
        else:
            name = pos[0] if pos else "origin"
            if url_host(name) or "/" in name:
                urls = [name]
            elif name in info["remotes"]:
                urls = [info["remotes"][name]]
            elif not pos and len(info["remotes"]) == 1:
                urls = list(info["remotes"].values())
        if not urls:
            reasons.append("unknown:git-remote-unresolvable")
        for u in urls:
            if is_dyn(u):
                reasons.append("unknown:git-remote-unresolvable")
                continue
            h = url_host(u)
            if h is None:
                continue
            host, transport, owner = h
            if transport == "ssh":
                if not pol.ssh_ok:
                    reasons.append("breaks:denied-read(~/.ssh)")
                check_host(host, ctx, reasons)
                continue
            check_host(host, ctx, reasons)
            needs_auth = sub == "push" or owner in pol.private_owners
            if needs_auth and pol.git_https_broken and host.endswith("github.com"):
                reasons.append("breaks:git-auth")
    return Simple(fam, reasons)


# ---------------------------------------------------------------------------
# Public classification entry point
# ---------------------------------------------------------------------------

def classify(command: str, cwd: str | None, policy: Policy, git: GitInfo | None = None,
             script_cache: dict | None = None) -> dict:
    ctx = Ctx(cwd=cwd, root=cwd, policy=policy, git=git or GitInfo(),
              script_cache=script_cache if script_cache is not None else {})
    try:
        simples = classify_text(command, ctx)
    except Unparseable as e:
        return {"status": "unknown", "reasons": [f"unknown:{e}"], "simples": [("(unparseable)", [f"unknown:{e}"])]}
    except RecursionError:
        return {"status": "unknown", "reasons": ["unknown:nesting-too-deep"], "simples": []}
    reasons = []
    for s in simples:
        for r in s.reasons:
            if r not in reasons:
                reasons.append(r)
    if any(r.startswith("breaks:") for r in reasons):
        status = "breaks"
    elif any(r.startswith("unknown:") for r in reasons):
        status = "unknown"
    else:
        status = "ok"
    return {"status": status, "reasons": reasons, "simples": [(s.family, s.reasons) for s in simples]}


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def _parse_ts(ts: str):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def read_claude(projects_dir: str, cutoff: dt.datetime):
    files = glob.glob(os.path.join(projects_dir, "*", "*.jsonl")) + \
        glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"))
    seen = set()
    cut_epoch = cutoff.timestamp()
    for f in files:
        try:
            if os.path.getmtime(f) < cut_epoch:
                continue
            fh = open(f, "rb")
        except OSError:
            continue
        with fh:
            for line in fh:
                if b'"Bash"' not in line or b'"tool_use"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") != "assistant":
                    continue
                ts = _parse_ts(r.get("timestamp", ""))
                if ts is None or ts < cutoff:
                    continue
                content = (r.get("message") or {}).get("content") or []
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict) or c.get("type") != "tool_use" or c.get("name") != "Bash":
                        continue
                    tid = c.get("id")
                    if tid in seen:
                        continue
                    seen.add(tid)
                    cmd = (c.get("input") or {}).get("command")
                    if isinstance(cmd, str) and cmd.strip():
                        yield {"source": "claude", "command": cmd, "cwd": r.get("cwd"), "ts": ts}


_JS_CMD = re.compile(r"""exec_command\(\s*\{\s*["']?cmd["']?\s*:\s*("(?:[^"\\]|\\.)*")""")
_JS_WD = re.compile(r"""["']?workdir["']?\s*:\s*("(?:[^"\\]|\\.)*")""")


def read_codex(sessions_dir: str, cutoff: dt.datetime, stats: dict):
    cut_epoch = cutoff.timestamp()
    for f in glob.glob(os.path.join(sessions_dir, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) < cut_epoch:
                continue
            fh = open(f, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        cwd = None
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                p = r.get("payload")
                if not isinstance(p, dict):
                    continue
                if r.get("type") == "turn_context" and p.get("cwd"):
                    cwd = p["cwd"]
                    continue
                if p.get("type") not in ("custom_tool_call", "function_call"):
                    continue
                ts = _parse_ts(r.get("timestamp", ""))
                if ts is None or ts < cutoff:
                    continue
                name = p.get("name")
                cmds = []
                if name == "exec" and isinstance(p.get("input"), str):
                    src = p["input"]
                    wd = _JS_WD.search(src)
                    try:
                        wdv = json.loads(wd.group(1)) if wd else cwd
                    except ValueError:
                        wdv = cwd
                    for m in _JS_CMD.finditer(src):
                        try:
                            cmds.append((json.loads(m.group(1)), wdv))
                        except ValueError:
                            stats["codex_unparsed"] += 1
                    if not cmds and "exec_command" in src:
                        stats["codex_unparsed"] += 1
                elif name in ("shell", "exec_command", "shell_command", "local_shell"):
                    try:
                        a = json.loads(p.get("arguments") or "{}")
                    except ValueError:
                        stats["codex_unparsed"] += 1
                        continue
                    c = a.get("cmd") or a.get("command")
                    if isinstance(c, list):
                        c = c[-1] if len(c) >= 3 and c[0] in ("bash", "sh", "zsh") and c[1].endswith("c") else shlex.join(c)
                    if isinstance(c, str):
                        cmds.append((c, a.get("workdir") or cwd))
                    else:
                        stats["codex_unparsed"] += 1
                for c, wd in cmds:
                    if c.strip():
                        yield {"source": "codex", "command": c, "cwd": wd, "ts": ts}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SECRET_RES = [
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})"), "<token>"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|sk-ant-[A-Za-z0-9_-]+|xox[abprs]-[A-Za-z0-9-]+|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})"), "<token>"),
    (re.compile(r"(?i)\b(bearer|authorization:?)\s+\S+"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b([A-Za-z_]*(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|auth|credential|private[_-]?key)[A-Za-z_]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1=<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[0-9a-fA-F]{20,}\b"), "<hex>"),
    (re.compile(r"(?=[A-Za-z0-9+_-]*\d)(?=[A-Za-z0-9+_-]*[A-Za-z])[A-Za-z0-9+_-]{32,}={0,2}"), "<b64>"),
]
_DEEP_DIRS = {"Repos", "repos", "worktrees", "Projects", "projects", "src", "code", "work", "clients", "Documents",
              "Downloads", "Desktop", "dev", "git", "Sync", "Dropbox", "nixos-config-worktrees"}


def redact_path(p: str) -> str:
    pre = ""
    if p.startswith(HOME):
        pre, rest = "~", p[len(HOME):]
    elif p.startswith("~"):
        pre, rest = "~", p[1:]
    else:
        rest = p
    parts = [x for x in rest.split("/") if x]
    keep = []
    for x in parts:
        keep.append(x)
        if x in _DEEP_DIRS or len(keep) >= 2:
            break
    out = pre + "/" + "/".join(keep)
    if len(keep) < len(parts):
        out += "/…"
    return out


_PATH_RE = re.compile(r"(?:(?<=[\s=:'\"(])|^)(~?/[^\s'\";|&)<>]*)")
_URL_FULL_RE = re.compile(r"\b([a-z]+://)([^/\s'\"]+)(/[^\s'\"]*)?")
_SCP_RE = re.compile(r"\b(git@[A-Za-z0-9.-]+:)[^\s'\"]+")


def redact(text: str, limit: int = 80, focus: str | None = None) -> str:
    t = text.replace("\n", " ⏎ ")
    for rx, rep_ in _SECRET_RES[:5]:
        t = rx.sub(rep_, t)
    t = _URL_FULL_RE.sub(lambda m: m.group(1) + m.group(2) + ("/…" if m.group(3) and len(m.group(3)) > 1 else ""), t)
    t = _SCP_RE.sub(lambda m: m.group(1) + "…", t)
    t = re.sub(r"\bgithub\.com[/:][^\s'\"]+", "github.com/…", t)
    t = _PATH_RE.sub(lambda m: redact_path(m.group(1)), t)
    for rx, rep_ in _SECRET_RES[5:]:
        t = rx.sub(rep_, t)
    t = re.sub(r"\s+", " ", t).strip()
    if focus and len(t) > limit:
        k = t.find(focus)
        if k > limit // 2:
            t = "…" + t[k - 10:]
    if len(t) > limit:
        t = t[: limit - 1] + "…"
    return t


# ---------------------------------------------------------------------------
# Aggregation / report
# ---------------------------------------------------------------------------

def reason_kind(r: str) -> str:
    return re.sub(r"[(\[].*$", "", r)


def write_root(p: str) -> str:
    if p.startswith(HOME + "/"):
        parts = p[len(HOME) + 1:].split("/")
        n = 2 if parts[0] in _DEEP_DIRS or parts[0] in (".local", ".config") else 1
        return "~/" + "/".join(parts[:n])
    parts = [x for x in p.split("/") if x]
    return "/" + "/".join(parts[:2])


def suggestion_key(r: str):
    m = re.fullmatch(r"breaks:network\(([^)]+)\)(\[.*\])?", r)
    if m and m.group(1) != "localhost" and not m.group(1).startswith("raw-"):
        return "domain", m.group(1)
    m = re.fullmatch(r"breaks:write\((.+)\)(\[.*\])?", r)
    if m:
        return "write", write_root(m.group(1))
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--settings", default=os.path.join(HOME, ".claude", "settings.json"))
    ap.add_argument("--claude-projects", default=os.path.join(HOME, ".claude", "projects"))
    ap.add_argument("--codex-sessions", default=os.path.join(HOME, ".codex", "sessions"))
    ap.add_argument("--no-codex", action="store_true")
    ap.add_argument("--now", help="ISO timestamp treated as now (testing)")
    ap.add_argument("--out", help="markdown report path")
    ap.add_argument("--json", help="JSON aggregates path")
    ap.add_argument("--sample", type=int, default=0, help="dump N random classified calls (UNREDACTED) to --sample-out")
    ap.add_argument("--sample-out")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)

    with open(a.policy) as f:
        raw = json.load(f)
    deny = []
    try:
        with open(a.settings) as f:
            deny = json.load(f).get("permissions", {}).get("deny", [])
    except (OSError, ValueError):
        pass
    try:
        policy = Policy(raw, deny)
    except PolicyUnsupported as exc:
        print(f"error: unsupported policy: {exc}", file=sys.stderr)
        return 2
    now = _parse_ts(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=a.days)

    stats = collections.Counter()
    git = GitInfo()
    script_cache: dict = {}
    calls = []
    sources = [read_claude(a.claude_projects, cutoff)]
    if not a.no_codex and os.path.isdir(a.codex_sessions):
        sources.append(read_codex(a.codex_sessions, cutoff, stats))
    for src in sources:
        for rec in src:
            if rec["ts"] > now:
                continue
            res = classify(rec["command"], rec["cwd"], policy, git, script_cache)
            stats[f"calls_{rec['source']}"] += 1
            calls.append((rec, res))

    total = len(calls)
    by_status = collections.Counter(res["status"] for _, res in calls)
    distinct = len({rec["command"] for rec, _ in calls})
    fam = collections.defaultdict(lambda: {"count": 0, "ok": 0, "breaks": collections.Counter(), "unknown": collections.Counter(),
                                           "examples": []})
    fam_calls_breaking = collections.Counter()
    unknown_causes = collections.Counter()
    break_kinds = collections.Counter()
    sugg_unblock = collections.Counter()
    sugg_touch = collections.Counter()
    for rec, res in calls:
        seen_f = set()
        for family, reasons in res["simples"]:
            d = fam[family]
            d["count"] += 1
            if not reasons:
                d["ok"] += 1
            for r in reasons:
                if r.startswith("breaks:"):
                    d["breaks"][reason_kind(r)] += 1
                else:
                    d["unknown"][r] += 1
            if reasons and any(r.startswith("breaks:") for r in reasons) and family not in seen_f:
                fam_calls_breaking[family] += 1
                seen_f.add(family)
            if reasons and len(d["examples"]) < 3:
                word = family.split(" ")[0]
                ex = redact(rec["command"], focus=word if word.isidentifier() or "-" in word else None)
                if ex not in d["examples"]:
                    d["examples"].append(ex)
        if res["status"] == "unknown":
            for r in res["reasons"]:
                if r.startswith("unknown:"):
                    unknown_causes[r] += 1
        if res["status"] == "breaks":
            br = [r for r in res["reasons"] if r.startswith("breaks:")]
            for k in {reason_kind(r) for r in br}:
                break_kinds[k] += 1
            keys = {suggestion_key(r) for r in br}
            for k in keys:
                if k:
                    sugg_touch[k] += 1
            if len(keys) == 1 and None not in keys:
                sugg_unblock[keys.pop()] += 1

    pct = lambda n: (100.0 * n / total) if total else 0.0
    breaks_n = by_status["breaks"]
    unk_n = by_status["unknown"]
    headline = {
        "calls": total, "distinct_commands": distinct,
        "calls_claude": stats["calls_claude"], "calls_codex": stats["calls_codex"],
        "codex_unparsed": stats["codex_unparsed"],
        "ok": by_status["ok"], "breaks": breaks_n, "unknown": unk_n,
        "pct_ok": round(pct(by_status["ok"]), 2), "pct_breaks": round(pct(breaks_n), 2), "pct_unknown": round(pct(unk_n), 2),
        "escape_hatch_per_100": round(pct(breaks_n), 2),
        "escape_hatch_per_100_upper": round(pct(breaks_n + unk_n), 2),
        "days": a.days, "cutoff": cutoff.isoformat(), "now": now.isoformat(),
    }

    fam_rows = sorted(fam.items(), key=lambda kv: -kv[1]["count"])
    agg = {
        "headline": headline,
        "break_kinds_by_call": dict(break_kinds.most_common()),
        "unknown_causes_by_call": dict(unknown_causes.most_common()),
        "families": {k: {"count": v["count"], "ok": v["ok"], "breaks": dict(v["breaks"]), "unknown": dict(v["unknown"]),
                         "calls_breaking": fam_calls_breaking[k], "examples": v["examples"]} for k, v in fam_rows},
        "suggestions_unblock": [{"kind": k[0], "value": redact(k[1]), "calls_unblocked": n, "calls_touched": sugg_touch[k]}
                                for k, n in sugg_unblock.most_common()],
        "suggestions_touch": [{"kind": k[0], "value": redact(k[1]), "calls_touched": n, "calls_unblocked": sugg_unblock[k]}
                              for k, n in sugg_touch.most_common()],
    }
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w") as f:
            json.dump(agg, f, indent=1)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            f.write(render_md(agg, raw))
    if a.sample and a.sample_out:
        rnd = random.Random(a.seed)
        picks = rnd.sample(calls, min(a.sample, total))
        with open(a.sample_out, "w") as f:
            for rec, res in picks:
                f.write(json.dumps({"cwd": rec["cwd"], "command": rec["command"], "status": res["status"],
                                    "reasons": res["reasons"], "families": [s[0] for s in res["simples"]]}) + "\n")
    print(json.dumps(headline))
    return 0


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("`", "'")


def render_md(agg: dict, raw_policy: dict) -> str:
    h = agg["headline"]
    L = []
    L.append("# Sandbox back-check report\n")
    L.append(f"Candidate policy: `policy/sandbox-candidate.json`. Window: last {h['days']} days "
             f"(since {h['cutoff'][:10]}). Generated by `scripts/sandbox-backcheck.py`.\n")
    L.append("Static, heuristic classification of historical Bash calls; nothing was executed. "
             "Examples are redacted and truncated.\n")
    L.append("## Headline\n")
    L.append("| metric | value |\n|---|---|")
    L.append(f"| calls | {h['calls']} (Claude {h['calls_claude']}, Codex {h['calls_codex']}) |")
    L.append(f"| distinct commands | {h['distinct_commands']} |")
    L.append(f"| ok | {h['ok']} ({h['pct_ok']}%) |")
    L.append(f"| breaks | {h['breaks']} ({h['pct_breaks']}%) |")
    L.append(f"| unknown | {h['unknown']} ({h['pct_unknown']}%) |")
    L.append(f"| Codex records skipped (unparseable) | {h['codex_unparsed']} |")
    L.append("")
    L.append("### Escape-hatch estimate\n")
    L.append(f"With `allowUnsandboxedCommands: true`, every breaking call would need a retry outside the sandbox: "
             f"**{h['escape_hatch_per_100']} per 100 calls** (upper bound counting unknowns as breaks: "
             f"{h['escape_hatch_per_100_upper']}).\n")
    L.append("## Break reasons (calls)\n")
    L.append("| reason | calls |\n|---|---|")
    for k, n in agg["break_kinds_by_call"].items():
        L.append(f"| {k} | {n} |")
    L.append("")
    L.append("## Top 20 breaking families\n")
    L.append("| family | calls breaking | reasons (simple commands) | examples |\n|---|---|---|---|")
    top = sorted(agg["families"].items(), key=lambda kv: -kv[1]["calls_breaking"])[:20]
    for k, v in top:
        if not v["calls_breaking"]:
            continue
        rs = ", ".join(f"{r} {n}" for r, n in sorted(v["breaks"].items(), key=lambda x: -x[1]))
        ex = "<br>".join(f"`{_md_cell(e)}`" for e in v["examples"])
        L.append(f"| {_md_cell(k)} | {v['calls_breaking']} | {rs} | {ex} |")
    L.append("")
    L.append("## Top unknown causes (calls classified unknown)\n")
    L.append("| cause | calls |\n|---|---|")
    for k, n in list(agg["unknown_causes_by_call"].items())[:20]:
        L.append(f"| {_md_cell(k)} | {n} |")
    L.append("")
    L.append("## Suggested policy additions\n")
    L.append("Ranked by calls fully unblocked (the addition is the only thing that call breaks on). "
             "`touched` counts calls where it is one of several breaks.\n")
    L.append("| kind | value | calls unblocked | calls touched |\n|---|---|---|---|")
    rows = sorted(agg["suggestions_touch"], key=lambda r: (-r["calls_unblocked"], -r["calls_touched"]))[:25]
    for r in rows:
        v = r["value"]
        for rx, rep_ in _SECRET_RES:
            v = rx.sub(rep_, v)
        L.append(f"| {r['kind']} | `{_md_cell(v)}` | {r['calls_unblocked']} | {r['calls_touched']} |")
    L.append("")
    L.append("## Per-family table\n")
    L.append("Counts are simple commands (a call with three commands counts three times).\n")
    L.append("| family | count | ok | breaks by reason | unknown |\n|---|---|---|---|---|")
    for k, v in agg["families"].items():
        if v["count"] < 5:
            continue
        rs = ", ".join(f"{r} {n}" for r, n in sorted(v["breaks"].items(), key=lambda x: -x[1])) or "-"
        us = sum(v["unknown"].values())
        L.append(f"| {_md_cell(k)} | {v['count']} | {v['ok']} | {rs} | {us or '-'} |")
    L.append("")
    L.append("Families with fewer than 5 simple commands are omitted here; the JSON aggregate has all of them.\n")
    L.append("## Known facts applied (`_backcheck`)\n")
    for k, v in raw_policy.get("_backcheck", {}).items():
        if k.endswith("note"):
            L.append(f"- {v}")
    L.append("")
    L.append("## Limitations\n")
    L.append("- Scripts (`bash x.sh`, `python3 x.py`, PATH scripts under `$HOME`) are judged by scanning their "
             "*current* text for markers (gh, `--user` D-Bus tools, nix, sudo, git network verbs, URLs). A marker on a "
             "branch the call never took still counts; scripts that no longer exist are `unknown`.")
    L.append("- Inline interpreter code (`python3 -c`, `python3 - <<EOF`) is scanned for URLs, home paths and the same "
             "markers only; file writes made from inside Python/Node are not seen.")
    L.append("- The sandbox write root is the session's recorded `cwd`. Linked worktrees keep their git objects in "
             "the primary checkout's `.git`, so commits and fetches from a worktree count as writes outside it.")
    L.append("- https git auth (`git-auth`) and `~/.npm` effects are derived from config, not measured in the sandbox.")
    L.append("- Removed worktrees are mapped back to their primary checkout to resolve remotes; when that fails the "
             "call is `unknown:git-remote-unresolvable`.")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
