# cwa-hardcover

Matches every book in the Calibre-Web-Automated library to [Hardcover](https://hardcover.app) and
writes the match's metadata. CWA's own fetch on ingest is off: it applies a provider's first search
result unchecked. This tool checks every match before it writes anything.

The tool is sent from a contributor machine and runs inside the `calibre-web-automated` container,
where the library, `calibredb` and `HARDCOVER_TOKEN` are. Standard library only (plus Pillow, which the
CWA image has, to measure covers).

## Matching

Strongest first. Every match must share an author with the book (surname plus first name or initial).

1. **ISBN:** the book's ISBN names a Hardcover edition.
2. **Goodreads ID:** Hardcover maps the book's `goodreads` identifier to a Hardcover book.
3. **Title search**, as CWA's book editor does it: the title, then its main title (before a colon or
   bracket), then main title plus first author. Each result scores 0.6 × title similarity + 0.4 for a
   shared author; a shared ISBN scores 1, and summaries, study guides and compilations lose points.
   The best result must reach `--threshold` (default 0.85); ties go to the book more Hardcover
   users have.

Statuses: `match` (apply writes it), `review` (an ID names a book with no shared author, ISBNs name
different books, or the titles differ widely) and `none`. Only `match` rows are written.

## Fields

Title and authors are never written. Any other field Hardcover has a value for is overwritten; the
tool never clears a field Hardcover lacks.

| Field | Value |
|---|---|
| identifiers | Adds `hardcover-id`, `hardcover-slug`, `hardcover-edition` (CWA's own types); adds `isbn` only when the book has none; keeps every other identifier |
| series | Hardcover's featured series and position, only when the book has no series; an existing series is never renamed or renumbered (Hardcover's often disagree: omnibuses, novellas, reading order) |
| description | Hardcover's, as HTML paragraphs |
| publisher | The ISBN's edition, else the default e-book, else the default physical edition |
| pubdate | Hardcover's original release date, unless the book already has a date in that year (Hardcover often stores a year as January 1) |
| rating | Hardcover's average, to the nearest half star, when at least 5 readers rated it |
| tags | Replaced by Hardcover's genre tags with 2 or more votes (most carry one reader's vote), up to 10; left alone when none qualify |
| cover | The largest Hardcover image, when it has more pixels than the current cover once calibre shrinks it to fit 1650×2200 |

## Run

From the repo root, with the Host's SSH keys from `.env`:

```sh
set -a; . ./.env; set +a
CWA="ssh -i $HOPS_TRUENAS_SSH_KEY $HOPS_TRUENAS_SSH_USER@$HOPS_TRUENAS_SSH_HOST sudo -n docker exec -i -u 4008:1002 calibre-web-automated"
$CWA "sh -c 'cat > /tmp/cwa_hardcover.py'" < tools/cwa-hardcover/cwa_hardcover.py

# 1. Plan (read-only): a sample, chosen ids, or the whole library (about 15 minutes)
$CWA python3 /tmp/cwa_hardcover.py plan --sample 100 > plan.jsonl
python3 tools/cwa-hardcover/cwa_hardcover.py report < plan.jsonl > plan.csv   # review this

# 2. Rollback Snapshot of the dataset holding the library, then apply
hops snapshot take --dataset <pool>/media --issue <PR or issue>
$CWA python3 /tmp/cwa_hardcover.py apply < plan.jsonl > applied.jsonl
```

`hops exec` passes no stdin, hence `ssh … docker exec -i`. `-u 4008:1002` is the library's owner
(`CWA_UID`, `MEDIA_SHARED_GID`), so new files keep it. `apply` skips a book whose planned fields
changed since the plan, and `--only 1,2,3` limits it to those books. Covers download at apply time;
a failed download writes the other fields. The book files keep their embedded metadata; CWA, OPDS
and Kobo sync read `metadata.db`.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/cwa-hardcover
```

The tests stub Hardcover; they need no network, library or `calibredb`.
