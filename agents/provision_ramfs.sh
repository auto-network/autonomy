#!/bin/bash
# provision_ramfs.sh — narrow, root-only helper for secret_ramfs.py.
#
# Creates a ramfs-backed directory owned by the given uid, mode 0700,
# verified to actually be ramfs (not tmpfs, which is swappable and defeats
# the whole point). ONE authorized target, hardcoded below — not a wildcard:
#   * the single dashboard key-cache mount (/run/autonomy-keycache),
#     KEYCACHE_MOUNT below, matching agents/secret_ramfs.py's own constant
# (Session secret delivery is per-container-private since 2026-08-30 and
# needs no host directory and no root helper.)
#
# This file is version-controlled here for review, but the copy that sudo
# is allowed to run as root must be a SEPARATE, root-owned, non-writable
# install elsewhere (see install command below) — granting sudo on a path
# the calling user can edit is not a privilege boundary, it's theater.
#
# Usage: provision_ramfs.sh <path> <uid>

set -euo pipefail

RAMFS_MAGIC=858458f6
TMPFS_MAGIC=01021994
KEYCACHE_MOUNT=/run/autonomy-keycache

path="${1:?usage: provision_ramfs.sh <path> <uid>}"
uid="${2:?usage: provision_ramfs.sh <path> <uid>}"

# Only digits for uid; only a safe path shape — this runs as root, be strict.
[[ "$uid" =~ ^[0-9]+$ ]] || { echo "REFUSE: uid must be numeric, got: $uid" >&2; exit 2; }
[[ "$path" != *".."* ]] || { echo "REFUSE: path contains '..': $path" >&2; exit 2; }

if [[ "$path" == "$KEYCACHE_MOUNT" ]]; then
    # Single dedicated mount, one consumer (the dashboard) — no multi-tenant
    # base-dir scheme needed, just mount it directly and lock it to the
    # given uid.
    mkdir -p "$path"
    if ! mountpoint -q "$path"; then
        mount -t ramfs ramfs "$path"
    fi
    chown "$uid:$uid" "$path"
    chmod 0700 "$path"
else
    echo "REFUSE: path must be $KEYCACHE_MOUNT, got: $path" >&2
    exit 2
fi

magic=$(stat -f -c %t "$path")
case "$magic" in
    "$RAMFS_MAGIC") : ;;
    "$TMPFS_MAGIC")
        echo "REFUSE: $path is tmpfs (0x$TMPFS_MAGIC), swappable — a secret here can reach disk" >&2
        exit 4
        ;;
    *)
        echo "REFUSE: $path is not ramfs (magic 0x$magic)" >&2
        exit 3
        ;;
esac

echo "provisioned: $path uid=$uid magic=0x$magic"
