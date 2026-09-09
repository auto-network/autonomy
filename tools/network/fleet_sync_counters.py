"""Clear the cumulative Fleet counters the operator sees, and nothing else.

The four numbers the Fleet screen calls resettable — received, sent, changes
applied and failed attempts — do not share one home.  Bytes and failed
attempts are in the machine-local telemetry Settings, per-organization bytes
are in the peer/scope Settings, and changes applied is a column on
``fleet_sync_peer_state`` in each scope's database.

Which makes the hazard worth stating once, here: those rows are not display
records.  A telemetry row also carries ``acknowledged_transaction_ref`` and
the resume breadcrumb trail, which a restored source uses to recompute its
position from content rather than from renumbered row ids; a peer-state row
carries the watermarks, roster epoch and error the scheduler reads to decide
what to request next.  Clearing a number an operator is looking at must never
cost a peer its place in the stream.  Every write here is therefore
field-level: no row is deleted and no payload is replaced wholesale.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.network import fleet_sync_peer_scope, fleet_sync_telemetry


def _peer_state_paths() -> list[Path]:
    """The personal database plus every organization scope on this machine."""
    from tools.graph.db import _org_db_path
    from tools.network.fleet_sync_scheduler import discover_org_sync_scopes

    paths = [Path(_org_db_path("personal"))]
    paths.extend(discover_org_sync_scopes().values())
    return [path for path in paths if path.exists()]


def _reset_transactions_applied(path: Path) -> int:
    """Zero ``transactions_applied`` in one scope's peer-state table.

    ``fleet_sync_peer_state`` is a LOCAL-policy table, so writing it authors
    no mutation and this cannot feed sync.  Only the one column moves: the
    watermarks beside it are how the scheduler resumes.
    """
    with sqlite3.connect(path) as conn:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_peer_state'"
        ).fetchone()
        if present is None:
            return 0
        cursor = conn.execute(
            "UPDATE fleet_sync_peer_state SET transactions_applied=0 "
            "WHERE transactions_applied>0"
        )
        return int(cursor.rowcount or 0)


def reset_counters(*, org: str = "machine") -> dict:
    """Clear the cumulative totals the Fleet screen displays.

    Machine-local by construction: every store touched is this machine's own,
    so another dashboard's counters are unaffected.
    """
    return {
        "telemetryRows": fleet_sync_telemetry.reset_byte_totals(org=org),
        "peerScopeRows": fleet_sync_peer_scope.reset_byte_totals(org=org),
        "peerStateRows": sum(
            _reset_transactions_applied(path) for path in _peer_state_paths()
        ),
    }
