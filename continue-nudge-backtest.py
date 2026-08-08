#!/usr/bin/env python3
"""
Backtest: run the classifier against hand-labeled examples in labels.jsonl.

For each labeled example, we reconstruct the transcript-cut context from
the harvested fields (preceding_context + assistant_text) so the classifier
never sees future info leaked into the transcript file.

Usage:
  python3 continue-nudge-backtest.py [--in PATH] [--limit N]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude"))
from importlib import import_module  # noqa: E402

classifier = import_module("continue-nudge-classifier")

DEFAULT_IN = Path.home() / ".claude" / "continue-nudge" / "labels.jsonl"

# Map ground-truth label strings to the verdict token we'd expect.
LABEL_TO_VERDICT = {
    "nudge": "NUDGE_CONTINUE",
    "nudge_worthy": "NUDGE_CONTINUE",
    "legit": "LEGIT_PAUSE",
    "legit_pause": "LEGIT_PAUSE",
    "unclear": "UNCERTAIN",
}


def build_recent_turns(example):
    """Reconstruct the 'recent turns' block from preceding_context + assistant_text.

    Mirrors classifier.build_transcript_context's formatting: role labels
    USER/ASSISTANT, oldest first, blank line between turns, last N turns.
    """
    turns = []
    for t in example.get("preceding_context") or []:
        role = t.get("role")
        text = t.get("text", "")
        if role not in ("user", "assistant") or not text:
            continue
        turns.append((role, text))

    asst_text = example.get("assistant_text", "")
    if asst_text:
        turns.append(("assistant", asst_text))

    turns = turns[-classifier.RECENT_TURNS:]
    lines = []
    for role, text in turns:
        label = "USER" if role == "user" else "ASSISTANT"
        lines.append(f"{label}: {text}")
    return "\n\n".join(lines)


def run_one(example):
    opening = example.get("opening_msg") or "(none captured)"
    recent = build_recent_turns(example)
    if not recent:
        return None
    prompt = classifier.CLASSIFIER_PROMPT.format(
        opening_msg=opening,
        recent_turns=recent,
    )
    resp = classifier.call_sonnet(prompt)
    if resp is None:
        return None
    parsed = classifier.parse_verdict(resp)
    return parsed


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_path", type=Path, default=DEFAULT_IN)
    ap.add_argument("--limit", type=int, default=None, help="Max examples to run")
    return ap.parse_args()


def load_labeled(path):
    out = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            label = obj.get("label")
            if label is None:
                continue
            out.append(obj)
    return out


def main():
    args = parse_args()

    if not args.in_path.exists():
        print(f"ERROR: {args.in_path} does not exist", file=sys.stderr)
        sys.exit(1)

    examples = load_labeled(args.in_path)
    if args.limit:
        examples = examples[: args.limit]

    if not examples:
        print("No labeled examples found (label field non-null).")
        sys.exit(0)

    print(f"Running classifier on {len(examples)} labeled examples...\n")

    results = []
    # confusion: rows=label (nudge/legit/unclear), cols=verdict.
    confusion = {
        lbl: {"NUDGE_CONTINUE": 0, "LEGIT_PAUSE": 0, "UNCERTAIN": 0, "UNPARSED": 0}
        for lbl in ("nudge", "legit", "unclear", "other")
    }

    for i, ex in enumerate(examples, 1):
        raw_label = ex.get("label", "")
        label_norm = LABEL_TO_VERDICT.get(str(raw_label).lower().strip(), None)
        label_bucket = {
            "NUDGE_CONTINUE": "nudge",
            "LEGIT_PAUSE": "legit",
            "UNCERTAIN": "unclear",
        }.get(label_norm, "other")

        sid = (ex.get("session_id") or "?")[:8]
        turn_idx = ex.get("turn_index", "?")
        start = time.time()
        parsed = run_one(ex)
        dur = time.time() - start

        if parsed is None:
            confusion[label_bucket]["UNPARSED"] += 1
            print(f"[{i}/{len(examples)}] {sid}#{turn_idx} label={raw_label} verdict=UNPARSED ({dur:.1f}s)")
            results.append({"example": ex, "verdict": None, "reason": None})
            continue

        verdict = parsed["verdict"]
        confusion[label_bucket][verdict] += 1
        correct = (verdict == label_norm) if label_norm else None
        mark = "OK " if correct else ("XX " if correct is False else "?? ")
        print(
            f"[{i}/{len(examples)}] {mark}{sid}#{turn_idx} "
            f"label={raw_label} verdict={verdict} ({dur:.1f}s) — {parsed['reason'][:80]}"
        )
        results.append({"example": ex, "verdict": verdict, "reason": parsed["reason"]})

    # Precision / recall / F1 for the NUDGE class.
    tp = confusion["nudge"]["NUDGE_CONTINUE"]
    fn = (
        confusion["nudge"]["LEGIT_PAUSE"]
        + confusion["nudge"]["UNCERTAIN"]
        + confusion["nudge"]["UNPARSED"]
    )
    fp = (
        confusion["legit"]["NUDGE_CONTINUE"]
        + confusion["unclear"]["NUDGE_CONTINUE"]
        + confusion["other"]["NUDGE_CONTINUE"]
    )
    tn = confusion["legit"]["LEGIT_PAUSE"]

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )

    print("\n" + "=" * 70)
    print("CONFUSION MATRIX (rows = ground truth, cols = classifier verdict):")
    header = f"{'label\\verdict':>16}  {'NUDGE_CONT':>11}  {'LEGIT_PAUSE':>12}  {'UNCERTAIN':>10}  {'UNPARSED':>9}"
    print(header)
    for lbl in ("nudge", "legit", "unclear", "other"):
        row = confusion[lbl]
        print(
            f"{lbl:>16}  "
            f"{row['NUDGE_CONTINUE']:>11}  "
            f"{row['LEGIT_PAUSE']:>12}  "
            f"{row['UNCERTAIN']:>10}  "
            f"{row['UNPARSED']:>9}"
        )

    print(
        f"\nNUDGE_CONTINUE class: precision={precision:.2%} "
        f"recall={recall:.2%} F1={f1:.2%}  "
        f"(TP={tp} FP={fp} FN={fn} TN={tn})"
    )
    print(f"Targets: precision >=90%, recall >=60%")


if __name__ == "__main__":
    main()
