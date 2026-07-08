#!/usr/bin/env python3
"""Generic brand-copy linter. Enforces a project's copy-rules TOML mechanically.

All tonality/rules come from configuration; nothing brand-specific is hardcoded.

Usage:
    lint.py --rules PATH FILE [FILE ...]
    lint.py --rules PATH --text "copy to check" [--as-file relative/path.md]
    lint.py FILE ...            # discovers docs/brand/copy-rules.toml upward from CWD

Rules TOML shape (all sections optional):
    [errors]                banned_phrases = ["..."]
    [errors.em_dash]        ban_em_dash = true, allow_en_dash_between_digits = true
    [errors.competitor_names] names = ["..."], allowed_files = ["docs/..."]
    [warnings]              <name> = "<regex>"   (any number)
    [warnings.burstiness]   min_sentences = N, min_stdev_words = X

Exit codes: 0 = clean (warnings allowed), 1 = errors found, 2 = usage/config problem.
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import tomllib
from pathlib import Path


def discover_rules(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        cand = d / "docs" / "brand" / "copy-rules.toml"
        if cand.is_file():
            return cand
    return None


def load_rules(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"copy-lint: cannot load rules ({path}): {e}", file=sys.stderr)
        sys.exit(2)


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def check_banned_phrases(text: str, rules: dict) -> list[tuple[int, str]]:
    hits = []
    low = text.lower()
    for phrase in rules.get("errors", {}).get("banned_phrases", []):
        start = 0
        while (i := low.find(phrase.lower(), start)) != -1:
            hits.append((line_of(text, i), f"banned phrase: {phrase!r}"))
            start = i + 1
    return hits


def check_dashes(text: str, rules: dict) -> list[tuple[int, str]]:
    cfg = rules.get("errors", {}).get("em_dash", {})
    hits = []
    if cfg.get("ban_em_dash", False):
        for m in re.finditer("—", text):
            hits.append((line_of(text, m.start()), "em dash (U+2014); use parens/colon/comma"))
    if cfg.get("allow_en_dash_between_digits", False):
        for m in re.finditer("–", text):
            i = m.start()
            before = text[i - 1] if i > 0 else ""
            after = text[i + 1] if i + 1 < len(text) else ""
            if not (before.isdigit() and after.isdigit()):
                hits.append((line_of(text, i), "en dash (U+2013) outside a numeric range"))
    return hits


def check_competitors(text: str, rules: dict, as_file: str | None) -> list[tuple[int, str]]:
    cfg = rules.get("errors", {}).get("competitor_names", {})
    names = cfg.get("names", [])
    if not names:
        return []
    allowed = {Path(p).as_posix() for p in cfg.get("allowed_files", [])}
    if as_file is not None and Path(as_file).as_posix() in allowed:
        return []
    hits = []
    for name in names:
        for m in re.finditer(re.escape(name), text, re.IGNORECASE):
            hits.append((line_of(text, m.start()),
                         f"competitor name {name!r} outside allowed pages"))
    return hits


def check_warnings(text: str, rules: dict) -> list[tuple[int, str]]:
    warns = []
    wsection = rules.get("warnings", {})
    for name, pattern in wsection.items():
        if not isinstance(pattern, str):
            continue  # sub-tables like burstiness
        for m in re.finditer(pattern, text, re.IGNORECASE):
            snippet = m.group(0)[:60].replace("\n", " ")
            warns.append((line_of(text, m.start()), f"{name}: {snippet!r}"))

    cfg = wsection.get("burstiness")
    if cfg:
        sentences = [s.strip() for s in re.split(r"[.!?]+\s", text) if s.strip()]
        if len(sentences) >= cfg["min_sentences"]:
            lengths = [len(s.split()) for s in sentences]
            stdev = statistics.pstdev(lengths)
            if stdev < cfg["min_stdev_words"]:
                warns.append(
                    (0, f"burstiness: sentence-length stdev {stdev:.1f} < "
                        f"{cfg['min_stdev_words']} (uniform AI rhythm; vary lengths)")
                )
    return warns


def lint(text: str, rules: dict, as_file: str | None, label: str) -> bool:
    errors = (
        check_banned_phrases(text, rules)
        + check_dashes(text, rules)
        + check_competitors(text, rules, as_file)
    )
    warnings = check_warnings(text, rules)
    for ln, msg in sorted(errors):
        print(f"{label}:{ln}: ERROR {msg}")
    for ln, msg in sorted(warnings):
        print(f"{label}:{ln}: WARN  {msg}")
    if not errors and not warnings:
        print(f"{label}: clean")
    return not errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="copy files to lint")
    ap.add_argument("--rules", help="path to copy-rules.toml "
                                    "(default: discover docs/brand/copy-rules.toml upward from CWD)")
    ap.add_argument("--text", help="lint a string instead of files")
    ap.add_argument("--as-file", help="treat --text as this project-relative path "
                                      "(for competitor-name allowlists)")
    args = ap.parse_args()
    if bool(args.files) == bool(args.text is not None):
        ap.error("pass either FILEs or --text, not both/neither")

    rules_path = Path(args.rules) if args.rules else discover_rules(Path.cwd())
    if rules_path is None:
        print("copy-lint: no --rules given and no docs/brand/copy-rules.toml found "
              "upward from CWD", file=sys.stderr)
        return 2
    rules = load_rules(rules_path)
    project_root = rules_path.parents[2] if len(rules_path.parents) >= 3 else rules_path.parent

    ok = True
    if args.text is not None:
        ok = lint(args.text, rules, args.as_file, "<text>")
    else:
        for f in args.files:
            p = Path(f)
            try:
                content = p.read_text(encoding="utf-8")
            except OSError as e:
                print(f"copy-lint: {e}", file=sys.stderr)
                return 2
            try:
                rel_str = p.resolve().relative_to(project_root).as_posix()
            except ValueError:
                rel_str = p.as_posix()
            ok = lint(content, rules, rel_str, f) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
