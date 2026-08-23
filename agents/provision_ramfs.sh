#!/bin/bash
# provision_ramfs.sh — narrow, root-only helper for secret_ramfs.py.
#
# Creates a ramfs-backed directory owned by the given uid, mode 0700,
# verified to actually be ramfs (not tmpfs, which is swappable and defeats
# the whole point). Two authorized targets, both hardcoded below — not a
# wildcard, not "anywhere under /run":
#   * a per-session subdir under BASE_MOUNT (/run/autonomy-secrets/<name>)
#   * the single dashboard key-cache mount (/run/autonomy-keycache),
#     KEYCACHE_MOUNT below, matching agents/secret_ramfs.py's own constant
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
BASE_MOUNT=/run/autonomy-secrets
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
elif [[ "$path" == "$BASE_MOUNT"/* ]]; then
    # Self-healing: ensure the base ramfs mount exists before touching the
    # per-session subdir under it. This script runs as real root on the host
    # (no container, no nsenter, no ptrace_scope dependency needed) — a plain
    # mount is sufficient here; the nsenter dance in secret_ramfs.py exists only
    # for the case where the CALLER is itself inside a container.
    mkdir -p "$BASE_MOUNT"
    if ! mountpoint -q "$BASE_MOUNT"; then
        mount -t ramfs ramfs "$BASE_MOUNT"
    fi
    # 0711, not 0700: the base dir must be TRAVERSABLE (search-only) by everyone
    # so a process can reach its own uid-owned subdir under it, but not LISTABLE
    # by anyone but root — nobody can enumerate other sessions' subdir names.
    # Isolation between sessions comes from each subdir being 0700-per-uid below
    # this, not from locking the base dir itself. Idempotent: corrects a
    # previously-wrong permission on every run, not just at mount time.
    chmod 0711 "$BASE_MOUNT"

    mkdir -p "$path"
    chown "$uid:$uid" "$path"
    chmod 0700 "$path"
else
    echo "REFUSE: path must be $KEYCACHE_MOUNT or under $BASE_MOUNT/, got: $path" >&2
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
