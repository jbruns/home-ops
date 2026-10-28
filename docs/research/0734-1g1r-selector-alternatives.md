# 1G1R selector alternatives

Findings for #785, part of #734. Researched 2026-10-28 against primary sources.

## Verdict

**Keep pinned Retool for the pilot. Do not switch.** No candidate found is both maintained and a
functional match. No-Intro's own `cloneofid` data does not make Retool's clone lists redundant: it is a
region-only parent/clone graph, with no language, compilation, or superset handling (§3). The best
available option is what #781 already chose: pinned Retool 2.4.9 (`59f169f`), patched to emit a structured
trace, with its clone lists and metadata pinned alongside it.

Why not switch:

- **igir** is the only actively maintained, headless, pinnable candidate, and it already reads
  `cloneofid` natively (§2, §3). But its `--prefer-*` chain is a region/language/revision scoring system,
  the same family of algorithm Retool's own docs argue is insufficient (§1, §2). It has no clone list, no
  metadata-language lookup, and no compilation/superset handling.
- **ClrMamePro** and **RomVault** are both actively maintained in 2026, but their 1G1R support is the
  classic parent/clone + region-priority model Retool was built to replace (§1), and neither publishes a
  rule-level trace.
- **SabreTools**, **romba**, and a "Retool successor" turned up by search (§2) are either not a 1G1R
  selector, dormant, or too new and unvetted to trust for #734's explainability requirement.
- No maintained fork of Retool exists, and no maintained successor to its clone-list data exists (§2, §3).

Risk of staying on Retool: it is frozen as code, and its own author says 1G1R quality will "wane over
time" as new titles outpace hand-curated clone lists ([issue #337](https://github.com/unexpectedpanda/retool/issues/337),
opened 2024-04-28). The Organizer should surface that drift, as #781 already recommended, rather than
hide it.

## 1. Retool's 1G1R model, and what it does differently from DAT parent/clone

Retool's own case against standard DAT-based 1G1R is laid out in
["Why Retool's 1G1R is better"](https://unexpectedpanda.github.io/retool/retool-1g1r/):

- **The standard model.** No-Intro's own `cloneof`/`cloneofid` parent/clone data, and the "P/C XML" DATs
  built from it, implement 1G1R as pure **region priority**, with language used only as a soft bonus, not
  a filter. No-Intro's own help text for these DATs confirms this directly: 1G1R and ROM-merging are both
  driven only by a user's region order, with no language input at all
  ([help_pc_dat.txt](https://datomatic.no-intro.org/stuff/help_pc_dat.txt)). Retool's docs give a worked
  example where this picks a French-only Canadian title over an English-capable Norwegian one, because
  region outranks language absolutely.
- **The version problem.** Parent/clone data has no concept of revision ordering across regions. Retool's
  docs show CLRMAMEPro and RomCenter picking different, and both wrong, titles from the same four-way
  clone group once a non-parent region is preferred, because the "parent" is arbitrarily the first
  region, not the best version.
- **What Retool does instead, per its own docs:**
  - It **ignores DAT `cloneof`/`cloneofid` entirely** and re-derives groups itself from title names, "to
    not only close the gap where automatic detection doesn't work out, but to recategorize and
    prioritize titles accordingly."
  - **Clone lists** (JSON files) hand-fix groupings automatic detection misses, including titles with
    unrelated names.
  - **Metadata files** are scraped language data from No-Intro's and Redump's sites, used when a title's
    name alone doesn't state its language.
  - Selection is a **filter pipeline**, not a region/language score: it treats language as both a filter
    and (optionally) a higher priority than region, and applies an ordered set of criteria covering
    version/revision, retail status, production status, and more, rather than one weighted score.
  - It has explicit, named handling for **compilations** (a title containing other titles) and
    **supersets** (e.g. Game of the Year editions), which the docs call out as unaddressed by the
    parent/clone score model.
  - **Title normalisation**: per #781's code reading, about 750 lines and 90 regexes
    ([titletools.py](https://github.com/unexpectedpanda/retool/blob/59f169f530580448c055c781cb3166910c7bdba5/modules/titletools.py)),
    used to strip region/language/version tags before name-based grouping.
- Retool's [terminology page](https://unexpectedpanda.github.io/retool/terminology/) also defines
  **implied languages** (e.g. USA implies English) used when neither a title's name nor its metadata
  states a language, which further distinguishes it from the no-language P/C model.

## 2. Candidates

| Tool | Last release | Last commit (primary source) | Licence | Language | Headless/CLI | Reads `cloneofid` / clone lists | Traceable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Retool | 2.4.9 (pinned) | 2024-04-28 announced unmaintained ([issue #337](https://github.com/unexpectedpanda/retool/issues/337)) | BSD-3-Clause | Python | CLI + GUI | No; own name-based grouping + its own clone lists | Console `--trace`; patch planned (#781 §3) |
| igir | v5.8.0, 2026-10-21 ([release](https://github.com/emmercm/igir/releases/tag/v5.8.0)) | 2026-10-28 ([commit 744bc27](https://github.com/emmercm/igir/commit/744bc279bd2d9722fcecf16d29608c217e7a1912)) | GPL-3.0-or-later ([`package.json`](https://raw.githubusercontent.com/emmercm/igir/v5.8.0/package.json)) | TypeScript/Node | CLI-first, scriptable by design | Yes, natively: `@Expose({ name: 'cloneofid' })` ([`game.ts:228-229`](https://github.com/emmercm/igir/blob/v5.8.0/src/models/dats/game.ts#L228-L229)), used in parent-building ([`dat.ts`](https://github.com/emmercm/igir/blob/v5.8.0/src/models/dats/dat.ts)) and inference fallback ([`datParentInferrer.ts`](https://github.com/emmercm/igir/blob/v5.8.0/src/modules/dats/datParentInferrer.ts)); no clone-list or metadata-language input | No rule trace; deterministic given same DATs/flags ([1g1r.md](https://raw.githubusercontent.com/emmercm/igir/v5.8.0/docs/roms/1g1r.md)) |
| ClrMamePro | 0.7.x ([changelog](https://mamedev.emulab.it/clrmamepro/)); exact date unverified | Closed source; changelog shows 2026 activity | Unverified (closed-source freeware; no licence file found) | Unverified (native Windows) | Has a scanner/rebuilder mode usable from scripts, but is fundamentally a Windows GUI tool; no documented headless Linux CLI found | Reads standard parent/clone and P/C XML DATs; region-priority 1G1R only ([help_pc_dat.txt](https://datomatic.no-intro.org/stuff/help_pc_dat.txt)) | No |
| RomVault | 3.8.0 GUI, 2026-08-04; CLI 3.7.6 ([romvault.com](https://www.romvault.com/)) | 2026-10-21 ([`RVWorld` repo](https://github.com/RomVault/RVWorld)) | Apache-2.0 (GitHub API licence field on `RomVault/RVWorld`) | C# | Dedicated `RVCmd` CLI build for Windows and Linux ([romvault.com downloads](https://www.romvault.com/)) | Reads parent/clone and P/C XML DATs; region-priority model, same as ClrMamePro | No rule trace found |
| SabreTools | 1.2.1, 2025-05-26 ([release](https://github.com/SabreTools/SabreTools/releases/tag/1.2.1)) | 2026-09-21 ([commit `2001df7`](https://github.com/SabreTools/SabreTools)) | MIT | C# | CLI DAT management tool | Parses parent/clone fields as part of general DAT editing; not a dedicated 1G1R selector — no `--prefer-*` equivalent found in its docs | Unverified |
| romba | None published | 2020-08-10 (`a51af6d`); repo last pushed 2023-09-22 ([repo](https://github.com/uwedeportivo/romba)) | Go licence, per its README | Go | CLI plus web shell; Docker image | Not a 1G1R selector: a de-duplicated ROM store that builds sets from DATs on demand | n/a |
| Retool forks (`retool-redux`, `retooled`, others) | n/a | Most recent fork push 2026-10-23, but the most descriptively named fork (`retool-redux`) is only 3 commits ahead and 23 behind upstream | Same as Retool | Python | Same as Retool | Same as Retool | Same as Retool |
| "Retro Tools 2026" ([repo](https://github.com/Patrickjaillet/retrotools26)) | v0.1.7, 2026-08-14 | 2026-08-14, all 34 commits from one author within a ~20 hour window ([commit list](https://github.com/Patrickjaillet/retrotools26/commits)) | MIT | Rust | Desktop GUI (egui) plus a CLI, per its own description | Claims No-Intro/Redump/TOSEC parsing and a "1G1R engine"; no evidence found of clone-list or metadata-language input | Unverified; single-contributor project, 1 GitHub star, three months old at research time |

Notes on the table:

- **igir** is the strongest technical candidate on headlessness and maintenance, but its own docs describe
  `--prefer-*` as a priority chain over region, language, revision, and a few other binary flags
  ([1g1r.md](https://raw.githubusercontent.com/emmercm/igir/v5.8.0/docs/roms/1g1r.md)) — exactly the
  region/language-score family Retool's docs argue against (§1). It has no compilation/superset concept
  and no clone-list equivalent; its own docs point users at Retool "(no longer maintained)" for anything
  clone lists would catch ([`processing.md`](https://raw.githubusercontent.com/emmercm/igir/v5.8.0/docs/dats/processing.md)).
- **ClrMamePro and RomVault** are both healthily maintained, but checked against §1's own criteria they
  are the tools Retool was written to replace. Neither exposes a decision trace; both are closed or
  effectively GUI-first (RomVault's CLI build exists, ClrMamePro's does not look confirmed).
- **SabreTools** is a general DAT-manipulation tool (merge, split, convert formats); its GitHub description
  is "DAT management tool with advanced editing and sorting features," not a 1G1R engine. No `--prefer-*`
  style option was found in a brief pass of its wiki links from Retool's own terminology page.
- **romba** is dormant (no commits since 2020) and is a de-duplicated ROM store that builds any DAT's set on
  demand ([README](https://github.com/uwedeportivo/romba)). It has no 1G1R selection of its own.
- **"Retro Tools 2026"** surfaced from a web search summary, not from GitHub's own search or a linked
  release note anywhere else. Its repository is three months old at research time, has one contributor,
  one star, and no issues or PR history to check correctness claims against. Its 1G1R and clone-handling
  claims come only from its own README/description, not from independent use. Treat this as **unverified
  and not evaluated further**; it is too new and unvetted for #734's explainability bar.
- **Retool forks**: the network of unexpectedpanda/retool on GitHub has about 20 forks. The most
  descriptively named continuation, `retool-redux`, is 3 commits ahead and 23 behind upstream
  main — not a meaningful continuation. No fork was found with a divergent, active commit history.

## 3. Is there a maintained successor to the clone-list/metadata data?

No maintained drop-in successor was found.

- **`retool-clonelists-metadata` itself** still receives commits, but almost all recent ones are an
  automated "Updated RetroAchievements files" job (10 of the last 10 commits checked, from
  2026-07-26 to 2026-10-26). The clone-list **content** itself was last substantively touched
  2026-07-25 (`16c22ae`, "Updated clone lists"), separate from the RetroAchievements sync. The repository
  is not archived and accepts some updates, but there is no stated maintainer plan and Retool's own
  "no longer maintained" announcement (2024-04-28) still stands.
- **No-Intro's own parent/clone data** (`cloneof`/`cloneofid`, and the "P/C XML" DAT download option) is
  actively maintained as part of each DAT itself — it is not a separate project that can go stale the way
  Retool's clone lists can. But per §1 and the P/C DAT help file, it is a region-priority-only model: no
  language filter, no compilation handling, no superset handling, and groupings are "generally
  administered by hand" per Retool's own terminology page, with no automatic detection for retitled
  cross-region releases that don't share a `cloneofid`.
- **Where No-Intro's own data falls short, concretely:** Retool's docs describe real DAT cases where
  cross-region releases are **not** linked by `cloneof`/`cloneofid` at all, because a human didn't
  manually make the connection, and Retool's own text-matching plus its clone lists exist specifically to
  close that gap. No-Intro's data also carries no per-title language-capability metadata beyond what's in
  the title string itself, so where a region's language isn't implied or named, there is nothing to go on
  without Retool-style scraped metadata.
- **Other 1G1R DATs published by others**: none were found in this spike beyond No-Intro's own P/C XML
  and Redump (which, per igir's own docs, doesn't publish parent/clone information at all —
  ["Redump DATs don't provide parent/clone information"](https://raw.githubusercontent.com/emmercm/igir/v5.8.0/docs/dats/processing.md)).

**So:** No-Intro's `cloneofid` does not make Retool's clone lists redundant. It covers a different,
narrower case (same-name or already-linked regional variants by region alone), and still needs the
Organizer, or a patched Retool, to supply language filtering, compilation/superset handling, and
cross-region title-variant linking that isn't already captured by a human-entered `cloneofid`.

## 4. Recommendation for #734

**Keep pinned Retool for the pilot (as #781 already decided), and track clone-list drift explicitly.**

- Do not switch to igir's `--prefer-*` chain as the primary selector: it reproduces the region/language
  scoring model Retool's own docs argue against, and it has no clone-list or compilation/superset
  handling. It remains useful for 1G1R sets built from DATs that already have good parent/clone data and
  no language-metadata gaps, but #734's Atari DAT (§2 of #781) shows exactly those gaps.
- Do not switch to ClrMamePro or RomVault: both are healthily maintained, but their 1G1R is the same
  region-priority model, and #734 requires a rule-responsible, explainable decision per title, which
  neither tool is documented to produce.
- Combining Retool-style rules over No-Intro's own `cloneofid` graph, instead of Retool's name-matched
  groups, was considered. It is not recommended now: `cloneofid` links are entered by hand (172 of 872 Atari 2600 games are
  marked as clones, per #781 §2), and Retool's docs describe cross-region variants that no human linked.
  Using it as the primary grouping risks missing groups that Retool's name matching and clone lists catch.
  How many is unmeasured; diffing Retool's groups against `cloneofid` on the Atari DAT would settle it. It could be layered in later as one more input
  to the trace (a clone group found "by `cloneofid`" is a distinct, citable rule from one found "by title
  match" or "by clone list"), which fits #734's rule-responsible requirement well, but that is new
  Organizer work, not a tool swap.
- **Risk of staying on Retool:** it is frozen, its clone lists will drift further from new DAT Revisions,
  and the Organizer now owns a patch set against unmaintained code (#781 §3). Mitigate by showing drift in
  the Organizer's output (unmatched search terms, uncovered names — #781 §2) rather than hiding it, and by
  keeping the patch small and oracle-testable so a future replacement selector can be validated against
  it.
- **Risk of the "do nothing" alternative** (ship with no clone lists, No-Intro `cloneofid` only): it would
  silently regress 1G1R quality for every game not already linked by a human-entered `cloneofid`, with no
  way for the Operator to see what was missed.

## Open questions

- Whether SabreTools' DAT-editing feature set includes any 1G1R-equivalent mode; only its GitHub
  description and release metadata were checked in this spike, not its CLI reference.
- Whether ClrMamePro or RomVault can run fully headless on Linux in the Host's container environment;
  RomVault publishes a Linux CLI build, but neither tool's 1G1R mode was traced for rule-level output, so
  this question is secondary to the headlessness question itself.
- Whether any of Retool's ~20 GitHub forks beyond the three checked here have meaningful, active
  divergence; only `retool-redux`, `retooled` and one other fork were checked for commit
  divergence.
- "Retro Tools 2026" should be re-checked in 6-12 months. If it survives, gains contributors, and
  publishes real DAT test coverage, it may be worth a second look; today it is unverified and too new to
  trust.
