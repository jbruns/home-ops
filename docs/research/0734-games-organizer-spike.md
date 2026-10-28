# Games Organizer: go/no-go spike

Findings for #781, part of #734. Researched 2026-10-28 against primary sources and the Operator's own data. The
Verified Set was read in place on the Host and read-only. Only DAT metadata and counts left the Host.

## Verdict

**Go.** Build the Games Organizer as an Owned Application ([ADR 0018](../adr/0018-owned-applications-live-in-their-own-repos.md)).

**Selection engine for the pilot: pinned Retool (Python) inside the image, patched to emit a structured trace.**
A Rust port stays possible later, gated on a conformance suite that uses pinned Retool as its oracle. The audit and
evidence engine, the job engine and the service are Rust, as #734 already says.

Why:

- igir and Retool together leave whole requirements unmet (§1): no DAT store or provenance, no Change Plans, no
  durable jobs or UI, no bad/unreadable statuses, and no reading of nested archives.
- Retool already produces rule-by-rule decisions on the real Atari DAT (§3). A structured trace is a patch of about
  300–600 lines. A Rust port is a Large effort: about 7,900 lines of order-sensitive selection code and 90 regexes
  with no written specification (§4).
- The data is not the risk. No-Intro's licence permits reuse (§5). The Atari DAT carries CRC32, MD5 and SHA-1 on
  every ROM (§2), and a full payload pass over the whole set takes under a second (§6).

## 1. Does igir plus Retool already cover the workflow?

No. Pinned igir v5.8.0 (`df2ff6f`) and Retool 2.4.9 (`59f169f`) were compared against the agreed workflow in #734.

| Requirement | igir | Retool | Gap |
| --- | --- | --- | --- |
| Revision-pinned DAT store with provenance | Reads DATs per run ([scanning](https://github.com/emmercm/igir/blob/v5.8.0/docs/dats/scanning.md)) | Writes new filtered DATs | No store, no revision pin, no provenance |
| Quick / incremental / full evidence | `--input-checksum-quick` / `-min` / `-max`; archive CRCs and CHD header hashes documented as claims ([matching](https://github.com/emmercm/igir/blob/v5.8.0/docs/roms/matching.md)); persistent cache invalidated on size+mtime ([fileCache.ts](https://github.com/emmercm/igir/blob/v5.8.0/src/cache/fileCache.ts)) | None | Report does not record which algorithms were checked or when the payload was read |
| Missing / bad / extra / misnamed / unreadable, errors never hidden | Statuses FOUND, INCOMPLETE, MISSING, DUPLICATE, UNUSED, DELETED ([datStatus.ts](https://github.com/emmercm/igir/blob/v5.8.0/src/models/datStatus.ts)) | None | No bad, conflicting or unreadable status. Parse errors are only logged ([scanner.ts](https://github.com/emmercm/igir/blob/v5.8.0/src/modules/scanner.ts)) |
| In-place audit, including the Operator's ZIP-of-ZIPs layout | `igir report` is read-only on single-level archives | None | "Igir will not read the contents of archives within archives" ([reading-archives](https://github.com/emmercm/igir/blob/v5.8.0/docs/input/reading-archives.md)) |
| Explained 1G1R selection | Ordered `--prefer-*` options ([1g1r](https://github.com/emmercm/igir/blob/v5.8.0/docs/roms/1g1r.md)) | Ordered filter pipeline with console `--trace` | No machine-readable trace in either tool |
| Previewable, approved repair and publish Change Plans | `--clean-dry-run` and `--clean-backup`, for `clean` only ([cleaning](https://github.com/emmercm/igir/blob/v5.8.0/docs/output/cleaning.md)) | None | No plan object, stale-plan rejection, external-change detection, output provenance or partial publication |
| Durable jobs, web UI, resource limits | Reader, writer and DAT thread counts only | None | No job state, server, CPU quota or byte-rate caps |

**What to reuse from igir:**

- Its three checksum tiers and size+mtime cache, as the baseline for the scale benchmark at equivalent evidence
  levels: `igir report --input-checksum-quick`, the default run, and
  `--input-checksum-min SHA1 --input-checksum-max SHA256 --disable-cache`.
- Its table of what each archive format can supply.
- Its non-destructive-by-default principles.

## 2. What does the Atari DAT actually contain?

`No-Intro Love Pack (DAT) (2026-10-28)/No-Intro/Atari - Atari 2600 (20261027-195049).dat`. Its SHA-256 begins
`254ece596bb473c2`.

- No-Intro XML schema v4. The header holds `id` 88, `version` 20261027-195049, a `tos` link to the terms in §5,
  and `clrmamepro forcenodump="required"`.
- **872 games and 878 ROMs.** 871 games have one ROM. One has seven `.wav` files, so the pilot must handle
  multi-file games. Extensions are 859 `.a26`, 12 `.bin` and 7 `.wav`.
- **Hashes:** every ROM has `size`, `crc`, `md5` and `sha1`. Only 312 have `sha256`. The strongest available hash
  therefore varies per ROM, so evidence must be recorded per ROM, not per DAT. No CRC or SHA-1 value repeats.
- **Status:** 152 ROMs are `verified`, 2 are `baddump` and 724 are unmarked.
- **Parent/clone:** 172 games carry a `cloneofid`, even in the standard DAT. No game uses `cloneof`.
- **Categories** are almost unused: 32 `Games` and 840 empty.
- ROM sizes run from 2 KiB to 9.1 MiB, 37.4 MiB in total.

**Clone list and metadata coverage** (pinned `retool-clonelists-metadata` `cdeb391`):

- The clone list was last updated 2026-01-02. It has 77 groups and 160 search terms; 12 terms match no title in
  this DAT, mostly aftermarket titles the DAT has since dropped.
- The metadata has 911 entries carrying `languages` only. It covers 853 of the 872 DAT names. The 19 uncovered
  names are recent renames. 58 metadata entries name titles absent from this DAT.
- Both upstream repositories are **unmaintained** ([retool readme](https://github.com/unexpectedpanda/retool/blob/59f169f530580448c055c781cb3166910c7bdba5/readme.md),
  [clone list readme](https://github.com/unexpectedpanda/retool-clonelists-metadata/blob/cdeb391e2ea422475dc82091278c9c9ae3f340ae/readme.md)).
  Clone and metadata coverage will drift from new DAT Revisions however the selector is built. The Organizer
  should show that drift (unmatched search terms, uncovered names), not hide it.

## 3. Can pinned Retool emit a structured decision trace?

Yes, and most of the work is already done.

- **Headless run on the real DAT:** pinned Retool runs without its GUI dependencies. Install only `alive-progress`,
  `psutil`, `strictyaml`, `validators` and `lxml`. It needs `config/internal-config.json` copied from the pinned
  clone list repository, because otherwise it downloads the latest one. Interactive pauses must be fed from stdin.
  With the pinned clone list and metadata it reduced the DAT from 872 to 602 titles in 0.48 s.
- **Existing trace:** Retool defines 134 named rule references (`REF0001`…), emitted from about 45 call sites in
  its selection chain ([titletools.py](https://github.com/unexpectedpanda/retool/blob/59f169f530580448c055c781cb3166910c7bdba5/modules/titletools.py)).
  A `--trace "^Asteroids"` run on the real DAT walked the group through each stage. One step was
  `REF0030: Favor multiple regions higher up user region order`, keeping `Asteroids (Japan, USA) (En)` and removing
  `Asteroids (USA)` and `Asteroids (USA) (Alt)`. This is the "rule responsible" #734 asks for.
- **Gap:** the trace is console text only, and runs single-process.
- **Patch:** change `TraceTools.trace_title` to append records to a sink, recording group, stage, rule reference,
  candidates before and after, kept and removed. Thread the sink through the chain, and merge per-worker sinks in
  the orchestrator. The rule catalogue stays unchanged. Estimate: 300–600 lines.
- **Ownership:** because Retool is unmaintained, this patch makes us its maintainer. Carry it as a patch set
  against a pinned upstream commit, inside the Organizer's repo.

## 4. Is a Rust port of Retool feasible?

It is feasible but **Large**. It is not justified for the pilot.

- **Size:** the selection core (DAT parsing, clone lists, title selection) is about 7,900 lines. The shared title
  helpers add 1,352 lines, including 90 regexes and about 750 lines of title normalisation. The GUI (30,000 lines)
  is irrelevant.
- **Highest risks:**
  - Title normalisation and system-specific version regexes, which are documented only by the code; the design
    document covers only its first stage.
  - Compilation and superset handling, 840 lines with 23 trace points.
  - The fixed order of more than 18 precedence stages.
  - Deterministic handling that Retool forces single-process.
- **Conformance:** Retool ships an integration suite with 176 synthetic source DATs, 11 user configurations and six
  golden scenarios. Run pinned Retool and a port on the same DAT, clone list, metadata and configuration, then:
  - diff the kept titles;
  - once the §3 patch exists, also compare structured traces stage by stage to catch "right answer, wrong reason";
  - extend to real DATs after that.
- **Recommendation:** keep the port as a later, separately approved option. Its precondition is the §3 trace patch
  plus a conformance suite, because the patched Retool is the oracle any port must match.

## 5. What are No-Intro's terms?

- **Licence:** No-Intro's [Data Usage License](https://datomatic.no-intro.org/terms.html#Data) (last updated
  2026-09-11) is linked from every DAT's `tos` header. It states that the data "may be freely used, copied,
  reproduced, modified, adapted, combined, published, distributed, and otherwise reused by anyone for any lawful
  purpose", with no attribution required. It covers only rights No-Intro controls, and third-party trademarks and
  names are excluded.
- **Manual import:** fine for the pilot. Store the DAT unmodified for provenance.
- **Test fixtures:** synthetic fixtures remain the default for edge cases. Real No-Intro DATs may be used for
  conformance tests under the licence.
- **Unattended refresh:** reuse of the data is licensed, but automated access is governed only by
  [robots.txt](https://datomatic.no-intro.org/robots.txt): `Crawl-delay: 5`, `Request-rate: 1/5`. Dat-o-Matic bans
  clients that send anomalous requests, and lifting a ban needs manual contact. A future adapter should:
  - fetch at most once a day, paced well inside those directives;
  - never retry in a loop;
  - fall back to manual import and keep the last good revision.
- **Redump:** its policy pages and wiki were unreachable at the time of checking, so its conditions are not
  stated. `robots.txt` is permissive. Do not build a Redump adapter until its terms can be read.

## 6. Prototype Audit of the Atari Verified Set

A throwaway script was run on the Host: no network, read-only mount, one CPU. It audited
`/mnt/andromeda/games/verified/No-Intro/Atari - 2600.zip` against the DAT twice. The first pass trusted only the
inner archive directory claims (name, size, CRC32). The second decompressed every payload and checked every hash
the DAT supplies, in one pass.

| Pass | Matched | Bad | Misnamed | Not in DAT | Time | Bytes |
| --- | --- | --- | --- | --- | --- | --- |
| Directory claims | 786 | 3 | 19 | 41 | 0.21 s | 23.7 MB compressed |
| Full payload (CRC32, MD5, SHA-1, plus SHA-256 where supplied) | 786 | 3 | 19 | 41 | 0.49 s | 39.2 MB uncompressed |

- 89 DAT games are missing. The 19 misnamed entries are titles the DAT has renamed (e.g. `Asteroids (USA)`, held as
  `Asteroids (USA) (Atari Anthology)`). The 41 entries not in the DAT are mostly aftermarket titles it has dropped.
  The 3 bad entries (`Alpha Beam with Ernie (Europe)`, `Breakout (Europe)`, `Space Invaders (Europe)`) fail every
  hash under their current names.
- Full verification checked CRC32, MD5 and SHA-1 for 562 ROMs, plus SHA-256 for 227.
- The two passes agree here, but only the second is payload evidence. The pilot must label them differently, as
  #734 requires.
- **Performance:** the Atari set is too small to say anything about performance. It confirms #734's split: the pilot
  proves correctness, and a separate gate measures scale.

## 7. Further findings

**The Verified Set does not record its DAT Revision.** The pattern above (renames, dropped titles, changed dumps)
is what a set built against an older DAT Revision looks like, but nothing records which one. The set is not kept
with its DAT either: DATs arrive as a dated Love Pack in a separate folder, and the set's filename
(`Atari - 2600.zip`) does not match the DAT's system name. Recording "last verified against" for each Verified Set is
one of the Organizer's core motivations. It falls out of pinning every Audit to a DAT Revision. For existing sets,
an optional bootstrap step can audit against each DAT Revision available and report the best fit, as a hint, never
as evidence.

**Storage layout.** 188 of the 195 No-Intro systems are stored as one ZIP per system; the other seven, all digital
sets, are folders. The Atari ZIP holds one ZIP per game (843 of them). That layout:

- cannot be read by igir;
- defeats incremental Audits, because one change rewrites the whole file;
- turns every repair into rewriting the system archive;
- complicates publishing a curated subset.

The Operator has agreed to move to **one ZIP per game, in a folder per system**, the common No-Intro layout. That
restructuring is an Operator storage task, separate from the Organizer. The pilot targets per-game ZIPs and loose
files and does not need nested-archive support.

**Glossary.** The glossary says a Verified Set is "kept with that DAT". Proposed sharpening: "kept with the DAT
Revision of its latest Audit".

## Consequences for the pilot

- Slice 1 (headless in-place Audit) assumes the per-game layout. Restructuring the Atari set first is a prerequisite
  owned by the Operator.
- Evidence is recorded per ROM, with the algorithms actually checked, because SHA-256 coverage is partial.
- Multi-file games (the `.wav` title) and `baddump` entries are in the pilot's fixture set.
- The Selection slice carries a patched, pinned Retool, and it shows clone and metadata coverage drift against the
  pinned DAT Revision.

## Open questions

- Where the Retool patch set lives (in the Organizer repo, or a fork) and how it is pinned. Settle this when the
  repo is scaffolded.
- Redump conditions, before any Redump adapter.
- Whether the unmaintained clone lists need a successor or local overrides as DATs drift. Revisit after the Selection
  slice.
