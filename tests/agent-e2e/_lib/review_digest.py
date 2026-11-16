"""Digest one run's transcript for the review-v2 scenarios, for graders and
for reading by hand.

Usage: review_digest.py RUN

Writes RUN/digest/:
  skills.txt          every Skill invocation from the main thread, in order
  dispatch-NN.prompt  each Agent dispatch's prompt (main thread, in order)
  dispatch-NN.reply   that subagent's final reply: its last spoken text, or
                      the tool_result text when the stream holds no turns
  dispatches.tsv      NN, subagent_type, opening line of the prompt
  edits.txt           every file path the main thread wrote or edited
Prints dispatches.tsv to stdout.
"""
import json
import sys
from pathlib import Path


def blocks(ev):
    m = ev.get("message")
    c = m.get("content") if isinstance(m, dict) else m
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def unframe(text):
    """The subagent's own report out of a hand-back tool_result: the harness
    wraps it in a preamble ending "The report follows:", indents every line,
    and appends `agentId:` / <usage> lines. Text without the frame is
    returned as is."""
    marker = "The report follows:"
    if marker not in text:
        return text
    body = text.split(marker, 1)[1]
    lines = []
    for line in body.splitlines():
        if line.startswith("agentId:") or line.startswith("<usage>"):
            break
        lines.append(line[2:] if line.startswith("  ") else line)
    return "\n".join(lines).strip() + "\n"


def main(run):
    run = Path(run)
    out = run / "digest"
    out.mkdir(exist_ok=True)
    skills, edits, dispatches = [], [], []
    spoken, results = {}, {}
    for line in open(run / "transcript.jsonl", encoding="utf-8"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        parent = ev.get("parent_tool_use_id")
        if parent:
            if ev.get("type") == "assistant":
                said = text_of([b for b in blocks(ev) if b.get("type") == "text"])
                if said.strip():
                    spoken[parent] = said
            continue
        for b in blocks(ev):
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                results[b["tool_use_id"]] = text_of(b.get("content"))
            if b.get("type") != "tool_use":
                continue
            inp = b.get("input") or {}
            name = b.get("name")
            if name == "Skill":
                skills.append(str(inp.get("skill", "")))
            elif name in ("Agent", "Task"):
                dispatches.append((b.get("id"), str(inp.get("subagent_type", "")),
                                   str(inp.get("prompt", ""))))
            elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                edits.append(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    (out / "skills.txt").write_text("".join(s + "\n" for s in skills), encoding="utf-8")
    (out / "edits.txt").write_text("".join(e + "\n" for e in edits), encoding="utf-8")
    rows = []
    for n, (tid, kind, prompt) in enumerate(dispatches, 1):
        (out / f"dispatch-{n:02d}.prompt").write_text(prompt, encoding="utf-8")
        reply = spoken.get(tid) or unframe(results.get(tid, ""))
        (out / f"dispatch-{n:02d}.reply").write_text(reply, encoding="utf-8")
        first = next((l for l in prompt.splitlines() if l.strip()), "")[:100]
        rows.append(f"{n:02d}\t{kind}\t{first}")
    (out / "dispatches.tsv").write_text("".join(r + "\n" for r in rows), encoding="utf-8")
    print("\n".join(rows))


if __name__ == "__main__":
    main(sys.argv[1])
