#!/usr/bin/env bash
# Inventory of the auto.network Hetzner project: estate VMs + the pet.
# Usage: list-vms.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
. ./lib.sh
resolve_token

api GET "/servers?per_page=50" | "$ESTATE_PY" -c '
import json, sys
for s in json.load(sys.stdin)["servers"]:
    labels = s["labels"]
    kind = "estate" if labels.get("managed-by") == "auto-network-estate" else "PET"
    ip = (s["public_net"]["ipv4"] or {}).get("ip", "-")
    role = labels.get("role", "-")
    print(f'"'"'{s["id"]:<10} {s["name"]:<20} {s["status"]:<8} {ip:<16} {kind:<7} {role}'"'"')
'
