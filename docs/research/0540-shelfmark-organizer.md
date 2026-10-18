# Can Shelfmark be the Organizer for Books and Audiobooks?

Spike for [#540](https://github.com/jbruns/home-ops/issues/540), feeding [#541](https://github.com/jbruns/home-ops/issues/541) (part of [#531](https://github.com/jbruns/home-ops/issues/531)). Vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md). Storage rules: [ADR 0009](../adr/0009-library-isolation-by-role.md).

Researched 2026-10-18 against Shelfmark [`v1.4.0`](https://github.com/calibrain/shelfmark/releases/tag/v1.4.0) (the pinned image in [`tn/shelfmark/compose.yaml`](../../tn/shelfmark/compose.yaml)) and AudioBookRequest [`1.10.7`](https://github.com/markbeep/AudioBookRequest/releases/tag/v1.10.7). Source links are pinned to those tags. Live facts come from read-only inspection of the Host; secrets and personal data are left out.

## Answer

**Yes, for the downloads Shelfmark starts itself; no, for anything else.** Shelfmark is a Requester that also takes over its own finished downloads from qBittorrent and SABnzbd and places them in a Library, the way Sonarr does for TV. It cannot adopt a download another Requester (AudioBookRequest) handed to a Downloader: it has no folder watcher and no "import from the client" scan. So Shelfmark can be the one Organizer for Audiobooks only if it is also the only Requester for Audiobooks, which means retiring AudioBookRequest.

For Books, Shelfmark already takes over the Downloader's file but hands it to Calibre-Web-Automated's ingest folder; CWA moves it into the Books Library. CWA is the Books Organizer; Shelfmark is the Requester (and, for direct downloads, the Downloader) feeding it.

**Recommendation for #541:** make Shelfmark the Requester and Organizer for Audiobooks, retire AudioBookRequest, `copy_complete.py` and `abr_mover.py`, and give Shelfmark one bind mount of the Audiobooks tree. No Custom Component. The trade-off is losing ABR's Audible search, automatic release selection and Audiobookshelf duplicate check (Q2); if those are must-haves, the fallback is in Q5.

## Today

```text
Requesters                Downloaders (category → folder)                   Writes into /media/audiobooks
AudioBookRequest ─Prowlarr─▶ qBittorrent  abr-prowlarr → tordl/abr-prowlarr ─▶ copy_complete.py (AutoRun, copy)
                          ▶ SABnzbd      abr-prowlarr → completed/ABR      ─▶ abr_mover.py (broken, never ran)
Shelfmark ──client API──▶ qBittorrent  cwd-audiobooks → tordl/cwd-audiobooks ─▶ Shelfmark (copy; see Q1)
                          ▶ SABnzbd      audiobooks → completed/Audiobooks  ─▶ Shelfmark (move)
                          ▶ qBittorrent  cwd-books / SABnzbd books           ─▶ Shelfmark → cwa-book-ingest → CWA → /media/books
```

- Shelfmark's live settings already route audiobooks: `DESTINATION_AUDIOBOOK=/audiobooks`, `FILE_ORGANIZATION_AUDIOBOOK=organize`, `TEMPLATE_AUDIOBOOK_ORGANIZE={Author}/{Title}`, `HARDLINK_TORRENTS_AUDIOBOOK=true`, qBittorrent categories `cwd-books`/`cwd-audiobooks`, SABnzbd categories `books`/`audiobooks`, `PROWLARR_USENET_ACTION=move`, `PROWLARR_TORRENT_ACTION` unset (default `keep`). Books go to `/cwa-book-ingest` with `FILE_ORGANIZATION=rename`.
- Requests are off (`REQUESTS_ENABLED=false`) and `AUTH_METHOD=none`: everyone who passes the IdP is an admin.
- AudioBookRequest grabs through Prowlarr's `POST /api/v1/search` ([source](https://github.com/markbeep/AudioBookRequest/blob/v1.10.7/app/internal/prowlarr/prowlarr.py#L61-L90)); Prowlarr's download client decides the `abr-prowlarr` category. ABR "does **not** handle moving, renaming, nor editing metadata after downloads" ([README](https://github.com/markbeep/AudioBookRequest/blob/v1.10.7/README.md#out-of-scope-features)).
- The Audiobooks Library is 13 flat `.m4b` files, last added June 2025. `tordl/abr-prowlarr` and `tordl/cwd-audiobooks` are empty and SABnzbd's `ABR` folder was never created, consistent with `abr_mover.py` never having run.
- qBittorrent and SABnzbd mount `/media/audiobooks` read-write only for those two scripts; Audiobookshelf mounts it read-write.

## Q1: Can Shelfmark take over a finished download and place it in its Library?

**How.** Shelfmark adds the release to the Downloader through its API with a per-content-type category ([`_get_category_for_task`](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/clients/base_handler.py#L234)), polls it until complete, then reads the path the client reports ([`_handle_completed_file`](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/clients/base_handler.py#L1070)). That path must exist at the same container path in Shelfmark, or be translated by a Remote Path Mapping ([docs](https://github.com/calibrain/shelfmark/blob/v1.4.0/docs/configuration.md#torrent--usenet-setup)). It then runs its post-processing pipeline into the destination and an optional custom script ([docs](https://github.com/calibrain/shelfmark/blob/v1.4.0/docs/custom-scripts.md)).

**Only its own tasks.** The queue is per task Shelfmark created; nothing scans a category or folder for downloads it did not start. The "Manual Import / Local Folder Processing" request ([shelfmark#562](https://github.com/calibrain/shelfmark/issues/562)) was closed by a retry button for Shelfmark's own failed post-processing ([shelfmark#679](https://github.com/calibrain/shelfmark/pull/679)), not by a scan. A "No-op Output Mode" for people who move files themselves is open ([shelfmark#800](https://github.com/calibrain/shelfmark/issues/800)).

**Move or copy** ([`_transfer_single_file`](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/postprocess/transfer.py#L146), [`is_torrent_source`](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/postprocess/transfer.py#L128)):

| Source | Hardlink setting | Operation | After import |
|---|---|---|---|
| Torrent | on (audiobook default) | `os.link`; falls back to copy on `EXDEV` and similar ([`_hardlink_not_supported`](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/fs.py#L384)) | `PROWLARR_TORRENT_ACTION`: keep seeding (default), remove, remove and delete, or change category ([setting](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/clients/settings.py#L896)) |
| Torrent | off (book default) | copy, so seeding continues | as above |
| Usenet | n/a | `os.rename`; copy-then-delete across file systems ([`atomic_move`](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/fs.py#L507)) | with `PROWLARR_USENET_ACTION=move`, deletes the job and its files in SABnzbd ([`post_process_cleanup`](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/download/clients/base_handler.py#L259)) |

That is the Organizer Role: move, or copy (hardlink) while seeding must continue. Hardlinks and atomic renames only work if the download folder and the Library sit inside **one** bind mount in Shelfmark. Today they are separate mounts (`/data/tordl/cwd-audiobooks`, `/audiobooks`), so every "hardlink" silently becomes a full copy, the same symptom as [shelfmark#1452](https://github.com/calibrain/shelfmark/issues/1452). ADR 0009's single-tree mount fixes this.

**Layout.** "Rename and Organize" writes a path template, "Recommended for Audiobookshelf" ([setting](https://github.com/calibrain/shelfmark/blob/v1.4.0/shelfmark/config/settings.py#L1445)); variables include `{Author}`, `{Series}`, `{SeriesPosition}`, `{Title}`, `{Year}`, `{Narrator}`, `{PartNumber}`. The last segment is the file name, and multi-file downloads get part numbers. Audiobookshelf allows single-file books only at the Library root; elsewhere "every book must be in its own folder" ([ABS docs](https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/)). The live template `{Author}/{Title}` puts files straight into the author folder, so two books by one author would be grouped as one. Use the upstream default `{Author}/{Title}/{Title}`, or `{Author}/{Series/}{Title}/{Title}`.

**Books.** Shelfmark copies or moves into `cwa-book-ingest` with rename only; upstream warns against organizing or hardlinking into an ingest folder, because the ingester consumes it ([docs](https://github.com/calibrain/shelfmark/blob/v1.4.0/docs/configuration.md#file-processing-options)). CWA then imports into `/media/books`. Shelfmark never touches the Books Library.

**Gaps.** In v1.4.0 the queue lives in memory: a restart while a download is in flight leaves it stranded. A fix that reopens recent interrupted tasks is merged but not released ([shelfmark#1409](https://github.com/calibrain/shelfmark/pull/1409)). Open: [#1463](https://github.com/calibrain/shelfmark/issues/1463) (Completed Path Wait for synced multi-file downloads; not our case, the Downloaders are on the Host).

## Q2: Does Shelfmark make AudioBookRequest redundant?

| Need | AudioBookRequest 1.10.7 | Shelfmark 1.4.0 |
|---|---|---|
| Users search | Audible catalogue, ASIN, Audible recommendations | Hardcover / Open Library / Google Books metadata, then releases across sources |
| Indexer search | Prowlarr, MAM | Prowlarr (incl. freeleech flags), AudiobookBay, Newznab, IRC, direct; MAM narrator/series enrichment ([docs](https://github.com/calibrain/shelfmark/blob/v1.4.0/docs/myanonamouse-enrichment.md)) |
| Hand to Downloader | Prowlarr grabs, using Prowlarr's client and category | Shelfmark calls qBittorrent/SABnzbd directly with its own book and audiobook categories |
| Take over finished file | No (out of scope) | Yes (Q1) |
| Multi-user and approval | Three groups; Trusted/Admin requests auto-download | Per-type and per-source policy: Download, Request Release, Request Book, Blocked; per-user overrides; admin fulfils ([docs](https://github.com/calibrain/shelfmark/blob/v1.4.0/docs/users-and-requests.md#requests)) |
| Sign-in | Own login, OIDC | Local, OIDC, proxy auth, Calibre-Web DB |
| Notifications | Apprise, Gotify, Discord, ntfy | Apprise URLs, global and per user |
| Already-owned check | Audiobookshelf library lookup | Calibre `metadata.db`, ebooks only ([docs](https://github.com/calibrain/shelfmark/blob/v1.4.0/docs/library-check.md)) |
| Tell Audiobookshelf | Triggers a library scan | None built in; a custom script could call the ABS scan API |

**Redundant for the Roles that matter here**: Shelfmark covers request, search, hand-off with categories, approval, multi-user and notifications, and adds the Organizer step ABR lacks.

**Lost if ABR goes:**

- Audible-based search, ASIN matching and Audible category recommendations.
- Automatic release selection: ABR auto-downloads the best-ranked release for Trusted users ([README](https://github.com/markbeep/AudioBookRequest/blob/v1.10.7/README.md#auto-download)); in Shelfmark a person always picks the release (the user, or the admin in "Request Book" mode).
- The Audiobookshelf "already have it" badge and the post-download ABS scan (Audiobookshelf's watcher normally picks new files up anyway).
- Requests made through ABR's UI (none are pending on the Host's evidence, but this was not inspected in ABR's database).

To replace ABR for other people, Shelfmark needs per-user identity: today `AUTH_METHOD=none`, so turning on requests means proxy auth from the IdP or OIDC.

## Q3: Storage access under ADR 0009

The Audiobooks tree becomes `/media/audiobooks/{library,usenet,torrents}`, and its one Organizer mounts the whole tree once:

```diff
 shelfmark:
-  - /media/audiobooks:/audiobooks
-  - /media/tordl/cwd-audiobooks:/data/tordl/cwd-audiobooks
-  - /media/nzbdl/completed/Audiobooks:/data/nzbdl/completed/Audiobooks
+  - /media/audiobooks:/data/audiobooks              # one mount: rename + hardlink work
   - /media/cwa-book-ingest:/cwa-book-ingest         # Books hand-off (see below)
   - /media/tordl/cwd-books:/data/tordl/cwd-books
   - /media/nzbdl/completed/Books:/data/nzbdl/completed/Books
 qbittorrent:
-  - /media/audiobooks:/data/audiobooks
+  - /media/audiobooks/torrents:/data/audiobooks/torrents   # category cwd-audiobooks
 sabnzbd:
-  - /media/audiobooks:/data/audiobooks
+  - /media/audiobooks/usenet:/data/audiobooks/usenet       # category audiobooks
 audiobookshelf:
-  - /media/audiobooks:/audiobooks
+  - /media/audiobooks/library:/audiobooks:ro
```

- Keep container paths identical in Shelfmark and each Downloader (`/data/audiobooks/torrents`, `/data/audiobooks/usenet`) so no Remote Path Mapping is needed; set `DESTINATION_AUDIOBOOK=/data/audiobooks/library`.
- SABnzbd's incomplete folder (`/data/nzbdl/tmp`) stays on another mount, so SABnzbd's own final move into `usenet/` becomes a copy. That is a Downloader-internal cost ADR 0009 already accepts.
- Moving the 13 existing files into `library/` is a rename within one dataset, so their inodes are kept; Audiobookshelf's library folder path must be updated in the same change.
- **Books is a two-Service chain**, which ADR 0009's "one Organizer mounts the whole tree" does not describe: Shelfmark takes over the Downloader's file and hands it to an ingest folder; CWA owns the Books Library. A layout that keeps the ADR's intent is `/media/books/{library,ingest,usenet,torrents}`: CWA mounts `library` and `ingest`, Shelfmark mounts `ingest`, `usenet` and `torrents` (plus its direct downloads). Separate mounts cost a copy per ebook, which upstream already requires (no hardlinks into ingest). This hand-off folder is a glossary gap worth settling with `/domain-modeling` in #534 or #541; it is not decided here.

## Q4: Maturity and risk

| | Shelfmark | AudioBookRequest |
|---|---|---|
| License | MIT ([LICENSE](https://github.com/calibrain/shelfmark/blob/v1.4.0/LICENSE)) | GPL-3.0 |
| Releases | 25 from v1.0.4 (2026-02-06) to v1.4.0 (2026-09-25); 18 commits on `main` since | 1.10.0 (2026-02-24) to 1.10.7 (2026-08-21) |
| Activity (6 months) | ~200 non-bot commits, three main contributors; last push 2026-10-16 | ~20 commits, one maintainer |
| Open issues | 44; relevant: [#1452](https://github.com/calibrain/shelfmark/issues/1452), [#927](https://github.com/calibrain/shelfmark/issues/927) (hardlinks across mounts), [#800](https://github.com/calibrain/shelfmark/issues/800), [#1463](https://github.com/calibrain/shelfmark/issues/1463) | not reviewed |

Risk: Shelfmark declares itself "feature stable and maintained on a best-effort basis … no roadmap for new features" ([README](https://github.com/calibrain/shelfmark/blob/v1.4.0/readme.md)). Bug and security fixes still ship weekly; the features we need already exist, so the risk is a slowdown, not a missing capability. Shelfmark also holds several Roles (Requester, Downloader for direct sources, Organizer for Audiobooks), so a compromise reaches the Audiobooks tree and the ebook ingest folder, but not the Books Library or any other Library.

## Q5: Recommendation for #541

**Shelfmark is the Audiobooks Organizer (and Requester); retire AudioBookRequest.** It already does the job for its own downloads, is already deployed and configured for it, and needs only the ADR 0009 mounts, a folder-per-book template and the Downloaders' write access removed. `copy_complete.py`, `abr_mover.py`, the qBittorrent AutoRun and the `abr-prowlarr` categories (qBittorrent, SABnzbd, and wherever Prowlarr's download clients set it; Prowlarr is off the Host and was not inspected) go with ABR. For Books nothing changes in Roles: Shelfmark requests, CWA organizes.

#541 should, in addition to its checklist:

- Set `TEMPLATE_AUDIOBOOK_ORGANIZE` to a folder per book (Q1).
- Turn on per-user identity (proxy auth or OIDC) and requests in Shelfmark before ABR's users move over.
- Accept that torrents added outside Shelfmark (manual adds, autobrr) are not organized; audiobooks must be requested through Shelfmark.

**Fallback, if ABR's Audible search or auto-download must stay:** keep ABR as a Requester and write a small Custom Component in `jbruns/homelab-components` that takes over the `abr-prowlarr` category's finished downloads (qBittorrent and SABnzbd APIs, hardlink or move inside the Audiobooks tree, folder per book). Retirement Condition: Shelfmark (or ABR) imports finished downloads it did not start, e.g. by scanning a Downloader category.

**Other upstreams considered:**

- [Listenarr](https://github.com/Listenarrs/Listenarr): an *arr-style Requester and Organizer for audiobooks with qBittorrent and SABnzbd support; self-declared beta, AGPL-3.0, only canary tags. Too early.
- [Bookshelf](https://github.com/pennydreadful/bookshelf): a revival of [Readarr](https://github.com/Readarr/Readarr) (archived June 2025) for ebooks and audiobooks; GPL-3.0, no releases. Would replace Shelfmark, not complement it.
- [LazyLibrarian](https://gitlab.com/LazyLibrarian/LazyLibrarian): long-lived, GPL-3.0, active (commit 2026-10-17); follows authors, marks ebooks or audiobooks wanted, sends to SABnzbd/qBittorrent and processes the results. A full replacement for both Shelfmark and ABR, worth revisiting only if Shelfmark stalls.
