"""Quoted-printable decode / canonical re-encode, used as a lossless transform.

qp_decode(body, eol) returns the decoded text with '\n' line ends (or None if
the body is not plain QP with `eol` line ends); qp_encode(text, eol, tabs)
re-encodes it.  emz stores the decoded text only when re-encoding reproduces
the original bytes exactly, so any encoder quirk not covered here simply
falls back to storing the body as is.
"""
import re
_qp_tok = re.compile(rb"=([0-9A-F]{2})")
_bad_eq = re.compile(rb"=(?![0-9A-F]{2})")

def qp_decode(body, eol=b"\r\n"):
    if eol == b"\r\n":
        if b"\r" in body.replace(b"\r\n", b"") or b"\n" in body.replace(b"\r\n", b""):
            return None
    elif b"\r" in body:
        return None
    lines = body.split(eol)
    out = []; cur = []
    for i, ln in enumerate(lines):
        if ln.endswith(b"=") and i < len(lines) - 1:
            cur.append(ln[:-1])
        else:
            cur.append(ln); out.append(b"".join(cur)); cur = []
    if cur: out.append(b"".join(cur))
    text = b"\n".join(out)
    if _bad_eq.search(text): return None
    return _qp_tok.sub(lambda m: bytes([int(m.group(1), 16)]), text)

def _enc_line(line, last, tabs, fit, wsall, maxlen=76):
    toks = []; n = len(line)
    ws = n                                   # start of trailing whitespace
    if not last:
        while ws > 0 and line[ws - 1] in (9, 32): ws -= 1
    for i, c in enumerate(line):
        end = (i == n - 1 or (wsall and i >= ws)) and not last
        if c == 61 or c > 126 or (c < 32 and c != 9) or (c == 9 and (tabs or end)):
            toks.append(b"=%02X" % c)
        elif c == 32 and end:
            toks.append(b"=20")
        else:
            toks.append(bytes([c]))
    out = []; cur = 0; buf = []
    rest = sum(map(len, toks))
    for t in toks:
        # fit=True: a remainder that fits in maxlen columns is not broken
        if not (fit and cur + rest <= maxlen) and cur + len(t) > maxlen - 1:
            out.append(b"".join(buf) + b"="); buf = []; cur = 0
        buf.append(t); cur += len(t); rest -= len(t)
    out.append(b"".join(buf))
    return out

def qp_encode(text, eol=b"\r\n", tabs=True, fit=False, wsall=False, trail=False):
    """tabs: every TAB as =09 (else only trailing ones); fit: a line of exactly
    76 columns is not broken; wsall: all trailing blanks of a line are encoded
    (else only the last); trail: the part ends with a soft line break."""
    ls = text.split(b"\n")
    out = []
    for i, l in enumerate(ls):
        out += _enc_line(l, i == len(ls) - 1, tabs, fit, wsall)
    return eol.join(out) + ((b"=" + eol) if trail else b"")

# variant index stored in the archive (bits: tabs, fit, wsall inverted for tabs);
# variant 0 is the encoder used in the Enron corpus
QP_VARIANTS = tuple((not (k & 1), bool(k & 2), bool(k & 4)) for k in range(8))
