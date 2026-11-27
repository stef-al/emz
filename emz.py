#!/usr/bin/env python3
"""emz -- structure-aware compressor for e-mail archives (maildir / Enron).

    emz.py c <corpus> <archive>     compress a canonical corpus file
    emz.py d <archive> <corpus>     decompress

The corpus is a sequence of messages (path, raw RFC-822 bytes).  Every message
is parsed into header fields + body; each field is coded by a model that knows
what the field is (dates and Message-IDs as numbers, folded address lists via
the folding rule, derived fields such as X-From/X-To/X-FileName via the field
they depend on, ...).  The models emit small decisions and residual literals
into separate streams that are entropy-coded by the context-mixing back end
`emcm`.  Messages whose content already occurred elsewhere in the archive are
coded as references.  Anything that does not fit the expected structure falls
back to a verbatim path, so the transform is always lossless.
"""
import os, sys, re, struct, hashlib, subprocess, tempfile, shutil, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qp import qp_decode, qp_encode

EMCM = os.environ.get("EMCM", os.path.join(os.path.dirname(os.path.abspath(__file__)), "emcm"))

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
    def text(self, s): self.b += s; self.b.append(10)          # s: bytes w/o '\n'
    def blob(self, s): self.b += s

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
    def blob(self, n):
        s = bytes(self.b[self.p:self.p + n]); self.p += n; return s

STREAMS = ["path", "kind", "dupref", "mid1", "mid2", "date", "dsec", "misc",
           "from_r", "from_l", "to_r", "to_l", "subj_r", "subj_l", "cc_r", "cc_l",
           "hdr_r", "hdr_l", "xfrom_r", "xfrom_l", "xto_r", "xto_l", "xcc_r", "xcc_l",
           "xbcc_r", "xbcc_l", "xal_r", "xal_l", "xfold_r", "xfold_l", "xorig_r", "xorig_l", "xfile_r", "xfile_l",
           "bodyref", "blen", "body", "raw"]

# --------------------------------------------------------------------------
# field models
class Recency:
    """Per-context move-to-front list of recent values.  A value is coded as
    its rank (one byte) in the list of its context, or as an escape followed
    by a literal."""
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

FIELDS = [b"Message-ID", b"Date", b"From", b"To", b"Subject", b"Cc", b"Mime-Version",
          b"Content-Type", b"Content-Transfer-Encoding", b"Bcc", b"X-From", b"X-To",
          b"X-cc", b"X-bcc", b"X-Folder", b"X-Origin", b"X-FileName"]
OPTIONAL = {b"To", b"Cc", b"Bcc"}

MID_RE = re.compile(rb"^<([1-9]\d*|0)\.([1-9]\d*)\.JavaMail\.evans@thyme>$")
DATE_RE = re.compile(rb"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([1-9]\d?) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) (\d\d):(\d\d):(\d\d) ([+-]\d{4}) \(([A-Z]{3})\)$")
MONTHS = [b"Jan", b"Feb", b"Mar", b"Apr", b"May", b"Jun", b"Jul", b"Aug", b"Sep", b"Oct", b"Nov", b"Dec"]
DOWS = [b"Mon", b"Tue", b"Wed", b"Thu", b"Fri", b"Sat", b"Sun"]
TZ = [(b"-0700", b"PDT"), (b"-0800", b"PST")]

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

def parse_date(v):
    """-> (minutes, seconds, tz index) if v is in the canonical format and
    re-serializes identically, else None."""
    m = DATE_RE.match(v)
    if not m: return None
    dow, day, mon, yr, hh, mi, ss, off, tzn = m.groups()
    try: tzi = TZ.index((off, tzn))
    except ValueError: return None
    y, mo, d = int(yr), MONTHS.index(mon) + 1, int(day)
    if not (1 <= d <= 31) or int(hh) > 23 or int(mi) > 59 or int(ss) > 59: return None
    days = days_from_civil(y, mo, d)
    mins = (days * 24 + int(hh)) * 60 + int(mi)
    res = (mins, int(ss), tzi)
    return res if format_date(*res) == v else None

def format_date(mins, ss, tzi):
    days, r = divmod(mins, 1440)
    y, mo, d = civil_from_days(days)
    dow = DOWS[(days + 3) % 7]
    return b"%s, %d %s %04d %02d:%02d:%02d %s (%s)" % (dow, d, MONTHS[mo - 1], y, r // 60, r % 60, ss,
                                                       TZ[tzi][0], TZ[tzi][1])

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
    """Split only after '>' (Outlook style 'Last, First <...>, ...')."""
    parts = v.split(b">, ")
    return [p + b">" for p in parts[:-1]] + [parts[-1]]

def fold_addrs(addrs, used=0):
    """JavaMail InternetAddress.toString(): ', '-separated, folded with
    CRLF TAB before an address that would pass column 76."""
    out = []
    for i, a in enumerate(addrs):
        if i:
            out.append(b", "); used += 2
        if used + len(a) > 76:
            out.append(b"\r\n\t"); used = 8
        out.append(a); used += len(a)
    return b"".join(out)

def split_addrs(v):
    """Return the address list if v is exactly the folded rendering of it."""
    if b"\n" in v.replace(b"\r\n\t", b""): return None
    if v == b"": return None
    addrs = v.replace(b"\r\n\t", b"").split(b", ")
    return addrs if fold_addrs(addrs) == v else None

# --------------------------------------------------------------------------
class Codec:
    def __init__(self, enc, streams):
        self.enc = enc; self.S = streams
        self.prev_dir = None; self.prev_num = -1
        self.prev_n2 = 0
        self.n1_list = []; self.pm_x = 0
        self.last_date = {}; self.last_date_global = 0
        self.content = {}          # content hash -> cid
        self.records = []          # cid -> dict of copied fields
        self.prev_ref = -1
        self.bodies = {}           # body hash -> body id
        self.nbodies = 0
        self.rc_from = Recency(8); self.rc_to = Recency(8); self.rc_subj = Recency(8); self.rc_cc = Recency(8)
        self.rc_hdr = Recency(4); self.rc_xfrom = Recency(4); self.rc_xto = Recency(4); self.rc_xcc = Recency(4)
        self.rc_xbcc = Recency(4); self.rc_name = Recency(4); self.rc_xfold = Recency(8); self.rc_xorig = Recency(4); self.rc_xfile = Recency(4)
        self.stats = {}

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

    # ---------------- header parsing (encoder only) ----------------
    @staticmethod
    def parse(msg):
        i = msg.find(b"\r\n\r\n")
        if i < 0: return None
        hdr, body = msg[:i], msg[i + 4:]
        if b"\r" in hdr.replace(b"\r\n", b"") or b"\n" in hdr.replace(b"\r\n", b""): return None
        if b"\x01" in hdr or b"\x02" in hdr: return None
        fields = []
        for ln in hdr.split(b"\r\n"):
            if ln[:1] in (b" ", b"\t"):
                if not fields: return None
                fields[-1][1] += b"\r\n" + ln
            else:
                k, sep, v = ln.partition(b": ")
                if not sep: return None
                fields.append([k, v])
        names = [k for k, _ in fields]
        has_to = b"To" in names; has_cc = b"Cc" in names
        expect = [f for f in FIELDS if f not in OPTIONAL or (f == b"To" and has_to) or (f in (b"Cc", b"Bcc") and has_cc)]
        if names != expect: return None
        F = dict((k, v) for k, v in fields)
        return F, has_to, has_cc, body

    # ---------------- per message ----------------
    def code_message(self, path=None, msg=None):
        path = self.code_path(path)
        K = self.S["kind"]
        if self.enc:
            p = self.parse(msg)
            if p is None:
                K.byte(2); self.S["raw"].uint(len(msg)); self.S["raw"].blob(msg)
                return path, msg
            F, has_to, has_cc, body = p
            key = hashlib.md5(b"\0".join([F.get(f, b"-") if f in F else b"<none>" for f in FIELDS
                                          if f not in (b"Message-ID", b"X-Folder", b"X-Origin", b"X-FileName")])
                              + b"\0" + body).digest()
            cid = self.content.get(key)
            if cid is not None:
                K.byte(1)
                self.S["dupref"].sint(cid - (self.prev_ref + 1))
                self.prev_ref = cid
                rec = self.records[cid]
            else:
                K.byte(0)
                rec = None
        else:
            t = K.byte()
            if t == 2:
                n = self.S["raw"].uint(); msg = self.S["raw"].blob(n)
                return path, msg
            F = {}
            if t == 1:
                cid = self.prev_ref + 1 + self.S["dupref"].sint()
                self.prev_ref = cid
                rec = self.records[cid]
            else:
                rec = None
        d = path.rpartition("/")[0]
        user = path.split("/")[0]
        # ---- Message-ID (always coded) ----
        self.code_mid(F)
        if rec is None:
            if self.enc:
                tmpl = (has_to, has_cc)
            else:
                tmpl = None
            rec = self.code_content(F, d, tmpl, body if self.enc else None)
            if self.enc:
                rec = {"mins": rec["mins"]}          # encoder needs only the date state
                self.content[key] = len(self.records)
            self.records.append(rec)
        else:
            # copy content fields; update date state
            self.last_date[d] = rec["mins"] if rec["mins"] is not None else self.last_date.get(d, self.last_date_global)
            if rec["mins"] is not None: self.last_date_global = rec["mins"]
        # ---- per-copy fields ----
        F[b"X-Folder"] = self.rc_xfold.code(self.enc, d, self.S["xfold_r"], self.S["xfold_l"], F.get(b"X-Folder"))
        F[b"X-Origin"] = self.rc_xorig.code(self.enc, user, self.S["xorig_r"], self.S["xorig_l"], F.get(b"X-Origin"))
        F[b"X-FileName"] = self.rc_xfile.code(self.enc, F[b"X-Folder"], self.S["xfile_r"], self.S["xfile_l"], F.get(b"X-FileName"))
        if self.enc:
            return path, msg
        # ---- rebuild message ----
        out = []
        for f in FIELDS:
            if f == b"To" and not rec["has_to"]: continue
            if f in (b"Cc", b"Bcc") and not rec["has_cc"]: continue
            v = F[f] if f in (b"Message-ID", b"X-Folder", b"X-Origin", b"X-FileName") else rec["F"][f]
            out.append(f + b": " + v)
        body = rec["body"]
        return path, b"\r\n".join(out) + b"\r\n\r\n" + body

    # Message-ID = <N1.N2.JavaMail.evans@thyme>.  N2 is a millisecond clock
    # (coded as a delta); N1 is the low 25 bits of the JVM's Park-Miller
    # generator x -> 16807 x mod (2^31-1), which advances once per message:
    # it is coded as the number of generator steps since the previous message.
    PM_M, PM_A, PM_MASK, PM_K = 2**31 - 1, 16807, (1 << 25) - 1, 64

    def code_mid(self, F):
        S2 = self.S["mid2"]
        if self.enc:
            m = MID_RE.match(F[b"Message-ID"])
            if m and int(m.group(1)) < (1 << 31):
                n1, n2 = int(m.group(1)), int(m.group(2))
                d = n2 - self.prev_n2; S2.uint(1 + ((d << 1) if d >= 0 else ((-d << 1) - 1)))
                self.prev_n2 = n2; self.n1_list.append(n1)
                return
            S2.uint(0); self.S["misc"].text(F[b"Message-ID"])
            return
        u = S2.uint()
        if u == 0:
            F[b"Message-ID"] = self.S["misc"].text(); return
        u -= 1; d = (u >> 1) if not (u & 1) else -((u + 1) >> 1)
        n2 = self.prev_n2 + d; self.prev_n2 = n2
        S1 = self.S["mid1"]
        k = S1.uint()
        if k == 0:
            n1 = struct.unpack(">I", S1.blob(4))[0]; h = S1.byte()
            self.pm_x = (h << 25) | (n1 & self.PM_MASK)
        else:
            x = self.pm_x
            for _ in range(k): x = (self.PM_A * x) % self.PM_M
            self.pm_x = x; n1 = x & self.PM_MASK
        F[b"Message-ID"] = b"<%d.%d.JavaMail.evans@thyme>" % (n1, n2)

    def finish_mid1(self):
        """Encoder post-pass: code the N1 sequence (needs one value of lookahead)."""
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

    def code_date(self, v, d):
        S, S2 = self.S["date"], self.S["dsec"]
        ref = self.last_date.get(d, self.last_date_global)
        if self.enc:
            p = parse_date(v)
            if p is None:
                S2.byte(255); self.S["misc"].text(v)
                return v, None
            mins, ss, tzi = p
            S2.byte(ss + 64 * tzi); S.sint(mins - ref)
        else:
            b = S2.byte()
            if b == 255: return self.S["misc"].text(), None
            ss, tzi = b % 64, b // 64
            mins = ref + S.sint()
            v = format_date(mins, ss, tzi)
        self.last_date[d] = mins; self.last_date_global = mins
        return v, mins

    def code_addrlist(self, v, rc, ctx, rname, lname):
        """Address list: if it is the canonical folded rendering of its list,
        code the unfolded list; else code raw value with a marker."""
        if self.enc:
            addrs = split_addrs(v)
            lit = b", ".join(addrs) if addrs is not None else b"\x01" + v.replace(b"\r\n", b"\x02")
            rc.code(True, ctx, self.S[rname], self.S[lname], lit)
            return v
        lit = rc.code(False, ctx, self.S[rname], self.S[lname])
        if lit[:1] == b"\x01": return lit[1:].replace(b"\x02", b"\r\n")
        return fold_addrs(lit.split(b", "))

    def code_text(self, v, rc, ctx, rname, lname):
        """Free-text header value (may contain folded continuation lines)."""
        if self.enc:
            rc.code(True, ctx, self.S[rname], self.S[lname], v.replace(b"\r\n", b"\x02"))
            return v
        return rc.code(False, ctx, self.S[rname], self.S[lname]).replace(b"\x02", b"\r\n")

    def code_xlist(self, v, addrv, rc, rname, lname):
        """X-To / X-cc: display-name list.  When it has one entry per address
        of the corresponding To / Cc list, each entry is predicted from the
        names previously seen with that address; otherwise the whole value is
        coded against the values previously seen with the same address list."""
        addrs = split_addrs(addrv) if addrv else None
        A = self.S["xal_r"]
        if self.enc:
            if addrs is not None:
                for mode, splitter in ((1, split_xlist), (2, split_xlist_angle)):
                    ents = splitter(v)
                    if len(ents) == len(addrs):
                        A.byte(mode)
                        for a, e in zip(addrs, ents):
                            self.rc_name.code(True, a, A, self.S["xal_l"], e)
                        return v
            A.byte(0)
            return self.code_text(v, rc, addrv, rname, lname)
        if A.byte():
            return b", ".join(self.rc_name.code(False, a, A, self.S["xal_l"]) for a in addrs)
        return self.code_text(None, rc, addrv, rname, lname)

    def code_content(self, F, d, tmpl, body):
        enc = self.enc
        K = self.S["kind"]
        if enc:
            has_to, has_cc = tmpl
            bcc_eq = has_cc and F[b"Bcc"] == F[b"Cc"]
            K.byte(has_to + 2 * has_cc + 4 * bcc_eq)
        else:
            t = K.byte(); has_to, has_cc, bcc_eq = t & 1, (t >> 1) & 1, (t >> 2) & 1
        G = {}
        G[b"Date"], mins = self.code_date(F.get(b"Date"), d)
        G[b"From"] = self.code_addrlist(F.get(b"From"), self.rc_from, d, "from_r", "from_l")
        if has_to:
            G[b"To"] = self.code_addrlist(F.get(b"To"), self.rc_to, d, "to_r", "to_l")
        G[b"Subject"] = self.code_text(F.get(b"Subject"), self.rc_subj, d, "subj_r", "subj_l")
        if has_cc:
            G[b"Cc"] = self.code_addrlist(F.get(b"Cc"), self.rc_cc, d, "cc_r", "cc_l")
            G[b"Bcc"] = G[b"Cc"] if bcc_eq else self.code_addrlist(F.get(b"Bcc"), self.rc_cc, d, "cc_r", "cc_l")
        for f in (b"Mime-Version", b"Content-Type", b"Content-Transfer-Encoding"):
            G[f] = self.code_text(F.get(f), self.rc_hdr, f, "hdr_r", "hdr_l")
        G[b"X-From"] = self.code_text(F.get(b"X-From"), self.rc_xfrom, G[b"From"], "xfrom_r", "xfrom_l")
        G[b"X-To"] = self.code_xlist(F.get(b"X-To"), G.get(b"To"), self.rc_xto, "xto_r", "xto_l")
        G[b"X-cc"] = self.code_xlist(F.get(b"X-cc"), G.get(b"Cc"), self.rc_xcc, "xcc_r", "xcc_l")
        G[b"X-bcc"] = self.code_text(F.get(b"X-bcc"), self.rc_xbcc, None, "xbcc_r", "xbcc_l")
        body = self.code_body(body)
        return {"F": G, "has_to": has_to, "has_cc": has_cc, "mins": mins, "body": body}

    def code_body(self, body):
        R = self.S["bodyref"]
        if self.enc:
            h = hashlib.md5(body).digest()
            bid = self.bodies.get(h)
            if bid is not None:
                R.uint(self.nbodies - bid)
                return body
            R.uint(0)
            if b"\r" not in body: mode, b = 0, body
            else:
                t = qp_decode(body) if b"=" in body else None
                if t is not None and qp_encode(t) == body: mode, b = 3, t
                elif b"\r" not in body.replace(b"\r\n", b"") and b"\n" not in body.replace(b"\r\n", b""):
                    mode, b = 1, body.replace(b"\r\n", b"\n")
                else: mode, b = 2, body
            self.S["blen"].uint(len(b) * 4 + mode)
            self.S["body"].blob(b)
            self.bodies[h] = self.nbodies; self.nbodies += 1
            return body
        r = R.uint()
        if r:
            return self.bodylist[self.nbodies - r]
        x = self.S["blen"].uint(); n, mode = x >> 2, x & 3
        b = self.S["body"].blob(n)
        body = b.replace(b"\n", b"\r\n") if mode == 1 else (qp_encode(b) if mode == 3 else b)
        self.bodylist.append(body); self.nbodies += 1
        return body

# --------------------------------------------------------------------------
def iter_corpus(fn):
    with open(fn, "rb") as f:
        while True:
            p = f.readline()
            if not p: return
            n = int(f.readline())
            yield p[:-1].decode("latin-1"), f.read(n)

def run_emcm(args):
    mode, src, dst = args
    t = time.time()
    env = dict(os.environ)
    if os.path.basename(src).startswith("body."): env["EMCM_TBMAX"] = os.environ.get("EMZ_BODY_TBITS", "27")
    subprocess.run([EMCM, mode, src, dst], check=True, env=env)
    return time.time() - t

def transform(corpus, tmp):
    S = {n: WStream() for n in STREAMS}
    C = Codec(True, S)
    nmsg = 0
    for path, msg in iter_corpus(corpus):
        C.code_message(path, msg); nmsg += 1
    C.finish_mid1()
    for n in STREAMS:
        with open(os.path.join(tmp, n + ".raw"), "wb") as f: f.write(S[n].b)
    with open(os.path.join(tmp, "nmsg"), "w") as f: f.write(str(nmsg))

def untransform(tmp, corpus):
    nmsg = int(open(os.path.join(tmp, "nmsg")).read())
    S = {n: RStream(open(os.path.join(tmp, n + ".raw"), "rb").read()) for n in STREAMS}
    C = Codec(False, S); C.bodylist = []
    with open(corpus, "wb") as out:
        for _ in range(nmsg):
            path, msg = C.code_message()
            out.write(path.encode("latin-1") + b"\n" + str(len(msg)).encode() + b"\n" + msg)

def compress(corpus, archive, jobs=2, keep=None):
    t0 = time.time()
    tmp = keep or tempfile.mkdtemp(prefix="emz")
    os.makedirs(tmp, exist_ok=True)
    # run the transform in a child process so its memory is released before entropy coding
    subprocess.run([sys.executable, os.path.abspath(__file__), "T", corpus, tmp], check=True)
    t1 = time.time()
    jobsl = [("c", os.path.join(tmp, n + ".raw"), os.path.join(tmp, n + ".emc")) for n in STREAMS]
    jobsl.sort(key=lambda j: -os.path.getsize(j[1]))
    with ThreadPoolExecutor(jobs) as ex: list(ex.map(run_emcm, jobsl))
    nmsg = int(open(os.path.join(tmp, "nmsg")).read())
    with open(archive, "wb") as out:
        out.write(b"EMZ2" + struct.pack("<I", nmsg))
        for n in STREAMS:
            data = open(os.path.join(tmp, n + ".emc"), "rb").read()
            out.write(struct.pack("<Q", len(data))); out.write(data)
    sizes = {n: (os.path.getsize(os.path.join(tmp, n + ".raw")), os.path.getsize(os.path.join(tmp, n + ".emc"))) for n in STREAMS}
    if not keep: shutil.rmtree(tmp)
    t2 = time.time()
    print(f"messages {nmsg}  transform {t1 - t0:.1f}s  entropy coding {t2 - t1:.1f}s", file=sys.stderr)
    return sizes

def decompress(archive, corpus, jobs=2):
    tmp = tempfile.mkdtemp(prefix="emzd")
    t0 = time.time()
    with open(archive, "rb") as f:
        assert f.read(4) == b"EMZ2"
        nmsg = struct.unpack("<I", f.read(4))[0]
        jobsl = []
        for n in STREAMS:
            ln = struct.unpack("<Q", f.read(8))[0]
            with open(os.path.join(tmp, n + ".emc"), "wb") as g: g.write(f.read(ln))
            jobsl.append(("d", os.path.join(tmp, n + ".emc"), os.path.join(tmp, n + ".raw")))
    with open(os.path.join(tmp, "nmsg"), "w") as g: g.write(str(nmsg))
    jobsl.sort(key=lambda j: -os.path.getsize(j[1]))
    with ThreadPoolExecutor(jobs) as ex: list(ex.map(run_emcm, jobsl))
    t1 = time.time()
    subprocess.run([sys.executable, os.path.abspath(__file__), "U", tmp, corpus], check=True)
    shutil.rmtree(tmp)
    print(f"entropy decoding {t1 - t0:.1f}s  inverse transform {time.time() - t1:.1f}s", file=sys.stderr)

if __name__ == "__main__":
    if sys.argv[1] == "T":
        transform(sys.argv[2], sys.argv[3]); sys.exit(0)
    if sys.argv[1] == "U":
        untransform(sys.argv[2], sys.argv[3]); sys.exit(0)
    if sys.argv[1] == "c":
        sizes = compress(sys.argv[2], sys.argv[3], keep=os.environ.get("EMZ_KEEP"))
        tot = 0
        for n, (r, c) in sorted(sizes.items(), key=lambda x: -x[1][1]):
            print(f"{n:10s} raw {r:>12,}  coded {c:>11,}", file=sys.stderr); tot += c
        print(f"total coded {tot:,}  archive {os.path.getsize(sys.argv[3]):,}", file=sys.stderr)
    else:
        decompress(sys.argv[2], sys.argv[3])
