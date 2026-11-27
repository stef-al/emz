# Further gains for archive (solid) compression

This list starts from the measured cost breakdown of emz on the full Enron
corpus (57.4 MB in total) and is ordered by expected gain. Each estimate is
labelled as **measured** (an experiment in this repository) or **estimated**
(reasoning from measurements, not yet tried).

| Where the bits go (full corpus) | Coded size | Share |
|---|---:|---:|
| Body text (after de-duplication) | 50.8 MB | 88.6% |
| Display names (X-From / X-To / X-cc) | 2.14 MB | 3.7% |
| Address lists (From / To / Cc) | 1.70 MB | 3.0% |
| Subject | 1.23 MB | 2.1% |
| Date, body lengths, Message-ID, references, path, other | 1.5 MB | 2.6% |

## 1. A stronger model for body text (largest lever)

Almost 89% of the output is first-occurrence body text, coded by `emcm`.

* **Measured:** on the same body stream (5 MB sample), paq8px v217 produces
  17% less output than `emcm` (226,756 vs 273,494 bytes). Putting the emz
  transform in front of paq8px beats paq8px alone by 2.6%, so the structure
  gains add to a stronger coder.
* **Measured:** adding a context to `emcm` (order 5, word trigram, or word +
  last byte) *hurt* by 0.5–0.9% at the table-to-data ratio used for the full
  corpus (12 MB body sample, 64 MB table). With a 4× larger table the
  12-context model came out 0.4% *better* than the 9-context one. The model is
  limited by hash-table capacity, not by missing contexts.

Recommended steps:

| Step | Expected gain on total | Cost |
|---|---|---|
| Two-level memory: direct tables for orders 1–2, small 7-byte paq8-style slots with count-based replacement for high orders. Then add paq8's word-model contexts (word trigram, word+position, column, case-folded words) and indirect contexts (byte history → next byte) | 4–7% (estimated) | moderate, about 0.8× speed |
| Larger hash table for the body: 2 GB → 4 GB | about 0.2–0.3% (measured: 0.6%, then 0.2% per doubling) | memory |
| Mixer and SSE: weight sets selected by an order-2 hash; an extra APM on order 2 and on match length; final-mixer tuning | 1–2% (estimated) | small |
| A paq8-class model: many more context models, optionally an LSTM input as in cmix/nncp | up to about 15% (paq8px's measured advantage on body text is 17%) | paq8px ran about 80× slower than `emcm` |

## 2. Body structure that a plain CM does not see

From the per-line cost breakdown of the body coder (12 MB sample):

| Line type | Share of body bits |
|---|---:|
| Embedded reply/forward headers (`From:`, `Sent:`, `To:`, `Subject:`, `cc:` inside the body) | 3.6% |
| Lines dominated by dates (`05/14/2001 04:39 PM`) | 1.6% |
| Lotus Notes addresses (`Name/HOU/ECT@ECT`) | 2.1% |
| Lines quoted with `>` | 3.4% |

* **Reply/forward header blocks** (`-----Original Message-----`,
  `---- Forwarded by X on <date> ----`, Notes `X <addr> on <date>`): parse
  them and code their fields against the quoted message's real header (sender
  name, date in the other format, recipients, subject). Most of that 7% of
  body bits is predictable. **Estimated: 2–4% of the total.**
* **Quote prefixes:** remove the `> ` and `> > ` prefixes into a side stream,
  so that the match model sees the original text uninterrupted. **Estimated:
  about 0.5%.**
* **Re-wrapped quotes:** forwarded text is often re-wrapped at a different
  column, which breaks the exact match at every line end. A match model that
  ignores whitespace (follow the match through spaces and newlines, and model
  the line breaks separately) would keep it. **Estimated: 0.5–1%.**
* **Tried, no gain:** ordering bodies by date or by thread instead of by
  mailbox/folder (0 to 1.1% worse). A word-replacement transform (WRT) was
  0.8% worse before counting its dictionary and 3.4% worse with it; it would
  need word-code-aware contexts in the CM.

## 3. Cross-field context (headers: about 9% of the output)

The streams are coded independently, so the coder cannot see related fields.
Coding all fields with one CM that accepts *known side information* (bytes
that update the contexts but are not coded) would allow:

* **Display name given the address:** "Mara, Susan </O=ENRON/OU=NA/CN=
  RECIPIENTS/CN=SMARA>" is largely implied by `susan.mara@enron.com`. New-name
  literals are 1.0 MB. **Estimated: −0.5 to −1% of the total.**
* **Subject:** strip `Re:`/`RE:`/`FW:` prefixes, code the prefix as a symbol
  and the base subject as a reference. In a 100 MB sample, 23% of new subjects
  had a base subject seen earlier but not among the folder's last 8.
  **Estimated: −0.3%.**
* **Address lists:** code each address as a rank in a per-folder frequency
  list, falling back to a global ID, instead of text. **Estimated: −0.3%.**
* **Body start given the header:** the greeting uses the recipient's first
  name and the signature the sender's. **Estimated: −0.2%.**

## 4. Small numeric streams (about 2% of the output)

* **Body length (0.7%):** an end-of-body symbol coded inside the body stream,
  where the model can predict it from the text (signatures, disclaimers),
  instead of a separate length.
* **Date (0.7%):** a reply is later than the message it quotes; copies in the
  sender's and recipients' mailboxes share dates; the date inside a reply block
  is the parent's date.
* **Message-ID clock N2 (0.4%):** the converter's millisecond clock advanced
  about 23 ms per message. The step probably depends on message size (not yet
  checked); if so, the length predicts it.
* **Duplicate references (0.3%):** key cross-mailbox copies by (Date, From)
  instead of an index delta.

Together, **estimated −0.5 to −1%.**

## 5. Speed and memory

* emz runs at about 1.2 MB/s in each direction. The body coder is the
  bottleneck and is single-threaded.
* Cheap speed-ups: profile-guided optimisation (typically 10–20%), software
  prefetch of the next nibble's buckets, and coding the header streams
  concurrently (already done).
* Parallel body coding: split the body stream into 2–4 independent segments
  by mailbox. **Estimated cost 1–2% ratio**, for a near-linear speed-up.

## 6. Beyond Enron

The Enron conversion is unusually regular. Modern archives need a few more
models, each of which follows the same "predict, verify, otherwise escape"
rule:

* A generic RFC 5322 parser: the field-name sequence coded as a template ID
  plus exceptions; `Received:` chains (hosts, IDs, timestamps that predict one
  another); `DKIM-Signature` (random, stored as is); `Message-ID`s with
  per-domain templates.
* MIME: multipart boundaries are often derivable. `text/plain` and `text/html`
  alternatives are near-copies of each other, so predict one from the other.
  This is likely the biggest single win on modern mail.
* Base64 attachments: decode, verify exact re-encoding (line length, padding),
  de-duplicate (single-instance storage across the archive), and recompress
  known formats (JPEG, ZIP/Office, PDF) with format-aware tools.
