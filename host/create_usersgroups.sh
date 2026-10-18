#!/usr/bin/env bash
# Creates the users and groups listed in host/identities.tsv on the TrueNAS Host with `cli`.
# Safe to re-run: an identity that already exists (by name, per getent) is skipped. A name that
# exists with a different id, an id taken by another name, or a failed cli command is reported
# and fails the run; the remaining identities are still created, except users whose groups are
# missing.
#
# Usage: host/create_usersgroups.sh [--dry-run] [--file PATH]
#   --dry-run  print the cli commands instead of running them
#   --file     identities file (default: identities.tsv beside this script)
set -euo pipefail

usage() {
    echo "usage: $0 [--dry-run] [--file PATH]" >&2
    exit 2
}

dry_run=0
file="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/identities.tsv"
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --file) [ $# -ge 2 ] || usage; file="$2"; shift ;;
        *) usage ;;
    esac
    shift
done
[ -r "$file" ] || { echo "cannot read $file" >&2; exit 2; }
command -v getent >/dev/null || { echo "getent not found; run this on the Host" >&2; exit 2; }
[ "$dry_run" -eq 1 ] || command -v cli >/dev/null || { echo "cli not found; run this on the Host" >&2; exit 2; }

bad() {
    echo "$file:$line_no: $1" >&2
    exit 2
}

groups=()
users=()
line_no=0
while IFS= read -r line || [ -n "$line" ]; do
    line_no=$((line_no + 1))
    line="${line%%#*}"
    # shellcheck disable=SC2086
    set -- $line
    [ $# -gt 0 ] || continue
    case "$1" in
        group)
            [ $# -eq 4 ] || bad "expected: group <name> <gid> <smb>"
            [[ "$3" =~ ^[0-9]+$ ]] || bad "gid must be a number"
            [[ "$4" =~ ^(true|false)$ ]] || bad "smb must be true or false"
            groups+=("$2 $3 $4")
            ;;
        user)
            [ $# -eq 5 ] || bad "expected: user <name> <uid> <primary group> <groups|->"
            [[ "$3" =~ ^[0-9]+$ ]] || bad "uid must be a number"
            users+=("$2 $3 $4 $5")
            ;;
        *) bad "unknown kind '$1' (group or user)" ;;
    esac
done <"$file"

status=0
# Groups that exist or were just created, space-delimited.
present=" "

error() {
    echo "ERROR: $1" >&2
    status=1
}

run() {
    echo "cli -c '$1'"
    [ "$dry_run" -eq 1 ] || cli -c "$1" || { error "cli failed: $1"; return 1; }
}

# ensure <group|user> <name> <id> <command>: run command unless name exists; flag id mismatches.
# Returns 0 if the name exists or was created.
ensure() {
    local kind="$1" name="$2" id="$3" command="$4" db=group id_name=gid entry owner
    [ "$kind" = group ] || { db=passwd; id_name=uid; }
    if entry="$(getent "$db" "$name")"; then
        local have
        have="$(echo "$entry" | cut -d: -f3)"
        if [ "$have" = "$id" ]; then
            echo "# skip: $kind $name exists ($id_name $id)"
        else
            error "$kind $name exists with $id_name $have, expected $id"
        fi
    elif entry="$(getent "$db" "$id")"; then
        owner="$(echo "$entry" | cut -d: -f1)"
        error "$id_name $id for $kind $name is taken by $owner"
        return 1
    else
        run "$command"
    fi
}

for g in ${groups[@]+"${groups[@]}"}; do
    read -r name gid smb <<<"$g"
    if ensure group "$name" "$gid" "account group create gid=$gid name=$name smb=$smb"; then
        present="$present$name "
    fi
done

for u in ${users[@]+"${users[@]}"}; do
    read -r name uid primary extra <<<"$u"
    supplementary=""
    [ "$extra" = "-" ] || supplementary=" groups=$extra"
    missing=""
    for g in "$primary" $( [ "$extra" = "-" ] || echo "$extra" | tr ',' ' ' ); do
        case "$present" in
            *" $g "*) ;;
            *) getent group "$g" >/dev/null || missing="$missing $g" ;;
        esac
    done
    if [ -n "$missing" ]; then
        error "user $name not created: missing group(s)$missing"
        continue
    fi
    ensure user "$name" "$uid" "account user create username=$name full_name=$name uid=$uid group_create=false home_create=false password_disabled=true home=/var/empty group=$primary$supplementary smb=false shell=/usr/sbin/nologin" || true
done

exit "$status"
