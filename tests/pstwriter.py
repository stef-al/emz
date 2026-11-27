"""Synthetic Unicode PST files for the tests: the node and block B-trees,
data blocks with trailers (permute-encoded or not), data trees and subnode
trees, and property contexts as heap-on-node pages.  Structurally
consistent for emz; not meant to be opened by Outlook."""
import os, random, struct, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emz_pst import ENC, crc, sig  # noqa: E402
from emz_msg import rtf_compress  # noqa: E402


def u16(s):
    return s.encode("utf-16-le")


def heap(client, allocs):
    """One heap-on-node page; allocation 0 is the user root."""
    offs = [12]
    for a in allocs:
        offs.append(offs[-1] + len(a))
    body = b"".join(allocs)
    return (struct.pack("<HBBI", 12 + len(body), 0xEC, client, 1 << 5) + bytes(4) + body
            + struct.pack("<HH", len(allocs), 0) + struct.pack("<%dH" % len(offs), *offs))


def pc(props, first_sub):
    """Property context from [(id, type, value)]: int values inline, byte
    values in allocations, or in subnodes above 3580 bytes.  Returns the
    heap and [(subnode nid, bytes)]."""
    allocs, recs, subs = [b"", b""], [], []
    for pid, typ, val in sorted(props):
        if isinstance(val, int):
            recs.append(struct.pack("<HHI", pid, typ, val))
        elif len(val) > 3580:
            nid = first_sub + 32 * len(subs)
            subs.append((nid, val)); recs.append(struct.pack("<HHI", pid, typ, nid))
        else:
            allocs.append(val); recs.append(struct.pack("<HHI", pid, typ, len(allocs) << 5))
    allocs[0] = struct.pack("<BBBBI", 0xB5, 2, 6, 0, 2 << 5)
    allocs[1] = b"".join(recs)
    return heap(0xBC, allocs), subs


def message(i, rnd, big=False):
    """A message node: a property context with strings, an entry ID, times
    and flags, the plain body and its compressed RTF (in subnodes when big)."""
    who = rnd.choice(["Alice Example", "Bob Example", "Carol Example"])
    addr = who.split()[0].lower() + "@example.org"
    subj = "Status update %d" % rnd.randrange(50)
    body = "Hello,\r\n\r\n" + "".join("item %d is at %d.\r\n" % (k, rnd.randrange(10 ** 5))
                                      for k in range(rnd.randrange(5, 40 if not big else 1200))) + "Regards,\r\n" + who
    rtf = ("{\\rtf1\\ansi\\ansicpg1252\\fromtext \\deff0{\\fonttbl\r\n{\\f0\\fswiss Arial;}}\r\n\\pard\\plain \\f0\\fs20 "
           + body.replace("\r\n", "\\par\r\n") + "}").encode()
    eid = bytes(4) + bytes(range(16)) + ("/O=EXAMPLE/CN=" + who.split()[0].upper()).encode() + b"\0"
    props = [(0x001A, 0x1F, u16("IPM.Note")), (0x0037, 0x1F, u16(subj)), (0x0E1D, 0x1F, u16(subj)),
             (0x0C1A, 0x1F, u16(who)), (0x0C1F, 0x1F, u16(addr)), (0x0042, 0x1F, u16(who)),
             (0x0C19, 0x102, eid), (0x0041, 0x102, eid), (0x0E07, 0x03, 1), (0x0E17, 0x03, 0),
             (0x0039, 0x40, struct.pack("<Q", 132500000000000000 + i * 10 ** 9)),
             (0x1000, 0x1F, u16(body)), (0x1009, 0x102, rtf_compress(rtf))]
    return pc(props, 0x1000 * (i + 1) + 0x1F)


def pst(nodes, crypt=1, garbage=0, orphan=False, seed=1):
    """File from nodes [(nid, data, [(subnode nid, data)])]."""
    rnd = random.Random(seed)
    blocks, bbt, nbt = [], [], []
    nb = [4]

    def block(plain, internal):
        bid = nb[0] | (2 if internal else 0); nb[0] += 4
        blocks.append((bid, plain, internal)); return bid

    def tree(b):
        chunks = [b[k:k + 8176] for k in range(0, len(b), 8176)] or [b""]
        bids = [block(c, False) for c in chunks]
        if len(bids) == 1:
            return bids[0]
        return block(struct.pack("<BBHI", 1, 1, len(bids), len(b)) + struct.pack("<%dQ" % len(bids), *bids), True)
    for nid, data, subs in nodes:
        bd = tree(data); bs = 0
        if subs:
            ents = b"".join(struct.pack("<QQQ", s, tree(d), 0) for s, d in subs)
            bs = block(struct.pack("<BBHI", 2, 0, len(subs), 0) + ents, True)
        nbt.append(struct.pack("<QQQII", nid, bd, bs, 0, 0))
    if orphan:
        block(b"an orphan block that no node refers to" * 3, False)
    out = bytearray(0x4400)
    for bid, plain, internal in blocks:
        stored = plain.translate(ENC) if crypt == 1 and not internal else plain
        ib = len(out); size = (len(stored) + 16 + 63) // 64 * 64
        out += stored + bytes(size - 16 - len(stored)) + struct.pack("<HHIQ", len(stored), sig(ib, bid), crc(stored), bid)
        bbt.append(struct.pack("<QQHHI", bid, ib, len(stored), 1, 0))
        if garbage and rnd.random() < 0.3:
            out += bytes(rnd.randrange(256) for _ in range(64 * garbage))
    out += bytes(-len(out) % 512)
    pb = [0x10001]

    def page(ptype, entries, cbent, level):
        bid = pb[0]; pb[0] += 4
        ib = len(out)
        p = b"".join(entries).ljust(488, b"\0") + bytes([len(entries), 488 // cbent, cbent, level]) + bytes(4)
        out.extend(p + struct.pack("<BBHIQ", ptype, ptype, sig(ib, bid), crc(p), bid))
        return bid, ib

    def btree(ptype, entries, cbent):
        per = 488 // cbent
        refs = [(entries[k][:8],) + page(ptype, entries[k:k + per], cbent, 0) for k in range(0, len(entries), per)] or \
            [(bytes(8),) + page(ptype, [], cbent, 0)]
        level = 1
        while len(refs) > 1:
            refs = [(grp[0][0],) + page(ptype, [k + struct.pack("<QQ", b, ib) for k, b, ib in grp], 24, level)
                    for grp in (refs[k:k + 20] for k in range(0, len(refs), 20))]
            level += 1
        return refs[0][1:]
    nbt_ref = btree(0x81, nbt, 32)
    bbt_ref = btree(0x80, bbt, 24)
    h = bytearray(564)
    h[0:4] = b"!BDN"; h[8:10] = b"SM"
    struct.pack_into("<HHBB", h, 10, 23, 19, 1, 1)
    struct.pack_into("<Q", h, 184, len(out))
    struct.pack_into("<QQQQ", h, 216, nbt_ref[0], nbt_ref[1], bbt_ref[0], bbt_ref[1])
    h[512] = 0x80; h[513] = crypt
    struct.pack_into("<Q", h, 516, nb[0])
    struct.pack_into("<I", h, 4, crc(bytes(h[8:479])))
    struct.pack_into("<I", h, 524, crc(bytes(h[8:524])))
    out[:564] = h
    return bytes(out)


def store(n=6, crypt=1, garbage=0, orphan=False, big=False, seed=1):
    """A store with a folder-like table node and n messages."""
    rnd = random.Random(seed)
    nodes = [(0x2D, heap(0x7C, [bytes(22), u16("Inbox"), u16("Status update 3"), bytes(range(40))]), [])]
    for i in range(n):
        data, subs = message(i, rnd, big=big and i == 1)
        nodes.append((0x200004 + 32 * i, data, subs))
    return pst(nodes, crypt, garbage, orphan, seed)
