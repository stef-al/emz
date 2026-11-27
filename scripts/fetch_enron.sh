#!/bin/bash
# Fetch the Enron e-mail corpus (CMU release of 2015-05-07, 517,401 messages)
# from a GitHub mirror and serialise it into the canonical corpus file used by
# all benchmarks.  Needs git and ~2.2 GB of disk.
#
#   scripts/fetch_enron.sh [dest-dir]        (default: ./data)
#
# Expected output of build_corpus.py:
#   messages=517401 message_bytes=1421183736
#   sha256=5e5765e00af914a0b828b57ea97f26e6800c44b907b990dd535d9d214a1e2fcc
# (The mirror's 517,401 files are hash-identical to a second independent mirror,
#  agocs/enronDataScience, whose files live under maildir/.)
set -euo pipefail
DEST=${1:-data}
mkdir -p "$DEST"
if [ ! -d "$DEST/enron-mirror/.git" ]; then
  git clone --depth 1 --no-checkout https://github.com/malted/enron "$DEST/enron-mirror"
fi
python3 "$(dirname "$0")/build_corpus.py" "$DEST/enron-mirror" "$DEST/enron.corpus"
