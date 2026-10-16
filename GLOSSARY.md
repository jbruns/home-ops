# Home Ops

The self-hosted services running on the TrueNAS host, and how they are deployed, ordered, watched, and alerted on.

## Topology

**Host**:
The single TrueNAS machine that runs every Service.
_Avoid_: Server, NAS, box

**Stack**:
The unit of deployment: one or more Services plus the Datastores they need, deployed, isolated, and backed up together (e.g. nextcloud, vaultwarden, arr, edge).
_Avoid_: Project, App Group, app

**Area**:
A topical grouping of Stacks (common, home, media, monitoring) with no runtime meaning.
_Avoid_: Stack (for the topical folders), domain

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

**Startup Order**:
The sequence in which Services must become ready after the Host boots, so that dependents start only after what they depend on.
_Avoid_: Boot order, dependency chain
