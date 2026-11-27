#!/usr/bin/env python3
"""Run a compressor config on a file: compress, decompress, verify, record.

usage: bench.py <input> <workdir> <results.jsonl> <config> [<config> ...]
"""
import json, os, subprocess, sys, hashlib, shutil, time

BSC = os.environ.get("BSC", "bsc")          # libbsc command-line tool
PAQ8PX = os.environ.get("PAQ8PX", "paq8px")  # paq8px binary

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 24)
            if not b: break
            h.update(b)
    return h.hexdigest()

# name: (compress cmd, decompress cmd, compressed file suffix)
CONFIGS = {
 "gzip-9":      ("gzip -9 -c {i} > {c}", "gzip -dc {c} > {d}", ".gz"),
 "bzip2-9":     ("bzip2 -9 -c {i} > {c}", "bzip2 -dc {c} > {d}", ".bz2"),
 "zstd-19":     ("zstd -19 -T1 -q -f {i} -o {c}", "zstd -d -q -f {c} -o {d}", ".zst"),
 "zstd-22-long31": ("zstd --ultra -22 --long=31 -T1 -q -f {i} -o {c}",
                    "zstd -d --long=31 -q -f {c} -o {d}", ".zst"),
 "xz-9e":       ("xz -9e -T1 -c {i} > {c}", "xz -dc {c} > {d}", ".xz"),
 "xz-9e-d256m": ("xz --lzma2=preset=9e,dict=256MiB -T1 -c {i} > {c}", "xz -dc {c} > {d}", ".xz"),
 "brotli-11-w24": ("brotli -q 11 -w 24 -c {i} > {c}", "brotli -dc {c} > {d}", ".br"),
 "ppmd-o16-1g": ("7z a -bd -bso0 -t7z -m0=PPMd:mem=1g:o=16 -mmt=1 {c} {i}",
                 "7z e -bd -so {c} > {d}", ".7z"),
 "ppmd-o32-1g": ("7z a -bd -bso0 -t7z -m0=PPMd:mem=1g:o=32 -mmt=1 {c} {i}",
                 "7z e -bd -so {c} > {d}", ".7z"),
 "bsc-b512-e2": (BSC + " e {i} {c} -b512 -e2", BSC + " d {c} {d}", ".bsc"),
 "zpaq-m5":     ("zpaq a {c} {i} -m5 -t2", "cd {dd} && zpaq x {c} -t2 -to x && find x -type f -exec mv {{}} {d} \\;", ".zpaq"),
 "zpaq-m4":     ("zpaq a {c} {i} -m4 -t2", "cd {dd} && zpaq x {c} -t2 -to x && find x -type f -exec mv {{}} {d} \\;", ".zpaq"),
 "lrzip-lzma":  ("lrzip -q -w 15 -o {c} {i}", "lrzip -d -q -o {d} {c}", ".lrz"),
 "lrzip-zpaq":  ("lrzip -z -q -w 15 -o {c} {i}", "lrzip -d -q -o {d} {c}", ".lrz"),
 "paq8px-8":    (PAQ8PX + " -8 {i} {c}", PAQ8PX + " -d {c} {d}", ".paq8px"),
}

def timed(cmd):
    tf = "/tmp/bench_time.txt"
    full = f"/usr/bin/time -o {tf} -f '%e %U %S %M' bash -c {json.dumps(cmd)}"
    r = subprocess.run(full, shell=True)
    if r.returncode != 0:
        raise RuntimeError(f"failed ({r.returncode}): {cmd}")
    e, u, s, m = open(tf).read().split()[-4:]
    return float(e), float(u) + float(s), int(m) // 1024

def main():
    inp, wd, res = sys.argv[1], sys.argv[2], sys.argv[3]
    insize = os.path.getsize(inp)
    insha = sha(inp)
    for name in sys.argv[4:]:
        ccmd, dcmd, suf = CONFIGS[name]
        d = os.path.join(wd, name)
        shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
        c = os.path.join(d, "out" + suf); dec = os.path.join(d, "dec")
        rec = {"config": name, "input": inp, "input_bytes": insize}
        try:
            rec["c_wall"], rec["c_cpu"], rec["c_mem_mb"] = timed(ccmd.format(i=inp, c=c, d=dec, dd=d))
            rec["compressed_bytes"] = os.path.getsize(c)
            rec["d_wall"], rec["d_cpu"], rec["d_mem_mb"] = timed(dcmd.format(i=inp, c=c, d=dec, dd=d))
            rec["roundtrip_ok"] = (sha(dec) == insha)
            rec["ratio"] = insize / rec["compressed_bytes"]
            rec["bpc"] = 8 * rec["compressed_bytes"] / insize
        except Exception as ex:
            rec["error"] = str(ex)
        rec["finished"] = time.strftime("%H:%M:%S")
        with open(res, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)
        if os.path.exists(dec): os.remove(dec)
        shutil.rmtree(os.path.join(d, "x"), ignore_errors=True)

main()
