"""Outlook PST and OST support for emz (MS-PST).

A personal store is a database: a header, two B-trees (nodes and blocks) in
pages, and data blocks, each with a trailer (size, signature, CRC, ID), all
in one file whose free space keeps old bytes.  emz splits it into

* the *skeleton*: header, B-tree pages and everything that is neither page
  nor block data (allocation maps, block padding, free space), with every
  CRC and signature that can be recomputed left out, and block trailers
  reduced to a flag when they are what the block predicts;
* the *blocks*, which emz codes node by node: data blocks decoded from the
  "permute" encoding, heap-on-node pages split into their allocations and
  property values coded with the .msg models (see emz.py, code_pst).

Unicode PST files (format 23) and OST files of Outlook 2013 and later
(format 36, 4 KB pages) are handled.  OST blocks that Outlook compressed
with deflate are kept as stored (zlib does not reproduce Outlook's
compressor), and so are the data blocks of stores with the "cyclic"
encoding.  Other formats (ANSI PST) and anything that does not parse raise
PSTError, and emz then codes the file as an opaque message.
"""
import struct, zlib
from emz_msg import _uint, _read_uint

PST_MAGIC = b"!BDN"
DEC = bytes.fromhex(
    "47f1b4e60b6a7248854e9eebe2f89453e0bba002e85a09abdbe3bac67cc310dd39059630f53760828cc9134a6b1df3fb8f2697ca911701c4322d6e3195ffd923"
    "d1005e79dc443b1a28c5615720903d83b943be67d2464276c06d5b7eb20f16293ca903540dda5ddff6b7c762cd8d06d3695c86d614f7a56675acb1e94521700c"
    "879f74a4224c6fbf1f56aa2eb3783350b0a392bccf191ca763cb1e4d3e4b1b9b4fe7f0eead3ab55904ea40552551e57a893868527bfc27aed7bdfa07f4cc8e5f"
    "ef359c842b15d5773449b6120a7f7188fd9d18417d93d8582ccefe24afdeb836c8a180a69998a82f0e816573e4c2a28ad4e111d0088b2af2ed9a643fc16cf9ec")
ENC = bytes(DEC.index(i) for i in range(256))
HEADER = 564


class PSTError(Exception):
    pass


def crc(b):
    return zlib.crc32(b, 0xFFFFFFFF) ^ 0xFFFFFFFF


def sig(ib, bid):
    x = (ib ^ bid) & 0xFFFFFFFF
    return ((x >> 16) ^ x) & 0xFFFF


class Store:
    """Structure of a PST/OST file, read through `page(ib, bid)`, which
    returns the page at file offset ib: the encoder reads the file, the
    decoder rebuilds pages from the skeleton in the same order."""

    def __init__(self, header, length, page, keep_all=False):
        if len(header) < HEADER or header[:4] != PST_MAGIC:
            raise PSTError("not a PST/OST file")
        self.length = length; self.keep_all = keep_all
        self.ver = struct.unpack_from("<H", header, 10)[0]
        if self.ver == 23:
            self.page, self.align, self.btl, self.meta, self.ptl = 512, 64, 16, 488, 496
        elif self.ver == 36:
            self.page, self.align, self.btl, self.meta, self.ptl = 4096, 512, 24, 4056, 4072
        else:
            raise PSTError("unsupported format %d" % self.ver)
        self.crypt = header[513]
        self.pages = []; self.nodes = []; self.blocks = {}; self.border = []
        self.spans = [(0, HEADER)]
        seen = set()

        def walk(ib, bid, ptype, level):
            if ib in seen or ib < HEADER or ib + self.page > length:
                raise PSTError("bad page reference")
            seen.add(ib)
            p = page(ib, bid)
            if p[self.ptl] != ptype or p[self.ptl + 1] != ptype:
                raise PSTError("bad page type")
            self.pages.append(ib); self.spans.append((ib, self.page))
            if self.ver == 23:
                cEnt, cEntMax, cbEnt, cLevel = p[488:492]
            else:
                cEnt, cEntMax = struct.unpack_from("<HH", p, 4056); cbEnt, cLevel = p[4060], p[4061]
            if cEnt > cEntMax or cEnt * cbEnt > self.meta or (level is not None and cLevel != level):
                raise PSTError("bad page header")
            if cLevel and cbEnt != 24 or not cLevel and cbEnt != (24 if ptype == 0x80 else 32):
                raise PSTError("bad entry size")
            for i in range(cEnt):
                o = i * cbEnt
                if cLevel:
                    cbid, cib = struct.unpack_from("<QQ", p, o + 8)
                    walk(cib, cbid, ptype, cLevel - 1)
                elif ptype == 0x80:
                    bid, ib2, cb = struct.unpack_from("<QQH", p, o)
                    cbinf = struct.unpack_from("<H", p, o + 18)[0] if self.ver == 36 else cb
                    if bid in self.blocks:
                        raise PSTError("duplicate block")
                    self.blocks[bid] = (ib2, cb, cbinf); self.border.append(bid)
                else:
                    self.nodes.append(struct.unpack_from("<QQQI", p, o))

        nbt_bid, nbt_ib, bbt_bid, bbt_ib = struct.unpack_from("<QQQQ", header, 216)
        walk(nbt_ib, nbt_bid, 0x81, None)
        walk(bbt_ib, bbt_bid, 0x80, None)
        for bid in self.border:
            ib, cb, cbinf = self.blocks[bid]
            size = (cb + self.btl + self.align - 1) // self.align * self.align
            if ib < HEADER or ib + size > length:
                raise PSTError("block outside the file")
            if not self.kept(bid):
                self.spans.append((ib, cb))
            self.spans.append((ib + size - self.btl, self.btl))
        self.spans.sort()
        end = 0
        for o, n in self.spans:
            if o < end:
                raise PSTError("overlapping structures")
            end = o + n

    def rest(self):
        out, pos = [], 0
        for o, n in self.spans:
            if o > pos:
                out.append((pos, o - pos))
            pos = max(pos, o + n)
        if pos < self.length:
            out.append((pos, self.length - pos))
        return out

    def kept(self, bid):
        """Blocks that stay in the skeleton, in file order next to the free
        space that often holds older copies of them: internal blocks, opaque
        blocks, and every block of a store that is mostly opaque (an OST
        whose blocks Outlook compressed), where the few plain blocks are
        better predicted from their old copies than by the property models."""
        return self.keep_all or bid & 2 or self.opaque(bid)

    def opaque(self, bid):
        """Blocks kept as stored: deflate-compressed (OST) or encoded with
        a method other than none and permute."""
        ib, cb, cbinf = self.blocks[bid]
        return cbinf != cb or (not bid & 2 and self.crypt not in (0, 1))

    def decode(self, bid, stored):
        """Stored block bytes -> plain bytes (None for opaque blocks)."""
        if self.opaque(bid):
            return None
        return stored.translate(DEC) if self.crypt == 1 and not bid & 2 else stored

    def encode(self, bid, plain):
        return plain.translate(ENC) if self.crypt == 1 and not bid & 2 else plain

    def inner(self, bid, stored):
        """Contents of an internal block (inflated if need be) for parsing."""
        ib, cb, cbinf = self.blocks[bid]
        return zlib.decompress(stored) if cbinf != cb else stored

    def trailer(self, bid, stored):
        ib, cb, cbinf = self.blocks[bid]
        t = struct.pack("<HHIQ", cb, sig(ib, bid), crc(stored), bid)
        return t + struct.pack("<HHI", 2, cbinf, 0) if self.ver == 36 else t


def _page_fields(st, ib, p):
    """(sig, crc) a B-tree page should carry."""
    bid = struct.unpack_from("<Q", p, st.ptl + 8)[0]
    return sig(ib, bid), crc(p[:st.ptl])


def skeleton_encode(data, st):
    out = bytearray()
    _uint(out, len(data)); out.append(st.keep_all)
    h = bytearray(data[:HEADER]); fl = 0
    if struct.unpack_from("<I", h, 4)[0] == crc(h[8:479]): fl |= 1; h[4:8] = bytes(4)
    if struct.unpack_from("<I", h, 524)[0] == crc(h[8:524]): fl |= 2; h[524:528] = bytes(4)
    out.append(fl); out += h
    for ib in st.pages:
        p = bytearray(data[ib:ib + st.page])
        s, c = _page_fields(st, ib, p); fl = 0
        if struct.unpack_from("<H", p, st.ptl + 2)[0] == s: fl |= 1; p[st.ptl + 2:st.ptl + 4] = bytes(2)
        if struct.unpack_from("<I", p, st.ptl + 4)[0] == c: fl |= 2; p[st.ptl + 4:st.ptl + 8] = bytes(4)
        out.append(fl); out += p
    for bid in st.border:
        ib, cb, cbinf = st.blocks[bid]
        size = (cb + st.btl + st.align - 1) // st.align * st.align
        t = data[ib + size - st.btl:ib + size]
        if t == st.trailer(bid, data[ib:ib + cb]):
            out.append(0)
        else:
            out.append(1); out += t
    for o, n in st.rest():                 # as they are: zero runs are cheap for the coder, and the free
        out += data[o:o + n]               # space holds UTF-16 text and records that runs would cut up
    return bytes(out)


def skeleton_decode(b, p=0):
    """-> (file with block data and trailers zeroed, Store, new index)."""
    L, p = _read_uint(b, p)
    keep_all = b[p]; fl = b[p + 1]; p += 2
    h = bytearray(b[p:p + HEADER]); p += HEADER
    if fl & 1: h[4:8] = struct.pack("<I", crc(h[8:479]))
    if fl & 2: h[524:528] = struct.pack("<I", crc(h[8:524]))
    sk = bytearray(L)
    sk[:HEADER] = h
    pos = [p]

    def page(ib, bid):
        q = pos[0]; fl = b[q]
        pg = bytearray(b[q + 1:q + 1 + size]); pos[0] = q + 1 + size
        s, c = _page_fields(st_proxy, ib, pg)
        if fl & 1: pg[st_proxy.ptl + 2:st_proxy.ptl + 4] = struct.pack("<H", s)
        if fl & 2: pg[st_proxy.ptl + 4:st_proxy.ptl + 8] = struct.pack("<I", c)
        sk[ib:ib + size] = pg
        return bytes(pg)

    ver = struct.unpack_from("<H", h, 10)[0]
    size = 4096 if ver == 36 else 512

    class st_proxy:
        ptl = 4072 if ver == 36 else 496
    st = Store(bytes(h), L, page, keep_all)
    p = pos[0]
    st.trailers = {}
    for bid in st.border:
        if b[p]:
            st.trailers[bid] = bytes(b[p + 1:p + 1 + st.btl]); p += 1 + st.btl
        else:
            p += 1
    for o, n in st.rest():
        sk[o:o + n] = b[p:p + n]; p += n
    st.zeroed = sk
    return sk, st, p


def fill(sk, st, stored):
    """Place block data {bid: stored bytes} and trailers into the skeleton."""
    out = bytearray(sk)
    for bid in st.border:
        ib, cb, cbinf = st.blocks[bid]
        size = (cb + st.btl + st.align - 1) // st.align * st.align
        s = out[ib:ib + cb] if st.kept(bid) else stored[bid]
        out[ib:ib + cb] = s
        out[ib + size - st.btl:ib + size] = st.trailers.get(bid) or st.trailer(bid, bytes(s))
    return bytes(out)


def split(data):
    """PST/OST file -> (skeleton bytes, Store, {bid: stored bytes}), verified
    to rebuild `data` exactly; None if the file can not be handled."""
    try:
        page = lambda ib, bid: data[ib:ib + (4096 if data[10] == 36 else 512)]
        st = Store(data[:HEADER], len(data), page)
        plain = sum(cb for bid, (ib, cb, cbinf) in st.blocks.items() if not st.kept(bid))
        if plain * 10 < len(data):
            st = Store(data[:HEADER], len(data), page, True)
        sk = skeleton_encode(data, st)
        zeroed, st2, p = skeleton_decode(sk)
    except (PSTError, struct.error, IndexError, ValueError, zlib.error):
        return None
    if p != len(sk) or st2.border != st.border:
        return None
    stored = {bid: data[ib:ib + cb] for bid, (ib, cb, cbinf) in st2.blocks.items()}
    st2.zeroed = zeroed
    if fill(zeroed, st2, stored) != data:
        return None
    return sk, st2, stored


# ---------------------------------------------------------------------------
# block trees
def xblock(plain):
    """XBLOCK / XXBLOCK -> (level, child bids) or None."""
    if len(plain) < 8 or plain[0] != 1 or plain[1] not in (1, 2):
        return None
    n = struct.unpack_from("<H", plain, 2)[0]
    if 8 + 8 * n > len(plain):
        return None
    return plain[1], list(struct.unpack_from("<%dQ" % n, plain, 8))


def slblock(plain):
    """SLBLOCK -> (0, [(nid, bidData, bidSub)]), SIBLOCK -> (1, [bid]), or None."""
    if len(plain) < 8 or plain[0] != 2 or plain[1] not in (0, 1):
        return None
    n = struct.unpack_from("<H", plain, 2)[0]
    if plain[1] == 0:
        if 8 + 24 * n > len(plain): return None
        return 0, [struct.unpack_from("<QQQ", plain, 8 + 24 * i) for i in range(n)]
    if 8 + 16 * n > len(plain): return None
    return 1, [struct.unpack_from("<Q", plain, 8 + 16 * i + 8)[0] for i in range(n)]


# ---------------------------------------------------------------------------
# heap-on-node pages
def hn_split(b, i):
    """Heap-on-node page i of a node -> (header, cFree, allocations, gap,
    tail), or None.  Page 0 starts with HNHDR, pages 8, 136, ... with
    HNBITMAPHDR, the others with HNPAGEHDR; the page map lists where the
    allocations start."""
    hs = 12 if i == 0 else 66 if i >= 8 and (i - 8) % 128 == 0 else 2
    if len(b) < hs + 4 or (i == 0 and b[2] != 0xEC):
        return None
    ibm = struct.unpack_from("<H", b, 0)[0]
    if not hs <= ibm <= len(b) - 4:
        return None
    n, nfree = struct.unpack_from("<HH", b, ibm)
    end = ibm + 4 + 2 * (n + 1)
    if end > len(b):
        return None
    offs = struct.unpack_from("<%dH" % (n + 1), b, ibm + 4)
    if offs[0] != hs or offs[-1] > ibm or any(offs[k] > offs[k + 1] for k in range(n)):
        return None
    return b[:hs], nfree, [b[offs[k]:offs[k + 1]] for k in range(n)], b[offs[-1]:ibm], b[end:]


def hn_join(hdr, nfree, allocs, gap, tail):
    offs = [len(hdr)]
    for a in allocs:
        offs.append(offs[-1] + len(a))
    return (hdr + b"".join(allocs) + gap + struct.pack("<HH", len(allocs), nfree)
            + struct.pack("<%dH" % len(offs), *offs) + tail)


def hid_ref(hid):
    """HID -> (page, allocation index), or None for a NID or HID 0."""
    if hid & 0x1F or not hid >> 5:
        return None
    return hid >> 16, ((hid >> 5) & 0x7FF) - 1
