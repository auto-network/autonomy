#!/usr/bin/env bash
# Restore a backup-zone.sh artifact into a FRESH primary — refuses a box
# that already has zone data (restore never overlays, mirroring the
# portability tool's posture). After restore, the secondary is forced to
# retransfer and both serials are proven equal.
#
#   restore-zone.sh root@<primary-ip> <backup.sqlite3> <secondary-ip>

set -euo pipefail
PRIMARY=${1:?usage: restore-zone.sh root@<primary-ip> <backup.sqlite3> <secondary-ip>}
BACKUP=${2:?path to pdns-<stamp>.sqlite3}
SECONDARY_IP=${3:?secondary IPv4}
ZONE="serve.auto.network"
[ -s "$BACKUP" ] || { echo "backup file missing/empty: $BACKUP" >&2; exit 1; }

if ssh -o IdentitiesOnly=yes "$PRIMARY" \
    "pdnsutil list-zone $ZONE >/dev/null 2>&1"; then
    echo "refusing: $PRIMARY already serves $ZONE — restore only into a" \
         "fresh primary (tear down or move the existing DB first)" >&2
    exit 1
fi

scp -o IdentitiesOnly=yes -q "$BACKUP" "$PRIMARY":/tmp/pdns-restore.sqlite3
ssh -o IdentitiesOnly=yes "$PRIMARY" bash -s <<'EOF'
set -euo pipefail
systemctl stop pdns
install -o pdns -g pdns -m 0640 /tmp/pdns-restore.sqlite3 \
    /var/lib/powerdns/pdns.sqlite3
systemctl start pdns
EOF

echo "== forcing secondary retransfer and proving serial equality"
ssh -o IdentitiesOnly=yes "root@$SECONDARY_IP" \
    "pdns_control retrieve $ZONE" || true
PRIMARY_IP=${PRIMARY#*@}
serial_p=$(dig +short "@$PRIMARY_IP" "$ZONE" SOA | awk '{print $3}')
for _ in $(seq 60); do
    serial_s=$(dig +short "@$SECONDARY_IP" "$ZONE" SOA | awk '{print $3}')
    [ "$serial_s" = "$serial_p" ] && break
    sleep 5
done
[ "${serial_s:-}" = "$serial_p" ] || {
    echo "secondary never converged after restore" >&2; exit 1; }
echo "restore complete: both endpoints at serial $serial_p"
