# Home Ops

The self-hosted services running on the TrueNAS host, and how they are deployed, ordered, watched, and alerted on.

## Topology

**Host**:
The single TrueNAS machine that runs every Service.
_Avoid_: Server, NAS, box

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
One kind of organized media that Media Servers present (e.g. TV, Movies, Music, Audiobooks).
_Avoid_: Collection, share, media folder

**Requester**:
A Service that decides what media to fetch and hands it to a Downloader, but never holds files itself (Shelfmark from users' requests, autobrr from announce filters).
_Avoid_: Indexer, grabber

**Downloader**:
A Service that fetches media and is responsible for each file until an Organizer takes it over (qBittorrent, SABnzbd). It never changes a Library.
_Avoid_: Download client (when meaning the role), grabber

**Organizer**:
A Service that takes over a Downloader's finished file, directly or through an Ingest Folder, moving it (or copying it while seeding must continue) into its own Library, and is responsible for it from then on (Sonarr for TV, Radarr for Movies, Shelfmark for Audiobooks, Calibre-Web-Automated for Books). It cannot change any other Library.
_Avoid_: arr, *arr, manager

**Ingest Folder**:
A folder outside a Library through which its Organizer takes over finished files that the Requester, not the Downloader, passes on (Calibre-Web-Automated's, which Shelfmark fills with Books; the `tv-ingest`, `movies-ingest` and `books-ingest` SMB shares, which people fill).
_Avoid_: Drop folder, watch folder, import folder

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

## Reliability

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
