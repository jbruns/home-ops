#!/bin/bash
# Hookscript of the RetroNAS relay (guests.yaml, #760). Proxmox VE runs it on the Proxmox Host as
# `<script> <vmid> <phase>`, and a failing pre-start stops the relay from starting. The relay starts only while the
# TrueNAS Host's retronas export is mounted at the path it bind-mounts, so RetroNAS never serves, or accepts writes
# into, an empty local directory (ADR 0019). Installed as local:snippets/retronas-relay-mount.sh, mode 0755.
#
# Pre-start runs synchronously, and anything that touches a hard NFS mount whose server is gone blocks. So the mount's
# identity comes from /proc/self/mountinfo, which never touches the export; every probe that does runs under
# `timeout -k`; and the whole check has a hard deadline. NFS waits are killable, so SIGKILL ends them.
set -u

vmid=${1:?vmid}
phase=${2:?phase}
storage=retronas
mnt=/mnt/pve/$storage
export_path=/mnt/andromeda/retronas
sentinel=.retronas-export
deadline=60  # seconds for the whole check; each probe has less

refuse() {
  echo "retronas-relay-mount: refusing to start $vmid: $1" >&2
  exit 1
}

[[ $phase == pre-start ]] || exit 0

if [[ -z ${RETRONAS_RELAY_MOUNT_CHECK:-} ]]; then
  RETRONAS_RELAY_MOUNT_CHECK=1 timeout -k 5 "$deadline" "$0" "$vmid" "$phase"
  rc=$?
  ((rc == 124 || rc == 137)) && refuse "the mount check took longer than ${deadline}s"
  exit "$rc"
fi

# Prints "<fstype> <source>" of the topmost mount at $mnt from mountinfo (the fields after " - "), or nothing; no stat().
mount_identity() {
  awk -v m="$mnt" '$5 == m { for (i = 7; i <= NF; i++) if ($i == "-") { id = $(i + 1) " " $(i + 2); break } }
    END { if (id != "") print id }' /proc/self/mountinfo
}

identity=$(mount_identity)
if [[ -z $identity ]]; then
  # Proxmox VE mounts an NFS storage when it first activates it; do that now rather than wait for pvestatd.
  timeout -k 5 30 pvesm status --storage "$storage" >/dev/null 2>&1
  identity=$(mount_identity)
fi
[[ -n $identity ]] || refuse "$mnt is not mounted"

read -r fstype source <<<"$identity"
[[ $fstype == nfs4 ]] || refuse "$mnt is $fstype, not nfs4"
[[ $source == *":$export_path" ]] || refuse "$mnt is not the retronas export"

first=$(timeout -k 5 10 find "$mnt" -mindepth 1 -maxdepth 1 -print -quit) || refuse "$mnt doesn't answer"
[[ -n $first ]] || refuse "$mnt is empty"
timeout -k 5 10 test -f "$mnt/$sentinel" || refuse "$mnt has no $sentinel"

exit 0
