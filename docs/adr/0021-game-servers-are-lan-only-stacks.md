# Game Servers are LAN-only Stacks, parked between events; Players are named only in SOPS

The Operator hosts LAN parties for children on four Game PCs, playing Minecraft and two other games (#891). We decided that **a Game Server is an ordinary Stack on the TrueNAS Host, reachable only from the home LAN**:

- **LAN-only, published ports.** Game protocols are raw TCP/UDP, which Traefik does not carry, so a Game Server publishes its ports on the TrueNAS Host directly, like Plex and Syncthing. No DNS record names it and no router forward reaches it: Game PCs find it by the Host's LAN address, written into their server lists by the Game PC bootstrap. Opening a Game Server to the WAN is a later decision, made per game when an audience needs it, and needs its own PR.
- **Always on, or Parked.** A Game Server that is played between events (Minecraft) runs as a Warning-Tier Service with a `probe.tcp` Probe. One played only at events is Parked (`deploy.replicas: 0`, `tier: info`) and unparked by PR the day before. Because Backups skip a Parked Service's volumes, it is parked again only after the first nightly Backup following the event, or the event's progress is never backed up.
- **Players are named only in SOPS.** This repo is public and the Players are children. Whitelists, operator lists, game account names and IDs, and which Player sits at which Game PC live only in SOPS-encrypted files, never in plaintext anywhere in this repo, issues or PRs; that extends ADR 0013 from the Operator to every Player.
- **Game PCs are not Hosts.** They run no Stacks and `hops` does not manage them. A rerunnable bootstrap script in this repo builds each one, so the clients and the Game Servers they must match (mod list, addresses) change together in one PR.

## Considered Options

- **Guests on the Proxmox Host**: rejected; it is often off, and a world the Players return to between events must be up and backed up.
- **Stop and start event-only Game Servers with `hops stop`/`hops start`**: rejected; a Host reboot or any Deploy of the Stack starts them again, and the declared state would not say they are meant to be off.
- **DNS names or SRV records**: rejected; this repo declares no DNS, and a hand-made record would be undeclared state for four machines whose bootstrap already knows the address.
- **Game PCs as Hosts in `hosts.yaml`**: rejected; four desktops that only run game clients do not justify Host Configuration, Probes or Ansible.
