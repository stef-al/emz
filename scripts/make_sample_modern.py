#!/usr/bin/env python3
"""Select tests/data/oss_sample.corpus from the oss-security corpus built by
scripts/fetch_oss_security.sh: a contiguous run of messages (threads whose
parents are inside the sample) plus messages that exercise every MIME and
header model path (quoted-printable variants, base64 text and binary parts,
nested multiparts, DKIM/ARC signatures, X-VM-v5-Data, Autocrypt).

usage: make_sample_modern.py <oss.corpus> <out.corpus>
"""
import os, sys, collections, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emz import iter_corpus, parse_message, mime_segments, looks_text

src, dst = sys.argv[1], sys.argv[2]
RUN = ("oss-security/2023/", 101, 200)          # contiguous block
want = set(); cases = collections.defaultdict(list); K = 4
for i, (path, msg) in enumerate(iter_corpus(src)):
    d, _, f = path.rpartition("/")
    if path.startswith(RUN[0]) and RUN[1] <= int(f.rstrip(".")) <= RUN[2]: want.add(i)
    P = parse_message(msg)
    if P is None:
        if len(cases["raw"]) < K: cases["raw"].append(i)
        continue
    crlf, fields, body = P
    vals = {}
    for n, sp, v in fields: vals.setdefault(n.lower(), v)
    def add(name, cond):
        if cond and len(cases[name]) < K: cases[name].append(i)
    eol = b"\r\n" if crlf else b"\n"
    ct = vals.get(b"content-type", b"").replace(eol, b" ")
    cte = vals.get(b"content-transfer-encoding", b"").strip().lower()
    segs = mime_segments(ct, cte, body, eol)
    for k, p in segs:
        if k == "Q": add("qp%d" % p[0], True)
        if k == "B": add("b64_text" if looks_text(p[2]) else "b64_bin", True)
    add("nested_multipart", len(re.findall(rb"(?i)content-type:\s*multipart/", body)) > 0)
    add("dkim", b"dkim-signature" in vals and len(msg) < 20000)
    add("arc", b"arc-seal" in vals and len(msg) < 30000)
    add("x_vm", b"x-vm-v5-data" in vals and len(msg) < 8000)
    add("autocrypt", b"autocrypt" in vals and len(msg) < 12000)
    add("references", b"references" in vals and len(msg) < 6000)
sel = set(want)
for idx in cases.values(): sel.update(idx)
n = tot = 0
with open(dst, "wb") as out:
    for i, (path, msg) in enumerate(iter_corpus(src)):
        if i in sel:
            out.write(path.encode("latin-1") + b"\n" + str(len(msg)).encode() + b"\n" + msg); n += 1; tot += len(msg)
print(f"selected {n} messages, {tot} bytes; cases: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(cases.items())))
