#!/usr/bin/env python3
"""emz -- structure-aware compressor for e-mail archives.

    emz.py c <input> <archive>      compress
    emz.py d <archive> <output>     decompress (restores the input's form)

The input is a directory tree of message files (maildir, MH, ...), an mbox
file, a corpus file (`path\\n`, `length\\n`, raw bytes per message; see
README) or a single message.  Every message is parsed into header fields and
body; an mbox `From ` line is coded against the message's own Return-Path and
Received date.  The field-name
sequence is coded as a template; each field is coded by a model chosen by its
name (dates as numbers, Message-IDs as references to earlier messages,
References/In-Reply-To against the parent message, Subject against the
parent's subject, Received dates relative to the Date field, address lists via
their folding rule, Enron's derived X- fields via the fields they repeat, and
recency lists for everything else).  Bodies are split into MIME parts;
quoted-printable and base64 parts are decoded when re-encoding reproduces the
original exactly.  Messages whose content already occurred are coded as a
reference plus a per-field difference mask.  An Outlook .msg file is split
into its compound-file skeleton and its MAPI property streams (emz_msg.py),
a PST or OST store into its skeleton and its data blocks (emz_pst.py); the
property values of both are coded by property tag.  All decisions go to separate
streams, which the context-mixing coder `emcm` compresses.  Anything that does
not fit is stored verbatim, so the result is always byte-exact.
"""
import os, sys, re, struct, hashlib, subprocess, tempfile, shutil, time, base64, binascii, zlib
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qp import qp_decode, qp_encode, QP_VARIANTS
from emz_msg import (CFB_MAGIC, split as msg_split, skeleton_decode, fill as msg_fill, rtf_split, rtf_join,
                     rtf_markup_split, rtf_markup_join)
from emz_pst import (PST_MAGIC, split as pst_split, skeleton_decode as pst_skeleton_decode, fill as pst_fill,
                     xblock, slblock, hn_split, hn_join, hid_ref)

EMCM = os.environ.get("EMCM", os.path.join(os.path.dirname(os.path.abspath(__file__)), "emcm"))
MAGIC = b"EMZ4"
KIND_CORPUS, KIND_MBOX, KIND_DIR, KIND_FILE = 0, 1, 2, 3
KIND_NAMES = {KIND_CORPUS: "corpus file", KIND_MBOX: "mbox", KIND_DIR: "directory", KIND_FILE: "single message"}
CHUNK = int(os.environ.get("EMZ_CHUNK", str(1 << 31)))   # streams are split into parts of at most 2 GiB

# --------------------------------------------------------------------------
# byte streams
class WStream:
    def __init__(self): self.b = bytearray()
    def byte(self, v): self.b.append(v)
    def uint(self, v):                       # LEB128
        while v >= 0x80:
            self.b.append((v & 0x7f) | 0x80); v >>= 7
        self.b.append(v)
    def sint(self, v): self.uint((v << 1) if v >= 0 else ((-v << 1) - 1))
    def text(self, s): self.b += s; self.b.append(10)          # s: bytes without '\n'
    def blob(self, s): self.b += s
    def text_end(self, s):               # any bytes; 00 is escaped as 00 01, the end mark is 00 00
        self.b += s.replace(b"\0", b"\0\1"); self.b += b"\0\0"

class RStream:
    def __init__(self, data): self.b = data; self.p = 0
    def byte(self):
        v = self.b[self.p]; self.p += 1; return v
    def uint(self):
        v = 0; sh = 0
        while True:
            c = self.b[self.p]; self.p += 1
            v |= (c & 0x7f) << sh; sh += 7
            if c < 0x80: return v
    def sint(self):
        u = self.uint(); return (u >> 1) if not (u & 1) else -((u + 1) >> 1)
    def text(self):
        i = self.b.index(10, self.p); s = bytes(self.b[self.p:i]); self.p = i + 1; return s
    def until_end(self):
        """Text written by WStream.text_end: bytes up to the end mark."""
        b = self.b; out = []; p = self.p
        while True:
            i = b.index(0, p)
            out.append(b[p:i])
            if b[i + 1] == 0: self.p = i + 2; return b"\0".join(out)
            p = i + 2
    def blob(self, n):
        s = bytes(self.b[self.p:self.p + n]); self.p += n; return s

STREAMS = ["path", "kind", "dupref", "diff", "tmpl_r", "tmpl_l",
           "hdr_r", "hdr_l", "sig_r", "sig_l", "addr_r", "xal_r", "subj_r",
           "id_r", "mid1", "mid2", "date", "dsec", "dfmt_r", "rdate",
           "seg", "bodyref", "body", "bin", "raw", "env_r", "env_l", "env_d",
           "msg_r", "msg_skel", "msg_str", "msg_bin", "msg_prop", "msg_rtf", "msg_rtfm",
           "pst_skel", "pst_hn", "pst_blk"]

# --------------------------------------------------------------------------
class Recency:
    """Per-context move-to-front list.  A value is coded as its rank (one byte)
    in its context's list, or as an escape followed by a newline-terminated
    literal.  Values must not contain b'\\n'."""
    def __init__(self, k=8):
        self.k = k; self.d = {}
    def code(self, enc, ctx, rs, ls, value=None):
        lst = self.d.get(ctx)
        if lst is None:
            lst = self.d[ctx] = []
        if enc:
            try: r = lst.index(value)
            except ValueError: r = self.k
            rs.byte(r)
            if r == self.k: ls.text(value)
        else:
            r = rs.byte()
            value = lst[r] if r < self.k else ls.text()
        if r < len(lst): del lst[r]
        lst.insert(0, value)
        if len(lst) > self.k: lst.pop()
        return value

# --------------------------------------------------------------------------
# dates
MONTHS = [b"Jan", b"Feb", b"Mar", b"Apr", b"May", b"Jun", b"Jul", b"Aug", b"Sep", b"Oct", b"Nov", b"Dec"]
DOWS = [b"Mon", b"Tue", b"Wed", b"Thu", b"Fri", b"Sat", b"Sun"]
NAMED_ZONES = {b"UT": 0, b"GMT": 0, b"UTC": 0, b"Z": 0, b"EST": -300, b"EDT": -240, b"CST": -360,
               b"CDT": -300, b"MST": -420, b"MDT": -360, b"PST": -480, b"PDT": -420}
DATE_RE = re.compile(rb"^(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun), )?(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
                     rb"(\d{4}) (\d\d):(\d\d)(?::(\d\d))? ([+-]\d{4}|[A-Z]{1,4})(?: (\([^()\n]{1,40}\)))?$")

def days_from_civil(y, m, d):           # proleptic Gregorian -> days since 1970-01-01
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468

def civil_from_days(z):
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return y + (m <= 2), m, d

ENV_RE = re.compile(rb"^From (\S*)( +)(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
                    rb"( \d|\d\d?) (\d\d):(\d\d):(\d\d)( [+-]\d{4})? (\d{4})( [+-]\d{4})?(.*)$", re.S)

def render_envelope(sender, sep, t, style, zone1, zone2, rest):
    """mbox `From ` line (without its line end) for a time t in seconds;
    style: day padded with a space (0), with a zero (1), or not at all (2)."""
    days, sec = divmod(t, 86400)
    y, m, d = civil_from_days(days)
    day = (b"%2d", b"%02d", b"%d")[style] % d
    return b"From %s%s%s %s %s %02d:%02d:%02d%s %d%s%s" % (sender, sep, DOWS[(days + 3) % 7], MONTHS[m - 1], day,
                                                          sec // 3600, sec // 60 % 60, sec % 60, zone1, y, zone2, rest)

def parse_date(v):
    """RFC 5322 date -> (local minutes, seconds, fmt) if it re-serialises
    byte-exactly; fmt = (flags, zone, comment)."""
    m = DATE_RE.match(v)
    if not m: return None
    dow, day, mon, yr, hh, mi, ss, zone, cmt = m.groups()
    if zone[:1] not in b"+-" and zone not in NAMED_ZONES: return None
    d = int(day)
    if not 1 <= d <= 31 or int(hh) > 23 or int(mi) > 59 or (ss is not None and int(ss) > 59): return None
    if cmt and b"\x00" in cmt: return None
    days = days_from_civil(int(yr), MONTHS.index(mon) + 1, d)
    flags = (dow is not None) | (day[:1] == b"0") << 1 | (ss is not None) << 2
    r = ((days * 24 + int(hh)) * 60 + int(mi), int(ss) if ss else 0, (flags, zone, cmt or b""))
    return r if format_date(*r) == v else None

def format_date(lmins, sec, fmt):
    flags, zone, cmt = fmt
    days, r = divmod(lmins, 1440)
    y, mo, d = civil_from_days(days)
    out = (DOWS[(days + 3) % 7] + b", ") if flags & 1 else b""
    out += (b"%02d" % d if flags & 2 else b"%d" % d) + b" " + MONTHS[mo - 1] + b" %04d %02d:%02d" % (y, r // 60, r % 60)
    if flags & 4: out += b":%02d" % sec
    out += b" " + zone
    if cmt: out += b" " + cmt
    return out

def zone_minutes(zone):
    if zone[:1] in b"+-":
        o = int(zone[1:3]) * 60 + int(zone[3:5]); return o if zone[:1] == b"+" else -o
    return NAMED_ZONES[zone]

def utc_seconds(p):
    lmins, sec, (flags, zone, cmt) = p
    return (lmins - zone_minutes(zone)) * 60 + sec

def fmt_bytes(fmt):
    flags, zone, cmt = fmt
    return bytes([48 + flags]) + zone + b"\x00" + cmt

def fmt_from_bytes(b):
    zone, _, cmt = b[1:].partition(b"\x00")
    return (b[0] - 48, zone, cmt)

# --------------------------------------------------------------------------
# addresses (JavaMail folding, Enron) and display-name lists
def fold_addrs(addrs, eol, used=0):
    out = []
    for i, a in enumerate(addrs):
        if i:
            out.append(b", "); used += 2
        if used + len(a) > 76:
            out.append(eol + b"\t"); used = 8
        out.append(a); used += len(a)
    return b"".join(out)

def split_addrs(v, eol):
    if not v: return None
    u = v.replace(eol + b"\t", b"")
    if b"\n" in u or b"\r" in u: return None
    addrs = u.split(b", ")
    return addrs if fold_addrs(addrs, eol) == v else None

def split_xlist(v):
    """Split a display-name list at top-level ', ' (outside quotes / <>)."""
    ents = []; start = 0; i = 0; n = len(v); q = False; ang = 0
    while i < n:
        c = v[i]
        if c == 34: q = not q
        elif not q and c == 60: ang += 1
        elif not q and c == 62 and ang: ang -= 1
        elif not q and not ang and c == 44 and i + 1 < n and v[i + 1] == 32:
            ents.append(v[start:i]); start = i + 2; i += 2; continue
        i += 1
    ents.append(v[start:])
    return ents

def split_xlist_angle(v):
    parts = v.split(b">, ")
    return [p + b">" for p in parts[:-1]] + [parts[-1]]

# --------------------------------------------------------------------------
# message parsing
NAME_RE = re.compile(rb"^[!-9;-~]+$")
ID_FIELDS = (b"message-id", b"in-reply-to", b"references")
GLOBAL_CTX = {b"mime-version", b"content-type", b"content-transfer-encoding", b"precedence",
              b"mailing-list", b"list-id", b"list-post", b"list-help", b"list-unsubscribe",
              b"list-subscribe", b"list-archive", b"list-owner", b"reply-to"}
SENDER_CTX = {b"autocrypt", b"user-agent", b"x-mailer", b"organization", b"openpgp", b"x-pgp-key",
              b"face", b"x-face", b"x-operating-system", b"content-language", b"x-pgp-sig", b"x-mailer-version"}
ADDR_FIELDS = {b"from", b"to", b"cc", b"bcc", b"sender", b"delivered-to", b"return-path", b"x-original-to"}
VOLATILE = {b"message-id", b"x-folder", b"x-origin", b"x-filename", b"received", b"x-received",
            b"delivered-to", b"return-path", b"x-original-to", b"status", b"x-status",
            b"x-mozilla-status", b"x-mozilla-status2", b"x-mozilla-keys", b"x-vm-v5-data",
            b"x-uid", b"x-keywords", b"envelope-to", b"x-gm-thrid", b"x-gmail-labels", b"x-uidl",
            b"lines", b"content-length", b"x-ms-reactions"}
SUBJ_PFX = re.compile(rb"^(?:\s*(?:(?:re|fw|fwd|aw|sv|antw|wg|tr)\s*(?:\[\d+\])?\s*:|\[[^\]\x02]{1,60}\]))*\s*", re.I)
MID_JAVAMAIL = re.compile(rb"^<([1-9]\d*|0)\.([1-9]\d*)\.JavaMail\.evans@thyme>$")
MID_TOKEN = re.compile(rb"^<[^<>\s\x02]+>$")
IDLIST_SPLIT = re.compile(rb"(<[^<>]*>)")
B64RUN = re.compile(rb"[A-Za-z0-9+/]{16,}={0,2}")
SIG_TS = re.compile(rb"((?:^|[;\s])[tx]=)([1-9]\d{8,10})(?=[;\s]|$)")
DOMAIN = re.compile(rb"@([A-Za-z0-9.-]+)")

def is_sig(lname):
    return (b"dkim" in lname or lname.startswith(b"arc-") or lname.startswith(b"x-google-") or
            lname.startswith(b"x-gm-") or lname.startswith(b"x-ms-") or lname.startswith(b"x-microsoft-") or
            lname.startswith(b"x-forefront-") or lname in (b"authentication-results", b"received-spf",
                                                            b"x-received"))

MODELLED = ({b"message-id", b"in-reply-to", b"references", b"date", b"received", b"x-received", b"subject",
             b"x-from", b"x-to", b"x-cc", b"x-folder", b"x-origin", b"x-filename"} | ADDR_FIELDS | GLOBAL_CTX | SENDER_CTX)

def is_generic(lname):
    """True for fields that code_field has no model for (see code_generic)."""
    return lname not in MODELLED and not is_sig(lname)

ANGLE_ADDR = re.compile(rb"<([^<>\s]+@[^<>\s]+)>")
GEN_MARK = re.compile(rb"\x06(.)", re.S)
WS = re.compile(rb"\s+")
WSP_RUN = re.compile(rb"[ \t]+")

def dkim_body_hash(v, body, eol):
    """The bh= value a DKIM/ARC signature with tag list v carries if it was
    computed over this body (RFC 6376 sections 3.4.3-3.4.5 and 3.7), or None
    if the tags name no supported hash."""
    t = {}
    for part in v.split(b";"):
        k, eq, val = part.partition(b"=")
        if eq: t[WS.sub(b"", k)] = WS.sub(b"", val)
    c = t.get(b"c", b"simple/simple").lower().split(b"/")
    a = t.get(b"a", b"").lower()
    if a.endswith(b"-sha256"): h = hashlib.sha256
    elif a.endswith(b"-sha1"): h = hashlib.sha1
    else: return None
    b = body if eol == b"\r\n" else body.replace(b"\n", b"\r\n")
    relaxed = len(c) > 1 and c[1] == b"relaxed"
    if relaxed:
        b = b"\r\n".join(WSP_RUN.sub(b" ", ln).rstrip(b" ") for ln in b.split(b"\r\n"))
    n = len(b)
    while n >= 2 and b[n - 2:n] == b"\r\n": n -= 2
    b = b[:n] + b"\r\n" if n or not relaxed else b""
    if b"l" in t:
        if not t[b"l"].isdigit(): return None
        b = b[:int(t[b"l"])]
    return base64.b64encode(h(b).digest())

def parse_message(msg):
    """-> (crlf, [(name, sp, value)], body) or None.  Header lines end with
    the message's line ending; continuation lines stay inside the value."""
    i = msg.find(b"\n")
    if i <= 0: return None
    crlf = msg[i - 1:i] == b"\r"
    eol = b"\r\n" if crlf else b"\n"
    j = msg.find(eol + eol)
    if j < 0: return None
    hdr, body = msg[:j], msg[j + 2 * len(eol):]
    if crlf:
        z = hdr.replace(b"\r\n", b"")
        if b"\r" in z or b"\n" in z: return None
    elif b"\r\n" in hdr: return None
    if b"\x01" in hdr or b"\x02" in hdr: return None
    fields = []
    for ln in hdr.split(eol):
        if ln[:1] in (b" ", b"\t"):
            if not fields: return None
            fields[-1][2] += eol + ln
            continue
        k = ln.find(b":")
        if k <= 0 or not NAME_RE.match(ln[:k]): return None
        rest = ln[k + 1:]
        if rest[:1] == b" ": fields.append([ln[:k], 0, rest[1:]])
        else: fields.append([ln[:k], 1, rest])
    return crlf, fields, body

def render_message(crlf, tmpl, vals, body):
    eol = b"\r\n" if crlf else b"\n"
    out = [n + (b": " if sp == 0 else b":") + v for (n, sp), v in zip(tmpl, vals)]
    return eol.join(out) + eol + eol + body

# --------------------------------------------------------------------------
# MIME body segmentation
CT_BOUNDARY = re.compile(rb'boundary\s*=\s*(?:"([^"\r\n]{1,200})"|([^\s;"]{1,200}))', re.I)
B64_LINE = re.compile(rb"^[A-Za-z0-9+/]*={0,2}$")

def header_params(raw_hdr):
    """Content-Type / -Transfer-Encoding of a MIME part header block."""
    ct = cte = b""
    for m in re.finditer(rb"(?im)^(content-type|content-transfer-encoding)[ \t]*:[ \t]*((?:.*)(?:\r?\n[ \t].*)*)", raw_hdr):
        if m.group(1).lower() == b"content-type": ct = m.group(2)
        else: cte = m.group(2).strip().lower()
    return ct, cte

def b64_try(data, eol):
    """base64 part body -> (decoded, line length, trailing eols) if it re-encodes exactly."""
    t = 0; d = data
    while d.endswith(eol) and t < 3: d = d[:-len(eol)]; t += 1
    if not d or (eol == b"\n" and b"\r" in d): return None
    lines = d.split(eol)
    L = len(lines[0])
    if L == 0 or L % 4 or any(len(x) != L for x in lines[:-1]) or len(lines[-1]) > L: return None
    if not all(B64_LINE.match(x) for x in lines): return None
    try: dec = base64.b64decode(b"".join(lines), validate=True)
    except (binascii.Error, ValueError): return None
    if b64_render(dec, L, t, eol) != data: return None
    return dec, L, t

def b64_render(dec, L, t, eol):
    e = base64.b64encode(dec)
    return eol.join(e[i:i + L] for i in range(0, len(e), L)) + eol * t

def mime_segments(ct, cte, body, eol, depth=0):
    """Split a body into segments (kind, payload):
       ('T', text), ('Q', (variant, decoded)), ('B', (L, t, decoded))."""
    m = CT_BOUNDARY.search(ct) if depth < 8 and ct[:10].lower() == b"multipart/" else None
    if m is None:
        if cte == b"quoted-printable":
            trail = body.endswith(b"=" + eol)
            core = body[:-1 - len(eol)] if trail else body
            dec = qp_decode(core, eol)
            if dec is not None:
                for k, var in enumerate(QP_VARIANTS):
                    if qp_encode(dec, eol, *var, trail) == body: return [("Q", (k + 8 * trail, dec))]
        elif cte == b"base64":
            r = b64_try(body, eol)
            if r is not None: return [("B", (r[1], r[2], r[0]))]
        return [("T", body)]
    delim = eol + b"--" + (m.group(1) or m.group(2))
    segs = []; pos = 0
    # the first delimiter may sit at the very start of the body (no preceding line end)
    at_start = body.startswith(delim[len(eol):])
    start = 0 if at_start else body.find(delim)
    if start < 0: return [("T", body)]
    first = True
    while True:
        dl = len(delim) - len(eol) if (first and at_start) else len(delim)
        first = False
        # text up to and including the delimiter line
        line_end = body.find(eol, start + dl)
        if line_end < 0: segs.append(("T", body[pos:])); return merge_text(segs)
        is_close = body[start + dl:start + dl + 2] == b"--"
        segs.append(("T", body[pos:line_end + len(eol)]))
        pos = line_end + len(eol)
        if is_close:
            if pos < len(body): segs.append(("T", body[pos:]))
            return merge_text(segs)
        # part header
        he = body.find(eol + eol, pos - len(eol))
        nxt = body.find(delim, pos - len(eol))
        if he < 0 or nxt < 0 or he >= nxt: segs.append(("T", body[pos:])); return merge_text(segs)
        hdr_end = he + 2 * len(eol)
        pct, pcte = header_params(body[pos:hdr_end])
        segs.append(("T", body[pos:hdr_end]))
        part = body[hdr_end:nxt] if nxt >= hdr_end else b""
        segs += mime_segments(pct, pcte, part, eol, depth + 1)
        pos = nxt; start = nxt

def first_delimiter(body, eol):
    """The boundary named by the body's first line that starts with '--'."""
    pos = 0 if body.startswith(b"--") else body.find(eol + b"--")
    if pos < 0: return None
    if pos or not body.startswith(b"--"): pos += len(eol)
    end = body.find(eol, pos)
    return body[pos + 2:end if end >= 0 else len(body)].rstrip(b" \t") or None

def looks_text(b):
    """Decoded base64 that is mostly printable text (patches, logs, HTML)."""
    if not b: return True
    s = b[:4096]
    bad = sum(1 for c in s if c < 9 or 13 < c < 32 or c == 127)
    return bad * 100 < len(s)

def merge_text(segs):
    out = []
    for s in segs:
        if s[0] == "T" and out and out[-1][0] == "T": out[-1] = ("T", out[-1][1] + s[1])
        elif s[0] != "T" or s[1]: out.append(s)
    return out

def render_segments(segs, eol):
    out = []
    for kind, p in segs:
        if kind == "T": out.append(p)
        elif kind == "Q": out.append(qp_encode(p[1], eol, *QP_VARIANTS[p[0] & 7], p[0] >= 8))
        else: out.append(b64_render(p[2], p[0], p[1], eol))
    return b"".join(out)

# --------------------------------------------------------------------------
# Outlook .msg streams (the compound file itself: emz_msg.py)
MSG_BIG = 4096        # values from this size on are deduplicated and coded into body / bin
MSG_KEY_IDS = {0x0037, 0x0E1D, 0x0070, 0x0C1A, 0x0C1F, 0x0042, 0x0065, 0x5D01, 0x5D02, 0x0E04, 0x0E03,
               0x1035, 0x0040, 0x0044, 0x0076, 0x0078, 0x3FFA, 0x1042, 0x1039, 0x3001, 0x3003, 0x39FE}
MSG_VARLEN = {0x001E, 0x001F, 0x0102, 0x000D, 0x0048}
MSG_KEYNORM = re.compile(r"#[0-9A-Fa-f]{8}")

def msg_tag(name):
    """Property tag of a `__substg1.0_TTTTYYYY` stream name, else None."""
    if name.startswith("__substg1.0_") and len(name) == 20:
        try: return int(name[12:], 16)
        except ValueError: pass
    return None

def msg_priority(path):
    """Coding order of the streams of a .msg: transport headers first (their
    fields become known strings), the strings other values repeat, other
    strings, binary values, property streams, named-property maps, then
    bodies (plain text before HTML before RTF) and attachments."""
    name = path.rpartition("/")[2]
    if name == "__properties_version1.0": return 5
    if path.startswith("__nameid_version1.0/"): return 6
    tag = msg_tag(name)
    if tag is None: return 7
    pid, typ = tag >> 16, tag & 0xFFFF
    if pid == 0x007D: return 0
    order = {0x1000: 8, 0x1013: 9, 0x1009: 10, 0x3701: 11}
    if pid in order: return order[pid]
    if typ in (0x1E, 0x1F): return 1 if pid in MSG_KEY_IDS else 2
    return 3 if typ == 0x0102 else 4

def msg_text(v):
    """UTF-16LE string value -> UTF-8 (lone surrogates kept), or None."""
    if len(v) % 2: return None
    try:
        t = v.decode("utf-16-le", "surrogatepass").encode("utf-8", "surrogatepass")
        return t if msg_untext(t) == v else None
    except UnicodeError:
        return None

def msg_untext(t):
    return t.decode("utf-8", "surrogatepass").encode("utf-16-le", "surrogatepass")

# --------------------------------------------------------------------------
class Codec:
    PM_M, PM_A, PM_MASK, PM_K = 2**31 - 1, 16807, (1 << 25) - 1, 64

    def __init__(self, enc, streams):
        self.enc = enc; self.S = streams
        self.prev_dir = None; self.prev_num = -1
        self.prev_n2 = 0; self.n1_list = []; self.pm_x = 0
        self.last_lmins = {}; self.last_lmins_global = 0; self.last_utc = 0
        self.content = {}; self.records = []; self.prev_ref = -1
        self.bodies = {}; self.bodylist = []; self.nbodies = 0
        self.id_events = []; self.id_last = {}; self.msginfo = {}
        self.rc_tmpl = Recency(254); self.rc_fname = Recency(254)
        self.rc = Recency(8); self.rc_big = Recency(64); self.rc_small = Recency(4)
        self.rc_name = Recency(4); self.rc_dfmt = Recency(16)
        self.env_style = 0
        self.msg_rc = {}; self.big = {}; self.nbig = 0; self.bigs = []
        self.prop_delta = {}; self.last_ft = 0; self.gval = {}; self.gvals = []

    # ---------------- path ----------------
    def code_path(self, path=None):
        S = self.S["path"]
        if self.enc:
            d, _, f = path.rpartition("/")
            m = re.match(r"^([1-9]\d*)\.$", f)
            num = int(m.group(1)) if m else None
            if d == self.prev_dir and num is not None and num == self.prev_num + 1: S.byte(0)
            elif d == self.prev_dir and num is not None: S.byte(1); S.uint(num)
            elif num is not None: S.byte(2); S.text(d.encode("latin-1")); S.uint(num)
            else: S.byte(3); S.text(path.encode("latin-1"))
            self.prev_dir, self.prev_num = d, (num if num is not None else -1)
            return path
        t = S.byte()
        if t == 0: num = self.prev_num + 1; d = self.prev_dir
        elif t == 1: num = S.uint(); d = self.prev_dir
        elif t == 2: d = S.text().decode("latin-1"); num = S.uint()
        else:
            path = S.text().decode("latin-1")
            self.prev_dir = path.rpartition("/")[0]; self.prev_num = -1
            return path
        self.prev_dir, self.prev_num = d, num
        return "%s/%d." % (d, num)

    # ---------------- helpers ----------------
    def lit(self, v):                    # header value -> literal-safe bytes
        return v.replace(self.eol, b"\x02")
    def unlit(self, v):
        return v.replace(b"\x02", self.eol)

    def rc_code(self, rc, ctx, rname, lname, v):
        """Recency-code a header value (with folding)."""
        if self.enc:
            rc.code(True, ctx, self.S[rname], self.S[lname], self.lit(v)); return v
        return self.unlit(rc.code(False, ctx, self.S[rname], self.S[lname]))

    def code_idtoken(self, tok=None):
        R = self.S["id_r"]
        if self.enc:
            i = self.id_last.get(tok)
            d = len(self.id_events) - i if i is not None else 0
            R.uint(d)
            if d == 0: self.S["hdr_l"].text(tok)
        else:
            d = R.uint()
            tok = self.id_events[len(self.id_events) - d] if d else self.S["hdr_l"].text()
        self.id_last[tok] = len(self.id_events); self.id_events.append(tok)
        return tok

    # ---------------- per message ----------------
    def code_message(self, path=None, msg=None, env=None):
        """Encoder: code one message (env: its mbox `From ` line or None).
        Decoder: return (path, message, env)."""
        path = self.code_path(path)
        if self.enc and msg[:8] == CFB_MAGIC:
            parts = msg_split(msg)
            if parts is not None:
                self.S["kind"].byte(3 | 8 * (env is not None))
                self.code_msg(path, msg, parts)
                if env is not None: self.code_envelope(env, None, None)
                return path, msg
        if self.enc and msg[:4] == PST_MAGIC:
            parts = pst_split(msg)
            if parts is not None:
                self.S["kind"].byte(6 | 8 * (env is not None))
                self.code_pst(path, msg, parts)
                if env is not None: self.code_envelope(env, None, None)
                return path, msg
        return self.code_rfc(path, msg, env)

    def code_rfc(self, path, msg=None, env=None):
        """An RFC 5322 message (or anything else, kept raw)."""
        K = self.S["kind"]
        folder = path.rpartition("/")[0]; user = path.split("/")[0]
        if self.enc:
            has_env = env is not None
            P = parse_message(msg)
            if P is None:
                K.byte(2 | 8 * has_env); self.S["raw"].uint(len(msg)); self.S["raw"].blob(msg)
                if has_env: self.code_envelope(env, None, None)
                return path, msg
            crlf, fields, body = P
            tmpl = tuple((n, sp) for n, sp, _ in fields); vals = [v for _, _, v in fields]
            key = hashlib.md5(b"\0".join([bytes([crlf])] + [n + bytes([1 + sp]) + (v if n.lower() not in VOLATILE else b"")
                                                            for (n, sp), v in zip(tmpl, vals)]) + b"\0" + body).digest()
            cid = self.content.get(key)
            K.byte(crlf * 4 + (1 if cid is not None else 0) + 8 * has_env)
        else:
            t = K.byte()
            has_env = t >= 8; t &= 7
            if t in (3, 6):
                m = self.code_msg(path) if t == 3 else self.code_pst(path)
                return path, m, (self.code_envelope(None, None, None) if has_env else None)
            if t == 2:
                n = self.S["raw"].uint(); m = self.S["raw"].blob(n)
                return path, m, (self.code_envelope(None, None, None) if has_env else None)
            crlf = t >> 2; cid = None; tmpl = vals = body = None
        self.eol = b"\r\n" if crlf else b"\n"
        self.cur = {"folder": folder, "user": user, "utc": None, "refs": None, "parent": None, "vals": {},
                    "body": body}
        copy = (cid is not None) if self.enc else (t & 3) == 1
        if copy:
            if self.enc:
                self.S["dupref"].sint(cid - (self.prev_ref + 1))
            else:
                cid = self.prev_ref + 1 + self.S["dupref"].sint()
            self.prev_ref = cid
            rtmpl, rvals, rbody = self.records[cid]
            tmpl = rtmpl; n = len(tmpl)
            D = self.S["diff"]
            if self.enc:
                same = [vals[i] == rvals[i] for i in range(n)]
                for b in range(0, n, 8):
                    D.byte(sum(1 << j for j in range(8) if b + j < n and not same[b + j]))
                out = [rvals[i] if same[i] else None for i in range(n)]
            else:
                same = []
                for b in range(0, n, 8):
                    x = D.byte(); same += [not (x >> j) & 1 for j in range(8)]
                out = [rvals[i] if same[i] else None for i in range(n)]
            todo = [i for i in range(n) if not same[i]]
            body = rbody
        else:
            tmpl = self.code_template(tmpl)
            n = len(tmpl); out = [None] * n; todo = list(range(n))
        lnames = [nm.lower() for nm, _ in tmpl]
        for i in range(n):
            if out[i] is not None: self.cur["vals"].setdefault(lnames[i], out[i])
            if out[i] is not None and lnames[i] == b"date" and self.cur["utc"] is None:
                p = parse_date(out[i])
                if p: self.cur["utc"] = utc_seconds(p)
        if copy and self.cur["vals"].get(b"references"):
            self.cur["refs"] = IDLIST_SPLIT.split(self.cur["vals"][b"references"])[1::2]
        # coding order: ids, then date and from, then the modelled fields in
        # template order, then the body, and last the fields without a model
        # of their own, whose values may refer to all of these (code_generic)
        early = (b"date", b"from")
        gen = [i for i in todo if is_generic(lnames[i])]
        order = ([i for i in todo if lnames[i] in ID_FIELDS] + [i for i in todo if lnames[i] in early] +
                 [i for i in todo if lnames[i] not in ID_FIELDS and lnames[i] not in early and not is_generic(lnames[i])])
        for i in order:
            v = self.code_field(lnames[i], vals[i] if self.enc else None)
            out[i] = v
            self.cur["vals"].setdefault(lnames[i], v)
        if not copy:
            body = self.code_body(self.cur["vals"].get(b"content-type", b""),
                                  self.cur["vals"].get(b"content-transfer-encoding", b""), body)
        if gen:
            known = self.known_strings(body)
            for i in gen:
                v = self.code_generic(lnames[i], vals[i] if self.enc else None, known)
                out[i] = v
                self.cur["vals"].setdefault(lnames[i], v)
        if not self.enc: self.finish_fields(lnames, out, order, body)
        # bookkeeping shared by encoder and decoder
        if not copy:
            if self.enc: self.content[key] = len(self.records)
            self.records.append((tmpl, out, body))
        dv = self.cur["vals"].get(b"date")
        p = parse_date(dv) if dv is not None else None
        if p:
            self.last_lmins[folder] = p[0]; self.last_lmins_global = p[0]; self.last_utc = utc_seconds(p)
        mid = self.cur["vals"].get(b"message-id")
        if mid is not None and MID_TOKEN.match(mid):
            s = self.cur["vals"].get(b"subject")
            self.msginfo[mid] = (SUBJ_PFX.sub(b"", self.lit(s), 1) if s is not None else None, self.cur["refs"])
        env = self.code_envelope(env, lnames, out) if has_env else None
        if self.enc: return path, msg
        return path, render_message(crlf, tmpl, out, body), env

    def code_envelope(self, env, lnames, out):
        """mbox `From <sender> <asctime>` line.  The time is coded in seconds
        relative to the topmost Received header (else Date), so the delivery
        delay and the delivering host's zone offset are what remains; the
        sender may refer to Return-Path, From or X-GM-THRID.  Lines of another
        shape are stored as they are."""
        E, L, D = self.S["env_r"], self.S["env_l"], self.S["env_d"]
        ref = self.last_utc; k = {}
        if lnames is not None:
            vals = {}
            for ln, v in zip(lnames, out): vals.setdefault(ln, v)
            rv = vals.get(b"received")
            p = parse_date(rv[rv.rfind(b";") + 1:].replace(self.eol, b"").strip()) if rv and b";" in rv else None
            if p: ref = utc_seconds(p)
            elif self.cur["utc"] is not None: ref = self.cur["utc"]
            def addr(v):
                v = (v or b"").replace(self.eol, b"").strip()
                m = ANGLE_ADDR.search(v)
                return m.group(1) if m else (v if b" " not in v else b"")
            for code, x in ((b"R", addr(vals.get(b"return-path"))), (b"F", addr(vals.get(b"from"))),
                            (b"T", (vals.get(b"x-gm-thrid") or b"").strip())):
                if len(x) >= 2 and not any(c in x for c in (b"\x06", b"\n", b"\x00")) and x not in k.values(): k[code] = x
        if self.enc:
            if env.endswith(b"\r\n"): line, eolc = env[:-2], 1
            elif env.endswith(b"\n"): line, eolc = env[:-1], 0
            else: line, eolc = env, 2
            m = ENV_RE.match(line) if not any(c in line for c in (b"\x00", b"\x06", b"\n")) else None
            t = None
            if m:
                day = m.group(5)
                style = 0 if day[:1] == b" " else 1 if day[:1] == b"0" else 2 if len(day) == 1 else self.env_style
                hh, mi, ss, d = int(m.group(6)), int(m.group(7)), int(m.group(8)), int(day)
                if hh < 24 and mi < 60 and ss < 60 and 1 <= d <= 31:
                    t = (days_from_civil(int(m.group(10)), MONTHS.index(m.group(4)) + 1, d) * 86400 +
                         hh * 3600 + mi * 60 + ss)
                    parts = (m.group(1), m.group(2), style, m.group(9) or b"", m.group(11) or b"", m.group(12))
                    if render_envelope(parts[0], parts[1], t, *parts[2:]) != line: t = None
            if t is None:
                E.byte(0); D.uint(len(env)); L.blob(env)
                return env
            E.byte(1)
            inv = {x: c for c, x in k.items()}
            alts = sorted(k.values(), key=len, reverse=True)
            rx = re.compile(b"|".join((rb"(?<!\d)" + re.escape(x) + rb"(?!\d)") if x.isdigit() else re.escape(x)
                                      for x in alts)) if alts else None
            snd = rx.sub(lambda mm: b"\x06" + inv[mm.group(0)], parts[0]) if rx else parts[0]
            self.rc_small.code(True, (b"envs",), E, L, snd)
            fmt = b"%d%d" % (style, eolc) + b"\x00".join(parts[1:2] + parts[3:])
            self.rc_dfmt.code(True, 3, E, L, fmt)
            D.sint(t - ref)
            self.env_style = style
            return env
        if not E.byte():
            return L.blob(D.uint())
        snd = GEN_MARK.sub(lambda mm: k[mm.group(1)], self.rc_small.code(False, (b"envs",), E, L))
        fmt = self.rc_dfmt.code(False, 3, E, L)
        style, eolc = fmt[0] - 48, fmt[1] - 48
        sep, z1, z2, rest = fmt[2:].split(b"\x00", 3)
        self.env_style = style
        line = render_envelope(snd, sep, ref + D.sint(), style, z1, z2, rest)
        return line + (b"\n", b"\r\n", b"")[eolc]

    def code_template(self, tmpl=None):
        S = self.S
        if self.enc:
            r = self.rc_tmpl.d.get(0, [])
            try: k = r.index(tmpl)
            except ValueError: k = self.rc_tmpl.k
            S["tmpl_r"].byte(k)
            if k == self.rc_tmpl.k:
                S["tmpl_l"].uint(len(tmpl))
                for nm, sp in tmpl:
                    self.rc_fname.code(True, 0, S["tmpl_r"], S["tmpl_l"], nm + (b"\x01" if sp else b""))
            lst = self.rc_tmpl.d.setdefault(0, [])
        else:
            lst = self.rc_tmpl.d.setdefault(0, [])
            k = S["tmpl_r"].byte()
            if k < self.rc_tmpl.k: tmpl = lst[k]
            else:
                n = S["tmpl_l"].uint(); t = []
                for _ in range(n):
                    x = self.rc_fname.code(False, 0, S["tmpl_r"], S["tmpl_l"])
                    t.append((x[:-1], 1) if x.endswith(b"\x01") else (x, 0))
                tmpl = tuple(t)
        if k < len(lst): del lst[k]
        lst.insert(0, tmpl)
        if len(lst) > self.rc_tmpl.k: lst.pop()
        return tmpl

    # ---------------- field models ----------------
    def code_field(self, ln, v=None):
        c = self.cur
        if ln == b"message-id": return self.code_msgid(v)
        if ln in (b"in-reply-to", b"references"): return self.code_idlist(ln, v)
        if ln == b"date": return self.code_date(v)
        if ln in (b"received", b"x-received"): return self.code_received(v)
        if ln == b"subject": return self.code_subject(v)
        if ln in ADDR_FIELDS: return self.code_addr(ln, v)
        if ln == b"x-from":
            return self.rc_code(self.rc_small, (b"xfrom", c["vals"].get(b"from")), "xal_r", "hdr_l", v)
        if ln in (b"x-to", b"x-cc"):
            return self.code_xlist(ln, v, c["vals"].get(b"to" if ln == b"x-to" else b"cc"))
        if ln == b"x-folder": return self.rc_code(self.rc, (ln, c["folder"]), "hdr_r", "hdr_l", v)
        if ln == b"x-origin": return self.rc_code(self.rc_small, (ln, c["user"]), "hdr_r", "hdr_l", v)
        if ln == b"x-filename": return self.rc_code(self.rc_small, (ln, c["vals"].get(b"x-folder")), "hdr_r", "hdr_l", v)
        if is_sig(ln): return self.code_sig(ln, v)
        if ln == b"content-type": return self.code_ctype(v)
        if ln in GLOBAL_CTX: return self.rc_code(self.rc, (ln,), "hdr_r", "hdr_l", v)
        if ln in SENDER_CTX: return self.rc_code(self.rc_small, (ln, c["vals"].get(b"from")), "hdr_r", "hdr_l", v)
        raise AssertionError("generic fields are coded by code_generic")

    def known_strings(self, body):
        """Values of this message that other header fields often repeat:
        code letter -> string.  Mail clients cache them in their own headers
        (Emacs VM's X-VM-v5-Data holds size, line count, date parts, sender,
        Message-ID, subject and parent ID); mbox writes Content-Length and
        Lines."""
        vals = self.cur["vals"]; k = {}
        def put(code, s):
            if s and len(s) >= 2 and b"\x06" not in s and b"\n" not in s and b"\r" not in s and s not in k.values():
                k[code] = s
        put(b"A", (vals.get(b"message-id") or b"").strip())
        put(b"B", (vals.get(b"in-reply-to") or b"").strip())
        if self.cur["refs"]: put(b"C", self.cur["refs"][-1])
        fr = vals.get(b"from")
        if fr:
            fr = fr.replace(self.eol, b"").strip()
            m = ANGLE_ADDR.search(fr)
            if m:
                put(b"D", m.group(1)); put(b"E", fr[:m.start()].strip().strip(b'"').strip())
            else:
                put(b"D", fr)
        s = vals.get(b"subject")
        if s:
            s = s.replace(self.eol, b"")
            put(b"F", s); put(b"G", SUBJ_PFX.sub(b"", s, 1))
        put(b"H", b"%d" % len(body)); put(b"I", b"%d" % body.count(b"\n"))
        dv = vals.get(b"date")
        m = DATE_RE.match(dv) if dv else None
        if m:
            put(b"K", b"%s:%s:%s" % (m.group(5), m.group(6), m.group(7)) if m.group(7) else None)
            put(b"L", m.group(8))
        alts = sorted(k.items(), key=lambda cs: -len(cs[1]))
        pat = b"|".join((rb"(?<!\d)" + re.escape(s) + rb"(?!\d)") if s.isdigit() else re.escape(s) for _, s in alts)
        rx = re.compile(pat + b"|\x06" if pat else b"\x06")
        return k, {s: c for c, s in k.items()}, rx

    def code_generic(self, ln, v, known):
        """Fields without a model of their own: move-to-front per (field,
        folder) over the value in which copies of the known strings are
        replaced by \\x06 + code letter (a literal \\x06 becomes \\x06\\x06).
        Such a value often repeats across messages even when the strings in
        it do not, and the literals left over are much shorter."""
        k, inv, rx = known
        ctx = (ln, self.cur["folder"])
        if self.enc:
            t = rx.sub(lambda m: b"\x06" + inv.get(m.group(0), b"\x06"), v)
            self.rc_code(self.rc, ctx, "hdr_r", "hdr_l", t)
            return v
        t = self.rc_code(self.rc, ctx, "hdr_r", "hdr_l", None)
        return GEN_MARK.sub(lambda m: b"\x06" if m.group(1) == b"\x06" else k[m.group(1)], t)

    def code_msgid(self, v=None):
        """Enron's JavaMail IDs via the Park-Miller generator and the clock;
        other IDs as tokens (back-references when seen before)."""
        S2 = self.S["mid2"]
        if self.enc:
            m = MID_JAVAMAIL.match(v)
            if m and int(m.group(1)) < (1 << 31):
                n1, n2 = int(m.group(1)), int(m.group(2))
                d = n2 - self.prev_n2; S2.uint(2 + ((d << 1) if d >= 0 else ((-d << 1) - 1)))
                self.prev_n2 = n2; self.n1_list.append(n1)
                self.id_last[v] = len(self.id_events); self.id_events.append(v)
                return v
            if MID_TOKEN.match(v):
                S2.uint(1); return self.code_idtoken(v)
            S2.uint(0); return self.rc_code(self.rc, (b"message-id",), "hdr_r", "hdr_l", v)
        u = S2.uint()
        if u == 0: return self.rc_code(self.rc, (b"message-id",), "hdr_r", "hdr_l", None)
        if u == 1: return self.code_idtoken()
        u -= 2; d = (u >> 1) if not (u & 1) else -((u + 1) >> 1)
        n2 = self.prev_n2 + d; self.prev_n2 = n2
        S1 = self.S["mid1"]; k = S1.uint()
        if k == 0:
            n1 = struct.unpack(">I", S1.blob(4))[0]; h = S1.byte()
            self.pm_x = (h << 25) | (n1 & self.PM_MASK)
        else:
            x = self.pm_x
            for _ in range(k): x = (self.PM_A * x) % self.PM_M
            self.pm_x = x; n1 = x & self.PM_MASK
        v = b"<%d.%d.JavaMail.evans@thyme>" % (n1, n2)
        self.id_last[v] = len(self.id_events); self.id_events.append(v)
        return v

    def finish_mid1(self):
        """Encoder post-pass for the JavaMail generator stream (needs lookahead)."""
        S1 = self.S["mid1"]; L = self.n1_list; x = 0
        A, M, MASK = self.PM_A, self.PM_M, self.PM_MASK
        for i, n1 in enumerate(L):
            k = 0
            if x:
                y = x
                for j in range(1, self.PM_K + 1):
                    y = (A * y) % M
                    if (y & MASK) == n1 and n1 <= MASK: k = j; break
            if k:
                S1.uint(k); x = y; continue
            h = 0
            if i + 1 < len(L):
                for hh in range(64):
                    z = (hh << 25) | (n1 & MASK)
                    if 0 < z < M and ((A * z) % M) & MASK == L[i + 1]: h = hh; break
            S1.uint(0); S1.blob(struct.pack(">I", n1)); S1.byte(h)
            x = (h << 25) | (n1 & MASK)
            if not (0 < x < M): x = 0

    def code_idlist(self, ln, v=None):
        """In-Reply-To / References: message-id tokens coded as back-references
        to earlier IDs; a References list is predicted as the parent's
        References plus the parent's ID (parent = its last token)."""
        R = self.S["id_r"]; c = self.cur
        if self.enc:
            parts = IDLIST_SPLIT.split(v); toks = parts[1::2]
            ok = bool(toks) and all(MID_TOKEN.match(t) for t in toks) and b"\x00" not in v
            R.uint(len(toks) if ok else 0)
            if not ok: return self.rc_code(self.rc, (ln, c["folder"]), "hdr_r", "hdr_l", v)
            self.rc_code(self.rc, (b"sep", ln, min(len(toks), 3)), "hdr_r", "hdr_l", b"\x00".join(parts[0::2]))
        else:
            n = R.uint()
            if n == 0: return self.rc_code(self.rc, (ln, c["folder"]), "hdr_r", "hdr_l", None)
            seps = self.rc_code(self.rc, (b"sep", ln, min(n, 3)), "hdr_r", "hdr_l", None).split(b"\x00")
            toks = [None] * n
        n = len(toks)
        if ln == b"references":
            toks[-1] = self.code_idtoken(toks[-1])
            par = self.msginfo.get(toks[-1])
            pred = par[1] if par is not None and par[1] is not None else []
            rest = toks[:-1] if self.enc else None
            if self.enc:
                hit = len(pred) == n - 1 and pred == rest
                R.byte(hit)
            else:
                hit = R.byte()
            if hit: toks[:-1] = pred
            else:
                for i in range(n - 1): toks[i] = self.code_idtoken(toks[i])
            c["refs"] = toks
            if c["parent"] is None: c["parent"] = toks[-1]
        else:
            pred = [c["refs"][-1]] if c["refs"] else None
            if self.enc:
                hit = pred is not None and toks == pred
                R.byte(hit)
            else:
                hit = R.byte()
            if hit: toks = list(pred)
            else:
                for i in range(n): toks[i] = self.code_idtoken(toks[i])
            if ln == b"in-reply-to": c["parent"] = toks[0]
        if self.enc: return v
        out = [seps[0]]
        for i, t in enumerate(toks): out += [t, seps[i + 1]]
        return b"".join(out)

    def code_date(self, v=None):
        S, S2 = self.S["date"], self.S["dsec"]; c = self.cur
        ref = self.last_lmins.get(c["folder"], self.last_lmins_global)
        if self.enc:
            p = parse_date(v)
            if p is None:
                S2.byte(255); self.S["hdr_l"].text(self.lit(v)); return v
            lmins, sec, fmt = p
            S2.byte(sec); S.sint(lmins - ref)
            self.rc_dfmt.code(True, 0, self.S["dfmt_r"], self.S["hdr_l"], fmt_bytes(fmt))
        else:
            sec = S2.byte()
            if sec == 255: return self.unlit(self.S["hdr_l"].text())
            lmins = ref + S.sint()
            fmt = fmt_from_bytes(self.rc_dfmt.code(False, 0, self.S["dfmt_r"], self.S["hdr_l"]))
            p = (lmins, sec, fmt); v = format_date(*p)
        if c["utc"] is None: c["utc"] = utc_seconds((lmins, sec, fmt))
        return v

    def code_received(self, v=None):
        """'<route>; <date>': the route by recency/literal, the date as seconds
        relative to the message's Date."""
        R = self.S["rdate"]; c = self.cur
        ref = c["utc"] if c["utc"] is not None else self.last_utc
        k = c.setdefault("nrecv", 0); c["nrecv"] = k + 1
        if self.enc:
            i = v.rfind(b";")
            p = None
            if i >= 0:
                tail = v[i + 1:]; pre = tail[:len(tail) - len(tail.lstrip())]
                p = parse_date(tail[len(pre):]) if pre else None
            if p is None:
                R.byte(0); return self.rc_code(self.rc, (b"received", k), "hdr_r", "hdr_l", v)
            R.byte(1)
            self.rc_code(self.rc, (b"received", k), "hdr_r", "hdr_l", v[:i])
            self.rc_code(self.rc_small, (b"rpre",), "hdr_r", "hdr_l", pre)
            R.sint(utc_seconds(p) - ref)
            self.rc_dfmt.code(True, 1, self.S["dfmt_r"], self.S["hdr_l"], fmt_bytes(p[2]))
            return v
        if R.byte() == 0: return self.rc_code(self.rc, (b"received", k), "hdr_r", "hdr_l", None)
        head = self.rc_code(self.rc, (b"received", k), "hdr_r", "hdr_l", None)
        pre = self.rc_code(self.rc_small, (b"rpre",), "hdr_r", "hdr_l", None)
        utc = ref + R.sint()
        fmt = fmt_from_bytes(self.rc_dfmt.code(False, 1, self.S["dfmt_r"], self.S["hdr_l"]))
        t = utc + zone_minutes(fmt[1]) * 60
        return head + b";" + pre + format_date(t // 60, t % 60, fmt)

    def code_sig(self, ln, v=None):
        """DKIM/ARC-style fields: a skeleton (tags, selectors, folding) that
        repeats per signer, plus the random base64 runs and timestamps.  A
        body hash (bh=) that matches the stored body is not coded at all
        (mode 2): the decoder leaves a \\x05 placeholder and fills it in once
        the body is known (finish_sigs)."""
        R = self.S["sig_r"]; c = self.cur
        ref = c["utc"] if c["utc"] is not None else self.last_utc
        fr = c["vals"].get(b"from"); dm = DOMAIN.search(fr) if fr else None
        ctx = (ln, dm.group(1).lower() if dm else None)
        if self.enc:
            if b"\x03" in v or b"\x04" in v or b"\x05" in v:
                R.byte(0); return self.rc_code(self.rc_small, (ln,), "sig_r", "sig_l", v)
            blobs = []; nums = []
            sk = B64RUN.sub(lambda m: blobs.append(m.group(0)) or b"\x03", v)
            sk = SIG_TS.sub(lambda m: nums.append(int(m.group(2))) or (m.group(1) + b"\x04"), sk)
            mode = 1
            if sk.count(b"bh=\x03") == 1:
                k = sk.count(b"\x03", 0, sk.index(b"bh=\x03") + 3)
                if blobs[k] == dkim_body_hash(v, c["body"], self.eol):
                    mode = 2; del blobs[k]
            R.byte(mode)
            self.rc_code(self.rc_small, ctx, "sig_r", "hdr_l", sk)
            for x in nums: self.S["rdate"].sint(x - ref)
            for b in blobs: self.S["sig_l"].text(b)
            return v
        mode = R.byte()
        if not mode: return self.rc_code(self.rc_small, (ln,), "sig_r", "sig_l", None)
        sk = self.rc_code(self.rc_small, ctx, "sig_r", "hdr_l", None)
        bhp = sk.index(b"bh=\x03") + 3 if mode == 2 else -1
        out = bytearray()
        for i, ch in enumerate(sk):
            if ch == 3: out += b"\x05" if i == bhp else self.S["sig_l"].text()
            elif ch == 4: out += b"%d" % (ref + self.S["rdate"].sint())
            else: out.append(ch)
        return bytes(out)

    def finish_fields(self, lnames, out, idx, body):
        """Decoder: fill in what code_sig and code_ctype left to the body."""
        for i in idx:
            v = out[i]; ln = lnames[i]
            if ln == b"content-type" and b"\x06" in v:
                out[i] = GEN_MARK.sub(lambda m: b"\x06" if m.group(1) == b"\x06" else first_delimiter(body, self.eol), v)
            elif b"\x05" in v and is_sig(ln):
                out[i] = v.replace(b"\x05", dkim_body_hash(v, body, self.eol))
            if self.cur["vals"].get(ln) is v: self.cur["vals"][ln] = out[i]

    def code_ctype(self, v=None):
        """Content-Type: a multipart boundary that the body's first delimiter
        line repeats is replaced by \\x06b and taken from the body on decoding
        (finish_fields); a literal \\x06 is stuffed as \\x06\\x06."""
        if self.enc:
            t = v.replace(b"\x06", b"\x06\x06")
            m = CT_BOUNDARY.search(v)
            if m and b"\x06" not in v and first_delimiter(self.cur["body"], self.eol) == (m.group(1) or m.group(2)):
                g = 1 if m.group(1) is not None else 2
                t = v[:m.start(g)] + b"\x06b" + v[m.end(g):]
            self.rc_code(self.rc, (b"content-type",), "hdr_r", "hdr_l", t)
            return v
        return self.rc_code(self.rc, (b"content-type",), "hdr_r", "hdr_l", None)

    def code_subject(self, v=None):
        """prefix (Re:, [list]) + base; the base is predicted from the parent
        message's subject, else from recently seen bases."""
        R = self.S["subj_r"]; c = self.cur
        par = self.msginfo.get(c["parent"]) if c["parent"] is not None else None
        pb = par[0] if par is not None else None
        if self.enc:
            lv = self.lit(v); base = SUBJ_PFX.sub(b"", lv, 1); pfx = lv[:len(lv) - len(base)]
            hit = pb is not None and pb == base
            R.byte(hit)
            self.rc_big.code(True, (b"spfx",), self.S["subj_r"], self.S["hdr_l"], pfx)
            if not hit: self.rc_big.code(True, (b"sbase",), self.S["subj_r"], self.S["hdr_l"], base)
            return v
        hit = R.byte()
        pfx = self.rc_big.code(False, (b"spfx",), self.S["subj_r"], self.S["hdr_l"])
        base = pb if hit else self.rc_big.code(False, (b"sbase",), self.S["subj_r"], self.S["hdr_l"])
        return self.unlit(pfx + base)

    def code_addr(self, ln, v=None):
        R = self.S["addr_r"]; c = self.cur
        if ln == b"bcc" and c["vals"].get(b"cc") is not None:
            if self.enc:
                same = v == c["vals"][b"cc"]; R.byte(same)
                if same: return v
            elif R.byte(): return c["vals"][b"cc"]
        if self.enc:
            a = split_addrs(v, self.eol)
            lit = b", ".join(a) if a is not None else b"\x01" + self.lit(v)
            self.rc.code(True, (ln, c["folder"]), R, self.S["hdr_l"], lit)
            return v
        lit = self.rc.code(False, (ln, c["folder"]), R, self.S["hdr_l"])
        if lit[:1] == b"\x01": return self.unlit(lit[1:])
        return fold_addrs(lit.split(b", "), self.eol)

    def code_xlist(self, ln, v=None, addrv=None):
        """Enron X-To / X-cc: one display name per address of To / Cc, each
        predicted from the names seen earlier with that address."""
        A = self.S["xal_r"]
        addrs = split_addrs(addrv, self.eol) if addrv else None
        if self.enc:
            if addrs is not None:
                for mode, splitter in ((1, split_xlist), (2, split_xlist_angle)):
                    ents = splitter(v)
                    if len(ents) == len(addrs):
                        A.byte(mode)
                        for a, e in zip(addrs, ents):
                            self.rc_name.code(True, a, A, self.S["hdr_l"], self.lit(e))
                        return v
            A.byte(0)
            return self.rc_code(self.rc_small, (ln, addrv), "xal_r", "hdr_l", v)
        if A.byte():
            return b", ".join(self.unlit(self.rc_name.code(False, a, A, self.S["hdr_l"])) for a in addrs)
        return self.rc_code(self.rc_small, (ln, addrv), "xal_r", "hdr_l", None)

    # ---------------- body ----------------
    def code_body(self, ct, cte, body=None):
        """Body: exact repeat of an earlier body, or MIME segments (text,
        decoded quoted-printable, decoded base64)."""
        R, G = self.S["bodyref"], self.S["seg"]
        eol = self.eol
        if self.enc:
            h = hashlib.md5(body).digest()
            bid = self.bodies.get(h)
            if bid is not None:
                R.uint(self.nbodies - bid); return body
            R.uint(0)
            ct = ct.replace(eol, b" "); cte = cte.strip().lower()
            segs = mime_segments(ct, cte, body, eol)
            if render_segments(segs, eol) != body: segs = [("T", body)]
            G.uint(len(segs))
            for kind, p in segs:
                if kind == "T":
                    # CRLF text is stored with LF when that is reversible
                    lf = eol == b"\r\n" and b"\r" not in p.replace(b"\r\n", b"") and b"\n" not in p.replace(b"\r\n", b"")
                    t = p.replace(b"\r\n", b"\n") if lf else p
                    G.byte(0 + lf); self.S["body"].text_end(t)
                elif kind == "Q":
                    G.byte(2); G.byte(p[0]); self.S["body"].text_end(p[1])
                else:
                    L, t, dec = p
                    txt = looks_text(dec)
                    G.byte(4 + txt); G.uint(L // 4); G.byte(t)
                    if txt: self.S["body"].text_end(dec)
                    else: G.uint(len(dec)); self.S["bin"].blob(dec)
            self.bodies[h] = self.nbodies; self.nbodies += 1; self.bodylist.append(None)
            return body
        r = R.uint()
        if r: return self.bodylist[self.nbodies - r]
        out = []
        for _ in range(G.uint()):
            kind = G.byte()
            if kind <= 1:
                t = self.S["body"].until_end()
                out.append(t.replace(b"\n", b"\r\n") if kind == 1 else t)
            elif kind == 2:
                var = G.byte(); dec = self.S["body"].until_end()
                out.append(qp_encode(dec, eol, *QP_VARIANTS[var & 7], var >= 8))
            else:
                L = G.uint() * 4; t = G.byte()
                dec = self.S["body"].until_end() if kind == 5 else self.S["bin"].blob(G.uint())
                out.append(b64_render(dec, L, t, eol))
        body = b"".join(out)
        self.bodylist.append(body); self.nbodies += 1
        return body

    # ---------------- Outlook .msg ----------------
    def code_msg(self, path, msg=None, parts=None):
        """Compound-file message (emz_msg.py): the skeleton, then every
        stream with the model for its property tag, in msg_priority order."""
        if self.enc:
            sk, cfb, contents = parts
            self.S["msg_skel"].blob(sk)
        else:
            Sk = self.S["msg_skel"]
            zeroed, cfb, Sk.p = skeleton_decode(Sk.b, Sk.p)
            contents = [None] * len(cfb.streams)
        sizes = {st[1]: st[2] for st in cfb.streams}
        self.mvals = []; self.mpos = {}; self.mknown = []; self.mrx = None
        for k in sorted(range(len(cfb.streams)), key=lambda k: (msg_priority(cfb.streams[k][1]), k)):
            _, spath, size, _ = cfb.streams[k]
            contents[k] = self.code_msg_stream(path, spath, size, sizes, contents[k])
        return msg if self.enc else msg_fill(zeroed, cfb, contents)

    def code_msg_stream(self, path, spath, size, sizes, v):
        if not size: return b""
        name = spath.rpartition("/")[2]
        if name == "__properties_version1.0":
            return self.code_props(spath, size, sizes, v)
        return self.code_value(path, spath, msg_tag(name), size, v)

    def code_value(self, path, key, tag, size, v):
        """One property value of a .msg or PST message; tag None if not
        known.  key selects the recency list."""
        if not size: return b""
        pid, typ = (tag >> 16, tag & 0xFFFF) if tag is not None else (None, None)
        if pid == 0x007D and typ in (0x1E, 0x1F):
            return self.code_transport(path, typ, size, v)
        if size >= MSG_BIG or pid in (0x1000, 0x1009, 0x1013, 0x3701):
            return self.code_msg_big(path, pid, typ, size, v)
        v = self.code_msg_small(key, typ, size, v)
        if typ in (0x1E, 0x1F):
            t = msg_text(v) if typ == 0x1F else v
            if t is not None: self.add_known(t.rstrip(b"\0"))
        return v

    def add_known(self, t):
        """Strings that later values of this .msg may contain (msg_sub)."""
        for x in (t, t.upper() if b"@" in t or b"/O=" in t.upper() else t):
            if 4 <= len(x) <= 1024 and b"\x06" not in x and x not in self.mknown and len(self.mknown) < 240:
                self.mknown.append(x); self.mrx = None

    def msg_sub(self, t):
        """Copies of known strings -> \x06 + index byte (literal \x06 -> \x06\x06)."""
        if self.mrx is None:
            self.mcode = {x: bytes([6, 0x10 + i]) for i, x in enumerate(self.mknown)}
            pat = b"|".join(re.escape(x) for x in sorted(self.mknown, key=len, reverse=True))
            self.mrx = re.compile(pat + b"|\x06" if pat else b"\x06")
        code = self.mcode
        return self.mrx.sub(lambda m: code.get(m.group(0), b"\x06\x06"), t)

    def msg_unsub(self, t):
        known = self.mknown
        return GEN_MARK.sub(lambda m: b"\x06" if m.group(1) == b"\x06" else known[m.group(1)[0] - 0x10], t)

    def code_msg_small(self, spath, typ, size, v):
        """Value below MSG_BIG: copy of an earlier value of this .msg,
        recency rank among the last values of the same property, copy of an
        earlier value of at least 64 bytes anywhere in the archive, or literal
        (strings as UTF-8 with known strings replaced)."""
        R = self.S["msg_r"]
        lst = self.msg_rc.setdefault(MSG_KEYNORM.sub("#", spath), [])
        if self.enc:
            j = self.mpos.get(v)
            if j is not None:
                R.byte(1); R.uint(len(self.mvals) - 1 - j)
            elif v in lst:
                R.byte(2 + lst.index(v))
            elif len(v) >= 64 and hashlib.md5(v).digest() in self.gval:
                R.byte(12); R.uint(len(self.gval) - 1 - self.gval[hashlib.md5(v).digest()])
            else:
                t = msg_text(v) if typ == 0x1F else None
                if t is not None:
                    R.byte(10); self.S["msg_str"].text_end(self.msg_sub(t))
                else:
                    t = self.msg_sub(v)
                    if typ == 0x1E or t != v.replace(b"\x06", b"\x06\x06"):
                        R.byte(11); self.S["msg_str"].text_end(t)
                    else:
                        R.byte(0); self.S["msg_bin"].blob(v)
        else:
            m = R.byte()
            if m == 1: v = self.mvals[len(self.mvals) - 1 - R.uint()]
            elif m < 10 and m >= 2: v = lst[m - 2]
            elif m == 10: v = msg_untext(self.msg_unsub(self.S["msg_str"].until_end()))
            elif m == 11: v = self.msg_unsub(self.S["msg_str"].until_end())
            elif m == 12: v = self.gvals[len(self.gvals) - 1 - R.uint()]
            else: v = self.S["msg_bin"].blob(size)
        if len(v) >= 64:
            h = hashlib.md5(v).digest()
            if h not in self.gval:
                self.gval[h] = len(self.gval)
                if not self.enc: self.gvals.append(v)
        if v not in self.mpos:
            self.mpos[v] = len(self.mvals); self.mvals.append(v)
        if v in lst: lst.remove(v)
        lst.insert(0, v); del lst[8:]
        return v

    def code_msg_big(self, path, pid, typ, size, v):
        """Bodies, attachments and other large values: copy of an earlier
        one, compressed RTF as its text runs (into the body stream, after the
        plain and HTML bodies), markup and token choices, an attached .msg
        or MIME entity by its own models, text into the body stream (UTF-16
        as UTF-8, CRLF as LF when reversible), else bytes into bin."""
        R = self.S["msg_r"]
        if self.enc:
            h = hashlib.md5(v).digest(); j = self.big.get(h)
            if j is not None:
                R.byte(1); R.uint(self.nbig - 1 - j); return v
            self.big[h] = self.nbig; self.nbig += 1
            r = None
            if pid == 0x1009 and typ == 0x0102:
                try: r = rtf_split(v)
                except (IndexError, ValueError, struct.error): r = None
            if r is not None:
                sp = rtf_markup_split(r[0])
                if sp is not None:          # text runs next to the other bodies, markup on its own
                    R.byte(9); R.byte(sp[0]); self.S["msg_rtfm"].text_end(sp[1]); self.S["body"].text_end(sp[2])
                else:
                    R.byte(6); self.S["body"].text_end(r[0])
                R.uint(len(r[1])); self.S["msg_rtf"].blob(r[1])
                return v
            if pid == 0x3701 and typ == 0x0102:
                parts = msg_split(v) if v[:8] == CFB_MAGIC else None
                if parts is not None:                       # a .msg attached as a file
                    R.byte(8); self.code_msg(path, v, parts); return v
                if parse_message(v) is not None:            # a MIME entity (S/MIME smime.p7m, .eml)
                    R.byte(7); self.code_rfc(path, v, None); return v
            t = msg_text(v) if typ == 0x1F else None
            if t is not None: mode = 2
            elif typ == 0x1E or looks_text(v): mode, t = 4, v
            else:
                R.byte(0); self.S["bin"].blob(v); return v
            z = t.replace(b"\r\n", b"")
            lf = b"\r\n" in t and b"\r" not in z and b"\n" not in z
            R.byte(mode + lf); self.S["body"].text_end(t.replace(b"\r\n", b"\n") if lf else t)
            return v
        m = R.byte()
        if m == 1: return self.bigs[len(self.bigs) - 1 - R.uint()]
        if m == 0: v = self.S["bin"].blob(size)
        elif m in (6, 9):
            if m == 9:
                fl = R.byte(); mk = self.S["msg_rtfm"].until_end()
                t = rtf_markup_join(fl, mk, self.S["body"].until_end())
            else:
                t = self.S["body"].until_end()
            v = rtf_join(t, self.S["msg_rtf"].blob(R.uint()))
        elif m == 7: v = self.code_rfc(path)[1]
        elif m == 8: v = self.code_msg(path)
        else:
            t = self.S["body"].until_end()
            if m & 1: t = t.replace(b"\n", b"\r\n")
            v = msg_untext(t) if m < 4 else t
        self.bigs.append(v)
        return v

    def code_transport(self, path, typ, size, v):
        """PR_TRANSPORT_MESSAGE_HEADERS: coded as an RFC 5322 header block
        by the header field models; its fields become known strings."""
        R = self.S["msg_r"]
        if self.enc:
            t = msg_text(v) if typ == 0x1F else v
            M = None
            if t is not None:
                i = t.find(b"\n")
                eol = b"\r\n" if i > 0 and t[i - 1:i] == b"\r" else b"\n"
                suf = b"" if t.endswith(eol + eol) else eol if t.endswith(eol) else eol + eol
                M = t + suf
                if parse_message(M) is None or M[:8] == CFB_MAGIC: M = None
            if M is None:
                R.byte(0); return self.code_msg_big(path, 0x007D, typ, size, v)
            R.byte(1 + len(suf)); self.code_rfc(path, M, None)
        else:
            m = R.byte()
            if m == 0: return self.code_msg_big(path, 0x007D, typ, size, None)
            M = self.code_rfc(path)[1]
            t = M[:len(M) - (m - 1)]
            v = msg_untext(t) if typ == 0x1F else t
        vals = self.cur["vals"]; eol = self.eol
        for ln in (b"subject", b"thread-topic", b"message-id", b"in-reply-to"):
            x = (vals.get(ln) or b"").replace(eol, b"").strip()
            if x:
                self.add_known(x)
                if ln == b"subject": self.add_known(SUBJ_PFX.sub(b"", x, 1))
        for ln in (b"from", b"sender", b"reply-to", b"to", b"cc"):
            x = (vals.get(ln) or b"").replace(eol, b"")
            for a in ANGLE_ADDR.findall(x): self.add_known(a)
            for part in x.split(b","):
                nm = part.split(b"<")[0].strip().strip(b'"').strip()
                if nm: self.add_known(nm)
        return v

    def code_props(self, spath, size, sizes, v):
        """__properties_version1.0: header, then 16-byte entries (tag, flags,
        value), coded as columns.  The size field of a variable-length
        property is coded against the size of its stream, times against the
        previous time."""
        R, P = self.S["msg_r"], self.S["msg_prop"]
        storage = spath.rpartition("/")[0]
        pre = storage + "/" if storage else ""
        hb = 32 if not storage else 24 if storage.endswith("__substg1.0_3701000D") else 8
        if size < hb or (size - hb) % 16:
            if self.enc: R.byte(0); self.S["msg_bin"].blob(v); return v
            R.byte(); return self.S["msg_bin"].blob(size)
        n = (size - hb) // 16
        if self.enc:
            R.byte(1); P.blob(v[:hb])
            P.blob(b"".join(v[o:o + 4] for o in range(hb, size, 16)))
            P.blob(b"".join(v[o + 4:o + 8] for o in range(hb, size, 16)))
            head = v[:hb]; tags = [struct.unpack_from("<I", v, o)[0] for o in range(hb, size, 16)]
        else:
            R.byte(); head = P.blob(hb)
            tb = P.blob(4 * n); fb = P.blob(4 * n)
            tags = list(struct.unpack("<%dI" % n, tb))
        vals = []
        for k, tag in enumerate(tags):
            typ = tag & 0xFFFF
            o = hb + 16 * k + 8
            if typ in MSG_VARLEN or typ & 0x1000:
                sz = sizes.get(pre + "__substg1.0_%08X" % tag)
                pred = (sz + self.prop_delta.get(typ, 0)) & 0xFFFFFFFF if sz is not None else 0xFFFFFFFF
                if self.enc:
                    lo, hi = struct.unpack_from("<II", v, o)
                    P.blob(struct.pack("<II", (lo - pred) & 0xFFFFFFFF, hi))
                else:
                    d, hi = struct.unpack("<II", P.blob(8))
                    lo = (d + pred) & 0xFFFFFFFF
                if sz is not None: self.prop_delta[typ] = (lo - sz) & 0xFFFFFFFF
                vals.append(struct.pack("<II", lo, hi))
            elif typ == 0x0040:
                if self.enc:
                    ft = struct.unpack_from("<Q", v, o)[0]
                    P.blob(struct.pack("<Q", (ft - self.last_ft) & 0xFFFFFFFFFFFFFFFF))
                else:
                    ft = (struct.unpack("<Q", P.blob(8))[0] + self.last_ft) & 0xFFFFFFFFFFFFFFFF
                self.last_ft = ft; vals.append(struct.pack("<Q", ft))
            else:
                x = v[o:o + 8] if self.enc else P.blob(8)
                if self.enc: P.blob(x)
                vals.append(x)
        if self.enc: return v
        return head + b"".join(tb[4 * k:4 * k + 4] + fb[4 * k:4 * k + 4] + vals[k] for k in range(n))

    # ---------------- Outlook PST / OST ----------------
    def code_pst(self, path, data=None, parts=None):
        """Personal store (emz_pst.py): the skeleton (which keeps internal
        and opaque blocks in place), then the data blocks node by node in node
        B-tree order, then the blocks no node refers to."""
        if self.enc:
            sk, st, stored = parts
            self.S["pst_skel"].blob(sk)
        else:
            Sk = self.S["pst_skel"]
            zeroed, st, Sk.p = pst_skeleton_decode(Sk.b, Sk.p)
            stored = {}
        done = set()
        for bid in st.border:                  # internal and opaque blocks are in the skeleton
            if st.kept(bid):
                ib, cb, cbinf = st.blocks[bid]
                stored[bid] = bytes(st.zeroed[ib:ib + cb]); done.add(bid)
        self.pst = (st, stored, done)
        for nid, bdata, bsub, parent in st.nodes:
            self.mvals = []; self.mpos = {}; self.mknown = []; self.mrx = None
            self.code_pst_node(path, nid & 0xFFFFFFFF, bdata, bsub, 0)
        for bid in st.border:
            self.pst_raw(bid)
        return data if self.enc else pst_fill(zeroed, st, stored)

    def pst_raw(self, bid):
        """A block as it is (decoded from the permute encoding when it is a
        plain data block)."""
        st, stored, done = self.pst
        if bid in done: return
        done.add(bid)
        if self.enc:
            p = st.decode(bid, stored[bid])
            self.S["pst_blk"].blob(stored[bid] if p is None else p)
        else:
            b = self.S["pst_blk"].blob(st.blocks[bid][1])
            stored[bid] = b if st.opaque(bid) else st.encode(bid, b)

    def pst_tree(self, bid, depth=0):
        """Data tree -> its data blocks (internal blocks coded on the way),
        or None if it is not a valid tree."""
        st, stored, done = self.pst
        if not bid: return []
        if bid not in st.blocks or depth > 3: return None
        if not bid & 2: return [bid]
        self.pst_raw(bid)
        try: x = xblock(st.inner(bid, stored[bid]))
        except zlib.error: return None
        if x is None: return None
        out = []
        for c in x[1]:
            sub = self.pst_tree(c, depth + 1)
            if sub is None: return None
            out += sub
        return out

    def pst_subs(self, bid, depth=0):
        """Subnode tree -> [(nid, data bid, subnode bid)], or None."""
        st, stored, done = self.pst
        if not bid: return []
        if bid not in st.blocks or not bid & 2 or depth > 3: return None
        self.pst_raw(bid)
        try: x = slblock(st.inner(bid, stored[bid]))
        except zlib.error: return None
        if x is None: return None
        if x[0] == 0: return x[1]
        out = []
        for c in x[1]:
            sub = self.pst_subs(c, depth + 1)
            if sub is None: return None
            out += sub
        return out

    def code_pst_node(self, path, nid, bdata, bsub, depth):
        """A node: its data (heap-on-node pages, property values), then its
        subnodes: values of its properties in msg_priority order, then the
        others (tables, attachments, embedded messages) recursively."""
        data = self.pst_tree(bdata)
        subs = self.pst_subs(bsub)
        tags = self.code_pst_data(path, nid, data) if data else {}
        if not subs or depth > 8: return

        def order(k):
            t = tags.get(subs[k][0] & 0xFFFFFFFF)
            return (0, msg_priority("__substg1.0_%08X" % t), k) if t is not None and not subs[k][2] else (1, 0, k)
        for k in sorted(range(len(subs)), key=order):
            snid, sbd, sbs = subs[k]
            t = tags.get(snid & 0xFFFFFFFF)
            if t is not None and not sbs: self.code_pst_value(path, t, sbd)
            else: self.code_pst_node(path, snid & 0xFFFFFFFF, sbd, sbs, depth + 1)

    def pst_whole(self, data):
        """True if the data blocks can be coded as one value."""
        st, stored, done = self.pst
        return data and not any(b in done or st.opaque(b) for b in data) and len(set(data)) == len(data)

    def pst_put(self, data, v):
        """Decoder: plain node data -> stored blocks."""
        st, stored, done = self.pst
        q = 0
        for b in data:
            n = st.blocks[b][1]; stored[b] = st.encode(b, v[q:q + n]); q += n

    def code_pst_value(self, path, tag, bdata):
        """A property value that lives in a subnode."""
        st, stored, done = self.pst
        data = self.pst_tree(bdata)
        if not self.pst_whole(data):
            for b in data or []: self.pst_raw(b)
            return
        size = sum(st.blocks[b][1] for b in data)
        v = b"".join(st.decode(b, stored[b]) for b in data) if self.enc else None
        v = self.code_value(path, "pst/%08X" % tag, tag, size, v)
        if not self.enc: self.pst_put(data, v)
        done.update(data)

    def code_pst_data(self, path, nid, data):
        """Node data: heap-on-node pages (structure into pst_hn, then the
        allocations: a property context's B-tree first, its values by
        property tag in msg_priority order, other allocations as values whose
        kind (UTF-16 text or binary) is flagged), else one value.  Returns
        {subnode NID: property tag} for the values kept in subnodes."""
        st, stored, done = self.pst
        R, H = self.S["msg_r"], self.S["pst_hn"]
        if not self.pst_whole(data):
            for b in data: self.pst_raw(b)
            return {}
        sizes = [st.blocks[b][1] for b in data]
        if self.enc:
            plains = [st.decode(b, stored[b]) for b in data]
            pages = [hn_split(p, i) for i, p in enumerate(plains)]
            ok = all(pg is not None and hn_join(*pg) == p for pg, p in zip(pages, plains))
            R.byte(ok)
        else:
            ok = R.byte()
        if not ok:
            v = b"".join(plains) if self.enc else None
            v = self.code_value(path, "pst/node/%02X" % (nid & 0x1F), None, sum(sizes), v)
            if not self.enc: self.pst_put(data, v)
            done.update(data)
            return {}
        if self.enc:
            for hdr, nfree, allocs, gap, tail in pages:
                H.blob(hdr); H.uint(len(allocs)); H.uint(nfree)
                for a in allocs: H.uint(len(a))
                H.uint(len(gap)); H.blob(gap); H.uint(len(tail)); H.blob(tail)
            alens = [[len(a) for a in pg[2]] for pg in pages]
        else:
            pages, alens = [], []
            for i in range(len(data)):
                hdr = H.blob(12 if i == 0 else 66 if i >= 8 and (i - 8) % 128 == 0 else 2)
                n = H.uint(); nfree = H.uint()
                alens.append([H.uint() for _ in range(n)])
                gap = H.blob(H.uint()); tail = H.blob(H.uint())
                pages.append((hdr, nfree, None, gap, tail))
        avals = {}

        def valid(ref):
            return ref is not None and ref[0] < len(alens) and ref[1] < len(alens[ref[0]])

        def raw(ref):                       # an allocation that is structure
            if ref not in avals:
                if self.enc:
                    v = pages[ref[0]][2][ref[1]]; H.blob(v)
                else:
                    v = H.blob(alens[ref[0]][ref[1]])
                avals[ref] = v
            return avals[ref]
        tags, subtags = {}, {}
        hdr0 = pages[0][0]
        if hdr0[3] == 0xBC:                 # property context: BTH of (id, type, value or HNID)
            ref = hid_ref(struct.unpack_from("<I", hdr0, 4)[0])
            bh = raw(ref) if valid(ref) else b""
            if len(bh) == 8 and bh[:3] == b"\xb5\x02\x06" and bh[3] <= 4:
                recs, seen = [], set()

                def walk(hid, lev):
                    r = hid_ref(hid)
                    if not valid(r) or r in seen: return
                    seen.add(r); b = raw(r)
                    if lev:
                        for o in range(0, len(b) - 5, 6): walk(struct.unpack_from("<I", b, o + 2)[0], lev - 1)
                    else:
                        recs.extend(struct.unpack_from("<HHI", b, o) for o in range(0, len(b) - 7, 8))
                walk(struct.unpack_from("<I", bh, 4)[0], bh[3])
                for pid, typ, hnid in recs:
                    if typ in (0x0001, 0x0002, 0x0003, 0x0004, 0x000A, 0x000B): continue
                    r = hid_ref(hnid)
                    if valid(r): tags.setdefault(r, pid << 16 | typ)
                    elif hnid & 0x1F: subtags.setdefault(hnid, pid << 16 | typ)
        for r in sorted(tags, key=lambda r: (msg_priority("__substg1.0_%08X" % tags[r]), r)):
            if r not in avals:
                n = alens[r[0]][r[1]]
                avals[r] = self.code_value(path, "pst/%08X" % tags[r], tags[r], n,
                                           pages[r[0]][2][r[1]] if self.enc else None)
        for pi in range(len(alens)):
            for ai in range(len(alens[pi])):
                r = (pi, ai)
                if r in avals: continue
                if self.enc:
                    v = pages[pi][2][ai]; t = msg_text(v) if len(v) >= 4 else None
                    txt = t is not None and not any(c < 32 and c not in (9, 10, 13) for c in t)
                    R.byte(txt)
                else:
                    v = None; txt = R.byte()
                avals[r] = self.code_value(path, "pst/%02X/%d" % (hdr0[3], min(ai, 3)), 0x1F if txt else 0x0102,
                                           alens[pi][ai], v)
        if not self.enc:
            self.pst_put(data, b"".join(hn_join(pg[0], pg[1], [avals[(pi, ai)] for ai in range(len(alens[pi]))],
                                                pg[3], pg[4]) for pi, pg in enumerate(pages)))
        done.update(data)
        return subtags

# --------------------------------------------------------------------------
def iter_corpus(fn):
    with open(fn, "rb") as f:
        while True:
            p = f.readline()
            if not p: return
            n = int(f.readline())
            yield p[:-1].decode("latin-1"), f.read(n)

# --------------------------------------------------------------------------
# input forms: corpus file, mbox, directory tree, single message
def is_corpus(fn):
    """True if fn consists exactly of records `path\n`, `length\n`, bytes."""
    size = os.path.getsize(fn)
    if size == 0: return False
    with open(fn, "rb") as f:
        while f.tell() < size:
            if not f.readline().endswith(b"\n"): return False
            ln = f.readline()
            if not ln.endswith(b"\n") or not ln[:-1].isdigit(): return False
            end = f.tell() + int(ln)
            if end > size: return False
            f.seek(end)
    return True

def natural_key(name):
    return [(0, int(x), b"") if x.isdigit() else (1, 0, x) for x in re.split(rb"(\d+)", name)]

def walk_dir(root):
    """Regular files under root in natural path order, and the empty
    directories; paths are relative and '/'-separated (bytes)."""
    import stat
    root = os.fsencode(root)
    files, empty = [], []
    def visit(rel):
        full = os.path.join(root, rel) if rel else root
        names = sorted(os.listdir(full), key=natural_key)
        if not names and rel: empty.append(rel)
        for nm in names:
            r = rel + b"/" + nm if rel else nm
            st = os.lstat(os.path.join(root, r))
            if stat.S_ISDIR(st.st_mode): visit(r)
            elif stat.S_ISREG(st.st_mode): files.append(r)
            else: raise SystemExit("emz: not a regular file or directory: %s" % os.fsdecode(os.path.join(root, r)))
    visit(b"")
    return files, empty

def split_mbox(data):
    """mbox -> (From_ line with its line end, message) pairs: a message
    starts at every line that begins with 'From '."""
    starts = [0] + [m.start() + 1 for m in re.finditer(rb"\nFrom ", data)]
    for a, b in zip(starts, starts[1:] + [len(data)]):
        e = data.find(b"\n", a, b)
        e = b if e < 0 else e + 1
        yield data[a:e], data[e:b]

def safe_relpath(rel):
    """A relative path from an archive may not leave the output directory."""
    parts = rel.split(b"/")
    if rel.startswith(b"/") or any(p in (b"", b".", b"..") or b"\0" in p for p in parts):
        raise SystemExit("emz: refusing unsafe path in archive: %r" % rel)
    return rel

def detect_kind(path):
    if os.path.isdir(path): return KIND_DIR
    with open(path, "rb") as f: head = f.read(5)
    if head == b"From ": return KIND_MBOX
    return KIND_CORPUS if is_corpus(path) else KIND_FILE

class Hasher:
    """SHA-256 of the canonical serialisation of an input (its bytes for
    files; for a directory every file as `path\n`, `length\n`, bytes, then
    `\0D path\n` per empty directory)."""
    def __init__(self): self.h = hashlib.sha256()
    def add(self, b): self.h.update(b)
    def record(self, path, data): self.h.update(path + b"\n" + str(len(data)).encode() + b"\n"); self.h.update(data)
    def digest(self): return self.h.digest()

def iter_input(path, kind, H):
    """-> (path str, message, mbox From_ line or None) for every message,
    feeding the canonical serialisation into the hasher H."""
    if kind == KIND_CORPUS:
        for p, m in iter_corpus(path):
            H.record(p.encode("latin-1"), m); yield p, m, None
    elif kind == KIND_MBOX:
        with open(path, "rb") as f: data = f.read()
        H.add(data)
        for i, (env, m) in enumerate(split_mbox(data)): yield "mbox/%d." % (i + 1), m, env
    elif kind == KIND_FILE:
        with open(path, "rb") as f: data = f.read()
        H.add(data)
        yield os.path.basename(path), data, None
    else:
        files, _ = walk_dir(path)
        for r in files:
            with open(os.path.join(os.fsencode(path), r), "rb") as f: data = f.read()
            H.record(r, data); yield r.decode("latin-1"), data, None

# --------------------------------------------------------------------------
# container
def run_emcm(args):
    mode, src, dst = args
    t = time.time()
    env = dict(os.environ)
    if os.path.basename(src).startswith("body."): env["EMCM_TBMAX"] = os.environ.get("EMZ_BODY_TBITS", "27")
    subprocess.run([EMCM, mode, src, dst], check=True, env=env)
    return time.time() - t

def transform(src, tmp):
    kind = detect_kind(src)
    S = {n: WStream() for n in STREAMS}
    C = Codec(True, S)
    H = Hasher(); nmsg = 0
    for path, msg, env in iter_input(src, kind, H):
        C.code_message(path, msg, env); nmsg += 1
    meta = b""
    if kind == KIND_DIR:
        empty = walk_dir(src)[1]
        for r in empty: H.add(b"\0D " + r + b"\n")
        meta = b"\n".join(empty)
    C.finish_mid1()
    for n in STREAMS:
        with open(os.path.join(tmp, n + ".raw"), "wb") as f: f.write(S[n].b)
    with open(os.path.join(tmp, "info"), "wb") as f:
        f.write(struct.pack("<BI", kind, nmsg) + H.digest() + meta)

def untransform(tmp, dst):
    with open(os.path.join(tmp, "info"), "rb") as f: info = f.read()
    kind, nmsg = struct.unpack("<BI", info[:5]); digest = info[5:37]; meta = info[37:]
    S = {n: RStream(open(os.path.join(tmp, n + ".raw"), "rb").read()) for n in STREAMS}
    C = Codec(False, S)
    H = Hasher()
    if kind == KIND_DIR:
        root = os.fsencode(dst)
        if os.path.exists(root) and (not os.path.isdir(root) or os.listdir(root)):
            raise SystemExit("emz: %s exists and is not an empty directory" % dst)
        os.makedirs(root, exist_ok=True)
        for _ in range(nmsg):
            path, msg, _env = C.code_message()
            rel = safe_relpath(path.encode("latin-1"))
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f: f.write(msg)
            H.record(rel, msg)
        for r in (meta.split(b"\n") if meta else []):
            os.makedirs(os.path.join(root, safe_relpath(r)), exist_ok=True); H.add(b"\0D " + r + b"\n")
    else:
        with open(dst, "wb") as out:
            for _ in range(nmsg):
                path, msg, env = C.code_message()
                if kind == KIND_CORPUS:
                    rec = path.encode("latin-1") + b"\n" + str(len(msg)).encode() + b"\n"
                    out.write(rec); H.add(rec)
                elif env is not None:
                    out.write(env); H.add(env)
                out.write(msg); H.add(msg)
    if H.digest() != digest:
        raise SystemExit("emz: SHA-256 MISMATCH: the output differs from the original input")

def compress(src, archive, jobs=2, keep=None):
    t0 = time.time()
    tmp = keep or tempfile.mkdtemp(prefix="emz")
    os.makedirs(tmp, exist_ok=True)
    # the transform runs in a child process so its memory is released before entropy coding
    subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "T", src, tmp], check=True)
    t1 = time.time()
    parts = {}
    jobsl = []
    for n in STREAMS:
        raw = os.path.join(tmp, n + ".raw"); size = os.path.getsize(raw)
        k = (size + CHUNK - 1) // CHUNK
        parts[n] = k
        if k == 1:
            jobsl.append(("c", raw, os.path.join(tmp, n + ".emc.0")))
        elif k > 1:
            with open(raw, "rb") as f:
                for i in range(k):
                    with open(os.path.join(tmp, "%s.raw.%d" % (n, i)), "wb") as g: g.write(f.read(CHUNK))
                    jobsl.append(("c", os.path.join(tmp, "%s.raw.%d" % (n, i)), os.path.join(tmp, "%s.emc.%d" % (n, i))))
    jobsl.sort(key=lambda j: -os.path.getsize(j[1]))
    with ThreadPoolExecutor(jobs) as ex: list(ex.map(run_emcm, jobsl))
    with open(os.path.join(tmp, "info"), "rb") as f: info = f.read()
    with open(archive, "wb") as out:
        out.write(MAGIC + struct.pack("<I", len(info)) + info)
        for n in STREAMS:
            out.write(struct.pack("<B", parts[n]))
            for i in range(parts[n]):
                data = open(os.path.join(tmp, "%s.emc.%d" % (n, i)), "rb").read()
                out.write(struct.pack("<Q", len(data))); out.write(data)
    sizes = {n: (os.path.getsize(os.path.join(tmp, n + ".raw")),
                 sum(os.path.getsize(os.path.join(tmp, "%s.emc.%d" % (n, i))) for i in range(parts[n]))) for n in STREAMS}
    if not keep: shutil.rmtree(tmp)
    t2 = time.time()
    kind, nmsg = struct.unpack("<BI", info[:5])
    print(f"{KIND_NAMES[kind]}: messages {nmsg}  transform {t1 - t0:.1f}s  entropy coding {t2 - t1:.1f}s", file=sys.stderr)
    return sizes

def decompress(archive, dst, jobs=2):
    tmp = tempfile.mkdtemp(prefix="emzd")
    try:
        t0 = time.time()
        with open(archive, "rb") as f:
            if f.read(4) != MAGIC: raise SystemExit("emz: not an emz archive of this version")
            info = f.read(struct.unpack("<I", f.read(4))[0])
            with open(os.path.join(tmp, "info"), "wb") as g: g.write(info)
            jobsl = []; parts = {}
            for n in STREAMS:
                parts[n] = struct.unpack("<B", f.read(1))[0]
                for i in range(parts[n]):
                    ln = struct.unpack("<Q", f.read(8))[0]
                    with open(os.path.join(tmp, "%s.emc.%d" % (n, i)), "wb") as g: g.write(f.read(ln))
                    jobsl.append(("d", os.path.join(tmp, "%s.emc.%d" % (n, i)), os.path.join(tmp, "%s.raw.%d" % (n, i))))
        jobsl.sort(key=lambda j: -os.path.getsize(j[1]))
        with ThreadPoolExecutor(jobs) as ex: list(ex.map(run_emcm, jobsl))
        for n in STREAMS:
            with open(os.path.join(tmp, n + ".raw"), "wb") as g:
                for i in range(parts[n]):
                    with open(os.path.join(tmp, "%s.raw.%d" % (n, i)), "rb") as h: shutil.copyfileobj(h, g)
        t1 = time.time()
        subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "U", tmp, dst], check=True)
        print(f"entropy decoding {t1 - t0:.1f}s  inverse transform {time.time() - t1:.1f}s  (SHA-256 verified)",
              file=sys.stderr)
    finally:
        shutil.rmtree(tmp)

if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("c", "d", "T", "U"):
        raise SystemExit(__doc__)
    try:
        if sys.argv[1] == "T":
            transform(sys.argv[2], sys.argv[3])
        elif sys.argv[1] == "U":
            untransform(sys.argv[2], sys.argv[3])
        elif sys.argv[1] == "c":
            sizes = compress(sys.argv[2], sys.argv[3], keep=os.environ.get("EMZ_KEEP"))
            tot = 0
            for n, (r, c) in sorted(sizes.items(), key=lambda x: -x[1][1]):
                if r: print(f"{n:10s} raw {r:>12,}  coded {c:>11,}", file=sys.stderr)
                tot += c
            print(f"total coded {tot:,}  archive {os.path.getsize(sys.argv[3]):,}", file=sys.stderr)
        else:
            decompress(sys.argv[2], sys.argv[3])
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"emz: {os.path.basename(str(e.cmd[0]))} failed (exit {e.returncode})")
