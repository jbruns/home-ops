# Runbook: Library isolation cutover (#543)

This runbook is the one maintenance window that moves the media Stacks from one whole-dataset
mount to the per-Library mounts of [ADR 0009](../adr/0009-library-isolation-by-role.md). The
design, the experiments behind it and the reason for each step are in the
[0552 spike](../research/0552-unix-identities.md); this file is the order of work and the exact
commands. Vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md).

> [!WARNING]
> **No media Service may run with its old mounts after the ACL reset (step 3).** Today's named-user
> ACL denies are the only thing keeping Sonarr and Radarr, which mount the whole media dataset rw,
> out of the other Libraries. From step 3 until the #543 PR is merged and deployed, doco-cd and
> every media Stack stay stopped. If the window has to stop part-way, [roll back](#rollback)
> instead of starting anything.

## Placeholders

The repo is public, so real names stay out of it. Substitute these when running the commands:

| Placeholder | Meaning |
|---|---|
| `$MEDIA_ROOT` | The media dataset's mountpoint, `${ZFS_POOL_ROOT_HDD}/media` |
| `<pool>/<media-dataset>` | The media dataset's ZFS name |
| `<pool>` | Its pool |
| `<backup-dataset>` | A mounted path **outside** the media dataset, for the backups and logs |
| `<owner-smb-user>` | The person's TrueNAS account that drops files over SMB |

## How to run Host commands

Root commands run on the Host through a privileged helper container that enters the Host's
namespaces. This gives a root shell with the Host's GNU tools (`getfacl`, `setfacl`, `find`, `zfs`,
`docker`). Define it once in the admin shell on the Host:

```sh
MEDIA_ROOT=…                               # substitute the media dataset's mountpoint
export M="$MEDIA_ROOT"
export B=<backup-dataset>/543               # substitute; created in step 2
hostsh() {
  sudo -n docker run --rm -i --label startup-order=skip --privileged --pid=host \
    -e M="$M" -e B="$B" alpine:3 nsenter -t 1 -m -u -n -i sh -s
}
```

Each block below is `hostsh <<'EOF' … EOF`. It runs in a fresh shell, which gets `M` and `B` from
the environment. `--label startup-order=skip` keeps the helper out of the startup-order tooling
([host/README.md](../../host/README.md)). Blocks that only need Docker run as plain
`sudo -n docker …`.

## Before the window

Do these in the days before; none of them changes storage.

1. **Sonarr: turn Use Hardlinks off.** Settings → Media Management → Importing → *Use Hardlinks
   instead of Copy* = **off**. Radarr already has it off; check it too. Once Sonarr has a single TV
   tree mount, a hardlink from `torrents/` would succeed and leave qBittorrent a link to the
   Library file.
2. **Check the other import settings**, which must stay as they are:
   - Sonarr and Radarr: Settings → Media Management → Permissions → *Set Permissions* = **off**.
   - SABnzbd: Config → Folders → *Permissions for completed downloads* = **`2770`**.
   - qBittorrent: no `UMASK` in its environment (the compose sets none).
3. **Drain the download queues** on the day: pause SABnzbd once in-progress post-processing has
   finished, and wait until Sonarr's, Radarr's and Shelfmark's queues are empty or show only
   pending items.
4. **qBittorrent:** confirm no torrent uses the `cwd-books` or `cwd-audiobooks` category. Those
   folders move, and a torrent in them would lose its files. If one exists, let Shelfmark finish,
   then remove the torrent (keeping its files) before the window.
5. **Keep the PR open.** Don't merge it before step 9.
6. **Silence alerts** in Alertmanager for the window plus a margin (for example 6h), with two
   silences:
   - `stack=~"plex|emby|sonarr|radarr|sabnzbd|qbittorrent|autobrr|shelfmark|audiobookshelf|calibre-web-automated|navidrome|nextpvr|ownfoil|doco-cd"`
     covers `ContainerDown`, `ContainerRestarting`, `TargetDown`, `ArrQueueDown`,
     `SabnzbdStatusDown`, `DeployFailing` and the other Stack-labelled alerts.
   - `alertname=~"ProbeFailed|DependencyDown|Http5xxRatio"` covers the Probes, which are labelled
     by `container` and not always by `stack`.

   Use the Alertmanager UI, or `amtool` in the container (it may need the basic-auth user from
   `tn/monitoring/alertmanager/web.sops.yml` through `--http.config.file`):

   ```sh
   sudo -n docker exec alertmanager amtool silence add --alertmanager.url=http://127.0.0.1:9093 \
     --duration=6h --comment='#543 window' 'stack=~"<the Stacks above>"'
   ```

   `ContainerDown` looks back over one day, so it keeps firing for about 25h after a container is
   removed or renamed. This window removes and renames none, so the alerts clear once the
   containers are back. If one lingers, extend the silence instead of chasing it.
7. **Pick `<backup-dataset>`** with room for an ACL dump of the media dataset (one text line per
   ACL entry, tens of MB for a large tree) and copies of four app configs.

## The window

Run the steps in order, and note the time each one finishes.

### 1. Stop doco-cd and every media Stack

Stop doco-cd first so it can't redeploy anything, then every media Stack's containers, sidecars
included. Containers stopped with `docker stop` stay stopped across a Docker restart.

```sh
hostsh <<'EOF'
set -eu
docker stop $(docker ps -q --filter label=com.docker.compose.project=doco-cd --filter label=com.docker.compose.service=doco-cd)
for s in plex emby sonarr radarr sabnzbd qbittorrent autobrr shelfmark audiobookshelf \
         calibre-web-automated navidrome nextpvr ownfoil; do
  ids=$(docker ps -q --filter "label=com.docker.compose.project=$s")
  [ -z "$ids" ] || docker stop $ids
done
docker ps --format '{{.Names}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}' \
  | grep -E ' (doco-cd doco-cd|(plex|emby|sonarr|radarr|sabnzbd|qbittorrent|autobrr|shelfmark|audiobookshelf|calibre-web-automated|navidrome|nextpvr|ownfoil) .*)$' \
  || echo "all stopped"
EOF
```

Expect `all stopped`. doco-cd's apprise sidecar may keep running.

### 2. Back up

The ZFS snapshot covers the whole media dataset. The ACL dump allows an ACL-only rollback. The
volume dumps record the ownership of every volume this window re-UIDs.

```sh
hostsh <<'EOF'
set -eu
mkdir -p "$B"
zfs snapshot <pool>/<media-dataset>@pre-543
getfacl -R -p -n "$M" > "$B/media-acl.txt"
for v in asot-media_cwa-downloader_config asot-media_audiobookshelf_config \
         asot-media_audiobookshelf_metadata media_nextpvr_config media_nextpvr_buffer \
         asot-media_ownfoil_config media_autobrr_config; do
  getfacl -R -p -n "$(docker volume inspect -f '{{.Mountpoint}}' "$v")" > "$B/volume-$v.acl.txt"
done
# Ownfoil's ROM folder is outside the media dataset, and changes owner too.
docker inspect ownfoil -f '{{range .Mounts}}{{if eq .Destination "/games"}}{{.Source}}{{end}}{{end}}' > "$B/ownfoil-games.path"
getfacl -R -p -n "$(cat "$B/ownfoil-games.path")" > "$B/ownfoil-games.acl.txt"
ls -la "$B"; zfs list -t snapshot -o name,creation | grep '@pre-543'
EOF
```

Then copy the app configs that step 7 edits. The apps are stopped, so the files are consistent:

```sh
for v in media_sonarr_config media_radarr_config media_sabnzbd_config media_qbittorrent_config; do
  sudo -n docker run --rm --label startup-order=skip -v "$v":/c:ro -v "$B":/b alpine:3 \
    sh -c "mkdir -p /b/$v && cp -a /c/. /b/$v/"
done
```

If the config volumes are large, copying only `sonarr.db*`, `radarr.db*`, `sabnzbd.ini` and
`qBittorrent/categories.json` is enough.

### 3. Reset the media dataset's ACLs

This is the [ACL reset procedure](../research/0552-unix-identities.md#acl-reset-procedure). Every
hand-made ACL goes. Folders become `2770` with the UID-free default ACL, files `0660`. The dataset
root (`1001:1002 2550`) is left alone (`-mindepth 1`).

First the inventory and a dry run, which change nothing:

```sh
hostsh <<'EOF'
getfacl -R -s -p -n "$M" | grep -c '^# file:'              # inodes with extended ACLs
getfacl -R -p -n "$M" | grep -cE '^(default:)?user:[^:]'   # named-user entries
setfacl --test -R -b "$M/tv" | head
# At 0660, a file whose group isn't media is unreadable to every Service: list them.
find "$M" -mindepth 1 ! -group 1002 -printf '%g %p\n' > "$B/not-media-group.txt"
wc -l < "$B/not-media-group.txt"
EOF
```

Expect a few dozen entries in `not-media-group.txt`: mostly under `music/` (which Navidrome reads),
plus `music_videos/` and the two `root:root` download folders that step 6 removes. Then reset:

```sh
hostsh <<'EOF'
set -eu
setfacl -R -b "$M"
find "$M" -mindepth 1 ! -group 1002 -exec chgrp -h 1002 {} +
find "$M" -mindepth 1 -type d -exec chmod 2770 {} +
find "$M" -type f -exec chmod 0660 {} +
find "$M" -mindepth 1 -type d -exec setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- {} +
echo "named entries left: $(getfacl -R -p -n "$M" | grep -cE '^(default:)?user:[^:]' || true)"   # expect 0
find "$M" -mindepth 1 -type d ! -perm 2770 | head                                                # expect nothing
find "$M" -mindepth 1 ! -group 1002 | head                                                       # expect nothing
EOF
```

`setfacl -R -b` keeps the old mask as the group bits, so nothing widens between the commands. This
step changes modes, groups and ACLs only; owners stay (decision 8 in the spike).

### 4. Identities

In the TrueNAS UI (Credentials → Users and Groups), together with the identities runbook in
[host/README.md](../../host/README.md#runbook-create-users-and-groups):

1. Rename the user `books` (4008) to **`cwa`**. The runbook matches by name, so this must come
   first; otherwise `cwa` fails with "uid 4008 taken by books".
2. Copy `host/create_usersgroups.sh` and `host/identities.tsv` from the PR branch to the Host. Run
   the script with `--dry-run` and check that it only creates `shelfmark` 4009, `audiobookshelf`
   4010, `nextpvr` 4011, `ownfoil` 4012 and `autobrr` 4013. Then run it without `--dry-run`.
3. For each of `sonarr`, `radarr`, `sabnzbd`, `navidrome`, `plex`, `emby`, `qbittor` and `cwa`, set
   the **primary group** to `media` and remove every auxiliary group.
4. Delete the groups with GIDs **4001–4008** (`sonarr`, `radarr`, `sabnzbd`, `navidrome`, `plex`,
   `emby`, `qbittor`, `books`), which are now unused.
5. User `media` (1001): turn **SMB access off**. Keep the user, which owns the dataset root,
   `music` and `ebooks`. Don't `chown` its files.
6. Make sure `<owner-smb-user>` is a member of the `media` group. The SMB Ingest Folders rely on it.

Check:

```sh
hostsh <<'EOF'
for u in sonarr radarr sabnzbd navidrome plex emby qbittor cwa shelfmark audiobookshelf nextpvr ownfoil autobrr media; do
  printf '%-15s ' "$u"; id "$u"
done
getent group | awk -F: '$3 >= 4001 && $3 <= 4013'   # expect nothing
EOF
```

Every user shows `gid=1002(media)` and no other groups.

### 5. Re-own the re-UIDed Services' volumes

This changes the owner only, and only on files owned by the old UID. Groups stay.

```sh
hostsh <<'EOF'
set -eu
mp() { docker volume inspect -f '{{.Mountpoint}}' "$1"; }
chown -R --from=4008 4009 "$(mp asot-media_cwa-downloader_config)"      # Shelfmark
chown -R --from=4008 4010 "$(mp asot-media_audiobookshelf_config)"      # Audiobookshelf
chown -R --from=4008 4010 "$(mp asot-media_audiobookshelf_metadata)"
chown -R --from=568  4011 "$(mp media_nextpvr_config)"                  # NextPVR
chown -R --from=568  4011 "$(mp media_nextpvr_buffer)"
chown -R --from=568  4012 "$(mp asot-media_ownfoil_config)"             # Ownfoil
chown -R --from=568  4012 "$(cat "$B/ownfoil-games.path")"
chown -R --from=568  4013 "$(mp media_autobrr_config)"                  # autobrr
for v in asot-media_cwa-downloader_config asot-media_audiobookshelf_config asot-media_audiobookshelf_metadata \
         media_nextpvr_config media_nextpvr_buffer asot-media_ownfoil_config media_autobrr_config; do
  printf '%s: ' "$v"; find "$(mp "$v")" -printf '%U\n' | sort | uniq -c | tr '\n' ' '; echo
done
EOF
```

Expect each volume to show only its new UID. Calibre-Web-Automated keeps 4008, now named `cwa`.

### 6. Restructure the media folders

Each Library becomes one tree, and the download folders that served a Library move into it. Each
move is printed by `mv -v` into a log. New folders are created now, owned as in the spike's
[layout](../research/0552-unix-identities.md#per-library-tree), because the compose mounts refuse
to create a missing source.

Target layout under `$MEDIA_ROOT`:

| Folder | From | Owner | Mounted by |
|---|---|---|---|
| `tv/library/` | everything now in `tv/` | sonarr | Sonarr (tree); Plex and Emby `ro` |
| `tv/usenet/` | contents of `nzbdl/completed/Series/` | sabnzbd | SABnzbd; Sonarr (tree) |
| `tv/torrents/` | new | qbittor | qBittorrent; Sonarr (tree) |
| `tv/ingest/` | new | sonarr | Sonarr (tree); SMB `tv-ingest` |
| `movies/library/` | everything now in `movies/` | radarr | Radarr (tree); Plex and Emby `ro` |
| `movies/usenet/` | contents of `nzbdl/completed/Movies/` | sabnzbd | SABnzbd; Radarr (tree) |
| `movies/torrents/` | new | qbittor | qBittorrent; Radarr (tree) |
| `movies/ingest/` | new | radarr | Radarr (tree); SMB `movies-ingest` |
| `audiobooks/library/` | everything now in `audiobooks/` | shelfmark | Shelfmark (tree); Audiobookshelf `ro` |
| `audiobooks/usenet/` | new (`nzbdl/completed/Audiobooks/` is empty) | sabnzbd | SABnzbd; Shelfmark (tree) |
| `audiobooks/torrents/` | new (`tordl/cwd-audiobooks/` is empty) | qbittor | qBittorrent; Shelfmark (tree) |
| `books/library/` | everything now in `books/` (the calibre library) | cwa | CWA |
| `books/usenet/` | contents of `nzbdl/completed/Books/` | sabnzbd | SABnzbd; Shelfmark |
| `books/torrents/` | new (`tordl/cwd-books/` is empty) | qbittor | qBittorrent; Shelfmark |
| `books/ingest/` | `cwa-book-ingest/`, renamed | cwa | CWA; Shelfmark; SMB `books-ingest` |

`nzbdl/` (SABnzbd's incomplete folder and the categories with no Organizer) and `tordl/` (torrents
for no Library) stay, as do `podcasts/`, `music/`, `ebooks/`, `dronescan/`, `music_videos/` and the
three recordings folders.

First check that nothing is in the way. Every line must say `ok`:

```sh
hostsh <<'EOF'
for p in tv/library tv/usenet tv/torrents tv/ingest movies/library movies/usenet movies/torrents \
         movies/ingest audiobooks/library audiobooks/usenet audiobooks/torrents books/library \
         books/usenet books/torrents books/ingest; do
  [ -e "$M/$p" ] && echo "IN THE WAY: $p" || echo "ok: $p"
done
for d in tordl/cwd-books tordl/cwd-audiobooks nzbdl/completed/Audiobooks; do
  [ -z "$(ls -A "$M/$d" 2>/dev/null)" ] && echo "ok: $d is empty" || echo "NOT EMPTY: $d"
done
for d in Series Movies Books; do
  printf 'ok: nzbdl/completed/%s has %s entries\n' "$d" "$(ls -A "$M/nzbdl/completed/$d" 2>/dev/null | wc -l)"
done
EOF
```

If anything is `IN THE WAY` or `NOT EMPTY`, stop and resolve it by hand, or roll back. Then move:

```sh
hostsh <<'EOF'
set -eu
LOG="$B/moves.log"
mk() {
  install -d -o "$2" -g media -m 2770 "$M/$1"
  setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- "$M/$1"
  echo "created '$M/$1' ($2)" >> "$LOG"
}
# lib <library> <organizer>: create library/ and move the tree's current contents into it.
lib() {
  mk "$1/library" "$2"
  find "$M/$1" -mindepth 1 -maxdepth 1 ! -name library -exec mv -v -t "$M/$1/library" -- {} + >> "$LOG"
}
lib tv sonarr
lib movies radarr
lib audiobooks shelfmark
lib books cwa
chown -v shelfmark "$M/audiobooks" >> "$LOG"    # the tree's top folder belongs to its Organizer

# Usenet: finished jobs not yet imported move with their category.
mk tv/usenet sabnzbd
find "$M/nzbdl/completed/Series" -mindepth 1 -maxdepth 1 -exec mv -v -t "$M/tv/usenet" -- {} + >> "$LOG"
mk movies/usenet sabnzbd
find "$M/nzbdl/completed/Movies" -mindepth 1 -maxdepth 1 -exec mv -v -t "$M/movies/usenet" -- {} + >> "$LOG"
mk books/usenet sabnzbd
find "$M/nzbdl/completed/Books" -mindepth 1 -maxdepth 1 -exec mv -v -t "$M/books/usenet" -- {} + >> "$LOG"
mk audiobooks/usenet sabnzbd
rmdir -v "$M/nzbdl/completed/Series" "$M/nzbdl/completed/Movies" "$M/nzbdl/completed/Books" \
         "$M/nzbdl/completed/Audiobooks" >> "$LOG"

# Torrents: Shelfmark's two categories were empty (checked above); new folders replace them.
mk tv/torrents qbittor
mk movies/torrents qbittor
mk audiobooks/torrents qbittor
mk books/torrents qbittor
rmdir -v "$M/tordl/cwd-books" "$M/tordl/cwd-audiobooks" >> "$LOG"

# Ingest Folders.
mk tv/ingest sonarr
mk movies/ingest radarr
mv -v -T "$M/cwa-book-ingest" "$M/books/ingest" >> "$LOG"

wc -l "$LOG"
for t in tv movies audiobooks books; do stat -c '%U:%G %a %n' "$M/$t" "$M/$t"/*; done
getfacl -p -n "$M/tv/usenet" | grep default
EOF
```

Expect every folder in the listing to be `<owner>:media 2770`, and the default ACL
`default:user::rwx default:group::rwx default:mask::rwx default:other::---`.

### 7. Change the in-app paths

The container paths of the Libraries change for their Organizers and Downloaders. Media Servers and
CWA keep theirs (`/data/tv`, `/data/movies`, `/audiobooks`, `/calibre-library`, `/cwa-book-ingest`).
These settings live in the apps' databases and config files. They change now, while the apps are
stopped, so that no app starts against a missing path.

**Sonarr** (`/data/tv/…` → `/data/tv/library/…`). Sonarr removes the episode-file records of a
series whose folder it can't find, so the series paths must be right before it starts. The columns
below are the path columns of the live Sonarr 4.0 schema. The last query lists them again, in case
an update changed the schema.

```sh
sudo -n docker run --rm -i --label startup-order=skip -v media_sonarr_config:/c alpine:3 sh -s <<'EOF'
set -eu
apk add -q sqlite
cd /c
sqlite3 sonarr.db <<'SQL'
PRAGMA integrity_check;
BEGIN;
UPDATE Series      SET Path           = '/data/tv/library' || substr(Path, 9)           WHERE Path = '/data/tv' OR Path LIKE '/data/tv/%';
UPDATE RootFolders SET Path           = '/data/tv/library' || substr(Path, 9)           WHERE Path = '/data/tv' OR Path LIKE '/data/tv/%';
UPDATE ImportLists SET RootFolderPath = '/data/tv/library' || substr(RootFolderPath, 9) WHERE RootFolderPath = '/data/tv' OR RootFolderPath LIKE '/data/tv/%';
COMMIT;
SELECT 'series not under library: ' || count(*) FROM Series WHERE Path NOT LIKE '/data/tv/library/%';
SELECT 'root folder: ' || Path FROM RootFolders;
SELECT 'import list: ' || RootFolderPath FROM ImportLists;
SELECT m.name || '.' || p.name FROM sqlite_master m JOIN pragma_table_info(m.name) p
  WHERE m.type = 'table' AND p.type = 'TEXT' AND (p.name LIKE '%Path%' OR p.name LIKE '%Folder%');
SQL
chown 4001:1002 sonarr.db*
EOF
```

Run it once only. Expect `series not under library: 0`, the root folder `/data/tv/library/` and the
import list `/data/tv/library`. `EpisodeFiles.RelativePath` and the other `RelativePath` columns are
relative to the series folder and don't change. `History`, `Blocklist` and `OriginalFilePath` keep
the old paths as history.

**Radarr** (`/data/movies/…` → `/data/movies/library/…`), with the live Radarr 6 schema:

```sh
sudo -n docker run --rm -i --label startup-order=skip -v media_radarr_config:/c alpine:3 sh -s <<'EOF'
set -eu
apk add -q sqlite
cd /c
sqlite3 radarr.db <<'SQL'
PRAGMA integrity_check;
BEGIN;
UPDATE Movies      SET Path           = '/data/movies/library' || substr(Path, 13)           WHERE Path = '/data/movies' OR Path LIKE '/data/movies/%';
UPDATE RootFolders SET Path           = '/data/movies/library' || substr(Path, 13)           WHERE Path = '/data/movies' OR Path LIKE '/data/movies/%';
UPDATE ImportLists SET RootFolderPath = '/data/movies/library' || substr(RootFolderPath, 13) WHERE RootFolderPath = '/data/movies' OR RootFolderPath LIKE '/data/movies/%';
UPDATE Collections SET RootFolderPath = '/data/movies/library' || substr(RootFolderPath, 13) WHERE RootFolderPath = '/data/movies' OR RootFolderPath LIKE '/data/movies/%';
COMMIT;
SELECT 'movies not under library: ' || count(*) FROM Movies WHERE Path NOT LIKE '/data/movies/library/%';
SELECT 'root folder: ' || Path FROM RootFolders;
SELECT 'import lists: ' || group_concat(DISTINCT RootFolderPath) FROM ImportLists;
SELECT 'collections: ' || group_concat(DISTINCT RootFolderPath) FROM Collections;
SELECT m.name || '.' || p.name FROM sqlite_master m JOIN pragma_table_info(m.name) p
  WHERE m.type = 'table' AND p.type = 'TEXT' AND (p.name LIKE '%Path%' OR p.name LIKE '%Folder%');
SQL
chown 4002:1002 radarr.db*
EOF
```

Run it once only. Expect `movies not under library: 0` and every other path under
`/data/movies/library`.

Neither app has Remote Path Mappings, and none are needed: SABnzbd and qBittorrent see each
Library's download folder at the same container path as its Organizer does.

**SABnzbd** categories. The Library ones get absolute paths; `ondemand`, `prowlarr` and `*` stay in
`/data/nzbdl/completed`.

| Category | `dir` before | `dir` after |
|---|---|---|
| series | `Series` | `/data/tv/usenet` |
| movies | `Movies` | `/data/movies/usenet` |
| books | `Books` | `/data/books/usenet` |
| audiobooks | `Audiobooks` | `/data/audiobooks/usenet` |

```sh
sudo -n docker run --rm -i --label startup-order=skip --user 4003:1002 -v media_sabnzbd_config:/c alpine:3 sh -s <<'EOF'
set -eu
cd /c
awk '
  /^\[categories\]/ { incat = 1; print; next }
  /^\[[^[]/         { incat = 0 }
  incat && /^\[\[/  { cat = $0; gsub(/[][ ]/, "", cat) }
  incat && /^dir = / {
    if (cat == "series")     $0 = "dir = /data/tv/usenet"
    if (cat == "movies")     $0 = "dir = /data/movies/usenet"
    if (cat == "books")      $0 = "dir = /data/books/usenet"
    if (cat == "audiobooks") $0 = "dir = /data/audiobooks/usenet"
  }
  { print }' sabnzbd.ini > sabnzbd.ini.new
diff sabnzbd.ini sabnzbd.ini.new || true
cat sabnzbd.ini.new > sabnzbd.ini && rm sabnzbd.ini.new
EOF
```

Expect the diff to show exactly the four `dir` lines. Keep *Permissions for completed downloads* at
`2770`.

**qBittorrent** categories:

| Category | Save path before | Save path after |
|---|---|---|
| cwd-books | `/data/tordl/cwd-books` | `/data/books/torrents` |
| cwd-audiobooks | `/data/tordl/cwd-audiobooks` | `/data/audiobooks/torrents` |

```sh
sudo -n docker run --rm -i --label startup-order=skip --user 4007:1002 -v media_qbittorrent_config:/c alpine:3 sh -s <<'EOF'
set -eu
f=/c/qBittorrent/categories.json
sed -e 's|"/data/tordl/cwd-books"|"/data/books/torrents"|' \
    -e 's|"/data/tordl/cwd-audiobooks"|"/data/audiobooks/torrents"|' "$f" > "$f.new"
diff "$f" "$f.new" || true
cat "$f.new" > "$f" && rm "$f.new"
EOF
```

**Shelfmark:** nothing to edit. The compose sets `DESTINATION_AUDIOBOOK=/data/audiobooks/library`,
`HARDLINK_TORRENTS_AUDIOBOOK=false` and `HARDLINK_TORRENTS=false`, and environment values override
and lock the settings. Its download paths come from SABnzbd and qBittorrent, which now report
`/data/{books,audiobooks}/{usenet,torrents}`, the same paths Shelfmark mounts.

**Unchanged:** Plex and Emby library and recording paths; Audiobookshelf (`/audiobooks`,
`/podcasts`); Calibre-Web-Automated (`/calibre-library`, `/cwa-book-ingest`); Navidrome; autobrr's
action save path (`/data/tordl/…`).

### 8. Last check before the merge

Every media folder a compose file mounts must exist now, or that Stack's Deploy fails: the mounts
set `create_host_path: false`.

```sh
hostsh <<'EOF'
for p in tv movies audiobooks tv/library movies/library audiobooks/library books/library books/ingest \
         tv/usenet movies/usenet books/usenet audiobooks/usenet tv/torrents movies/torrents \
         books/torrents audiobooks/torrents nzbdl tordl podcasts plex_recordings emby_recordings \
         nextpvr_recordings; do
  [ -d "$M/$p" ] && echo "ok: $p" || echo "MISSING: $p"
done
EOF
```

### 9. Merge the PR

doco-cd is stopped, so the merge deploys nothing yet. Merge the #543 PR on GitHub, then confirm on
`main` that no media Service keeps a mount of the whole dataset:

```sh
git fetch origin main && git checkout --detach origin/main
grep -rnE 'ZFS_POOL_ROOT_HDD\}/media(:|$)' tn/   # expect nothing
grep -rn 'BOOKS_UID' tn/                         # expect nothing
```

### 10. Start doco-cd, then everything it doesn't start

```sh
hostsh <<'EOF'
docker start $(docker ps -aq --filter label=com.docker.compose.project=doco-cd --filter label=com.docker.compose.service=doco-cd)
EOF
```

Within one poll (about 2 minutes), doco-cd redeploys the Stacks the PR changed: Plex, Emby, Sonarr,
Radarr, SABnzbd, qBittorrent, autobrr, Shelfmark, Audiobookshelf, Calibre-Web-Automated, NextPVR
and Ownfoil. Watch its log for a failed Deploy (a `bind source path does not exist` error means a
folder from step 8 is missing). It doesn't touch unchanged Stacks, so then start every media
container that is still stopped, Navidrome at least:

```sh
hostsh <<'EOF'
for s in plex emby sonarr radarr sabnzbd qbittorrent autobrr shelfmark audiobookshelf \
         calibre-web-automated navidrome nextpvr ownfoil; do
  ids=$(docker ps -aq --filter "label=com.docker.compose.project=$s" --filter status=exited)
  [ -z "$ids" ] || docker start $ids
done
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'plex|emby|sonarr|radarr|sabnzbd|qbittorrent|autobrr|shelfmark|audiobookshelf|calibre|navidrome|nextpvr|ownfoil'
EOF
```

Then resume SABnzbd.

## Verify

**Identity and mounts.** Each Service runs as its own UID with group `media`, and the Media Servers'
Library mounts are read-only:

```sh
for c in sonarr:4001 radarr:4002 sabnzbd:4003 navidrome:4004 plex:4005 emby:4006 qbittorrent:4007 \
         calibre-web-automated:4008 shelfmark:4009 audiobookshelf:4010 nextpvr:4011 ownfoil:4012 autobrr:4013; do
  n=${c%:*}; printf '%-22s want %-5s ' "$n" "${c#*:}"
  sudo -n docker inspect "$n" -f '{{.Config.User}} {{range .Mounts}}{{.Destination}}{{if not .RW}}(ro){{end}} {{end}}'
done
```

Calibre-Web-Automated starts as root and drops to `PUID`, so its `User` is empty; check
`sudo -n docker top calibre-web-automated` shows 4008 instead.

**Negative checks.** Every line must say `ok, denied` (`No such file or directory`, or `Read-only
file system` for the Media Servers):

```sh
x() { sudo -n docker exec "$1" sh -c "$2" >/dev/null 2>&1 && echo "!! ALLOWED: $1: $2" || echo "ok, denied: $1: $2"; }
x sabnzbd        'ls /data/tv/library'
x sabnzbd        'ls /data/movies/library'
x sabnzbd        'ls /data/books/library'
x qbittorrent    'ls /data/tv/library'
x qbittorrent    'ls /data/audiobooks/library'
x sonarr         'ls /data/movies'
x sonarr         'ls /data/nzbdl'
x radarr         'ls /data/tv'
x shelfmark      'ls /data/books/library'
x shelfmark      'ls /calibre-library'
x shelfmark      'ls /data/tv'
x calibre-web-automated 'ls /data/tv'
x audiobookshelf 'ls /data/tv'
x plex           'touch /data/tv/.543-test'
x plex           'touch /data/movies/.543-test'
x emby           'touch /data/tv/.543-test'
x emby           'touch /data/movies/.543-test'
x audiobookshelf 'touch /audiobooks/.543-test'
x navidrome      'touch /music/.543-test'
```

**Positive checks.** Each Organizer writes in its own tree, each Downloader in its own folders:

```sh
y() { sudo -n docker exec "$1" sh -c "touch '$2/.543-test' && rm '$2/.543-test'" && echo "ok: $1 writes $2" || echo "!! DENIED: $1: $2"; }
y sonarr /data/tv/library; y sonarr /data/tv/ingest
y radarr /data/movies/library
y sabnzbd /data/tv/usenet; y sabnzbd /data/books/usenet; y sabnzbd /data/nzbdl
y qbittorrent /data/audiobooks/torrents; y qbittorrent /data/tordl
y shelfmark /data/audiobooks/library; y shelfmark /cwa-book-ingest
y calibre-web-automated /calibre-library
y audiobookshelf /podcasts
y plex /recordings; y emby /recordings; y nextpvr /recordings
```

**Usenet handoff.** As SABnzbd, create a file in `usenet/`; as the Organizer, move it into the
Library. The move is a rename, so the inode stays:

```sh
sudo -n docker exec sabnzbd sh -c 'echo t > /data/tv/usenet/.543-handoff'
sudo -n docker exec sonarr sh -c 'mv /data/tv/usenet/.543-handoff /data/tv/library/ && stat -c "%i %h %u:%g %a" /data/tv/library/.543-handoff && rm /data/tv/library/.543-handoff'
```

Expect owner 4003 (sabnzbd), group 1002 (media), mode `660`.

**Real imports.** Grab one small TV episode and one movie through Sonarr and Radarr, and one
audiobook by torrent through Shelfmark. Before and after the audiobook:

```sh
hostsh <<'EOF'
zpool get -H -o value bclonesaved <pool>
# Seeding file and Library file: different inodes, each with link count 1.
find "$M/audiobooks/torrents" "$M/audiobooks/library" -type f -newermt '1 hour ago' -printf '%i %n %p\n'
EOF
```

The seeding file and the Library file must have different inodes, each with link count 1.
`bclonesaved` should grow by about the audiobook's size. If it doesn't, the copy was a full copy:
the import still works, but each torrent import costs its size in space until the torrent is
removed. Record it in #543.

**File modes.** `getfacl` on a fresh import shows `mask::rw-` and group `media`:

```sh
hostsh <<'EOF'
f=$(find "$M/tv/library" -type f -newermt '1 hour ago' | head -1); [ -n "$f" ] && getfacl -p -n "$f"
EOF
```

**Media Servers.** Before any scan, open Plex and Emby and check that TV Shows and Movies show their
items. Don't empty the trash until they do. Audiobookshelf shows its Audiobooks, Navidrome its
Music.

**Settings.** In Shelfmark, the audiobook destination shows `/data/audiobooks/library` and both
*hardlink torrents* settings show off and locked. Sonarr's and Radarr's Root Folders show
`/data/tv/library` and `/data/movies/library` with free space, and System → Status shows no
missing-root-folder warning. SABnzbd's categories show the new folders.

## SMB

After the Deploy, in the TrueNAS UI (Shares → SMB):

1. Add three shares. Each has the folder as its path and a share ACL that allows only
   `<owner-smb-user>`. Decline TrueNAS's prompt to configure the dataset ACL: the folders' POSIX ACLs
   are already right.

   | Share | Path |
   |---|---|
   | `tv-ingest` | `$MEDIA_ROOT/tv/ingest` |
   | `movies-ingest` | `$MEDIA_ROOT/movies/ingest` |
   | `books-ingest` | `$MEDIA_ROOT/books/ingest` |

2. Remove the `media` share, which exposes the whole dataset. Don't add a Music share: Music has no
   Organizer and takes no new files until it gets one.
3. User `media` (1001) already has SMB off (step 4).
4. From a client, as `<owner-smb-user>`, copy a small file into `tv-ingest`. If the write is denied,
   Samba isn't honouring the `media` group while the group has `smb=false`. In that case, turn
   **SMB group** on for `media` (Credentials → Groups → media → Edit), then test again. Afterwards,
   Manual Import (mode Move) the file in Sonarr from `/data/tv/ingest`, or delete it.
5. A file dropped into `books-ingest` is picked up by CWA automatically.

`dronescan`, `music_videos` and `ebooks` were only reachable through the `media` share and have no
share afterwards.

## Finish

- Expire the Alertmanager silences once everything is green.
- Comment on #543 with the evidence: each step's time, `wc -l moves.log`, the verify output and
  `bclonesaved` before and after.
- Keep the `@pre-543` snapshot and `$B` for a few weeks. Then destroy the snapshot
  (`zfs destroy <pool>/<media-dataset>@pre-543`), which pins every block the window changed.

## Rollback

Roll back if the window fails part-way, or if the Deploy shows a problem that can't be fixed
forward. Everything stays stopped until the last step.

1. **Stop** doco-cd and every media Stack (step 1). If they ran after step 10, they may have
   downloaded new files, which `zfs rollback` would discard. In that case use route 3b.
2. **Revert the code.** If the PR was merged, revert its merge commit on `main` (the GitHub
   *Revert* button, or `git revert -m 1 <merge-sha>`). doco-cd is stopped, so nothing deploys yet.
3. **Restore the media dataset**, one of:
   - a. **Snapshot**, if nothing needs keeping from after step 2:
     `zfs rollback <pool>/<media-dataset>@pre-543` (inside `hostsh`). This reverts the moves,
     groups, modes and ACLs together. If it refuses because newer snapshots exist (periodic
     snapshot tasks), `-r` destroys those; check what they are first.
   - b. **Reverse the moves, then the ACLs**, to keep downloads written since:

     ```sh
     hostsh <<'EOF'
     set -eu
     back() { find "$M/$1" -mindepth 1 -maxdepth 1 -exec mv -v -t "$M/$2" -- {} +; }
     mv -v -T "$M/books/ingest" "$M/cwa-book-ingest"
     mkdir -p "$M/nzbdl/completed/Series" "$M/nzbdl/completed/Movies" "$M/nzbdl/completed/Books" \
              "$M/nzbdl/completed/Audiobooks" "$M/tordl/cwd-books" "$M/tordl/cwd-audiobooks"
     back tv/usenet nzbdl/completed/Series
     back movies/usenet nzbdl/completed/Movies
     back books/usenet nzbdl/completed/Books
     back audiobooks/usenet nzbdl/completed/Audiobooks
     back books/torrents tordl/cwd-books
     back audiobooks/torrents tordl/cwd-audiobooks
     for t in tv movies books audiobooks; do
       for d in usenet torrents ingest; do
         if [ -d "$M/$t/$d" ]; then rmdir "$M/$t/$d"; fi   # fails, and stops, if not empty
       done
       back "$t/library" "$t"
       rmdir "$M/$t/library"
     done
     cd / && setfacl --restore="$B/media-acl.txt"
     EOF
     ```

     A file someone dropped into `tv/ingest` or `movies/ingest` stops the script at `rmdir`: move
     it into the tree's top folder and rerun from there. `setfacl --restore` resets the owner,
     group, mode and ACL of every path in the backup, so it runs after the moves are undone. Files
     created since keep their default-ACL modes, which grant the `media` group access as before.
4. **Restore the volumes' owners:** inside `hostsh`, run `cd / && setfacl --restore=<file>` for
   each `$B/volume-*.acl.txt` and for `$B/ownfoil-games.acl.txt`.
5. **Restore the app configs** from `$B/media_{sonarr,radarr,sabnzbd,qbittorrent}_config/`: copy
   back `sonarr.db*`, `radarr.db*`, `sabnzbd.ini` and `qBittorrent/categories.json`, keeping their
   owners (`cp -a`).
6. **Start doco-cd** (step 10), which deploys the reverted compose; then start the remaining media
   containers, resume SABnzbd, and restore the `media` SMB share if it was removed.

The identity changes can stay. The new users own nothing once the volumes are restored, `cwa` keeps
UID 4008, the deleted groups owned nothing, and the old compose runs Sonarr, Radarr, SABnzbd,
Navidrome, Plex, Emby and qBittorrent with the same UIDs. Shelfmark, Audiobookshelf and CWA go back
to `BOOKS_UID` (4008, now named `cwa`), and NextPVR, Ownfoil and autobrr to 568. Turn SMB back on
for the user `media` only if something still logs in as it.
