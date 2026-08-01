#!/usr/bin/env python3
"""UserPromptSubmit hook — append one JSONL line per user prompt.

Feeds /prompt-review-weekly skill: cluster repetitive prompt shapes into
an automation backlog. Adapted from Zijdel dev-hooks `prompt-log.sh`.

Security posture:
- File+dir created 0600/0700 (umask locked before mkdir/open).
- Prompt bodies pass through a redaction pass for common secret shapes
  (Anthropic, OpenAI, GitHub, AWS, Slack, GCP, Bearer, PEM, password=).
- Truncation to first 500 chars is length control, NOT security.
- File is size-capped and rotates once (.1 backup) at 10 MB.
- Never blocks: any failure exits 0 silently. Instrumentation, not gate.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path.home() / ".claude" / "automation-review" / "prompts.jsonl"
PROMPT_TRUNCATE = 500
SIZE_CAP_BYTES = 10 * 1024 * 1024

# Common secret shapes. Match, then substitute with a stable marker so the
# corpus still shows "there was a token here" for clustering purposes
# without leaking the token itself.
# Pattern order matters:
#   1. Multi-part / structured patterns (PEM, JWT) run FIRST so more-specific
#      markers win over the high-entropy fallback.
#   2. Named vendor patterns (sk-ant, ghp_, AKIA, etc.) — targeted, low FP.
#   3. Bearer / kv-secret / URL-userinfo — semi-structured shapes.
#   4. High-entropy fallback runs LAST so it only catches unlabeled tokens
#      no earlier pattern claimed. It's also the pattern that's SKIPPED
#      for cwd redaction (see redact_cwd() slice); this position is what
#      makes the skip work.
SECRET_PATTERNS = [
    # --- structured (specific > entropy) ---
    # Matches PKCS#8 (BEGIN PRIVATE KEY), PKCS#1 RSA, OPENSSH, EC, DSA, ENCRYPTED
    # — the type prefix is optional (`*` not `+`) so bare PKCS#8 doesn't slip past.
    (re.compile(r"-----BEGIN (?:[A-Z0-9 ]*)PRIVATE KEY-----[\s\S]+?-----END (?:[A-Z0-9 ]*)PRIVATE KEY-----"), "<REDACTED:pem>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "<REDACTED:jwt>"),
    # --- named vendors ---
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "<REDACTED:anthropic>"),
    (re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), "<REDACTED:openai>"),
    (re.compile(r"(?:sk|rk|pk|whsec)_(?:live|test)_[A-Za-z0-9]{20,}"), "<REDACTED:stripe>"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<REDACTED:github-pat>"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "<REDACTED:github-oauth>"),
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), "<REDACTED:github-server>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "<REDACTED:github-fine-grained>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<REDACTED:aws-access-key>"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "<REDACTED:aws-sts>"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "<REDACTED:google-api-key>"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "<REDACTED:slack>"),
    # --- semi-structured ---
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer <REDACTED:bearer>"),
    (re.compile(
        r"(?i)\b(?:password|passwd|secret|token|"
        r"api[_\-]?key|api[_\-]?secret|"
        r"access[_\-]?(?:key|token|secret)|"
        r"refresh[_\-]?token|"
        r"client[_\-]?(?:secret|key)|"
        r"secret[_\-]?(?:key|token|id)|"
        r"private[_\-]?key|"
        r"auth[_\-]?(?:token|key)|"
        r"bearer[_\-]?token|"
        r"session[_\-]?(?:token|key)|"
        r"db[_\-]?password|database[_\-]?url|"
        r"encryption[_\-]?key|signing[_\-]?key|master[_\-]?key|"
        r"shared[_\-]?access[_\-]?key|account[_\-]?key"
        r")['\"]?\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s]+)"
    ), "<REDACTED:kv-secret>"),
    # URL userinfo: postgres://user:pass@host, mongodb+srv://u:p@..., https://user:pass@github.com/...
    (re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:[^\s/@]+@[^\s]+"), "<REDACTED:url-userinfo>"),
    # --- HIGH-ENTROPY FALLBACK — MUST BE LAST ---
    # Catches inline unlabeled tokens no earlier pattern claimed. Excludes
    # `/` and `-` so path segments and long branch names don't over-redact.
    # SKIPPED for cwd via SECRET_PATTERNS[:-1] slice in main().
    (re.compile(r"\b[A-Za-z0-9+=_]{40,}\b"), "<REDACTED:high-entropy-blob>"),
]


def redact(text: str) -> str:
    for pat, marker in SECRET_PATTERNS:
        text = pat.sub(marker, text)
    return text


def rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= SIZE_CAP_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
            # Backup inherits the file's mode at rename time. If the caller had
            # not yet chmod'ed the source (e.g. first run against a pre-existing
            # loose file), the .1 backup would be world-readable. Force 0600
            # explicitly — this is the same posture the live file will have
            # after the caller's chmod.
            try:
                os.chmod(backup, 0o600)
            except Exception:
                pass
    except Exception:
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if not isinstance(payload, dict):
        sys.exit(0)

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        sys.exit(0)

    # ORDER MATTERS: redact BEFORE truncate. Truncating first can cut the END
    # marker off a PEM block or the tail of a matched token, defeating the regex
    # and leaking the head of a secret. Redact the full prompt, then truncate
    # the sanitized result to the byte cap.
    redacted = redact(prompt)[:PROMPT_TRUNCATE]

    # Cwd redaction skips the high-entropy fallback (last pattern) — long paths
    # are structure the /prompt-review-weekly clusterer needs. Vendor + URL +
    # kv-secret patterns still run in case a secret literally appears in cwd.
    raw_cwd = str(payload.get("cwd") or os.getcwd())
    redacted_cwd = raw_cwd
    for pat, marker in SECRET_PATTERNS[:-1]:
        redacted_cwd = pat.sub(marker, redacted_cwd)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": str(payload.get("session_id") or ""),
        "cwd": redacted_cwd,
        "prompt_len": len(prompt),
        "prompt_head": redacted,
    }

    try:
        prev_umask = os.umask(0o077)
        try:
            OUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(OUT.parent, 0o700)
            except Exception:
                pass
            rotate_if_needed(OUT)
            # Lock permissions on any pre-existing loose file BEFORE writing.
            # Prior order (open → write → chmod) left a window where a reader
            # racing the hook could read the new line at the pre-fix 0644 mode.
            if OUT.exists():
                try:
                    os.chmod(OUT, 0o600)
                except Exception:
                    pass
            with OUT.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # Chmod again in case the file was created just now by open("a").
            try:
                os.chmod(OUT, 0o600)
            except Exception:
                pass
        finally:
            os.umask(prev_umask)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
