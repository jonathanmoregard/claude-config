#!/usr/bin/env python3
"""Record what you decided about a permission-ledger proposal.

Nothing writes decided.json automatically — /review-improvements has no
knowledge of this feature. Without a decision recorded, a pattern you rejected
comes back in tomorrow's digest, and every digest after that, forever. This is
the thing that stops that.

Usage:
    record-decision.py <pattern_key> implemented|rejected [--count N]

--count is the group's count at the time you decided; it comes from the
proposal's "## Friction" line. It is what makes the one re-ask work: a rejected
pattern returns exactly once, when the friction has tripled. Omit it and the
pattern is suppressed permanently, which is a bigger commitment than it looks.
"""
import argparse
import datetime
import json
import os
import sys

DEFAULT_PATH = os.path.expanduser("~/.claude/permission-ledger/decided.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pattern_key")
    parser.add_argument("decision", choices=("implemented", "rejected"))
    parser.add_argument("--count", type=int, default=None,
                        help="group count at decision time; enables the 3x re-ask")
    parser.add_argument("--path", default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    path = os.path.expanduser(args.path)
    try:
        with open(path, encoding="utf-8") as fh:
            decided = json.load(fh)
        if not isinstance(decided, dict):
            raise ValueError("decided.json is not an object")
    except FileNotFoundError:
        decided = {}
    except (ValueError, OSError) as exc:
        print("refusing to overwrite an unreadable %s: %s" % (path, exc), file=sys.stderr)
        return 1

    entry = {
        "decision": args.decision,
        "date": datetime.date.today().isoformat(),
    }
    if args.count is not None:
        entry["count_at_decision"] = args.count

    previous = decided.get(args.pattern_key)
    decided[args.pattern_key] = entry

    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(decided, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    if previous:
        print("%s: %s -> %s" % (args.pattern_key, previous.get("decision"), args.decision))
    else:
        print("%s: %s" % (args.pattern_key, args.decision))
    if args.count is None:
        print("no --count given: suppressed permanently, no re-ask if friction grows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
