# The Proxmox Host is declared here; its Kept Guests back up as Device Backups

A second machine runs Proxmox VE, and nothing records why its settings and Guests exist. It has one permanent job: serving the retro PC network through a Windows Server 2003 VM and the RetroNAS relay, both of which must survive a rebuild. It also runs short-lived trials (a new OS, a project) that nobody needs to keep. It is a hobby machine that is often switched off. We decided that **the Proxmox Host is a second Host of this repo, declared in `host/proxmox/` and applied by the agent the same way as the TrueNAS Host (ADR 0015)**:

- **Declared and applied.** Every item in `host/proxmox/` has a purpose. A change is a PR. Once it merges, the agent applies it over SSH with `sudo -n pvesh`/`qm`/`pct`, and checks the result through `hops proxmox call`, which reads the Proxmox API with an audit-only token and redacts credential fields. `hosts.yaml` declares the Host with no deployer and a `proxmox` capability, and the call/batch front end is shared with `hops truenas`.
- **Kept and Scratch Guests.** A Kept Guest is declared and backed up. A Scratch Guest carries the `scratch` tag, is never declared or backed up, and may be destroyed at any time. A Guest that is neither is a Stray. Scratch Guests may join any network, but only the relay may join both the retro network and the main LAN.
- **Backups.** A weekly vzdump job (`repeat-missed`, so a week the Host was off runs at the next boot) writes uncompressed archives of the Kept Guests, keeping 3, to the Proxmox Host's own Device Backup share on the TrueNAS Host. stack-back carries them offsite like any other Device Backup, which amends ADR 0011's list of copies the TrueNAS Host keeps. They are uncompressed so that restic deduplicates unchanged blocks between weeks. Before each batch of changes, the agent also runs vzdump of every Kept Guest the batch touches.
- **No alerting.** The Proxmox Host being down is normal, so nothing Probes it and nothing Pages about it or its Guests. `hops host` reports Guests, their last backup and pending updates when run. backup-coverage checks that the vzdump archives reach the offsite repository but never notifies about their age. The only thing the Proxmox Host posts is its own available-updates notice, to the Digest.
- **The relay needs its share.** The Proxmox Host mounts the TrueNAS Host's `retronas` export and bind-mounts it into the relay (#760). The relay does not start without the mount, so RetroNAS never serves, or accepts writes into, an empty local directory.

## Considered Options

- **Keep the Proxmox Host outside this repo**: rejected; its Kept Guests depend on the TrueNAS Host's exports, shares and Backups, and nothing would record why its settings exist.
- **Record only, with the Operator applying changes from runbooks**: rejected for the same reason ADR 0015 rejected it for the TrueNAS Host.
- **OpenTofu (bpg/proxmox) or Ansible owning the Guests**: rejected for now; the permanent footprint (two Kept Guests, storage, bridges) does not justify a state file and a second toolchain. Worth revisiting if Scratch Guests should be built from code.
- **Proxmox Backup Server**: rejected; it is a second backup system beside restic, against ADR 0011, for two Guests that rarely change.
- **zstd-compressed vzdump archives**: rejected; each week's archive would differ almost throughout, and restic would upload every Guest in full.
- **Probes and Warning-Tier alerts from the TrueNAS Host**: rejected; they would fire whenever the Operator switches the Proxmox Host off.
