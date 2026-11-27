#!/usr/bin/env python3
"""Serialize the Enron maildir (git mirror) into one canonical corpus file.

Format, repeated for every message in canonical order:
    <relative path>\n<decimal byte length>\n<raw message bytes>
Canonical order: path components compared lexicographically, except the
final 'N.' file names, which are compared numerically.
"""
import subprocess, sys, re, hashlib

repo, out = sys.argv[1], sys.argv[2]
ls = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "-z", "HEAD"],
                    check=True, capture_output=True).stdout
entries = []
for rec in ls.split(b"\0"):
    if not rec:
        continue
    meta, path = rec.split(b"\t", 1)
    mode, typ, sha = meta.split()
    if typ != b"blob":
        continue
    entries.append((path.decode("latin-1"), sha.decode()))

num = re.compile(r"^(\d+)\.$")
def key(p):
    parts = p.split("/")
    m = num.match(parts[-1])
    last = (0, int(m.group(1)), "") if m else (1, 0, parts[-1])
    return (parts[:-1], last)
entries.sort(key=lambda e: key(e[0]))

proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
import threading
def feeder():
    for _, sha in entries:
        proc.stdin.write(sha.encode() + b"\n")
    proc.stdin.close()
threading.Thread(target=feeder, daemon=True).start()

h = hashlib.sha256()
total = 0
with open(out, "wb") as f:
    for path, sha in entries:
        hdr = proc.stdout.readline()
        osha, typ, size = hdr.split()
        assert osha.decode() == sha and typ == b"blob", hdr
        size = int(size)
        data = proc.stdout.read(size)
        assert proc.stdout.read(1) == b"\n"
        rec = path.encode("latin-1") + b"\n" + str(size).encode() + b"\n" + data
        f.write(rec); h.update(rec)
        total += size
proc.wait()
print(f"messages={len(entries)} message_bytes={total} sha256={h.hexdigest()}")
