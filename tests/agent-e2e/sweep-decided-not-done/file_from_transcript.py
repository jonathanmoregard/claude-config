"""Rebuild the newest file the agent wrote whose path matches, from its tool calls.

argv: transcript.jsonl, a file-name substring (e.g. "inventory"), output
file, and optionally a substring the full path must contain (e.g. "decisions/").
The agent may write sweep files to a scratch path (the sandbox's private /tmp)
and fail to copy them into the notes dir; the content is still in its Write
and Edit calls. Replays, per matching path, the last Write and every Edit
after it, and writes the most recently touched one to the output file.
Exit 1 if no call matched, 2 if this script itself failed.
"""
import json
import sys


def main():

    needle = sys.argv[2]
    in_dir = sys.argv[4] if len(sys.argv) > 4 else ''  # optional: the full path must contain this
    docs, order = {}, []
    for line in open(sys.argv[1], encoding='utf-8'):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get('type') != 'assistant':
            continue
        msg = ev.get('message')
        content = msg.get('content') if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get('type') != 'tool_use':
                continue
            inp = b.get('input') if isinstance(b.get('input'), dict) else {}
            path = str(inp.get('file_path', ''))
            if needle not in path.rsplit('/', 1)[-1] or in_dir not in path:
                continue
            if b.get('name') == 'Write':
                docs[path] = str(inp.get('content', ''))
            elif b.get('name') == 'Edit' and path in docs:
                old, new = str(inp.get('old_string', '')), str(inp.get('new_string', ''))
                docs[path] = docs[path].replace(old, new, -1 if inp.get('replace_all') else 1)
            else:
                continue
            order.append(path)
    if not order:
        return 1
    with open(sys.argv[3], 'w', encoding='utf-8') as f:
        f.write(docs[order[-1]])
    return 0


try:
    code = main()
except Exception as e:  # a reader fault is not the agent's FAIL
    print(f'file_from_transcript: {e!r}', file=sys.stderr)
    code = 2
sys.exit(code)
