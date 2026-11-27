"""Tests for emz.  Run `make test` (builds ./emcm first).

The sample corpus tests/data/enron_sample.corpus (564 Enron messages, 1.1 MB)
was selected by scripts/make_sample.py to exercise every code path: a run of
consecutive messages with their copies in other folders and mailboxes, the
verbatim fallback, quoted-printable and base64 bodies, non-ASCII bytes,
non-canonical dates, folded subjects, messages without To, Outlook-style
display-name lists and identical bodies under different headers.
"""
import os, random, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import emz  # noqa: E402
import qp   # noqa: E402

SAMPLE = os.path.join(ROOT, "tests", "data", "enron_sample.corpus")
EMCM = os.environ.get("EMCM", os.path.join(ROOT, "emcm"))


def sample_messages():
    return list(emz.iter_corpus(SAMPLE))


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


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


class ArchiveRoundTrip(unittest.TestCase):
    def test_sample_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            arc, out = os.path.join(d, "s.emz"), os.path.join(d, "s.out")
            env = dict(os.environ, EMCM=EMCM)
            subprocess.run([sys.executable, os.path.join(ROOT, "emz.py"), "c", SAMPLE, arc], check=True, env=env)
            subprocess.run([sys.executable, os.path.join(ROOT, "emz.py"), "d", arc, out], check=True, env=env)
            self.assertEqual(read(out), read(SAMPLE))
            # sanity bound on the ratio (about 18x on this sample)
            self.assertGreater(os.path.getsize(SAMPLE) / os.path.getsize(arc), 12)


class FieldModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = []
        for path, msg in sample_messages():
            p = emz.Codec.parse(msg)
            if p is not None:
                cls.parsed.append((path, p))

    def test_parser_covers_sample(self):
        self.assertGreater(len(self.parsed), 500)

    def test_date_roundtrip(self):
        n = 0
        for _, (F, _, _, _) in self.parsed:
            r = emz.parse_date(F[b"Date"])
            if r is not None:
                self.assertEqual(emz.format_date(*r), F[b"Date"]); n += 1
        self.assertGreater(n, 500)
        self.assertIsNone(emz.parse_date(b"Mon, 31 Feb 2001 10:00:00 -0800 (PST)"))   # wrong weekday/day
        self.assertIsNone(emz.parse_date(b"Mon, 05 Feb 2001 10:00:00 -0800 (PST)"))   # zero-padded day

    def test_civil_days(self):
        for z in range(-800000, 800000, 997):
            self.assertEqual(emz.days_from_civil(*emz.civil_from_days(z)), z)

    def test_address_folding(self):
        n = 0
        for _, (F, has_to, has_cc, _) in self.parsed:
            for f in (b"From", b"To", b"Cc"):
                v = F.get(f)
                if v:
                    a = emz.split_addrs(v)
                    if a is not None:
                        self.assertEqual(emz.fold_addrs(a), v); n += 1
        self.assertGreater(n, 1000)

    def test_display_name_split_is_exact(self):
        for _, (F, _, _, _) in self.parsed:
            for f in (b"X-To", b"X-cc"):
                for splitter in (emz.split_xlist, emz.split_xlist_angle):
                    self.assertEqual(b", ".join(splitter(F[f])), F[f])

    def test_quoted_printable(self):
        n = 0
        for _, (F, _, _, body) in self.parsed:
            if F[b"Content-Transfer-Encoding"] == b"quoted-printable":
                t = qp.qp_decode(body)
                self.assertIsNotNone(t)
                self.assertEqual(qp.qp_encode(t), body); n += 1
        self.assertGreater(n, 5)

    def test_message_id_generator(self):
        """Consecutive Message-IDs of one folder are consecutive outputs of the
        Park-Miller generator (low 25 bits)."""
        ids = []
        for path, (F, _, _, _) in self.parsed:
            if path.startswith("allen-p/_sent_mail/"):
                m = emz.MID_RE.match(F[b"Message-ID"])
                ids.append(int(m.group(1)))
        self.assertGreater(len(ids), 100)
        M, A, MASK = 2**31 - 1, 16807, (1 << 25) - 1
        hits = 0
        for a, b in zip(ids, ids[1:]):
            if any(((A * ((h << 25) | a)) % M) & MASK == b for h in range(64)):
                hits += 1
        self.assertGreater(hits / (len(ids) - 1), 0.95)


if __name__ == "__main__":
    unittest.main()
