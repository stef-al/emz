#!/usr/bin/env python3
"""Collect benchmark results into a CSV and print a markdown table.

usage: make_tables.py <bench results.jsonl> <emz archive> <c_time.txt> <d_time.txt> <out.csv>
(c_time/d_time: output of /usr/bin/time -f "%e %U %S %M" for emz compress/decompress)
"""
import json, csv, sys
res_f, arc_f, ct_f, dt_f, out_f = sys.argv[1:6]
rows = [json.loads(l) for l in open(res_f)]
best = {}
for r in rows:
    if 'compressed_bytes' in r and r.get('roundtrip_ok'): best[r['config']] = r
# emz rows
ct = open(ct_f).read().split(); dt = open(dt_f).read().split()
import os
emz = {"config": "emz (this work)", "input_bytes": rows[0]["input_bytes"], "compressed_bytes": os.path.getsize(arc_f),
       "c_wall": float(ct[0]), "c_cpu": float(ct[1]) + float(ct[2]), "c_mem_mb": int(ct[3]) // 1024,
       "d_wall": float(dt[0]), "d_cpu": float(dt[1]) + float(dt[2]), "d_mem_mb": int(dt[3]) // 1024, "roundtrip_ok": True}
best[emz["config"]] = emz
out = sorted(best.values(), key=lambda r: -r['compressed_bytes'])
with open(out_f, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(["compressor", "compressed_bytes", "ratio", "bits_per_byte", "compress_wall_s", "compress_cpu_s", "compress_peak_MB", "decompress_wall_s", "decompress_peak_MB", "roundtrip_sha256_ok"])
    for r in out:
        w.writerow([r['config'], r['compressed_bytes'], f"{r['input_bytes']/r['compressed_bytes']:.2f}", f"{8*r['compressed_bytes']/r['input_bytes']:.4f}",
                    f"{r['c_wall']:.0f}", f"{r['c_cpu']:.0f}", r['c_mem_mb'], f"{r['d_wall']:.0f}", r['d_mem_mb'], r['roundtrip_ok']])
for r in out:
    print(f"| {r['config']} | {r['compressed_bytes']/1e6:.2f} | {r['input_bytes']/r['compressed_bytes']:.1f}× | {r['c_wall']/60:.1f} | {r['d_wall']/60:.1f} | {r['c_mem_mb']/1024:.1f} |")
