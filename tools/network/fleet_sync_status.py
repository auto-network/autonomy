"""Read the fleet personal-DB sync status for the operator.

Surfaces the local per-peer sync state (``fleet_sync_peer_state``, written by the
scheduler) and a computed frontier, so "is a missing vault key late, or is sync
just behind?" is one command. Read-only, and never prints key material — the
peer key is a public roster id and nothing here decrypts anything.

Frontier (decision note): the minimum peer watermark among currently ONLINE
peers. When no peer is online, the last established frontier is returned and
marked ``stale`` so a disconnected peer never shows a falsely-current cut.

    python3 -m tools.network.fleet_sync_status [--org personal] [--json]
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

_COLS = (
    "machine_public_key", "roster_epoch", "online", "last_success_ns",
    "peer_watermark", "local_watermark", "bytes_sent", "bytes_received",
    "transactions_applied", "retries", "lag_ns", "last_error_code",
    "updated_at_ns",
)


def read_status(db_path) -> dict:
    """Return the sync status for a personal store, computed read-only."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"cannot open personal store: {exc}",
                "peers": []}
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "fleet_sync_peer_state" not in tables:
            return {"available": False,
                    "reason": "fleet sync is not activated on this store",
                    "peers": []}
        rows = [dict(r) for r in conn.execute(
            "SELECT " + ",".join(_COLS) + " FROM fleet_sync_peer_state "
            "ORDER BY machine_public_key")]
    finally:
        conn.close()

    peers = []
    online_wms = []
    established_wms = []
    for r in rows:
        online = bool(r["online"])
        wm = r["peer_watermark"]
        if online and wm is not None:
            online_wms.append(wm)
        if r["last_success_ns"] is not None and wm is not None:
            established_wms.append(wm)
        peers.append({
            "machine": r["machine_public_key"],
            "online": online,
            "last_success_ns": r["last_success_ns"],
            "peer_watermark": wm,
            "local_watermark": r["local_watermark"],
            "bytes": (r["bytes_sent"] or 0) + (r["bytes_received"] or 0),
            "transactions_applied": r["transactions_applied"],
            "retries": r["retries"],
            "lag_ns": r["lag_ns"],
            "last_error_code": r["last_error_code"],
        })

    if online_wms:
        frontier, stale = min(online_wms), False
    elif established_wms:
        frontier, stale = min(established_wms), True
    else:
        frontier, stale = None, True

    return {
        "available": True,
        "peers": peers,
        "peer_count": len(peers),
        "online_count": sum(1 for p in peers if p["online"]),
        "frontier": frontier,
        "stale": stale,
    }


def _ago(ns: Optional[int], now_ns: int) -> str:
    if ns is None:
        return "never"
    secs = max(0, (now_ns - ns) / 1e9)
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 5400:
        return f"{secs / 60:.0f}m ago"
    return f"{secs / 3600:.0f}h ago"


def format_human(status: dict, *, now_ns: Optional[int] = None) -> str:
    now_ns = time.time_ns() if now_ns is None else now_ns
    if not status.get("available"):
        return f"fleet sync status: {status.get('reason', 'unavailable')}"
    lines = []
    frontier = status["frontier"]
    head = (f"frontier {frontier}" if frontier is not None else "frontier —")
    if status["stale"]:
        head += " (stale: no peer online)"
    lines.append(f"Fleet sync — {status['online_count']}/{status['peer_count']} "
                 f"peers online · {head}")
    if not status["peers"]:
        lines.append("  no peers synced yet")
    for p in status["peers"]:
        mark = "●" if p["online"] else "○"
        err = f" err={p['last_error_code']}" if p["last_error_code"] else ""
        lines.append(
            f"  {mark} {p['machine'][:12]}… last {_ago(p['last_success_ns'], now_ns)}"
            f" · wm {p['peer_watermark']} · applied {p['transactions_applied']}"
            f" · retries {p['retries']}{err}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m tools.network.fleet_sync_status",
                                     description="Show fleet personal-DB sync status.")
    parser.add_argument("--org", default="personal",
                        help="store to read (default: personal)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    from tools.graph.db import resolve_caller_db_path

    db_path = resolve_caller_db_path(None if args.org == "personal" else args.org)
    status = read_status(db_path)
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(format_human(status))
    return 0 if status.get("available") else 1


if __name__ == "__main__":
    raise SystemExit(main())
