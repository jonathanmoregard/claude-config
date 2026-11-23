"""Grade a run by its actions: did any Bash command invoke sed or awk?

Usage: no_sed_awk.py RUN_DIR TARGET_FILE
Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE (the agent never touched TARGET_FILE).

Each command is split at shell operators into simple commands and then into
words; the program word (after env assignments and wrappers like sudo/env/
xargs) whose basename is a sed/awk binary is an invocation. The quoted script
handed to a shell or eval (`bash -c '...'`) is parsed again; `$(...)` and
backtick bodies are their own simple command. A sed/awk that only appears as an argument
(`grep 'sed' f`) is not an invocation.
"""
import json
import os
import re
import shlex
import sys

TOOLS = {"sed", "gsed", "awk", "gawk", "mawk", "nawk"}
PUNCT = "();<>|&`{}"
SHELLS = {"bash", "sh", "zsh", "dash", "eval"}


def segments(text: str):
    """Split TEXT into simple commands at shell operators outside quotes."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=PUNCT)
    lex.whitespace_split = True
    cur: list[str] = []
    try:
        for tok in lex:
            if tok and all(c in PUNCT for c in tok):
                if cur:
                    yield cur
                cur = []
            else:
                cur.append(tok)
    except ValueError:  # unbalanced quote: fall back to plain words
        cur = text.split()
    if cur:
        yield cur


WRAPPERS = {"sudo", "env", "command", "exec", "xargs", "nohup", "time", "nice", "timeout", "stdbuf"}


def invoked(text: str, depth: int = 0):
    """Yield the program word of every simple command in TEXT, recursing
    into the quoted script a shell or eval is handed (`bash -c '...'`)."""
    for toks in segments(text):
        i = 0
        while i < len(toks) and (re.match(r"^\w+=", toks[i]) or toks[i] in WRAPPERS
                                 or (i and toks[i - 1] == "timeout")):
            i += 1
        if i < len(toks):
            yield toks[i]
        if depth < 3 and i < len(toks) and os.path.basename(toks[i]) in SHELLS:
            for t in toks[i + 1:]:
                if re.search(r"\s", t):
                    yield from invoked(t, depth + 1)


def main() -> int:
    run, target = sys.argv[1], sys.argv[2]
    cmds = open(os.path.join(run, "commands.txt"), encoding="utf-8").read()
    for line in cmds.splitlines():
        for w in invoked(line):
            if os.path.basename(w) in TOOLS:
                print(f"FAIL: invoked {w}: {line[:160]}")
                return 1
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from skill_listing import tool_uses
    touched = any(target in json.dumps(i) for _, i in tool_uses(os.path.join(run, "transcript.jsonl")))
    if not touched:
        print(f"INCONCLUSIVE: {target} never touched")
        return 2
    print("PASS: no sed/awk invoked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
