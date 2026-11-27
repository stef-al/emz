"""Outlook .msg support for emz.

A .msg file is a Compound File (MS-CFB): a small FAT file system whose
streams hold the message's MAPI properties (MS-OXMSG).  emz splits it into

* the *skeleton*: header, DIFAT, FAT, directory and mini FAT, coded field by
  field (chain links as "next sector", directory entries as columns, start
  sectors against allocation order), plus every other byte that is not
  stream content (sector slack, free sectors) in file order;
* the *streams*, which emz codes with models chosen by property tag.

Compressed RTF bodies (MS-OXRTFCP, "LZFu") are stored decompressed.  The
compressed stream is rebuilt from the text and one choice byte per token:
0 when the token is what Outlook's own match finder (an LZSS binary search
tree, `_lzfu_predict`) produces, otherwise an explicit description of the
token, so streams from any writer are handled.
"""
import codecs, re, struct, zlib

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF


class CFBError(Exception):
    pass


class Compound:
    """Structure of a compound file.  Raises CFBError for anything unusual
    (bad chains, overlapping sectors), so the caller can keep the file as it
    is.  `streams`: (entry index, path, size, segments) in entry order,
    segments being (file offset, length) pairs that hold the stream's bytes."""

    def __init__(self, data):
        if len(data) < 512 or data[:8] != CFB_MAGIC:
            raise CFBError("not a compound file")
        self.length = len(data)
        self.major, bo, sshift, mshift = struct.unpack_from("<HHHH", data, 26)
        if bo != 0xFFFE or sshift not in (9, 12) or mshift != 6:
            raise CFBError("unsupported header")
        self.ssz = ssz = 1 << sshift
        self.n_fat, self.dir_start = struct.unpack_from("<II", data, 44)
        self.cutoff, self.mf_start, self.n_mf, self.dif_start, self.n_dif = struct.unpack_from("<IIIII", data, 56)
        self.nsec = nsec = (len(data) - ssz) // ssz
        if nsec <= 0 or self.n_fat > nsec or self.n_dif > nsec or self.n_mf > nsec or not 64 <= self.cutoff <= 1 << 20:
            raise CFBError("bad sizes")
        claimed = set()

        def claim(i):
            if not 0 <= i < nsec or i in claimed:
                raise CFBError("bad structural sector")
            claimed.add(i)
            return (i + 1) * ssz

        self.difat_secs = []
        difat = list(struct.unpack_from("<109I", data, 76))
        s = self.dif_start
        for _ in range(self.n_dif):
            o = claim(s); self.difat_secs.append(s)
            difat += struct.unpack_from("<%dI" % (ssz // 4 - 1), data, o)
            s = struct.unpack_from("<I", data, o + ssz - 4)[0]
        self.fat_secs = difat[:self.n_fat]
        if len(self.fat_secs) < self.n_fat:
            raise CFBError("short DIFAT")
        fat = []
        for i in self.fat_secs:
            fat += struct.unpack_from("<%dI" % (ssz // 4), data, claim(i))
        self.fat = fat
        self.dir_secs = self.chain(self.dir_start, fat, nsec)
        for i in self.dir_secs:
            claim(i)
        dirdata = b"".join(data[(i + 1) * ssz:(i + 2) * ssz] for i in self.dir_secs)
        self.entries = [dirdata[o:o + 128] for o in range(0, len(dirdata), 128)]
        if not self.entries or self.entries[0][66] != 5:
            raise CFBError("no root entry")
        self.mf_secs = self.chain(self.mf_start, fat, nsec)[:self.n_mf] if self.n_mf else []
        if len(self.mf_secs) < self.n_mf:
            raise CFBError("short mini FAT chain")
        minifat = []
        for i in self.mf_secs:
            minifat += struct.unpack_from("<%dI" % (ssz // 4), data, claim(i))
        self.minifat = minifat
        root = self.entry(0)
        ministream = self.chain(root[5], fat, nsec) if root[6] else []

        paths = {}
        stack = [(root[4], "")]
        while stack:
            idx, prefix = stack.pop()
            if idx == NOSTREAM:
                continue
            if idx >= len(self.entries) or idx in paths:
                raise CFBError("bad directory tree")
            name, etype, left, right, child = self.entry(idx)[:5]
            if etype not in (1, 2) or "/" in name:
                raise CFBError("bad directory entry")
            paths[idx] = prefix + name
            stack.append((left, prefix)); stack.append((right, prefix))
            if etype == 1:
                stack.append((child, prefix + name + "/"))

        self.streams = []
        for idx in sorted(paths):
            name, etype, left, right, child, start, size = self.entry(idx)
            if etype != 2:
                continue
            segs = []
            if size >= self.cutoff:
                unit, secs = ssz, self.chain(start, fat, (size + ssz - 1) // ssz + 1)
            elif size:
                unit, secs = 64, self.chain(start, minifat, (size + 63) // 64 + 1)
            else:
                unit, secs = 64, []
            if len(secs) * unit < size:
                raise CFBError("short chain")
            left_b = size
            for c in secs:
                if not left_b:
                    break
                n = min(unit, left_b)
                if unit == ssz:
                    o = (c + 1) * ssz
                else:
                    if c * 64 // ssz >= len(ministream):
                        raise CFBError("mini sector outside the mini stream")
                    o = (ministream[c * 64 // ssz] + 1) * ssz + c * 64 % ssz
                segs.append((o, n)); left_b -= n
            self.streams.append((idx, paths[idx], size, segs))
        # content may not overlap structure, the header or other content
        spans = [(0, 512)] + [((i + 1) * ssz, ssz) for i in claimed]
        spans += [s for st in self.streams for s in st[3]]
        spans.sort()
        end = 0
        for o, n in spans:
            if o < end or o + n > len(data):
                raise CFBError("overlapping or truncated content")
            end = o + n
        self.spans = spans

    def entry(self, i):
        e = self.entries[i]
        nlen = struct.unpack_from("<H", e, 64)[0]
        if nlen > 64 or nlen % 2:
            raise CFBError("bad name length")
        name = e[:max(0, nlen - 2)].decode("utf-16-le", "surrogatepass")
        left, right, child = struct.unpack_from("<III", e, 68)
        start = struct.unpack_from("<I", e, 116)[0]
        size = struct.unpack_from("<Q", e, 120)[0]
        if self.major == 3:
            size &= 0xFFFFFFFF
        return name, e[66], left, right, child, start, size

    @staticmethod
    def chain(start, table, limit):
        out, seen = [], set()
        while start != ENDOFCHAIN:
            if start >= len(table) or start in seen or len(out) > limit:
                raise CFBError("broken chain")
            seen.add(start); out.append(start); start = table[start]
        return out

    def rest(self):
        """(offset, length) of the bytes that are neither structure nor
        stream content, in file order."""
        out, pos = [], 0
        for o, n in self.spans:
            if o > pos:
                out.append((pos, o - pos))
            pos = o + n
        if pos < self.length:
            out.append((pos, self.length - pos))
        return out


def stream_bytes(data, segs):
    return b"".join(data[o:o + n] for o, n in segs)


# ---------------------------------------------------------------------------
# skeleton
def _uint(out, v):
    while True:
        out.append((v & 0x7f) | (0x80 if v > 0x7f else 0)); v >>= 7
        if not v: return


def _read_uint(b, i):
    v = sh = 0
    while True:
        c = b[i]; i += 1
        v |= (c & 0x7f) << sh; sh += 7
        if c < 0x80: return v, i


def _rle0(out, b):
    """Zero runs as 00 + run length - 1; other bytes as they are."""
    i, n = 0, len(b)
    while i < n:
        j = b.find(0, i)
        if j < 0:
            out += b[i:]; return
        out += b[i:j]
        k = j
        while k < n and b[k] == 0:
            k += 1
        out.append(0); _uint(out, k - j - 1)
        i = k


def _unrle0(b, i, n):
    """n bytes of _rle0 output starting at b[i]: (bytes, new index)."""
    out = bytearray()
    while len(out) < n:
        c = b[i]; i += 1
        if c:
            out.append(c)
        else:
            v, i = _read_uint(b, i)
            out += bytes(v + 1)
    if len(out) != n:
        raise CFBError("bad zero run")
    return bytes(out), i


_SPECIAL = {ENDOFCHAIN: 1, FREESECT: 2, FATSECT: 3, DIFSECT: 4}
_SPECIAL_INV = {v: k for k, v in _SPECIAL.items()}


def _code_table(out, vals, first):
    """FAT / mini FAT entries: 0 = next sector, 1-4 = special values."""
    for i, v in enumerate(vals, first):
        _uint(out, 0 if v == i + 1 else _SPECIAL.get(v) or v + 5)


def _decode_table(b, p, n, first):
    vals = []
    for i in range(first, first + n):
        m, p = _read_uint(b, p)
        vals.append(i + 1 if m == 0 else _SPECIAL_INV[m] if m < 5 else m - 5)
    return vals, p


def _start_class(etype, size, cutoff):
    """Allocation class of a directory entry's start sector: 0 sectors,
    1 mini sectors, None none."""
    if etype == 5 or (etype == 2 and size >= cutoff): return 0
    if etype == 2 and size: return 1
    return None


def _next_start(cls, start, size, ssz):
    unit = ssz if cls == 0 else 64
    return start + (size + unit - 1) // unit


def _code_dir(out, entries, cutoff, major, ssz):
    """Directory entries as columns: modes, names, odd names, type and
    colour, tree links, CLSID/state/times when not zero, start sectors
    against allocation order, sizes."""
    modes, names, raws, tc, links, extra, starts, sizes = (bytearray() for _ in range(8))
    nxt = [0, 0]
    for e in entries:
        if not any(e):
            modes.append(4); continue
        nlen = struct.unpack_from("<H", e, 64)[0]
        mode = 0
        if 2 <= nlen <= 64 and nlen % 2 == 0 and not any(e[nlen - 2:64]):
            try:
                nm = e[:nlen - 2].decode("utf-16-le")
                if nm.encode("utf-16-le") == e[:nlen - 2] and "\n" not in nm and "\0" not in nm:
                    names += nm.encode("utf-8") + b"\n"; mode = 1
            except UnicodeError:
                pass
        if not mode:
            raws += e[:66]
        if any(e[80:116]):
            mode |= 2; extra += e[80:116]
        modes.append(mode)
        tc += e[66:68]
        for v in struct.unpack_from("<III", e, 68):
            _uint(links, (v + 1) & 0xFFFFFFFF)
        start, lo, hi = struct.unpack_from("<III", e, 116)
        size = lo if major == 3 else lo | hi << 32
        cls = _start_class(e[66], size, cutoff)
        if cls is not None and start == nxt[cls]: _uint(starts, 0)
        elif start in _SPECIAL: _uint(starts, _SPECIAL[start])
        else: _uint(starts, start + 5)
        if cls is not None and start < 0xFFFFFFF0:
            nxt[cls] = _next_start(cls, start, size, ssz)
        _uint(sizes, lo); _uint(sizes, hi)
    out += modes + names + raws + tc + links + extra + starts + sizes


def _decode_dir(b, p, n, cutoff, major, ssz):
    modes = b[p:p + n]; p += n
    live = [m for m in modes if m != 4]
    names = []
    for m in live:
        if m & 1:
            j = b.index(10, p); names.append(b[p:j].decode("utf-8")); p = j + 1
    raws = []
    for m in live:
        if not m & 1:
            raws.append(b[p:p + 66]); p += 66
    tc = []
    for _ in live:
        tc.append(b[p:p + 2]); p += 2
    links = []
    for _ in live:
        three = []
        for _ in range(3):
            v, p = _read_uint(b, p); three.append((v - 1) & 0xFFFFFFFF)
        links.append(three)
    extra = []
    for m in live:
        if m & 2:
            extra.append(b[p:p + 36]); p += 36
    smodes = []
    for _ in live:
        v, p = _read_uint(b, p); smodes.append(v)
    szs = []
    for _ in live:
        lo, p = _read_uint(b, p); hi, p = _read_uint(b, p); szs.append((lo, hi))
    out = []; k = 0; it_n = iter(names); it_r = iter(raws); it_x = iter(extra)
    nxt = [0, 0]
    for m in modes:
        if m == 4:
            out.append(bytes(128)); continue
        if m & 1:
            nm = next(it_n).encode("utf-16-le") + b"\0\0"
            head = nm + bytes(64 - len(nm)) + struct.pack("<H", len(nm))
        else:
            head = next(it_r)
        ext = next(it_x) if m & 2 else bytes(36)
        lo, hi = szs[k]
        size = lo if major == 3 else lo | hi << 32
        cls = _start_class(tc[k][0], size, cutoff)
        sm = smodes[k]
        if sm == 0: start = nxt[cls]
        elif sm < 5: start = _SPECIAL_INV[sm]
        else: start = sm - 5
        if cls is not None and start < 0xFFFFFFF0:
            nxt[cls] = _next_start(cls, start, size, ssz)
        out.append(head + tc[k] + struct.pack("<III", *links[k]) + ext + struct.pack("<III", start, lo, hi))
        k += 1
    return out, p


def skeleton_encode(data, cfb):
    """Everything of `data` but the stream content, as bytes."""
    out = bytearray()
    _uint(out, len(data))
    out += data[:512]
    ssz = cfb.ssz
    for i in cfb.difat_secs:
        out += data[(i + 1) * ssz:(i + 2) * ssz]
    per = ssz // 4
    for k in range(len(cfb.fat_secs)):
        _code_table(out, cfb.fat[k * per:(k + 1) * per], k * per)
    _code_dir(out, cfb.entries, cfb.cutoff, cfb.major, ssz)
    for k in range(len(cfb.mf_secs)):
        _code_table(out, cfb.minifat[k * per:(k + 1) * per], k * per)
    _rle0(out, b"".join(data[o:o + n] for o, n in cfb.rest()))
    return bytes(out)


def skeleton_decode(b, p=0):
    """Inverse of skeleton_encode: (file with zeroed stream content,
    its Compound, new index)."""
    L, p = _read_uint(b, p)
    if L < 512:
        raise CFBError("bad length")
    sk = bytearray(L)
    sk[:512] = b[p:p + 512]; p += 512
    ssz = 1 << struct.unpack_from("<H", sk, 30)[0]
    if ssz not in (512, 4096):
        raise CFBError("bad sector size")
    major = struct.unpack_from("<H", sk, 26)[0]
    n_fat, dir_start = struct.unpack_from("<II", sk, 44)
    cutoff, mf_start, n_mf, dif_start, n_dif = struct.unpack_from("<IIIII", sk, 56)
    nsec = (L - ssz) // ssz

    def put(i, data):
        if not 0 <= i < nsec:
            raise CFBError("sector out of range")
        sk[(i + 1) * ssz:(i + 2) * ssz] = data

    difat = list(struct.unpack_from("<109I", sk, 76))
    s = dif_start
    for _ in range(n_dif):
        sec = b[p:p + ssz]; p += ssz
        put(s, sec)
        difat += struct.unpack_from("<%dI" % (ssz // 4 - 1), sec, 0)
        s = struct.unpack_from("<I", sec, ssz - 4)[0]
    per = ssz // 4
    fat = []
    for k, i in enumerate(difat[:n_fat]):
        vals, p = _decode_table(b, p, per, k * per)
        put(i, struct.pack("<%dI" % per, *vals))
        fat += vals
    dir_secs = Compound.chain(dir_start, fat, nsec)
    entries, p = _decode_dir(b, p, len(dir_secs) * ssz // 128, cutoff, major, ssz)
    dirdata = b"".join(entries)
    for k, i in enumerate(dir_secs):
        put(i, dirdata[k * ssz:(k + 1) * ssz])
    mf_secs = Compound.chain(mf_start, fat, nsec)[:n_mf] if n_mf else []
    for k, i in enumerate(mf_secs):
        vals, p = _decode_table(b, p, per, k * per)
        put(i, struct.pack("<%dI" % per, *vals))
    cfb = Compound(bytes(sk))
    rest = cfb.rest()
    blob, p = _unrle0(b, p, sum(n for _, n in rest))
    q = 0
    for o, n in rest:
        sk[o:o + n] = blob[q:q + n]; q += n
    return bytes(sk), cfb, p


def fill(skel, cfb, contents):
    out = bytearray(skel)
    for (_, _, _, segs), c in zip(cfb.streams, contents):
        q = 0
        for o, n in segs:
            out[o:o + n] = c[q:q + n]; q += n
    return bytes(out)


def split(data):
    """.msg file -> (skeleton bytes, Compound, stream contents), verified to
    rebuild `data` exactly; None if the file can not be handled."""
    try:
        cfb = Compound(data)
        sk = skeleton_encode(data, cfb)
        zeroed, cfb2, p = skeleton_decode(sk)
    except (CFBError, struct.error, IndexError, ValueError, KeyError, UnicodeError):
        return None
    if p != len(sk) or [s[:3] for s in cfb2.streams] != [s[:3] for s in cfb.streams]:
        return None
    contents = [stream_bytes(data, s[3]) for s in cfb2.streams]
    if fill(zeroed, cfb2, contents) != data:
        return None
    return sk, cfb2, contents


# ---------------------------------------------------------------------------
# compressed RTF
RTF_INIT = (b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman \\fswiss \\fmodern \\fscript "
            b"\\fdecor MS Sans SerifSymbolArialTimes New RomanCourier{\\colortbl\\red0\\green0\\blue0\r\n\\par "
            b"\\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx")
S_LIT, S_SHORT, S_FAR, S_RAW = 250, 251, 252, 253
RTF_MELA = b"\xff"


def rtf_crc(b):
    return zlib.crc32(b, 0xFFFFFFFF) ^ 0xFFFFFFFF


def _lzfu_predict(text):
    """The match Outlook's compressor codes at every position of `text`:
    lists of lengths and dictionary offsets.  It is an LZSS binary search
    tree (Okumura's, with a root per first byte, the first of several
    equally long matches on the search path winning, a node replaced by the
    newer one on a full 17-byte match, and on deletion a node with two
    children replaced by its successor) over the 4096-byte dictionary whose
    first 207 bytes are RTF_INIT.  The 17-byte lookahead lives in the
    dictionary, so matches may overlap the bytes they produce."""
    N, F = 4096, 17
    NIL = N
    buf = bytearray(N + F)
    buf[:len(RTF_INIT)] = RTF_INIT
    lson = [NIL] * (N + 1); rson = [NIL] * (N + 257); dad = [NIL] * (N + 1)

    def insert(r):
        cmp = 1; p = N + 1 + buf[r]
        rson[r] = lson[r] = NIL; ml = mp = 0
        while True:
            if cmp >= 0:
                if rson[p] != NIL: p = rson[p]
                else:
                    rson[p] = r; dad[r] = p; return ml, mp
            else:
                if lson[p] != NIL: p = lson[p]
                else:
                    lson[p] = r; dad[r] = p; return ml, mp
            i = 1
            while i < F:
                cmp = buf[r + i] - buf[p + i]
                if cmp: break
                i += 1
            if i > ml:
                mp = p; ml = i
                if ml >= F: break
        dad[r] = dad[p]; lson[r] = lson[p]; rson[r] = rson[p]
        dad[lson[p]] = r; dad[rson[p]] = r
        if rson[dad[p]] == p: rson[dad[p]] = r
        else: lson[dad[p]] = r
        dad[p] = NIL
        return ml, mp

    def delete(p):
        if dad[p] == NIL: return
        if rson[p] == NIL: q = lson[p]
        elif lson[p] == NIL: q = rson[p]
        else:
            q = rson[p]
            if lson[q] != NIL:
                while lson[q] != NIL: q = lson[q]
                lson[dad[q]] = rson[q]; dad[rson[q]] = dad[q]
                rson[q] = rson[p]; dad[rson[p]] = q
            lson[q] = lson[p]; dad[lson[p]] = q
        dad[q] = dad[p]
        if rson[dad[p]] == p: rson[dad[p]] = q
        else: lson[dad[p]] = q
        dad[p] = NIL

    n = len(text)
    lens = [0] * n; offs = [0] * n
    if not n:
        return lens, offs
    r = len(RTF_INIT)
    la = min(F, n)
    buf[r:r + la] = text[:la]
    ip = la
    for q in range(r):
        insert(q)
    lens[0], offs[0] = insert(r)
    for k in range(1, n):
        if ip < n:
            s = (r + F) & (N - 1)
            delete(s)
            c = text[ip]; ip += 1
            buf[s] = c
            if s < F - 1: buf[s + N] = c
            r = (r + 1) & (N - 1)
            lens[k], offs[k] = insert(r)
        else:                       # end of input: nothing is overwritten any more
            r = (r + 1) & (N - 1); la -= 1
            if la:
                lens[k], offs[k] = insert(r)
    return lens, offs


class _Dict:
    """Dictionary before text[p], and the candidate search for tokens that
    differ from the prediction."""
    def __init__(self, text, p):
        total = len(RTF_INIT) + p
        self.w = w = total % 4096; self.total = total
        if total <= 4096:
            self.D = RTF_INIT + text[:p] + b" " * (4096 - total)
        else:
            tail = text[p - 4096:p] if p >= 4096 else RTF_INIT[total - 4096:] + text[:p]
            self.D = tail[4096 - w:] + tail[:4096 - w]

    def candidates(self, look, length=None):
        """Longest match length for `look` (or the given length) and the
        positions that give it, most recent first.  A match may run into the
        bytes the token itself writes; positions just ahead of the write
        position still hold their old bytes."""
        w, wrapped = self.w, self.total >= 4096
        V = bytearray(self.D)
        for j, c in enumerate(look):
            V[(w + j) & 4095] = c
        Vl = bytes(V) + bytes(V[:17]); Dl = self.D + self.D[:17]

        def occ(L):
            pat = look[:L]; res = []
            s = Vl.find(pat)
            while 0 <= s < 4096:
                if s != w and (s < w if not wrapped else not 0 < (s - w) & 4095 < 17):
                    res.append(s)
                s = Vl.find(pat, s + 1)
            if wrapped:
                s = Dl.find(pat)
                while 0 <= s < 4096:
                    if 0 < (s - w) & 4095 < 17:
                        res.append(s)
                    s = Dl.find(pat, s + 1)
            return res

        if length is not None:
            return length, sorted(occ(length), key=lambda c: (w - c) & 4095)
        best, cands = 0, []
        lo, hi = 2, len(look)
        while lo <= hi:
            mid = (lo + hi) // 2
            o = occ(mid)
            if o:
                best, cands, lo = mid, o, mid + 1
            else:
                hi = mid - 1
        return best, sorted(cands, key=lambda c: (w - c) & 4095)


def _tokens(buf, raw):
    """Decompress an LZFu payload: (text, [(offset or None, length)])."""
    D = bytearray(RTF_INIT + b" " * (4096 - len(RTF_INIT))); w = len(RTF_INIT)
    text = bytearray(); toks = []; p, end = 16, len(buf)
    while p < end:
        ctl = buf[p]; p += 1
        for bit in range(8):
            if p >= end:
                return None
            if ctl >> bit & 1:
                if p + 2 > end:
                    return None
                ref = buf[p] << 8 | buf[p + 1]; p += 2
                off, ln = ref >> 4, (ref & 15) + 2
                if off == w:
                    return (bytes(text), toks) if p == end and len(text) == raw else None
                for i in range(ln):
                    c = D[(off + i) & 4095]; text.append(c); D[w] = c; w = (w + 1) & 4095
                toks.append((off, ln))
            else:
                c = buf[p]; p += 1; text.append(c); D[w] = c; w = (w + 1) & 4095
                toks.append((None, 1))
    return None


def rtf_split(buf):
    """Compressed RTF stream -> (rtf text, choice bytes), or None when the
    stream can not be re-created exactly from them (rtf_join)."""
    if len(buf) < 16:
        return None
    comp, raw, magic, crc = struct.unpack_from("<II4sI", buf, 0)
    if comp + 4 != len(buf):
        return None
    if magic == b"MELA":
        return (buf[16:], RTF_MELA) if crc == 0 and raw == len(buf) - 16 else None
    if magic != b"LZFu" or rtf_crc(buf[16:]) != crc:
        return None
    r = _tokens(buf, raw)
    if r is None:
        return None
    text, toks = r
    lens, offs = _lzfu_predict(text)
    n = len(text); ch = bytearray(); p = 0
    for off, ln in toks:
        ml = min(lens[p], n - p)
        if (off, ln) == ((offs[p], ml) if ml >= 2 else (None, 1)):
            ch.append(0)
        elif off is None:
            ch.append(S_LIT)
        else:
            d = _Dict(text, p); look = text[p:p + 17]
            best, cands = d.candidates(look)
            if ln == best and off in cands:
                k = cands.index(off)
                if k + 1 < S_LIT: ch.append(k + 1)
                else: ch.append(S_FAR); _uint(ch, k)
            else:
                c2 = d.candidates(look, ln)[1] if 2 <= ln < best else []
                if off in c2:
                    ch.append(S_SHORT); ch.append(ln); _uint(ch, c2.index(off))
                else:
                    ch.append(S_RAW); _uint(ch, off); ch.append(ln)
        p += ln
    ch = bytes(ch)
    return (text, ch) if rtf_join(text, ch) == buf else None


def rtf_compress(text):
    """Compress RTF exactly as Outlook does (as far as _lzfu_predict knows it)."""
    lens, offs = _lzfu_predict(text)
    p = k = 0
    while p < len(text):
        ml = min(lens[p], len(text) - p)
        p += ml if ml >= 2 else 1; k += 1
    return rtf_join(text, bytes(k))


def rtf_join(text, ch):
    """Inverse of rtf_split."""
    n = len(text)
    if ch == RTF_MELA:
        return struct.pack("<II4sI", n + 12, n, b"MELA", 0) + text
    lens, offs = _lzfu_predict(text)
    out = bytearray(); grp = bytearray(); ctl = bit = 0
    p = i = 0
    while True:
        if p >= n:
            ref = (len(RTF_INIT) + n) % 4096 << 4         # end mark
        else:
            c = ch[i]; i += 1
            if c == 0:
                ml = min(lens[p], n - p)
                off, ln = (offs[p], ml) if ml >= 2 else (None, 1)
            elif c == S_LIT:
                off, ln = None, 1
            elif c == S_RAW:
                off, i = _read_uint(ch, i); ln = ch[i]; i += 1
            else:
                d = _Dict(text, p); look = text[p:p + 17]
                if c == S_SHORT:
                    ln = ch[i]; i += 1
                    k, i = _read_uint(ch, i)
                    off = d.candidates(look, ln)[1][k]
                else:
                    if c == S_FAR: k, i = _read_uint(ch, i)
                    else: k = c - 1
                    ln, cands = d.candidates(look)
                    off = cands[k]
            ref = None if off is None else off << 4 | (ln - 2)
        if ref is None:
            grp.append(text[p])
        else:
            ctl |= 1 << bit; grp += struct.pack(">H", ref)
        bit += 1
        if bit == 8 or p >= n:
            out.append(ctl); out += grp; grp = bytearray(); ctl = bit = 0
        if p >= n:
            break
        p += ln
    payload = bytes(out)
    return struct.pack("<II4sI", len(payload) + 12, n, b"LZFu", rtf_crc(payload)) + payload


# ---------------------------------------------------------------------------
# RTF text against the other bodies
RTF_TOK = re.compile(rb"\\[a-zA-Z]{1,32}(?:-?[0-9]{1,10})? ?|\\'[0-9a-fA-F]{2}|\\.|\\$|[{}\r\n]|[^\\{}\r\n]+", re.S)
RTF_PAR = re.compile(rb"\\par(?![a-zA-Z])")
RTF_CPG = re.compile(rb"\\ansicpg([0-9]{1,5})")
RTF_HI = re.compile(rb"[\x80-\xff]")


def _rtf_codepage(markup):
    m = RTF_CPG.search(markup)
    if not m:
        return None
    try:
        return codecs.lookup("cp" + m.group(1).decode()).name
    except LookupError:
        return None


def rtf_markup_split(text):
    """Decompressed RTF -> (flags, markup, runs): the text runs (literal
    characters, escaped braces and backslashes, and lowercase \\'xx escapes
    of bytes >= 0x80 when the file has no raw 8-bit bytes) each ended by a
    newline, and the rest with byte 01 where a run was.  Every \\par ends a
    run, even an empty one, so the runs of an RTF made from plain text are the
    plain text's lines.  Runs are converted from the RTF's ANSI code page to
    UTF-8 when that is reversible.  None if the text has 01 bytes."""
    if b"\x01" in text:
        return None
    hi = RTF_HI.search(text) is None
    markup = bytearray(); runs = []; cur = bytearray()
    for m in RTF_TOK.finditer(text):
        t = m.group()
        if t[0] != 0x5C and t not in (b"{", b"}", b"\r", b"\n"):
            cur += t; continue
        if t in (b"\\{", b"\\}", b"\\\\"):
            cur += t[1:]; continue
        if hi and len(t) == 4 and t[1] == 0x27 and t[2:] == t[2:].lower() and int(t[2:], 16) >= 0x80:
            cur.append(int(t[2:], 16)); continue
        if cur or RTF_PAR.match(t):
            markup.append(1); runs.append(bytes(cur)); cur = bytearray()
        markup += t
    if cur:
        markup.append(1); runs.append(bytes(cur))
    flags = 1 if hi else 0
    cp = _rtf_codepage(bytes(markup)) if hi else None
    if cp:
        try:
            u = [r.decode(cp).encode("utf-8") for r in runs]
            if all(x.decode("utf-8").encode(cp) == r for x, r in zip(u, runs)) and not any(b"\n" in x for x in u):
                runs = u; flags |= 2
        except UnicodeError:
            pass
    out = (flags, bytes(markup), b"".join(r + b"\n" for r in runs))
    return out if rtf_markup_join(*out) == text else None


def rtf_markup_join(flags, markup, runs):
    """Inverse of rtf_markup_split."""
    rl = runs.split(b"\n")
    cp = _rtf_codepage(markup) if flags & 2 else None
    parts = markup.split(b"\x01")
    out = bytearray(parts[0])
    for k, part in enumerate(parts[1:]):
        r = rl[k]
        if cp: r = r.decode("utf-8").encode(cp)
        r = r.replace(b"\\", b"\\\\").replace(b"{", b"\\{").replace(b"}", b"\\}")
        if flags & 1: r = RTF_HI.sub(lambda m: b"\\'%02x" % m.group()[0], r)
        out += r; out += part
    return bytes(out)
