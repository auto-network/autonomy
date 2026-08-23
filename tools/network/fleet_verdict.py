"""One decisive verdict for "is fleet-sync working, and if not, why" --
the CLI face is ``fleet_doctor``'s top line, the HTTP face is
``GET /api/fleet/status``. Same probes either way, so an operator SSH'd
into a box and a browser hitting the dashboard see the same answer.

Built 2026-08-23 after a debugging cycle that burned a scarce manual
unlock on a stale-code deploy that ``ps``/log-grepping never caught:
the fix was on disk, but the running process had imported the old
module and nothing said so. The two checks that would have ended that
guessing sooner are first for a reason -- STALE-CODE and the last
pull's distinct failure reason.

Deliberately partial: TOP_LINE is one of
SYNCED-OK | STALE-CODE | LOCKED | BLOCKED:<reason> | UNKNOWN. The
reachability (relay/registry round trip) and frontier (watermark
behind-count) probes described alongside this in the design
conversation are not implemented yet -- extend ``compute_verdict``
rather than re-deriving this file's shape from scratch.
"""

from __future__ import annotations

import time

from tools.network import build_version


def _connector_version_check(org: str | None) -> dict:
    """STALE-CODE probe: the running connector subprocess's loaded commit
    vs. what's on disk right now. Best-effort -- a connector that isn't up
    at all is a different, earlier failure this returns as "unknown"."""
    try:
        from tools.dashboard import link_serving_supervisor as sup

        status = sup.control(org, "connector-status", {})
    except Exception as exc:
        return {"status": "unknown", "detail": f"no connector reachable: {exc!r}"}
    process_commit = status.get("process_commit")
    disk_commit = build_version.disk_head()
    if process_commit is None or disk_commit is None:
        return {"status": "unknown", "process_commit": process_commit,
                "disk_commit": disk_commit}
    if process_commit != disk_commit:
        return {"status": "stale", "process_commit": process_commit,
                "disk_commit": disk_commit}
    return {"status": "fresh", "process_commit": process_commit,
            "disk_commit": disk_commit}


def _dashboard_process_version_check() -> dict:
    """Same STALE-CODE question, but for THIS process (the dashboard
    itself, which runs the checkpoint-pull loop) rather than the
    connector subprocess -- the two can be stale independently."""
    disk_commit = build_version.disk_head()
    if build_version.PROCESS_COMMIT is None or disk_commit is None:
        return {"status": "unknown", "process_commit": build_version.PROCESS_COMMIT,
                "disk_commit": disk_commit}
    if build_version.PROCESS_COMMIT != disk_commit:
        return {"status": "stale", "process_commit": build_version.PROCESS_COMMIT,
                "disk_commit": disk_commit}
    return {"status": "fresh", "process_commit": build_version.PROCESS_COMMIT,
            "disk_commit": disk_commit}


def _cred_check(org: str | None) -> dict:
    try:
        from tools.dashboard import link_serving_supervisor as sup

        status = sup.control(org, "connector-status", {})
    except Exception as exc:
        return {"configured": None, "detail": f"no connector reachable: {exc!r}"}
    return {"configured": status.get("fleet_runtime_configured")}


def _last_pull_check() -> dict:
    """The money line: this process's own record of its most recent
    checkpoint-pull attempt, with a distinct machine-readable reason --
    not "check the logs and guess from the traceback"."""
    try:
        from tools.network.fleet_relay_sync import dashboard_relay_sync_service

        result = dashboard_relay_sync_service.last_result
    except Exception as exc:
        return {"outcome": "unknown", "detail": f"could not read pull state: {exc!r}"}
    if result is None:
        return {"outcome": "never_attempted"}
    return dict(result)


def _data_check(org: str | None) -> dict:
    try:
        from tools.graph.db import _org_db_path
        import sqlite3

        path = _org_db_path(org or "personal")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        counts = {}
        for table in ("thoughts", "sources", "settings", "fleet_sync_catalog",
                      "fleet_sync_quarantine"):
            try:
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = None
        con.close()
        return {"path": str(path), "counts": counts}
    except Exception as exc:
        return {"detail": f"could not read local store: {exc!r}"}


def compute_verdict(org: str | None = None) -> dict:
    """Everything fleet_doctor's top line and /api/fleet/status need,
    in one call. org=None is the personal/scopeless sync scope."""
    connector_version = _connector_version_check(org)
    dashboard_version = _dashboard_process_version_check()
    cred = _cred_check(org)
    last_pull = _last_pull_check()
    data = _data_check(org)

    stale = connector_version.get("status") == "stale" \
        or dashboard_version.get("status") == "stale"
    if stale:
        top_line = "STALE-CODE"
    elif cred.get("configured") is False:
        top_line = "LOCKED"
    elif last_pull.get("outcome") == "failed":
        top_line = f"BLOCKED:{last_pull.get('reason', 'unknown')}"
    elif last_pull.get("outcome") == "success":
        top_line = "SYNCED-OK"
    else:
        top_line = "UNKNOWN"

    return {
        "top_line": top_line,
        "checked_at": time.time(),
        "connector_version": connector_version,
        "dashboard_version": dashboard_version,
        "credential": cred,
        "last_pull": last_pull,
        "data": data,
    }
