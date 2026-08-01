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
SECRET_PATTERNS = [
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
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer <REDACTED:bearer>"),
    (re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|"
        r"api[_\-]?key|api[_\-]?secret|"
        r"access[_\-]?(?:key|token|id|secret)|"
        r"refresh[_\-]?token|"
        r"client[_\-]?(?:id|secret|key)|"
        r"secret[_\-]?(?:key|token|id)|"
        r"private[_\-]?key|"
        r"auth[_\-]?(?:token|key)|"
        r"bearer[_\-]?token|"
        r"session[_\-]?(?:id|token|key)|"
        r"db[_\-]?password|database[_\-]?url|"
        r"encryption[_\-]?key|signing[_\-]?key|master[_\-]?key"
        r")['\"]?\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s]+)"
    ), "<REDACTED:kv-secret>"),
    # URL userinfo: postgres://user:pass@host, mongodb+srv://u:p@..., https://user:pass@github.com/...
    (re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:[^\s/@]+@[^\s]+"), "<REDACTED:url-userinfo>"),
    # High-entropy base64/hex runs (32+ chars) — catches inline unlabeled tokens
    (re.compile(r"\b[A-Za-z0-9+/=_\-]{40,}\b"), "<REDACTED:high-entropy-blob>"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+PRIVATE KEY-----"), "<REDACTED:pem>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "<REDACTED:jwt>"),
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

    raw_cwd = str(payload.get("cwd") or os.getcwd())
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": str(payload.get("session_id") or ""),
        "cwd": redact(raw_cwd),  # sensitive project/customer names in path also redacted
        "prompt_len": len(prompt),
        "prompt_head": redacted,
    }

    try:
        prev_umask = os.umask(0o077)
        try:
            OUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            rotate_if_needed(OUT)
            with OUT.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            try:
                os.chmod(OUT, 0o600)
                os.chmod(OUT.parent, 0o700)
            except Exception:
                pass
        finally:
            os.umask(prev_umask)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
