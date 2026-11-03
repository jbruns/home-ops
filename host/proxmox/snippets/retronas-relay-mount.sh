#!/bin/bash
# Hookscript of the RetroNAS relay (guests.yaml, #760). Proxmox VE runs it on the Proxmox Host as
# `<script> <vmid> <phase>`, and a failing pre-start stops the relay from starting. The relay starts only while the
# TrueNAS Host's retronas export is mounted at the path it bind-mounts, so RetroNAS never serves, or accepts writes
# into, an empty local directory (ADR 0019). Installed as local:snippets/retronas-relay-mount.sh, mode 0755.
set -u

vmid=${1:?vmid}
phase=${2:?phase}
storage=retronas
mnt=/mnt/pve/$storage
export_path=/mnt/andromeda/retronas
sentinel=.retronas-export

refuse() {
  echo "retronas-relay-mount: refusing to start $vmid: $1" >&2
  exit 1
}

[[ $phase == pre-start ]] || exit 0

if ! mountpoint -q "$mnt"; then
  # Proxmox VE mounts an NFS storage when it first activates it; do that now rather than wait for pvestatd.
  timeout 30 pvesm status --storage "$storage" >/dev/null 2>&1 || true
fi
mountpoint -q "$mnt" || refuse "$mnt is not mounted"

fstype=$(findmnt -n -o FSTYPE --mountpoint "$mnt") || refuse "can't read the mount at $mnt"
source=$(findmnt -n -o SOURCE --mountpoint "$mnt") || refuse "can't read the mount at $mnt"
[[ $fstype == nfs4 ]] || refuse "$mnt is $fstype, not nfs4"
[[ $source == *":$export_path" ]] || refuse "$mnt is not the retronas export"

# A hung NFS server must refuse the start, not hang it.
first=$(timeout 10 find "$mnt" -mindepth 1 -maxdepth 1 -print -quit) || refuse "$mnt doesn't answer"
[[ -n $first ]] || refuse "$mnt is empty"
timeout 10 test -f "$mnt/$sentinel" || refuse "$mnt has no $sentinel"

exit 0
