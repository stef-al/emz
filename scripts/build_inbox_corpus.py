#!/usr/bin/env python3
"""Serialise a public-inbox v2 epoch (one commit per message, blob 'm') into
the canonical corpus format, in chronological order of the Date header.

usage: build_inbox_corpus.py <inbox.git> <out.corpus> [name] [--maildir-only] [--rev COMMIT]
Paths: <name>/<YYYY>/<n>.  (n = position within the year)
--maildir-only skips messages reconstructed from web pages
(X-Archive-Source: openwall-scrape); --rev selects a snapshot (default HEAD).
"""
import sys, subprocess, hashlib, email.utils, collections, datetime, re
repo, out = sys.argv[1], sys.argv[2]
name = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else "list"
maildir_only = "--maildir-only" in sys.argv
rev = sys.argv[sys.argv.index("--rev") + 1] if "--rev" in sys.argv else "HEAD"
log = subprocess.run(["git", "-C", repo, "log", "--reverse", "--raw", "--no-abbrev", "--format=C %H %ct", rev],
                     check=True, capture_output=True, text=True).stdout
blobs = []; ct = None
for line in log.splitlines():
    if line.startswith("C "):
        ct = int(line.split()[2])
    elif line.startswith(":") and line.endswith("\tm"):
        parts = line.split()
        if parts[4] in ("A", "M"):
            blobs.append((parts[3], ct))
p = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
import threading
def feed():
    for sha, _ in blobs: p.stdin.write(sha.encode() + b"\n")
    p.stdin.close()
threading.Thread(target=feed, daemon=True).start()
msgs = []; stats = collections.Counter()
DATE = re.compile(rb"^Date:[ \t]*(.*?)\r?$", re.M | re.I)
for k, (sha, ct) in enumerate(blobs):
    hdr = p.stdout.readline().split(); n = int(hdr[2]); data = p.stdout.read(n); p.stdout.read(1)
    head = data.split(b"\n\n", 1)[0] if b"\r\n\r\n" not in data[:65536] else data.split(b"\r\n\r\n", 1)[0]
    scrape = b"X-Archive-Source: openwall-scrape" in head
    stats["scrape" if scrape else "full"] += 1
    if maildir_only and scrape: continue
    ts = None
    m = DATE.search(head)
    if m:
        try:
            ts = email.utils.parsedate_to_datetime(m.group(1).decode("latin-1")).timestamp()
        except Exception:
            ts = None
    if ts is None: ts = ct; stats["date_fallback"] += 1
    msgs.append((ts, k, data))
p.wait()
msgs.sort(key=lambda x: (x[0], x[1]))
h = hashlib.sha256(); per_year = collections.Counter(); total = 0
with open(out, "wb") as f:
    for ts, k, data in msgs:
        y = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).year if ts > 0 else 1970
        per_year[y] += 1
        rec = f"{name}/{y}/{per_year[y]}.".encode() + b"\n" + str(len(data)).encode() + b"\n" + data
        f.write(rec); h.update(rec); total += len(data)
print(f"messages={len(msgs)} message_bytes={total} sha256={h.hexdigest()} stats={dict(stats)}")
print("per year:", dict(sorted(per_year.items())))
