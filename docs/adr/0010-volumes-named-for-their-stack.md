# Volumes are named for their Stack; data volumes are external

ADR 0004 kept the topical prefixes (`home_`, `media_`, `common_`, `asot-media_`) on volumes created before the one-Stack-per-app split, so that the split would move no data. That left most volumes named after a Stack that no longer exists: `home_mariadb_data` belongs to nextcloud, `common_traefik_le` to edge, `asot-media_cwa-downloader_config` to shelfmark. Finding a volume's owner, or a Datastore's data, took a lookup. We now name every volume after the Stack that owns it, which supersedes ADR 0004's "keep the old prefixes" clause.

A volume is named `<stack>_[<service>_]<purpose>`, leaving out `<service>` when it equals the Stack: `vaultwarden_data`, `vaultwarden_mariadb_data`, `nextcloud_data`, `edge_traefik_le`, `ops_portainer_data`. This is the name Compose would give the key `<service>_<purpose>` in project `<stack>`, so a volume reads the same on the Host as in the repo.

Every volume carries an explicit `name:`. A volume that holds data is `external: true`: Compose never creates or removes it, so a `docker compose down -v`, or a Stack folder deleted by mistake, cannot take the data with it. A volume holding only what its Service regenerates (a cache, transcode scratch, a live-TV buffer, generated service discovery) may be owned by Compose, and says so with the volume label `homeops.regenerable: "<what regenerates it>"`. A Stack that mounts another Stack's volume (monitoring's Alloy reading edge's Traefik log) declares it `external` under the owner's name.

Container names are global on the Host, while Service names are scoped to their Stack. Datastores and their exporters are named after their engine (`mariadb`, `valkey`, `mariadb-exporter`, `valkey-exporter`), so their containers are named `<stack>-<service>` (`nextcloud-mariadb`, `vaultwarden-mariadb-exporter`). App Services keep `container_name` equal to their Service name.

Docker cannot rename a volume. A rename stops the Stack, takes a Rollback Snapshot ([ADR 0011](0011-restic-is-the-only-backup.md)), creates the new external volume, and moves its contents across with `hops volume move`. One container mounts Docker's volumes directory once, so `rename(2)` moves each entry within one filesystem: the rename is instant and keeps ownership, even for the 322 GB Nextcloud volume. The PR changing `name:` then Deploys. `validate.py` enforces the naming and the external rule, and carries the old names on an allowlist that only shrinks as each Stack is moved.

## Considered Options

- **Keep the old prefixes** (ADR 0004's position): rejected; every volume lookup needs the history of the split, and the prefixes no longer group anything.
- **Copy into new volumes**: rejected; it doubles space for the largest volumes (322, 94 and 64 GB) and takes hours, where a move within one filesystem takes seconds.
- **Every volume external, caches included**: rejected; a Deploy could no longer create a cache, and a regenerable volume gains nothing from protection against deletion.
