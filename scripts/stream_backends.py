#!/usr/bin/env python3
"""Ablation: compress emz transform streams with standard back ends instead of emcm.

usage: stream_backends.py <streams-dir (from EMZ_KEEP)> <backend> [<backend> ...]
backends: xz-9e zstd-19-long bsc-e2 ppmd-o16  (bsc path via $BSC)
"""
import os, sys, subprocess, json, glob
d = sys.argv[1]
B = {
  "xz-9e":   "xz -9e -T1 -c {f} | wc -c",
  "zstd-19-long": "zstd -19 --long=30 -T1 -c -q {f} | wc -c",
  "bsc-e2":  os.environ.get("BSC", "bsc") + " e {f} /tmp/sb.bsc -b512 -e2 >/dev/null && stat -c %s /tmp/sb.bsc",
  "ppmd-o16": "rm -f /tmp/sb.7z && 7z a -bd -bso0 -m0=PPMd:mem=1g:o=16 -mmt=1 /tmp/sb.7z {f} && stat -c %s /tmp/sb.7z",
}
res = {}
for name in sys.argv[2:]:
    tot = 0; per = {}
    for f in sorted(glob.glob(os.path.join(d, "*.raw"))):
        if os.path.getsize(f) == 0: continue
        n = int(subprocess.run(B[name].format(f=f), shell=True, capture_output=True, text=True, check=True).stdout.split()[-1])
        per[os.path.basename(f)] = n; tot += n
    res[name] = tot
    print(json.dumps({"backend": name, "total": tot, "body": per.get("body.raw")}), flush=True)
