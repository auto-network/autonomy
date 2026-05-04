"""One-shot SQL cleanup for retired settings-mediator scaffolding.

Bead auto-rc27t collapsed the mediator into a thin handler registry and
retired the cursor + state schemas (``dashboard.action-registry-cursor``
and ``dashboard.action-registry-state``). Their backing rows are
harmless once the new loop is deployed — but they sit in every org DB
forever. This script deletes them.

Run once after the deploy that lands the new loop:

    python -m tools.dashboard.scripts.sweep_dead_mediator_rows

Idempotent: re-running on a swept DB is a no-op.
"""
from __future__ import annotations

import sys

from tools.graph import org_ops, settings_ops


_DEAD_SET_IDS = (
    "dashboard.action-registry-cursor",
    "dashboard.action-registry-state",
)


def _sweep_one(org: str | None) -> int:
    db = settings_ops._open(org)
    try:
        cur = db.conn.execute(
            "DELETE FROM settings WHERE set_id IN (?, ?)",
            _DEAD_SET_IDS,
        )
        deleted = cur.rowcount
        db.conn.commit()
        return deleted
    finally:
        db.close()


def main() -> int:
    total = 0
    for ref in org_ops.list_orgs():
        deleted = _sweep_one(ref.slug)
        print(f"  org={ref.slug}: removed {deleted} dead row(s)")
        total += deleted
    print(f"swept {total} dead mediator row(s) total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
