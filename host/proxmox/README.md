# host/proxmox

The Proxmox Host's declared state: its own settings that no Kept Guest declares, and its Kept Guests, each with
the purpose it serves (ADR 0019). A change is a PR here. Once it merges, the agent applies it over SSH with
`sudo -n pvesh`, `qm` or `pct` and checks the result with `hops proxmox call`. It never reads with `pvesh`, which
prints credentials unredacted.

Before each batch, the agent runs vzdump of every Kept Guest the batch touches. A change that could cut off access,
such as a bridge or firewall change, keeps a console-reachable way back.

## Files

One YAML file per area. The top-level keys are the area's items. Items and settings are named as the Proxmox API
names them. The inventory fills these in.

| File | Items |
|---|---|
| `guests.yaml` | Kept Guests: ID, kind (VM or container), resources, disks, network, mounts, idmap, boot order |
| `storage.yaml` | storage definitions, including the TrueNAS Host's `retronas` export and the Device Backup share |
| `network.yaml` | bridges, VLANs, addresses, the firewall |
| `access.yaml` | the agent's user, its token and their ACLs |
| `backup.yaml` | the vzdump job and the update notification |

## Rules

The rules of [`host/truenas`](../truenas/README.md#rules) apply here too: every item has a `purpose:`, only
settings that matter are listed, lists are complete, addresses and logins are named by role, and no secrets.

- **Guests.** A Guest exists on the Proxmox Host only if `guests.yaml` lists it or it carries the `scratch` tag.
  Any other Guest is a Stray. A Scratch Guest is never listed. It becomes a Kept Guest by being listed, and is
  backed up from then on.
- **Networks.** Only the relay may have interfaces on both the retro network and the main LAN.
