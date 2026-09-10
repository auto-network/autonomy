"""Clear the cumulative Fleet counters the operator sees, without a race.

The first version of this mutated the counters in place. That cannot be correct
here: the dashboard runs the reset while four connector processes are
read-modify-writing the same telemetry rows on every serve, and both sides take
a ``threading`` lock, which is process-local. Measured 2026-09-10, one reset
cleared 3,348 failed attempts and 1.27 GB received, moved 52.7 GB sent only as
far as 28.0 GB, and left transactions applied untouched -- a lost update, worst
on the counters written most often.

A cross-process lock is the wrong answer. It would put a settings-store lock in
the path of every sync completion, so the bookkeeping would contend with the
work it describes.

So nothing is mutated. The reset writes ONE baseline row per peer and scope,
holding what the counters read at that moment, and the view subtracts. The
connectors keep writing monotonically, the subtraction is exact however much
traffic is in flight, and the real lifetime totals survive on disk.
"""

from __future__ import annotations

from tools.network import (
    fleet_counter_baseline,
    fleet_sync_peer_scope,
    fleet_sync_telemetry,
)


def reset_counters(*, org: str = "machine") -> dict:
    """Record what the displayed counters read now, so the view can subtract.

    Machine-local by construction: every store read is this machine's own, so
    another dashboard's counters are unaffected.
    """
    entries: dict[tuple[str, str], dict] = {}

    # Machine-wide totals, keyed under the sentinel scope.
    for peer, totals in fleet_sync_telemetry.read_peer_totals(org=org).items():
        entries[(peer, fleet_counter_baseline.MACHINE_SCOPE)] = {
            "bytes_sent": totals.get("bytes_sent"),
            "bytes_received": totals.get("bytes_received"),
            "attempts_failed": totals.get("failed_iterations"),
        }

    # Per-organization byte totals, at the grain they are displayed.
    for peer, scopes in fleet_sync_peer_scope.read_peer_scopes(org=org).items():
        for row in scopes:
            entries[(peer, row["scope"])] = {
                "bytes_sent": row.get("bytes_out"),
                "bytes_received": row.get("bytes_in"),
            }

    written = fleet_counter_baseline.record(entries, org=org)
    return {"baselineRows": written}
