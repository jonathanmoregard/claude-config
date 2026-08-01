#!/usr/bin/env python3
"""Central intake gate for the proposals spine.

Every unattended agent on this machine (RSI daily reviewer, permission-ledger
evaluator, router, clv2, ...) drops proposal files into subdirs of one spine
(default ~/.claude/proposals). Empirically (2026-08-01), proposers score their
own homework badly: the 03:00 RSI run produced two proposals that duplicated
code ALREADY SHIPPED — a model fallback merged in research-agent PR #19 and a
scanner auto-updater that had existed for weeks. Root cause: no grep-before-
verdict and no scorer separate from the proposer.

This gate is that separate scorer's harness (Distyl separate-scorer
discipline). Trusted-script division of labor:

  * this script (trusted, stdlib-only) does ALL filesystem writes;
  * one headless `claude` scorer per batch gets READ-ONLY tools
    (Read Grep Glob) and must return grep-cited verdicts as strict JSON.

The gate only ANNOTATES: it writes a `gate:` block into each proposal's
frontmatter (verdict + evidence + model + date) and appends to a JSONL log.
It never rejects, never deletes, never flips `status:` — the human decides in
/review-improvements, now with independent evidence in front of them.

Config lives in <spine>/gate-config.json (path overridable via
PROPOSAL_GATE_CONFIG or --config); every path and behavior has a default but
nothing is compiled in — see DEFAULTS.

Exit codes:
  0  gated everything eligible (or nothing was eligible)
  2  every scorer dispatch failed (primary and fallback) — nothing gated
  3  scorer output unparseable or invalid and nothing gated
  4  ungated content present: a failed or partially-answered chunk left
     proposals for the next run (annotated chunks stay annotated), and/or
     the spine holds push-blocking files the gate cannot cover (bad-named
     .md, stray non-.md, nonconforming subdir) — push must not proceed
"""
import argparse
import datetime
import fcntl
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

DEFAULT_CONFIG_PATH = "~/.claude/proposals/gate-config.json"
# Overall wall cap for pr-context collection: one hung `gh` (or many repos)
# must not starve the actual gating run.
PR_CONTEXT_WALL_CAP = 120
# Whole-run lock. Lives under ~/.claude/logs, NOT in the spine:
# push-proposals.sh commits the proposals/ subtree wholesale, so a lock file
# inside it would land in the pushed git history on every run.
GATE_LOCK_PATH = "~/.claude/logs/gate.lock"
# How long --run-callback waits for the lock before giving up (the gating run
# itself never waits: contended means another gate is active, exit 0).
CALLBACK_LOCK_TIMEOUT_SECONDS = 10.0
# Hard cap on scorer stdout. Enforced by fstat on the stdout tempfile BEFORE
# any read — capture_output used to buffer the whole pipe in this process
# first, making the cap decorative (R8).
SCORER_STDOUT_CAP = 5 * 1024 * 1024

DEFAULTS = {
    # Root of the proposals spine; every proposal subdir lives directly under it.
    "spine_root": "~/.claude/proposals",
    # Explicit subdir names to walk. Empty list = auto-discover every subdir
    # under spine_root (symlinked subdirs like rsi -> legacy path are followed).
    "subdirs": [],
    "excluded_subdirs": [".*", "archived"],
    # Must stay identical to references/gate-config.default.json (tested):
    # omitted config keys are documented to fall back to the same values.
    "excluded_files": ["README*", ".*", "on-decision", "on-decision.*"],
    "scorer": {
        "model": "claude-fable-5",       # strongest tier first ...
        "fallback_model": "opus",        # ... opus when fable is capped/absent
        "allowed_tools": "Read Grep Glob",  # read-only: the scorer never writes
        "timeout_seconds": 600,
        # Max proposals per scorer dispatch: a 50-file backlog must never land
        # on the model as one prompt (budget blowout + all-or-nothing parse).
        "batch_size": 10,
    },
    # Where the scorer should grep for prior art, beyond the repos referenced
    # by the proposals themselves.
    "search_paths": ["~/.claude"],
    "pr_context": {
        # A proposal that mentions ~/Repos/<name> pulls that repo's recently
        # merged PR titles into the scorer's context file.
        "repo_roots": ["~/Repos"],
        # name -> path additions/overrides for repos living elsewhere.
        "extra_repos": {},
        "merged_limit": 30,
        "timeout_seconds": 30,
    },
    # Read by the review-improvements skill, not by this script: after a
    # decision on a proposal in <subdir>, run `<callback> <path> <decision>`.
    "on_decision": {},
    "gate_log": "~/.claude/proposals/gate-log.jsonl",
}

VALID_VERDICTS = ("sharp", "duplicate", "rot")
VALID_DECISIONS = ("implemented", "rejected", "deferred")
EVIDENCE_MAX = 200
REPO_REF_RE = re.compile(r"(?:~|/home/[A-Za-z0-9._-]+)/Repos/([A-Za-z0-9._-]+)")
# Candidate ids are embedded verbatim in the scorer's instruction text, so
# both path components are allowlisted: no whitespace, no newlines, nothing
# that could read as instructions to the scorer.
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.md$")
SUBDIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
# Credential-shaped runs (base64/hex/token-ish) never belong in evidence,
# frontmatter, or logs.
TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
# Non-whitespace control characters (whitespace is collapsed separately).
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Words that tend to prefix or carry credentials in tool stderr.
SECRETY_WORD_RE = re.compile(r"(?i)(?<!\S)(?:key|token|secret|bearer)\S*")


def sanitize_evidence(text):
    """Scorer evidence is model output: collapse whitespace, strip control
    characters, redact credential-shaped token runs, cap the length."""
    text = re.sub(r"\s+", " ", text)
    text = CONTROL_RE.sub("", text)
    text = TOKEN_RUN_RE.sub("[redacted]", text)
    return text.strip()[:EVIDENCE_MAX]


def redact_stderr(text):
    """Scorer stderr can echo environment/auth details — drop secret-ish
    words and token runs before any of it reaches our own log output."""
    text = TOKEN_RUN_RE.sub("[redacted]", text)
    text = SECRETY_WORD_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def log(msg):
    print(msg, flush=True)


def read_text_strict(path):
    """Read raw bytes and decode strict UTF-8 — newline='' semantics: no
    universal-newline translation (a CRLF proposal keeps its \\r\\n bytes) and
    no errors='replace' mangling. Raises UnicodeDecodeError on non-UTF8
    content; callers classify such files as nonconforming and NEVER rewrite
    them (round 7: text-mode reads silently rewrote bytes on annotate)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return data.decode("utf-8")


def split_lines_keepends(text):
    """Split on \\n ONLY, keeping the terminator on each line. Unlike
    str.splitlines this never splits on \\r, \\x0b, \\u2028 etc., so a CRLF
    file's lines carry their \\r and re-joining round-trips byte-identically."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def contained(path, root):
    """True when `path` realpath-resolves to `root` or below it."""
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def acquire_gate_lock(timeout=0.0):
    """Open GATE_LOCK_PATH and take an exclusive flock, retrying non-blocking
    attempts until `timeout` seconds have passed. Returns the open file object
    (keep it referenced until process exit) or None when the lock stayed
    contended. OSError from open/makedirs propagates to the caller."""
    lock_path = os.path.expanduser(GATE_LOCK_PATH)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    # O_CREAT|O_RDWR, not open(path, "w"): never truncate a lock file another
    # process may be holding, and create with an explicit 0600 mode.
    lock_fh = os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "r+")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_fh
        except OSError:
            if time.monotonic() >= deadline:
                lock_fh.close()
                return None
            time.sleep(0.2)


def require_under_home(path, label):
    home = os.path.expanduser("~")
    if not contained(path, home):
        raise ValueError("%s %r resolves outside %s — refusing config"
                         % (label, path, home))


def load_config(path):
    """Merge the user's config file over DEFAULTS (one level of nesting deep:
    dict values merge one level; list values replace wholesale).

    A missing config file is fine — defaults apply. A present-but-broken one
    is not: better to stop than to gate against the wrong spine. Config paths
    that write or get grepped (spine_root, gate_log, search_paths, and the
    pr_context repo paths — they set gh's cwd and join the scorer's grep
    roots) must realpath-resolve under $HOME — this script runs unattended
    and its config file is only as trusted as whatever last wrote it.
    """
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update({k: v for k, v in value.items() if v is not None})
            elif value is not None:
                cfg[key] = value
    cfg["spine_root"] = os.path.expanduser(cfg["spine_root"])
    cfg["gate_log"] = os.path.expanduser(cfg["gate_log"])
    cfg["search_paths"] = [os.path.expanduser(p) for p in cfg["search_paths"]]
    pr = cfg["pr_context"]
    pr["repo_roots"] = [os.path.expanduser(p) for p in pr["repo_roots"]]
    pr["extra_repos"] = {k: os.path.expanduser(v) for k, v in pr["extra_repos"].items()}
    require_under_home(cfg["spine_root"], "spine_root")
    require_under_home(cfg["gate_log"], "gate_log")
    for p in cfg["search_paths"]:
        require_under_home(p, "search_paths entry")
    for p in pr["repo_roots"]:
        require_under_home(p, "pr_context.repo_roots entry")
    for name, p in pr["extra_repos"].items():
        require_under_home(p, "pr_context.extra_repos[%r]" % name)
    bs = cfg["scorer"].get("batch_size")
    if isinstance(bs, bool) or not isinstance(bs, int) or bs < 1:
        raise ValueError("scorer.batch_size must be a positive integer, got %r"
                         % (bs,))
    return cfg


def split_frontmatter(text):
    """Return (lines, closing_index) if `text` opens with a --- frontmatter
    block, else None. `lines` keeps line endings; lines[closing_index] is the
    closing --- line."""
    if not text.startswith("---"):
        return None
    lines = split_lines_keepends(text)
    if lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines, i
    return None


# The ONLY status values that settle a file (a human or producer already
# decided). `open` counts as settled here because the pre-round-6 gate never
# scored it (it was "anything but pending") and live spines carry it;
# `skipped` is written by review-improvements decision records. Everything
# else fails CLOSED: quoted pending, unknown strings (`wip`), parse oddities
# are all pending-equivalent and get gated — empirically, `status: "pending"`
# used to slip past an exact-match test and ship ungated.
SETTLED_STATUSES = frozenset((
    "rejected", "implemented", "deferred", "info", "open", "archived",
    "skipped"))


def normalize_status_value(value):
    """Normalize a raw frontmatter status value: strip a trailing comment
    (whitespace + '#'), surrounding whitespace, and one layer of matching
    quotes. Anything this cannot reduce to a bare word stays as-is and will
    fail the settled match (fail closed)."""
    value = re.sub(r"\s#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def has_settled_status(fm):
    """True ONLY when the frontmatter carries a status field whose value,
    after normalize_status_value(), exactly matches SETTLED_STATUSES — the
    file is then out of gate scope. Any other value — `pending`, quoted
    `"pending"`, `pending  # note`, unknown strings — and a missing status
    field are pending-equivalent: producers with their own frontmatter
    shapes (e.g. the permissions evaluator) still get gated."""
    m = re.search(r"(?m)^status:(.*)$", fm)
    if not m:
        return False
    return normalize_status_value(m.group(1)) in SETTLED_STATUSES


def candidate_state(text):
    """Gate-eligibility of a proposal's text:
      "no-frontmatter"  no --- frontmatter block at all: eligible; annotate()
                        prepends a fresh frontmatter carrying the gate block
      "pending"         frontmatter with a status that does not normalize
                        into SETTLED_STATUSES, or with no status field
                        (pending-equivalent, fail closed): eligible
      "settled"         status normalizes into SETTLED_STATUSES: excluded
      "gated"           frontmatter already carries a gate: block
    """
    parsed = split_frontmatter(text)
    if parsed is None:
        return "no-frontmatter"
    lines, closing = parsed
    fm = "".join(lines[1:closing])
    if re.search(r"(?m)^gate:", fm):
        return "gated"
    if has_settled_status(fm):
        return "settled"
    return "pending"


def find_gate_block(lines, closing):
    """(start, end) line indices of a gate: block inside the frontmatter
    (end exclusive: the block is `gate:` plus its indented children), or
    None when the frontmatter has no gate key."""
    for i in range(1, closing):
        if re.match(r"^gate:", lines[i]):
            end = i + 1
            while end < closing and re.match(r"^[ \t]+\S", lines[end]):
                end += 1
            return i, end
    return None


def parse_gate_block(lines, start, end):
    """(verdict, date) as written in the block — either may be None."""
    verdict = date = None
    for line in lines[start + 1:end]:
        m = re.match(r"^[ \t]+verdict:\s*(\S+)\s*$", line)
        if m:
            verdict = m.group(1)
        m = re.match(r"^[ \t]+date:\s*(\S+)\s*$", line)
        if m:
            date = m.group(1)
    return verdict, date


def load_gate_log_index(cfg):
    """{(file_id, date, verdict)} for every line this script has written to
    the gate log. A gate: block in a proposal only counts as real if it
    reconciles against one of these — producers write frontmatter wholesale,
    so an unreconciled block is spurious and gets re-scored. File ids are
    always subdir-qualified; a bare-basename entry never reconciles (it
    would create a cross-subdir acceptance edge)."""
    index = set()
    try:
        with open(cfg["gate_log"], encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                date = entry.get("date") or str(entry.get("ts", ""))[:10]
                index.add((entry.get("file"), date, entry.get("verdict")))
    except OSError:
        pass
    return index


def atomic_write(path, text):
    """Atomic same-directory replace, preserving the file's mode.
    newline='' end-to-end: `text` was read without newline translation, so
    it must be written back without any either."""
    mode = os.stat(path).st_mode
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".gate-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def discover_subdirs(cfg):
    root = cfg["spine_root"]
    if cfg["subdirs"]:
        names = list(cfg["subdirs"])
    else:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return []
    out = []
    for name in names:
        if any(fnmatch.fnmatch(name, pat) for pat in cfg["excluded_subdirs"]):
            continue
        if not SUBDIR_RE.match(name):
            # Subdir names become half of every candidate id embedded in the
            # scorer instruction text — same allowlist as basenames.
            log("warning: skipping subdir with nonconforming name under %s" % root)
            continue
        full = os.path.join(root, name)
        if not os.path.isdir(full):  # follows symlinks (rsi is one in production)
            continue
        # Symlinked subdirs are legitimate (rsi resolves inside ~/.claude)
        # but must stay logically rooted: never follow one out of $HOME.
        if not contained(full, os.path.expanduser("~")):
            log("warning: skipping subdir %s — resolves outside $HOME" % name)
            continue
        out.append((name, full))
    return out


def collect_candidates(cfg, repair=True):
    """([{id, path, subdir, basename, content}], [exclusion notes]) for every
    ungated proposal. Ids are always subdir-qualified (`<subdir>/<basename>`)
    — one scheme everywhere, including the gate log.

    Coverage matches what push-proposals.sh ships: EVERY allowlisted .md in a
    discovered subdir is a candidate unless it opts out. Files with no
    frontmatter at all, or frontmatter with no status field (producers with
    their own formats, e.g. the permissions evaluator), are pending-equivalent
    and get gated; only a status normalizing into SETTLED_STATUSES or a
    reconciled gate block excludes a file.

    A file carrying a gate: block is only skipped when that block reconciles
    against the gate log (qualified id + date + verdict). An unreconciled
    block is spurious (producers write frontmatter wholesale): it is stripped
    from the file (when `repair` is true) and the proposal is re-scored this
    run.
    """
    log_index = load_gate_log_index(cfg)
    candidates, excluded = [], []
    for subdir, dirpath in discover_subdirs(cfg):
        try:
            entries = sorted(os.listdir(dirpath))
        except OSError:
            continue
        for fname in entries:
            if not fname.endswith(".md"):
                continue
            if any(fnmatch.fnmatch(fname, pat) for pat in cfg["excluded_files"]):
                continue
            if not FILENAME_RE.match(fname):
                excluded.append({
                    "event": "excluded-nonconforming-filename",
                    "subdir": subdir,
                    "file": fname[:200],
                })
                continue
            fpath = os.path.join(dirpath, fname)
            if not os.path.isfile(fpath):
                continue
            if not contained(fpath, dirpath):
                # R8: annotate() and the strip-repair below rewrite the
                # candidate's REALPATH, so a per-file symlink escaping the
                # subdir (rsi/x.md -> ~/.zshrc) would let the gate prepend
                # frontmatter to an arbitrary file. Never read it;
                # collect_push_blockers lists it (exit 4). Containment is
                # against the subdir with ITS OWN symlink followed, so files
                # inside the production rsi -> legacy subdir stay candidates.
                continue
            try:
                content = read_text_strict(fpath)
            except UnicodeDecodeError:
                # Non-UTF8: never a candidate, never rewritten —
                # collect_push_blockers reports it and forces exit 4.
                continue
            except OSError:
                continue
            qid = subdir + "/" + fname
            parsed = split_frontmatter(content)
            if parsed is not None:
                lines, closing = parsed
                fm = "".join(lines[1:closing])
                if has_settled_status(fm):
                    continue  # human/producer already decided — out of scope
                block = find_gate_block(lines, closing)
                if block is not None:
                    verdict, bdate = parse_gate_block(lines, block[0], block[1])
                    if (qid, bdate, verdict) in log_index:
                        continue  # genuinely gated by a prior run of this script
                    log("warning: %s carries a gate block with no matching "
                        "gate-log line — stripping it and re-scoring" % qid)
                    content = "".join(lines[:block[0]] + lines[block[1]:])
                    if repair:
                        # Same realpath semantics as annotate(): a symlinked
                        # proposal stays a symlink; its target gets rewritten.
                        atomic_write(os.path.realpath(fpath), content)
            # parsed is None → no frontmatter: still a candidate (annotate()
            # creates the frontmatter) — push-proposals.sh would ship it.
            candidates.append({
                "id": qid,
                "path": os.path.abspath(fpath),
                "subdir": subdir,
                "basename": fname,
                "content": content,
            })
    return candidates, excluded


def blocker_display(root, path):
    """Path as printed in a push-blocker line: relative to the spine root,
    control characters (an attacker-shaped filename may embed newlines)
    replaced, length capped."""
    rel = os.path.relpath(path, root)
    return CONTROL_RE.sub("?", rel)[:200]


def collect_push_blockers(cfg):
    """[{path, display, reason}] for every file push-proposals.sh would stage
    from the spine that the gate cannot cover — each one is a fail-open path
    (it would ship ungated), so its presence must block the push (exit 4).
    Covers bad-named .md, stray non-.md, nonconforming subdirs, and (round 7)
    .md files that do not strict-decode as UTF-8: the gate must never rewrite
    such bytes, so it can never annotate them either.

    Non-blocking exclusions: files matching excluded_files (dotfiles,
    README*), subdirs matching excluded_subdirs (archived/, dot-dirs),
    spine-root gate-config.json and gate-log.jsonl (versioned config /
    push-excluded forensics), and symlinked subdirs escaping $HOME (git
    stages the symlink object, not the content behind it)."""
    root = cfg["spine_root"]
    home = os.path.expanduser("~")
    covered = {path for _, path in discover_subdirs(cfg)}
    root_special = ("gate-config.json", "gate-log.jsonl")
    blockers = []

    def note(path, reason):
        blockers.append({"path": path,
                         "display": blocker_display(root, path),
                         "reason": reason})

    def note_tree(top, reason):
        for base, _dirs, files in os.walk(top):
            for f in sorted(files):
                note(os.path.join(base, f), reason)

    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return blockers
    for name in entries:
        full = os.path.join(root, name)
        if os.path.isdir(full):  # follows symlinks, like discover_subdirs
            if any(fnmatch.fnmatch(name, pat) for pat in cfg["excluded_subdirs"]):
                continue
            if not SUBDIR_RE.match(name):
                note_tree(full, "inside a subdir with a nonconforming name")
                continue
            if not contained(full, home):
                continue  # skipped by discovery; git stages only the symlink
            if full not in covered:
                # e.g. an explicit `subdirs` config narrower than the spine.
                note_tree(full, "subdir not covered by the gate scan")
                continue
            for fname in sorted(os.listdir(full)):
                fpath = os.path.join(full, fname)
                if any(fnmatch.fnmatch(fname, pat) for pat in cfg["excluded_files"]):
                    continue
                if not contained(fpath, full):
                    # R8: a per-file symlink whose realpath escapes the
                    # subdir's own realpath is never read or rewritten —
                    # candidacy skips it, so it must block the push.
                    note(fpath, "symlink escapes its subdir — the gate "
                                "never follows it")
                elif os.path.isdir(fpath):
                    note_tree(fpath, "nested directory — the gate never scans it")
                elif not fname.endswith(".md"):
                    note(fpath, "non-.md file — push would ship it ungated")
                elif not FILENAME_RE.match(fname):
                    note(fpath, "nonconforming .md filename")
                else:
                    try:
                        read_text_strict(fpath)
                    except UnicodeDecodeError:
                        note(fpath, "not valid UTF-8 — the gate cannot "
                                    "annotate it")
                    except OSError:
                        pass
        else:
            if name in root_special:
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in cfg["excluded_files"]):
                continue
            note(full, "file at the spine root — the gate never scans it")
    return blockers


def report_push_blockers(blockers):
    log("error: %d file(s) would be staged by push-proposals.sh but cannot "
        "be gated:" % len(blockers))
    for b in blockers:
        log("  - %s: %s" % (b["display"], b["reason"]))
    log("rename them to conform (or move them under archived/) and rerun the gate")


def is_git_checkout(path):
    """gh runs with cwd inside these dirs — only trust real checkouts
    (a dir containing .git; .git may be a file in worktrees)."""
    return os.path.isdir(path) and os.path.exists(os.path.join(path, ".git"))


def infer_repos(candidates, cfg):
    """name -> checkout path for every ~/Repos/<name> reference that resolves
    to a real git checkout, plus configured extra_repos (same requirement)."""
    pr = cfg["pr_context"]
    names = set()
    for c in candidates:
        names.update(REPO_REF_RE.findall(c["content"]))
    repos = {}
    for name in sorted(names):
        for root in pr["repo_roots"]:
            path = os.path.join(root, name)
            if is_git_checkout(path):
                repos[name] = path
                break
    for name, path in pr["extra_repos"].items():
        if is_git_checkout(path):
            repos[name] = path
        else:
            log("warning: extra_repos[%r] is not a git checkout — skipped" % name)
    return repos


def collect_pr_context(repos, cfg):
    """Write recently merged PR titles for each referenced repo to a temp file
    the scorer can Read. gh being down must never block gating — each failure
    becomes a 'pr-context unavailable' note instead."""
    pr = cfg["pr_context"]
    start = time.monotonic()
    fd, path = tempfile.mkstemp(prefix="proposal-gate-pr-context-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Recently merged PRs for repos referenced by this batch\n\n")
        if not repos:
            fh.write("No repo checkouts referenced by this batch; nothing collected.\n")
        for name, repo_path in sorted(repos.items()):
            fh.write("## %s (%s)\n\n" % (name, repo_path))
            remaining = PR_CONTEXT_WALL_CAP - (time.monotonic() - start)
            if remaining <= 0:
                fh.write("pr-context unavailable: %ds wall cap reached\n\n"
                         % PR_CONTEXT_WALL_CAP)
                continue
            try:
                proc = subprocess.run(
                    ["gh", "pr", "list", "--state", "merged",
                     "--limit", str(pr["merged_limit"]), "--json", "title,mergedAt"],
                    cwd=repo_path, capture_output=True, text=True,
                    timeout=min(pr["timeout_seconds"], remaining),
                )
                if proc.returncode == 0:
                    fh.write(proc.stdout.strip() + "\n\n")
                else:
                    fh.write("pr-context unavailable: gh exited %d (%s)\n\n"
                             % (proc.returncode, proc.stderr.strip()[:200]))
            except (OSError, subprocess.TimeoutExpired) as exc:
                fh.write("pr-context unavailable: %s\n\n" % exc)
    return path


def build_prompt(candidates, repos, context_path, cfg):
    search_paths = list(cfg["search_paths"]) + sorted(repos.values())
    listing = "\n".join("- id: %s\n  path: %s" % (c["id"], c["path"]) for c in candidates)
    return """You are the independent intake scorer for a batch of improvement proposals.
You are deliberately SEPARATE from the agents that wrote them; your job is
verification, not sympathy. Every proposal below was written by an unattended
agent and may describe work that is already done, or aimed at code that no
longer exists.

Proposals to score (Read each file):
%s

Deterministic context: recently merged PR titles for the repos these proposals
reference were pre-collected into %s — Read it first.

MANDATORY EVIDENCE RULE — for EACH proposal, before any verdict:
1. Read the proposal and extract its concrete fix keywords: function names,
   file paths, script names, flags, config keys.
2. Grep for those keywords under each of these roots:
%s
   (skills, hooks, settings, CLAUDE.md under the dot-claude root; source code
   under the repo checkouts).
3. Cross-check the merged PR titles in the context file.
A verdict without a grep/PR citation is invalid.

Verdict meanings:
- "duplicate": the proposed fix already exists — cite path:line or the merged
  PR title that shipped it.
- "rot": the proposal's premise no longer holds (the code/config it targets is
  gone or has materially changed) — cite what you found instead.
- "sharp": still valid and unimplemented — cite where you looked and found
  nothing.

Output STRICT JSON on stdout and nothing else — no prose, no code fences:
{"verdicts":[{"file":"<id>","verdict":"sharp|duplicate|rot","evidence":"<=200 chars citing path:line or PR title"}]}
Exactly one entry per proposal, using the ids exactly as listed above.
""" % (listing, context_path,
       "\n".join("   - " + p for p in search_paths))


def unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def dispatch_scorer(prompt, cfg):
    """One headless scorer call for the whole batch; fall back once when the
    primary model is capped or unavailable. The scorer's stdout streams into
    a 0600 tempfile (R8: capture_output buffered an unbounded pipe in this
    process before the 5MB cap could look at it). Returns
    (stdout_tempfile_path, model_used) or (None, None); the caller owns the
    tempfile. stderr stays captured — it is only ever truncated + redacted."""
    scorer = cfg["scorer"]
    models = [scorer["model"]]
    if scorer.get("fallback_model") and scorer["fallback_model"] != scorer["model"]:
        models.append(scorer["fallback_model"])
    last_err = None
    for model in models:
        cmd = ["claude", "--model", model, "--print",
               "--allowedTools", scorer["allowed_tools"], "-p", prompt]
        fd, out_path = tempfile.mkstemp(prefix="gate-scorer-stdout-",
                                        suffix=".txt")  # mkstemp: mode 0600
        try:
            with os.fdopen(fd, "wb") as out_fh:
                proc = subprocess.run(cmd, stdout=out_fh,
                                      stderr=subprocess.PIPE, text=True,
                                      timeout=scorer["timeout_seconds"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            unlink_quiet(out_path)
            last_err = "dispatch failed for %s: %s" % (model, exc)
            log(last_err)
            continue
        if proc.returncode == 0:
            return out_path, model
        snippet = (proc.stderr or "").strip()
        if not snippet:  # bounded read: first 300 chars of the stdout file
            with open(out_path, encoding="utf-8", errors="replace") as fh:
                snippet = fh.read(300).strip()
        unlink_quiet(out_path)
        last_err = "scorer exited %d for %s: %s" % (
            proc.returncode, model, redact_stderr(snippet[:300]))
        log(last_err)
    log("error: all scorer dispatches failed (%s)" % last_err)
    return None, None


def preserve_raw_scorer_output(src_path):
    """Move unusable scorer stdout (the dispatch tempfile) into the logs dir
    verbatim for diagnosis — 0600, never echoed into our own stdout (cron
    logs), and never read into memory (it may be the >5MB overflow case)."""
    logs_dir = os.path.expanduser("~/.claude/logs")
    os.makedirs(logs_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(logs_dir, "gate-scorer-raw-%s.txt" % ts)
    if os.path.exists(path):  # two failures within one second
        path = os.path.join(logs_dir, "gate-scorer-raw-%s-%d.txt" % (ts, os.getpid()))
    os.chmod(src_path, 0o600)  # mkstemp already 0600; keep it explicit
    try:
        os.replace(src_path, path)
    except OSError:
        # TMPDIR on another filesystem: chunked copy (copy2 keeps 0600),
        # then drop the source — still no full read into memory.
        shutil.move(src_path, path)
    os.chmod(path, 0o600)
    return path


def parse_verdicts(stdout, candidates):
    """Strict parse of the scorer's JSON — the contract is JSON on stdout and
    nothing else; no salvage of JSON embedded in prose. Returns
    id -> (verdict, evidence) or None when the output is unusable (caller
    exits nonzero, gates nothing)."""
    if len(stdout) > 5 * 1024 * 1024:
        log("error: scorer output exceeds 5MB — refusing to parse")
        return None
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
        log("error: scorer output is not the required JSON shape")
        return None
    known_ids = {c["id"] for c in candidates}
    verdicts = {}
    for entry in payload["verdicts"]:
        if not isinstance(entry, dict):
            log("error: verdict entry is not an object: %r" % (entry,))
            return None
        fid = entry.get("file")
        verdict = entry.get("verdict")
        evidence = entry.get("evidence", "")
        if fid not in known_ids:
            log("error: verdict for unknown file id %r" % (fid,))
            return None
        if verdict not in VALID_VERDICTS:
            log("error: invalid verdict %r for %s (want sharp|duplicate|rot)"
                % (verdict, fid))
            return None
        if not isinstance(evidence, str):
            log("error: evidence for %s is not a string" % fid)
            return None
        if fid in verdicts:
            # No silent last-write-wins: conflicting entries mean the scorer
            # did not follow the contract — reject the whole batch.
            log("error: duplicate verdict for file id %r — batch rejected" % fid)
            return None
        verdicts[fid] = (verdict, sanitize_evidence(evidence))
    return verdicts


def annotate(candidate, verdict, evidence, model, today):
    """Write the gate: block into the proposal — inserted just before the
    closing --- when frontmatter exists, or as a freshly created frontmatter
    block prepended to the file (body bytes untouched) when it does not.
    Atomic write; preserves file mode. Operates on the proposal's realpath so
    a symlinked proposal keeps being a symlink and its target gets replaced.

    Byte fidelity (round 7): the file is read strict-UTF-8 with newline=''
    semantics and written back the same way, so every byte outside the
    inserted block round-trips identically (CRLF files stay CRLF). The gate
    block itself ALWAYS uses \\n line endings — documented contract."""
    path = os.path.realpath(candidate["path"])
    try:
        text = read_text_strict(path)
    except UnicodeDecodeError:
        return False  # turned non-UTF8 underneath us — never rewrite it
    state = candidate_state(text)
    if state not in ("pending", "no-frontmatter"):
        # Changed underneath us since collection — leave it alone.
        return False
    block = (
        "gate:\n"
        "  verdict: %s\n"
        "  evidence: %s\n"
        "  model: %s\n"
        "  date: %s\n" % (verdict, json.dumps(evidence), model, today)
    )
    if state == "no-frontmatter":
        new_text = "---\n" + block + "---\n" + text
    else:
        lines, closing = split_frontmatter(text)
        new_text = "".join(lines[:closing]) + block + "".join(lines[closing:])
    atomic_write(path, new_text)
    return True


def append_gate_log(cfg, entry):
    path = cfg["gate_log"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def run_callback(cfg, subdir, proposal_path, decision):
    """Validate and run the on_decision callback configured for `subdir`.

    The config map is the single source of truth, but the executing agent
    never shells out to an arbitrary configured string: the callback must
    realpath-resolve under ~/.claude, be a regular file (not itself a
    symlink), be owned by the current uid, carry no group/other write bits,
    and be executable. The proposal path must realpath-resolve inside the
    spine subdir the callback was registered for (following the subdir
    symlink — production rsi resolves outside spine_root's realpath) and
    under $HOME. Executed as list argv, shell=False.
    """
    if decision not in VALID_DECISIONS:
        log("error: invalid decision %r (want %s)"
            % (decision, "|".join(VALID_DECISIONS)))
        return 1
    if not SUBDIR_RE.match(subdir):
        log("error: nonconforming subdir name %r — refusing callback" % subdir)
        return 1
    callback = cfg.get("on_decision", {}).get(subdir)
    if not callback:
        log("no on_decision callback configured for subdir %r — nothing to run"
            % subdir)
        return 0
    # Same whole-run lock as gating: callbacks may rewrite or move proposal
    # files, and racing a concurrent gate's check-then-replace would corrupt
    # them. Bounded blocking (unlike the gating run's single attempt) because
    # callbacks are human-invoked and a short wait beats a spurious failure.
    try:
        lock_fh = acquire_gate_lock(timeout=CALLBACK_LOCK_TIMEOUT_SECONDS)
    except OSError as exc:
        log("error: cannot open lock file %s: %s"
            % (os.path.expanduser(GATE_LOCK_PATH), exc))
        return 1
    if lock_fh is None:  # else: held until process exit
        log("error: could not acquire the gate lock at %s within %ds — "
            "another gate run is active; retry shortly"
            % (os.path.expanduser(GATE_LOCK_PATH), CALLBACK_LOCK_TIMEOUT_SECONDS))
        return 1
    callback = os.path.expanduser(str(callback))
    claude_root = os.path.expanduser("~/.claude")
    problems = []
    if os.path.islink(callback):
        problems.append("is a symlink")
    if not os.path.isfile(callback):
        problems.append("is not a regular file")
    if not contained(callback, claude_root):
        problems.append("resolves outside %s" % claude_root)
    if not problems:
        st = os.stat(callback)
        if st.st_uid != os.getuid():
            problems.append("not owned by the current user")
        if st.st_mode & 0o022:
            problems.append("group/other writable")
        if not os.access(callback, os.X_OK):
            problems.append("not executable")
    if problems:
        log("error: refusing on_decision callback %s for subdir %r: %s"
            % (callback, subdir, "; ".join(problems)))
        return 1
    proposal_path = os.path.realpath(proposal_path)
    # The callback only ever gets a proposal from the subdir it was
    # registered for. Containment is checked against the subdir dir with its
    # symlink followed (NOT against spine_root's realpath) precisely so the
    # production rsi symlink keeps working; the $HOME check mirrors
    # discover_subdirs' rule for symlinks escaping home.
    subdir_root = os.path.join(cfg["spine_root"], subdir)
    if not (contained(proposal_path, subdir_root)
            and contained(proposal_path, os.path.expanduser("~"))):
        log("error: proposal path %s does not resolve inside %s — refusing "
            "callback" % (proposal_path, subdir_root))
        return 1
    # No shell, no capture: the callback's stdout/stderr surface verbatim.
    proc = subprocess.run([callback, proposal_path, decision])
    return proc.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Annotate pending proposals with independent scorer verdicts.")
    parser.add_argument("--config",
                        default=os.environ.get("PROPOSAL_GATE_CONFIG",
                                               DEFAULT_CONFIG_PATH),
                        help="gate config JSON (default: %s)" % DEFAULT_CONFIG_PATH)
    parser.add_argument("--list", action="store_true",
                        help="print what would be gated, dispatch nothing")
    parser.add_argument("--run-callback", nargs=3,
                        metavar=("SUBDIR", "PROPOSAL_PATH", "DECISION"),
                        help="validate and run the on_decision callback "
                             "configured for SUBDIR (decision: %s)"
                             % "|".join(VALID_DECISIONS))
    args = parser.parse_args(argv)

    try:
        cfg = load_config(os.path.expanduser(args.config))
    except (OSError, ValueError) as exc:
        log("error: cannot load gate config %s: %s" % (args.config, exc))
        return 1

    if args.run_callback:
        return run_callback(cfg, *args.run_callback)

    if not args.list:
        # Single-instance discipline (R9): the gating run strips/annotates
        # frontmatter (check-then-replace) and appends to the gate log —
        # two concurrent runs would interleave both. --list never writes
        # (repair=False below), so it stays lock-free. The lock file lives
        # under ~/.claude/logs (see GATE_LOCK_PATH), never in the spine.
        try:
            lock_fh = acquire_gate_lock()  # held until process exit
        except OSError as exc:
            log("error: cannot open lock file %s: %s"
                % (os.path.expanduser(GATE_LOCK_PATH), exc))
            return 1
        if lock_fh is None:
            log("proposal-intake-gate: another instance holds the lock at %s "
                "— exiting without gating" % os.path.expanduser(GATE_LOCK_PATH))
            return 0

    candidates, excluded = collect_candidates(cfg, repair=not args.list)
    # Round-6 item 2: push-proposals.sh stages the spine subtree wholesale,
    # so content the gate cannot cover must BLOCK the push (exit 4), not
    # slip past it with a log line.
    blockers = collect_push_blockers(cfg)

    if args.list:
        for note in excluded:
            log("excluded (nonconforming filename) in subdir: %s" % note["subdir"])
        for b in blockers:
            log("would block push: %s (%s)" % (b["display"], b["reason"]))
        for c in candidates:
            log("would gate: %s" % c["path"])
        return 0

    now = datetime.datetime.now().isoformat(timespec="seconds")
    for note in excluded:
        log("warning: excluded nonconforming filename in subdir %s (noted in gate log)"
            % note["subdir"])
        append_gate_log(cfg, dict(note, ts=now))

    if not candidates:
        if blockers:
            report_push_blockers(blockers)
            return 4  # nothing to dispatch, but the push must not proceed
        log("proposal-intake-gate: nothing to gate (no ungated pending proposals under %s)"
            % cfg["spine_root"])
        return 0

    repos = infer_repos(candidates, cfg)
    context_path = collect_pr_context(repos, cfg)
    log("pr-context: %s" % context_path)

    # Chunked dispatch: at most scorer.batch_size proposals per scorer call,
    # sequentially. A failed chunk (dispatch or parse) leaves ONLY that chunk
    # ungated for the next run; chunks already annotated stay annotated.
    batch_size = cfg["scorer"]["batch_size"]
    chunks = [candidates[i:i + batch_size]
              for i in range(0, len(candidates), batch_size)]
    today = datetime.date.today().isoformat()
    gated = 0
    dispatch_failures = parse_failures = 0
    ungated_ids = []
    try:
        for idx, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                log("dispatching chunk %d/%d (%d proposal(s))"
                    % (idx, len(chunks), len(chunk)))
            prompt = build_prompt(chunk, repos, context_path, cfg)
            stdout_path, model = dispatch_scorer(prompt, cfg)
            if stdout_path is None:
                dispatch_failures += 1
                ungated_ids.extend(c["id"] for c in chunk)
                continue
            # R8: enforce the cap on the FILE before any read — an
            # oversized chunk fails without this process ever holding it.
            if os.stat(stdout_path).st_size > SCORER_STDOUT_CAP:
                parse_failures += 1
                raw_path = preserve_raw_scorer_output(stdout_path)
                log("error: scorer output exceeds 5MB — refusing to parse; "
                    "raw output preserved at %s (0600)" % raw_path)
                ungated_ids.extend(c["id"] for c in chunk)
                continue
            with open(stdout_path, encoding="utf-8", errors="replace") as fh:
                stdout = fh.read()
            verdicts = parse_verdicts(stdout, chunk)
            if verdicts is None:
                parse_failures += 1
                raw_path = preserve_raw_scorer_output(stdout_path)
                log("scorer output unusable for this chunk — raw output "
                    "preserved at %s (0600)" % raw_path)
                ungated_ids.extend(c["id"] for c in chunk)
                continue
            unlink_quiet(stdout_path)
            for c in chunk:
                if c["id"] not in verdicts:
                    ungated_ids.append(c["id"])
                    continue
                verdict, evidence = verdicts[c["id"]]
                if annotate(c, verdict, evidence, model, today):
                    gated += 1
                    append_gate_log(cfg, {
                        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                        "date": today,
                        "file": c["id"],
                        "path": c["path"],
                        "subdir": c["subdir"],
                        "verdict": verdict,
                        "evidence": evidence,
                        "model": model,
                    })
                    log("gated %s: %s" % (c["id"], verdict))
    finally:
        # The scorer only needs the context file during dispatch (R6).
        try:
            os.unlink(context_path)
        except OSError:
            pass

    if blockers:
        report_push_blockers(blockers)
    if not ungated_ids:
        log("proposal-intake-gate: gated %d proposal(s)" % gated)
        return 4 if blockers else 0  # blockers: ungated content would ship
    log("warning: no verdict applied for: %s" % ", ".join(ungated_ids))
    log("their annotations are deferred to the next run")
    if gated or blockers:
        return 4  # partial progress and/or push-blocking ungated content
    if parse_failures:
        return 3
    if dispatch_failures:
        return 2
    return 4  # scorer answered but returned no usable verdicts — still partial


if __name__ == "__main__":
    sys.exit(main())
