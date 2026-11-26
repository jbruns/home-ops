# RomM

RomM presents the Games Library: browsing, metadata and artwork, in-browser play, save sync and companion apps. It is a
Media Server (ADR 0009). It mounts `games/library` read-only as its `roms/` (the Library Folders Stage Select builds,
one per platform) and `games/bios` read-only as its `bios/`. Stage Select, the Games Organizer, is the only thing that
puts games there. RomM signs users in at the IdP itself (native OIDC, ADR 0006). It keeps its state in MariaDB and
Valkey, both in this Stack, and keeps saves, states, screenshots and artwork in `romm_data`. Its configuration is
`romm/compose.yaml` plus `romm/config.yml`, which RomM reads but cannot change.

Address: `https://rmm.<domain>`. Ownfoil, not RomM, serves the Switch titles.

## Before the first Deploy

1. In `romm/romm.sops.env`, replace each `OPERATOR_SETS` with its value: the IdP client (below), and the IGDB
   (Twitch app), ScreenScraper, SteamGridDB and RetroAchievements accounts. For example,
   `sops set tn/romm/romm/romm.sops.env '["SCREENSCRAPER_USER"]' '"<value>"'`. Every other secret is already
   generated.
2. On the Host, create the data volumes and give each to the user its Service runs as. `romm_cache` holds only
   caches, but it is external so that it can be chowned:

   ```sh
   for v in romm_data romm_cache romm_mariadb_data romm_valkey_data; do sudo docker volume create "$v"; done
   sudo chown 4014:1002 "$(sudo docker volume inspect -f '{{.Mountpoint}}' romm_data)" \
     "$(sudo docker volume inspect -f '{{.Mountpoint}}' romm_cache)"
   sudo chown 999:999 "$(sudo docker volume inspect -f '{{.Mountpoint}}' romm_mariadb_data)"
   sudo chown 999:1000 "$(sudo docker volume inspect -f '{{.Mountpoint}}' romm_valkey_data)"
   ```

3. Create `games/bios` as `host/truenas/storage.yaml` declares it (mode `2750`, owner operator, group gaming). Put
   BIOS files in it as `bios/<platform>/<file>`, using RomM's platform names (e.g. `bios/psx/scph5501.bin`). The bind
   is never created by the Deploy, so a missing folder fails it.
4. Create the `romm` user (uid 4014, group media) with `host/create_usersgroups.sh`, as host/README.md describes. It
   only records which Service owns RomM's files; the containers run without it.

## The IdP

Make an OAuth2/OpenID provider and an application with slug `romm`, as for the other native-OIDC Services:

- Redirect URI `https://rmm.<domain>/api/oauth/openid`, exactly, with no trailing slash. Implicit consent.
- The client ID and secret go into `romm.sops.env`. The issuer, `OIDC_SERVER_APPLICATION_URL`, is already there:
  `https://<idp>/application/o/romm`, without `.well-known`.
- A scope mapping for scope `email` that returns `{"email": user.email, "email_verified": True}`. It replaces the
  default `email` mapping on this provider. The IdP's 2025.10 release sends `email_verified: false` by default, and
  then RomM refuses every sign-in silently.
- The `groups` claim comes from the default `profile` mapping. RomM asks for `openid profile email groups`.
- Two groups. `romm-users` lets someone in as a user, and `romm-admins` makes them an admin. A person in neither group
  is refused, and membership is read again at every sign-in. Bind the same IdP gate policy as the other applications.

## First run

1. Open `https://rmm.<domain>/login?bypass_autologin=true`. The setup wizard makes the first admin, which is the
   break-glass account: a local login that works when the IdP is down. Keep its password with the Operator's
   credentials.
2. Create the mysqld-exporter's account, once:
   `hops db romm/mariadb -e "CREATE USER 'exporter'@'%' IDENTIFIED BY '<MARIADB_EXPORTER_PASSWORD>' WITH MAX_USER_CONNECTIONS 3; GRANT PROCESS, SLAVE MONITOR ON *.* TO 'exporter'@'%';"`,
   where the password is the one in `.sops.env`.
3. Sign in through the IdP as a `romm-admins` member and check that you land as an admin.
4. In Settings → Users → Permission groups, make the groups children's accounts need, including their parental
   controls. Each child signs in once through the IdP, and then you move their account into the group. Players are
   named only in SOPS (ADR 0021), never in this repo, issues or PRs.
5. Run a scan, and check that `nes` and its games appear, with artwork from ScreenScraper.

## Stage Select

Stage Select asks RomM to scan a platform after each Build, keeps a public "Additions" collection, and compares what
RomM matched with what each Build made. It authenticates as its own RomM user:

1. As the break-glass admin, make a permission group `stage-select`. Give it Tasks write and nothing else that writes.
2. Make a local user `stage-select` in that group. It is not an admin. Sign in as it at
   `/login?bypass_autologin=true`.
3. Under Profile → Client API tokens, make a token with scopes `tasks.run`, `roms.read`, `platforms.read`,
   `collections.read`, `collections.write` and `me.read`, with no expiry.
4. Enter it in Stage Select, as `tn/stage-select/README.md` describes ("RomM").

## Clients

- **Argosy (Android), Grout (muOS/NextUI), Playnite:** pair from the app. Approve its code under Profile → Devices, or
  make a client API token and pair it. These work for IdP-only accounts. Argosy also takes device installs: from a
  game, send it to the device.
- **RetroArch Cloud Sync:** RetroArch 1.22 or later, Settings → Saving → Cloud Sync, driver `webdav`, URL
  `https://rmm.<domain>/api/sync/retroarch/` (the trailing slash matters). It uses the user's RomM username and a
  local password. An IdP-only account has no password, so the user sets one under Profile → Change password. That
  password is a second credential the IdP doesn't control.
- **Feeds:** PS3 `https://rmm.<domain>/api/feeds/pkgi/ps3/game` (also `dlc`, `demo`, `update` and `patch`), and DS
  `https://rmm.<domain>/api/feeds/kekatsu/nds`. Both use HTTP Basic with a local password, as RetroArch does. The DS's
  Wi-Fi only supports WEP or WPA1.

## Notes

- **Memory:** the memory limits of `romm`, `romm-mariadb` and `romm-valkey` are unmeasured guesses. Once a month of
  use has passed, set each to about 1.5 times its 30-day peak (#912).
- **Scans:** the file watcher stays off. Stage Select asks RomM to scan, and a nightly quick scan runs at 04:30.
  Turning the watcher on would double-scan every Build and push Stage Select into backoff.
- **Netplay:** off. It would switch every player to nightly EmulatorJS from a CDN. Turning it on means ICE servers in
  `config.yml`, and a TURN server beyond the LAN, which would need its own decision (ADR 0021).
- **Backup:** `romm_data`, the MariaDB dump and Valkey's hourly snapshot (`romm_valkey_data`) are backed up nightly.
  The Library and BIOS files are covered by the Games dataset's Periodic Snapshots and the Offline Copy, not by the
  Backup.
- **Platforms:** a Library Folder's name is RomM's platform name. If Stage Select ever builds a folder RomM doesn't
  recognise, bind it under `system.platforms` in `config.yml`. RomM 5.5 splits `arcade` into `mame`, `fbneo` and
  `fba`, so check that before bumping past 5.4 with an arcade folder built.
