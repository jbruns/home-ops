# proxmox-access

A wizard the Operator runs once, and again after rebuilding the Proxmox Host, to grant the agent its access there
(ADR 0019):

- an SSH login using the agent's key, whose `sudo -n` allows only `pvesh`, `qm`, `pct`, `vzdump`, `tee` to the
  idmap files, Guest configs and snippets, and `pvesm set truenas-backup --password`, which reads the Device Backup
  share's password from stdin (`pvesh` takes it only as an argument, which `ps` and sudo's log would show). Writes
  go through these.
- `hops-reader@pve` and its privilege-separated token `hops`, holding only `PVEAuditor` on `/`. `hops proxmox`
  reads through it.

```sh
tools/proxmox-access/grant-access.sh
```

Run it from the repo root with console access to the Proxmox Host at hand. It writes only the `HOPS_PROXMOX_*` keys
in `.env`, and the token secret goes straight there without being shown. It checks that SSH and the scoped sudo work,
that other `sudo` commands are refused, that the token can read, that its effective privileges are PVEAuditor's only, and that it is refused a write (403).
Re-running it keeps existing values unless you choose to replace them.
