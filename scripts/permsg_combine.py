#!/usr/bin/env python3
"""Combine single-instance storage with the primed-CM per-message results.

usage: permsg_combine.py <enron.corpus> <permsg-dir>

Reads the per-message sizes written by scripts/emcm_permsg.c for test set S1
(every 100th message): sizes_<raw|tr>_<N>MB.txt in <permsg-dir>.  A message
whose content occurred earlier in the archive is charged a 4-byte reference
plus its per-copy fields in the transformed form (Message-ID via the generator
anchors, folder fields via the folder table); every other message is charged
its primed-CM size plus a 2-byte length.
"""
import os, sys, glob, json, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emz import iter_corpus, Codec, FIELDS
from permsg_analysis import PERCOPY, build_tables, enc_mid

corpus, d = sys.argv[1], sys.argv[2]
ckey, S1, TR = [], [], []
for i, (path, m) in enumerate(iter_corpus(corpus)):
    p = Codec.parse(m)
    ckey.append(hashlib.md5(b"\0".join(p[0].get(f, b"<none>") for f in FIELDS if f not in PERCOPY) + b"\0" + p[3]).digest()
                if p is not None else hashlib.md5(m).digest())
    if i % 100 == 0: S1.append((i, path, m))
    elif i % 10 == 5: TR.append((i, path, m))
first = {}
for i, k in enumerate(ckey): first.setdefault(k, i)
T = build_tables(TR)
raw = sum(len(m) for _, _, m in S1)
percopy = {}
for i, path, m in S1:
    p = Codec.parse(m)
    if first[ckey[i]] < i and p is not None:
        F = p[0]; mid, _ = enc_mid(path, F, T)
        pc = (F[b"X-Folder"], F[b"X-Origin"], F[b"X-FileName"])
        hit = T["fold"].get(path.rpartition("/")[0]) == pc
        percopy[i] = 4 + 1 + len(mid) + (0 if hit else sum(len(x) + 1 for x in pc))
out = open(os.path.join(d, "cm_combined.jsonl"), "a")
for f in sorted(glob.glob(os.path.join(d, "sizes_*MB.txt"))):
    sizes = [int(x) for x in open(f)]
    if len(sizes) != len(S1): continue
    cm = sum(s + 2 for s in sizes)
    sis = sum(percopy[i] if i in percopy else s + 2 for (i, _, _), s in zip(S1, sizes))
    r = {"run": os.path.basename(f), "messages": len(S1), "copies": len(percopy), "raw_bytes": raw,
         "ratio_cm": raw / cm, "ratio_sis_plus_cm": raw / sis}
    out.write(json.dumps(r) + "\n"); print(json.dumps(r))
