# host

What runs on the TrueNAS Host itself. doco-cd does not deploy any of it; install or run it by hand.

- `startup-order.pin`: the pinned release of the startup-order Custom Component, which starts and
  stops Stacks in order from TrueNAS Init/Shutdown Scripts (runbook below; see also the main
  [README](../README.md#startup-order)).
- `create_usersgroups.sh` + `identities.tsv`: the users and groups the Stacks run as (runbook below).

## Runbook: install or upgrade startup-order

The startup-order Custom Component lives in
the Repo Tooling repo, under `components/startup-order`
(configuration, exit status and logs are in its README). The Host runs the release pinned in
[`startup-order.pin`](startup-order.pin), installed at `/mnt/luna/homeops/bin/startup-order.sh`: on a pool
dataset, so a TrueNAS update keeps it, and apart from its state and logs in `/mnt/luna/homeops/startup/`.
Every step is safe to re-run.

1. Copy `startup-order.pin` to the Host. As root, in that directory, download the pinned release and
   install it only if its sha256 matches:

   ```sh
   . ./startup-order.pin
   dest=/mnt/luna/homeops/bin/startup-order.sh
   mkdir -p /mnt/luna/homeops/bin
   curl -fsSL -o "$dest.new" \
     "https://github.com/jbruns/homelab-components/releases/download/$STARTUP_ORDER_TAG/startup-order.sh"
   echo "$STARTUP_ORDER_SHA256  $dest.new" | sha256sum -c - &&
     chmod 0755 "$dest.new" && mv "$dest.new" "$dest" || rm -f "$dest.new"
   ```

   `sha256sum` prints `OK` on a match. On a mismatch it prints `FAILED`, the download is deleted and the
   installed script is unchanged.
2. Check that `flock` resolves: `sh -c 'command -v flock'` prints `/usr/bin/flock` (util-linux, part of
   TrueNAS SCALE). The script needs it for every command and exits 2 without it. Init/Shutdown commands
   run as `sh -c` with middlewared's `PATH`, the same one that finds `/usr/bin/docker`.
3. Under System → Advanced → Init/Shutdown Scripts, have exactly these two entries:

   | Type | When | Command | Timeout |
   |---|---|---|---|
   | Command | Post Init (`POSTINIT`) | `STARTUP_ORDER_DIR=/mnt/luna/homeops/startup sh /mnt/luna/homeops/bin/startup-order.sh start` | 60 |
   | Command | Shutdown (`SHUTDOWN`) | `STARTUP_ORDER_DIR=/mnt/luna/homeops/startup sh /mnt/luna/homeops/bin/startup-order.sh stop` | 300 |

   `start` returns at once and leaves `run` in the background, so 60 s is plenty. `stop` may wait 30 s
   (`STARTUP_ORDER_STOP_WAIT`) for a boot run to give up, then runs one `docker stop` per level, each
   bounded by the longest stop grace period at that level: 10 s at levels 3, 2 and 0, 60 s at level 1
   (`nextcloud-mariadb`, `vaultwarden-mariadb`). That is 120 s at worst; 300 s leaves headroom (keep it at least 180 s). Raise it if a Service
   gets a longer `stop_grace_period`.
4. Remove the lock directory the old in-repo script used, once no old run is left:
   `pgrep -f startup-order.sh || rmdir /mnt/luna/homeops/startup/lock 2>/dev/null; true`.
   The new script locks with `flock` on `run.lock`, which is never removed.
5. Check what the next boot would do; this changes nothing:

   ```sh
   STARTUP_ORDER_DIR=/mnt/luna/homeops/startup sh /mnt/luna/homeops/bin/startup-order.sh plan
   ```

After a reboot, `shutdown.log` and `last.log` in `/mnt/luna/homeops/startup/` show what it did.

To upgrade, merge the Renovate PR that bumps `startup-order.pin` (the new tag and its sha256), then run
step 1 again. Renovate never automerges it, because the Host does not pick up the new release on its own.

## Runbook: create users and groups

The identities are listed in [`identities.tsv`](identities.tsv): the `media` group (gid 1002), one
user per media app Service (4001-4013) with `media` as its primary group and no other groups, and
the no-login `media` user (1001) that owns files added over SMB before #543. There are no
per-Service groups: access inside a Library tree comes from `media` (see
[0552](../docs/research/0552-unix-identities.md)). To change the set, edit only that file.

The script only creates what is missing. It never renames, re-groups or deletes, so those are done
by hand in the TrueNAS UI (Credentials), for example renaming `books` to `cwa` before a run, moving
an existing user's primary group, deleting an unused group or turning off a user's SMB access. The
#543 cutover lists each one in its [runbook](../docs/runbooks/0543-library-isolation.md).

1. Copy `create_usersgroups.sh` and `identities.tsv` to the Host, side by side.
2. As root, preview what would change:

   ```sh
   ./create_usersgroups.sh --dry-run
   ```

   It prints the `cli -c '...'` command for each missing identity and `# skip:` for each one
   that already exists with the right id.
3. Run it for real:

   ```sh
   ./create_usersgroups.sh
   ```

It is safe to re-run: an identity whose name already exists (per `getent`) is skipped. It exits
1 and prints `ERROR:` if a name exists with a different id, an id belongs to another name, a `cli`
command fails, or a user's groups are missing; everything else is still created. Resolve those by
hand in the TrueNAS UI (Credentials) and run it again. `--file PATH` uses another identities file.

### Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest host
```

The test runs the script with stub `cli` and `getent` commands on `PATH`; it needs no Host.
