# Which UNIX identities isolate Libraries, and how does a Downloader's file become the Organizer's?

Spike for [#552](https://github.com/jbruns/home-ops/issues/552), feeding [#543](https://github.com/jbruns/home-ops/issues/543) (part of [#531](https://github.com/jbruns/home-ops/issues/531)). Vocabulary: [`GLOSSARY.md`](../../GLOSSARY.md). Storage rules: [ADR 0009](../adr/0009-library-isolation-by-role.md).

Researched 2026-10-18 against the Host:
- Versions: TrueNAS 25.10, kernel 6.12, Docker 28, OpenZFS 2.3.
- The media dataset, `$MEDIA_ROOT`, is one dataset with `acltype=posix` and `xattr=on`. `<pool>` is its pool.
- Live facts come from read-only inspection of the Host:
  - `docker inspect`, `getfacl` and `find`;
  - GET calls to the TrueNAS API and to the Sonarr and Radarr APIs;
  - the generated `smb4.conf`.
- Download names and other personal data are left out.

The same day, the doc was revised with the user's decisions and an adversarial review ([Decisions](#decisions)).

How the experiments ran ([Appendix](#appendix-experiments) has the scripts and their output):
- **E3:** real containers on Docker 29. Each one exposes only the bind mounts #543 would give it.
- **E1, E2, E4–E12:** a throwaway privileged container on a loopback ext4 image, switching UIDs with `setpriv`.

Permission checks, POSIX ACLs and the `rename`/`link` rules are kernel VFS behaviour, so they behave the same on ZFS with `acltype=posix`.

## Answer

**Mounts isolate paths, and ownership can't add to that.** ADR 0009 is about an attacker who controls a Service's process inside its container. Container escape is out of scope (decision 1).
- **Paths:** the bind mounts are the barrier. E3 checked this with real containers: a Service can't name what it doesn't mount, can't mount anything itself, and can't write through a `:ro` mount whatever the ownership.
- **Inside a mount,** ownership has one job: let the Organizer take over the Downloader's file. The handoff is a rename (or a copy, or a hardlink), followed by removing the leftover folder.
- **What does that job:** one shared group, setgid directories, and **a default ACL that keeps new files group-writable**. Every Service runs with umask `0022`, so without that ACL the handoff fails (E1); with it, it works (E2).

**Contents are a separate matter: they follow the inode, not the path.**
- A rename keeps the inode. A Downloader that kept a second link or an open file descriptor can still rewrite an imported file after the Organizer has moved it into the Library (E9). Re-owning the file doesn't stop this.
- Only a copy into a fresh inode cuts that access.
- **On this Host, a copy should cost no extra space:** ZFS block cloning is enabled, and each Organizer's copy path asks for a clone. Sonarr tries a reflink itself; Radarr's .NET 8 runtime and Shelfmark's Python 3.14 use `copy_file_range`. This is unconfirmed until a real Organizer import on the Host grows `bclonesaved` ([details](#pathnames-versus-inode-contents)).
- **Recommendations:**
  - Organizers copy from `torrents/` instead of hardlinking: Sonarr's Use Hardlinks off (decision 9), and Shelfmark's `HARDLINK_TORRENTS_AUDIOBOOK` off. That ends the "a seeding Downloader can rewrite a Library file" residual.
  - Accept the narrower usenet residual (decision 10), because Sonarr always moves SABnzbd's downloads ([details](#pathnames-versus-inode-contents)).

**Identity model:**
- One group, `media`, which becomes every Service user's primary group.
- One UID per media app Service, everywhere, including separate UIDs for Shelfmark, Calibre-Web-Automated and Audiobookshelf.
  - Separate UIDs don't isolate anything.
  - They do record which Service created each file. That matters after a Downloader compromise: it lists exactly the Library files that Downloader could still rewrite ([details](#do-per-service-uids-add-value)).

**Clean up the storage:**
- Reset every hand-made ACL across the media trees to that one UID-free default ACL, keeping a `getfacl` backup ([procedure](#acl-reset-procedure)).
- Take the Libraries off SMB, and give people an **Ingest Folder** share instead, one per Library whose Organizer can import from a folder ([details](#smb-ingest-folders)). Music gets no share (decision 7).
- Retire the SMB login `media` (1001). Its files keep their owner (decision 8).

## Today

### Identities

| Identity | UID:GID | Defined in | Runs | Writes media storage |
|---|---|---|---|---|
| `sonarr`, `radarr` | 4001, 4002 :1002 | Host users (own groups), `SONARR_UID`, `RADARR_UID` | Sonarr, Radarr | every Library, rw |
| `sabnzbd` | 4003:1002 | Host user (own group), `SABNZBD_UID` | SABnzbd | its download folder |
| `navidrome`, `plex`, `emby` | 4004–4006 :1002 (Plex also video/render) | Host users (own groups), `<SVC>_UID` | Media Servers | their recordings only; Libraries `:ro` |
| `qbittor` | 4007:1002 | Host user (own group), **not in `identities.tsv`**, `QBITTORRENT_UID` | qBittorrent | its download folder |
| `books` | 4008:1002 | Host user (own group), **not in `identities.tsv`**, `BOOKS_UID` | Shelfmark, Audiobookshelf, Calibre-Web-Automated's app process | Books, Audiobooks, Podcasts and their download/ingest folders |
| `media` (group) | 1002 | Host group: `smb=false` on the Host, `smb true` in `identities.tsv`. Env: `MEDIA_SHARED_GID` | primary group of every media Service; the owner's account is a member | all Libraries |
| `media` (user) | 1001:1002 | Host user, SMB login; not in `identities.tsv` | no Service | owns the dataset root, `music` and `ebooks` |
| `apps` (TrueNAS builtin) | 568:568 | five env aliases, one value: `MEDIA_UID/GID`, `COMMON_*`, `HOME_*`, `MONITORING_*` | exporters and non-media Stacks; **568:1002** for NextPVR, autobrr, Ownfoil | NextPVR recordings; Ownfoil's ROM folder |
| image-fixed | 33, 999, 1000, 0, … | literals | images that require them | none under `/media` |

- Calibre-Web-Automated runs its s6 supervisors as root with `DAC_OVERRIDE` among other capabilities, and its app processes as 4008:1002 with none. Root with `DAC_OVERRIDE` ignores both mode and ACL, so for CWA only the mounts limit what it can touch.
- The per-Service groups 4001–4008 own no file in the media dataset or the Docker volumes. Every container runs with GID 1002.
- Every media Service's main process has `Umask: 0022` (from `/proc/<pid>/status`).
- UIDs 4009–4013 are free on the Host.

### Library trees

- Library roots are owned by their Organizer (or 1001) in group `media`, mode `2770` or `2775`.
- They carry hand-made POSIX ACLs: named-user entries that deny some Service UIDs and allow others, in both the access and the default ACL. Nearly every inode in the media trees has inherited them.
- Two download subfolders were created by Docker as `root:root 755`.

### App settings the handoff depends on

| Service | Setting (read-only check) | Effect on new files |
|---|---|---|
| SABnzbd | `permissions = 2770` in `sabnzbd.ini` | chmods each finished job: folders `2770`, files `660`, ACL `mask::rw-` |
| qBittorrent (libtorrent) | no `UMASK` override | creates files with mode `0666`, so the default ACL gives `660`/`664` with `mask::rw-` |
| Sonarr | Set Permissions off (`setPermissionsLinux=false`, chmod folder `755`); Use Hardlinks **on**; SABnzbd client "Remove Completed" on; no torrent client | doesn't chmod; moves usenet downloads |
| Radarr | Set Permissions off (chmod folder `770`); Use Hardlinks **off**; SABnzbd client "Remove Completed" on; no torrent client | doesn't chmod; copies when it can't move |

### Storage and SMB

- **Block cloning is enabled and in use** on the pool (`zfs_bclone_enabled=1`, `feature@block_cloning=active`).
- **SMB:**
  - One `media` share exports the whole media dataset; file permissions alone decide access.
  - Samba's globals set `create mask = 0664` and `directory mask = 0775`.
  - SMB logins are the owner's account, the legacy `media` login (1001) and three device accounts.
  - `music` is mostly owned by 1001, so Music was written over SMB as the `media` login in the past. Nothing logs in as `media` any more (decision 8).

### Findings

What #543 must fix:

1. **The deny-list ACLs fail open.** The modes alone would let every member of `media` write every Library. The hand-made ACLs deny specific UIDs, but each list misses some identities, new identities are allowed by default, and no list is kept in the repo. Any unlisted Service that mounted a Library could delete it (E6).
2. **The default ACLs are what make imports work today.** Their `default:group::rwx` and `default:mask::rwx` override umask `0022`. Without them:
   - SABnzbd's job folders become `755` and its files `644`;
   - Sonarr can neither move the file out nor delete the folder;
   - `fs.protected_hardlinks=1` (set on the Host) blocks hardlinks (E1).
3. **Imported files keep the Downloader's UID and ACL**, so Library files are owned by SABnzbd and carry the download folder's ACL.
4. **Torrent payloads block the Organizers.** The torrent folder's inherited ACL denies Sonarr and Radarr, so they can't read any existing torrent payload.
5. **The whole dataset is one SMB share**, so every Library is writable over SMB.
6. **Docker created two bind sources as `root:root 755`** because the directories didn't exist when a Service started ([Docker docs](https://docs.docker.com/engine/storage/bind-mounts/#differences-between--v-and---mount-behavior)).
7. **`identities.tsv` has drifted from the Host.**
   - `media` is `smb=false` on the Host but `true` in the file.
   - `qbittor` and `books` are missing from the file.

## Q1: Which identities buy isolation?

| Identity | For isolation | Required by an image | Historical / redundant | Verdict |
|---|---|---|---|---|
| `media` 1002 (one shared group) | **Yes:** the Organizer needs group access to the Downloader's folders and files | | | Keep; the only media group |
| One UID per media app Service | No barrier while the mounts hold. **It does record which Service created each file** ([below](#do-per-service-uids-add-value)) | | | Keep, and apply to every media app Service |
| Per-Service groups 4001–4008 | No: no process runs with them and no file has them | | Yes (TrueNAS's default of one group per user) | Delete (decision 4) |
| `books` 4008 shared by three Services | No | | Breaks the one-UID-per-Service rule | Split (decision 6) |
| `apps` 568 for exporters and sidecars | No: they mount no media storage | | `MEDIA_UID/GID`, `COMMON_*`, `HOME_*`, `MONITORING_*` are aliases | Keep 568; collapse the aliases later |
| `media` user 1001 | No | | Owns the dataset root, `music` and `ebooks`; an unused SMB login | Keep as a no-login owner of existing files; turn off its SMB access (decision 8) |
| Named-user ACL entries on the trees | **Superseded:** they fail open (E6) and go stale once the mounts are scoped | | Yes | Reset everything (decision 5) |
| Default ACL `d:g::rwx,d:m::rwx` | **Yes:** the only filesystem way to get group-writable files at umask `0022` | | | Keep, without named entries |
| 33, 999, 1000, 0, 44/107 (video/render) | | Yes | | Keep |

## Q2: Options for the Downloader → Organizer handoff

| | Atomic moves | Seeding | Downloader still reaches the imported inode? | Blast radius inside the threat model | Operational cost |
|---|---|---|---|---|---|
| **(a)** group per Library + setgid | ✅ (E4) | ✅ | Yes, through a retained link, an open fd or group write | Same as (d). Its extra layer, against mis-scoped mounts and container escape, is out of scope (decision 1) | High: ~9 groups; `group_add` on every Media Server for each Library it presents (E4); recursive `chgrp`; still needs the default ACL |
| **(b)** Organizer re-owns on import | ✅ | ✅ | **Yes:** `chown` doesn't remove group write, a retained link or an open fd (E9) | **Worse:** `CAP_CHOWN` lets the Organizer give anything it sees to anyone (E7). Without that capability, `chown` and `chgrp` fail (E7) | Needs a capability or a Custom Component |
| **(c)** named-user ACL entries | ✅ (E5) | ✅ | Yes | Same as (d); a deny-list fails open (E6) | The named entries travel with the inode (E5); invisible in `ls -l`; the mask pitfall below |
| **(d)** shared group + setgid + default ACL | ✅ (E2, E3) | ✅ | Yes for a move or hardlink; **no** for a copy (E9) | The mounts decide it (E3) | Lowest: one group, no `group_add` |
| (d) with umask only | ❌ (E1) | ❌ hardlink `EPERM` | | | The images expose no umask setting |
| **(e)** idmapped mounts / userns-remap | n/a | n/a | Yes | No change: it maps how ownership *looks*, not what a Service can reach | Not available (below) |

**The mode-mask pitfall applies to (c) and (d) alike** ([acl(5)](https://man7.org/linux/man-pages/man5/acl.5.html)).
- **The cause:** when a file is created in a folder with a default ACL, the mode the app passes to `open()` caps the ACL mask, and a later `chmod` rewrites the mask.
- **The effect:** an app that creates or chmods a file as `0644` leaves the group with `r--`. The Organizer can still rename or copy that file, but its hardlink fails with `EPERM`, because `fs.protected_hardlinks=1` requires read *and* write ([kernel docs](https://docs.kernel.org/admin-guide/sysctl/fs.html#protected-hardlinks)). E10 shows this.
- **What the Host does today:**
  - SABnzbd chmods its files to `660`;
  - libtorrent creates files as `0666`;
  - Sonarr and Radarr don't chmod.

  So every Downloader file on the Host has `mask::rw-` (checked on samples).
- **(d) therefore relies on these settings:**
  - SABnzbd keeps `permissions` at `2770` (or `770`);
  - qBittorrent gets no restrictive `UMASK`;
  - Organizers keep Set Permissions off, or set chmod folder to `2770`/`770`.
- **With Organizers copying instead of hardlinking** (recommended below), the Organizer only needs group read on the file. Read survives a `0644` file, so the pitfall stops mattering for imports.

**(e) on the Host:**
- **What supports idmapped mounts:** the kernel (6.12), OpenZFS 2.3 ([openzfs#13671](https://github.com/openzfs/zfs/pull/13671), since 2.2) and runc 1.2 ([mount_setattr(2)](https://man7.org/linux/man-pages/man2/mount_setattr.2.html)).
- **Docker 28 has no API for them.**
  - [moby#52061](https://github.com/moby/moby/issues/52061) was closed without an implementation.
  - [moby#53368](https://github.com/moby/moby/pull/53368) (`BindOptions.IDMapping`) is an open PR.
- **`userns-remap` doesn't fit either:**
  - it maps *all* containers into one range, so it can't tell Services apart;
  - it hides existing images and volumes ([docs](https://docs.docker.com/engine/security/userns-remap/));
  - TrueNAS middleware writes `daemon.json`.
- **E8 shows the effect:** an idmapped mount would make Sonarr *see* SABnzbd's file as its own, while storing what Sonarr creates under SABnzbd's UID.

## Pathnames versus inode contents

A bind mount limits which **names** a Service can resolve. It doesn't limit which **inodes** the Service already holds. A file has as many names as links, and stays open through any file descriptor. A rename moves one name and keeps the inode, its other links, and every open fd. So does a hardlink, by design.

**E9, as SABnzbd, Sonarr and qBittorrent would see it:**

1. Before handing a file over, a compromised SABnzbd adds a second link inside `usenet/` (or keeps the file open).
2. Sonarr renames the file into `library/`.
3. SABnzbd truncates its link, and the Library file is now 0 bytes.
4. Re-owning the file after the rename (option b) changes nothing: SABnzbd still has the link and group write.
5. Copying into a fresh inode and then deleting the source leaves the Library file intact.

SABnzbd can never **unlink** or rename the Library's name for the file, because that needs write access to `library/`, which it can't resolve.

**What a compromised Downloader can still rewrite:**

| Path into the Library | Exposed Library files | Fix |
|---|---|---|
| Hardlink from `torrents/` (Sonarr today; Shelfmark for Audiobooks once #543 gives it one tree mount; qBittorrent seeding) | **Every Library file still seeding**, including files imported before the compromise | Copy instead of hardlink, in every Organizer |
| Move from `usenet/` (Sonarr and Radarr always move SABnzbd downloads) | Only files handed over **while** SABnzbd was compromised. It has no path, link or fd to files imported earlier | Copy-then-delete; not configurable |

**Copy should be a block clone on this Host, but that's unconfirmed.** Each Organizer reaches a clone by a different path:

| Organizer | Copy path | Clones when |
|---|---|---|
| Sonarr (.NET 6 runtime) | Sonarr's own `DiskTransferService` calls `TryCreateRefLink` when source and target are both `zfs` or `btrfs`. That's `ioctl(FICLONE)` in `RefLinkCreator`, x86_64 only. On failure it falls back to a full verified copy ([`DiskTransferService`](https://github.com/Sonarr/Sonarr/blob/main/src/NzbDrone.Common/Disk/DiskTransferService.cs), [`RefLinkCreator`](https://github.com/Sonarr/Sonarr/blob/main/src/NzbDrone.Mono/Disk/RefLinkCreator.cs)). The .NET 6 `File.Copy` fallback uses `sendfile` and doesn't clone ([.NET 6 `pal_io.c`](https://github.com/dotnet/runtime/blob/release/6.0/src/libraries/Native/Unix/System.Native/pal_io.c)) | `FICLONE` succeeds on the ZFS bind mount |
| Radarr (.NET 8 runtime) | No app-level reflink. `File.Copy` tries `FICLONE`, then `copy_file_range` ([.NET 8 `pal_io.c`](https://github.com/dotnet/runtime/blob/release/8.0/src/native/libs/System.Native/pal_io.c)) | either call clones |
| Shelfmark (Python 3.14) | `shutil.copy2` / `copyfile`, which try `copy_file_range` first ([CPython `shutil.py`](https://github.com/python/cpython/blob/3.14/Lib/shutil.py)) | `copy_file_range` clones |

- OpenZFS 2.3 with `zfs_bclone_enabled=1` turns `FICLONE` and `copy_file_range` within a pool into a block clone. Source and destination sit in one dataset, inside one mount (ADR 0009).
- **A clone** is a separate inode at no extra space and almost no I/O. When qBittorrent writes to its copy, those blocks diverge copy-on-write, and the Library file doesn't change.
- **If cloning doesn't happen,** each torrent import is a full data copy: import I/O for the whole file, and double the space until the torrent is removed from qBittorrent. Isolation still holds; only the cost changes.
- **Verify with a real import, not `cp`:** read `zpool get bclonesaved <pool>`, let the Organizer import one torrent (Shelfmark today; Sonarr and Radarr have no torrent client yet), and read it again. It should grow by about the file's size. If it doesn't, check the Organizer's trace log for the reflink attempt before accepting the space cost.
- **Decided:** turn **Use Hardlinks instead of Copy off in Sonarr** (decision 9); it is already off in Radarr. Torrents keep seeding, and the seeding residual (decision 2) goes away.
- **Shelfmark has the same setting.** `tn/shelfmark/compose.yaml` sets `HARDLINK_TORRENTS_AUDIOBOOK=true`, and the live settings agree. Today Shelfmark's download folder and `/audiobooks` are separate mounts, so `os.link` fails with `EXDEV` and Shelfmark falls back to a copy ([0540](0540-shelfmark-organizer.md)). Once #543 gives Shelfmark the one Audiobooks tree mount, the link succeeds and qBittorrent keeps a writable link to the Library file. **Set `HARDLINK_TORRENTS_AUDIOBOOK=false`.** Keep `HARDLINK_TORRENTS` (Books) at `false`, as it is today, and leave the legacy `TORRENT_HARDLINK` unset. Books needs nothing else: CWA's calibre import writes a new file into the Library, so the Books Library never shares an inode with a Downloader.

**Usenet residual: accepted (decision 10).** Sonarr and Radarr hard-code `CanMoveFiles = true` for SABnzbd ([Sonarr](https://github.com/Sonarr/Sonarr/blob/main/src/NzbDrone.Core/Download/Clients/Sabnzbd/Sabnzbd.cs), [Radarr](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/Download/Clients/Sabnzbd/Sabnzbd.cs)), so no setting makes them copy. Accept it because:
- the exposure is only the files SABnzbd handed over while compromised;
- a compromised Downloader already controls those bytes when it hands them over;
- per-Service UIDs make the exposed files findable afterwards: `find /media/*/library -user sabnzbd -newer <marker>`.

Closing it would take a Custom Component that copies instead of moving. That isn't worth it.

## Q3: The ADR 0009 goals under (d) and #543's mounts

E3 ran each Service as `--user <uid>:1002` (a member of `media`, which has `rwx` on every tree), with `--cap-drop ALL` and `no-new-privileges`. Each container mounted only the subfolders #543 would give it, using Docker's `volume-subpath`, which is a bind mount of a subfolder.

| Goal | Holds? | Evidence |
|---|---|---|
| SABnzbd cannot wipe the TV Library | ✅ for paths | E3: SABnzbd's mount table shows only `/data/{tv,movies}/usenet`. Reading, writing and unlinking `/data/tv/library/old.mkv` all fail with `ENOENT`, including through `usenet/../library`. `mount(2)` returns `EPERM`, and a walk of its whole filesystem finds no Library file. Inode residual: [above](#pathnames-versus-inode-contents) |
| Sonarr cannot delete Movies | ✅ | E3: Sonarr's only media mount is `/data/tv`; `film.mkv` is nowhere in its filesystem. Rename and hardlink within `/data/tv` succeed |
| qBittorrent cannot delete a Library | ✅ | It mounts only `<library>/torrents`. With copy instead of hardlink, it can't rewrite Library contents either |
| Media Servers cannot write any Library except their recordings | ✅ | E3: Plex is in `media` with group `rwx`, yet writing and unlinking fail with `EROFS` through its `:ro` mounts |
| Rename and hardlink stay possible between the download folder and `library/` | ✅ | E3: both succeed inside the one tree mount. Across two bind mounts, both fail with `EXDEV` ([rename(2)](https://man7.org/linux/man-pages/man2/rename.2.html)) |

## Do per-Service UIDs add value?

Isolation comes from mounts, and access within a mount comes from the shared group. So the question is what a separate UID still buys **inside** the threat model, where each container has its own mount, PID, IPC and network namespaces.

| Property | Separate UID per Service | One shared UID |
|---|---|---|
| Library isolation | None (mounts) | None (mounts) |
| Config volumes | None: each is mounted only into its own container | Same |
| Signals, ptrace, SysV IPC, abstract sockets | None: separate PID, IPC and network namespaces | Same |
| **Attribution:** the file owner is the Service that created the inode | **Yes.** After a Downloader compromise, `find -user <downloader>` lists exactly the Library files it could still rewrite. Ownership also shows where stray files came from | Lost |
| Per-UID kernel limits (inotify instances, `RLIMIT_NPROC`) | Separate budgets | Shared, though ample on the Host (`max_user_instances=8192`) |
| Ownership-gated calls (`chmod`, `utimensat` with explicit times, used by "change file date") | Only on files the Service created | On every media file; a small convenience |
| Need for the default ACL | Needed | Still needed: SMB drops, existing files and other owners stay |
| Cost | One `identities.tsv` row and one env key per Service | Fewer rows, plus a one-time `chown` of config volumes |

**Decision: keep one UID per media app Service, consistently.** Attribution is cheap, and it bounds the only residual that remains (usenet).

The rule for the media Stacks: **every app Service runs as its own UID with primary group `media`; sidecars and exporters run as `apps` 568:568.**

That splits `books` 4008 three ways and gives NextPVR, Ownfoil and autobrr their own UIDs:

| Service | Today | Proposed |
|---|---|---|
| Calibre-Web-Automated | `books` 4008 | `cwa` 4008 (rename the Host user; it keeps the UID, so no file changes) |
| Shelfmark | `books` 4008 | `shelfmark` 4009 |
| Audiobookshelf | `books` 4008 | `audiobookshelf` 4010 |
| NextPVR | 568:1002 | `nextpvr` 4011 |
| Ownfoil | 568:1002 | `ownfoil` 4012 |
| autobrr | 568:1002 | `autobrr` 4013 |

Existing media files keep their owners. After the ACL reset they're `660` in group `media`, so the new UIDs reach them through the group. Each re-UIDed Service's own Docker volumes need `chown -R`:

| Service | Volumes |
|---|---|
| Shelfmark | `asot-media_cwa-downloader_config` |
| Audiobookshelf | `asot-media_audiobookshelf_{config,metadata}` |
| NextPVR | `media_nextpvr_{config,buffer}` |
| Ownfoil | `asot-media_ownfoil_config` |
| autobrr | `media_autobrr_config` |

Ownfoil's ROM folder is already group `media` `775`.

## SMB Ingest Folders

Decision 3: people may drop new media into a folder over SMB, but the Libraries themselves are not reachable over SMB. A person dropping files acts as the Requester, and the folder is that Library's **Ingest Folder** (GLOSSARY).

| Question | Design |
|---|---|
| Where | `$MEDIA_ROOT/<library>/ingest/`, inside the Library tree, so it's on the Organizer's one mount: `ingest → library/` is a rename. For Books, the existing CWA Ingest Folder (`cwa-book-ingest`, wherever #543 puts it); CWA copies into its Library through calibre, so it needs no rename |
| Which Libraries | Those whose Organizer can import from a folder: **TV** (Sonarr), **Movies** (Radarr), **Books** (CWA). Not Audiobooks: Shelfmark can't adopt files it didn't download (ADR 0009). Not Music: it has no Organizer, and it gets no share either (decision 7). Music takes no new files until a Music Organizer exists ([follow-ups](#follow-ups-not-543)) |
| SMB shares | One share per Ingest Folder (`tv-ingest`, `movies-ingest`, `books-ingest`) with the folder as its path. TrueNAS 25.10 accepts a folder inside a dataset as a share path ([middleware `validate_mount_info`](https://github.com/truenas/middleware/blob/stable/goldeye/src/middlewared/middlewared/plugins/smb.py)). **Remove the `media` share.** Samba's default `wide links = no` keeps clients inside the share path |
| Who writes | The owner's own SMB account, a member of `media`; not the shared `media` login. The share ACL names that account instead of allowing everyone |
| Folder ownership and ACL | `<organizer>:media 2770` with ACL A. Samba's `create mask 0664` and `directory mask 0775` combine with the default ACL, so a dropped file is `<owner>:media 660` and a dropped folder is `2770` (E11) |
| How the Organizer picks files up | **CWA:** automatically; it watches its Ingest Folder. **Sonarr / Radarr:** a person runs Manual Import (mode Move) on `/data/<library>/ingest`. Sonarr v4 and Radarr have no folder watcher, and this needs no Custom Component. The move is a rename, which needs only group `rwx` on the folders (E11) |
| Isolation | E11: an account not in `media` can't even list the Ingest Folder. The share path confines SMB clients to the Ingest Folder, so the Library is out of reach whatever the account's groups |
| `smb` flag on group `media` | `false`, as on the Host: the share ACL names the user, and file access comes from the user's Unix groups. If the first test write over SMB is denied, set it to `true` |

## Recommendation

### Identity model

- **Groups:** `media` (1002) only; delete 4001–4008 (decision 4).
- **Users:** one per media app Service, with `media` as the primary group and no supplementary groups.
  - Compose `user:` lines stay `<SVC>_UID:MEDIA_SHARED_GID`.
  - No `group_add` beyond Plex's video/render groups.
- **Sidecars, exporters and non-media Stacks:** `apps` 568 or the UID their image requires.

### Per Library tree

```text
/media/<library>/      <organizer>:media     2770   ACL A
├── library/           <organizer>:media     2770   ACL A   Media Servers mount :ro
├── usenet/            sabnzbd:media         2770   ACL A   SABnzbd mounts rw
├── torrents/          qbittor:media         2770   ACL A   qBittorrent mounts rw
└── ingest/            <organizer>:media     2770   ACL A   SMB share <library>-ingest (TV, Movies)
recordings Library     <media server>:media  2770   ACL A
files                  <creator>:media       0660
/media, music/, ebooks/  media(1001):media  unchanged owner; no ingest/, no share

ACL A: no named entries; default d:u::rwx,d:g::rwx,d:m::rwx,d:o::---
```

### ACL reset procedure

Decision 5 is to start clean. **The reset and the mount scoping happen in one maintenance window, with every media Stack stopped.** The named-user denies are today's only barrier between Sonarr and Radarr (which mount the whole media dataset rw) and the other Libraries. Resetting them while those broad mounts still run would let Sonarr delete Movies. So no media Service may start between the reset and the Deploy of #543's scoped mounts ([the window](#changes-for-543-in-order)). E12 rehearses the ACL steps.

```sh
M=$MEDIA_ROOT
B=<backup-dataset>/media-acl-$(date +%F).txt   # outside the dataset being changed

# 0. doco-cd and every media Stack are already stopped (window step 1). With nothing running,
#    a rollback to this snapshot loses no downloads.
zfs snapshot <pool>/<media-dataset>@pre-543

# 1. Back up owner, group, mode and ACL of every inode, with numeric ids (groups 4001-4008 will be deleted).
getfacl -R -p -n "$M" > "$B"

# 2. Inventory and dry run. Nothing changes.
getfacl -R -s -p -n "$M" | grep -c '^# file:'              # inodes with extended ACLs
getfacl -R -p -n "$M" | grep -cE '^(default:)?user:[^:]'   # named-user entries
setfacl --test -R -b "$M/tv" | head                       # what -b would leave

# 3. Reset. setfacl -b keeps the old mask as the group bits (E12), so access doesn't widen.
setfacl -R -b "$M"
find "$M" -mindepth 1 -type d -exec chmod 2770 {} +
find "$M" -type f -exec chmod 0660 {} +          # drops stray x and setgid bits on files (670, 2660)
find "$M" -mindepth 1 -type d -exec setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- {} +

# 4. Verify: no named entries remain, every folder is 2770 with the default ACL.
getfacl -R -p -n "$M" | grep -cE '^(default:)?user:[^:]'   # expect 0
find "$M" -mindepth 1 -type d ! -perm 2770 | head          # expect nothing
```

- **ACL-only rollback:** `setfacl --restore="$B"` restores owner, group, mode and ACL for every path in the backup. E12 shows the result is byte-identical to the backup. It needs the old paths, so undo any folder moves first.
- **Scope:** the dataset root stays `1001:1002 2550`, and `-mindepth 1` leaves it alone.
- **Owners are not changed.** The reset touches modes and ACLs only. The files owned by `media` (1001) under `music` and `ebooks` keep that owner (decision 8).
- **Size:** every inode in the media dataset, metadata only.
- **Run time:** minutes.

### Changes for #543, in order

**Before the window,** turn Sonarr's Use Hardlinks **off** (decision 9). It has no effect today, because Sonarr has no torrent client. Once Sonarr starts with one TV tree mount, a hardlink would succeed. Radarr already has it off. Shelfmark's setting changes with the compose in step 5.

Steps 1–7 are **one maintenance window**. No media Service starts until step 7, so nothing runs with the old whole-dataset mounts after the reset.

1. **Stop** doco-cd (so it can't redeploy) and every media Stack: Sonarr, Radarr, SABnzbd, qBittorrent, Shelfmark, CWA, Audiobookshelf, the Media Servers, NextPVR, Ownfoil and autobrr.
2. **Back up:** take the ZFS snapshot and the `getfacl -R` backup (steps 0–1 of the [procedure](#acl-reset-procedure)), and record the current ownership of each re-UIDed Service's volumes.
3. **ACL reset** (steps 2–4 of the procedure).
4. **Identities** (runbook from #544):
   - In the TrueNAS UI, rename `books` → `cwa`.
   - Apply the new `identities.tsv`, which creates 4009–4013.
   - Move every user's primary group to `media`.
   - Delete groups 4001–4008.
   - Turn off SMB access for the user `media` (1001) in the TrueNAS UI. Keep the user: it owns the dataset root, `music` and `ebooks`, and no Service runs as it.
   - **Don't `chown` 1001's files.** Access comes from the group, so the owner grants nothing extra. As the owner, 1001 records "added by a person over SMB before #543". A future Music Organizer reaches those files through `media`, so it doesn't need a `chown` either.
5. **Env and compose:**
   - Replace `BOOKS_UID` with `CWA_UID`, `SHELFMARK_UID` and `AUDIOBOOKSHELF_UID`.
   - Replace `MEDIA_UID` with `NEXTPVR_UID`, `OWNFOIL_UID` and `AUTOBRR_UID` on those three Services. Sidecars keep `MEDIA_UID`.
   - Shelfmark: set `HARDLINK_TORRENTS_AUDIOBOOK=false` in `tn/shelfmark/compose.yaml`, and the same in its settings file if that overrides the environment.
   - `chown -R` each re-UIDed Service's volumes ([list](#do-per-service-uids-add-value)).
   - Every rw media mount is scoped to one Library tree or one download folder. No Service keeps `${ZFS_POOL_ROOT_HDD}/media:/data` rw.
6. **Layout:**
   - Create each new folder before the Deploy, so Docker doesn't make it `root:root 755`:

     ```sh
     d=$MEDIA_ROOT/tv/usenet
     install -d -o sabnzbd -g media -m 2770 "$d"
     setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- "$d"
     ```

   - Move the existing folders into place, which is renames only.
   - Fix or remove the two `root:root 755` folders.
   - Mounts follow ADR 0009 and the 0540 spike.
   - Record each move, so it can be reversed.
7. **Deploy:** merge #543's compose and env change. Before starting doco-cd, confirm on `main` that no media Service has a rw mount of the whole dataset. Then start doco-cd and let it deploy the scoped mounts.

**Rollback of the window:**
- Keep doco-cd and the media Stacks stopped. If step 7 already ran, stop them first; files downloaded since then are lost by the snapshot rollback, so use the `setfacl --restore` route instead.
- Roll the media dataset back with `zfs rollback <pool>/<media-dataset>@pre-543`. That reverts the moves, ACLs and modes together, and nothing was written during the window. Or, without the snapshot: reverse the recorded moves, then `setfacl --restore`.
- Revert #543's compose and env commit on `main`.
- Restore the re-UIDed volumes' ownership.
- Then start doco-cd. The Host identity changes can stay: new users are unused, `cwa` keeps UID 4008, and the deleted groups owned nothing.

After the window, configure the apps:

8. **App settings:**
   - Sonarr's Use Hardlinks is already off (before the window); Radarr's too.
   - Shelfmark: confirm its live settings show `HARDLINK_TORRENTS_AUDIOBOOK=false` and `HARDLINK_TORRENTS=false`.
   - Keep SABnzbd `permissions = 2770`.
   - Leave qBittorrent without a `UMASK`.
   - Keep Set Permissions off in Sonarr and Radarr.
9. **SMB:**
   - Create `tv-ingest`, `movies-ingest` and `books-ingest`, with a share ACL that names the person's account.
   - Remove the `media` share. Don't create a Music share (decision 7).
10. **Check after the Deploy:**
   - Usenet handoff: as SABnzbd, create a file in `usenet/`; as the Organizer, `mv` it into `library/`.
   - Torrent handoff: let each Organizer with a torrent client import one real torrent: Shelfmark today, and Sonarr or Radarr once they get one. Confirm with `stat -c '%i %h'` that the Library file's inode differs from the seeding file's, with link count 1, and that `bclonesaved` grew ([above](#pathnames-versus-inode-contents)).
   - SMB: drop a file into `tv-ingest` over SMB, then Manual Import it.
   - Isolation: `docker exec sabnzbd ls /data/tv/library` fails.
   - File mode: `getfacl` on a fresh download shows `mask::rw-`.
11. **ADR 0009 amendment:**
   - Extend "the shared group plus setgid directories give access within it" with: "and a default ACL that keeps new files group-writable (every Service runs at umask 0022); no named-user ACL entries; one UID per app Service, for attribution".
   - Note that Organizers copy instead of hardlinking from `torrents/` (Sonarr, Radarr and Shelfmark), so that a Downloader keeps no access to Library inodes.
   - GLOSSARY: add the SMB Ingest Folders to the Ingest Folder example.

### `identities.tsv` (the runbook from #544)

```diff
-group  media      1002  true
-group  sonarr     4001  false
-group  radarr     4002  false
-group  sabnzbd    4003  false
-group  navidrome  4004  false
-group  plex       4005  false
-group  emby       4006  false
+group  media      1002  false

-user   sonarr     4001  sonarr     media
-user   radarr     4002  radarr     media
-user   sabnzbd    4003  sabnzbd    media
-user   navidrome  4004  navidrome  media
-user   plex       4005  plex       media
-user   emby       4006  emby       media
+user   sonarr          4001  media  -
+user   radarr          4002  media  -
+user   sabnzbd         4003  media  -
+user   navidrome       4004  media  -
+user   plex            4005  media  -
+user   emby            4006  media  -
+user   qbittor         4007  media  -
+user   cwa             4008  media  -
+user   shelfmark       4009  media  -
+user   audiobookshelf  4010  media  -
+user   nextpvr         4011  media  -
+user   ownfoil         4012  media  -
+user   autobrr         4013  media  -
+user   media           1001  media  -
```

- **Rename `books` first.** The runbook matches identities by name and skips names that exist, so `cwa` would otherwise fail with "uid 4008 taken by books". Rename `books` to `cwa` in the TrueNAS UI before running it.
- **Move primary groups by hand.** The runbook doesn't change existing users, so moving them to `media` is a manual step.
- **Keep `qbittor`,** the existing Host name.
- **Record `media` (1001)** so the file matches the Host. It exists already, so the runbook skips it. Its SMB access is turned off by hand in step 2, because the runbook doesn't change existing users.
- **Update `host/README.md`'s runbook paragraph** ("one user and group per Service … 4001-4006") to match.

### Follow-ups (not #543)

- Collapse `MEDIA_UID/GID`, `COMMON_*`, `HOME_*` and `MONITORING_*` into one name for 568.
- **Add a Music Organizer** (for example Lidarr or beets) with its own UID and a `music/ingest` share. Until then Music takes no new files (decision 7).

## Decisions

The user's answers on 2026-10-18 to the first version's open questions:

1. **Container escape is out of the threat model.** So (d) stands, and (a) isn't added.
2. **Accept that qBittorrent can rewrite, but not delete, a Library file it seeds.** The recommendation above now removes even that, at no cost, by copying.
3. **SMB:** allow drops into an Ingest Folder, with no SMB access to the Libraries ([design](#smb-ingest-folders)).
4. **Delete groups 4001–4008 on the Host** as a manual, low-priority step.
5. **Reset every hand-made ACL** and start clean ([procedure](#acl-reset-procedure)).
6. **Be consistent about identities:** one UID per Service everywhere if that adds value, otherwise consolidate. [It adds attribution](#do-per-service-uids-add-value), so the answer is one UID per media app Service, and `books` is split.

The adversarial review raised three points, which this revision addresses:

- **Imports keep the Downloader's inode (high).** A new section, [pathnames versus inode contents](#pathnames-versus-inode-contents), with E9. Option (b) is corrected.
- **The mode-mask pitfall also applies to the default ACL in (d) (medium).** It now applies to both (c) and (d). The app settings were verified on the Host and the required ones are listed. Copying removes the dependency for imports (E10).
- **E3 ran in cloned mount namespaces (medium).** It is redone with real containers exposing only the intended mounts, with negative read, write, unlink and `mount` checks.

The user's answers on 2026-10-18 to the second version's open questions:

7. **No Music share.** Music has no Organizer, so it gets no Ingest Folder and keeps no SMB access. It takes no new files until a Music Organizer is added ([follow-ups](#follow-ups-not-543)).
8. **Nothing logs in over SMB as the user `media` (1001).** Turn off its SMB access. Keep the user as the no-login owner of its existing files, with no `chown` in #543.
9. **Turn Sonarr's Use Hardlinks off.** Imports from `torrents/` become copies, which should be block clones (to be confirmed). The re-review added Shelfmark's `HARDLINK_TORRENTS_AUDIOBOOK` to the same change.
10. **Accept the usenet inode residual:** files SABnzbd hands over while compromised.

## Open questions

None remain. #543 checks two facts while it carries out the changes:
- Whether Samba honours `media` group membership while the group has `smb=false`. If the first test write is denied, set it to `true` ([SMB Ingest Folders](#smb-ingest-folders)).
- Whether each Organizer's copy is a block clone: `bclonesaved` grows on its first real import. If not, each torrent import costs a full copy ([Pathnames versus inode contents](#pathnames-versus-inode-contents)).

## Appendix: experiments

### E3: isolation by mounts, with real containers

`bash isolation.sh` on any Docker 26 or later. Each Service runs as `--user <uid>:1002 --cap-drop ALL --security-opt no-new-privileges`, with only its `volume-subpath` mounts.

<details>
<summary>Output</summary>

```text
== E3 SABnzbd: usenet folders only
  sabnzbd:      media mounts: ['/data/movies/usenet', '/data/tv/usenet']
  sabnzbd: OK    write own folder /data/tv/usenet/ep.mkv
  sabnzbd: FAIL  read  /data/tv/library/old.mkv -> No such file or directory
  sabnzbd: FAIL  read  /data/tv/usenet/../library/old.mkv -> No such file or directory
  sabnzbd: FAIL  write /data/tv/library/new.mkv -> No such file or directory
  sabnzbd: FAIL  unlink /data/tv/library/old.mkv -> No such file or directory
  sabnzbd: FAIL  mount(2) a tmpfs to get out -> Operation not permitted
  sabnzbd:      search whole filesystem for old.mkv -> none
  sabnzbd:      search whole filesystem for film.mkv -> none
== E3 Sonarr: the whole TV tree as one mount
  sonarr:      media mounts: ['/data/tv']
  sonarr: OK    rename usenet/ep.mkv -> library/ep.mkv
  sonarr: OK    link torrents/seed.mkv -> library/seed.mkv
  sonarr: FAIL  unlink /data/movies/library/film.mkv -> No such file or directory
  sonarr:      search whole filesystem for film.mkv -> none
== E3 Sonarr with split mounts (rejected in ADR 0009)
  sonarr-split: FAIL  rename torrents -> library -> Cross-device link
  sonarr-split: FAIL  link   torrents -> library -> Cross-device link
== E3 Plex: both Libraries read-only (and a member of media, which has rwx on them)
  plex: OK    read  /data/tv/old.mkv
  plex: FAIL  write /data/tv/old.mkv -> Read-only file system
  plex: FAIL  unlink /data/movies/film.mkv -> Read-only file system
== E3 state of the Libraries afterwards
  tv/library/old.mkv 5 bytes nlink 1
  tv/library/ep.mkv 1 bytes nlink 1
  tv/library/seed.mkv 1 bytes nlink 2
  movies/library/film.mkv 5 bytes nlink 1
```

</details>

<details>
<summary>Script</summary>

```bash
#!/usr/bin/env bash
# E3: isolation by mounts, with real containers that expose only the bind mounts #543 would give.
# Reproduce on any Docker >= 26 (volume-subpath): bash isolation.sh
# Each Service: --user <uid>:1002 (member of media), --cap-drop ALL, no-new-privileges.
set -u
V=hops552-media
IMG=python:3.13-alpine
docker pull -q $IMG >/dev/null; docker volume rm -f $V >/dev/null; docker volume create $V >/dev/null
docker run --rm -i -v $V:/m alpine:3 sh -s <<'EOF'
apk add -q acl >/dev/null 2>&1
cd /m
mkdir -p tv/library tv/usenet tv/torrents movies/library movies/usenet movies/torrents
chown 4001:1002 tv tv/library; chown 4002:1002 movies movies/library
chown 4003:1002 tv/usenet movies/usenet; chown 4007:1002 tv/torrents movies/torrents
find . -mindepth 1 -type d -exec chmod 2770 {} + -exec setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- {} +
for f in tv/library/old.mkv movies/library/film.mkv; do echo keep > $f; chmod 660 $f; done
chown 4001:1002 tv/library/old.mkv; chown 4002:1002 movies/library/film.mkv
EOF
# svc <label> <uid> <subpath:target[:ro]>... -- <python statements>
svc() {
  local label=$1 uid=$2; shift 2; local m=()
  while [ "$1" != -- ]; do
    IFS=: read -r sub dst ro <<<"$1"
    m+=(--mount "type=volume,src=$V,dst=$dst,volume-subpath=$sub${ro:+,readonly}"); shift
  done; shift
  docker run --rm --user "$uid:1002" --cap-drop ALL --security-opt no-new-privileges "${m[@]}" $IMG \
    python3 -c "$PY
$*" 2>&1 | sed "s|^|  $label: |"
}
PY='
import os, ctypes
libc = ctypes.CDLL(None, use_errno=True)
def mount_tmpfs():
    if libc.mount(b"x", b"/mnt", b"tmpfs", 0, None) != 0:
        e = ctypes.get_errno(); raise OSError(e, os.strerror(e))
def t(desc, f, *a):
    try: f(*a); print("OK   ", desc)
    except OSError as e: print("FAIL ", desc, "->", e.strerror)
def w(p, s=b"x"):
    with open(p, "wb") as fh: fh.write(s)
def r(p): open(p, "rb").read()
def find(name):
    hits = [os.path.join(d, name) for top in ("/",) for d, _, fs in os.walk(top) if not d.startswith(("/proc", "/sys")) and name in fs]
    print("     search whole filesystem for", name, "->", hits or "none")
def mounts():
    print("     media mounts:", sorted(l.split()[4] for l in open("/proc/self/mountinfo") if "/data" in l.split()[4]))
'
echo "== E3 SABnzbd: usenet folders only"
svc sabnzbd 4003 tv/usenet:/data/tv/usenet movies/usenet:/data/movies/usenet -- '
mounts()
t("write own folder /data/tv/usenet/ep.mkv", w, "/data/tv/usenet/ep.mkv")
t("read  /data/tv/library/old.mkv", r, "/data/tv/library/old.mkv")
t("read  /data/tv/usenet/../library/old.mkv", r, "/data/tv/usenet/../library/old.mkv")
t("write /data/tv/library/new.mkv", w, "/data/tv/library/new.mkv")
t("unlink /data/tv/library/old.mkv", os.unlink, "/data/tv/library/old.mkv")
t("mount(2) a tmpfs to get out", mount_tmpfs)
find("old.mkv"); find("film.mkv")'
echo "== E3 Sonarr: the whole TV tree as one mount"
svc sonarr 4001 tv:/data/tv -- '
mounts()
t("rename usenet/ep.mkv -> library/ep.mkv", os.rename, "/data/tv/usenet/ep.mkv", "/data/tv/library/ep.mkv")
w("/data/tv/torrents/seed.mkv")
t("link torrents/seed.mkv -> library/seed.mkv", os.link, "/data/tv/torrents/seed.mkv", "/data/tv/library/seed.mkv")
t("unlink /data/movies/library/film.mkv", os.unlink, "/data/movies/library/film.mkv")
find("film.mkv")'
echo "== E3 Sonarr with split mounts (rejected in ADR 0009)"
svc sonarr-split 4001 tv/torrents:/data/torrents tv/library:/data/library -- '
w("/data/torrents/s2.mkv")
t("rename torrents -> library", os.rename, "/data/torrents/s2.mkv", "/data/library/s2.mkv")
t("link   torrents -> library", os.link, "/data/torrents/s2.mkv", "/data/library/s2b.mkv")'
echo "== E3 Plex: both Libraries read-only (and a member of media, which has rwx on them)"
svc plex 4005 tv/library:/data/tv:ro movies/library:/data/movies:ro -- '
t("read  /data/tv/old.mkv", r, "/data/tv/old.mkv")
t("write /data/tv/old.mkv", w, "/data/tv/old.mkv")
t("unlink /data/movies/film.mkv", os.unlink, "/data/movies/film.mkv")'
echo "== E3 state of the Libraries afterwards"
docker run --rm -v $V:/m:ro alpine:3 sh -c 'cd /m; for f in tv/library/old.mkv tv/library/ep.mkv tv/library/seed.mkv movies/library/film.mkv; do stat -c "  %n %s bytes nlink %h" $f; done'
docker volume rm $V >/dev/null
```

</details>

### E1, E2, E4–E12: permissions, ACLs, inodes, ACL reset

`docker run --rm -i --privileged alpine:3 sh -s < experiment.sh`. This runs on an ext4 loopback image inside the container. Each actor is a different UID under `setpriv` with all capabilities dropped and umask `022`.

<details>
<summary>Output</summary>

```text
protected_hardlinks=1

== E1 (d) shared group media, setgid 2770, NO default ACL, umask 022 (as on the Host)
  OK    sabnzbd: mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv && stat -c "%U:%G %a" tv/usenet/job/ep.mkv
  file: 4003:1002 644
  FAIL  sonarr: mv tv/usenet/job/ep.mkv tv/library/ep.mkv -> Permission denied
  FAIL  sonarr: rmdir tv/usenet/job -> Directory not empty
  OK    qbittorrent: mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv
  FAIL  sonarr: ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv -> Operation not permitted

== E2 (d) as E1 plus default ACL d:g::rwx,d:m::rwx,d:o::--- (the 'umask' part of today's Host ACLs)
  OK    sabnzbd: mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv
  file: 4003:1002 660
  OK    sonarr: mv tv/usenet/job/ep.mkv tv/library/ep.mkv
  OK    sonarr: rmdir tv/usenet/job
  imported: 4003:1002 660 (keeps the Downloader uid)
  OK    qbittorrent: mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv
  OK    sonarr: ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv
  OK    qbittorrent: cat tv/torrents/t/ep2.mkv >/dev/null
  -- in-tree, the shared group alone does not stop other members (mounts must):
  OK    radarr: rm tv/library/ep.mkv
  OK    sabnzbd: echo junk > tv/library/ep2.mkv
  OK    qbittorrent: echo junk >> tv/torrents/t/ep2.mkv
  hardlinked library file now 10 bytes, nlink 2

== E4 (a) group per Library: tv=5001, movies=5002; Downloader owns its folder, is in no Library group
  OK    sabnzbd: mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv
  file: 4003:5001 660
  OK    sonarr: mv tv/usenet/job/ep.mkv tv/library/ep.mkv
  OK    sonarr: rmdir tv/usenet/job
  OK    qbittorrent: mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv
  OK    sonarr: ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv
  OK    qbittorrent: cat tv/torrents/t/ep2.mkv >/dev/null
  FAIL  radarr: ls tv/library -> Permission denied
  FAIL  sabnzbd: ls tv/library -> Permission denied
  OK    qbittorrent: echo junk >> tv/torrents/t/ep2.mkv
  -- a Media Server must join every Library group it presents (with, then without, group tv):
  OK    plex: ls tv/library >/dev/null
  FAIL  plex: ls tv/library >/dev/null -> Permission denied

== E5 (c) ACLs: default ACE u:sonarr:rwx on download folders, no shared group
  OK    sabnzbd: mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv
  file ACL: user::rw- user:4001:rwx	#effective:rw- group::--- mask::rw- other::---
  OK    sonarr: mv tv/usenet/job/ep.mkv tv/library/ep.mkv
  OK    sonarr: rmdir tv/usenet/job
  after rename (ACL travels with the inode): user::rw- user:4001:rwx	#effective:rw- group::--- mask::rw- other::---
  OK    qbittorrent: mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv
  OK    sonarr: ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv
  -- mask pitfall: an app that creates with mode 0644 caps the named ACE at r--:
  OK    qbittorrent: fsop create 644 tv/torrents/t/x
  user:4001:rwx	#effective:r-- mask::r--
  FAIL  sonarr: fsop link tv/torrents/t/x tv/library/x -> link -> Operation not permitted
  OK    sonarr: fsop rename tv/torrents/t/x tv/library/x

== E6 Host-style deny-list ACL on a Library (u:radarr:---, group media rwx): fails open
  FAIL  radarr: ls tv/library -> Permission denied
  FAIL  sabnzbd: ls tv/library -> Permission denied
  OK    qbittorrent: rm tv/library/ep.mkv

== E7 (b) Organizer re-owns on import
  OK    sabnzbd: echo x > tv/usenet/ep.mkv
  FAIL  sonarr: chown 4001 tv/usenet/ep.mkv -> Operation not permitted
  FAIL  sonarr: chgrp 1002 tv/usenet/ep.mkv -> Operation not permitted
  OK    sonarr+CAP_CHOWN: chown 4001 (and could chown anything it can see, to anyone)

== E8 (e) idmapped bind mount at the kernel level (util-linux X-mount.idmap=u:<disk>:<mount>:<n>)
  on disk: 4003:1002 600
  Sonarr sees through the idmapped mount: 4001:1002 600
  OK    sonarr: cat /scratch/idm/ep.mkv >/dev/null
  OK    sonarr: echo y > /scratch/idm/new.mkv
  file Sonarr created through it, on disk: 4003:1002

== E9 inode retention: a compromised Downloader keeps a link to a file it hands over
  OK    sabnzbd: mkdir tv/usenet/job && echo payload > tv/usenet/job/ep.mkv && ln tv/usenet/job/ep.mkv tv/usenet/.kept
  OK    sonarr: mv tv/usenet/job/ep.mkv tv/library/ep.mkv
  OK    sabnzbd: : > tv/usenet/.kept
  move (rename): library file now 0 bytes, inode shared with the Downloader (nlink 2)
  -- (b) re-owning after the rename does not help: group write and the link remain
  OK    sabnzbd: echo payload > tv/usenet/ep3.mkv && ln tv/usenet/ep3.mkv tv/usenet/.kept3
  OK    sonarr: mv tv/usenet/ep3.mkv tv/library/ep3.mkv
  OK    sabnzbd: : > tv/usenet/.kept3
  chowned to 4001: library file now 0 bytes
  -- copy into a fresh inode, then delete the source:
  OK    sabnzbd: echo payload > tv/usenet/ep2.mkv && ln tv/usenet/ep2.mkv tv/usenet/.kept2
  OK    sonarr: cp tv/usenet/ep2.mkv tv/library/.ep2.partial && mv tv/library/.ep2.partial tv/library/ep2.mkv && rm tv/usenet/ep2.mkv
  OK    sabnzbd: : > tv/usenet/.kept2
  copy: library file still 8 bytes, nlink 1

== E10 mode-mask pitfall applies to (d) too: the creating app's mode caps the ACL mask
  OK    qbittorrent: fsop create 644 tv/torrents/a.mkv
  created 0644: group::rwx	#effective:r-- mask::r--
  FAIL  sonarr: fsop link tv/torrents/a.mkv tv/library/a.mkv -> link -> Operation not permitted
  OK    sonarr: fsop rename tv/torrents/a.mkv tv/library/a2.mkv
  OK    qbittorrent: fsop create 666 tv/torrents/b.mkv
  created 0666 (libtorrent): mask::rw-
  OK    sonarr: fsop link tv/torrents/b.mkv tv/library/b.mkv
  OK    sabnzbd: fsop create 644 tv/usenet/c.mkv && chmod 660 tv/usenet/c.mkv
  created 0644 then chmod 660 (SABnzbd permissions=2770): mask::rw-
  OK    sonarr: fsop link tv/usenet/c.mkv tv/library/c.mkv
  OK    sabnzbd: chmod 644 tv/usenet/c.mkv
  OK    sonarr: cat tv/usenet/c.mkv >/dev/null && fsop rename tv/usenet/c.mkv tv/library/c2.mkv

== E11 SMB-style writer into an Ingest Folder (create mask 0664, directory mask 0775, as smb4.conf)
  OK    owner: fsop mkdir 775 tv/ingest/Show && fsop create 664 tv/ingest/Show/ep.mkv
  dropped dir: 1000:1002 2770
  dropped file: 1000:1002 660
  OK    sonarr: mv tv/ingest/Show/ep.mkv tv/library/ep.mkv
  OK    sonarr: rmdir tv/ingest/Show
  FAIL  owner-not-in-media: ls tv/ingest -> Permission denied

== E12 ACL reset of a Host-like tree: backup, dry run, reset, verify, roll back
  before: user::rw- user:4001:rwx	#effective:rw- user:4002:--- user:4003:--- group::rwx	#effective:rw- mask::rw- other::---
  before mode 660
  inventory: 4 inodes with extended ACLs, 21 named-user entries
  dry run prints: tv/library/Show/ep.mkv: u::rw-,g::rw-,o::---,*
  dry run changed nothing: yes
  after setfacl -b: file mode 660 (group bits take the old mask)
  normalized: file 4003:1002 660
  verify: named-user entries left: 0
  OK    sonarr: fsop create 644 tv/library/Show/new.nfo >/dev/null && mv tv/library/Show/ep.mkv tv/library/Show/ep2.mkv
  rollback: setfacl --restore reproduces the backup exactly: yes
```

</details>

<details>
<summary>Script</summary>

```sh
#!/bin/sh
# Reproduce: docker run --rm -i --privileged alpine:3 sh -s < experiment.sh
# Runs E1, E2, E4-E12 (E3 is isolation.sh).
# Works only on a loopback ext4 image inside the throwaway container; Linux VFS permission,
# POSIX ACL and rename/link rules are the same on ZFS with acltype=posix.
set -u
apk add -q acl setpriv util-linux util-linux-misc e2fsprogs python3 >/dev/null 2>&1
mkdir -p /scratch && truncate -s 64M /img && mkfs.ext4 -q /img && mount -o loop,acl /img /scratch
cd /scratch
cat > /usr/local/bin/fsop <<'PYEOF'
#!/usr/bin/env python3
# fsop rename|link SRC DST, or fsop create|mkdir MODE PATH: one syscall, prints the errno text on failure
import os, sys
op, a = sys.argv[1], sys.argv[2:]
try:
    if op == "create":
        os.close(os.open(a[1], os.O_CREAT | os.O_WRONLY, int(a[0], 8)))
    elif op == "mkdir":
        os.mkdir(a[1], int(a[0], 8))
    else:
        getattr(os, op)(*a)
    print(op, "OK")
except OSError as e:
    print(op, "->", e.strerror); sys.exit(1)
PYEOF
chmod 755 /usr/local/bin/fsop
sysctl -w fs.protected_hardlinks=1 >/dev/null 2>&1; echo "protected_hardlinks=$(cat /proc/sys/fs/protected_hardlinks)"

# as <name> <uid> <gid> <groups|-> <cmd>; prints OK or FAIL with the error
as() {
  n=$1 u=$2 g=$3 gs=$4; shift 4
  if [ "$gs" = - ]; then grp=--clear-groups; else grp="--groups=$gs"; fi
  out=$(setpriv --reuid="$u" --regid="$g" $grp --inh-caps=-all --bounding-set=-all sh -c "umask 022; $*" 2>&1) \
    && echo "  OK    $n: $*" || echo "  FAIL  $n: $* -> $(echo "$out" | sed 's/.*: //' | head -1)"
}
SON="sonarr 4001 1002 1002"; RAD="radarr 4002 1002 1002"; SAB="sabnzbd 4003 1002 1002"; QB="qbittorrent 4007 1002 1002"

reset() { cd /; rm -rf /scratch/m; mkdir -p /scratch/m; cd /scratch/m; }
tree() { # tree <group> <sonarr-gid>
  mkdir -p tv/library tv/usenet tv/torrents movies/library
  chown 4001:$1 tv tv/library; chown 4003:$1 tv/usenet; chown 4007:$1 tv/torrents
  chown 4002:${2:-$1} movies movies/library
  chmod 2770 tv tv/library tv/usenet tv/torrents movies movies/library
}

echo; echo "== E1 (d) shared group media, setgid 2770, NO default ACL, umask 022 (as on the Host)"
reset; tree 1002
as $SAB 'mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv && stat -c "%U:%G %a" tv/usenet/job/ep.mkv'
stat -c '  file: %u:%g %a' tv/usenet/job/ep.mkv
as $SON 'mv tv/usenet/job/ep.mkv tv/library/ep.mkv'
as $SON 'rmdir tv/usenet/job'
as $QB 'mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv'
as $SON 'ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv'

echo; echo "== E2 (d) as E1 plus default ACL d:g::rwx,d:m::rwx,d:o::--- (the 'umask' part of today's Host ACLs)"
reset; tree 1002
for d in tv/usenet tv/torrents tv/library; do setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- $d; done
as $SAB 'mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv'
stat -c '  file: %u:%g %a' tv/usenet/job/ep.mkv
as $SON 'mv tv/usenet/job/ep.mkv tv/library/ep.mkv'
as $SON 'rmdir tv/usenet/job'
stat -c '  imported: %u:%g %a (keeps the Downloader uid)' tv/library/ep.mkv
as $QB 'mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv'
as $SON 'ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv'
as $QB 'cat tv/torrents/t/ep2.mkv >/dev/null'
echo "  -- in-tree, the shared group alone does not stop other members (mounts must):"
as $RAD 'rm tv/library/ep.mkv'
as $SAB 'echo junk > tv/library/ep2.mkv'
as $QB 'echo junk >> tv/torrents/t/ep2.mkv'
stat -c '  hardlinked library file now %s bytes, nlink %h' tv/library/ep2.mkv

# E3 (isolation by mounts) runs as real containers: see isolation.sh.

echo; echo "== E4 (a) group per Library: tv=5001, movies=5002; Downloader owns its folder, is in no Library group"
reset; tree 5001 5002
for d in tv/usenet tv/torrents tv/library; do setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- $d; done
chmod 2771 tv movies  # o+x only: stands in for each Service bind-mounting its own folder (E3)
SONa="sonarr 4001 5001 5001"; RADa="radarr 4002 5002 5002"; SABa="sabnzbd 4003 4003 -"; QBa="qbittorrent 4007 4007 -"
as $SABa 'mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv'
stat -c '  file: %u:%g %a' tv/usenet/job/ep.mkv
as $SONa 'mv tv/usenet/job/ep.mkv tv/library/ep.mkv'
as $SONa 'rmdir tv/usenet/job'
as $QBa 'mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv'
as $SONa 'ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv'
as $QBa 'cat tv/torrents/t/ep2.mkv >/dev/null'
as $RADa 'ls tv/library'
as $SABa 'ls tv/library'
as $QBa 'echo junk >> tv/torrents/t/ep2.mkv'
echo "  -- a Media Server must join every Library group it presents (with, then without, group tv):"
as plex 4005 4005 5001 'ls tv/library >/dev/null'
as plex 4005 4005 - 'ls tv/library >/dev/null'

echo; echo "== E5 (c) ACLs: default ACE u:sonarr:rwx on download folders, no shared group"
reset; tree 4001
chown 4003:4003 tv/usenet; chown 4007:4007 tv/torrents; chgrp 4001 tv tv/library
for d in tv/usenet tv/torrents; do setfacl -m u:4001:rwx,d:u:4001:rwx,d:u::rwx,d:g::---,d:m::rwx,d:o::--- $d; done
chmod 2771 tv
SONc="sonarr 4001 4001 -"; SABc="sabnzbd 4003 4003 -"; QBc="qbittorrent 4007 4007 -"
as $SABc 'mkdir tv/usenet/job && echo x > tv/usenet/job/ep.mkv'
getfacl -cp tv/usenet/job/ep.mkv 2>/dev/null | tr '\n' ' ' | sed 's/^/  file ACL: /'; echo
as $SONc 'mv tv/usenet/job/ep.mkv tv/library/ep.mkv'
as $SONc 'rmdir tv/usenet/job'
getfacl -cp tv/library/ep.mkv 2>/dev/null | tr '\n' ' ' | sed 's/^/  after rename (ACL travels with the inode): /'; echo
as $QBc 'mkdir tv/torrents/t && echo x > tv/torrents/t/ep2.mkv'
as $SONc 'ln tv/torrents/t/ep2.mkv tv/library/ep2.mkv'
echo "  -- mask pitfall: an app that creates with mode 0644 caps the named ACE at r--:"
as $QBc 'fsop create 644 tv/torrents/t/x'
getfacl -cp tv/torrents/t/x 2>/dev/null | grep -E 'user:4001|mask' | tr '\n' ' ' | sed 's/^/  /'; echo
as $SONc 'fsop link tv/torrents/t/x tv/library/x'
as $SONc 'fsop rename tv/torrents/t/x tv/library/x'

echo; echo "== E6 Host-style deny-list ACL on a Library (u:radarr:---, group media rwx): fails open"
reset; mkdir -p tv/library; chown -R 4001:1002 tv; chmod 2770 tv tv/library
setfacl -R -m u:4002:---,u:4003:---,d:u:4002:---,d:u:4003:---,d:g::rwx,d:m::rwx,d:o::--- tv
echo x > tv/library/ep.mkv; chown 4001:1002 tv/library/ep.mkv
as $RAD 'ls tv/library'
as $SAB 'ls tv/library'
as $QB 'rm tv/library/ep.mkv'

echo; echo "== E7 (b) Organizer re-owns on import"
reset; tree 1002
for d in tv/usenet tv/library; do setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- $d; done
as $SAB 'echo x > tv/usenet/ep.mkv'
as $SON 'chown 4001 tv/usenet/ep.mkv'
as $SON 'chgrp 1002 tv/usenet/ep.mkv'
setpriv --reuid=4001 --regid=1002 --groups=1002 --inh-caps=-all,+chown --ambient-caps=+chown --bounding-set=-all,+chown sh -c 'chown 4001 tv/usenet/ep.mkv && echo "  OK    sonarr+CAP_CHOWN: chown 4001 (and could chown anything it can see, to anyone)"'

echo; echo "== E8 (e) idmapped bind mount at the kernel level (util-linux X-mount.idmap=u:<disk>:<mount>:<n>)"
reset; mkdir -p tv/usenet; chown 4003:1002 tv/usenet; chmod 700 tv/usenet
echo x > tv/usenet/ep.mkv; chown 4003:1002 tv/usenet/ep.mkv; chmod 600 tv/usenet/ep.mkv
mkdir -p /scratch/idm
mount --bind -o X-mount.idmap='u:4003:4001:1 g:1002:1002:1' /scratch/m/tv/usenet /scratch/idm
stat -c '  on disk: %u:%g %a' tv/usenet/ep.mkv
setpriv --reuid=4001 --regid=1002 --groups=1002 sh -c 'stat -c "  Sonarr sees through the idmapped mount: %u:%g %a" /scratch/idm/ep.mkv'
as $SON 'cat /scratch/idm/ep.mkv >/dev/null'
as $SON 'echo y > /scratch/idm/new.mkv'
stat -c '  file Sonarr created through it, on disk: %u:%g' tv/usenet/new.mkv
umount /scratch/idm

echo; echo "== E9 inode retention: a compromised Downloader keeps a link to a file it hands over"
reset; tree 1002
for d in tv/usenet tv/library; do setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- $d; done
as $SAB 'mkdir tv/usenet/job && echo payload > tv/usenet/job/ep.mkv && ln tv/usenet/job/ep.mkv tv/usenet/.kept'
as $SON 'mv tv/usenet/job/ep.mkv tv/library/ep.mkv'
as $SAB ': > tv/usenet/.kept'
stat -c '  move (rename): library file now %s bytes, inode shared with the Downloader (nlink %h)' tv/library/ep.mkv
echo "  -- (b) re-owning after the rename does not help: group write and the link remain"
as $SAB 'echo payload > tv/usenet/ep3.mkv && ln tv/usenet/ep3.mkv tv/usenet/.kept3'
as $SON 'mv tv/usenet/ep3.mkv tv/library/ep3.mkv'
chown 4001 tv/library/ep3.mkv
as $SAB ': > tv/usenet/.kept3'
stat -c '  chowned to %u: library file now %s bytes' tv/library/ep3.mkv
echo "  -- copy into a fresh inode, then delete the source:"
as $SAB 'echo payload > tv/usenet/ep2.mkv && ln tv/usenet/ep2.mkv tv/usenet/.kept2'
as $SON 'cp tv/usenet/ep2.mkv tv/library/.ep2.partial && mv tv/library/.ep2.partial tv/library/ep2.mkv && rm tv/usenet/ep2.mkv'
as $SAB ': > tv/usenet/.kept2'
stat -c '  copy: library file still %s bytes, nlink %h' tv/library/ep2.mkv

echo; echo "== E10 mode-mask pitfall applies to (d) too: the creating app's mode caps the ACL mask"
reset; tree 1002
for d in tv/usenet tv/torrents tv/library; do setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- $d; done
as $QB 'fsop create 644 tv/torrents/a.mkv'
getfacl -cp tv/torrents/a.mkv 2>/dev/null | grep -E '^group::|mask' | tr '\n' ' ' | sed 's/^/  created 0644: /'; echo
as $SON 'fsop link tv/torrents/a.mkv tv/library/a.mkv'
as $SON 'fsop rename tv/torrents/a.mkv tv/library/a2.mkv'
as $QB 'fsop create 666 tv/torrents/b.mkv'
getfacl -cp tv/torrents/b.mkv 2>/dev/null | grep -E 'mask' | tr '\n' ' ' | sed 's/^/  created 0666 (libtorrent): /'; echo
as $SON 'fsop link tv/torrents/b.mkv tv/library/b.mkv'
as $SAB 'fsop create 644 tv/usenet/c.mkv && chmod 660 tv/usenet/c.mkv'
getfacl -cp tv/usenet/c.mkv 2>/dev/null | grep -E 'mask' | tr '\n' ' ' | sed 's/^/  created 0644 then chmod 660 (SABnzbd permissions=2770): /'; echo
as $SON 'fsop link tv/usenet/c.mkv tv/library/c.mkv'
as $SAB 'chmod 644 tv/usenet/c.mkv'
as $SON 'cat tv/usenet/c.mkv >/dev/null && fsop rename tv/usenet/c.mkv tv/library/c2.mkv'

echo; echo "== E11 SMB-style writer into an Ingest Folder (create mask 0664, directory mask 0775, as smb4.conf)"
reset; tree 1002
mkdir tv/ingest; chown 4001:1002 tv/ingest; chmod 2770 tv/ingest
setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- tv/ingest
JB="owner 1000 1001 1001,1002"
as $JB 'fsop mkdir 775 tv/ingest/Show && fsop create 664 tv/ingest/Show/ep.mkv'
stat -c '  dropped dir: %u:%g %a' tv/ingest/Show; stat -c '  dropped file: %u:%g %a' tv/ingest/Show/ep.mkv
as $SON 'mv tv/ingest/Show/ep.mkv tv/library/ep.mkv'
as $SON 'rmdir tv/ingest/Show'
JBX="owner-not-in-media 1000 1001 1001"
as $JBX 'ls tv/ingest'

echo; echo "== E12 ACL reset of a Host-like tree: backup, dry run, reset, verify, roll back"
reset; mkdir -p tv/library/Show; chown -R 4001:1002 tv; chmod 2770 tv tv/library tv/library/Show
setfacl -R -m u:4002:---,u:4003:---,u:4005:r-x,d:u:4001:rwx,d:u:4002:---,d:u:4003:---,d:g::rwx,d:m::rwx,d:o::--- tv
fsop create 660 tv/library/Show/ep.mkv >/dev/null; chown 4003:1002 tv/library/Show/ep.mkv
getfacl -cp tv/library/Show/ep.mkv | tr '\n' ' ' | sed 's/^/  before: /'; echo; stat -c '  before mode %a' tv/library/Show/ep.mkv
getfacl -R -p tv > /scratch/acl-backup.txt
echo "  inventory: $(getfacl -R -s -p tv | grep -c '^# file:') inodes with extended ACLs, $(getfacl -R -p tv | grep -cE '^(default:)?user:[0-9]') named-user entries"
setfacl --test -R -b tv | grep 'ep.mkv' | sed 's/^/  dry run prints: /'
echo "  dry run changed nothing: $(getfacl -R -p tv | cmp -s - /scratch/acl-backup.txt && echo yes)"
setfacl -R -b tv
stat -c '  after setfacl -b: file mode %a (group bits take the old mask)' tv/library/Show/ep.mkv
find tv -type d -exec chmod 2770 {} + ; find tv -type f -exec chmod 0660 {} +
find tv -type d -exec setfacl -m d:u::rwx,d:g::rwx,d:m::rwx,d:o::--- {} +
stat -c '  normalized: file %u:%g %a' tv/library/Show/ep.mkv
echo "  verify: named-user entries left: $(getfacl -R -p tv | grep -cE '^(default:)?user:[0-9]')"
as $SON 'fsop create 644 tv/library/Show/new.nfo >/dev/null && mv tv/library/Show/ep.mkv tv/library/Show/ep2.mkv'
mv tv/library/Show/ep2.mkv tv/library/Show/ep.mkv; rm tv/library/Show/new.nfo
setfacl --restore=/scratch/acl-backup.txt
echo "  rollback: setfacl --restore reproduces the backup exactly: $(getfacl -R -p tv | cmp -s - /scratch/acl-backup.txt && echo yes || echo no)"
```

</details>
