# Compressing each e-mail separately (random access)

Mail servers usually store and compress each message on its own, so that a
single message can be read without decoding anything else. Dovecot's
`mail_compress` plugin, for example, compresses every message file with gzip
or zstd. This note measures what limits the ratio in that setting and what
helps, on the Enron corpus.

**Rule of the game.** Every message must decode on its own. It may use
*static* shared state built from other mail: a dictionary, a frozen model or
lookup tables, like a zstd dictionary. Section 5 relaxes the rule with storage
designs that let a message refer to one other stored object.

**Data.**

* Test set S10: every 10th message (51,741 messages, 142.3 MB).
* Test set S1: every 100th message (5,175 messages, 14.3 MB), used for the
  slower experiments.
* Training set: a disjoint 10% (index mod 10 = 5). Every dictionary, model and
  table below is built from the training set only.

Scripts: `scripts/permsg.py` (baselines), `scripts/permsg_analysis.py`,
`scripts/emcm_permsg.c` and `scripts/permsg_combine.py`. Raw results are in
`results/per_message_10pct.txt` and `results/permsg_*.jsonl`.

## 1. Baselines: no shared state

| Method (S10) | Ratio |
|---|---:|
| zstd -3 (Dovecot's default zstd level) | 2.14× |
| gzip -6 | 2.22× |
| zstd -19 | 2.22× |
| brotli -11 (has a built-in static dictionary) | 2.66× |
| *emz on the whole archive, for reference (version 1 / version 2)* | *25.1× / 25.8×* |

The average message is 2.75 KB, too short for an adaptive compressor to learn
much. The archive coder's tenfold advantage comes almost entirely from
*between-message* redundancy.

## 2. Where the bytes go

With zstd -19 and a 110 KB trained dictionary (separate dictionaries for
header and body, S10):

| Part | Raw per message | Compressed per message | Share of output |
|---|---:|---:|---:|
| Header | 882 B | 187 B | 24% |
| Body | 1,868 B | 602 B | 76% |

Marginal cost of each header field (the size drop when the field is removed;
S1, 184 bytes of compressed header per message):

| Field | Share of header bytes | emz model that removes most of it |
|---|---:|---|
| X-To | 20.7% | names predicted from the To addresses |
| To | 14.1% | address-book IDs |
| Subject | 10.0% | (text; thread reference) |
| Message-ID | 8.7% | Park–Miller generator anchors |
| Date | 4.1% | binary minutes, seconds and time zone |
| X-From, X-cc | 3.5%, 3.4% | names predicted from addresses |
| X-Folder, X-Origin, X-FileName | 4.0% together | per-folder table |
| From, Cc, Bcc | 1.6%, 0.6%, 0.6% | address-book IDs; Bcc = Cc flag |

The fields are short, unique-looking strings: IDs, addresses and names. A
general-purpose dictionary helps little with them, but a model that knows
their meaning makes them nearly free.

## 3. What helps: measured

| Method | Ratio (S10) | Ratio (S1) |
|---|---:|---:|
| zstd -19, no dictionary | 2.22× | |
| zstd -19 + trained dictionary 110 KB | 3.47× | 3.44× |
| zstd -19 + trained dictionary 1 MB | 3.98× | |
| zstd -19 + raw-content dictionary 8 MB (recent training mail) | | 4.05× |
| zstd -19 + raw-content dictionary 32 MB | | 4.46× |
| **field transform** + zstd -19 + dictionary 110 KB | 4.03× | 3.97× |
| **field transform** + zstd -19 + dictionary 1 MB | **4.58×** | |
| **primed CM** (`emcm` trained on 8 MB of mail, frozen; 64 MB table) | | 5.69× |
| **primed CM** (trained on 32 MB of mail, frozen; 256 MB table) | | **6.75×** |
| **field transform + primed CM** (8 MB / 32 MB of training mail) | | 6.54× / **7.89×** |

**Field transform.** This is a per-message version of the emz field models
with static tables:

* an address book of 34.5k addresses;
* an address → display-name map of 32.3k names;
* a per-folder table of X-Folder, X-Origin and X-FileName (2.3k folders);
* MIME combinations;
* 47.8k Message-ID generator anchors.

The tables total 3.7 MB before compression. A Message-ID whose folder has a
nearby anchor is computed as `16807^(n−n₀) · x₀ mod (2³¹−1)` from the
message's file number *n*. That covers 94.6% of messages. The Message-ID
then costs about 2 bytes (the clock residual) instead of about 16 compressed
bytes. Bodies get the same quoted-printable decoding and CRLF normalisation as
in emz.

The transform is worth 15–17% on top of any back end: 3.47→4.03 with a
110 KB dictionary, 3.98→4.58 with 1 MB, and 6.75→7.89 with the primed CM. The
numbers measure the size of the transformed representation. Its inverse is
not implemented here, but every field codec is one that emz verifies.

**Primed context-mixing model.** This is the per-message counterpart of
emz's back end. `emcm` is trained once on recent mail, and its state (hash
tables, bit histories, mixer weights, match history) is frozen and shared by
encoder and decoder. Each message is coded from that state as if it were the
continuation of the training text. It roughly doubles the zstd-dictionary
ratio (6.75× against 3.44× on S1), because a CM generalises from statistics
where a dictionary only offers literal matches. Costs:

* 0.2–0.4 GB of model memory, shared read-only by all decoders.
* Per-message coding at about 0.45 MB/s, i.e. about 6 ms for an average
  message, plus restoring the frozen state. A server would use copy-on-write
  pages or an overlay of modified buckets; the experiment uses `fork()`.

## 4. Grouping messages into small blocks

Random access per *block* of consecutive messages (one mailbox folder in
archive order, zstd -19 + 110 KB dictionary):

| Messages per block | 1 | 4 | 16 | 64 | 256 | 1024 |
|---|---:|---:|---:|---:|---:|---:|
| Ratio | 3.44× | 3.96× | 4.39× | 4.81× | 5.17× | 5.65× |

Blocks help slowly, because in Enron most of the redundancy between messages
sits across folders and mailboxes, not within a short run. A good static model
(section 3) beats blocks of 1,000 messages coded with zstd.

## 5. Sharing content between messages while keeping random access

Exactly half of the Enron messages (50.3% of S10) are copies of content
stored elsewhere in the archive (other folders, the other party's mailbox).
These storage designs keep one-message reads cheap:

| Design (S10 unless noted) | Ratio |
|---|---:|
| zstd + 110 KB dictionary (reference point) | 3.47× |
| **Single-instance storage (SIS)**: a copy = 4-byte content reference + its own Message-ID and folder fields; content compressed with zstd + dictionary | 6.41× |
| SIS + field transform | 7.62× |
| Thread parent: code a message against *one* earlier message of the same thread (subject without `Re:`/`Fw:`) used as a zstd raw dictionary; take the better of parent and dictionary | 6.23× |
| **SIS + thread parent** | **7.91×** |
| SIS + primed CM (S1; 8 MB / 32 MB priming) | 10.85× / 12.50× |
| **SIS + field transform + primed CM** (S1; 8 MB / 32 MB priming) | 12.17× / **14.32×** |

The primed-CM rows in this table include a 2-byte length field per stored
message; the rows in section 3 do not.

* **SIS** is cheap: content-addressed blobs (a hash of the content key) and a
  tiny per-copy record. A read costs one extra lookup.
* **Thread-parent coding** found a usable parent for 35.7k of the 51.7k test
  messages, and it won for 8.3k of the non-copies. A read needs the parent
  decoded first. Allowing only parents that are not themselves
  parent-coded caps the chain at one hop; this costs a little ratio and was
  not separately measured.

## 6. Recommendations for a random-access emz

In order of gain per unit of complexity:

1. **Single-instance storage of message content.** About 1.85× by itself
   (3.47 → 6.41). Trivial to implement and verify.
2. **Field transform with static tables.** About +15% on every back end. The
   tables are built from the existing mailbox and versioned like a
   dictionary.
3. **Primed CM instead of a zstd dictionary.** About 2× on the remaining
   bytes (3.44 → 6.75 on S1), at about 6 ms per message and 0.2–0.4 GB of
   shared model. Where that is too slow, use zstd with a 1–32 MB dictionary
   (4.0–4.5×).
4. **Thread-parent references for replies and forwards.** About +23% over
   SIS alone (6.41 → 7.91), with a bounded reference chain.
5. **Retrain the static state periodically:** dictionary, model and tables
   get stale as vocabulary and correspondents change. Keep old versions so
   that old messages remain readable.

Taken together (1+2+3, measured on S1 above), random-access storage reaches
**14.3×**, against 2.1–2.2× for today's per-message zstd/gzip. That is 57% of
the solid-archive ratio of emz version 1 (25.1×), whose field models these
experiments use, while every message stays independently readable.
Thread-parent references (4) were measured only with zstd and would add to
this.

The remaining gap to the archive has two sources:

* The frozen model does not learn from the messages it codes: no adaptation
  to the current thread, the new vocabulary or the folder's own regularities.
* Quoted text in replies is matched only if it is in the training data or the
  parent (step 4).

Periodic retraining narrows the first; thread references narrow the second.
