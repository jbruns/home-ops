# Hardcover is the only automated metadata authority for Books

The Books Library's metadata was damaged: some Books had title and author swapped, some author fields held a title or a filename, series names were malformed or near-duplicates, and tags were noisy (an "Audiobook" tag on 399 ebooks). The obvious fix is to take each field from the source that is best at it. Goodreads curates series best, a private tracker's uploads carry series and tags, and ISFDB holds the most reliable original publication dates for genre fiction. Of these, only Hardcover offers a documented API that may be used in bulk. Goodreads has issued no API keys since 2020 and is reachable only by scraping, which its terms forbid. The tracker's rules restrict automation. ISFDB is published as a database dump. A spot check of existing Hardcover matches found title and author right 39 times in 40, and series right 28 times in 34. We decided that **Hardcover is the only source any automation writes metadata from, for every field, title and authors included**. Goodreads, ISFDB and the tracker are consulted by the Operator by hand.

- **Confident matches only.** A Hardcover match is written only when it is confident: the scoring in `tools/cwa-hardcover`, where a match must share an author with the Book. A new Book that does not match confidently enters the Library flagged for review. CWA's own fetch on ingest stays off, and CWA's scheduled Hardcover ID task is turned off, so that one writer owns the metadata.
- **Series.** When Hardcover's series or position disagrees with an existing one, the change is flagged for review, not applied. The Operator settles it, checking Goodreads or ISFDB by hand.
- **Author names** take Hardcover's form. A short override list covers Hardcover's own mistakes.
- **Tags** are mapped from Hardcover's genres onto a controlled list of about 30 to 50 tags kept in this repo. Anything outside the list is dropped, and the list changes only by PR.
- **The review flag** is a reserved tag outside that list, shown on an admin-only magic shelf. It lives on the Book and clears when the Book is fixed.
- **Embedded metadata.** Metadata is written into each EPUB after every change, because Send to Kindle reads the file, not the Library.
- **Who can edit.** Only admins can edit, upload or delete. Readers download, send to Kindle, keep shelves and request through Shelfmark.

## Considered Options

- **Per-field sources, with Goodreads scraped for series**: rejected. It breaks Goodreads' terms and stops working whenever Goodreads blocks the scraper.
- **The private tracker's metadata**: rejected. Uploaders type it and it is unstructured, and the tracker restricts automated access.
- **ISFDB's dump for original publication dates**: deferred. It can be added if Hardcover's dates prove weak.
- **Leave title and authors alone, as `tools/cwa-hardcover` did**: rejected. Those were the most damaged fields.
