"""Synthetic Outlook .msg files for the tests: a small compound-file writer
(MS-CFB) and a builder for the MS-OXMSG property layout.  No third-party
messages are needed."""
import os, random, struct, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from emz_msg import rtf_compress  # noqa: E402

ENDOFCHAIN, FREESECT, FATSECT, DIFSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFC, 0xFFFFFFFF


def cfb(items, ssz=512, garbage=0, seed=1):
    """Compound file holding the streams `items` [(path, bytes)], storages
    implied by the paths.  `garbage` free sectors of random bytes are added."""
    rnd = random.Random(seed)
    names, kind, parent, data = ["Root Entry"], [5], [None], [b""]
    index = {"": 0}
    for path, blob in items:
        parts = path.split("/")
        for i in range(1, len(parts)):
            sp = "/".join(parts[:i])
            if sp not in index:
                index[sp] = len(names); names.append(parts[i - 1]); kind.append(1)
                parent.append(index["/".join(parts[:i - 1])]); data.append(b"")
        index[path] = len(names); names.append(parts[-1]); kind.append(2)
        parent.append(index["/".join(parts[:-1])]); data.append(blob)
    n = len(names)
    left, right, child = [NOSTREAM] * n, [NOSTREAM] * n, [NOSTREAM] * n

    def bst(ids):                                   # balanced tree, CFB name order
        if not ids: return NOSTREAM
        m = len(ids) // 2
        left[ids[m]] = bst(ids[:m]); right[ids[m]] = bst(ids[m + 1:])
        return ids[m]
    for s in range(n):
        if kind[s] in (1, 5):
            kids = sorted((i for i in range(n) if parent[i] == s), key=lambda i: (len(names[i]), names[i].upper()))
            child[s] = bst(kids)
    # mini stream
    mini = bytearray(); minifat = []; start = [ENDOFCHAIN] * n
    for i in range(n):
        if kind[i] == 2 and 0 < len(data[i]) < 4096:
            start[i] = len(mini) // 64
            k = (len(data[i]) + 63) // 64
            minifat += [start[i] + j + 1 for j in range(k - 1)] + [ENDOFCHAIN]
            mini += data[i] + bytes(k * 64 - len(data[i]))
    sectors = []; fat = []

    def alloc(blob):
        if not blob: return ENDOFCHAIN
        first = len(sectors); k = (len(blob) + ssz - 1) // ssz
        for j in range(k):
            sectors.append(blob[j * ssz:(j + 1) * ssz].ljust(ssz, b"\0"))
            fat.append(first + j + 1 if j < k - 1 else ENDOFCHAIN)
        return first
    for i in range(n):
        if kind[i] == 2 and len(data[i]) >= 4096:
            start[i] = alloc(data[i])
    for _ in range(garbage):
        sectors.append(bytes(rnd.randrange(256) for _ in range(ssz))); fat.append(FREESECT)
    root_start = alloc(bytes(mini))
    per = ssz // 4
    mf_start = alloc(struct.pack("<%dI" % len(minifat), *minifat).ljust(-(-len(minifat) // per) * ssz, b"\xff")) if minifat else ENDOFCHAIN
    n_mf = -(-len(minifat) // per)
    entries = []
    for i in range(n):
        nm = names[i].encode("utf-16-le") + b"\0\0"
        size = len(mini) if kind[i] == 5 else len(data[i])
        st = root_start if kind[i] == 5 else start[i] if kind[i] == 2 else 0
        entries.append(nm.ljust(64, b"\0") + struct.pack("<HBBIII", len(nm), kind[i], 1, left[i], right[i], child[i])
                       + bytes(36) + struct.pack("<IQ", st, size))
    while len(entries) * 128 % ssz:
        entries.append(bytes(64) + struct.pack("<HBBIII", 0, 0, 0, NOSTREAM, NOSTREAM, NOSTREAM) + bytes(48))
    dir_start = alloc(b"".join(entries))
    n_dir = len(entries) * 128 // ssz
    # FAT (and DIFAT) sectors describe themselves
    n_fat = n_dif = 0
    while True:
        total = len(sectors) + n_fat + n_dif
        need_fat = -(-total // per)
        need_dif = max(0, -(-(need_fat - 109) // (per - 1)))
        if (need_fat, need_dif) == (n_fat, n_dif): break
        n_fat, n_dif = need_fat, need_dif
    fat_first = len(sectors); dif_first = fat_first + n_fat
    fat += [FATSECT] * n_fat + [DIFSECT] * n_dif
    fat += [FREESECT] * (n_fat * per - len(fat))
    for k in range(n_fat):
        sectors.append(struct.pack("<%dI" % per, *fat[k * per:(k + 1) * per]))
    fat_secs = list(range(fat_first, fat_first + n_fat))
    rest = fat_secs[109:]
    for k in range(n_dif):
        chunk = rest[k * (per - 1):(k + 1) * (per - 1)]
        chunk += [FREESECT] * (per - 1 - len(chunk))
        sectors.append(struct.pack("<%dI" % per, *chunk, dif_first + k + 1 if k < n_dif - 1 else ENDOFCHAIN))
    difat = (fat_secs[:109] + [FREESECT] * 109)[:109]
    hdr = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(16) + struct.pack("<HHHHH", 0x3E, 3 if ssz == 512 else 4, 0xFFFE,
                                                                        9 if ssz == 512 else 12, 6)
           + bytes(6) + struct.pack("<IIIIIIIII", 0 if ssz == 512 else n_dir, n_fat, dir_start, 0, 4096,
                                    mf_start, n_mf, dif_first if n_dif else ENDOFCHAIN, n_dif)
           + struct.pack("<109I", *difat))
    return hdr.ljust(ssz, b"\0") + b"".join(sectors)


def u16(s):
    return s.encode("utf-16-le")


def props(entries, header):
    """__properties_version1.0: header + (tag, flags, 8-byte value) entries."""
    return header + b"".join(struct.pack("<II", t, 6) + v for t, v in entries)


def storage(prefix, strings, binaries, fixed, header):
    """Streams of one property storage: strings {id: str}, binaries
    {id: bytes}, fixed [(tag, 8-byte value)]."""
    items, ents = [], list(fixed)
    for pid, s in strings.items():
        tag = pid << 16 | 0x1F
        items.append((prefix + "__substg1.0_%08X" % tag, u16(s)))
        ents.append((tag, struct.pack("<II", len(u16(s)) + 2, 3)))
    for pid, b in binaries.items():
        tag = pid << 16 | 0x102
        items.append((prefix + "__substg1.0_%08X" % tag, b))
        ents.append((tag, struct.pack("<II", len(b), 0)))
    ents.sort()
    return [(prefix + "__properties_version1.0", props(ents, header))] + items


def message(i, rnd, attach=b"", embedded=False, rtf=True, transport=True):
    """A plausible Outlook message: properties, recipients, RTF and plain
    bodies, transport headers, optionally an attachment and an embedded
    message."""
    who = rnd.choice(["Alice Example", "Bob Example", "Carol Example"])
    addr = who.split()[0].lower() + "@example.org"
    subj = "Quarterly report %d" % rnd.randrange(100)
    body = "Hello,\r\n\r\nplease find the numbers for item %d below.\r\n\r\n%s\r\n\r\nRegards,\r\n%s\r\n" % (
        i, "\r\n".join("line %d: %d" % (k, rnd.randrange(10 ** 6)) for k in range(rnd.randrange(3, 30))), who)
    ft = struct.pack("<Q", 132000000000000000 + i * 10 ** 9 + rnd.randrange(10 ** 7))
    strings = {0x001A: "IPM.Note", 0x0037: subj, 0x0E1D: subj, 0x0070: subj, 0x0C1A: who, 0x0C1F: addr,
               0x0042: who, 0x0065: addr, 0x5D01: addr, 0x0E04: "Dave Example", 0x1000: body,
               0x1035: "<msg%d.%d@example.org>" % (i, rnd.randrange(10 ** 9))}
    if transport:
        strings[0x007D] = ("Received: from mx.example.org ([192.0.2.%d]) by mail.example.org\r\n with SMTP; "
                           "Tue, %d Mar 2021 10:%02d:00 +0000\r\nFrom: %s <%s>\r\nTo: Dave Example <dave@example.org>\r\n"
                           "Subject: %s\r\nDate: Tue, %d Mar 2021 10:%02d:00 +0000\r\nMessage-ID: %s\r\n"
                           "MIME-Version: 1.0\r\nContent-Type: text/plain\r\n\r\n" % (
                               i % 250, 1 + i % 28, i % 60, who, addr, subj, 1 + i % 28, i % 60, strings[0x1035]))
    eid = b"\0\0\0\0\xdc\xa7\x40\xc8\xc0\x42\x10\x1a\xb4\xb9\x08\x00\x2b\x2f\xe1\x82\x01\0\0\0\0\0\0\0" + \
        ("/O=EXAMPLE/OU=FIRST ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=" + who.split()[0].upper()).encode() + b"\0"
    binaries = {0x0C19: eid, 0x0041: eid, 0x003B: b"SMTP:" + addr.upper().encode() + b"\0",
                0x0071: bytes(rnd.randrange(256) for _ in range(22))}
    if rtf:
        text = ("{\\rtf1\\ansi\\ansicpg1252\\fromtext \\fbidis \\deff0{\\fonttbl\r\n{\\f0\\fswiss Arial;}\r\n"
                "{\\f1\\fmodern Courier New;}}\r\n{\\colortbl\\red0\\green0\\blue0;}\r\n\\uc1\\pard\\plain\\deftab360 "
                "\\f0\\fs20 " + body.replace("\r\n", "\r\n\\par ") + "\r\n}").encode()
        binaries[0x1009] = rtf_compress(text)
    fixed = [(0x0E070003, struct.pack("<II", 1, 0)), (0x00390040, ft), (0x0E060040, ft),
             (0x30070040, ft), (0x30080040, ft)]
    items = storage("", strings, binaries, fixed, struct.pack("<8sIIII8s", bytes(8), 1, 1 if attach else 0, 1,
                                                                  1 if attach else 0, bytes(8)))
    items += storage("__recip_version1.0_#00000000/", {0x3001: "Dave Example", 0x3003: "dave@example.org"},
                     {0x0FFF: eid}, [(0x0C150003, struct.pack("<II", 1, 0))], bytes(8))
    if attach:
        items += storage("__attach_version1.0_#00000000/", {0x3707: "data.bin", 0x3704: "data.bin"},
                         {0x3701: attach}, [(0x37050003, struct.pack("<II", 1, 0))], bytes(8))
    if embedded:
        pre = "__attach_version1.0_#00000001/"
        items += [(pre + "__properties_version1.0", props([(0x37050003, struct.pack("<II", 5, 0))], bytes(8)))]
        items += storage(pre + "__substg1.0_3701000D/", {0x001A: "IPM.Note", 0x0037: "Fwd: " + subj, 0x1000: body},
                         {}, [(0x0E070003, struct.pack("<II", 1, 0))], bytes(24))
    items += [("__nameid_version1.0/__substg1.0_00020102", bytes(16) + bytes(range(16))),
              ("__nameid_version1.0/__substg1.0_00030102", struct.pack("<IHH", 0x8554, 6, 0)),
              ("__nameid_version1.0/__substg1.0_00040102", b"")]
    return items
