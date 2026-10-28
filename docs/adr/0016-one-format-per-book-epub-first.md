# The Books Library holds one Format per Book, EPUB first, and no Kindle Format

An inventory of the Books Library found 2,533 Books holding 13 Formats. 1,818 Books held more than one Format, most often EPUB with a MOBI beside it. 688 had no EPUB at all. Nearly every EPUB and MOBI sampled was calibre's own conversion output, not the file a publisher released. The extra Formats existed for Kindles, but every Kindle reader receives Books through Send to Kindle, which accepts EPUB, and nobody sideloads by USB. We decided that **each Book holds exactly one Format: EPUB for reflowable text, or PDF only where no EPUB exists and the layout is the content (textbooks, manuals)**. No Kindle Format is stored, and the Library is English only. Comics and magazines are not Books and leave the Books Library.

- **Kindle readers** use Send to Kindle, with CWA's Kindle EPUB fixer on. Amazon converts the EPUB on its side.
- **New Books** come only as EPUB or PDF, with EPUB first. Shelfmark offers no other Format. CWA still converts anything an admin uploads to EPUB, and that converted EPUB is a candidate for a Retail EPUB replacement. This list stays restrictive until rehydration shows how often an English Book exists only in another Format.
- **Choosing between copies** of a Book follows one deterministic ranking. The first difference decides, and every merge logs the step that decided it:
  1. the allowed Format for the content;
  2. a Retail EPUB over a converted one. A converted EPUB shows what conversion leaves in its content: calibre's `page_styles.css`, `index_split_*` files or `calibre` classes. Calibre named as producer in the OPF doesn't count on its own, because editing a file's metadata in calibre adds it too (#755);
  3. fewer `epubcheck` fatal errors and errors;
  4. a table of contents with more than one entry;
  5. the larger file;
  6. otherwise, keep the existing file.
- **Dropped Formats, duplicate Books and non-English Books are deleted**, with a Rollback Snapshot as the only safety net (ADR 0011). Each can be obtained again. An omnibus is dropped when every Book it collects is held.

## Considered Options

- **Keep AZW3 or MOBI beside EPUB for Kindles**: rejected. Send to Kindle takes EPUB, and a second Format is a second file to keep in step.
- **Keep PDF beside EPUB for the same Book**: rejected. EPUB is preferred wherever both exist.
- **Allow MOBI, AZW3 and FB2 at request time and convert them**: rejected for now. A converted EPUB is the weakest copy of a Book. Without these Formats a request fails visibly, and the Operator can still fetch the Book by hand.
- **Keep non-English Editions as deliberate second Books**: rejected. Readers request another language explicitly.
