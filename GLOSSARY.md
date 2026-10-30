# Self-hosted Services

The self-hosted services running on the TrueNAS host, and how they are deployed, ordered, watched, and alerted on.

## Topology

**Host**:
The single TrueNAS machine that runs every Service.
_Avoid_: Server, NAS, box

**Host Configuration**:
The Host's own settings that no Stack declares (its storage layout, shares, users, services and network), each kept only for a recorded purpose.
_Avoid_: TrueNAS settings, system config

**Stack**:
The unit of deployment: one or more Services plus the Datastores they need, deployed, isolated, and backed up together (e.g. nextcloud, vaultwarden, sonarr, edge).
_Avoid_: Project, App Group, app

**Platform Stack**:
A Stack grouped by the role it plays for the other Stacks rather than by app (edge, monitoring, ops). It is the only kind of Stack that may hold more than one app.
_Avoid_: Infra stack, core stack, common

**Service**:
One long-running workload in a Stack, plus any sidecars that exist only to serve it.
_Avoid_: App, container

**Exposed Service**:
A Service reachable by users through the reverse proxy.

**Protected Service**:
An Exposed Service whose web UI users sign in at the IdP, either through the reverse proxy or through the Service's own IdP support. Its web UI sends users to the IdP by default. Required clients that cannot sign in there use the Service's own sign-in: through a Client Path when the reverse proxy enforces the IdP, or directly when the Service redirects to the IdP itself.
_Avoid_: Authentik-protected, SSO app, behind Authentik

**Client Path**:
A route into a Protected Service that skips the reverse proxy's IdP check so a required non-browser client (e.g. an OPDS reader or Tinfoil) keeps working; the Service's own sign-in still applies.
_Avoid_: Bypass, hole, auth exception

**IdP**:
The identity provider where users of a Protected Service sign in. It is an External Dependency of every Protected Service.
_Avoid_: Authentik (the product, not the role), SSO

**Datastore**:
A Service whose only job is to hold state for the other Services in its Stack (MariaDB, Valkey, Postgres); never shared across Stacks.
_Avoid_: Backend, DB container

## Media

**Library**:
One kind of organized media that Media Servers present (e.g. TV, Movies, Music, Audiobooks, Books, Games).
_Avoid_: Collection, share, media folder

**Requester**:
A Service that decides what media to fetch and hands it to a Downloader, but never holds files itself (Shelfmark from users' requests, autobrr from announce filters).
_Avoid_: Indexer, grabber

**Downloader**:
A Service that fetches media and is responsible for each file until an Organizer takes it over (qBittorrent, SABnzbd). It never changes a Library.
_Avoid_: Download client (when meaning the role), grabber

**Organizer**:
A Service that takes over a Downloader's finished file, directly or through an Ingest Folder, moving it (or copying it while seeding must continue) into its own Library, or that publishes a Selection from a Source Set into it, and is responsible for it from then on (Sonarr for TV, Radarr for Movies, Shelfmark for Audiobooks, Calibre-Web-Automated for Books). It cannot change any other Library. The Operator is the Organizer of a Library, or of the part of one, that no Service organizes, curating it by hand (e.g. Rarities, or the Games folders no Service manages).
_Avoid_: arr, *arr, manager

**Source Set**:
A raw collection that can be audited against its published DAT (e.g. Redump, No-Intro, MAME), recorded with the DAT Revision of its latest Audit; the source from which the Games Library is curated. It changes only through an approved repair Change Plan.
_Avoid_: Verified Set, archive, romset, dump folder

**DAT Revision**:
One particular edition of a published inventory of expected game files and their identifying checksums. A newer revision does not replace the reference used by an earlier Audit or Selection.

**Audit**:
An assessment of a Source Set against a particular DAT Revision, recording discrepancies and the verification evidence actually obtained.

**Selection Profile**:
The Operator's rules for including or excluding releases and choosing preferred releases of a game.
_Avoid_: Filtered DAT (when meaning the rules)

**Selection**:
The exact releases chosen from a particular DAT Revision using a Selection Profile and any explicit Operator overrides.
_Avoid_: Profile (when meaning the chosen releases)

**Operator Addition**:
An item the Operator approves for management in the Games Library independently of an official DAT, such as a ROM hack. Its recorded provenance and local integrity checks do not make it an official DAT match.

**Change Plan**:
A proposed set of changes that the Operator approves before a Source Set is repaired or a Selection is published into the Games Library. Approval to repair a Source Set is separate from approval to publish into the Library.

**Ingest Folder**:
A folder outside a Library through which its Organizer takes over finished files that the Requester, not the Downloader, passes on (Calibre-Web-Automated's, which Shelfmark fills with Books; the `tv-ingest`, `movies-ingest` and `books-ingest` SMB shares, which people fill).
_Avoid_: Drop folder, watch folder, import folder

**Book**:
One record in the Books Library: a single work as the Operator chooses to keep it. A work has one Book unless a second Edition is kept on purpose.
_Avoid_: Title, entry, ebook (ambiguous with Format)

**Edition**:
One published version of a work (a translation, an abridgement, an annotated or illustrated release). A Book holds exactly one Edition.
_Avoid_: Version, release, copy

**Format**:
The one file type a Book holds, chosen by its content: EPUB for reflowable text, or PDF only where no EPUB exists and the layout is the content (textbooks, manuals). Comics and magazines are not Books.
_Avoid_: File, copy, version

**Retail EPUB**:
An EPUB as its publisher released it, rather than one a converter produced from another Format. The preferred EPUB when choosing between copies of a Book.
_Avoid_: Original, clean copy

**Media Server**:
A Service that presents Libraries to users and only reads those it does not organize (Plex, Emby, Navidrome, Audiobookshelf).
_Avoid_: Player, streamer

**Role**:
A part a Service plays in getting media into a Library: Requester, Downloader, Organizer, or Media Server. A Service may hold several Roles (Sonarr is a Requester and an Organizer; a recording Plex organizes its recordings Library), and gets only the storage access its Roles need.
_Avoid_: Type, kind

## Custom code

**Custom Component**:
Code we wrote that runs on the Host, kept only because no upstream or community option covers the need, and generic enough to adopt on another host.
_Avoid_: Script, glue, homegrown exporter

**Repo Tooling**:
Code we wrote that runs only in CI or on a contributor's machine, never on the Host.
_Avoid_: Scripts, helpers

**Retirement Condition**:
The stated upstream or community capability whose arrival makes a Custom Component unnecessary.
_Avoid_: Sunset, deprecation note

**Owned Application**:
An application we build and intend to maintain as a product, in its own repo, shipped and pinned like a Custom Component but without a Retirement Condition (e.g. Fuzzbin, Title Select).
_Avoid_: Custom Component (when meaning this), app

**Contract Test**:
A check that a Custom Component still works against the version of each upstream it depends on that the repo currently pins.

**Canary**:
A check that a Custom Component works against the newest stable release of each upstream it depends on. It warns ahead of a version bump and never blocks a merge.
_Avoid_: Nightly, smoke test

## Delivery

**Deploy**:
Making the Host match what the repo's default branch declares for a Stack.
_Avoid_: Sync, rollout, redeploy

**Baseline**:
The settings every Service must carry so that it is safe and observable by default, unless it records an Exemption.
_Avoid_: Template (the template is a starting point; the Baseline is the enforced minimum), defaults

**Exemption**:
A recorded, reasoned waiver of one Baseline item for one Service; an Exemption for an item the Service already meets is invalid.
_Avoid_: Override, exception

**Stray**:
A container, image, volume, or network on the Host that no Stack declares or uses, or a Rollback Snapshot past its release date. Data that belongs to a Stack is never a Stray, even while nothing uses it.
_Avoid_: Ad-hoc, orphan, leftover

**Rollback Snapshot**:
A ZFS snapshot taken before a risky change, recording the change it serves and a release date, and released once the change is verified. It replaces hand-made copies of data on the Host; it is never a Backup.
_Avoid_: .bak, pre-change copy, safety copy

**Periodic Snapshot**:
A ZFS snapshot taken on a schedule of data that people write to over a share, kept for a fixed period so they can restore what they deleted or overwrote themselves. It is neither a Backup nor a Rollback Snapshot.
_Avoid_: Auto snapshot, shadow copy

**Offline Copy**:
A copy of data that can be obtained again but is costly to rebuild (Libraries, software), kept on disks disconnected from the Host between runs. It is never a Backup.
_Avoid_: Cold backup, offline backup, external backup

## Secrets

**Operator**:
The person who edits this repo's secrets and holds the offline copies of its keys.
_Avoid_: Admin, owner

**Host Key**:
The age key doco-cd decrypts SOPS files with on the Host; the only key the Host holds.
_Avoid_: SOPS key, age key (ambiguous), deploy key

**Operator Key**:
The Operator's own age key, the second recipient of every SOPS file; it never goes on the Host.
_Avoid_: Personal key, admin key

## Reliability

**Backup**:
A restic snapshot of a Stack's volumes and database dumps, of Device Backups and of the Operator's private files, taken nightly by stack-back and stored offsite; the only authoritative copy of a Stack's data. An app's own scheduled backups are part of its state, not Backups.
_Avoid_: Dump, archive, copy, .bak

**Device Backup**:
A backup that a device off the Host writes into its own space on the Host. A device can reach only its own Device Backups, never another's.
_Avoid_: Backup (alone), device dump

**Tier**:
How much it matters when a Service is down: Critical, Warning, or Info.
_Avoid_: Priority, importance

**Critical**:
A Tier whose failure Pages immediately, at any hour (e.g. Traefik, Vaultwarden, Nextcloud and its database, pool health, backups).

**Warning**:
A Tier whose failure is reported in the Digest, not Paged (e.g. media and download Services).

**Info**:
A Tier that is visible on dashboards only and never notifies.

**Page**:
A notification that demands attention now, delivered to a phone.
_Avoid_: Alert (too broad), ping

**Digest**:
A lower-urgency notification delivered to a chat channel for review at leisure.

**Heartbeat**:
A continuous "still alive" signal sent off-Host, whose absence is itself the alarm.
_Avoid_: Watchdog, dead man's switch

**Probe**:
An outside-in check that a Service answers as a user would reach it.
_Avoid_: Health check (that is the Service's own self-report)

**Uptime**:
Whether a Service is serving: for an Exposed Service, its Probes succeed (for a Protected Service, both the redirect to the IdP and an unauthenticated health path); for any other Service, its own health check reports healthy. A running container behind a failing Probe is not up.
_Avoid_: Availability (reserve for Uptime aggregated over a period), up (the scrape metric)

**Availability**:
The share of a period during which a Service had Uptime (e.g. 99.9% over 30 days).

**Dependency**:
Something a Service needs in order to do its job, such as Sonarr needing its indexer search and download client. It is distinct from Startup Order, which only governs readiness at boot.
_Avoid_: Upstream, backend

**Internal Dependency**:
A Dependency that is itself a Service on the Host.

**External Dependency**:
A Dependency outside the Host, whether on another machine at home (e.g. nzbhydra2) or a third party (e.g. a Usenet provider).

**Startup Order**:
The sequence in which Services must become ready after the Host boots, so that dependents start only after what they depend on.
_Avoid_: Boot order, dependency chain
