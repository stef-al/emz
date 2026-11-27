#!/usr/bin/env python3
"""Per-message (random-access) compression on a 10% sample: every 10th message.
Dictionary for zstd is trained on a disjoint 10% (index % 10 == 5)."""
import os, sys, zlib, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import zstandard as zstd, brotli
from emz import iter_corpus
sample, train = [], []
for i, (p, m) in enumerate(iter_corpus(sys.argv[1])):
    if i % 10 == 0: sample.append(m)
    elif i % 10 == 5 and len(train) < 20000: train.append(m)
raw = sum(len(m) for m in sample)
res = {"messages": len(sample), "raw_bytes": raw}
def run(name, f):
    t = time.time(); n = sum(len(f(m)) for m in sample); res[name] = n
    print(f"{name:22s} {n:>12,}  ratio {raw / n:6.2f}  {time.time() - t:6.1f}s", flush=True)
run("gzip-6 (zlib)", lambda m: zlib.compress(m, 6))
run("zstd-3", zstd.ZstdCompressor(level=3).compress)
run("zstd-19", zstd.ZstdCompressor(level=19).compress)
d = zstd.train_dictionary(112640, train)
run("zstd-19 + 110KB dict", zstd.ZstdCompressor(level=19, dict_data=d).compress)
run("brotli-11", lambda m: brotli.compress(m, quality=11))
print(json.dumps(res))
