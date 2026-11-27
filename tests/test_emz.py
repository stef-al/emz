"""Tests for emz.  Run `make test` (builds ./emcm first).

Two sample corpora, each chosen to exercise every code path:

* tests/data/enron_sample.corpus -- 564 Enron messages (1.1 MB, 2000-2002,
  CRLF headers, JavaMail conversion): copies across folders and mailboxes,
  the verbatim fallback, quoted-printable and base64 bodies, non-ASCII bytes,
  non-canonical dates, folded subjects, Outlook-style display-name lists.
* tests/data/oss_sample.corpus -- 184 messages of the oss-security mailing
  list (1.2 MB, 2015-2026, LF): threads with References/In-Reply-To, DKIM and
  ARC signatures, Received chains, MIME multiparts with quoted-printable
  parts of every supported encoder variant and base64 text/binary parts.
"""
import os, random, re, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import emz  # noqa: E402
import qp   # noqa: E402

ENRON = os.path.join(ROOT, "tests", "data", "enron_sample.corpus")
OSS = os.path.join(ROOT, "tests", "data", "oss_sample.corpus")
EMCM = os.environ.get("EMCM", os.path.join(ROOT, "emcm"))


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def parsed(corpus):
    out = []
    for path, msg in emz.iter_corpus(corpus):
        p = emz.parse_message(msg)
        if p is not None:
            out.append((path, p))
    return out


def transform_roundtrip(msgs):
    """Run messages through the field models and back (no entropy coder)."""
    S = {n: emz.WStream() for n in emz.STREAMS}
    enc = emz.Codec(True, S)
    for i, m in enumerate(msgs):
        enc.code_message("box/inbox/%d." % (i + 1), m)
    enc.finish_mid1()
    dec = emz.Codec(False, {n: emz.RStream(bytes(S[n].b)) for n in emz.STREAMS})
    return S, [dec.code_message()[1] for _ in msgs]


class EmcmRoundTrip(unittest.TestCase):
    def roundtrip(self, data):
        with tempfile.TemporaryDirectory() as d:
            a, b, c = (os.path.join(d, x) for x in ("in", "out.emc", "back"))
            write(a, data)
            subprocess.run([EMCM, "c", a, b], check=True)
            subprocess.run([EMCM, "d", b, c], check=True)
            self.assertEqual(read(c), data)
            return os.path.getsize(b)

    def test_empty(self):
        self.roundtrip(b"")

    def test_random_bytes(self):
        rnd = random.Random(1)
        data = bytes(rnd.randrange(256) for _ in range(50000))
        self.assertLess(self.roundtrip(data), len(data) * 1.02)

    def test_repetitive_text(self):
        data = b"Please see the attached file.\n" * 3000 + bytes(range(256)) * 10
        self.assertLess(self.roundtrip(data), len(data) // 100)

    def test_quoted_reply_text(self):
        """Quoted text ('> ' prefixes, nested) must round-trip and be cheap
        thanks to the quote-aware match model."""
        rnd = random.Random(7)
        words = [bytes(rnd.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(rnd.randint(2, 9))) for _ in range(400)]
        orig = b"\n".join(b" ".join(rnd.choice(words) for _ in range(12)) for _ in range(300)) + b"\n"
        quoted = b"".join(b"> " + ln + b"\n" for ln in orig.split(b"\n")[:-1])
        nested = b"".join(b"> > " + ln + b"\n" for ln in orig.split(b"\n")[:-1])
        n_orig = self.roundtrip(orig)
        n_all = self.roundtrip(orig + quoted + nested)
        self.assertLess(n_all - n_orig, n_orig // 10)


class ArchiveRoundTrip(unittest.TestCase):
    def roundtrip(self, corpus, min_ratio):
        with tempfile.TemporaryDirectory() as d:
            arc, out = os.path.join(d, "s.emz"), os.path.join(d, "s.out")
            env = dict(os.environ, EMCM=EMCM)
            subprocess.run([sys.executable, os.path.join(ROOT, "emz.py"), "c", corpus, arc], check=True, env=env)
            subprocess.run([sys.executable, os.path.join(ROOT, "emz.py"), "d", arc, out], check=True, env=env)
            self.assertEqual(read(out), read(corpus))
            self.assertGreater(os.path.getsize(corpus) / os.path.getsize(arc), min_ratio)

    def test_enron_sample(self):
        self.roundtrip(ENRON, 15)          # about 18.9x

    def test_oss_sample(self):
        self.roundtrip(OSS, 4.2)           # about 4.46x (binary attachments, signatures)


class FieldModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enron = parsed(ENRON)
        cls.oss = parsed(OSS)

    def test_parser_covers_samples(self):
        self.assertGreater(len(self.enron), 550)
        self.assertEqual(len(self.oss), 184)

    def test_render_is_inverse_of_parse(self):
        for corpus in (ENRON, OSS):
            for path, msg in emz.iter_corpus(corpus):
                p = emz.parse_message(msg)
                if p is None:
                    continue
                crlf, fields, body = p
                tmpl = tuple((n, sp) for n, sp, _ in fields)
                self.assertEqual(emz.render_message(crlf, tmpl, [v for _, _, v in fields], body), msg)

    def test_dates(self):
        n = 0
        for _, (crlf, fields, _) in self.enron + self.oss:
            for name, _, v in fields:
                if name.lower() == b"date":
                    r = emz.parse_date(v)
                    if r is not None:
                        self.assertEqual(emz.format_date(*r), v); n += 1
        self.assertGreater(n, 700)
        for good in (b"Tue, 6 Dec 2016 17:04:15 +0530 (IST)", b"6 Dec 2016 11:34:32 -0000",
                     b"Tue, 06 Dec 2016 11:34:20 +0000", b"Mon, 14 May 2001 16:39 -0700"):
            r = emz.parse_date(good)
            self.assertIsNotNone(r, good)
            self.assertEqual(emz.format_date(*r), good)
        for bad in (b"Mon, 31 Feb 2001 10:00:00 -0800 (PST)", b"Tue, 14 May 2001 16:39:00 -0700",
                    b"Mon, 14 May 2001 16:39:60 -0700"):
            self.assertIsNone(emz.parse_date(bad), bad)

    def test_civil_days(self):
        for z in range(-800000, 800000, 997):
            self.assertEqual(emz.days_from_civil(*emz.civil_from_days(z)), z)

    def test_address_folding(self):
        n = 0
        for _, (crlf, fields, _) in self.enron:
            eol = b"\r\n" if crlf else b"\n"
            for name, _, v in fields:
                if name in (b"From", b"To", b"Cc"):
                    a = emz.split_addrs(v, eol)
                    if a is not None:
                        self.assertEqual(emz.fold_addrs(a, eol), v); n += 1
        self.assertGreater(n, 1000)

    def test_id_lists_split_exactly(self):
        n = 0
        for _, (_, fields, _) in self.oss:
            for name, _, v in fields:
                if name.lower() in (b"references", b"in-reply-to"):
                    self.assertEqual(b"".join(emz.IDLIST_SPLIT.split(v)), v); n += 1
        self.assertGreater(n, 50)

    def test_mime_segments_render_exactly(self):
        kinds = set()
        for corpus in (ENRON, OSS):
            for _, (crlf, fields, body) in parsed(corpus):
                eol = b"\r\n" if crlf else b"\n"
                vals = {}
                for n, sp, v in fields:
                    vals.setdefault(n.lower(), v)
                segs = emz.mime_segments(vals.get(b"content-type", b"").replace(eol, b" "),
                                         vals.get(b"content-transfer-encoding", b"").strip().lower(), body, eol)
                if len(segs) > 1 or segs[0][0] != "T":
                    self.assertEqual(emz.render_segments(segs, eol), body)
                kinds.update(k if k != "Q" else "Q%d" % p[0] for k, p in segs)
        # base64 and (nearly) all quoted-printable encoder variants occur in the samples
        self.assertTrue({"T", "B"} <= kinds)
        self.assertGreaterEqual(len([k for k in kinds if k.startswith("Q")]), 12)

    def test_quoted_printable_enron(self):
        n = 0
        for _, (crlf, fields, body) in self.enron:
            vals = {k: v for k, _, v in fields}
            if vals[b"Content-Transfer-Encoding"] == b"quoted-printable":
                t = qp.qp_decode(body)
                self.assertIsNotNone(t)
                self.assertEqual(qp.qp_encode(t), body); n += 1
        self.assertGreater(n, 5)

    def test_message_id_generator(self):
        """Consecutive Enron Message-IDs of one folder are consecutive outputs
        of the Park-Miller generator (low 25 bits)."""
        ids = []
        for path, (_, fields, _) in self.enron:
            if path.startswith("allen-p/_sent_mail/"):
                v = dict((n, v) for n, _, v in fields)[b"Message-ID"]
                ids.append(int(emz.MID_JAVAMAIL.match(v).group(1)))
        self.assertGreater(len(ids), 100)
        M, A, MASK = 2**31 - 1, 16807, (1 << 25) - 1
        hits = sum(1 for a, b in zip(ids, ids[1:])
                   if any(((A * ((h << 25) | a)) % M) & MASK == b for h in range(64)))
        self.assertGreater(hits / (len(ids) - 1), 0.95)

    def test_signature_skeleton(self):
        """DKIM values split into skeleton, base64 runs and timestamps exactly."""
        n = 0
        for _, (_, fields, _) in self.oss:
            for name, _, v in fields:
                if name.lower() == b"dkim-signature":
                    blobs, nums = [], []
                    sk = emz.B64RUN.sub(lambda m: blobs.append(m.group(0)) or b"\x03", v)
                    sk = emz.SIG_TS.sub(lambda m: nums.append(m.group(2)) or (m.group(1) + b"\x04"), sk)
                    it_b, it_n = iter(blobs), iter(nums)
                    back = re.sub(rb"[\x03\x04]", lambda m: next(it_b) if m.group(0) == b"\x03" else next(it_n), sk)
                    self.assertEqual(back, v); n += 1
        self.assertGreater(n, 3)

    def test_dkim_body_hash(self):
        """bh= recomputed from the body: RFC 6376 appendix A example (simple),
        the empty-body constants, relaxed whitespace rules, LF-stored mail."""
        tags = (b"v=1; a=rsa-sha256; s=brisbane; d=example.com;\r\n      c=simple/simple; q=dns/txt;"
                b" i=joe@football.example.com;\r\n      bh=\x05;")
        body = b"Hi.\r\n\r\nWe lost the game. Are you hungry yet?\r\n\r\nJoe.\r\n"
        self.assertEqual(emz.dkim_body_hash(tags, body, b"\r\n"), b"2jUSOH9NhtVGCQWNr9BrIAPreKQjO6Sn7XIkfJVOzv8=")
        self.assertEqual(emz.dkim_body_hash(tags, body.replace(b"\r\n", b"\n") + b"\n\n", b"\n"),
                         b"2jUSOH9NhtVGCQWNr9BrIAPreKQjO6Sn7XIkfJVOzv8=")
        self.assertEqual(emz.dkim_body_hash(b"a=rsa-sha256; bh=", b"", b"\r\n"),
                         b"frcCV1k9oG9oKj3dpUqdJg1PxRT2RSN/XKdLCPjaYaY=")
        self.assertEqual(emz.dkim_body_hash(b"a=rsa-sha256; c=relaxed/relaxed; bh=", b"\r\n\r\n", b"\r\n"),
                         b"47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=")
        self.assertEqual(emz.dkim_body_hash(b"a=rsa-sha256; c=relaxed/relaxed", b"Hi \t there  \r\n\r\n", b"\r\n"),
                         emz.dkim_body_hash(b"a=rsa-sha256; c=relaxed", b"Hi there\r\n", b"\r\n"))
        self.assertIsNone(emz.dkim_body_hash(b"a=rsa-md5; bh=", body, b"\r\n"))
        # the sample contains signatures whose bh= is derivable, and some that are not
        hits = misses = 0
        for _, (crlf, fields, body) in self.oss:
            for name, _, v in fields:
                m = re.search(rb"bh=([A-Za-z0-9+/]{16,}={0,2})", v)
                if name.lower() == b"dkim-signature" and m:
                    if emz.dkim_body_hash(v, body, b"\r\n" if crlf else b"\n") == m.group(1): hits += 1
                    else: misses += 1
        self.assertGreater(hits, 3); self.assertGreater(misses, 0)

    def test_derived_body_hash_not_stored(self):
        body = b"Hi.\r\n\r\nWe lost the game. Are you hungry yet?\r\n\r\nJoe.\r\n"
        bh = b"2jUSOH9NhtVGCQWNr9BrIAPreKQjO6Sn7XIkfJVOzv8="
        def msg(sig_bh):
            return (b"DKIM-Signature: v=1; a=rsa-sha256; c=simple/simple; d=example.com; s=x;\r\n"
                    b"\tbh=" + sig_bh + b"; b=QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0MTIzNDU2Nzg5\r\n"
                    b"Message-ID: <1@example.com>\r\nFrom: joe@example.com\r\n\r\n" + body)
        msgs = [msg(bh), msg(bh.replace(b"2j", b"3j"))]
        S, out = transform_roundtrip(msgs)
        self.assertEqual(out, msgs)
        self.assertEqual(S["sig_l"].b.count(bh[2:20]), 1)   # only the wrong hash is stored

    def test_generic_fields_refer_to_known_strings(self):
        """Mail-client caches (here an X-VM-v5-Data lookalike), Content-Length
        and Lines repeat the message's own fields and body size."""
        body = b"Hello,\n\nthe patch is attached.\n\n-- \nJo\n"
        def msg(i, extra=b""):
            mid = b"<%d.abc@example.org>" % i
            subj = b"Re: [list] bug %d in parser" % i
            vm = (b'X-VM-v5-Data: ([nil nil]\n\t["%d" "Jo Doe" "jo@example.org" "%s" "%d" "%s" nil])\n'
                  % (len(body), mid, body.count(b"\n"), subj))
            return (vm + b"Message-ID: " + mid + b"\nDate: Tue, 6 Dec 2016 17:04:15 +0530\n"
                    b"From: Jo Doe <jo@example.org>\nSubject: " + subj + b"\n" +
                    b"Content-Length: %d\nLines: %d\n" % (len(body), body.count(b"\n")) + extra + b"\n" + body)
        msgs = [msg(i) for i in range(20)] + [msg(99, b"X-Odd: a\x06b <99.abc@example.org>\n")]
        S, out = transform_roundtrip(msgs)
        self.assertEqual(out, msgs)
        lit = bytes(S["hdr_l"].b)
        self.assertEqual(lit.count(b"\x06A"), 2)      # X-VM once, then move-to-front hits; X-Odd
        self.assertEqual(lit.count(b"\x06\x06"), 1)   # a literal \x06 is stuffed
        self.assertEqual(lit.count(b"\x06H"), 2)      # body size: X-VM and Content-Length, once each

    def test_multipart_boundary_taken_from_body(self):
        body = b"preamble\n--b1=_x\nContent-Type: text/plain\n\nhi\n--b1=_x--\n"
        m1 = b"Message-ID: <a@b>\nContent-Type: multipart/mixed;\n boundary=\"b1=_x\"\n\n" + body
        m2 = b"Message-ID: <c@b>\nContent-Type: multipart/mixed; boundary=zz2\n\n--other\n" + body.replace(b"b1=_x", b"zz2")
        m3 = b"Message-ID: <d@b>\nContent-Type: text/plain; x=\x06\n\nplain\n"
        S, out = transform_roundtrip([m1, m2, m3])
        self.assertEqual(out, [m1, m2, m3])
        self.assertNotIn(b"b1=_x", S["hdr_l"].b)   # restored from the body's first delimiter
        self.assertIn(b"zz2", S["hdr_l"].b)        # first '--' line is not the delimiter: stored


if __name__ == "__main__":
    unittest.main()
