# Further gains for archive (solid) compression

This note started as a ranked list of ideas after version 1 (Enron only). It
now records what version 2 implemented and measured, and what is left, for
both test corpora. Each estimate is labelled as **measured** (an experiment in
this repository) or **estimated** (reasoning from measurements, not yet
tried).

## Where the bits go now

| Enron, full corpus (54.47 MB) | Coded size | Share |
|---|---:|---:|
| Body text (after de-duplication) | 48.93 MB | 89.8% |
| Header literals (display names, addresses, subjects) | 3.94 MB | 7.2% |
| Move-to-front ranks of header values | 0.54 MB | 1.0% |
| Date | 0.43 MB | 0.8% |
| Message-ID and thread references | 0.27 MB | 0.5% |
| Message kind, copies, templates | 0.26 MB | 0.5% |
| Path, verbatim fallback, MIME structure | 0.11 MB | 0.2% |

| oss-security 2015–2026 (19.92 MB) | Coded size | Share |
|---|---:|---:|
| Body text (including decoded QP and base64 text) | 8.85 MB | 44.4% |
| Signature material (base64 runs in DKIM, ARC, Google, Microsoft fields) | 5.53 MB | 27.8% |
| Binary attachments (decoded base64) | 3.31 MB | 16.6% |
| Header literals | 1.98 MB | 9.9% |
| Everything else | 0.26 MB | 1.3% |

On Enron nearly everything left is first-occurrence body text. On recent mail
almost half of the output is random by construction (signatures, encrypted
anti-spam reports, compressed attachments), which caps what any model can
gain there.

## Implemented in version 2

| Change | Measured gain |
|---|---|
| Generic RFC 5322 header model: template coding of the field-name sequence, per-field move-to-front values, sender-keyed fields | Recent corpus: version 1 could not parse any message (23.05 MB, all verbatim); the version 2 transform takes the same old coder to 20.46 MB (−11.3%) |
| Threads: Message-ID tokens as back-references, References = parent's References + parent, subject = prefix + parent's base subject (item 3 of the old list) | part of the −11.3% above |
| Received / X-Received / signature timestamps relative to Date | part of the −11.3% above |
| DKIM/ARC skeleton: tags and folding as move-to-front values per signer, base64 runs and timestamps split off | part of the −11.3% above |
| DKIM/ARC body hash (bh=) recomputed from the body (55% of DKIM signatures match) | −0.7% (1,500-message sample) |
| Fields without a model refer to other fields and to the body size and line count (Emacs VM's X-VM-v5-Data in 68% of messages; mbox Content-Length/Lines) | −2.8% (sample); X-VM-v5-Data falls from about 570 KB to 137 KB (current coder) |
| Multipart boundary taken from the body's first delimiter line | −0.9% (sample) |
| MIME segmentation with verified quoted-printable (5,394 parts; all 8 encoder variants occur) and base64 (4,249 text parts, 21.8 MB decoded, go to the body stream; 228 binary parts to their own stream) | part of the −11.3% above |
| Copies coded as reference + bit mask of differing fields | together with the subject and generic header models: −1.4% on the Enron 100 MB subset (version 1 → version 2 transform, both with the version 2 coder; the body stream is identical) |
| Quote-aware match model in `emcm`: predicts quoted text from the original through `> ` prefixes (item 2, "quote prefixes") | −1.84% on recent bodies, −0.46% on Enron bodies (12 MB samples) |
| 4-way bucket search, sparse insertion for the long match model, order-2 APM (item 1, "mixer and SSE") | the three together: −0.13% on recent bodies, −0.21% on Enron bodies |
| Run model in `emcm`: per context the last byte seen there and its repeat count, one mixer input per context | switched off on its own: +0.65% on Enron bodies, +0.83% on recent bodies (12 MB samples) |
| Direct order-1 and order-2 inputs; third mixer selected by previous byte × partial byte; learned final mix of the APM outputs; order 5 instead of order 6 (item 1, "mixer and SSE") | switched off one at a time: +0.09/+0.18% (direct inputs), +0.29/+0.29% (third mixer), +0.23/+0.33% (final mix), +0.12/+0.08% (order 6) on Enron/recent bodies. With the run model, the coder is 1.4% (Enron bodies) and 1.8% (recent bodies) smaller than the version 2 coder as first released, at about 1.45× the time |
| End marks instead of lengths for body text parts (item 6, "body length") | −0.63% on the Enron 100 MB subset, −0.33% on the recent sample; the length stream almost disappears and the body stream does not grow |
| Input forms (directory trees, mbox with a `From ` line model, single messages), SHA-256 check on decompression, streams over 2 GiB split | no effect on the benchmarks; a `From ` line costs about 2 bytes on a constructed mbox |

In total: recent corpus 23.05 → 19.92 MB (−13.6%); Enron 57.35 → 54.47 MB
(−5.0%).

## Tried without gain

* **Recompressing deflate data in attachments** (precomp style: inflate,
  then re-deflate with zlib and check that the bytes match). In the recent
  corpus 3.35 MB of attachments are gzip, zip, PDF or PNG, but only 168 KB of
  their deflate streams (5%) come out identical from zlib at any level; the
  rest were written by other deflate implementations. Reproducing those needs
  a preflate-style diff of the deflate decisions, which is a project of its
  own.
* **Timestamps inside Message-IDs.** 25% of the recent Message-IDs embed the
  send time (mutt, git send-email, Alpine), but the coder already predicts
  most of those digits from the previous ID; the estimated saving is about
  10 KB (0.05%).
* **16-bit probabilities in the arithmetic coder:** −0.02% to −0.05%.
* **More contexts** (word trigram, word + previous byte): still 0.1–0.8%
  worse at the table size of the full corpus, also with the run model;
  order 12 instead of order 8 was worse on Enron.
* **Faster StateMap adaptation** (limit 255): no change. **Final mixer**
  selected by the partial byte, or used without the fixed average: worse.
  **First mixer** selected by how many run predictions agree: no change.

## 1. A stronger model for body text (still the largest lever)

Body text is 90% of the Enron output and 44% of the recent one.

* **Measured:** on the same body stream (5 MB Enron sample), paq8px v217
  produces 17% less output than `emcm` version 1 (226,756 vs 273,494 bytes).
  Putting the emz transform in front of paq8px beats paq8px alone by 2.6%, so
  the structure gains add to a stronger coder.
* **Measured:** on the first 1,500 messages of the recent corpus, paq8px
  codes the body stream 14.5% smaller than `emcm` as first released in
  version 2 (605,619 vs 708,459 bytes; the current coder: 697,915, so the gap
  is now 13%), and the emz transform with paq8px as back end is 7.5% smaller
  than paq8px alone (881,698 vs 952,713 bytes).
* **Measured:** adding a context to `emcm` (order 5, word trigram, or word +
  last byte) *hurt* by 0.5–0.9% at the table-to-data ratio used for the full
  corpus (12 MB body sample, 64 MB table). With a 4× larger table the
  12-context model came out 0.4% *better* than the 9-context one. The model is
  limited by hash-table capacity, not by missing contexts. The 4-way bucket
  search of version 2 recovered only 0.1%, so the next step is a different
  memory layout rather than smarter replacement.

| Step | Expected gain on total | Cost |
|---|---|---|
| Two-level memory: direct tables for orders 1–2, small 7-byte paq8-style slots with count-based replacement for high orders. Then add paq8's word-model contexts (word trigram, word+position, column, case-folded words) and indirect contexts (byte history → next byte) | 4–7% on Enron, 2–3% on recent mail (estimated) | moderate, about 0.8× speed |
| Larger hash table for the body: 2 GB → 4 GB | about 0.2–0.3% (measured: 0.6%, then 0.2% per doubling) | memory |
| A paq8-class model: many more context models, optionally an LSTM input as in cmix/nncp | up to about 15% of the body share (paq8px's measured advantage on body text is 17%) | paq8px ran about 80× slower than `emcm` |

## 2. Body structure that a plain CM does not see

From the per-line cost breakdown of the body coder (12 MB Enron sample):

| Line type | Share of body bits |
|---|---:|
| Embedded reply/forward headers (`From:`, `Sent:`, `To:`, `Subject:`, `cc:` inside the body) | 3.6% |
| Lines dominated by dates (`05/14/2001 04:39 PM`) | 1.6% |
| Lotus Notes addresses (`Name/HOU/ECT@ECT`) | 2.1% |
| Lines quoted with `>` | 3.4% |

* **Reply/forward header blocks** (`-----Original Message-----`,
  `---- Forwarded by X on <date> ----`, Notes `X <addr> on <date>`, and on
  recent mail `On <date>, <name> wrote:`): parse them and code their fields
  against the quoted message's real header (sender name, date in the other
  format, recipients, subject). Most of that 7% of body bits is predictable.
  **Estimated: 2–4% of the Enron total, about 1% of the recent one.**
* **Re-wrapped quotes:** forwarded text is often re-wrapped at a different
  column, which breaks the exact match at every line end. A match model that
  ignores whitespace (follow the match through spaces and newlines, and model
  the line breaks separately) would keep it. **Estimated: 0.5–1%.**
* **Quote prefixes: done** in version 2 as a separate match model rather than
  a side stream (see above).
* **Tried, no gain:** ordering bodies by date or by thread instead of by
  mailbox/folder (0 to 1.1% worse). A word-replacement transform (WRT) was
  0.8% worse before counting its dictionary and 3.4% worse with it; it would
  need word-code-aware contexts in the CM.

## 3. Headers of recent mail

What is left in the recent corpus's header literals (2.0 MB, about 117 bytes
per message; `results/oss_header_literals_by_field.txt`):

* **Message-IDs (17% of header literals).** Many IDs embed a timestamp
  close to the Date header (mutt: `20150316201621.GA30446@host`, the send
  time in UTC; Alpine: `alpine.LFD.2.20.1612061653090…`, when composing
  started). Coding that part relative to Date would remove 10–14 digits per
  such ID. **Estimated: −0.1 to −0.2%.**
* **Autocrypt keys (17%).** OpenPGP public keys are close to random; only
  repeats can be removed, and the match model already finds those. Little to
  gain.
* **Received chains (8%).** Hop *n*'s `from` host is hop *n+1*'s `by` host;
  queue IDs, TLS parameters and software versions repeat per relay. Code the
  chain as a path through a graph of relays seen before. **Estimated: −0.2
  to −0.4%.**
* **X-VM-v5-Data remainder (7%).** The weekday and month names, the month
  number and a UTC sort key are all functions of the Date header, and the
  summary line truncates the sender address to 12 characters. A client-specific
  model would remove most of the remaining 137 KB. **Estimated: −0.5%** on
  this corpus, nothing elsewhere.
* **Signature material (27.5% of the total)** cannot be compressed: RSA
  signatures, encrypted anti-spam reports and random tokens. The only lever
  would be lossy (dropping signatures that no longer verify), which emz does
  not do.

## 4. Cross-field context (Enron headers: about 10% of the output)

The streams are coded independently, so the coder cannot see related fields.
Coding all fields with one CM that accepts *known side information* (bytes
that update the contexts but are not coded) would allow:

* **Display name given the address:** "Mara, Susan </O=ENRON/OU=NA/CN=
  RECIPIENTS/CN=SMARA>" is largely implied by `susan.mara@enron.com`.
  **Estimated: −0.5 to −1% of the total.**
* **Address lists:** code each address as a rank in a per-folder frequency
  list, falling back to a global ID, instead of text. **Estimated: −0.3%.**
* **Body start given the header:** the greeting uses the recipient's first
  name and the signature the sender's. **Estimated: −0.2%.**

## 5. Binary attachments (16% of the recent output)

* Decoded base64 attachments are coded by the same CM. Most are already
  compressed (PNG, PDF, ZIP, gzip) or are signatures (S/MIME, PGP).
  Recompressing deflate streams (precomp/preflate style: inflate, verify the
  exact re-deflate, code the raw data) and JPEG-aware coding (packJPG/Lepton
  style) typically gain 10–20% on such files. **Estimated: −1.5 to −3% of
  the recent total.**

## 6. Small numeric streams (Enron, about 2% of the output)

* **Body length:** done; body text parts now end with a mark in the text
  stream (see above).
* **Date:** a reply is later than the message it quotes; copies in the
  sender's and recipients' mailboxes share dates; the date inside a reply
  block is the parent's date.
* **Message-ID clock N2:** the converter's millisecond clock advanced about
  23 ms per message. The step probably depends on message size (not yet
  checked); if so, the length predicts it.

Together, **estimated −0.5 to −1%.**

## 7. Speed and memory

* emz runs at about 1 MB/s in each direction. The body coder is the
  bottleneck and is single-threaded.
* Cheap speed-ups: profile-guided optimisation (typically 10–20%), software
  prefetch of the next nibble's buckets, and coding the header streams
  concurrently (already done).
* Parallel body coding: split the body stream into 2–4 independent segments
  by mailbox. **Estimated cost 1–2% ratio**, for a near-linear speed-up.

## 8. Outlook files

**Implemented: `.msg`** (see the README section and
`results/msg_samples.txt`). On 25 sample files with attachment bytes zeroed
the archive is 97.1 KB, against 186.2 KB for emz without `.msg` support and
181.1 KB for xz -9e (**measured**). Where the 96.8 KB of coded streams go:

| Part | Coded (bytes) | Share |
|---|---:|---:|
| Body text: plain, HTML and decompressed RTF | 23,874 | 24.7% |
| Small binary values (entry IDs, Exchange metadata, named-property maps) | 20,763 | 21.4% |
| String literals | 15,895 | 16.4% |
| Transport headers (signature material 8,220, other literals 5,723) | 15,546 | 16.1% |
| Compound-file skeleton | 11,123 | 11.5% |
| Property streams | 6,419 | 6.6% |
| Value modes, RTF token choices, zeroed attachments, paths | 3,194 | 3.3% |

Next steps, largest first:

* **RTF against the other bodies** (**estimated**: most of the RTF's share
  of the body stream, about 15 KB of 24 KB here). Outlook's RTF either wraps
  the plain-text body (`\fromtext`) or the HTML body (`\fromhtml1`, the HTML
  kept in `\*\htmltag` groups). Decoding the encapsulation and coding the RTF
  as the operations that turn the other body into it would leave the RTF
  boilerplate, which repeats from message to message.
* **PST and OST stores.** The node database (pages, blocks, B-trees) is a
  container like the compound file: blocks are "permute"-encoded (a fixed
  byte substitution) or "cyclic"-encoded (keyed by block ID), both reversible;
  the property contexts inside hold the same properties as a `.msg`, so the
  value models carry over. OST files of Outlook 2013 and later store blocks
  zlib-compressed in 4 KB pages; Python's zlib does not reproduce
  Outlook's deflate output (tried on a sample OST), so those blocks need
  either a deflate re-encoder that can be told the encoder's choices
  (precomp-style) or verbatim storage. A PST of the Enron sample is in
  pst-extractor's test data.
* **Directory trees** (**measured**: standard red-black insertion in entry
  order reproduces 81 of 119 sibling trees exactly, another 25 up to the
  colours). Coding each entry's parent and then predicting the tree would
  save most of the 2.6 KB of tree links and 0.3 KB of colours.
* **Directory sizes** (2.0 KB) could be coded with the stream name as
  context (fixed-size properties such as GUIDs and times).
* **ZIP inside values**: Exchange stores a theme as a small Office package
  (deflate) in a named property, and attachments are often Office files.
  Re-encoding deflate exactly would make them compressible.
* **The last 0.6% of RTF tokens**: all differ only after the 4 KB dictionary
  wraps, so some detail of node deletion is still unknown.
