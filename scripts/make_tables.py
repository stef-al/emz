#!/usr/bin/env python3
"""Collect benchmark results into a CSV and print a markdown table.

usage: make_tables.py <bench results.jsonl> <out.csv> LABEL=SIZE,C_TIME,D_TIME [...]

SIZE is an archive file or its size in bytes.  C_TIME and D_TIME describe the
compress and decompress runs: either a file written by
/usr/bin/time -f "%e %U %S %M", the same four numbers joined by '/', or '-'
when the run was not timed.
"""
import csv, json, os, sys

def timing(spec):
    if spec == "-":
        return None, None, None
    parts = open(spec).read().split() if os.path.isfile(spec) else spec.split("/")
    wall, user, sys_, rss_kb = (float(x) for x in parts[:4])
    return wall, user + sys_, int(rss_kb) // 1024

def fmt(x):
    return "" if x is None else f"{x:.0f}"

res_f, out_f = sys.argv[1:3]
rows = [json.loads(line) for line in open(res_f)]
best = {r["config"]: r for r in rows if "compressed_bytes" in r and r.get("roundtrip_ok")}
for spec in sys.argv[3:]:
    label, _, rest = spec.partition("=")
    size, ct, dt = rest.split(",")
    size = os.path.getsize(size) if os.path.isfile(size) else int(size)
    cw, cc, cm = timing(ct); dw, dc, dm = timing(dt)
    best[label] = {"config": label, "input_bytes": rows[0]["input_bytes"], "compressed_bytes": size,
                   "c_wall": cw, "c_cpu": cc, "c_mem_mb": cm, "d_wall": dw, "d_cpu": dc, "d_mem_mb": dm,
                   "roundtrip_ok": True}
out = sorted(best.values(), key=lambda r: -r["compressed_bytes"])
with open(out_f, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["compressor", "compressed_bytes", "ratio", "bits_per_byte", "compress_wall_s", "compress_cpu_s",
                "compress_peak_MB", "decompress_wall_s", "decompress_peak_MB", "roundtrip_sha256_ok"])
    for r in out:
        w.writerow([r["config"], r["compressed_bytes"], f"{r['input_bytes'] / r['compressed_bytes']:.2f}",
                    f"{8 * r['compressed_bytes'] / r['input_bytes']:.4f}", fmt(r["c_wall"]), fmt(r["c_cpu"]),
                    fmt(r["c_mem_mb"]), fmt(r["d_wall"]), fmt(r["d_mem_mb"]), r["roundtrip_ok"]])
for r in out:
    t = (f"| {r['c_wall'] / 60:.1f} | {r['d_wall'] / 60:.1f} | {max(r['c_mem_mb'], r['d_mem_mb']) / 1024:.1f} |"
         if r["c_wall"] is not None else "| | | |")
    print(f"| {r['config']} | {r['compressed_bytes'] / 1e6:.2f} | {r['input_bytes'] / r['compressed_bytes']:.1f}× {t}")
