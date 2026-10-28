# cwa-hardcover

Matches every book in the Calibre-Web-Automated library to [Hardcover](https://hardcover.app), the
metadata authority (ADR 0017), and writes the match's metadata. CWA's own fetch on ingest is off: it
applies a provider's first search result unchecked. This tool checks every match before it writes
anything, and flags what it cannot settle for the Operator.

The tool is sent from a contributor machine and runs inside the `calibre-web-automated` container,
where the library, `app.db`, `calibredb` and `HARDCOVER_TOKEN` are. Standard library only (plus Pillow, which the
CWA image has, to measure covers).

## Matching

Strongest first. Every match must share an author with the book (surname plus first name or initial).

1. **ISBN:** the book's ISBN names a Hardcover edition.
2. **Goodreads ID:** Hardcover maps the book's `goodreads` identifier to a Hardcover book.
3. **Hardcover ID:** the book's own `hardcover-id` identifier.
4. **Title search**, as CWA's book editor does it: the title, then its main title (before a colon or
   bracket), then main title plus first author. Each result scores 0.6 × title similarity + 0.4 for a
   shared author; a shared ISBN scores 1, and summaries, study guides and compilations lose points.
   The best result must reach `--threshold` (default 0.85); ties go to the book more Hardcover
   users have. If nothing passes, the search is scored again with title and author swapped; a match
   that way writes Hardcover's title and author and is flagged.

A match found by ID (1–3) needs a title as close as the search threshold. Statuses:

- `match`: apply writes every field below.
- `review`: an ID names a book with no shared author or a different title, ISBNs name different
  books, or two books match one Hardcover book (both go to review). Only tags are written.
- `none`: nothing matched. Only tags are written.

## Flags and the Review shelf

A book is flagged when it is `review` or `none`, its title and author were swapped, its series is
none of Hardcover's (`series: ours …, Hardcover …`; the library's stands), or it is an omnibus whose
books are not all held. A flagged book gets the `review` tag, which only the Operator removes. The
plan gives each admin without one a private magic shelf, "Review", of books tagged `review`.

An **omnibus** (a Hardcover compilation) whose series details ("1-3", "1-3, 5") name only positions
other matched books hold is removed with `calibredb remove --permanent`, its reading history with
it. Otherwise it is flagged with the positions not held.

## Fields

Any field Hardcover has a value for is overwritten; the tool never clears a field Hardcover lacks.
After writing, `calibredb embed_metadata` writes the fields into the book's file, so a Kindle or a
download carries them.

| Field | Value |
|---|---|
| title | Hardcover's title, plus `: subtitle` when it has one; a sentence-case title is title-cased. Left alone when it differs only in case or punctuation |
| authors | Hardcover's authors (not narrators or editors), each through `author-overrides.json` (`{"Hardcover name": "our name"}`). Left alone when they differ only in case or punctuation |
| identifiers | Adds `hardcover-id`, `hardcover-slug`, `hardcover-edition` (CWA's own types); adds `isbn` only when the book has none; keeps every other identifier |
| series | Hardcover's featured series and position, only when the book has no series; an existing series is never renamed or renumbered (Hardcover's often disagree: omnibuses, novellas, reading order) |
| description | Hardcover's, as HTML paragraphs |
| publisher | The ISBN's edition, else the default e-book, else the default physical edition |
| pubdate | Hardcover's original release date, unless the book already has a date in that year (Hardcover often stores a year as January 1) or the year is before 1000 (a Hardcover typo) |
| rating | Hardcover's average, to the nearest half star, when at least 5 readers rated it |
| tags | Hardcover's genre tags with 2 or more votes (most carry one reader's vote), mapped to the controlled list; if none map, the book's own tags mapped instead; up to 10. `review` stays. Every book's tags, matched or not, end up on the list |
| cover | The largest Hardcover image, when it has more pixels than the current cover once calibre shrinks it to fit 1650×2200 |

## The controlled tag list

`tags.json` lists every tag a book may carry, and `synonyms` maps another name to one or more listed
tags (case-insensitive). A name neither listed nor a synonym is dropped. A magic shelf rule naming an
unlisted tag is rewritten to its single synonym; the plan shows a rule with no single synonym, and
apply leaves it. `tags < plan.jsonl` counts the plan's Hardcover genres and what each maps to, to
tune the list.

## Run

From the repo root, with the Host's SSH keys from `.env`:

```sh
set -a; . ./.env; set +a
CWA="ssh -i $HOPS_TRUENAS_SSH_KEY $HOPS_TRUENAS_SSH_USER@$HOPS_TRUENAS_SSH_HOST sudo -n docker exec -i -u 4008:1002 calibre-web-automated"
$CWA mkdir -p /tmp/hc
for f in cwa_hardcover.py tags.json author-overrides.json; do
  $CWA "sh -c 'cat > /tmp/hc/$f'" < tools/cwa-hardcover/$f
done

# 1. Plan (read-only): a sample, chosen ids, or the whole library (about 15 minutes)
$CWA python3 /tmp/hc/cwa_hardcover.py plan --sample 100 > plan.jsonl
python3 tools/cwa-hardcover/cwa_hardcover.py report < plan.jsonl > plan.csv   # review this
python3 tools/cwa-hardcover/cwa_hardcover.py tags < plan.jsonl                # genres and their tags

# 2. Rollback Snapshots of the library's dataset and of app.db's (the default), then apply
hops snapshot take --dataset <pool>/media --issue <PR or issue>
hops snapshot take --issue <PR or issue>
$CWA python3 /tmp/hc/cwa_hardcover.py apply < plan.jsonl > applied.jsonl
```

`hops exec` passes no stdin, hence `ssh … docker exec -i`. `-u 4008:1002` is the library's owner
(`CWA_UID`, `MEDIA_SHARED_GID`), so new files keep it. `apply` skips a book whose planned fields
changed since the plan, and `--only 1,2,3` limits it to those books and leaves magic shelves alone. Covers download at apply time;
a failed download writes the other fields.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/cwa-hardcover
```

The tests stub Hardcover; they need no network, library or `calibredb`.
