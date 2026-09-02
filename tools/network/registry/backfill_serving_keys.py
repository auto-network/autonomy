"""auto-e2ufw backfill: register existing orgs' serving machine keys.

Option B (graph://a374b260-e4a) closes the transitional accept per org as
its serving-key allow-set becomes non-empty. This host-terminal tool, run
by the operator (who holds the personal root), derives each machine's
per-(org, machine) serving pubkey and registers it with the registry's
allow-set. Idempotent: re-running registers nothing new. It writes ONLY
public keys — the private seed never leaves the operator's root context.

Usage (on the registry host, or against its DB):
    python -m tools.network.registry.backfill_serving_keys \\
        --db /var/lib/autonomy-registry/registry.db \\
        --org <genesis_id> --machine <machine_id> --serving-pub <hex>

The caller derives <hex> with idkit.derive_serving_machine_key(root,
genesis_id, machine_id).public_hex on the operator's machine; this tool
takes the already-derived PUBLIC key so the root never touches the host.
"""

from __future__ import annotations

import argparse
import time

from tools.network.registry.store import RegistryStore


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--org", required=True, help="org genesis/uuid")
    p.add_argument("--machine", required=True,
                   help="machine id (for the audit line only)")
    p.add_argument("--serving-pub", required=True,
                   help="64-hex serving machine PUBLIC key, derived by the "
                        "operator with derive_serving_machine_key")
    a = p.parse_args()
    store = RegistryStore(a.db)
    store.register_serving_machine_key(
        a.org, a.serving_pub, now=int(time.time()))
    n = len(store.registered_serving_keys(a.org))
    print(f"registered serving key for org={a.org[:8]} machine={a.machine[:8]}"
          f"; org now has {n} registered serving key(s)")


if __name__ == "__main__":
    main()
