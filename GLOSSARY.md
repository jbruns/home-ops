# Home Ops

The self-hosted services running on the TrueNAS host, and how they are deployed, ordered, watched, and alerted on.

## Topology

**Host**:
The single TrueNAS machine that runs every Service.
_Avoid_: Server, NAS, box

**Stack**:
The unit of deployment: one or more Services plus the Datastores they need, deployed, isolated, and backed up together (e.g. nextcloud, vaultwarden, arr, edge).
_Avoid_: Project, App Group, app

**Service**:
One long-running workload in a Stack, plus any sidecars that exist only to serve it.
_Avoid_: App, container

**Exposed Service**:
A Service reachable by users through the reverse proxy.

**Datastore**:
A Service whose only job is to hold state for the other Services in its Stack (MariaDB, Valkey, Postgres); never shared across Stacks.
_Avoid_: Backend, DB container

## Delivery

**Deploy**:
Making the Host match what the repo's default branch declares for a Stack.
_Avoid_: Sync, rollout, redeploy

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
Whether a Service is serving: for an Exposed Service, its Probe succeeds; for any other Service, its own health check reports healthy. A running container behind a failing Probe is not up.
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
