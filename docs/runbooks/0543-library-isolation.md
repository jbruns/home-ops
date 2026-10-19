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
> instead of starting anything. After the merge, only doco-cd starts the changed Stacks, and step
> 10's gate removes any container still built from the old compose. Never `docker start` an
> existing container of a changed Stack.

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
MEDIA_ROOT=…                               # the media dataset's mountpoint, no trailing slash
export M="$MEDIA_ROOT"
export B=<backup-dataset>/543               # substitute; created in step 2
export SHA=                                 # set in step 9 (and in a rollback)
hostsh() {
  sudo -n docker run --rm -i --label startup-order=skip --privileged --pid=host \
    -e M="$M" -e B="$B" -e SHA="$SHA" alpine:3 nsenter -t 1 -m -u -n -i sh -s
}
```

Each block below is `hostsh <<'EOF' … EOF`. It runs in a fresh shell, which gets `M`, `B` and `SHA`
from the environment. `--label startup-order=skip` keeps the helper out of the startup-order tooling
([host/README.md](../../host/README.md)). Blocks that only need Docker run as plain
`sudo -n docker …`.

Don't reboot the Host or restart Docker during the window: keep every media container's state
exactly as the steps leave it.

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
3. **Drain gate, on the day.** The window changes where each Library's finished downloads live.
   SABnzbd's history keeps each finished job's old path, so a job that is finished but not yet
   imported can't be imported after the window. So nothing may be finished-but-unimported when
   step 1 starts:
   - Stop intake: pause SABnzbd's queue (jobs still queued finish into the new folders after the
     window), disable autobrr's filters, add no torrents by hand, and make no new Shelfmark
     requests.
   - Let running downloads and post-processing finish, and let Sonarr, Radarr and Shelfmark import
     everything finished.
   - Check that nothing is waiting. Sonarr's and Radarr's Activity → Queue show no item that is
     completed, importing, import pending, import blocked or failed; or by API:

     ```sh
     for a in sonarr:8989 radarr:7878; do n=${a%:*}
       sudo -n docker exec "$n" sh -c "k=\$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /config/config.xml);
         wget -qO- --header \"X-Api-Key: \$k\" 'http://127.0.0.1:${a#*:}/api/v3/queue?pageSize=500&includeUnknownSeriesItems=true&includeUnknownMovieItems=true'" \
         | grep -o '"trackedDownloadState":"[a-zA-Z]*"' | sort | uniq -c | sed "s/^/$n /"
     done
     ```

     Expect only `downloading` (paused in SABnzbd), or nothing at all.
   - Shelfmark's queue shows nothing completed and not yet processed.
   - SABnzbd's History: every job in `series`, `movies`, `books` or `audiobooks` was either
     imported (its Organizer removes it) or is deleted now.
   - qBittorrent: no torrent uses the `cwd-books` or `cwd-audiobooks` category. Those folders move,
     and a torrent in them would lose its files. If one exists, let Shelfmark finish, then remove
     the torrent (keeping its files).

   **If any check fails, postpone the window.** Step 6 moves no finished downloads: what is left in
   SABnzbd's completed category folders stays there for review.
4. **Dronescan.** `dronescan/` was the old TV and Movies drop point, reached through the `media`
   SMB share. The window retires it in favour of the `tv-ingest` and `movies-ingest` Ingest Folder
   shares: one shared drop folder would have to be mounted by both Sonarr and Radarr, which breaks
   isolation. Step 6 moves each top-level entry into `tv/ingest/` or `movies/ingest/`. Preview the
   classification now (read-only):

   ```sh
   hostsh <<'EOF'
   for e in "$M/dronescan"/* "$M/dronescan"/.[!.]*; do
     [ -e "$e" ] || continue
     if find "$e" -type f | grep -qE '[Ss][0-9]{1,2}[Ee][0-9]{1,3}'; then c=tv
     elif basename "$e" | grep -qE '(19|20)[0-9]{2}'; then c=movies
     else c=UNSURE; fi
     printf '%-7s %s\n' "$c" "$(basename "$e")"
   done
   EOF
   ```

   A name with an episode tag (`S01E02`) is TV; otherwise a year in its name makes it a movie.
   Correct a wrong guess, or classify an `UNSURE` entry, by writing `tv <name>` or `movies <name>`
   (or `skip <name>`, to leave it in `dronescan/`) as a line of `$B/dronescan-classes.txt`; those
   lines win over the guess. Nothing in the repo or in any app's settings refers to `dronescan/`
   (only Sonarr's and Radarr's import history mention it).
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
| `tv/usenet/` | new | sabnzbd | SABnzbd; Sonarr (tree) |
| `tv/ingest/` | new, plus the TV entries of `dronescan/` | sonarr | Sonarr (tree); SMB `tv-ingest` |
| `movies/library/` | everything now in `movies/` | radarr | Radarr (tree); Plex and Emby `ro` |
| `movies/usenet/` | new | sabnzbd | SABnzbd; Radarr (tree) |
| `movies/ingest/` | new, plus the movie entries of `dronescan/` | radarr | Radarr (tree); SMB `movies-ingest` |
| `audiobooks/library/` | everything now in `audiobooks/` | shelfmark | Shelfmark (tree); Audiobookshelf `ro` |
| `audiobooks/usenet/` | new | sabnzbd | SABnzbd; Shelfmark (tree) |
| `audiobooks/torrents/` | new (`tordl/cwd-audiobooks/` is empty) | qbittor | qBittorrent; Shelfmark (tree) |
| `books/library/` | everything now in `books/` (the calibre library) | cwa | CWA |
| `books/usenet/` | new | sabnzbd | SABnzbd; Shelfmark |
| `books/torrents/` | new (`tordl/cwd-books/` is empty) | qbittor | qBittorrent; Shelfmark |
| `books/ingest/` | `cwa-book-ingest/`, renamed | cwa | CWA; Shelfmark; SMB `books-ingest` |

`nzbdl/` (SABnzbd's incomplete folder and the categories with no Organizer) and `tordl/` (torrents
for no Library) stay, as do `podcasts/`, `music/`, `ebooks/`, `music_videos/` and the three
recordings folders. `dronescan/` is emptied into the two Ingest Folders and removed.

No finished download moves: after the drain gate, anything left in `nzbdl/completed/{Series,Movies,Books}`
is not tracked by any app. It stays in SABnzbd's own folder for review, and the empty category
folders are removed. TV and Movies get no `torrents/` folder until Sonarr or Radarr get a torrent
client (a follow-up); creating one then is a `mk` line plus a qBittorrent mount.

First check that nothing is in the way. Every line must say `ok`:

```sh
hostsh <<'EOF'
for p in tv/library tv/usenet tv/ingest movies/library movies/usenet \
         movies/ingest audiobooks/library audiobooks/usenet audiobooks/torrents books/library \
         books/usenet books/torrents books/ingest; do
  [ -e "$M/$p" ] && echo "IN THE WAY: $p" || echo "ok: $p"
done
for d in tordl/cwd-books tordl/cwd-audiobooks nzbdl/completed/Audiobooks; do
  [ -z "$(ls -A "$M/$d" 2>/dev/null)" ] && echo "ok: $d is empty" || echo "NOT EMPTY: $d"
done
for d in Series Movies Books; do
  printf 'ok: nzbdl/completed/%s has %s entries (left for review)\n' "$d" "$(ls -A "$M/nzbdl/completed/$d" 2>/dev/null | wc -l)"
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

# Usenet: new category folders. Leftovers in the old ones stay in nzbdl/ (drain gate).
mk tv/usenet sabnzbd
mk movies/usenet sabnzbd
mk books/usenet sabnzbd
mk audiobooks/usenet sabnzbd
for d in Series Movies Books Audiobooks; do
  rmdir -v "$M/nzbdl/completed/$d" >> "$LOG" 2>/dev/null || echo "left for review: nzbdl/completed/$d"
done

# Torrents: Shelfmark's two categories were empty (checked above); new folders replace them.
mk audiobooks/torrents qbittor
mk books/torrents qbittor
rmdir -v "$M/tordl/cwd-books" "$M/tordl/cwd-audiobooks" >> "$LOG"

# Ingest Folders.
mk tv/ingest sonarr
mk movies/ingest radarr
mv -v -T "$M/cwa-book-ingest" "$M/books/ingest" >> "$LOG"

# Dronescan: each top-level entry goes to an Ingest Folder; $B/dronescan.moves records each move
# (NUL-separated) for the rollback.
if [ -d "$M/dronescan" ]; then
  for e in "$M/dronescan"/* "$M/dronescan"/.[!.]*; do
    [ -e "$e" ] || continue
    n=$(basename "$e"); c=
    [ -f "$B/dronescan-classes.txt" ] && c=$(awk -v n="$n" 'substr($0, index($0, " ") + 1) == n { print $1 }' "$B/dronescan-classes.txt")
    if [ -z "$c" ]; then
      if find "$e" -type f | grep -qE '[Ss][0-9]{1,2}[Ee][0-9]{1,3}'; then c=tv
      elif printf '%s' "$n" | grep -qE '(19|20)[0-9]{2}'; then c=movies
      else c=UNSURE; fi
    fi
    case "$c" in
      tv|movies) mv -v -t "$M/$c/ingest" -- "$e" >> "$LOG"; printf '%s\0' "$c/ingest/$n" >> "$B/dronescan.moves" ;;
      *) echo "left in dronescan ($c): $n" ;;
    esac
  done
  rmdir -v "$M/dronescan" >> "$LOG" 2>/dev/null || echo "dronescan/ is not empty: see 'left in dronescan' above"
fi

wc -l "$LOG"
for t in tv movies audiobooks books; do stat -c '%U:%G %a %n' "$M/$t" "$M/$t"/*; done
ls -A "$M/tv/ingest" "$M/movies/ingest" | head -40
getfacl -p -n "$M/tv/usenet" | grep default
EOF
```

Expect every folder in the listing to be `<owner>:media 2770`, and the default ACL
`default:user::rwx default:group::rwx default:mask::rwx default:other::---`. Any `left in
dronescan` line is for the operator: classify it in `$B/dronescan-classes.txt` and rerun only the
dronescan part, or leave it (`dronescan/` then stays, with no share and no Service mounting it).
After the window, import the dronescan entries with Sonarr's and Radarr's Manual Import (mode Move)
from `/data/tv/ingest` and `/data/movies/ingest`, as for any SMB drop.

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
         tv/usenet movies/usenet books/usenet audiobooks/usenet books/torrents audiobooks/torrents \
         nzbdl tordl podcasts plex_recordings emby_recordings nextpvr_recordings; do
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
export SHA=$(git rev-parse HEAD)                 # the merge commit; step 10 checks for it
```

Merge nothing else to `main` until step 10 is done: the gate expects every changed Stack to carry
exactly this commit.

### 10. Start doco-cd and gate every changed Stack

Start only doco-cd:

```sh
hostsh <<'EOF'
docker start $(docker ps -aq --filter label=com.docker.compose.project=doco-cd --filter label=com.docker.compose.service=doco-cd)
EOF
```

Within one poll (about 2 minutes), doco-cd deploys the merge to the Stacks it changed: Plex, Emby,
Sonarr, Radarr, SABnzbd, qBittorrent, autobrr, Shelfmark, Audiobookshelf, Calibre-Web-Automated,
NextPVR and Ownfoil. Each Deploy recreates the Stack's containers from the new compose and starts
them. A Deploy that fails (a `bind source path does not exist` error means a folder from step 8 is
missing; an image pull can fail too) leaves that Stack's old, stopped containers behind, and doco-cd
retries it with a forced recreate.

An old container still has the whole-dataset mount and, after the ACL reset, nothing keeps it out of
the other Libraries. So **start nothing by hand.** Run the gate. It waits up to 15 minutes for every
container of every changed Stack to match the merged compose: the `cd.doco.deployment.target.sha`
label doco-cd sets is `$SHA`; the user is the Service's UID and `media` (Calibre-Web-Automated:
`PUID`); and its mounts under `$M` are exactly the table's, with no mount of `$M` itself. A
container not in the table (an exporter) must mount nothing under `$M`. When time runs out, the gate
removes every container of each Stack that still fails (`docker rm -f`), so nothing old can be
started again.

```sh
hostsh <<'EOF'
want='sonarr 4001:1002 tv:/data/tv:rw
radarr 4002:1002 movies:/data/movies:rw
sabnzbd 4003:1002 nzbdl:/data/nzbdl:rw tv/usenet:/data/tv/usenet:rw movies/usenet:/data/movies/usenet:rw books/usenet:/data/books/usenet:rw audiobooks/usenet:/data/audiobooks/usenet:rw
qbittorrent 4007:1002 tordl:/data/tordl:rw books/torrents:/data/books/torrents:rw audiobooks/torrents:/data/audiobooks/torrents:rw
plex 4005:1002 tv/library:/data/tv:ro movies/library:/data/movies:ro plex_recordings:/recordings:rw
emby 4006:1002 tv/library:/data/tv:ro movies/library:/data/movies:ro emby_recordings:/recordings:rw
shelfmark 4009:1002 audiobooks:/data/audiobooks:rw books/ingest:/cwa-book-ingest:rw books/usenet:/data/books/usenet:rw books/torrents:/data/books/torrents:rw
audiobookshelf 4010:1002 audiobooks/library:/audiobooks:ro podcasts:/podcasts:rw
calibre-web-automated 4008 books/ingest:/cwa-book-ingest:rw books/library:/calibre-library:rw
nextpvr 4011:1002 nextpvr_recordings:/recordings:rw
ownfoil 4012:1002
autobrr 4013:1002'
stacks='plex emby sonarr radarr sabnzbd qbittorrent autobrr shelfmark audiobookshelf calibre-web-automated nextpvr ownfoil'
[ -n "$SHA" ] || { echo "SHA is not set (step 9)"; exit 1; }
words() { tr ' ' '\n' | sed '/^$/d' | sort | tr '\n' ' '; }
gate() {   # prints one FAIL line per problem; returns 1 if there was one
  rc=0
  for s in $stacks; do
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$s")
    [ -n "$ids" ] || { echo "FAIL $s: no containers"; rc=1; continue; }
    for id in $ids; do
      n=$(docker inspect -f '{{.Name}}' "$id"); n=${n#/}
      w=$(printf '%s\n' "$want" | awk -v n="$n" '$1 == n')
      sha=$(docker inspect -f '{{index .Config.Labels "cd.doco.deployment.target.sha"}}' "$id")
      if [ "$n" = calibre-web-automated ]; then
        user=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$id" | sed -n 's/^PUID=//p')
      else
        user=$(docker inspect -f '{{.Config.User}}' "$id")
      fi
      mounts=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source .Destination .RW}}{{end}}{{end}}' "$id" |
        awk -v m="$M" '$1 == m { print "WHOLE-DATASET:" $2 ":" $3; next }
          index($1, m "/") == 1 { print substr($1, length(m) + 2) ":" $2 ":" ($3 == "true" ? "rw" : "ro") }' | words)
      if [ -n "$w" ]; then
        exp=$(printf '%s\n' "$w" | cut -d' ' -f3- | words)
        [ "$sha" = "$SHA" ] || { echo "FAIL $s/$n: deployed revision '$sha'"; rc=1; }
        [ "$user" = "$(printf '%s\n' "$w" | cut -d' ' -f2)" ] || { echo "FAIL $s/$n: user '$user'"; rc=1; }
        [ "$mounts" = "$exp" ] || { echo "FAIL $s/$n: mounts '$mounts'"; rc=1; }
      else
        [ -z "$mounts" ] || { echo "FAIL $s/$n: mounts '$mounts'"; rc=1; }
      fi
    done
  done
  return $rc
}
i=0
until out=$(gate); do
  i=$((i + 1))
  if [ $i -ge 30 ]; then
    printf '%s\n' "$out"
    for s in $(printf '%s\n' "$out" | awk '{ sub(/[\/:].*/, "", $2); print $2 }' | sort -u); do
      ids=$(docker ps -aq --filter "label=com.docker.compose.project=$s")
      [ -z "$ids" ] || { echo "REMOVING $s"; docker rm -f $ids; }
    done
    echo "GATE FAILED"; exit 1
  fi
  sleep 30
done
echo "GATE PASSED"
docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'plex|emby|sonarr|radarr|sabnzbd|qbittorrent|autobrr|shelfmark|audiobookshelf|calibre|nextpvr|ownfoil'
EOF
```

**If the gate passes,** every changed Stack runs the merged compose. Start Navidrome, the one media
Stack the PR doesn't change (doco-cd doesn't redeploy it), after checking it still has its own
narrow mount:

```sh
hostsh <<'EOF'
got=$(docker inspect -f '{{.Config.User}}{{range .Mounts}}{{if eq .Type "bind"}} {{.Source}}:{{.Destination}}:{{.RW}}{{end}}{{end}}' navidrome)
if [ "$got" = "4004:1002 $M/music/FLAC:/music:false" ]; then docker start navidrome
else echo "navidrome: unexpected config '$got', not started"; fi
EOF
```

Then resume SABnzbd's queue and re-enable autobrr's filters.

**If the gate fails,** the Stacks it names are removed; the others run. For each removed Stack, fix
the cause from doco-cd's log (a missing folder: create it as in step 6; a failed pull: retry), then
have doco-cd recreate it from the merged revision with its REST API ([README](../../README.md#how-changes-reach-the-host)),
once per Service of the Stack. `KEY` is the value of `tn/doco-cd/api_secret.sops.txt` (`sops -d`):

```sh
sudo -n docker exec prometheus wget -qO- --post-data '' --header "x-api-key: $KEY" \
  "http://doco-cd/v1/api/project/<stack>/recreate?service=<service>"
```

Then rerun the gate. Don't create the containers any other way. If a Stack can't be fixed, leave it
down (it has no containers to start by mistake) or [roll back](#rollback).

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

**SABnzbd handoff cost.** SABnzbd unpacks each job straight into its category's folder
(`/data/tv/usenet`), which is a different mount from `/data/nzbdl/incomplete`. Extracted files are
written there directly; only files the job didn't pack in an archive cross the mounts as a copy
instead of a rename. Measure it rather than assume it: after the first real TV grab, read the job's
History entry and record its *Unpack* and *Moving* (or *Post-processing*) times and size in #543.
If a typical job's moving time is large, revisit the mounts there.

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

`music_videos` and `ebooks` were only reachable through the `media` share and have no share
afterwards, as decided. `dronescan/` is gone (step 6): drop TV into `tv-ingest` and movies into
`movies-ingest` instead. A single shared drop folder would have to be mounted by both Sonarr and
Radarr, which breaks isolation.

## Finish

- Expire the Alertmanager silences once everything is green.
- Comment on #543 with the evidence: each step's time, `wc -l moves.log`, the verify output and
  `bclonesaved` before and after.
- Keep the `@pre-543` snapshot and `$B` for a few weeks. Then destroy the snapshot
  (`zfs destroy <pool>/<media-dataset>@pre-543`), which pins every block the window changed.

## Rollback

Roll back if the window fails part-way, or if the Deploy shows a problem that can't be fixed
forward. Everything stays stopped until the last step.

1. **Drain, then stop.** If the Stacks ran after step 10, first run the [drain gate](#before-the-window)
   again (item 3): stop intake and let Sonarr, Radarr and Shelfmark import everything finished, so
   that no finished-but-unimported job is left in a `usenet/` or `torrents/` folder whose path the
   old compose doesn't have. Then stop doco-cd and every media Stack (step 1). Stacks that ran
   after step 10 may have downloaded new files, which `zfs rollback` would discard; in that case
   use route 3b.
2. **Revert the code.** If the PR was merged, revert its merge commit on `main` (the GitHub
   *Revert* button, or `git revert -m 1 <merge-sha>`). doco-cd is stopped, so nothing deploys yet.
   Set `export SHA=<revert-sha>` (the new head of `main`) for step 6's gate.
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
     # Dronescan entries go back from the Ingest Folders; one already imported is reported.
     mkdir -p "$M/dronescan"
     if [ -f "$B/dronescan.moves" ]; then
       xargs -0 -n 1 sh -c 'if [ -e "$M/$1" ]; then mv -v -t "$M/dronescan" -- "$M/$1";
         else echo "not in ingest any more (imported?): $1"; fi' sh < "$B/dronescan.moves"
     fi
     mkdir -p "$M/nzbdl/completed/Series" "$M/nzbdl/completed/Movies" "$M/nzbdl/completed/Books" \
              "$M/nzbdl/completed/Audiobooks" "$M/tordl/cwd-books" "$M/tordl/cwd-audiobooks"
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

     After the drain gate the `usenet/` and `torrents/` folders are empty. A file left in one, or
     dropped into `tv/ingest` or `movies/ingest`, stops the script at `rmdir`: import it, or move it
     where the old layout keeps it (the matching `nzbdl/completed/` or `tordl/cwd-` folder, or the
     tree's top folder), and rerun from there. `setfacl --restore` resets the owner, group, mode and
     ACL of every path in the backup, so it runs after the moves are undone; it reports, and skips,
     paths that are gone since (an imported dronescan entry). Files created since keep their
     default-ACL modes, which grant the `media` group access as before.
4. **Restore the volumes' owners:** inside `hostsh`, run `cd / && setfacl --restore=<file>` for
   each `$B/volume-*.acl.txt` and for `$B/ownfoil-games.acl.txt`.
5. **Restore the app configs** from `$B/media_{sonarr,radarr,sabnzbd,qbittorrent}_config/`: copy
   back `sonarr.db*`, `radarr.db*`, `sabnzbd.ini` and `qBittorrent/categories.json`, keeping their
   owners (`cp -a`).
6. **Start doco-cd, then gate.** Only now, with the dataset, volumes and configs restored, start
   doco-cd (the first block of step 10). It deploys the revert to the same 12 Stacks. Start nothing
   by hand: the new containers have the narrow mounts, which point at folders the restore removed.
   Gate on the reverted revision with this block, which waits up to 15 minutes for every container
   of those Stacks to carry `$SHA` (the revert) and to mount none of the new folders, and removes
   the Stacks that don't:

   ```sh
   hostsh <<'EOF'
   stacks='plex emby sonarr radarr sabnzbd qbittorrent autobrr shelfmark audiobookshelf calibre-web-automated nextpvr ownfoil'
   [ -n "$SHA" ] || { echo "SHA is not set"; exit 1; }
   gate() {
     rc=0
     for s in $stacks; do
       ids=$(docker ps -aq --filter "label=com.docker.compose.project=$s")
       [ -n "$ids" ] || { echo "FAIL $s: no containers"; rc=1; continue; }
       for id in $ids; do
         n=$(docker inspect -f '{{.Name}}' "$id"); n=${n#/}
         sha=$(docker inspect -f '{{index .Config.Labels "cd.doco.deployment.target.sha"}}' "$id")
         new=$(docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' "$id" |
           grep -cE "^$M/(tv|movies|books|audiobooks)/(library|usenet|torrents|ingest)(/|$)" || true)
         [ "$new" = 0 ] || { echo "FAIL $s/$n: mounts a new folder"; rc=1; }
         m=$(docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' "$id" | grep -c "^$M\(/\|$\)" || true)
         [ "$m" = 0 ] || [ "$sha" = "$SHA" ] || { echo "FAIL $s/$n: deployed revision '$sha'"; rc=1; }
       done
     done
     return $rc
   }
   i=0
   until out=$(gate); do
     i=$((i + 1))
     if [ $i -ge 30 ]; then
       printf '%s\n' "$out"
       for s in $(printf '%s\n' "$out" | awk '{ sub(/[\/:].*/, "", $2); print $2 }' | sort -u); do
         ids=$(docker ps -aq --filter "label=com.docker.compose.project=$s")
         [ -z "$ids" ] || { echo "REMOVING $s"; docker rm -f $ids; }
       done
       echo "GATE FAILED"; exit 1
     fi
     sleep 30
   done
   echo "GATE PASSED"
   EOF
   ```

   If the PR was never merged, skip the revert and this gate: the stopped containers are still the
   old compose's, so once steps 3–5 are done, start them per Stack (`docker start`), and doco-cd.

   A removed Stack is recreated through doco-cd's REST API, as in step 10. Once the gate passes,
   start Navidrome (`docker start navidrome`; its compose is the same in both revisions), resume
   SABnzbd, re-enable autobrr's filters, and restore the `media` SMB share if it was removed.

The identity changes can stay. The new users own nothing once the volumes are restored, `cwa` keeps
UID 4008, the deleted groups owned nothing, and the old compose runs Sonarr, Radarr, SABnzbd,
Navidrome, Plex, Emby and qBittorrent with the same UIDs. Shelfmark, Audiobookshelf and CWA go back
to `BOOKS_UID` (4008, now named `cwa`), and NextPVR, Ownfoil and autobrr to 568. Turn SMB back on
for the user `media` only if something still logs in as it.
