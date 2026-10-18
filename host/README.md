# host

Scripts that run on the TrueNAS Host itself. doco-cd does not deploy them; copy or run them by hand.

- `startup-order.sh`: Init/Shutdown Scripts that start and stop Stacks in order. See the main
  [README](../README.md#startup-order).
- `create_usersgroups.sh` + `identities.tsv`: the users and groups the Stacks run as (runbook below).

## Runbook: create users and groups

The identities are listed in [`identities.tsv`](identities.tsv): the `media` group (gid 1002) and
one user and group per Service that needs a fixed id (sonarr, radarr, sabnzbd, navidrome, plex,
emby: 4001-4006), each user also in `media`. To change the set, edit only that file.

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
