#!/bin/bash
# Fetch the oss-security mailing list archive (public-inbox v2 epoch mirrored
# on GitHub, one commit per message) and serialise the full-fidelity
# subscriber copies (2015-2026) into the canonical corpus format.
# The archive also holds ~32k messages reconstructed from openwall.com's HTML
# pages (X-Archive-Source: openwall-scrape, synthetic headers); they are
# excluded with --maildir-only.  The mirror keeps growing, so later fetches
# contain more messages than the 16,967 used in the README.
#
#   scripts/fetch_oss_security.sh [dest-dir]        (default: ./data)
#
# For the README results (snapshot of 2026-11-27):
#   messages=16967 message_bytes=131634517
#   sha256=1b3c992464f4f960ec4526119b826e9ec32b63db040e911ebaa1857f030bd9f8
# Pass REV=53b32cbfafccf948233f43de767a9f5b7b852ead to reproduce that snapshot.
set -euo pipefail
DEST=${1:-data}
mkdir -p "$DEST"
if [ ! -d "$DEST/oss-security-inbox.git" ]; then
  git clone --bare https://github.com/joemalcolm/oss-security-inbox "$DEST/oss-security-inbox.git"
fi
python3 "$(dirname "$0")/build_inbox_corpus.py" "$DEST/oss-security-inbox.git" "$DEST/oss_full.corpus" oss-security \
  --maildir-only --rev "${REV:-HEAD}"
