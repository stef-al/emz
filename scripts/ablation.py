#!/usr/bin/env python3
"""Ablation ladder streams (sizes only; each format is trivially invertible).
  split : header block -> 'hdr' stream, body -> 'body' stream (no models)
  dedup : split + message/body de-duplication (copies coded as reference +
          their per-copy header lines as text)
usage: ablation.py <corpus> <outdir> split|dedup
"""
import sys, os, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emz import iter_corpus, Codec, FIELDS, WStream
corpus, out, mode = sys.argv[1:4]
os.makedirs(out, exist_ok=True)
S = {n: WStream() for n in ("hdr", "body", "ref", "path")}
content = {}; bodies = {}; prev_ref = -1; ncont = 0
PERCOPY = (b"Message-ID", b"X-Folder", b"X-Origin", b"X-FileName")
for path, msg in iter_corpus(corpus):
    S["path"].text(path.encode("latin-1"))
    i = msg.find(b"\r\n\r\n")
    if i < 0: i = len(msg); hdr, body = msg, b""
    else: hdr, body = msg[:i + 2], msg[i + 4:]
    if mode == "split":
        S["hdr"].blob(hdr + b"\0"); S["body"].blob(body + b"\0"); continue
    p = Codec.parse(msg)
    if p is not None:
        F = p[0]
        key = hashlib.md5(b"\0".join([F.get(f, b"<none>") for f in FIELDS if f not in PERCOPY]) + b"\0" + body).digest()
        cid = content.get(key)
        if cid is not None:
            d = cid - (prev_ref + 1); prev_ref = cid
            S["ref"].byte(1); S["ref"].sint(d)
            S["hdr"].blob(b"".join(f + b": " + F[f] + b"\r\n" for f in PERCOPY) + b"\0")
            continue
        content[key] = ncont; ncont += 1
    S["ref"].byte(0)
    S["hdr"].blob(hdr + b"\0")
    h = hashlib.md5(body).digest()
    if h in bodies: S["ref"].uint(len(bodies) - bodies[h]); continue
    bodies[h] = len(bodies); S["ref"].uint(0)
    S["body"].blob(body + b"\0")
for n, w in S.items():
    open(os.path.join(out, n + ".raw"), "wb").write(w.b)
