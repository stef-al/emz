# emz — structure-aware compression of e-mail archives

`emz` is a lossless compressor for mail archives (maildir and other
directory trees, mbox files, mailing-list archives, Outlook `.msg` files) that
models what an e-mail *is* instead of treating the archive as a flat byte
stream. Every model checks
that it reproduces the original bytes exactly. Anything that does not fit is
coded as is, so the output is always a byte-exact copy, and decompression
checks a SHA-256 of the original.

It is evaluated on two corpora from different eras of e-mail:

| Corpus | Messages | Size | **emz** | Best existing tool | xz -9e |
|---|---:|---:|---:|---:|---:|
| Enron (CMU release; corporate mail of the early 2000s) | 517,401 | 1,437.5 MB | **54.47 MB (26.4×)** | 80.28 MB (lrzip -z) | 112.38 MB |
| oss-security mailing list, 2015–2026 | 16,967 | 132.1 MB | **19.92 MB (6.63×)** | 23.74 MB (zpaq -m5) | 26.64 MB |

* emz is 32% smaller than the best existing tool on Enron, and 16%
  smaller than zpaq -m5 (25% smaller than xz) on the recent list archive.
* Recent mail compresses far less well. Each message carries DKIM/ARC
  signatures and other cryptographic header values that are random by design:
  they make up 28% of emz's output on the recent corpus. Binary attachments
  are another 17%.
* If each message must stay readable on its own, today's per-message
  zstd/gzip gets 2.1–2.2× on Enron. The per-message variant of the same ideas
  measured **14.3×** on a 1% sample: static field tables, a frozen
  context-mixing model and single-instance storage of repeated content (see
  [`docs/PER_MESSAGE.md`](docs/PER_MESSAGE.md)).

## What changed in version 2

Version 1 parsed only Enron's fixed 17-field header; all other mail went to
the verbatim path. Version 2 handles any RFC 5322 message:

* **Header layout.** The sequence of field names is coded as a template
  (move-to-front list of templates; a new template is coded field by field).
  Fields without a model of their own get a move-to-front list per
  (field, folder); fields that depend on the sender (User-Agent, Autocrypt,
  Organization, …) are keyed by the From address.
* **Threads.** Message-IDs, In-Reply-To and References are coded as tokens; a
  token seen before becomes a back-reference. References are predicted as the
  parent's References plus the parent's ID, and the subject as the parent's
  subject behind a `Re:`/`[list]` prefix.
* **Timestamps.** Received and X-Received times are coded in seconds relative
  to the Date header, and so are the t= and x= times of signatures.
* **Signatures.** DKIM, ARC, Google and Microsoft header values are split
  into a skeleton that repeats per signer (tags, selectors, header lists,
  folding) and the random base64 runs. A body hash (bh=) that matches the
  stored body is recomputed when decoding instead of being stored; that holds
  for 55% of the DKIM signatures in the recent corpus.
* **Fields that repeat other fields.** Values of fields without a model of
  their own may refer to the message's Message-ID, parent ID, sender address
  and name, subject, time, time zone, body size and body line count. Mail
  clients cache these in headers of their own (Emacs VM's `X-VM-v5-Data`, in
  68% of the recent corpus), and mbox files carry `Content-Length` and
  `Lines`.
* **MIME.** Multipart bodies are split into parts. Quoted-printable parts are
  decoded and re-encoded with one of 8 encoder variants, base64 parts are
  decoded with their line length and padding; both are verified byte for
  byte. Decoded text joins the body text, binary data goes to its own stream.
  The multipart boundary is not stored in the header when the body's first
  delimiter line repeats it.
* **Copies** of a message are coded as a reference plus the fields that
  differ.
* **Body ends.** Each body text part ends with a mark inside the text stream
  instead of a stored length, so the coder predicts where a message ends from
  its last lines (signatures, disclaimers): −0.6% on Enron.
* **Coder.** A run model per context (the last byte seen in that context and
  how often it repeated), direct order-1 and order-2 inputs, finer mixer
  weight sets, a learned final mix of the APM stages, a match model that
  follows quoted text through `> ` prefixes, 4-way bucket search, sparse
  insertion for the long match model and an order-2 APM stage. Together
  −2.1% (Enron) and −3.7% (recent mail) on message bodies against version 1.
* **Input forms.** emz reads a directory tree (maildir, MH: every regular file
  is a message), an mbox file, a corpus file or a single message, and
  decompression restores the same form. An mbox `From ` line is coded against
  the message's own Return-Path and topmost Received time, which leaves about
  2 bytes per message.
* **Safety.** The archive stores a SHA-256 of the input, and decompression
  fails loudly if the output differs. Streams larger than 2 GiB are split so
  the coder's 32-bit positions never overflow.

The Enron-specific models of version 1 (Park–Miller Message-IDs, JavaMail
address folding, X-From/X-To name alignment, X-Folder/X-Origin/X-FileName)
are unchanged.

## Outlook .msg files

An Outlook `.msg` file is a compound file (a small FAT file system, MS-CFB)
whose streams hold the message's MAPI properties (MS-OXMSG). Plain
compressors see sector padding, directory records, UTF-16 text, the same
names and addresses stored five times, and an RTF body that Outlook has
already compressed. emz takes such a file apart and codes each part with a
model of its own:

* **Skeleton.** The header, FAT, mini FAT and directory are coded field by
  field: chain links as "next sector", directory entries as columns (names
  as text, tree links, start sectors against allocation order, sizes), and
  the bytes that are neither structure nor stream content (sector slack, free
  sectors) as zero runs.
* **Property values** are coded in an order that puts what others repeat
  first. The transport headers (`PR_TRANSPORT_MESSAGE_HEADERS`) go through
  the RFC 5322 field models above, and their subject, addresses, names and
  Message-ID become known strings. A value is then a copy of an earlier value
  of the same file, a recency rank among the last values of the same
  property, a copy of an earlier value anywhere in the archive, or a literal
  in which known strings are replaced by markers. UTF-16 strings are stored
  as UTF-8.
* **Property streams** (`__properties_version1.0`) are coded as columns
  (tags, flags, values); the size field of a string or binary property is
  predicted from the size of its stream, times from the previous time.
* **Compressed RTF** is stored decompressed and split into its text runs
  and its markup. The runs (escapes decoded, code page converted to UTF-8,
  one line per `\par`) go to the body text stream right after the plain-text
  and HTML bodies, where they repeat them; the markup goes to a stream of its
  own. The exact compressed bytes are rebuilt from the text
  and one choice per token. The choice is "as predicted" whenever the token
  is what Outlook's own match finder would code. That finder turned out to be
  an LZSS binary search tree in the style of Okumura's LZSS.C (a tree per
  first byte, the first of several equally long matches on the search path
  wins, deletion by successor); simulating it predicts 99.4% of the tokens
  of Outlook's streams. Streams from any other writer are still rebuilt
  exactly, with explicit choices.
* **Bodies and attachments** are de-duplicated across the archive; text goes
  to the body stream (CRLF as LF when reversible), an attached MIME entity
  (for example the `smime.p7m` of a signed message) through the MIME models,
  an attached `.msg` recursively, anything else to the binary stream.

Every step is verified when compressing; a file that does not parse as a
compound file, or whose layout is unusual (overlapping or out-of-range
sectors, broken chains), is stored verbatim.

On 25 sample files from the test suites of two open-source `.msg` readers
(8.5 MB, details in [`results/msg_samples.txt`](results/msg_samples.txt)):

| Archive of the 25 files | As they are | Attachment bytes zeroed |
|---|---:|---:|
| **emz** | **2,595,113** | **95,538** |
| emz before `.msg` support (file coded by `emcm` alone) | 2,691,840 | 186,153 |
| xz -9e (tar) | 2,726,152 | 181,148 |
| zstd -19 --long=27 (tar) | 2,742,258 | 196,251 |

Attachments (TIFF, PDF, Office files, JPEG, mostly compressed already) are
83% of the input bytes and 96% of emz's output in the left column. With the
attachment bytes set to zero, the messages code to 47% less than xz -9e and
49% less than emz did before. The 23 compressed RTF bodies (120,003 bytes) cost 581 bytes of token
choices plus their text in the body stream.

## Quick start

```sh
make                 # builds ./emcm (the context-mixing coder); needs a C compiler
make test            # unit tests + round trips of the sample corpora, directories and mbox files

python3 emz.py c ~/Maildir mail.emz          # a maildir (or any directory of message files)
python3 emz.py d mail.emz restored/          # recreates the tree; restored/ must not exist or be empty
python3 emz.py c archive.mbox archive.emz    # an mbox file
python3 emz.py d archive.emz archive.mbox
```

The input form is detected automatically: a directory (every regular file is
one message; names, sub-directories and empty directories are kept, file times
and permissions are not), an mbox file (starts with `From `), a *corpus* file
(for each message `path\n`, `length\n`, then the raw bytes; the benchmark
corpora use it), or any other file as a single message. A message that is an
Outlook `.msg` file is recognised by its content, in any of these forms
(`python3 emz.py c saved_mails/ mails.emz`).

The full corpora are not stored in this repository. To fetch and serialise
them:

```sh
scripts/fetch_enron.sh data          # -> data/enron.corpus, 1,437,509,559 bytes (about 2.2 GB of disk)
REV=53b32cbfafccf948233f43de767a9f5b7b852ead scripts/fetch_oss_security.sh data
                                     # -> data/oss_full.corpus, 132,116,039 bytes
python3 emz.py c data/enron.corpus data/enron.emz
```

Python 3.8+ only; no packages are needed for emz itself. The benchmark
scripts in `scripts/` also use `zstandard` and `brotli` (pip) and the
command-line compressors they compare against. A full Enron run needs about
3.2 GB of RAM, the recent corpus about 1 GB.

## Results

Every result below was decompressed and checked against the original with
SHA-256. All tools received the same single canonical file per corpus.
Machine: 2 vCPU (Intel Xeon, 2.1 GHz), 8 GB RAM; zstd and xz ran
single-threaded, bsc, lrzip and zpaq used both cores, and emz codes two
streams in parallel. Timings come from a shared VM where other jobs ran at the
same time, so treat them as approximate. Tools: gzip 1.12, bzip2 1.0.8,
xz 5.4.5, zstd 1.5.5, brotli 1.1.0, 7-Zip 23.01, lrzip 0.651, zpaq 7.15,
bsc 3.4.1, paq8px v217.

### Recent corpus: oss-security, 2015–2026

**Corpus.** The oss-security list (lists.openwall.com) as delivered to a
subscriber, from the public git mirror `joemalcolm/oss-security-inbox`
(snapshot 53b32cbfa). The mirror also holds 32,075 messages that it rebuilt
from the web archive (`X-Archive-Source: openwall-scrape`) with synthetic
headers; those are skipped. The remaining 16,967 messages with their original
headers (131.6 MB of mail, 36% of it headers, LF line endings) are sorted by
Date and serialised like Enron. `scripts/fetch_oss_security.sh` rebuilds the
file byte for byte (SHA-256 `1b3c9924…30bd9f8`).

It looks very different from Enron. All messages but one have a Received
chain; 43% carry DKIM signatures and 4% ARC seals; 34% are MIME multipart
(patches, PGP/S/MIME signatures, base64 attachments); 53% are replies with
In-Reply-To; and 68% carry an `X-VM-v5-Data` header, in which the subscriber's
mail client cached the message's size, date, sender, Message-ID and subject.
There are no copies across mailboxes, which made up half of Enron.

| Compressor | Size (MB) | Ratio | Compress (min) | Decompress (min) | Peak RAM (GB) |
|---|---:|---:|---:|---:|---:|
| gzip -9 | 42.76 | 3.1× | 0.1 | <0.1 | <0.1 |
| bzip2 -9 | 36.99 | 3.6× | 0.2 | 0.1 | <0.1 |
| zstd -19 | 28.50 | 4.6× | 1.1 | <0.1 | 0.2 |
| lrzip (rzip + LZMA) | 28.02 | 4.7× | 0.4 | <0.1 | 0.4 |
| zstd --ultra -22 --long=31 | 27.44 | 4.8× | 1.8 | <0.1 | 0.8 |
| xz -9e | 26.64 | 5.0× | 1.6 | <0.1 | 0.7 |
| bsc 3.4.1 (-b512 -e2) | 26.57 | 5.0× | 0.1 | 0.1 | 0.5 |
| brotli -q 11 -w 24 | 26.46 | 5.0× | 4.8 | <0.1 | 0.2 |
| lrzip -z (rzip + ZPAQ) | 25.80 | 5.1× | 0.9 | 0.9 | 0.4 |
| 7-Zip PPMd (order 16, 1 GB) | 25.72 | 5.1× | 0.5 | 0.5 | 1.0 |
| zpaq -m5 | 23.74 | 5.6× | 3.7 | 5.1 | 1.7 |
| emz version 1¹ | 23.05 | 5.7× | | | |
| **emz** | **19.92** | **6.63×** | 5.4² | 5.5² | 0.9 |

¹ Version 1 parsed only Enron's header layout, so every message here took
the verbatim path: this row is v1's coder on the raw text.
² Measured while lower-priority jobs used the second core.

emz is 16% smaller than zpaq -m5, the strongest standard tool here, at
similar speed. It is 25% smaller than xz -9e and 27% smaller than zstd at its
strongest long-range setting.

#### Where the bits go (recent corpus)

| Stream group | Input to coder (bytes) | Coded (bytes) | Share |
|---|---:|---:|---:|
| Body text, including decoded quoted-printable and base64 text parts | 70,143,945 | 8,849,715 | 44.4% |
| Signature material: base64 runs in DKIM, ARC, Google and Microsoft fields | 7,692,008 | 5,530,741 | 27.8% |
| Binary attachments (decoded base64) | 4,382,926 | 3,306,549 | 16.6% |
| Header text not predicted by a model (literals) | 15,318,574 | 1,979,746 | 9.9% |
| Dates (Date, Received, signature timestamps) | 201,288 | 87,346 | 0.4% |
| Templates (field-name sequences) | 212,991 | 81,194 | 0.4% |
| Move-to-front ranks of header values | 507,470 | 65,596 | 0.3% |
| Message-IDs, In-Reply-To, References | 79,964 | 16,503 | 0.1% |
| MIME structure and repeated-body references | 80,366 | 6,442 | <0.1% |
| Path, message kind | 34,162 | 116 | <0.1% |
| **Total** | 98,653,694 | **19,923,948** | |

The 47.3 MB of raw headers cost 7.8 MB, about 457 bytes per message (Enron:
10.6 bytes). Of those, 326 bytes are signature material: RSA signatures,
Microsoft's encrypted anti-spam reports and Google's message tokens are
random, and base64 of random bytes cannot be coded below 6 bits per character
(emz reaches 5.8). Another 117 bytes are header text that no model predicts:
new Message-IDs and Autocrypt public keys (a sixth each), subjects, Received
lines, what is left of the X-VM-v5-Data cache, and a long tail of other fields
([`results/oss_header_literals_by_field.txt`](results/oss_header_literals_by_field.txt),
from `scripts/header_literals.py`). Templates, move-to-front ranks, dates and
thread references take the last 15 bytes.

#### What each part contributes (recent corpus)

Structure models and coder, measured separately:

| | Version 1 coder | Current coder |
|---|---:|---:|
| Raw corpus, no transform | 23,062,984 | 22,393,093 (−2.9%) |
| Current transform | 20,420,192 (−11.5%) | **19,924,181** (−13.6%) |

The coder alone on the raw text already beats zpaq -m5 by 5.7%; the
structure models remove another 11.0%.

Coder components, each switched off on its own (12 MB samples of body text
from each corpus; the switches are environment variables of `emcm`):

| Coder | Enron bodies | Recent bodies |
|---|---:|---:|
| version 1 | 1,665,925 | 2,292,081 |
| **current** | **1,631,303 (−2.08%)** | **2,206,575 (−3.73%)** |
| without the run model (`EMCM_RUN=0`) | +0.65% | +0.83% |
| without the third mixer's finer weight sets (`EMCM_M3SEL=0`) | +0.29% | +0.29% |
| without the learned final mix (`EMCM_FMIX=0`) | +0.23% | +0.33% |
| order 6 instead of order 5 (`EMCM_CTX=0,1,2,3,4,5,6,7,8`) | +0.12% | +0.08% |
| without the direct order-1/2 inputs (`EMCM_DIRECT=0`) | +0.09% | +0.18% |
| without the quote-aware match model (`EMCM_QM=0`) | +0.48% | +1.81% |
| without the 4-way bucket search (`EMCM_WAYS=2`) | +0.08% | +0.09% |
| without sparse insertion in the long match model (`EMCM_SPARSE=0`) | +0.08% | +0.06% |
| without the order-2 APM, and with it the learned final mix (`EMCM_APM2=0`) | +0.32% | +0.37% |

With the five newest switched off, the coder is the one first released with
version 2: 1,654,769 and 2,247,646 bytes, one byte away from that coder's
output because the mixers now add up their inputs in a fixed different order.
With the last four switched off as well, the model is version 1's.

The structure models added since the first version 2 transform, one at a
time, measured with the coder of that time (the first 1,500 messages, all
from 2015, 7.36 MB; on the full corpus the three together took the output
from 20,661,082 to 20,237,556 bytes, −2.1%):

| Step | Size (bytes) | Change |
|---|---:|---:|
| emz version 1 | 1,177,708 | |
| version 2 header, thread, signature and MIME models | 1,070,622 | −9.1% |
| + body hash recomputed from the body | 1,063,502 | −0.7% |
| + fields that repeat other fields or the body size | 1,033,767 | −2.8% |
| + multipart boundary taken from the body | 1,024,377 | −0.9% |

The latest changes, on both corpora:

| Step | Enron 100 MB subset | Recent, first 1,500 messages |
|---|---:|---:|
| version 2 as first released | 5,195,569 | 1,024,377 |
| + coder: run model, direct inputs, mixer weight sets, learned final mix, order 5 | 5,122,800 (−1.4%) | 1,011,532 (−1.3%) |
| + end marks instead of lengths for body text | 5,090,615 (−0.6%) | 1,008,184 (−0.3%) |

#### Small sample against the strongest general-purpose CM

The first 1,500 messages of the recent corpus (7.36 MB, all from 2015):

| Compressor | Size (bytes) | Ratio |
|---|---:|---:|
| zstd --ultra -22 --long=31 | 1,434,277 | 5.1× |
| xz -9e | 1,390,948 | 5.3× |
| zpaq -m5 | 1,175,070 | 6.3× |
| `emcm` alone | 1,137,837 | 6.5× |
| emz | 1,008,184 | 7.3× |
| paq8px v217 -8 | 952,713 | 7.7× |
| **emz transform¹ + paq8px as back end** | **881,698** | **8.4×** |

¹ The version 2 transform as first released.

paq8px's much stronger models beat emz with `emcm` by 5.5% here, at about 70
times the time (26 minutes against 22 seconds). Putting the emz transform in
front of paq8px makes it another 7.5% smaller, against 2.6% on Enron's 5 MB
sample (below): the structure models matter more for recent mail, and they add
to even the strongest CM.

### Full Enron corpus

The corpus has 517,401 messages and 1,437,509,559 bytes. It was fetched from
the GitHub mirror `malted/enron`; all 517,401 files are hash-identical to a
second, independent mirror (`agocs/enronDataScience`). Serialised in path
order as `path\n length\n bytes`.

| Compressor | Size (MB) | Ratio | Compress (min) | Decompress (min) | Peak RAM (GB) |
|---|---:|---:|---:|---:|---:|
| gzip -9 | 366.91 | 3.9× | 1.1 | 0.2 | <0.1 |
| bzip2 -9 | 265.02 | 5.4× | 2.4 | 0.9 | <0.1 |
| zstd -19 | 171.67 | 8.4× | 16.1 | 0.1 | 0.2 |
| 7-Zip PPMd (order 16, 1 GB) | 123.11 | 11.7× | 4.6 | 5.1 | 1.0 |
| xz -9e | 112.38 | 12.8× | 18.0 | 0.2 | 0.7 |
| bsc 3.4.1 (-b512 -e2) | 103.97 | 13.8× | 0.7 | 0.4 | 3.5 |
| lrzip (rzip + LZMA, 1.5 GB window) | 98.01 | 14.7× | 7.2 | 0.3 | 1.9 |
| zstd --ultra -22 --long=31 | 94.48 | 15.2× | 22.9 | 0.1 | 2.1 |
| lrzip -z (rzip + ZPAQ) | 80.28 | 17.9× | 7.7 | 7.3 | 1.8 |
| emz version 1 | 57.35 | 25.1× | 20.2 | 19.8 | 2.6 |
| **emz** | **54.47** | **26.4×** | 32.4 | 36.2 | 3.2 |

The emz archive (54,470,287 bytes) was written by an `-O3 -march=native` build
of `emcm` and decoded byte-exact by a separately built generic `-O2` binary;
lower-priority jobs used the second core during both runs. emz is 32% smaller
than the best existing tool (lrzip -z) and 42% smaller than zstd at its
strongest long-range setting, and 5.0% smaller than version 1. Most of that
gain comes from the coder: the body text stream, nearly the same 467 MB of
input in all versions, codes to 48.93 MB instead of 50.83 MB. The price is
speed: about 0.7 MB/s in each direction (35 CPU-minutes to compress, 38 to
decompress with the generic build). That suits archival storage and is far too
slow for interactive use. paq8px was not feasible on the full corpus: it ran
at 5.4 KB/s, which extrapolates to about 3 days.

#### Where the bits go (Enron)

| Stream group | Input to coder (bytes) | Coded (bytes) | Share |
|---|---:|---:|---:|
| Body text (after de-duplication, CRLF and QP decoding) | 467,797,911 | 48,933,480 | 89.8% |
| Header text not predicted by a model (names, addresses, subjects, …) | 49,154,416 | 3,935,710 | 7.2% |
| Move-to-front ranks of header values | 5,842,815 | 535,900 | 1.0% |
| Date | 918,526 | 426,063 | 0.8% |
| Message-ID (generator steps, clock) and thread references | 1,095,373 | 268,055 | 0.5% |
| Message kind, copies (reference + changed fields), templates | 1,725,016 | 264,177 | 0.5% |
| Path | 622,764 | 46,628 | <0.1% |
| MIME structure and repeated-body references | 778,424 | 34,931 | <0.1% |
| Verbatim fallback | 184,012 | 25,102 | <0.1% |
| **Total** | 528,119,257 | **54,470,046** | |

The 462 MB of raw headers code to 5.5 MB, about 10.6 bytes per message
(version 1: 11.8), including 0.5 bytes per Message-ID. What remains is almost
entirely first-occurrence body text.

#### Ablations (Enron)

**Same back end, with and without the structure models (full corpus,
version 1).** On the raw corpus, `emcm` gives 69.62 MB. This run measured
size only, with the same 2 GB table as the body stream in emz. The structure
models brought that to 57.35 MB, 17.6% smaller.

**The transform helps every back end (full corpus, version 1).** Here the
emz streams are coded with standard compressors instead of `emcm`:

| Back end | On the raw corpus | On the emz streams | Change |
|---|---:|---:|---:|
| bsc -e2 | 103.97 MB | **65.81 MB** | −37% |
| PPMd o16 | 123.11 MB | 72.51 MB | −41% |
| xz -9e | 112.38 MB | 78.23 MB | −30% |
| zstd -19 (with --long=30 on the streams) | 171.67 MB; 94.48 MB at -22 --long=31 | 79.57 MB | −54%; −16% |

The emz transform followed by off-the-shelf bsc already beats every existing
tool on the raw corpus.

**Ablation ladder (100 MB subset, 38,607 messages, `emcm` back end).** Each
step adds one component:

| Step | Size | Cumulative gain |
|---|---:|---:|
| Raw corpus → `emcm` (version 1) | 5,976,320 | — |
| + header/body separation | 5,838,565 | −2.3% |
| + message and body de-duplication | 5,597,695 | −6.3% |
| + field models, QP decoding, PRNG Message-ID (emz version 1) | 5,310,297 | −11.1% |
| version 1 transform with the current coder | 5,200,448 | −13.0% |
| **emz (current)** | **5,090,615** | **−14.8%** |

The subset is the first 100 MB in path order: 12 mailboxes, allen-p through
part of buy-r. It has fewer cross-mailbox copies than the full archive, so the
gains are smaller here than on the full corpus.

Effects of individual version 1 models, measured on the subset:

* Park–Miller Message-ID model: the N1 stream fell from 132,425 to 4,711 bytes
  (−96%). On the full corpus N1 costs 41 KB for 517k IDs, against 1.6 MB for
  25 random bits each.
* QP decoding: body stream −2.3%.
* Address-aligned X-To/X-cc name model: display-name streams −16%.

#### 100 MB subset: all tools

| Compressor | Size (bytes) | Ratio |
|---|---:|---:|
| gzip -9 | 25,285,341 | 4.0× |
| bzip2 -9 | 18,520,229 | 5.4× |
| zstd -19 | 10,675,904 | 9.4× |
| brotli -q 11 -w 24 | 8,537,505 | 11.7× |
| PPMd o16 | 8,329,443 | 12.0× |
| bsc -b512 -e2 | 8,294,326 | 12.1× |
| zstd -22 --long=31 | 8,206,769 | 12.2× |
| lrzip (LZMA) | 8,166,649 | 12.2× |
| xz -9e | 7,930,452 | 12.6× |
| zpaq -m5 | 7,064,080 | 14.2× |
| lrzip -z (ZPAQ) | 6,930,535 | 14.4× |
| `emcm` (version 1) alone | 5,976,320 | 16.7× |
| emz version 1 | 5,310,297 | 18.8× |
| **emz** | **5,090,615** | **19.6×** |

#### Small Enron sample against the strongest general-purpose CM (5 MB, 2,835 messages)

| Compressor | Size (bytes) | Time |
|---|---:|---:|
| paq8px v217 -8 | 275,520 | 15 min |
| **emz version 1 transform + paq8px as back end** | **268,245** | ≈7 min |
| emz | 312,383 | 8 s¹ |
| emz version 1 | 327,403 | 4 s |
| `emcm` alone | 349,469 | 19 s¹ |
| zpaq -m5 | 362,869 | 15 s |
| lrzip -z | 410,816 | 2 s |
| xz -9e | 471,456 | 2 s |
| zstd -22 --long=31 | 494,841 | 4 s |

¹ Measured while another job ran on the second core.

On a sample this small there is little duplication, and paq8px's much stronger
text model beats emz with `emcm`. Putting the emz transform in front of paq8px
still makes it 2.6% smaller, which confirms that the structure models add to
even the strongest CM. A stronger back end for the body stream is the obvious
next improvement.

### Random access: compressing each message on its own

Mail servers usually compress each message on its own; Dovecot's
`mail_compress` plugin, for example, uses zstd or gzip. A test on a 10%
sample of Enron (51,741 messages, every 10th) and a 1% subsample (5,175
messages) is written up in [`docs/PER_MESSAGE.md`](docs/PER_MESSAGE.md).
Every shared dictionary, model and table there was built from a disjoint 10%
of the corpus. These experiments used version 1 of the field models.

| Method (each message decodable on its own) | Ratio |
|---|---:|
| zstd -3 / gzip -6 / zstd -19 per message | 2.14× / 2.22× / 2.22× |
| brotli -11 per message | 2.66× |
| zstd -19 + trained 110 KB / 1 MB dictionary | 3.47× / 3.98× |
| emz field transform + zstd -19 + 1 MB dictionary | 4.58×² |
| primed `emcm` (model trained on 32 MB of mail, then frozen) | 6.75×¹ |
| field transform + primed `emcm` | 7.89×¹ ² |
| + single-instance storage of repeated content (a copy = reference + its own Message-ID and folder fields) | **14.3×¹ ²** |
| *emz, solid archive (for comparison)* | *26.4× (version 1: 25.1×)* |

¹ Measured on the 1% subsample. On that subsample, zstd with the 110 KB
dictionary gives 3.44×. The single-instance row also charges a 2-byte length
per stored message.
² Size of a prototype transform's output. Its field codecs are the verified
emz ones, but the per-message inverse is not implemented yet.

Most of the redundancy in a mail archive sits *between* messages: copies,
quotes, recurring headers. Per-message zstd/gzip captures almost none of it. A
frozen context-mixing model, the field transform and single-instance storage
recover more than half of the archive ratio while keeping every message
independently readable.

## Limitations and generality

* **Corpus-specific parts.** The Message-ID generator model, the JavaMail
  folding rule and the X-Folder/X-Origin/X-FileName models are specific to
  Enron's JavaMail conversion. Everything else is generic RFC 5322 / MIME
  modelling. All models follow "predict, verify, otherwise escape", so on
  mail they do not fit they cost only escape symbols.
* **Verbatim fallback.** A message whose header does not parse (bare CR or
  mixed line endings in the header, bytes 0x01/0x02 that the streams use as
  separators) is stored verbatim and coded by `emcm` alone. That applies to
  76 of Enron's 517,401 messages and to none of the recent corpus.
* **Lossless by construction.** Every parsed field and every decoded MIME
  part is re-serialised and compared with the original before its model is
  used. Both full-corpus round trips are SHA-256 identical.
* **Resources.** About 3.2 GB of RAM in each direction for Enron: the 2 GB
  hash table for the body model, a quote-stripped copy of the body text
  (0.47 GB) for the quote-aware match model, and smaller tables (the run
  model's is 64 MB). The recent corpus needs 0.9 GB.
  The decoder must rebuild all earlier messages (no random access).
* **Back end.** On the same body text, paq8px's output is 13–17% smaller
  than `emcm`'s (samples of both corpora). A stronger body model, such as an
  LSTM or paq8-style word and indirect models, would shrink the dominant body
  share directly.
* **Directories.** File names, the directory structure and empty directories
  are restored; file times, permissions and links are not (a symbolic link in
  the input is an error).
* **What cannot be compressed.** Cryptographic signatures, encrypted
  anti-spam reports and already-compressed attachments are close to random.
  On the recent corpus they set a floor of roughly 8–9 MB, over 40% of emz's
  output.
* **Outlook files.** `.msg` files are supported; PST and OST stores are not
  yet (see [`docs/IMPROVEMENTS.md`](docs/IMPROVEMENTS.md)). The RTF match
  finder runs in Python: about 0.14 MB/s of RTF text when compressing
  (prediction plus verification) and 0.29 MB/s when decompressing.
  The `.msg` samples are small (25 files), so the numbers above show the
  method rather than what a large Outlook archive would give.

## Related approaches

* **XMill** (Liefke and Suciu, SIGMOD 2000): separates XML structure from
  content, groups values into containers by path, and compresses each
  container on its own. emz applies the same principle to RFC 5322 fields and
  adds field-specific semantic models and archive-level de-duplication.
* **Mail servers** compress each message on its own. Dovecot's
  `mail_compress` plugin, for example, uses gzip or zstd at default level 3.
  Servers de-duplicate only attachments ("single-instance storage"). The
  random-access table above shows the cost of that design.
* **Long-range compressors** (rzip/lrzip, zstd's long-distance matcher)
  remove repeats at any distance but know nothing about fields. On Enron they
  are the strongest existing tools.
* **paq8px / cmix / zpaq** have the best general-purpose models but run at a
  few KB/s (paq8px, cmix), which is impractical for a 1.4 GB archive. They
  could serve as a stronger emz back end.

## Algorithm

Every message is split into **header fields** and **body**. Each field gets a
model that knows what the field means, and every model writes its decisions to
its own stream (the XMill "containers" idea). A bitwise context-mixing (CM)
coder then entropy-codes each stream.

### 1. Parsing and the verbatim fallback

* Directory trees are read in natural path order (`2.` before `10.`). In an
  mbox file a message starts at every line that begins with `From `; that
  line is coded on its own (table below) and the rest is the message.
* The header ends at the first empty line. Each field line must be
  `Name: value` (or `Name:value`), possibly folded with continuation lines
  that start with a space or tab. The line ending (CRLF or LF) is taken from
  the first line and must be the same throughout the header.
* A message that does not parse goes to the `raw` stream unchanged.

### 2. Content de-duplication (archive level)

The same e-mail is often stored many times. In Enron it sits in
*all_documents*, *discussion_threads*, *sent* and *inbox* of one user, and in
the sender's and every recipient's mailbox: of the 517,325 messages that
parse, only 256,957 have distinct content.

* **Content key** = every header field except the per-copy ones (Message-ID,
  folder and delivery fields such as X-Folder, Received, Delivered-To,
  Return-Path, client status flags), plus the body.
* A repeated key becomes a **reference**: the index of the earlier content,
  coded as a delta from *previous reference + 1*. Copies in two folders tend
  to appear in the same order, so the delta is usually small. A bit mask then
  names the fields that differ from the earlier copy, and only those are
  coded.
* Bodies are de-duplicated again on their own (same body, different headers).

### 3. Field models

Fields are coded in a fixed order: Message-ID, In-Reply-To and References
first, then Date and From, then the other modelled fields in header order,
then the body, and last the fields without a model of their own, so that
these can refer to everything else.

| Field | Model |
|---|---|
| Field-name sequence | Move-to-front list of the last 254 templates (name sequence plus `Name:value` spacing); a new template is coded name by name |
| Path | same folder and next file number → one symbol; otherwise folder name and number |
| mbox `From ` line | Sender as a reference to Return-Path, From or X-GM-THRID, else a move-to-front value; time in seconds relative to the topmost Received header (else Date); format (day padding, time zone, trailing text, line end) as a move-to-front value. Lines of any other shape are stored as they are |
| Message-ID, In-Reply-To, References | IDs are tokens; a token seen before is coded as the distance to its last occurrence. References are predicted as the parent's References followed by the parent's ID, In-Reply-To as the last reference |
| Message-ID `<N1.N2.JavaMail.evans@thyme>` (Enron) | **N2** is the converter's millisecond clock and is coded as a delta (≈23 ms per message). **N1** is the low 25 bits of the JVM's Park–Miller generator *x → 16807·x mod (2³¹−1)*, which advances once per message. The model keeps the full 31-bit state and codes N1 as the number of generator steps (almost always 1); the hidden 6 bits are recovered once per folder. Cost falls from 25 bits to ≈0.6 bits per message |
| Date | Parsed to minutes, seconds and time zone; weekday and zone name are derived. Coded as a minute delta from the previous message in the same folder, and verified by re-serialising. The format variant (weekday or not, seconds or not, zone comment) is a move-to-front symbol |
| Received, X-Received | The text before the `;` is a move-to-front value; the timestamp is coded in seconds relative to the Date header, with its format variant |
| Subject | Prefix (`Re:`, `Fwd:`, `[list]`, …) and base subject. The base is usually the parent's base subject (one flag); otherwise it comes from a global move-to-front list or a literal |
| From / To / Cc and other address fields | Move-to-front list per folder. Enron: unfolded with the JavaMail folding rule (wrap before an address that would pass column 76, CRLF TAB); only the plain list is stored |
| Bcc | One flag when identical to Cc (true for every Enron message that has a Cc) |
| DKIM-Signature, ARC-*, X-Google-*, X-Gm-*, X-MS-*, Authentication-Results, … | Base64 runs of 16+ characters are cut out (random), t=/x= timestamps are coded relative to Date, and the remaining skeleton is a move-to-front value keyed by field name and sender domain. A bh= body hash that the body reproduces (RFC 6376 simple or relaxed canonicalisation, SHA-1 or SHA-256, l= limit) is not stored |
| Content-Type | Move-to-front list; a `boundary=` value equal to the body's first delimiter line is replaced by a marker |
| Mime-Version, Content-Transfer-Encoding, List-*, Precedence, Reply-To | Move-to-front list per field (≈0 bits) |
| User-Agent, X-Mailer, Autocrypt, Organization, OpenPGP, Face, … | Move-to-front list keyed by the From address |
| X-From (Enron) | Predicted from the From address (names seen with that address) |
| X-To / X-cc (Enron) | Split into one display name per address (Notes style `First Last, …` or Outlook style `Last, First <…>, …`). When the counts match, each name is predicted from the names previously seen with *that address*; otherwise the whole value is coded against the values seen with the same address list |
| X-Folder / X-Origin / X-FileName (Enron) | Move-to-front per maildir folder, per mailbox owner, and keyed by X-Folder |
| Every other field | Copies of the message's Message-ID, parent ID, sender address and name, subject (with and without prefix), time, time zone, body size and body line count are replaced by markers; the result is a move-to-front value per (field, folder). An `X-VM-v5-Data` value becomes the same template in every message, apart from a few dates and flags |

Literal strings go to text streams, newline-terminated, so the CM back end
sees each kind of literal on its own.

### 4. Body transforms

* **MIME.** A multipart body is split at its delimiter lines (recursively, up
  to 8 levels); part headers and delimiter lines stay text. A
  quoted-printable part is decoded and re-encoded with one of 8 encoder
  variants (tabs and trailing spaces as `=09`/`=20` or literal, lines of
  exactly 76 columns broken or not, a final soft line break or not); the
  variant that reproduces the part exactly is stored. A base64 part is
  decoded and stored with its line length and padding. Decoded data that
  looks like text joins the body text stream; the rest goes to a binary
  stream. A part that no variant reproduces is kept as encoded text.
* **Line endings**: CRLF text is stored with LF plus a flag when that is
  reversible.
* **Ends of text.** Every text part ends with a two-byte mark in the body
  text stream (a zero byte inside the text is escaped), so no length is
  stored and the coder learns how bodies end. Binary parts keep a length.
* Bodies are concatenated in archive order (owner → folder → number for
  Enron, date for the list archive). Re-ordering Enron by date or by thread
  was tested; neither helped, and date order was 1.1% worse.

### 5. Outlook .msg files

See [Outlook .msg files](#outlook-msg-files) for the parts. Stream order
inside a `.msg`: transport headers, the strings other values repeat
(subject, sender, recipients, Message-ID), other strings, binary values,
property streams, named-property maps, plain-text body, HTML body, RTF body,
attachments. The skeleton is decoded first, so the decoder knows every
stream's name and size before any content. `emz_msg.py` holds the compound
file reader and skeleton coder and the compressed-RTF codec; `rtf_compress`
there compresses RTF the way Outlook does.

### 6. Entropy coding: `emcm`

`emcm.c` is a bitwise context-mixing coder in the paq/lpaq family, written for
this project:

* Contexts: orders 1, 2, 3, 4, 5, 8; current word; word bigram; column with
  the byte above (for tables and quoted blocks). They live in nibble-bucketed
  hash tables (16-byte buckets, checksum, 4-way search, replacement by
  priority).
* Each context node holds a bit-history state, an (n0, n1) pair with
  non-stationary discounting. Adaptive StateMaps map states to probabilities,
  giving two inputs per context.
* A run model remembers, for each context, the last byte seen there and how
  many times in a row it occurred (a table of 32-bit slots, 1/32 of the main
  table's size); while the bits of the current byte agree with that byte, it
  adds one input per context whose weight a StateMap learns by run length.
* Direct (unhashed) tables for order 1 and order 2 add two more inputs.
* Two match models (minimum lengths 6 and 24) index the whole stream history;
  the long one indexes every 4th position. A third match model works on the
  text with quote prefixes (`> `, `>> `, `> > `) removed, so a quoted reply
  is predicted from the original message even though every line now starts
  differently. Quoted replies and forwards that repeat earlier mail cost about
  0.05 bit/char.
* Three gated logistic mixers (selected by partial byte, match state × number
  of known contexts × quote-match state, and previous byte × partial byte)
  feed a final mixer, followed by four APM/SSE stages (order 0, order 1,
  order 2, match byte). Their outputs are averaged with fixed weights and,
  in parallel, combined by a small learned mixer; the two results are
  averaged. The arithmetic coder uses 12-bit probabilities.
* The body stream gets up to a 2 GB hash table; smaller streams get tables
  sized to their length. Streams are coded in parallel (two workers).

## Further improvements

* [`docs/IMPROVEMENTS.md`](docs/IMPROVEMENTS.md): what version 2 implemented
  with the measured gains, and the ranked remaining ideas, from a stronger
  body model to reply-block parsing and Received chains.
* [`docs/PER_MESSAGE.md`](docs/PER_MESSAGE.md): what limits the ratio when
  every e-mail must be compressed on its own (random access), and what was
  measured to help.

## Repository layout

| Path | Contents |
|---|---|
| `emz.py` | parser, field and MIME models, streams, container, decoder |
| `emcm.c` | context-mixing back end (build with `make`) |
| `qp.py` | quoted-printable decoder and the 8 encoder variants |
| `emz_msg.py` | Outlook `.msg`: compound-file reader, skeleton coder, compressed-RTF codec with Outlook's match finder |
| `tests/` | unit and round-trip tests, and two sample corpora chosen to exercise every code path: `enron_sample.corpus` (564 Enron messages, 1.1 MB) and `oss_sample.corpus` (184 oss-security messages, 1.2 MB); `msgwriter.py` writes synthetic `.msg` files for the `.msg` tests |
| `scripts/fetch_enron.sh`, `scripts/build_corpus.py` | download the Enron corpus and build the canonical file |
| `scripts/fetch_oss_security.sh`, `scripts/build_inbox_corpus.py` | the same for the oss-security mirror |
| `scripts/make_sample.py`, `scripts/make_sample_modern.py` | how the test samples were selected |
| `scripts/bench.py`, `scripts/make_tables.py` | baseline benchmark runner (compress, decompress, SHA-256 check) and tables |
| `scripts/ablation.py`, `scripts/run_ladder.sh`, `scripts/stream_backends.py`, `scripts/paq_streams.sh`, `scripts/header_literals.py` | ablation and cost-breakdown experiments |
| `scripts/permsg.py`, `scripts/permsg_analysis.py`, `scripts/emcm_permsg.c` | per-message (random-access) experiments |
| `results/` | raw logs and JSON lines behind every table |
| `docs/` | improvement notes and the per-message analysis |

## Portability note

`emcm` mixes predictions with float arithmetic. An archive decodes only with
a build that rounds identically, so the Makefile disables fused multiply-add
contraction (`-ffp-contract=off`), and the mixers add up their inputs in 8
partial sums in a fixed order, which the compiler may vectorize without
changing the result. With GCC's default contraction and `-march=native`,
archives would not decode with a differently built binary. CI checks that
archives written by an `-O3` build decode with an `-O1` build, on both x86-64
and arm64, and every decompression checks the SHA-256 of the original input.

## License and credits

GPL-3.0 (see `LICENSE`). The design of `emcm` follows the paq/lpaq family of
context-mixing compressors by Matt Mahoney and contributors: bit-history
states, StateMaps, logistic mixing and APM/SSE stages. The Enron corpus is
the CMU release of 2015-05-07 by William W. Cohen and the CALO project. The
oss-security list is run by Openwall; its messages belong to their authors.
The `.msg` measurements used the sample files of the MSGReader (MIT) and
msg-extractor (GPL-3.0) projects, which are not included here. The RTF match
finder follows Haruhiko Okumura's LZSS.C (1989).
