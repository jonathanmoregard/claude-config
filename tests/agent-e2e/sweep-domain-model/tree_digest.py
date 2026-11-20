"""Digest of the fixture's files, read straight from disk: no git, so nothing
the agent under test wrote into .git (config, index flags, excludes, hooks)
can change what is seen or run code in the grader. Skips .git and
node_modules (the links setup.sh makes). usage: tree_digest.py WORK_DIR"""
import hashlib
import os
import sys

root = sys.argv[1]
h = hashlib.sha256()
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = sorted(d for d in dirnames if d not in ('.git', 'node_modules'))
    for name in sorted(filenames):
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, root)
        h.update(rel.encode() + b'\0')
        h.update((b'L' + os.readlink(path).encode()) if os.path.islink(path) else open(path, 'rb').read())
        h.update(b'\0')
print(h.hexdigest())
