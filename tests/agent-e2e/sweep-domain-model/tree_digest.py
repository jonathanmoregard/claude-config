"""Digest of the fixture's files, read straight from disk: no git, so nothing
the agent under test wrote into .git (config, index flags, excludes, hooks)
can change what is seen or run code in the grader. Every entry counts: files
by content and mode, symlinks (to files or directories) by target, empty
directories by name. Only the top-level .git and node_modules (the links
setup.sh makes) are skipped. usage: tree_digest.py WORK_DIR"""
import hashlib
import os
import stat
import sys

root = sys.argv[1]
h = hashlib.sha256()


def entry(kind, rel, payload=b''):
    h.update(kind + b'\0' + rel.encode() + b'\0' + payload + b'\0')


for dirpath, dirnames, filenames in os.walk(root):
    top = dirpath == root
    names = sorted(dirnames + filenames)
    dirnames[:] = []
    for name in names:
        if top and name in ('.git', 'node_modules'):
            continue
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, root)
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            entry(b'L', rel, os.readlink(path).encode())
        elif stat.S_ISDIR(st.st_mode):
            entry(b'D', rel)
            dirnames.append(name)
        elif stat.S_ISREG(st.st_mode):
            entry(b'F', rel, oct(stat.S_IMODE(st.st_mode)).encode() + b'\0' + open(path, 'rb').read())
        else:
            entry(b'?', rel)  # fifo, socket, device: never opened, still counted
    dirnames.sort()
print(h.hexdigest())
