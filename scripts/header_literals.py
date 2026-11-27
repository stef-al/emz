#!/usr/bin/env python3
"""Which header fields the remaining header bytes come from.

usage: header_literals.py <corpus> [top_n]

Runs the emz transform and attributes the growth of the literal streams
(hdr_l: unpredicted header text; sig_l: base64 runs of signature fields) to
the field being coded.  Each field's share is then coded on its own with
`emcm` (EMCM environment variable, default ./emcm) to estimate what it costs.
"""
import collections, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import emz  # noqa: E402

EMCM = os.environ.get("EMCM", os.path.join(ROOT, "emcm"))
STREAMS = ("hdr_l", "sig_l")


def group(ln):
    for p in (b"x-ms-", b"x-microsoft-", b"x-forefront-"):
        if ln.startswith(p):
            return b"x-ms-* / x-microsoft-* / x-forefront-*"
    return ln


def main():
    corpus = sys.argv[1]
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    S = {n: emz.WStream() for n in emz.STREAMS}
    C = emz.Codec(True, S)
    parts = {n: collections.defaultdict(bytearray) for n in STREAMS}
    count = collections.Counter()

    def wrap(fn):
        def run(self, ln, *a):
            before = {n: len(S[n].b) for n in STREAMS}
            r = fn(self, ln, *a)
            g = group(ln)
            for n in STREAMS:
                parts[n][g] += S[n].b[before[n]:]
            count[g] += 1
            return r
        return run
    emz.Codec.code_field = wrap(emz.Codec.code_field)
    emz.Codec.code_generic = wrap(emz.Codec.code_generic)
    for path, msg in emz.iter_corpus(corpus):
        C.code_message(path, msg)

    with tempfile.TemporaryDirectory() as d:
        for n in STREAMS:
            ranked = sorted(parts[n].items(), key=lambda kv: -len(kv[1]))
            rest = bytearray()
            for _, b in ranked[top:]:
                rest += b
            rows = ranked[:top] + ([(b"(other fields)", rest)] if rest else [])
            total_raw = sum(len(b) for _, b in rows)
            out = []
            for name, b in rows:
                src, dst = os.path.join(d, "x.raw"), os.path.join(d, "x.emc")
                with open(src, "wb") as f:
                    f.write(b)
                subprocess.run([EMCM, "c", src, dst], check=True, stderr=subprocess.DEVNULL)
                out.append((name, len(b), os.path.getsize(dst)))
            total_coded = sum(c for _, _, c in out)
            print(f"== {n}: {total_raw:,} bytes, {total_coded:,} coded field by field")
            print(f"{'field':42s} {'fields':>8s} {'raw':>12s} {'coded':>11s} {'share':>6s}")
            for name, raw, coded in sorted(out, key=lambda r: -r[2]):
                print(f"{name.decode():42s} {count.get(name, 0):8,} {raw:12,} {coded:11,} {100 * coded / total_coded:5.1f}%")


if __name__ == "__main__":
    main()
