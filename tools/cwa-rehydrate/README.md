# cwa-rehydrate

Replaces converted EPUBs in the Calibre-Web-Automated Library with Retail EPUBs from Anna's Archive
(ADR 0016, #755). Standard library only, plus Flask in the Shelfmark container.

## Rules

**Retail EPUB:** an EPUB without what calibre's conversion leaves in its content: `page_styles.css`,
`index_split_*` files or `calibre` classes. Calibre named as producer in the OPF doesn't count on its own,
because editing a file's metadata in calibre adds it too. The ingest gate ranks files the same way.

**Candidates** (`plan`): every Book without a Retail EPUB, in this order: Books with no EPUB, Books whose
EPUB won't open, then Books with a converted EPUB.

**Fetch:** for each candidate, Shelfmark's release search (Direct Download only, English) finds Anna's
Archive releases for the Book's Hardcover ID. The tool downloads up to `--tries` English EPUB releases,
in Shelfmark's order, into a staging folder, through Anna's Archive's fast-download API and the membership key
in Shelfmark's `AA_DONATOR_KEY`. The first Retail EPUB goes into the Ingest Folder. Every other download is deleted, except that a Book with no EPUB or a broken
one takes the first readable English EPUB when no retail one turns up, because any EPUB outranks those. A Book
whose only file is a PDF takes only a Retail EPUB, because its layout may be the content.

**Same Book:** a search by Hardcover ID can return other Books of the series, or one Book an omnibus collects. A
download counts only when one title's main title (before any `:` or `(`) runs through the other's, word for word,
and, when its OPF names authors, one of the Book's surnames appears in them or in its title, spelt alike (a typo
like "Chrichton" passes). Otherwise it's recorded as `wrong book`
with its title and authors, deleted, and the next release tried. Text in `(…)` or `[…]` doesn't count, and
neither does the Book's title followed by a number, `Book`, `Volume` or `Part`: "The Eternal Champion 3: The Dragon
in the Sword" is another Book than "The Eternal Champion".

**Stamp:** before a copy goes into the Ingest Folder, its OPF gets the Book's title, authors and Hardcover ID and
loses every other identifier, so CWA and the gate match it to the Book, not by the copy's own ISBN, Goodreads ID
or title. Its package identifier gets a fresh UUID. Obfuscated fonts (`META-INF/encryption.xml`) are keyed to
an identifier, so they're restored first. A copy whose fonts no identifier unlocks keeps its identifiers, which
may include an ISBN; the gate's verdicts show if one matched elsewhere. A copy that can't be stamped counts as a
failed try. A calibre metadata jacket (a page of the uploader's library metadata, marked `calibre-content`
`jacket`) is removed with its manifest, spine and guide entries. Then:

- A Book with no EPUB gets the copy through CWA's title-and-author automerge, which adds a Format the Book lacks
  without a new record. The gate then trims the Book to its best file, the EPUB, and logs `trimmed`.
- A Book with an EPUB gets a new record, which the gate merges into the Book by Hardcover ID.

- A Book without a Hardcover ID isn't searched; the gate couldn't merge what it found.
- Once a download has reported the fast downloads left, a Book starts only when all its tries fit above
  `--reserve`, so readers' requests still work. The tool also stops when Anna's Archive says no downloads are
  left. Rerun it the next day.
- A failed release search, or a download that finds Anna's Archive unavailable (a server error or a network
  failure), is retried after 30 s, 2 min and 5 min. If it still fails, the run stops (`search failed` or
  `aa unavailable`) without recording the Book, so a rerun tries it again. Any other failed download is recorded
  by its error and the next release is tried.
- It waits while the Ingest Folder holds `--backlog` EPUBs, and can space downloads
  `--pace` seconds apart (none by default: the members' fast-download API needs no pause).
- Each Book's outcome is appended to the attempts file as it finishes, so a rerun resumes after the last one.
  Results: `retail`, `converted ingested`, `no retail`, `no epub` (no English EPUB release),
  `no hardcover id`, `failed` (every download failed or was another Book). `available` counts the releases by
  Format and language.

**Report:** counts before and after, results, and the Formats Anna's Archive had for the Books still without a
Retail EPUB. The CSV lists every Book the after plan still lacks a Retail EPUB for, for the Operator to fetch by
hand, with its result: one of the above, `not tried`, or `retail copy not merged` (the gate didn't merge the
copy into the Book, so check the gate's log for a `flagged` Book).

## Run

From the repo root, with the Host's SSH keys from `.env`. `plan` and `report` run in the
`calibre-web-automated` container, where the library is; `fetch` runs in the `shelfmark` container, where the
membership key, the release search and the Ingest Folder are. Its files live in Shelfmark's `/config/rehydrate`,
which survives a restart.

```sh
set -a; . ./.env; set +a
HOST="ssh -i $HOPS_TRUENAS_SSH_KEY $HOPS_TRUENAS_SSH_USER@$HOPS_TRUENAS_SSH_HOST sudo -n docker exec"
CWA="$HOST -i -u 4008:1002 calibre-web-automated" SM="$HOST -i shelfmark" T=tools/cwa-rehydrate/cwa_rehydrate.py

# 1. Plan (read-only) and copy it to Shelfmark
$CWA "sh -c 'cat > /tmp/cwa_rehydrate.py'" < $T
$CWA python3 /tmp/cwa_rehydrate.py plan > before.jsonl
$SM "sh -c 'mkdir -p /config/rehydrate && cat > /config/rehydrate/cwa_rehydrate.py'" < $T
$SM "sh -c 'cat > /config/rehydrate/plan.jsonl'" < before.jsonl

# 2. Rollback Snapshot of the dataset holding the library, then fetch in the background (resumable)
hops snapshot take --dataset <pool>/media --issue 755
$HOST -d shelfmark "sh -c 'cd /config/rehydrate && python3 cwa_rehydrate.py fetch plan.jsonl \
  --attempts attempts.jsonl --staging /tmp/shelfmark/rehydrate --limit 50 >> fetch.log 2>&1'"
$SM tail -1 /config/rehydrate/fetch.log   # {"status": "done" | "limit" | "quota" | "search failed" | "aa unavailable", ...}

# 3. After each batch: check the gate's verdicts (below)

# 4. Once the gate has merged everything: plan again, then report
$CWA python3 /tmp/cwa_rehydrate.py plan > after.jsonl
$SM cat /config/rehydrate/attempts.jsonl > attempts.jsonl
python3 $T report before.jsonl after.jsonl attempts.jsonl --fallback fallback.csv > report.md
$SM rm -r /config/rehydrate
```

Check the gate's verdicts with `hops logs cwa-ingest-gate`: each copy for a Book with an EPUB should log `merged`
with `step: 2 retail EPUB`, each Book that had no EPUB should log `trimmed` with `kept: EPUB`, and none should be
`flagged`.
