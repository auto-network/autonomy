"""Organization-scoped operational projection. Reads only; tasks remain beads.

No transcript synthesis and no guessed activity. Each failed source is named,
and missing prerequisite evidence can never turn into an execution-ready row.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os

from tools.data_paths import org_beads_dir
from tools.dashboard.org_identity import session_org_slug
from tools.dashboard.plugins.mission import bridge, compose
from tools.dashboard.plugins.mission.entrypoints.schemas import MISSION_SET_ID, valid_iso

_EXTRA_SQL = ("SELECT id, assignee, closed_at, design <> '' AS has_design, "
              "acceptance_criteria <> '' AS has_acceptance, metadata FROM issues")
_EDGES_SQL = ("SELECT issue_id, COALESCE(depends_on_issue_id, depends_on_wisp_id, "
              "depends_on_external) AS depends_on_id, type FROM dependencies")
_PHASES = {"designing", "implementing", "debugging", "testing", "verifying", "waiting", "unknown"}


def project_metadata(value) -> dict:
    try:
        if isinstance(value, str):
            value = json.loads(value)
        value = value.get("mission_ops", {}) if isinstance(value, dict) else {}
        caps = {"headline": 160, "subtitle": 240, "phase": 32, "phase_at": 64, "reported_by": 160}
        if not isinstance(value, dict) or set(value) - set(caps):
            return {}
        if any(not isinstance(v, str) or len(v) > caps[k] for k, v in value.items()):
            return {}
        if "phase" in value and value["phase"] not in _PHASES:
            return {}
        if "phase_at" in value and not valid_iso(value["phase_at"]):
            return {}
        return dict(value)
    except (TypeError, ValueError):
        return {}


def readiness(task: dict, tasks: dict[str, dict]) -> str:
    status = task.get("status")
    if status == "closed":
        return "complete"
    targets = task.get("blocks_on", [])
    if not task.get("dependencies_known") or not task.get("metadata_known") or any(
        bid not in tasks for bid in targets
    ):
        return "unknown"
    if status in ("blocked", "deferred"):
        return status
    if any(tasks[bid].get("status") != "closed" for bid in targets):
        return "dependency_wait"
    if status == "in_progress":
        return "running"
    if status == "open":
        return "ready" if task.get("has_design") or task.get("has_acceptance") else "needs_specification"
    return "unknown"


def _session_rows() -> list[dict]:
    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import sessions
        return sessions.get_active_sessions()
    from tools.dashboard.dao import dashboard_db
    return dashboard_db.get_live_sessions()


def load_ops(org: str, mission_id: str) -> dict:
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "org": org,
           "selected_mission": mission_id, "missions": [], "pillars": [],
           "items": [], "tasks": [], "sessions": [], "errors": []}

    def error(source):
        out["errors"].append({"source": source, "message": f"{source.capitalize()} source unavailable; no empty-success claim."})

    try:
        for member in compose._read(MISSION_SET_ID, org):
            mission = {**dict(member.payload), "mission_id": member.key}
            out["missions"].append(mission)
            try:
                out["pillars"].extend({**p, "mission_id": member.key}
                                       for p in compose.load_pillars(org, member.key))
                out["items"].extend({**i, "mission_id": member.key}
                                     for i in compose.load_items(org, member.key)
                                     if not i.get("retired"))
            except Exception:
                error("mission_items")
    except Exception:
        error("missions")
    try:
        for row in _session_rows():
            if org in ("personal", "?") or session_org_slug(row) != org:
                continue
            name = row.get("tmux_name") or row.get("session_id")
            if not isinstance(name, str) or not name:
                continue
            # Only named, necessary display fields. Never forward whole DAO rows.
            out["sessions"].append({"id": name, "label": row.get("label") or name,
                                    "state": row.get("state") or row.get("activity_state") or "unknown",
                                    "topics": row.get("topics") or [],
                                    "last_activity": row.get("last_activity")})
    except Exception:
        error("sessions")
    if org != "autonomy" and org_beads_dir(org) is None:
        error("tracker")
        return out
    commands = [["list", "--all", "--limit", "0"], ["sql", _EXTRA_SQL], ["sql", _EDGES_SQL]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows, extras, raw_edges = list(pool.map(lambda cmd: bridge._bd(cmd, org=org), commands))
    if not isinstance(rows, list):
        error("tasks")
        rows = []
    if not isinstance(extras, list):
        error("metadata")
        extras = []
    elif any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in extras):
        error("metadata")
        extras = []
    edges = bridge.group_dependencies(raw_edges)
    if edges is None:
        error("dependencies")
    extra_map = {r["id"]: r for r in extras if isinstance(r, dict) and r.get("id")}
    missions = {m["mission_id"] for m in out["missions"]}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            error("tasks")
            continue
        labels = row.get("labels") or []
        if not isinstance(labels, list) or any(not isinstance(lb, str) for lb in labels):
            error("tasks")
            continue
        if any(lb.startswith("org:") and lb != f"org:{org}" for lb in labels):
            continue
        mid = [lb[8:] for lb in labels if lb.startswith("mission:")]
        extra = extra_map.get(row["id"], {})
        task = bridge._summary(row, (edges or {}).get(row["id"], []),
                               dependencies_known=edges is not None)
        task.update({"status": row.get("status", "unknown"), "priority": row.get("priority", 2),
                     "updated_at": row.get("updated_at"), "closed_at": extra.get("closed_at"),
                     "assignee": extra.get("assignee"), "owner": row.get("owner"),
                     "has_design": bool(extra.get("has_design")), "has_acceptance": bool(extra.get("has_acceptance")),
                     "metadata_known": row["id"] in extra_map,
                     "mission_ops": project_metadata(extra.get("metadata")),
                     "mission_ids": mid, "pillar_labels": [lb for lb in labels if lb.startswith("pillar:")],
                     "allocation": "selected_mission" if mission_id in mid else
                                   "other_mission" if any(m in missions for m in mid) else
                                   "unresolved_mission" if mid else "unallocated"})
        if task["state"] == "defined" and (task["has_design"] or task["has_acceptance"]):
            task["state"] = "specified"
        out["tasks"].append(task)
    tasks = {t["id"]: t for t in out["tasks"]}
    task_source_failed = any(e["source"] == "tasks" for e in out["errors"])
    for task in out["tasks"]:
        if task_source_failed:
            task["dependencies_known"] = False
        task["readiness"] = readiness(task, tasks)
    return out
