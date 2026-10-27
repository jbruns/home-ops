# Restic is the only Backup; risky changes use Rollback Snapshots, not copies

An audit of the Host found 1.2 TiB of tarballs from a backup tool retired in July 2025, sitting next to the restic repository and mirrored offsite, plus about twenty one-off copies made before risky changes and never removed: `.bak` files, dated `pre-*` dumps, and a copy of Traefik's certificate store holding TLS private keys. Each was made for a good reason. None had a plan for its removal.

The stack-back restic repository, copied offsite to B2, is the only Backup. [ADR 0014](0014-backups-go-straight-to-the-hosts-own-bucket.md) amends where it lives: stack-back now writes straight to the Host's own B2 bucket, with no local copy. An app's own scheduled backups (Plex's dated databases, Emby's server backups, the *arr `scheduled/` zips) stay: they give a quick in-app restore and are pruned by the app, and they are part of the app's state, not Backups. Device Backups (other devices' own backups, one dataset each under `andromeda/backups`) are the one other kind of copy the Host keeps: each device writes and prunes its own, and the Backup carries them offsite too, through read-only volumes on the `device-backups` container (#728). Nothing else on the Host is a copy of data.

Before a risky change, take a Rollback Snapshot rather than a copy: a ZFS snapshot carrying the user properties `homeops:issue` (the change it serves) and `homeops:expires` (its release date, by default 7 days out), taken and released with `hops snapshot`. Release it once the change is verified. One left past its release date is a Stray, so `hops strays` reports it; piling up snapshots would only move the problem. Where a snapshot cannot serve (data that must leave the Host, or a single file needed outside its dataset), record the copy in the issue or PR that made it with a removal checkbox, and the work is not done until it is ticked.

## Considered Options

- **Keep pre-change copies in one agreed folder with an expiry**: rejected; it still needs a sweeper, copies sensitive data into a second place, and costs space a snapshot shares.
- **Turn off the apps' own backups**: rejected; they restore inside the app in seconds, and each app prunes its own.
- **Monitor volumes for `*.bak` and dated copies**: rejected for now; making copies explicit and tracked is cheaper than detecting them afterwards.
