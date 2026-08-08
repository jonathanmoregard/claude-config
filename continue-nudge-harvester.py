#!/usr/bin/env python3
"""
Harvest candidate stop/deferral moments from historic transcripts.

For each assistant->user transition where the assistant text passes the
fast gate, emit a JSONL row describing the transition so a human can
hand-label it later. Overwrites the output file each run.

Usage:
  python3 continue-nudge-harvester.py [--days 60] [--max 300] [--out PATH]
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude"))
from importlib import import_module  # noqa: E402

classifier = import_module("continue-nudge-classifier")
detector = import_module("continue-nudge-detector")

PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_OUT = Path.home() / ".claude" / "continue-nudge" / "labels.jsonl"

FAST_GATE_RE = detector.FAST_GATE_RE

# Positive-class user reply patterns (from spec § Ground truth harvest).
POS_PATTERNS = [
    r"\bjust\s+(do it|go|proceed|continue|keep going)\b",
    r"\b(please )?(do it|go on|go ahead|proceed|continue|keep going)\b",
    r"\bdon'?t\s+(defer|ask|stop|wait|pause)\b",
    r"\bwhy did you (stop|pause|ask|defer)\b",
    r"\byou (should have|didn'?t need to) (kept? going|asked|stopped|paused)\b",
    r"\bbe (more )?autonomous\b",
    r"\btrust your (judg[e]?ment|intuition|discretion)\b",
    r"\bstop (asking|deferring|checking in)\b",
    r"\bno need to ask\b",
]
POS_RE = re.compile("|".join(POS_PATTERNS), re.IGNORECASE)

# Short affirmatives after an assistant question — count as positive.
SHORT_AFFIRMATIVES = {"yes", "y", "yep", "yeah", "sure", "go", "ok", "okay"}

# Negative-class signals.
COMPLETE_SIGNALS_RE = re.compile(
    r"^\s*(thanks|thank you|thx|done|perfect|great|nice|awesome|amazing|beautiful|lovely)\b[\s!.?]*$",
    re.IGNORECASE,
)
REDIRECT_RE = re.compile(r"\bno,?\s+(actually|instead|wait)\b", re.IGNORECASE)


def word_count(s):
    return len(re.findall(r"\S+", s))


def classify_next_user(assistant_text, next_user_text):
    """Return 'positive' | 'negative' | 'neither'."""
    if not next_user_text:
        return "neither"

    stripped = next_user_text.strip()
    lower_short = stripped.lower().rstrip("!.?,").strip()

    # Positive: explicit push-back patterns.
    if POS_RE.search(next_user_text):
        return "positive"

    # Positive: very short affirmative reply to an assistant question.
    if lower_short in SHORT_AFFIRMATIVES:
        # Assistant text already passed the fast gate, so it's a deferral/question.
        return "positive"

    wc = word_count(next_user_text)

    # Negative: task-complete signal (short "thanks!" etc.).
    if COMPLETE_SIGNALS_RE.match(stripped) and wc <= 6:
        return "negative"

    # Negative: substantial redirection.
    if REDIRECT_RE.search(next_user_text) and wc >= 15:
        return "negative"

    # Negative: long new instruction with concrete content (>=60 words).
    if wc >= 60:
        return "negative"

    return "neither"


def iter_jsonl(path):
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def entry_text_and_role(obj):
    """Return (role, text) for a real user/assistant entry, else (None, '')."""
    t = obj.get("type")
    if t not in ("user", "assistant"):
        return None, ""
    if obj.get("isSidechain") or obj.get("isMeta"):
        return None, ""
    if t == "user" and not classifier.is_real_user_entry(obj):
        return None, ""
    content = obj.get("message", {}).get("content")
    text = classifier.clean_text(classifier.extract_text(content))
    if not text:
        return None, ""
    return t, text


def assistant_last_text_block(obj):
    """Return the last text-block text of an assistant entry, or ''."""
    content = obj.get("message", {}).get("content")
    text_parts = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                s = item.get("text", "")
                if s:
                    text_parts.append(s)
    elif isinstance(content, str):
        text_parts.append(content)
    text = " ".join(text_parts).strip()
    text = classifier.clean_text(text)
    return text


def harvest_file(path, cutoff_mtime):
    """Yield candidate dicts for one transcript file."""
    try:
        if path.stat().st_mtime < cutoff_mtime:
            return
    except OSError:
        return

    # Load all entries, but keep only real user + assistant turns in order,
    # collapsing consecutive same-role entries by concatenating text.
    turns = []  # list of (role, text, orig_index)
    for i, obj in enumerate(iter_jsonl(path)):
        role, text = entry_text_and_role(obj)
        if role is None:
            continue

        # For assistant, we care about the last TEXT block specifically (that's
        # what the fast gate + classifier see).
        if role == "assistant":
            asst_text = assistant_last_text_block(obj)
            if not asst_text:
                continue
            text = asst_text

        # Collapse consecutive same-role turns (rare but happens).
        if turns and turns[-1][0] == role:
            prev_role, prev_text, prev_i = turns[-1]
            turns[-1] = (prev_role, prev_text + "\n\n" + text, prev_i)
        else:
            turns.append((role, text, i))

    session_id = path.stem  # jsonl file stem is the session id
    transcript_path = str(path)

    # Task-opening user msg = first user turn's text.
    opening_msg = ""
    for role, text, _ in turns:
        if role == "user":
            opening_msg = text
            break

    # Walk transitions; for each assistant->user pair where assistant matches
    # the fast gate, emit a candidate.
    for j in range(len(turns) - 1):
        role_a, text_a, idx_a = turns[j]
        role_b, text_b, idx_b = turns[j + 1]
        if role_a != "assistant" or role_b != "user":
            continue
        # Fast gate applies to last 3000 chars of assistant text.
        gate_input = text_a[-classifier.MAX_ASSISTANT_TAIL:]
        if not FAST_GATE_RE.search(gate_input):
            continue

        # Preceding context = last 8 turns strictly before this assistant.
        preceding = turns[max(0, j - 8):j]
        preceding_context = [
            {"role": r, "text": t[:classifier.MAX_TEXT_PER_TURN]}
            for r, t, _ in preceding
        ]

        candidate_class = classify_next_user(text_a, text_b)

        yield {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "turn_index": j,
            "assistant_text": text_a[-classifier.MAX_ASSISTANT_TAIL:],
            "next_user_text": text_b[:classifier.MAX_TEXT_PER_TURN],
            "opening_msg": opening_msg[:classifier.MAX_TEXT_PER_TURN],
            "preceding_context": preceding_context,
            "candidate_class": candidate_class,
            "label": None,
            "label_source": None,
        }


def should_skip_file(path):
    parts = path.parts
    if "subagents" in parts:
        return True
    s = str(path)
    if "homunculus" in s:
        return True
    return False


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60, help="Look back N days (default 60)")
    ap.add_argument("--max", type=int, default=300, help="Max candidates to keep (default 300)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSONL path")
    return ap.parse_args()


def main():
    args = parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    cutoff = (datetime.now() - timedelta(days=args.days)).timestamp()

    files = []
    if PROJECTS.exists():
        for p in PROJECTS.rglob("*.jsonl"):
            if should_skip_file(p):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            files.append((mtime, p))
    files.sort(reverse=True)

    candidates = []
    counts = {"positive": 0, "negative": 0, "neither": 0}

    for _, path in files:
        if len(candidates) >= args.max:
            break
        try:
            for cand in harvest_file(path, cutoff):
                candidates.append(cand)
                counts[cand["candidate_class"]] += 1
                if len(candidates) >= args.max:
                    break
        except Exception as e:
            print(f"warn: {path}: {e!r}", file=sys.stderr)
            continue

    # Overwrite output.
    with open(args.out, "w") as f:
        for cand in candidates:
            f.write(json.dumps(cand) + "\n")

    print(f"Wrote {len(candidates)} candidates to {args.out}")
    print(f"  positive: {counts['positive']}")
    print(f"  negative: {counts['negative']}")
    print(f"  neither:  {counts['neither']}")
    print(f"  window:   last {args.days} days across {len(files)} transcripts")


if __name__ == "__main__":
    main()
