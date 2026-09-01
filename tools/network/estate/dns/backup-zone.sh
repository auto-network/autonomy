#!/usr/bin/env bash
# Pull a self-contained backup of the primary's zone state to the local
# machine (estate boxes hold no long-term backups): a live-safe sqlite
# .backup of the whole database PLUS a canonical text export of the zone,
# timestamped. The challenge ledger rides along (advisory state only).
#
#   backup-zone.sh root@<primary-ip> <outdir>

set -euo pipefail
PRIMARY=${1:?usage: backup-zone.sh root@<primary-ip> <outdir>}
OUTDIR=${2:?usage: backup-zone.sh root@<primary-ip> <outdir>}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ZONE="serve.auto.network"
mkdir -p "$OUTDIR"

ssh -o IdentitiesOnly=yes "$PRIMARY" bash -s <<'EOF'
set -euo pipefail
sqlite3 /var/lib/powerdns/pdns.sqlite3 ".backup /tmp/pdns-backup.sqlite3"
pdnsutil list-zone serve.auto.network > /tmp/pdns-zone-export.txt
cp /var/lib/autonomy-dns/challenges.json /tmp/pdns-challenges.json 2>/dev/null \
    || echo '{}' > /tmp/pdns-challenges.json
EOF
scp -o IdentitiesOnly=yes -q \
    "$PRIMARY":/tmp/pdns-backup.sqlite3 \
    "$OUTDIR/pdns-$STAMP.sqlite3"
scp -o IdentitiesOnly=yes -q \
    "$PRIMARY":/tmp/pdns-zone-export.txt \
    "$OUTDIR/$ZONE-$STAMP.zone"
scp -o IdentitiesOnly=yes -q \
    "$PRIMARY":/tmp/pdns-challenges.json \
    "$OUTDIR/challenges-$STAMP.json"
sha256sum "$OUTDIR"/*"$STAMP"* | tee "$OUTDIR/SHA256-$STAMP"
echo "backup complete: $OUTDIR (*-$STAMP)"
