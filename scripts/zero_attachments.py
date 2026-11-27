#!/usr/bin/env python3
"""Set the attachment data of Outlook files to zero, keeping them valid, to
measure how well the message part compresses (results/msg_samples.txt,
results/pst_samples.txt).

    scripts/zero_attachments.py <in.msg|in.pst|in.ost> <out>

.msg: every __substg1.0_3701* stream is overwritten in place.  PST/OST: the
blocks of every PidTagAttachDataBinary value are zeroed, re-encoded, and
their trailers recomputed."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import emz, emz_msg, emz_pst  # noqa: E402

src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()
if data[:8] == emz_msg.CFB_MAGIC:
    out = bytearray(data)
    for _, path, size, segs in emz_msg.Compound(data).streams:
        if path.rpartition("/")[2].startswith("__substg1.0_3701"):
            for o, n in segs:
                out[o:o + n] = bytes(n)
    out = bytes(out)
else:
    sk, st, stored = emz_pst.split(data)
    att = set()
    orig = emz.Codec.code_pst_value

    def watch(self, path, tag, bdata):
        if tag == 0x37010102:
            att.update(self.pst_tree(bdata) or [])
        return orig(self, path, tag, bdata)
    emz.Codec.code_pst_value = watch
    emz.Codec(True, {n: emz.WStream() for n in emz.STREAMS}).code_message("x", data)
    new = dict(stored)
    for b in att:
        if not st.kept(b):
            new[b] = st.encode(b, bytes(st.blocks[b][1]))
    out = emz_pst.fill(st.zeroed, st, new)
open(dst, "wb").write(out)
