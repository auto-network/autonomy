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
    itself, which runs the sync-pull loop) rather than the
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


def _direct_check() -> dict:
    """The direct tier, as THIS process sees it: what the machine-local
    fleet-direct row asks for, what the listener actually bound, what was
    last announced to the registry, and which roster peers resolved to a
    dialable address. ``enabled`` false with a loopback/ephemeral bind is
    the historical default -- every pull rides the relay."""
    out: dict = {}
    try:
        from tools.network import fleet_direct_config

        cfg = fleet_direct_config.load()
        out.update({
            "enabled": cfg.enabled,
            "listen_host": cfg.listen_host,
            "listen_port": cfg.listen_port,
            "advertise_addrs": list(cfg.advertise_addrs),
            "serve_in": cfg.serve_in,
            "pull_direct": cfg.pull_direct,
        })
    except Exception as exc:
        out["detail"] = f"could not read fleet-direct config: {exc!r}"
    try:
        from tools.network.fleet_sync_scheduler import dashboard_fleet_sync_service

        scheduler = dashboard_fleet_sync_service.scheduler
        out["listener_bound_port"] = (
            scheduler.port if scheduler is not None and scheduler.running else None
        )
    except Exception:
        out["listener_bound_port"] = None
    try:
        from tools.dashboard import fleet_enrollment_routes as fer

        cache = fer._reachability_cache
        if cache is not None:
            peers = cache.snapshot()
            out["announced"] = cache.last_announce is not None
            out["discovered_peers"] = {
                pub[:12]: list(addrs) for pub, addrs in peers.items()
            }
        else:
            out["announced"] = False
            out["discovered_peers"] = {}
    except Exception:
        pass
    return out


def _connector_direct_listener(org: str | None):
    try:
        from tools.dashboard import link_serving_supervisor as sup

        return sup.control(org, "connector-status", {}).get("direct_listener")
    except Exception:
        return None


def _paths_check() -> dict:
    """Per peer and channel: which path carried the newest attempt and the
    newest success, and cumulative bytes by path class (auto-wryex)."""
    try:
        from tools.network.fleet_sync_telemetry import read_channel_rows

        out: dict = {}
        for row in read_channel_rows():
            payload = row["payload"]
            entry = out.setdefault(row["peer"][:12], {})
            entry[f"{row['channel']}/{row['direction']}/{row['scope']}"] = {
                "last_path_class": payload.get("last_path_class"),
                "last_address": payload.get("last_address"),
                "last_success_path_class": payload.get("last_success_path_class"),
                "last_success_address": payload.get("last_success_address"),
                "bytes_by_path_class": payload.get("bytes_by_path_class") or {},
                "last_outcome": payload.get("last_outcome"),
            }
        return out
    except Exception as exc:
        return {"detail": f"could not read telemetry: {exc!r}"}


def compute_verdict(org: str | None = None) -> dict:
    """Everything fleet_doctor's top line and /api/fleet/status need,
    in one call. org=None is the personal/scopeless sync scope."""
    connector_version = _connector_version_check(org)
    dashboard_version = _dashboard_process_version_check()
    cred = _cred_check(org)
    data = _data_check(org)
    direct = _direct_check()
    direct["connector_listener"] = _connector_direct_listener(org)

    stale = connector_version.get("status") == "stale" \
        or dashboard_version.get("status") == "stale"
    if stale:
        top_line = "STALE-CODE"
    elif cred.get("configured") is False:
        top_line = "LOCKED"
    else:
        # No per-attempt outcome is recorded on the direct path. The relay
        # pull used to supply it; fleet sync no longer rides the viewer link
        # broker, so this reports UNKNOWN rather than a stale claim. A direct
        # last-attempt record belongs with the directed carrier work.
        top_line = "UNKNOWN"

    return {
        "top_line": top_line,
        "checked_at": time.time(),
        "connector_version": connector_version,
        "dashboard_version": dashboard_version,
        "credential": cred,
        "data": data,
        "direct": direct,
        "paths": _paths_check(),
    }
