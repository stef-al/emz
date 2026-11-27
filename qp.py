"""Quoted-printable decode / canonical re-encode, used as a lossless body transform."""
import re
HEX = b"0123456789ABCDEF"
_qp_tok = re.compile(rb"=([0-9A-F]{2})")

def qp_decode(body):
    """Return decoded text (LF line ends) or None if body is not plain QP with CRLF lines."""
    if b"\r" in body.replace(b"\r\n", b"") or b"\n" in body.replace(b"\r\n", b""):
        return None
    lines = body.split(b"\r\n")
    out = []; cur = []
    for i, ln in enumerate(lines):
        if ln.endswith(b"=") and i < len(lines) - 1:
            cur.append(ln[:-1])
        else:
            cur.append(ln); out.append(b"".join(cur)); cur = []
    if cur: out.append(b"".join(cur))
    text = b"\n".join(out)
    # every '=' must start a valid escape
    if re.search(rb"=(?![0-9A-F]{2})", text): return None
    return _qp_tok.sub(lambda m: bytes([int(m.group(1), 16)]), text)

def _enc_line(line, last, maxlen=76):
    toks = []
    n = len(line)
    for i, c in enumerate(line):
        if c == 61 or c == 9 or c > 126 or (c < 32):
            toks.append(b"=%02X" % c)
        elif c == 32 and i == n - 1 and not last:
            toks.append(b"=20")
        else:
            toks.append(bytes([c]))
    out = []; cur = 0; buf = []
    for t in toks:
        if cur + len(t) > maxlen - 1:
            out.append(b"".join(buf) + b"="); buf = []; cur = 0
        buf.append(t); cur += len(t)
    out.append(b"".join(buf))
    return b"\r\n".join(out)

def qp_encode(text):
    ls = text.split(b"\n")
    return b"\r\n".join(_enc_line(l, i == len(ls) - 1) for i, l in enumerate(ls))
