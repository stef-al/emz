#!/usr/bin/env python3
"""Per-message ("each e-mail compressed separately") analysis.

usage: permsg_analysis.py <enron.corpus> <outdir> [experiment ...]
experiments: dicts split loo transform cmprep sis parent blocks   (default: all)

Sets (deterministic):  test S10 = every 10th message (i % 10 == 0),
                       test S1  = every 100th message (i % 100 == 0),
                       training = i % 10 == 5 (disjoint from both test sets).
Every model, dictionary and static table is built from the training set only.

`transform` measures a per-message version of the emz field models: each
message is rewritten into a compact representation that a decoder holding
the same static tables (address book, address->name map, per-folder values,
MIME combinations, Message-ID generator anchors) could invert, and that
representation is then compressed on its own with zstd + dictionary.  It is a
size measurement; the inverse transform is not implemented here.
`cmprep` writes training/test files for the primed-CM driver
(scripts/emcm_permsg.c), which is run separately.
`parent` codes each test message against one earlier message of the same
thread (same subject without Re:/Fw: prefixes), used as a zstd raw-content
dictionary; random access then needs that one parent message as well.
"""
import os, re, sys, json, time, struct, hashlib, collections, bisect
import zstandard as zstd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from emz import (iter_corpus, Codec, FIELDS, parse_date, split_addrs, split_xlist,
                 split_xlist_angle, MID_RE)
from qp import qp_decode, qp_encode


PERCOPY = (b"Message-ID", b"X-Folder", b"X-Origin", b"X-FileName")
LOG = None

def log(**kw):
    kw["time"] = time.strftime("%H:%M:%S"); LOG.write(json.dumps(kw) + "\n"); LOG.flush(); print(json.dumps(kw), flush=True)

def zsize(msgs, level=19, d=None):
    c = zstd.ZstdCompressor(level=level, dict_data=d) if d else zstd.ZstdCompressor(level=level)
    return sum(len(c.compress(m)) for m in msgs)

def train(msgs, size):
    return zstd.train_dictionary(size, msgs[:40000], threads=2)

def newest(msgs, limit):
    """Concatenate the newest messages (end of the list) up to `limit` bytes."""
    take, n = [], 0
    for m in reversed(msgs):
        if n + len(m) > limit: break
        take.append(m); n += len(m)
    return b"".join(reversed(take))

def split_hb(m):
    i = m.find(b"\r\n\r\n")
    return (m, b"") if i < 0 else (m[:i + 4], m[i + 4:])

PM_M, PM_A, MASK = 2**31 - 1, 16807, (1 << 25) - 1
def vint(v):
    b = bytearray()
    while v >= 0x80: b.append((v & 0x7f) | 0x80); v >>= 7
    b.append(v); return bytes(b)
def zz(v): return (v << 1) if v >= 0 else ((-v << 1) - 1)

def build_tables(train):
    addr_cnt = collections.Counter(); name_cnt = collections.defaultdict(collections.Counter)
    fold_cnt = collections.defaultdict(collections.Counter); mime_cnt = collections.Counter()
    chains = collections.defaultdict(list)
    for i, path, m in train:
        p = Codec.parse(m)
        if p is None: continue
        F, has_to, has_cc, body = p
        folder, _, fn = path.rpartition("/")
        fold_cnt[folder][(F[b"X-Folder"], F[b"X-Origin"], F[b"X-FileName"])] += 1
        mime_cnt[(F[b"Mime-Version"], F[b"Content-Type"], F[b"Content-Transfer-Encoding"])] += 1
        for af, xf in ((b"From", b"X-From"), (b"To", b"X-To"), (b"Cc", b"X-cc")):
            if af not in F: continue
            a = split_addrs(F[af])
            if a is None: continue
            addr_cnt.update(a)
            for sp in (split_xlist, split_xlist_angle):
                e = sp(F[xf])
                if len(e) == len(a):
                    for x, y in zip(a, e): name_cnt[x][y] += 1
                    break
        mm = MID_RE.match(F[b"Message-ID"])
        if mm and fn.rstrip(".").isdigit():
            chains[folder].append((int(fn.rstrip(".")), int(mm.group(1)), int(mm.group(2))))
    addr_id = {a: k for k, (a, _) in enumerate(addr_cnt.most_common())}
    name_of = {a: c.most_common(1)[0][0] for a, c in name_cnt.items()}
    fold = {f: c.most_common(1)[0][0] for f, c in fold_cnt.items()}
    mime = {t: k for k, (t, _) in enumerate(mime_cnt.most_common())}
    # Park-Miller anchors: full 31-bit state of a training message, recovered
    # from the next training message of the same folder
    anchors = {}
    for f, ch in chains.items():
        ch.sort(); lst = []
        for (n1, a, t1), (n2, b, _) in zip(ch, ch[1:]):
            step = pow(PM_A, n2 - n1, PM_M)
            for h in range(64):
                x = (h << 25) | a
                if 0 < x < PM_M and (step * x) % PM_M & MASK == b:
                    lst.append((n1, x, t1)); break
        if lst: anchors[f] = lst
    size = (sum(len(a) + 1 for a in addr_id) + sum(len(a) + len(n) + 2 for a, n in name_of.items())
            + sum(len(f) + sum(map(len, v)) + 4 for f, v in fold.items()) + 16 * sum(map(len, anchors.values())))
    return dict(addr_id=addr_id, name_of=name_of, fold=fold, mime=mime, anchors=anchors, raw_size=size)

def enc_addr(a, T):
    k = T["addr_id"].get(a)
    return vint(k + 1) if k is not None else b"\x00" + a + b"\n"

def enc_list(v, T):
    a = split_addrs(v)
    if a is None: return b"\x00" + v.replace(b"\r\n", b"\x02") + b"\n"
    return vint(len(a)) + b"".join(enc_addr(x, T) for x in a)

def enc_names(v, addrv, T):
    a = split_addrs(addrv) if addrv else None
    if a is not None:
        for mode, sp in ((1, split_xlist), (2, split_xlist_angle)):
            e = sp(v)
            if len(e) == len(a):
                out = bytearray([mode])
                for x, y in zip(a, e):
                    out += b"\x00" if T["name_of"].get(x) == y else b"\x01" + y + b"\n"
                return bytes(out)
    return b"\x00" + v + b"\n"

def enc_mid(path, F, T, stats=None):
    """-> (bytes, flag bits) for the Message-ID in random-access form."""
    folder, _, fn = path.rpartition("/")
    mm = MID_RE.match(F[b"Message-ID"])
    if not (mm and fn.rstrip(".").isdigit()):
        return F[b"Message-ID"] + b"\n", 0
    n = int(fn.rstrip(".")); n1, n2 = int(mm.group(1)), int(mm.group(2))
    an = T["anchors"].get(folder); hit = False; t2 = 0
    if an:
        k = bisect.bisect_right(an, (n, 1 << 62)) - 1
        if k >= 0:
            a0, x0, t0 = an[k]
            hit = (pow(PM_A, n - a0, PM_M) * x0 % PM_M) & MASK == n1
            t2 = t0 + 23 * (n - a0)
    if stats is not None: stats["mid_hit"] += hit; stats["mid"] += 1
    return (b"" if hit else struct.pack(">I", n1)) + vint(zz(n2 - t2)), (1 << 5) | (hit << 6)

def transform(path, m, T, stats=None):
    p = Codec.parse(m)
    if p is None: return b"\xff" + m
    F, has_to, has_cc, body = p
    folder, _, fn = path.rpartition("/")
    r = bytearray()
    bcc_eq = has_cc and F[b"Bcc"] == F[b"Cc"]
    mt = (F[b"Mime-Version"], F[b"Content-Type"], F[b"Content-Transfer-Encoding"])
    fold_hit = T["fold"].get(folder) == (F[b"X-Folder"], F[b"X-Origin"], F[b"X-FileName"])
    flags = has_to | has_cc << 1 | bcc_eq << 2 | fold_hit << 4
    mid, mbits = enc_mid(path, F, T, stats)
    r.append(flags | mbits); r += mid
    dp = parse_date(F[b"Date"])
    r += (b"\x01" + struct.pack(">i", dp[0]) + bytes([dp[1] + 64 * dp[2]])) if dp else b"\x00" + F[b"Date"] + b"\n"
    r += enc_list(F[b"From"], T)
    if has_to: r += enc_list(F[b"To"], T)
    r += F[b"Subject"].replace(b"\r\n", b"\x02") + b"\n"
    if has_cc:
        r += enc_list(F[b"Cc"], T)
        if not bcc_eq: r += enc_list(F[b"Bcc"], T)
    r += bytes([T["mime"][mt]]) if T["mime"].get(mt, 255) < 255 else b"\xff" + b"\n".join(mt) + b"\n"
    r += enc_names(F[b"X-From"], F[b"From"], T)
    r += enc_names(F[b"X-To"], F.get(b"To"), T)
    r += enc_names(F[b"X-cc"], F.get(b"Cc"), T)
    r += F[b"X-bcc"] + b"\n"
    if not fold_hit: r += b"\n".join((F[b"X-Folder"], F[b"X-Origin"], F[b"X-FileName"])) + b"\n"
    # body: newline normalisation and quoted-printable decoding, as in emz
    if b"\r" not in body: mode, b = 0, body
    else:
        t = qp_decode(body) if b"=" in body else None
        if t is not None and qp_encode(t) == body: mode, b = 3, t
        elif b"\r" not in body.replace(b"\r\n", b"") and b"\n" not in body.replace(b"\r\n", b""):
            mode, b = 1, body.replace(b"\r\n", b"\n")
        else: mode, b = 2, body
    r.append(mode); r += b
    return bytes(r)


def main():
    global LOG
    corpus, out = sys.argv[1], sys.argv[2]
    todo = set(sys.argv[3:]) or {"dicts", "split", "loo", "transform", "cmprep", "sis", "parent", "blocks"}
    os.makedirs(out, exist_ok=True)
    LOG = open(os.path.join(out, "permsg_results.jsonl"), "a")
    S10, S1, TR = [], [], []
    ckey = []                     # content key of every message (for SIS)
    tkey = []                     # thread key: subject without Re:/Fw: prefixes
    PFX = re.compile(rb"^\s*((re|fw|fwd)\s*(\[\d+\])?\s*:\s*)+", re.I)
    for i, (path, msg) in enumerate(iter_corpus(corpus)):
        p = Codec.parse(msg)
        tkey.append(PFX.sub(b"", p[0][b"Subject"]).strip().lower() if p is not None else b"")
        if p is not None:
            F, _, _, body = p
            ckey.append(hashlib.md5(b"\0".join(F.get(f, b"<none>") for f in FIELDS if f not in PERCOPY) + b"\0" + body).digest())
        else:
            ckey.append(hashlib.md5(msg).digest())
        if i % 10 == 0:
            S10.append((i, path, msg))
            if i % 100 == 0: S1.append((i, path, msg))
        elif i % 10 == 5:
            TR.append((i, path, msg))
    raw10 = sum(len(m) for _, _, m in S10); raw1 = sum(len(m) for _, _, m in S1)
    log(stage="sets", S10=len(S10), S10_bytes=raw10, S1=len(S1), S1_bytes=raw1, train=len(TR))

    # ------------------------------------------------------------------ dicts
    if "dicts" in todo:
        trm = [m for _, _, m in TR]
        for size in (112640, 1 << 20):
            t = time.time(); d = train(trm, size)
            n = zsize([m for _, _, m in S10], 19, d)
            log(stage="dicts", method=f"zstd-19 + trained dict {size // 1024} KB", bytes=n, ratio=raw10 / n, train_s=round(time.time() - t))
        # raw-content dictionaries (concatenated training messages) on S1, with an
        # S1 baseline for comparison
        d = train(trm, 112640); n = zsize([m for _, _, m in S1], 19, d)
        log(stage="dicts", set="S1", method="zstd-19 + trained dict 110 KB", bytes=n, ratio=raw1 / n)
        for size in (8 << 20, 32 << 20):
            d = zstd.ZstdCompressionDict(newest(trm, size), dict_type=zstd.DICT_TYPE_RAWCONTENT)
            p = zstd.ZstdCompressionParameters.from_level(19, window_log=26)
            d.precompute_compress(compression_params=p)
            c = zstd.ZstdCompressor(compression_params=p, dict_data=d)
            t = time.time(); n = sum(len(c.compress(m)) for _, _, m in S1)
            log(stage="dicts", set="S1", method=f"zstd-19 + raw-content dict {size >> 20} MB", bytes=n, ratio=raw1 / n, secs=round(time.time() - t))

    # ------------------------------------------------------------------ split
    d_h = d_b = None
    if "split" in todo or "loo" in todo:
        th = [split_hb(m)[0] for _, _, m in TR]; tb = [split_hb(m)[1] for _, _, m in TR]
        d_h = train(th, 112640); d_b = train([b for b in tb if b], 112640)
    if "split" in todo:
        hs = [split_hb(m)[0] for _, _, m in S10]; bs = [split_hb(m)[1] for _, _, m in S10]
        nh = zsize(hs, 19, d_h); nb = zsize(bs, 19, d_b)
        log(stage="split", header_raw=sum(map(len, hs)), header_bytes=nh, body_raw=sum(map(len, bs)), body_bytes=nb,
            header_share=nh / (nh + nb), per_msg_header=nh / len(hs), per_msg_body=nb / len(bs))

    # ------------------------------------------------------------------ leave-one-field-out (header)
    if "loo" in todo:
        c = zstd.ZstdCompressor(level=19, dict_data=d_h)
        base = 0; drop = collections.Counter()
        for _, _, m in S1:
            p = Codec.parse(m)
            if p is None: continue
            F = p[0]; lines = [f + b": " + F[f] for f in FIELDS if f in F]
            full = len(c.compress(b"\r\n".join(lines) + b"\r\n\r\n")); base += full
            for k, f in enumerate([f for f in FIELDS if f in F]):
                drop[f.decode()] += full - len(c.compress(b"\r\n".join(lines[:k] + lines[k + 1:]) + b"\r\n\r\n"))
        log(stage="loo", header_bytes=base, marginal_bytes=dict(drop.most_common()))

    # ------------------------------------------------------------------ static tables + transform
    T = None
    if "transform" in todo or "cmprep" in todo or "sis" in todo:
        t = time.time(); T = build_tables(TR)
        log(stage="tables", addresses=len(T["addr_id"]), names=len(T["name_of"]), folders=len(T["fold"]),
            anchors=sum(map(len, T["anchors"].values())), raw_table_bytes=T["raw_size"], secs=round(time.time() - t))

    if "transform" in todo:
        st = collections.Counter()
        trR = [transform(p, m, T) for _, p, m in TR]
        teR = [transform(p, m, T, st) for _, p, m in S10]
        log(stage="transform", raw_bytes=raw10, transformed_bytes=sum(map(len, teR)), mid_hit_rate=st["mid_hit"] / max(1, st["mid"]))
        for size in (112640, 1 << 20):
            d = train(trR, size)
            n = zsize(teR, 19, d)
            log(stage="transform", method=f"field transform + zstd-19 + dict {size // 1024} KB", bytes=n, ratio=raw10 / n)

    if "cmprep" in todo:
        # files for scripts/emcm_permsg.c: prime data (training) and test messages (S1)
        def dump(name, items):
            with open(os.path.join(out, name), "wb") as f:
                for p, m in items: f.write(p.encode("latin-1") + b"\n" + str(len(m)).encode() + b"\n" + m)
        def prime(name, msgs, limit):
            with open(os.path.join(out, name), "wb") as f: f.write(newest(msgs, limit))
        dump("S1_raw.corpus", [(p, m) for _, p, m in S1])
        dump("S1_tr.corpus", [(p, transform(p, m, T)) for _, p, m in S1])
        trm = [m for _, _, m in TR]; trR = [transform(p, m, T) for _, p, m in TR]
        for mb in (8, 32):
            prime(f"prime_raw_{mb}MB.bin", trm, mb << 20)
            prime(f"prime_tr_{mb}MB.bin", trR, mb << 20)
        d = train(trm, 112640); n = zsize([m for _, _, m in S1], 19, d)
        log(stage="cmprep", note="S1 baseline for the CM runs", method="zstd-19 + trained dict 110 KB", bytes=n, ratio=raw1 / n)
        d = train(trR, 112640); n = zsize([transform(p, m, T) for _, p, m in S1], 19, d)
        log(stage="cmprep", note="S1 baseline for the CM runs", method="field transform + zstd-19 + dict 110 KB", bytes=n, ratio=raw1 / n)

    # ------------------------------------------------------------------ single-instance storage
    if "sis" in todo:
        # A copy whose content was stored earlier (archive order) is kept as a
        # reference (4 bytes) plus its per-copy fields in the transformed form.
        first = {}
        for i, k in enumerate(ckey): first.setdefault(k, i)
        trR = [transform(p, m, T) for _, p, m in TR]
        d = train(trR, 112640); c = zstd.ZstdCompressor(level=19, dict_data=d)
        c0 = zstd.ZstdCompressor(level=19, dict_data=train([m for _, _, m in TR], 112640))
        tot_t = tot_z = 0; copies = 0
        for i, path, m in S10:
            p = Codec.parse(m)
            if first[ckey[i]] < i and p is not None:
                # a copy: 4-byte content reference + its own per-copy fields
                copies += 1; F = p[0]
                pc = (F[b"X-Folder"], F[b"X-Origin"], F[b"X-FileName"])
                mid, _ = enc_mid(path, F, T)
                per_t = 1 + len(mid) + (0 if T["fold"].get(path.rpartition("/")[0]) == pc else sum(len(x) + 1 for x in pc))
                per_z = len(c0.compress(b"\r\n".join(f + b": " + F[f] for f in PERCOPY)))
                tot_t += 4 + per_t; tot_z += 4 + per_z
            else:
                tot_t += len(c.compress(transform(path, m, T))); tot_z += len(c0.compress(m))
        log(stage="sis", copies_in_sample=copies, sample=len(S10),
            sis_plus_zstd_dict=raw10 / tot_z, sis_plus_transform_zstd_dict=raw10 / tot_t,
            bytes_zstd=tot_z, bytes_transform=tot_t)

    # ------------------------------------------------------------------ thread parent as reference
    if "parent" in todo:
        # random access to any message of the corpus
        offs = []
        with open(corpus, "rb") as f:
            pos = 0
            while True:
                pth = f.readline()
                if not pth: break
                ln = f.readline(); n = int(ln); pos += len(pth) + len(ln)
                offs.append((pos, n)); f.seek(n, 1); pos += n
        fh = open(corpus, "rb")
        def get(i):
            fh.seek(offs[i][0]); return fh.read(offs[i][1])
        last = {}                                  # thread key -> latest message index with new content
        seen_content = set(); parent = {}
        want = {i for i, _, _ in S10}
        for i in range(len(ckey)):
            k = tkey[i]
            if i in want and k and k in last: parent[i] = last[k]
            if k and ckey[i] not in seen_content: last[k] = i
            seen_content.add(ckey[i])
        d = train([m for _, _, m in TR], 112640); c0 = zstd.ZstdCompressor(level=19, dict_data=d)
        p19 = zstd.ZstdCompressionParameters.from_level(19)
        first = {}
        for j, k in enumerate(ckey): first.setdefault(k, j)
        tot_base = tot_par = tot_sis = tot_both = 0; used = 0; copies = 0
        for i, path, m in S10:
            base = len(c0.compress(m)); best = base
            if i in parent:
                ref = zstd.ZstdCompressionDict(get(parent[i]), dict_type=zstd.DICT_TYPE_RAWCONTENT)
                alt = len(zstd.ZstdCompressor(compression_params=p19, dict_data=ref).compress(m)) + 4
                if alt < best: best = alt
            p = Codec.parse(m)
            if first[ckey[i]] < i and p is not None:       # an exact copy: reference + per-copy fields
                copies += 1
                sis = 4 + len(c0.compress(b"\r\n".join(f + b": " + p[0][f] for f in PERCOPY)))
                tot_sis += sis; tot_both += sis
            else:
                tot_sis += base; tot_both += best + 1; used += best < base
            tot_base += base; tot_par += best + 1
        log(stage="parent", messages=len(S10), copies=copies, with_parent_candidate=len(parent), parent_used_noncopies=used,
            ratio_dict_only=raw10 / tot_base, ratio_dict_or_parent=raw10 / tot_par,
            ratio_sis=raw10 / tot_sis, ratio_sis_plus_parent=raw10 / tot_both)

    # ------------------------------------------------------------------ blocks of consecutive messages
    if "blocks" in todo:
        d = train([m for _, _, m in TR], 112640)
        c = zstd.ZstdCompressor(level=19, dict_data=d)
        seq = []
        for i, (path, m) in enumerate(iter_corpus(corpus)):
            if i % 10 == 5: continue                     # training messages
            seq.append(m)
            if len(seq) >= 40000: break
        raw = sum(map(len, seq))
        for n in (1, 4, 16, 64, 256, 1024):
            tot = sum(len(c.compress(b"".join(seq[k:k + n]))) for k in range(0, len(seq), n))
            log(stage="blocks", block_messages=n, ratio=raw / tot, bytes=tot, raw=raw)


if __name__ == "__main__":
    main()
