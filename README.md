# emz — structure-aware compression of e-mail archives

`emz` is a lossless compressor for mail archives (maildir trees, mbox
collections) that models what an e-mail *is* instead of treating the archive
as a flat byte stream. It was developed and evaluated on the Enron corpus
(CMU release of 2015-05-07, 517,401 messages, 1.44 GB). Every model checks that
it reproduces the original bytes exactly. Anything that does not fit is stored
verbatim, so the output is always a byte-exact copy.

On the full corpus:

* emz produces **57.35 MB (25.1×)**, against 80.3 MB for the best existing
  tool (lrzip -z) and 94.5 MB for `zstd --ultra -22 --long=31`.
* If each message must stay readable on its own, today's per-message
  zstd/gzip gets 2.1–2.2×. The per-message variant of the same ideas measured
  **14.3×** on a 1% sample: static field tables, a frozen context-mixing model
  and single-instance storage of repeated content (see
  [`docs/PER_MESSAGE.md`](docs/PER_MESSAGE.md)).

## Quick start

```sh
make                 # builds ./emcm (the context-mixing coder); needs a C compiler
make test            # unit tests + round trip of tests/data/enron_sample.corpus

python3 emz.py c tests/data/enron_sample.corpus sample.emz    # compress
python3 emz.py d sample.emz sample.out                        # decompress
cmp tests/data/enron_sample.corpus sample.out
```

Input and output use a simple *corpus* format, which works for any maildir
tree: for each message, `path\n`, `length\n`, then the raw message bytes.

The full Enron corpus is not stored in this repository. To fetch it from a
GitHub mirror and serialise it (about 2.2 GB of disk):

```sh
scripts/fetch_enron.sh data          # -> data/enron.corpus, 1,437,509,559 bytes
python3 emz.py c data/enron.corpus data/enron.emz
```

Python 3.8+ only; no packages are needed for emz itself. The benchmark
scripts in `scripts/` also use `zstandard` and `brotli` (pip) and the
command-line compressors they compare against. A full-corpus run needs about
3 GB of RAM.

## Results

### Full Enron corpus

The corpus has 517,401 messages and 1,437,509,559 bytes. All tools received
the same single canonical file. Every result below was decompressed and checked
against the original with SHA-256.

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
| **emz (this work)** | **57.35** | **25.1×** | 20.2 | 19.8* | 2.6 |

\* The archive (57,351,451 bytes) was written by the repository build
(`-march=native`, `-ffp-contract=off`) and decoded byte-exact by a separately
built generic `-O3` binary. The decompression time is from an earlier,
otherwise identical run with less load on the machine. In the verification run
the decoder shared the 2 cores with three other jobs and took 33.7 minutes
(24.7 CPU-minutes).

Setup:

* **Corpus.** Fetched from the GitHub mirror `malted/enron`. All 517,401
  files are hash-identical to a second, independent mirror
  (`agocs/enronDataScience`). Serialised in path order as
  `path\n length\n bytes`.
* **Tools.** gzip 1.12, bzip2 1.0.8, xz 5.4.5, zstd 1.5.5, brotli 1.1.0,
  7-Zip 23.01, lrzip 0.651, zpaq 7.15, bsc 3.4.1, paq8px v217.
* **Machine.** 2 vCPU (Intel Xeon, 2.1 GHz), 8 GB RAM. zstd and xz ran
  single-threaded; bsc, lrzip and zpaq used both cores. emz codes two streams
  in parallel.

emz is **29% smaller** than the best existing tool (lrzip -z) and **39%
smaller** than zstd at its strongest long-range setting. The price is speed:
about 1.2 MB/s in each direction (about 21 CPU-minutes each way). That suits
archival storage and is far too slow for interactive use. Timings come from a
shared 2-vCPU VM where several jobs ran at once, so treat them as approximate.
paq8px was not feasible on the full corpus: it ran at 5.4 KB/s, which
extrapolates to about 3 days.

### Where the bits go (emz, full corpus)

| Stream group | Input to coder (bytes) | Coded (bytes) | Share |
|---|---:|---:|---:|
| Body text (after de-dup and CRLF/QP normalisation) | 467,299,989 | 50,830,823 | 88.6% |
| X-From / X-To / X-cc / X-bcc display names | 16,373,692 | 2,139,330 | 3.7% |
| From / To / Cc address lists | 29,913,572 | 1,702,691 | 3.0% |
| Subject | 6,999,966 | 1,226,907 | 2.1% |
| Date | 661,839 | 427,289 | 0.7% |
| Body lengths, modes, body references | 785,172 | 399,996 | 0.7% |
| Message-ID | 1,094,987 | 268,858 | 0.5% |
| Message kind, template flags, duplicate references | 1,137,724 | 227,087 | 0.4% |
| Path | 622,764 | 46,861 | 0.1% |
| X-Folder / X-Origin / X-FileName | 1,920,225 | 36,613 | 0.1% |
| Verbatim fallback and escapes | 194,232 | 26,414 | <0.1% |
| MIME headers | 770,968 | 18,270 | <0.1% |
| **Total** | 527,775,130 | **57,351,139** | |

The 462 MB of raw headers code to 6.5 MB, about 12.6 bytes per message,
including 0.52 bytes per Message-ID. What remains is almost entirely
first-occurrence body text.

### Ablations

**Same back end, with and without the structure models (full corpus).** On
the raw corpus, `emcm` gives 69.62 MB. This run measured size only, with the
same 2 GB table as the body stream in emz. The structure models bring that to
57.35 MB, 17.6% smaller. So emz's 29% lead over lrzip -z has two sources: the
back end alone is already 13% below lrzip -z, and the structure models
provide the rest (a little over half of the gap).

**The transform helps every back end (full corpus).** Here the emz streams are
coded with standard compressors instead of `emcm`:

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
| Raw corpus → `emcm` | 5,976,320 | — |
| + header/body separation | 5,838,565 | −2.3% |
| + message and body de-duplication | 5,597,695 | −6.3% |
| + field models, QP decoding, PRNG Message-ID (full emz) | 5,310,297 | −11.1% |

The subset is the first 100 MB in path order: 12 mailboxes, allen-p through
part of buy-r. It has fewer cross-mailbox copies than the full archive, so the
gains are smaller here than on the full corpus.

Effects of individual models, measured on the subset:

* Park–Miller Message-ID model: the N1 stream fell from 132,425 to 4,711 bytes
  (−96%). On the full corpus N1 costs 41 KB for 517k IDs, against 1.6 MB for
  25 random bits each.
* QP decoding: body stream −2.3%.
* Address-aligned X-To/X-cc name model: display-name streams −16%.

### 100 MB subset: all tools

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
| `emcm` alone | 5,976,320 | 16.7× |
| **emz** | **5,310,297** | **18.8×** |

### Small sample against the strongest general-purpose CM (5 MB, 2,835 messages)

| Compressor | Size (bytes) | Time |
|---|---:|---:|
| paq8px v217 -8 | 275,520 | 15 min |
| **emz transform + paq8px as back end** | **268,245** | ≈7 min |
| emz (`emcm` back end) | 327,403 | 4 s |
| `emcm` alone | 357,156 | 11 s |
| zpaq -m5 | 362,869 | 15 s |
| lrzip -z | 410,816 | 2 s |
| xz -9e | 471,456 | 2 s |
| zstd -22 --long=31 | 494,841 | 4 s |

On a sample this small there is little duplication, and paq8px's much stronger
text model beats emz with `emcm`. Putting the emz transform in front of paq8px
still makes it 2.6% smaller, which confirms that the structure models add to
even the strongest CM. A stronger back end for the body stream is the obvious
next improvement.

### Random access: compressing each message on its own

Mail servers usually compress each message on its own; Dovecot's
`mail_compress` plugin, for example, uses zstd or gzip. A test on a 10%
sample (51,741 messages, every 10th) and a 1% subsample (5,175 messages) is
written up in [`docs/PER_MESSAGE.md`](docs/PER_MESSAGE.md). Every shared
dictionary, model and table there was built from a disjoint 10% of the corpus.

| Method (each message decodable on its own) | Ratio |
|---|---:|
| zstd -3 / gzip -6 / zstd -19 per message | 2.14× / 2.22× / 2.22× |
| brotli -11 per message | 2.66× |
| zstd -19 + trained 110 KB / 1 MB dictionary | 3.47× / 3.98× |
| emz field transform + zstd -19 + 1 MB dictionary | 4.58×² |
| primed `emcm` (model trained on 32 MB of mail, then frozen) | 6.75×¹ |
| field transform + primed `emcm` | 7.89×¹ ² |
| + single-instance storage of repeated content (a copy = reference + its own Message-ID and folder fields) | **14.3×¹ ²** |
| *emz, solid archive (for comparison)* | *25.1×* |

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
  Enron's JavaMail conversion. They follow "predict, verify, otherwise
  escape", so on other mail they only cost escape symbols. The parser is
  stricter: it accepts only Enron's 17-field header template. A message that
  does not match is stored verbatim and coded by `emcm` alone, which is about
  the "emcm alone" row above. Using emz on general mail requires coding the
  field-name sequence explicitly (as a template ID plus exceptions) and adding
  models for `Received:` chains, MIME multipart trees and Base64 parts. The
  core ideas carry over to any archive unchanged: content de-duplication
  across folders, typed models for dates and IDs, address-list unfolding,
  address-to-display-name prediction, transfer-encoding decoding with
  verified re-encoding (QP here, Base64 likewise), and per-field streams fed
  to a CM.
* **Lossless by construction.** Every parsed field is re-serialised and
  compared with the original before its model is used. The full-corpus round
  trip is SHA-256 identical.
* **Resources.** About 2.6 GB of RAM in each direction, most of it the 2 GB
  hash table for the body model. The decoder must rebuild all earlier messages
  (no random access).
* **Back end.** On the same body text, paq8px's output is about 17% smaller
  than `emcm`'s. A stronger body model, such as an LSTM or paq8-style word and
  indirect models, would shrink the dominant 88.6% share directly.


## Related approaches

* **XMill** (Liefke and Suciu, SIGMOD 2000): separates XML structure from
  content, groups values into containers by path, and compresses each
  container on its own. emz applies the same principle to RFC 822 fields and
  adds field-specific semantic models and archive-level de-duplication.
* **Mail servers** compress each message on its own. Dovecot's
  `mail_compress` plugin, for example, uses gzip or zstd at default level 3.
  Servers de-duplicate only attachments ("single-instance storage"). The
  random-access table above shows the cost of that design.
* **Long-range compressors** (rzip/lrzip, zstd's long-distance matcher)
  remove repeats at any distance but know nothing about fields. They are the
  strongest existing tools here, and emz beats them by 29–39%.
* **paq8px / cmix** have the best general-purpose text models but run at a few
  KB/s, which is impractical for a 1.4 GB archive. They could serve as a
  stronger emz back end.

## Algorithm

Every message is split into **header fields** and **body**. Each field gets a
model that knows what the field means, and every model writes its decisions to
its own stream (the XMill "containers" idea). A bitwise context-mixing (CM)
coder then entropy-codes each stream.

### 1. Parsing and the verbatim fallback

* The header is split at the first blank line (`CRLF CRLF`). Each field line
  must be `Name: value`, possibly folded with `CRLF SP/TAB` continuations. The
  field sequence must be one of the corpus templates (17 fields, with To and
  Cc/Bcc optional).
* A message that does not parse (bare LF or CR in the header, an unknown field
  order, control bytes used as internal separators) goes to the `raw` stream
  unchanged. In Enron that applies to 76 of the 517,401 messages.

### 2. Content de-duplication (archive level)

The same e-mail is stored many times: in *all_documents*,
*discussion_threads*, *sent* and *inbox* of one user, and in the sender's and
every recipient's mailbox. Of the 517,325 messages that parse, only 256,957
have distinct content. The other 260,368 are copies.

* **Content key** = every header field except the per-copy ones (Message-ID,
  X-Folder, X-Origin, X-FileName), plus the body.
* A repeated key becomes a **reference**: the index of the earlier content,
  coded as a delta from *previous reference + 1*. Copies in two folders tend
  to appear in the same order, so the delta is usually small.
* After a reference, only the per-copy fields are coded, and those are
  predictable as well (see below).
* Bodies are de-duplicated again on their own (same body, different headers).

### 3. Field models

| Field | Model |
|---|---|
| Path | same folder and next file number → one symbol; otherwise folder name and number |
| Message-ID `<N1.N2.JavaMail.evans@thyme>` | **N2** is the converter's millisecond clock and is coded as a delta (≈23 ms per message). **N1** is the low 25 bits of the JVM's Park–Miller generator *x → 16807·x mod (2³¹−1)*, which advances once per message. The model keeps the full 31-bit state and codes N1 as the number of generator steps (almost always 1); the hidden 6 bits are recovered once per folder. Cost falls from 25 bits to ≈0.6 bits per message |
| Date | Parsed to minutes, seconds and time zone. Weekday and zone name are derived. Coded as a minute delta from the previous message in the same folder, and verified by re-serialising |
| From / To / Cc | Unfolded with the JavaMail folding rule (wrap before an address that would pass column 76, CRLF TAB). Only the plain list is stored; the folding is recomputed on decode |
| Bcc | One flag: identical to Cc (true for every Enron message that has a Cc) |
| To, Cc, Subject, From | Move-to-front list of recent values *per folder*; a miss is followed by a literal |
| Mime-Version, Content-Type, Content-Transfer-Encoding | Move-to-front list per field (≈0 bits) |
| X-From | Predicted from the From address (names seen with that address) |
| X-To / X-cc | Split into one display name per address (Notes style `First Last, …` or Outlook style `Last, First <…>, …`). When the counts match, each name is predicted from the names previously seen with *that address*; otherwise the whole value is coded against the values seen with the same address list |
| X-Folder | Move-to-front per maildir folder (usually 1, at most a handful of distinct values per folder) |
| X-Origin | Move-to-front per mailbox owner |
| X-FileName | Move-to-front keyed by X-Folder (the source PST/NSF file is a function of the folder) |

Literal strings go to per-field text streams, newline-terminated, so the CM
back end sees each field's vocabulary on its own.

### 4. Body transforms

* **Line endings**: headers are CRLF and most bodies are LF. A CRLF-only body
  is stored with LF plus a mode flag.
* **Quoted-printable**: QP bodies (4.3% of messages, mostly long newsletters
  and forwards) are decoded to plain text and stored with a flag. On decode
  they are re-encoded with a canonical encoder: 76-column soft breaks, `=09`
  for every TAB, `=20` for a space before a hard break, `=3D`. The encoder
  reproduces 100% of the Enron QP bodies byte for byte. Any body it did not
  reproduce would be stored as-is.
* Bodies are concatenated in archive order (owner → folder → number).
  Re-ordering by date or by thread was tested; neither helped, and date order
  was 1.1% worse.

### 5. Entropy coding: `emcm`

`emcm.c` is a bitwise context-mixing coder in the paq/lpaq family, written for
this project:

* Contexts: orders 1, 2, 3, 4, 6, 8; current word; word bigram; column with
  the byte above (for tables and quoted blocks). They live in nibble-bucketed
  hash tables (16-byte buckets, checksum, replacement by priority).
* Each context node holds a bit-history state, an (n0, n1) pair with
  non-stationary discounting. Adaptive StateMaps map states to probabilities,
  giving two inputs per context.
* Two match models (minimum lengths 6 and 24) index the whole stream history.
  Quoted replies and forwards that repeat earlier mail therefore cost about
  0.05 bit/char.
* Three gated logistic mixers (selected by partial byte, match state × number
  of known contexts, and previous byte × bit position) feed a final mixer, followed by three
  APM/SSE stages (order 0, order 1, match byte). The arithmetic coder uses
  12-bit probabilities.
* The body stream gets a 2 GB hash table; smaller streams get tables sized to
  their length. Streams are coded in parallel (two workers).

## Further improvements

* [`docs/IMPROVEMENTS.md`](docs/IMPROVEMENTS.md): ranked ideas for archive
  mode, from a stronger body model to reply-block parsing and cross-field
  context, each with the measurement that motivates it.
* [`docs/PER_MESSAGE.md`](docs/PER_MESSAGE.md): what limits the ratio when
  every e-mail must be compressed on its own (random access), and what was
  measured to help.

## Repository layout

| Path | Contents |
|---|---|
| `emz.py` | parser, field models, streams, container, decoder |
| `emcm.c` | context-mixing back end (build with `make`) |
| `qp.py` | canonical quoted-printable decoder and encoder |
| `tests/` | unit and round-trip tests; `tests/data/enron_sample.corpus` holds 564 Enron messages (1.1 MB) chosen to exercise every code path |
| `scripts/fetch_enron.sh`, `scripts/build_corpus.py` | download the corpus and build the canonical file |
| `scripts/make_sample.py` | how the test sample was selected |
| `scripts/bench.py`, `scripts/make_tables.py` | baseline benchmark runner (compress, decompress, SHA-256 check) and tables |
| `scripts/ablation.py`, `scripts/run_ladder.sh`, `scripts/stream_backends.py`, `scripts/paq_streams.sh` | ablation experiments |
| `scripts/permsg.py`, `scripts/permsg_analysis.py`, `scripts/emcm_permsg.c` | per-message (random-access) experiments |
| `results/` | raw logs and JSON lines behind every table |
| `docs/` | improvement notes and the per-message analysis |

## Portability note

`emcm` mixes predictions with float arithmetic. An archive decodes only with
a build that rounds identically, so the Makefile disables fused multiply-add
contraction (`-ffp-contract=off`). With GCC's default contraction and
`-march=native`, archives would not decode with a differently built binary.
CI checks that an archive written by an `-O3` build decodes with an `-O1`
build, on both x86-64 and arm64.

## License and credits

GPL-3.0 (see `LICENSE`). The design of `emcm` follows the paq/lpaq family of
context-mixing compressors by Matt Mahoney and contributors: bit-history
states, StateMaps, logistic mixing and APM/SSE stages. The Enron corpus is
the CMU release of 2015-05-07 by William W. Cohen and the CALO project.
