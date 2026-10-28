# cwa-consolidate

Reduces the Calibre-Web-Automated Library to one Book per work and one Format per Book (ADR 0016, #752).
It merges duplicate Books, deletes Books the Library doesn't keep, converts every other Format to EPUB,
and tags anything a person must decide with `review`.

The tool is sent from a contributor machine and runs inside the `calibre-web-automated` container, where
the library, `app.db`, `cwa.db`, `calibredb` and `ebook-convert` are. Standard library only.

## Rules

**Deleted:** Books with no files, comics (a CBR, CBZ, CB7 or CBT file), and Books that aren't in English.
The language comes from the Book's metadata. When the metadata has none, it comes from the EPUB's
`dc:language`; a Book with neither is flagged `language unknown`.

**Flagged, not deleted:** a comic genre tag with no comic file (`comic? tagged …`), because Hardcover
genres put prose on comic shelves. Flags add the `review` tag.

**Groups:** Books share a group when their title (without bracketed notes or a leading article) and
first author (surname and first initial) match, when they share a Hardcover ID, or when CWA's duplicate
cache groups them. Deleted Books never join a group. A group goes to `review` instead of merging when
its Hardcover IDs differ or its titles differ (a wrong Hardcover ID or the cache can join two works; a
title and its part before or after a colon agree). Each member of a review group is flagged
`possible duplicate`.

**Survivor:** the member holding the best file, by ADR 0016's ranking (first difference wins):

1. **Format:** EPUB, AZW3, MOBI, AZW, FB2, LIT, TXT, RTF, DOCX, then PDF. Anything else (KFX and its
   MD containers, ZIP, ORIGINAL_*) is unusable.
2. **Retail EPUB:** no marks of a calibre conversion.
3. **epubcheck:** fewer fatal errors, then fewer errors.
4. **Table of contents:** more than one entry.
5. **Larger file.**
6. **Existing:** the older Book, then the lower id.

The survivor takes the losers' identifiers it lacks and their `app.db` history (downloads, archive,
bookmarks, read status, shelves, Kobo sync); the losers are removed.

**Formats:** a Book keeps an EPUB as is. Without one, its best usable Format converts to EPUB; a PDF stays
only when it is the Book's sole usable Format, and is flagged `PDF only: confirm layout`. A Book with no
usable Format goes to review. Every other Format is removed.

## Run

From the repo root, with the Host's SSH keys from `.env`:

```sh
set -a; . ./.env; set +a
CWA="ssh -i $HOPS_TRUENAS_SSH_KEY $HOPS_TRUENAS_SSH_USER@$HOPS_TRUENAS_SSH_HOST sudo -n docker exec -i -u 4008:1002 calibre-web-automated"
$CWA "sh -c 'cat > /tmp/cwa_consolidate.py'" < tools/cwa-consolidate/cwa_consolidate.py

# 1. Counts to compare later, and the plan (read-only, about 2 minutes)
$CWA python3 /tmp/cwa_consolidate.py stats > stats-before.json
$CWA python3 /tmp/cwa_consolidate.py plan > plan.jsonl

# 2. epubcheck the EPUBs that decide a group, then plan again with the results
python3 tools/cwa-consolidate/cwa_consolidate.py epubcheck-list < plan.jsonl > epubs.txt
#    Unzip epubcheck's release (https://github.com/w3c/epubcheck/releases) next to epubs.txt, then:
COPYFILE_DISABLE=1 tar -czf bundle.tgz epubcheck-* epubs.txt
ssh -i $HOPS_TRUENAS_SSH_KEY $HOPS_TRUENAS_SSH_USER@$HOPS_TRUENAS_SSH_HOST sudo -n docker run -i --rm \
  --label startup-order=skip -u 4008:1002 --volumes-from calibre-web-automated:ro eclipse-temurin:21-jre \
  sh -c "$(printf '%q' "$(cat tools/cwa-consolidate/epubcheck.sh)")" < bundle.tgz > epubcheck.jsonl
$CWA "sh -c 'cat > /tmp/epubcheck.jsonl'" < epubcheck.jsonl
$CWA python3 /tmp/cwa_consolidate.py plan --epubcheck /tmp/epubcheck.jsonl > plan.jsonl

# 3. Review
python3 tools/cwa-consolidate/cwa_consolidate.py report < plan.jsonl > plan.csv

# 4. Rollback Snapshot of the dataset holding the library, then apply, then compare
hops snapshot take --dataset <pool>/media --issue 752
$CWA python3 /tmp/cwa_consolidate.py apply --skip 12,34 < plan.jsonl > applied.jsonl
$CWA python3 /tmp/cwa_consolidate.py stats > stats-after.json
```

epubcheck needs Java, which the CWA image lacks, so it runs in a throwaway JRE container that mounts the
library read-only. `hops exec` passes no stdin, hence `ssh … docker exec -i`. `-u 4008:1002` is the
library's owner (`CWA_UID`, `MEDIA_SHARED_GID`), so new files keep it.

`apply` merges groups first, then deletes, converts and flags Books. It skips a group or Book whose
Formats changed since the plan. `--skip` keeps out the Books the Operator chose to keep (any member
skips its whole group); `--only` limits a run to some Books, for batches. Conversions take most of
the time. `apply` clears CWA's magic shelf cache; `stats` counts each user's magic shelves.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/cwa-consolidate
```

The tests build a stub library (`metadata.db`, `app.db`, `cwa.db` and real EPUB files) and record
`calibredb` calls; they need no Host, `calibredb` or Java.
