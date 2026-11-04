# host/proxmox

The Proxmox Host's declared state: its own settings that no Kept Guest declares, and its Kept Guests, each with
the purpose it serves (ADR 0019). A change is a PR here. Once it merges, the agent applies it over SSH with
`sudo -n pvesh`, `qm` or `pct` and checks the result with `hops proxmox call`. It never reads with `pvesh`, which
prints credentials unredacted. A secret never goes in an argument: it comes from SOPS over stdin, into
`pvesm set truenas-backup --password` or `tee /etc/pve/priv/notifications.cfg`.
The agent's login and read token come from [`tools/proxmox-access`](../../tools/proxmox-access/README.md).

Before each batch, the agent runs vzdump of every Kept Guest the batch touches. A change that could cut off access,
such as a bridge or firewall change, keeps a console-reachable way back.

Lessons from applying #760, for every runbook here:

- **Addresses from SOPS may carry a prefix.** `addresses.host-v4` in `../truenas/identifiers.sops.yaml` is an
  interface address in CIDR form, like the other interface addresses there. A command that needs a bare address
  strips the prefix (`v=${v%/*}`) and still never prints it.
- **`pct push` exits 0 even when it fails.** Check every pushed file inside the Guest, by hash or `stat`.
- **The relay has no `pct snapshot`.** Its bind mount fails the snapshot feature check, so its rollback point is a
  vzdump (`--protected 1` for one that must outlive the job's pruning).
- **Inside the relay**, ansible needs `env LC_ALL=C.UTF-8 LANG=C.UTF-8`, and `sysctl --system` exits 1 over keys an
  unprivileged container may not set; check the keys that matter with `sysctl -n` (`guests.yaml`).

## Files

One YAML file per area. The top-level keys are the area's items. Items and settings are named as the Proxmox API
names them. `hops host --host proxmox` and `hops strays --host proxmox` read this record: each Kept Guest's
`vmid` and `type` in `guests.yaml`, and `exclusive_networks` in `network.yaml` (the hops README, "Guests on a
Proxmox VE Host").

| File | Items |
|---|---|
| `guests.yaml` | Kept Guests: ID, kind (VM or container), resources, disks, network, mounts, idmap, boot order |
| `storage.yaml` | storage definitions, including the TrueNAS Host's `retronas` export and the Device Backup share |
| `network.yaml` | the exclusive networks, bridges, VLANs, addresses, the firewall |
| `access.yaml` | the agent's user, its token and their ACLs |
| `backup.yaml` | the vzdump job (#832) and the update notification (#833) |
| `snippets/` | hookscripts, installed as `local:snippets/<name>` with mode 0755 (`retronas-relay-mount.sh`, the relay's pre-start mount check) |

## Rules

The rules of [`host/truenas`](../truenas/README.md#rules) apply here too: every item has a `purpose:`, only
settings that matter are listed, lists are complete, addresses and logins are named by role, and no secrets.

- **Guests.** A Guest exists on the Proxmox Host only if `guests.yaml` lists it or it carries the `scratch` tag.
  Any other Guest is a Stray. A Scratch Guest is never listed. It becomes a Kept Guest by being listed, and is
  backed up from then on.
- **Networks.** Only the relay may have interfaces on both the retro network and the main LAN.
