#!/usr/bin/env python3
"""Select the small test corpus (tests/data/enron_sample.corpus) from the full
canonical corpus.  The sample keeps the corpus format and order and is chosen
to exercise every code path of emz:

  * a contiguous run of one folder (path coding, Park-Miller Message-ID chain,
    date deltas) plus every copy of those messages anywhere in the archive
    (same-mailbox and cross-mailbox de-duplication);
  * messages that need the verbatim fallback; quoted-printable and base64
    bodies; non-ASCII bytes; non-canonical dates; address lists that do not
    follow the folding rule; folded subjects; messages without To; Outlook-style
    display-name lists; identical bodies under different headers.

usage: make_sample.py <enron.corpus> <out.corpus>
"""
import os, sys, hashlib, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emz import iter_corpus, Codec, FIELDS, parse_date, split_addrs, split_xlist, split_xlist_angle

src, dst = sys.argv[1], sys.argv[2]
PERCOPY = (b"Message-ID", b"X-Folder", b"X-Origin", b"X-FileName")
RUN_FOLDER, RUN_LEN = "allen-p/_sent_mail", 150
want = set(); content_of = {}; by_content = collections.defaultdict(list)
cases = collections.defaultdict(list); body_first = {}
K = 6
for i, (path, msg) in enumerate(iter_corpus(src)):
    folder, _, f = path.rpartition("/")
    p = Codec.parse(msg)
    if p is None:
        if len(cases["raw"]) < K: cases["raw"].append(i)
        continue
    F, has_to, has_cc, body = p
    key = hashlib.md5(b"\0".join(F.get(x, b"<none>") for x in FIELDS if x not in PERCOPY) + b"\0" + body).digest()
    by_content[key].append(i); content_of[i] = key
    if folder == RUN_FOLDER and f.rstrip(".").isdigit() and int(f.rstrip(".")) <= RUN_LEN: want.add(i)
    cte = F[b"Content-Transfer-Encoding"]
    def add(name, cond):
        if cond and len(cases[name]) < K: cases[name].append(i)
    add("qp", cte == b"quoted-printable" and len(cases["qp"]) < K)
    add("base64", cte == b"base64")
    add("nonascii", max(msg) > 127)
    add("bad_date", parse_date(F[b"Date"]) is None)
    add("to_not_canonical", has_to and split_addrs(F[b"To"]) is None)
    add("folded_subject", b"\r\n" in F[b"Subject"])
    add("no_to", not has_to)
    if has_to and split_addrs(F[b"To"]) is not None:
        n = len(split_addrs(F[b"To"]))
        add("outlook_names", n > 1 and len(split_xlist(F[b"X-To"])) != n and len(split_xlist_angle(F[b"X-To"])) == n)
    h = hashlib.md5(body).digest()
    if h in body_first and content_of.get(body_first[h]) != key and len(cases["same_body_other_headers"]) < 2 * K:
        cases["same_body_other_headers"] += [body_first[h], i]
    body_first.setdefault(h, i)
# every copy of the contiguous run's contents, and of the edge cases' contents
sel = set(want)
for i in list(want): sel.update(by_content[content_of[i]])
for name, idx in cases.items():
    sel.update(idx)
    for i in idx:
        if i in content_of: sel.update(by_content[content_of[i]][:3])
n = tot = 0
with open(dst, "wb") as out:
    for i, (path, msg) in enumerate(iter_corpus(src)):
        if i in sel:
            out.write(path.encode("latin-1") + b"\n" + str(len(msg)).encode() + b"\n" + msg); n += 1; tot += len(msg)
print(f"selected {n} messages, {tot} bytes; cases: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(cases.items())))
