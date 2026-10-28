# host/truenas

The Host Configuration: the Host's own TrueNAS settings that no Stack declares, each with the purpose it serves
(ADR 0015). doco-cd does not deploy any of it. A change is a PR here. Once it merges, the agent applies it with
`midclt`, then runs the drift check until it prints nothing:

```sh
uv run tools/truenas-drift/truenas_drift.py --unchecked
```

It prints one line per difference between this record and the Host, and lists what it could not compare
(see [`tools/truenas-drift`](../../tools/truenas-drift/README.md)).

## Files

One YAML file per area. The top-level keys are the area's items. Items and settings are named as the TrueNAS
API (JSON-RPC) names them.

| File | Items | Read with |
|---|---|---|
| `system.yaml` | general, advanced, update, NTP, init/shutdown scripts, tunables, cron jobs | `system.general.config`, `system.advanced.config`, `initshutdownscript.query`, `tunable.query`, `cronjob.query` |
| `storage.yaml` | pools, datasets, Periodic Snapshot tasks, scrubs, disk power settings | `pool.query`, `pool.dataset.query`, `pool.snapshottask.query`, `pool.scrub.query`, `disk.query` |
| `sharing.yaml` | SMB and NFS service settings, SMB shares, NFS exports | `smb.config`, `nfs.config`, `sharing.smb.query`, `sharing.nfs.query` |
| `services.yaml` | which services run, and their settings (SSH) | `service.query`, `ssh.config` |
| `identities.yaml` | users and groups other than the media identities and TrueNAS's built-ins | `user.query`, `group.query` |
| `network.yaml` | interfaces, addresses, routes | `interface.query`, `network.configuration.config` |
| `identifiers.sops.yaml` | the values behind role names: `addresses.<role>`, `logins.<role>` and `names.<role>` (domain, certificate) | (SOPS, Operator Key only) |
| `device-logins.sops.yaml` | `passwords.<device>`: each Device Backup login's SMB password, entered on the device | (SOPS, Operator Key only) |

The media identities stay in [`../identities.tsv`](../identities.tsv). Users and groups that TrueNAS creates
itself are not listed.

## Rules

- **Every item has a `purpose:`.** It says what the item is for, in one line. An item that has outlived its
  purpose says so and names the issue that removes it, e.g. `purpose: none; removed by #723`. That issue's
  PR deletes the item from the record.
- **Only settings that matter are listed.** An item lists the settings its purpose depends on, plus any that
  differ from TrueNAS's defaults. Anything not listed is left at TrueNAS's default.
- **Lists are complete.** Datasets, shares, exports, snapshot tasks, scripts, cron jobs, tunables and
  non-built-in users and groups exist on the Host only if they are listed here.
- **Addresses, the domain and the Operator's login are named by role** (ADR 0013). An address is written
  `address: <role>`, for the Host's own addresses and for each device a share or export is limited to. A
  name that holds the domain is written `name: <role>`. The Operator's login and its group are written
  `operator`. The real values are under `addresses.<role>`, `names.<role>` and `logins.<role>` in
  `identifiers.sops.yaml`. SOPS leaves the keys in plain text, so read the file itself to see which roles
  exist; decrypt it only to use a value, never to print one.
- **No secrets.** Passwords, keys and certificates never go here. Neither does anything else the API reports
  as a credential.
- **Settings are written in a form the drift check can compare.** Sizes are `100 GiB`. A schedule is a cron
  dict, e.g. `{minute: '0', hour: '2', dow: mon}`; omitted fields are `*`. `permissions` gives `mode`, `owner`
  and `group` of the dataset's top directory, every ACL entry beyond the mode (`acl`, `default_acl`, or
  `nfs4_acl` on an NFSv4 dataset) in `getfacl` form, and the same for subdirectories under `paths`. A
  directory with no ACL entries listed must have none. Notes the check doesn't read go in comments.
