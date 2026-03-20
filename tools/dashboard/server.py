"""Autonomy Dashboard — Starlette server.

Thin rendering layer over the bd and graph CLI tools.
Every view the dashboard shows, an agent can also produce via CLI.
"""

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow importing from agents/ (sibling of tools/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket
from sse_starlette.sse import EventSourceResponse

from agents.dispatch_db import list_runs, get_run, get_runs_for_bead, get_currently_running, DB_PATH
from agents.session_launcher import launch_session
from agents.experiments_db import (
    create_experiment, get_experiment, submit_results, list_pending as list_pending_experiments,
    dismiss_experiment,
)
from tools.dashboard.event_bus import event_bus
if os.environ.get("DASHBOARD_MOCK"):
    from tools.dashboard.dao import mock as dao_beads
    from tools.dashboard.dao import mock as dao_dispatch
    from tools.dashboard.dao import mock as dao_sessions
else:
    from tools.dashboard.dao import beads as dao_beads
    from tools.dashboard.dao import dispatch as dao_dispatch
    from tools.dashboard.dao import sessions as dao_sessions

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

DISPATCH_STATE_PATH = _REPO_ROOT / "data" / "dispatch.state"
# Labels always shown in pause UI even if not in dispatch.state
_KNOWN_PAUSE_LABELS = ["dashboard"]


# ── CLI Subprocess Helper ─────────────────────────────────────

async def run_cli(cmd: list[str], timeout: int = 30) -> tuple[str, str, int]:
    """Run a CLI command async and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(), stderr.decode(), proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        return "", "timeout", -1


async def run_cli_json(cmd: list[str], timeout: int = 30) -> list | dict:
    """Run CLI command and parse JSON output."""
    stdout, stderr, rc = await run_cli(cmd, timeout)
    if rc != 0 or not stdout.strip():
        return {"error": stderr or "no output", "returncode": rc}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "invalid JSON", "raw": stdout[:500]}


# ── API Endpoints ─────────────────────────────────────────────

async def api_beads_ready(request):
    return JSONResponse(await run_cli_json(["bd", "ready", "--json"]))

async def api_beads_list(request):
    return JSONResponse(await run_cli_json(["bd", "list", "--json", "-n", "100", "--sort", "updated"]))

async def api_bead_show(request):
    bead_id = request.path_params["id"]
    return JSONResponse(await run_cli_json(["bd", "show", bead_id, "--json"]))

async def api_bead_tree(request):
    bead_id = request.path_params["id"]
    return JSONResponse(await run_cli_json(["bd", "dep", "tree", bead_id, "--json"]))

async def api_beads_search(request):
    """Search beads by title and description. Falls back to issues.jsonl if bd unavailable."""
    q = request.query_params.get("q", "").strip().lower()
    if not q:
        return JSONResponse({"error": "missing q parameter"})

    # Try bd search first
    stdout, stderr, rc = await run_cli(["bd", "search", q, "--json"], timeout=10)
    if rc == 0 and stdout.strip():
        try:
            results = json.loads(stdout)
            if isinstance(results, list):
                return JSONResponse(results)
        except json.JSONDecodeError:
            pass

    # Fallback: read issues.jsonl directly and filter
    issues_path = _REPO_ROOT / ".beads" / "issues.jsonl"
    if not issues_path.exists():
        return JSONResponse({"error": "no beads data found"})

    terms = q.split()
    results = []
    with open(issues_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                issue = json.loads(line)
            except json.JSONDecodeError:
                continue
            searchable = f"{issue.get('title', '')} {issue.get('description', '')}".lower()
            if all(term in searchable for term in terms):
                results.append(issue)
    return JSONResponse(results)


async def api_bead_approve(request):
    """Set readiness=approved on a bead, releasing it for dispatch."""
    bead_id = request.path_params["id"]
    stdout, stderr, rc = await run_cli(["bd", "set-state", bead_id, "readiness=approved",
                                         "--reason", "dashboard: approved for dispatch"])
    if rc != 0:
        return JSONResponse({"error": stderr.strip(), "ok": False}, status_code=400)
    return JSONResponse({"ok": True, "bead_id": bead_id})

# ── Dispatch pause state helpers ──────────────────────────────

def _read_dispatch_state() -> dict:
    """Read dispatch.state file. Returns {} if missing or invalid."""
    try:
        return json.loads(DISPATCH_STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_dispatch_state(state: dict) -> None:
    """Write dispatch.state atomically via rename."""
    DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DISPATCH_STATE_PATH.with_suffix(".state.tmp")
    tmp.write_text(json.dumps(state))
    tmp.rename(DISPATCH_STATE_PATH)


def _get_pause_state() -> dict:
    """Return pause state for all known labels (always includes _KNOWN_PAUSE_LABELS)."""
    raw = _read_dispatch_state()
    result = {label: bool(raw.get(label, False)) for label in _KNOWN_PAUSE_LABELS}
    for label, paused in raw.items():
        if label not in result:
            result[label] = bool(paused)
    return result


async def api_dispatch_pause_get(request):
    """GET /api/dispatch/pause — return current pause state for all label queues."""
    return JSONResponse({"paused": _get_pause_state()})


async def api_dispatch_pause_post(request):
    """POST /api/dispatch/pause — set pause state for a label queue.

    Body: {"label": "dashboard", "paused": true}
    Returns updated full pause state.
    """
    body = await request.json()
    label = body.get("label")
    paused = bool(body.get("paused", False))
    if not label:
        return JSONResponse({"error": "label required"}, status_code=400)
    state = _read_dispatch_state()
    if paused:
        state[label] = True
    else:
        state.pop(label, None)
    _write_dispatch_state(state)
    new_pause = _get_pause_state()
    # Broadcast updated pause state via SSE dispatch topic
    await event_bus.broadcast("dispatch_pause", new_pause)
    return JSONResponse({"paused": new_pause})


async def api_dispatch_status(request):
    """Show dispatched beads with their dispatch state.

    Reads the dispatch dimension (queued/launching/running/collecting/merging/done/failed)
    from bd labels + docker ps for containers. Adds currently-running runs from SQLite.
    """
    claimed = await run_cli_json(["bd", "query", 'label="work:claimed"', "--json"])
    # Also query beads with active dispatch states for richer status
    dispatching = await run_cli_json(["bd", "query", 'label="dispatch:running" OR label="dispatch:launching" OR label="dispatch:collecting" OR label="dispatch:merging" OR label="dispatch:queued"', "--json"])
    # Containers are still useful for runtime info (uptime, image)
    stdout, _, _ = await run_cli(["docker", "ps", "--filter", "name=agent-", "--format", '{"name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}"}'])
    containers = []
    for line in stdout.strip().splitlines():
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    # Currently running runs from SQLite (started but no status yet)
    running_runs = await asyncio.to_thread(get_currently_running)
    return JSONResponse({
        "claimed": claimed if isinstance(claimed, list) else [],
        "dispatching": dispatching if isinstance(dispatching, list) else [],
        "containers": containers,
        "running_runs": running_runs,
    })


async def api_dispatch_approved(request):
    """Return approved beads split into waiting (unblocked) vs blocked.

    For each approved bead, checks dependencies via `bd dep list`.
    Blocked beads include their open blockers so the frontend can link to them.
    """
    all_beads = await run_cli_json(["bd", "list", "--json", "-n", "100"])
    bead_list = all_beads if isinstance(all_beads, list) else []

    # Filter to open, approved beads not currently being dispatched
    dispatch_labels = {
        "dispatch:queued", "dispatch:launching", "dispatch:running",
        "dispatch:collecting", "dispatch:merging",
    }
    approved = []
    for b in bead_list:
        if b.get("status") != "open":
            continue
        labels = set(b.get("labels") or [])
        if "readiness:approved" not in labels:
            continue
        if labels & dispatch_labels:
            continue
        approved.append(b)

    # Check dependencies for each approved bead in parallel
    async def check_deps(bead):
        dep_data = await run_cli_json(["bd", "dep", "list", bead["id"], "--json"])
        if not isinstance(dep_data, list):
            return bead, []
        open_blockers = []
        for dep in dep_data:
            if not isinstance(dep, dict):
                continue
            if dep.get("dependency_type") == "parent-child":
                continue
            if dep.get("status") != "closed":
                open_blockers.append({
                    "id": dep.get("id", ""),
                    "title": dep.get("title", ""),
                    "status": dep.get("status", ""),
                    "priority": dep.get("priority"),
                })
        return bead, open_blockers

    results = await asyncio.gather(*(check_deps(b) for b in approved))

    waiting = []
    blocked = []
    for bead, blockers in results:
        if blockers:
            blocked.append({**bead, "blockers": blockers})
        else:
            waiting.append(bead)

    return JSONResponse({"waiting": waiting, "blocked": blocked})


AGENT_RUNS_DIR = Path(__file__).parent.parent.parent / "data" / "agent-runs"

async def api_dispatch_runs(request):
    """List dispatch runs from SQLite (includes RUNNING rows)."""
    db_rows = await asyncio.to_thread(list_runs)
    runs = []
    for row in db_rows:
        # Reconstruct decision dict from flat columns for backward compat
        decision = None
        if row.get("status") and row["status"] != "RUNNING":
            decision = {"status": row["status"], "reason": row.get("reason")}
            scores = {}
            for key in ("tooling", "clarity", "confidence"):
                val = row.get(f"score_{key}")
                if val is not None:
                    scores[key] = val
            if scores:
                decision["scores"] = scores
            time_breakdown = {}
            for db_key, dec_key in [
                ("time_research_pct", "research_pct"),
                ("time_coding_pct", "coding_pct"),
                ("time_debugging_pct", "debugging_pct"),
                ("time_tooling_pct", "tooling_workaround_pct"),
            ]:
                val = row.get(db_key)
                if val is not None:
                    time_breakdown[dec_key] = val
            if time_breakdown:
                decision["time_breakdown"] = time_breakdown
            if row.get("failure_category"):
                decision["failure_category"] = row["failure_category"]
            if row.get("discovered_beads_count"):
                decision["discovered_beads_count"] = row["discovered_beads_count"]

        # Derive timestamp from completed_at, started_at, or dir name
        timestamp = ""
        if row.get("completed_at"):
            # completed_at is "YYYY-MM-DD HH:MM:SS" — convert to YYYYMMDD-HHMMSS
            ts = row["completed_at"].replace("-", "").replace(":", "").replace(" ", "-")
            timestamp = ts[:8] + "-" + ts[8:]  # YYYYMMDD-HHMMSS
        elif row.get("started_at"):
            ts = row["started_at"].replace("-", "").replace(":", "").replace(" ", "-")
            timestamp = ts[:8] + "-" + ts[8:]
        elif row.get("id"):
            parts = row["id"].rsplit("-", 2)
            if len(parts) >= 3:
                timestamp = f"{parts[1]}-{parts[2]}"

        runs.append({
            "bead_id": row.get("bead_id", ""),
            "timestamp": timestamp,
            "dir": row.get("id", ""),
            "decision": decision,
            "status": row.get("status") or "",
            "has_experience_report": bool(row.get("has_experience_report")),
            "commit_hash": row.get("commit_hash") or "",
            "branch": row.get("branch") or "",
            "duration_secs": row.get("duration_secs"),
            "lines_added": row.get("lines_added"),
            "lines_removed": row.get("lines_removed"),
            "files_changed": row.get("files_changed"),
            "commit_message": row.get("commit_message") or "",
        })
    return JSONResponse(runs)


# ── Timeline API ─────────────────────────────────────────────

_RANGE_MAP = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "14d": timedelta(days=14),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def _timeline_conn() -> sqlite3.Connection:
    """Get a read-only connection with row_factory for timeline queries."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _parse_range(range_str: str) -> str | None:
    """Convert range param to a UTC datetime cutoff string, or None for 'all'."""
    if not range_str or range_str == "all":
        return None
    td = _RANGE_MAP.get(range_str)
    if td is None:
        # Try parsing Nd or Nh patterns
        m = re.match(r"^(\d+)([dhm])$", range_str)
        if not m:
            return None
        val, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            td = timedelta(days=val)
        elif unit == "h":
            td = timedelta(hours=val)
        elif unit == "m":
            td = timedelta(minutes=val)
    cutoff = datetime.now(timezone.utc) - td
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def _build_timeline_where(
    range_str: str | None, project: str | None, q: str | None
) -> tuple[str, list]:
    """Build WHERE clause and params for timeline queries.

    Always excludes RUNNING rows — timeline shows completed work only.
    """
    clauses = ["status != 'RUNNING'"]
    params = []

    # Time range filter
    cutoff = _parse_range(range_str) if range_str else None
    if cutoff:
        clauses.append("completed_at >= ?")
        params.append(cutoff)

    # Project filter — match against bead_id prefix or image name
    if project:
        clauses.append("(bead_id LIKE ? OR image LIKE ?)")
        params.append(f"{project}%")
        params.append(f"%{project}%")

    # Text search — LIKE against bead_id, reason, commit_message
    if q:
        terms = q.strip().split()
        for term in terms:
            like = f"%{term}%"
            clauses.append(
                "(bead_id LIKE ? OR reason LIKE ? OR commit_message LIKE ?)"
            )
            params.extend([like, like, like])

    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


def _row_to_timeline_entry(row: sqlite3.Row) -> dict:
    """Convert a dispatch_runs row to a timeline entry dict."""
    scores = {}
    for key in ("tooling", "clarity", "confidence"):
        val = row[f"score_{key}"]
        if val is not None:
            scores[key] = val

    time_breakdown = {}
    for db_key, out_key in [
        ("time_research_pct", "research_pct"),
        ("time_coding_pct", "coding_pct"),
        ("time_debugging_pct", "debugging_pct"),
        ("time_tooling_pct", "tooling_workaround_pct"),
    ]:
        val = row[db_key]
        if val is not None:
            time_breakdown[out_key] = val

    librarian_type = row["librarian_type"] or None
    return {
        "run_id": row["id"] or "",
        "bead_id": row["bead_id"] or "",
        "title": librarian_type or row["bead_id"] or "",  # librarian type or bead_id
        "priority": None,  # not stored in dispatch_runs
        "status": row["status"] or "",
        "reason": row["reason"] or "",
        "duration_secs": row["duration_secs"],
        "commit_hash": row["commit_hash"] or "",
        "commit_message": row["commit_message"] or "",
        "lines_added": row["lines_added"],
        "lines_removed": row["lines_removed"],
        "files_changed": row["files_changed"],
        "scores": scores or None,
        "time_breakdown": time_breakdown or None,
        "failure_category": row["failure_category"] or None,
        "discovered_beads_count": row["discovered_beads_count"],
        "started_at": (row["started_at"] + "Z") if row["started_at"] else None,
        "completed_at": (row["completed_at"] + "Z") if row["completed_at"] else None,
        "has_experience_report": bool(row["has_experience_report"]),
        "token_count": row["token_count"],
        "librarian_type": librarian_type,
        "librarian_review": None,  # populated by _enrich_with_librarian_data
        "_output_dir": row["output_dir"] or "",  # internal field, stripped before response
    }


def _read_librarian_results(output_dir: str | None) -> dict | None:
    """Read results JSON from a librarian's output directory.

    Tries results.json first (structured output), then decision.json (fallback).
    Returns None if neither file exists or is readable.
    """
    if not output_dir:
        return None
    base = Path(output_dir)
    for filename in ("results.json", "decision.json"):
        path = base / filename
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return None


def _enrich_with_librarian_data(
    conn: sqlite3.Connection, entries: list[dict]
) -> None:
    """Enrich timeline entries with librarian review data in-place.

    For regular dispatch entries (librarian_type=None): queries librarian_jobs
    for any review_report job whose payload.run_id matches the dispatch run,
    then reads results JSON from the librarian's output directory.

    For standalone librarian entries (librarian_type set): reads results JSON
    from the entry's own output_dir (stored in _output_dir).
    """
    # Standalone librarian entries — read results from their own output_dir
    for entry in entries:
        if entry.get("librarian_type"):
            entry["librarian_review"] = _read_librarian_results(entry.get("_output_dir"))

    # Regular dispatch entries — bulk-query librarian_jobs for review results
    regular_run_ids = [
        e["run_id"] for e in entries
        if not e.get("librarian_type") and e.get("run_id")
    ]
    if not regular_run_ids:
        return

    placeholders = ",".join("?" * len(regular_run_ids))
    sql = f"""
        SELECT
            json_extract(lj.payload, '$.run_id') AS dispatch_run_id,
            lj.status AS job_status,
            dr.output_dir AS lib_output_dir
        FROM librarian_jobs lj
        LEFT JOIN dispatch_runs dr ON (
            dr.librarian_type = lj.job_type
            AND dr.id LIKE 'librarian-' || lj.job_type || '-' || substr(lj.id, 1, 8) || '-%'
        )
        WHERE lj.job_type = 'review_report'
        AND json_extract(lj.payload, '$.run_id') IN ({placeholders})
    """
    try:
        rows = conn.execute(sql, regular_run_ids).fetchall()
    except Exception:
        return

    reviews: dict[str, dict] = {}
    for row in rows:
        run_id = row["dispatch_run_id"]
        if run_id in reviews:
            continue  # keep first match
        if row["job_status"] == "running":
            reviews[run_id] = {"status": "running"}
        elif row["job_status"] == "done":
            results = _read_librarian_results(row["lib_output_dir"])
            reviews[run_id] = results if results is not None else {"status": "done"}

    for entry in entries:
        if not entry.get("librarian_type"):
            entry["librarian_review"] = reviews.get(entry["run_id"])


async def api_timeline(request):
    """Timeline entries from dispatch_runs.

    GET /api/timeline?range=1d&project=autonomy&q=search+terms

    Returns reverse-chronological array of timeline entries.
    """
    range_str = request.query_params.get("range")
    project = request.query_params.get("project")
    q = request.query_params.get("q")
    limit = min(int(request.query_params.get("limit", "200")), 1000)
    offset = int(request.query_params.get("offset", "0"))

    where, params = _build_timeline_where(range_str, project, q)
    sql = f"""
        SELECT * FROM dispatch_runs
        WHERE {where}
        ORDER BY completed_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    def _query():
        conn = _timeline_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            entries = [_row_to_timeline_entry(r) for r in rows]
            _enrich_with_librarian_data(conn, entries)
            for e in entries:
                e.pop("_output_dir", None)
            return entries
        finally:
            conn.close()

    try:
        entries = await asyncio.to_thread(_query)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # Enrich with bead title and priority from Dolt
    bead_ids = [e["bead_id"] for e in entries if e.get("bead_id")]
    if bead_ids:
        try:
            meta = await asyncio.to_thread(dao_beads.get_bead_title_priority, bead_ids)
            for entry in entries:
                bid = entry.get("bead_id")
                if bid and bid in meta:
                    entry["title"] = meta[bid].get("title") or entry["bead_id"]
                    entry["priority"] = meta[bid].get("priority")
        except Exception:
            pass  # fall back to bead_id as title

    return JSONResponse(entries)


async def api_timeline_stats(request):
    """Aggregate stats from dispatch_runs.

    GET /api/timeline/stats?range=1d&project=autonomy

    Returns: completed_count, success_rate, failed_count, blocked_count,
             avg_duration, avg_tooling_score, avg_confidence_score
    """
    range_str = request.query_params.get("range")
    project = request.query_params.get("project")

    where, params = _build_timeline_where(range_str, project, None)
    sql = f"""
        SELECT
            COUNT(*) as total_count,
            COUNT(CASE WHEN status = 'DONE' THEN 1 END) as completed_count,
            COUNT(CASE WHEN status = 'FAILED' THEN 1 END) as failed_count,
            COUNT(CASE WHEN status = 'BLOCKED' THEN 1 END) as blocked_count,
            AVG(duration_secs) as avg_duration,
            AVG(score_tooling) as avg_tooling_score,
            AVG(score_confidence) as avg_confidence_score,
            AVG(score_clarity) as avg_clarity_score
        FROM dispatch_runs
        WHERE {where}
    """

    def _query():
        conn = _timeline_conn()
        try:
            row = conn.execute(sql, params).fetchone()
            if not row or row["total_count"] == 0:
                return {
                    "completed_count": 0,
                    "success_rate": 0.0,
                    "failed_count": 0,
                    "blocked_count": 0,
                    "avg_duration": None,
                    "avg_tooling_score": None,
                    "avg_confidence_score": None,
                    "avg_clarity_score": None,
                }
            total = row["total_count"]
            completed = row["completed_count"]
            return {
                "completed_count": completed,
                "success_rate": round(completed / total, 4) if total > 0 else 0.0,
                "failed_count": row["failed_count"],
                "blocked_count": row["blocked_count"],
                "avg_duration": round(row["avg_duration"], 1) if row["avg_duration"] is not None else None,
                "avg_tooling_score": round(row["avg_tooling_score"], 2) if row["avg_tooling_score"] is not None else None,
                "avg_confidence_score": round(row["avg_confidence_score"], 2) if row["avg_confidence_score"] is not None else None,
                "avg_clarity_score": round(row["avg_clarity_score"], 2) if row["avg_clarity_score"] is not None else None,
            }
        finally:
            conn.close()

    try:
        stats = await asyncio.to_thread(_query)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(stats)


async def api_dispatch_trace(request):
    """Full trace for a completed dispatch run.

    Metadata (status, reason, scores, commit info, diff stats, duration)
    comes from SQLite. Large artifacts (experience_report.md, session JSONL,
    git diff) are still read from disk on demand.
    """
    run_name = request.path_params["run"]

    # Get structured metadata from SQLite
    row = await asyncio.to_thread(get_run, run_name)

    # Fall back to filesystem if not in DB yet
    run_dir = AGENT_RUNS_DIR / run_name
    if not row and not run_dir.exists():
        # Try as bead ID — resolve to most recent run
        runs = await asyncio.to_thread(get_runs_for_bead, run_name)
        if runs:
            row = runs[0]
            run_name = row["id"]
            run_dir = AGENT_RUNS_DIR / run_name
        else:
            return JSONResponse({"error": "run not found"}, status_code=404)

    # Extract fields from DB row (or fall back to filesystem)
    if row:
        bead_id = row.get("bead_id") or ""
        commit_hash = row.get("commit_hash") or ""
        branch = row.get("branch") or ""
        branch_base = row.get("branch_base") or ""
        status = row.get("status")
        reason = row.get("reason")
        duration_secs = row.get("duration_secs")
        commit_message = row.get("commit_message") or ""
        lines_added = row.get("lines_added")
        lines_removed = row.get("lines_removed")
        files_changed = row.get("files_changed")

        # Reconstruct decision dict from flat DB columns
        decision = None
        if status:
            decision = {"status": status, "reason": reason}
            scores = {}
            for key in ("tooling", "clarity", "confidence"):
                val = row.get(f"score_{key}")
                if val is not None:
                    scores[key] = val
            if scores:
                decision["scores"] = scores
            if row.get("failure_category"):
                decision["failure_category"] = row["failure_category"]
    else:
        # Filesystem fallback for runs not yet in DB
        parts = run_name.rsplit("-", 2)
        bead_id = parts[0] if len(parts) >= 3 else run_name
        commit_hash = ""
        commit_path = run_dir / ".commit_hash"
        if commit_path.exists():
            commit_hash = commit_path.read_text().strip()
        branch = ""
        branch_path = run_dir / ".branch"
        if branch_path.exists():
            branch = branch_path.read_text().strip()
        branch_base = ""
        base_path = run_dir / ".branch_base"
        if base_path.exists():
            branch_base = base_path.read_text().strip()
        decision = None
        decision_path = run_dir / "decision.json"
        if decision_path.exists():
            try:
                decision = json.loads(decision_path.read_text())
            except json.JSONDecodeError:
                pass
        duration_secs = None
        commit_message = ""
        lines_added = None
        lines_removed = None
        files_changed = None

    # Large artifacts still from disk
    experience = ""
    if run_dir.exists():
        exp_path = run_dir / "experience_report.md"
        if exp_path.exists():
            experience = exp_path.read_text()

    # Git diff (computed on demand)
    diff = ""
    if commit_hash and branch_base:
        stdout, _, rc = await run_cli(["git", "diff", f"{branch_base}..{commit_hash}"], timeout=10)
        if rc == 0:
            diff = stdout

    # Bead info
    bead = await run_cli_json(["bd", "show", bead_id, "--json"])

    # Session log availability
    has_session = bool(_find_session_files(run_name))

    is_live = row.get("status") == "RUNNING" if row else False

    return JSONResponse({
        "run": run_name,
        "bead_id": bead_id,
        "bead": bead,
        "decision": decision,
        "experience_report": experience,
        "commit_hash": commit_hash,
        "branch": branch,
        "diff": diff,
        "has_session": has_session,
        "is_live": is_live,
        "duration_secs": duration_secs,
        "commit_message": commit_message,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "files_changed": files_changed,
    })

async def api_search(request):
    q = request.query_params.get("q", "")
    if not q:
        return JSONResponse({"error": "missing q parameter"})
    cmd = ["graph", "search", q, "--json", "--limit", request.query_params.get("limit", "20")]
    project = request.query_params.get("project")
    if project:
        cmd += ["--project", project]
    if request.query_params.get("or"):
        cmd += ["--or"]
    return JSONResponse(await run_cli_json(cmd))

async def api_sources(request):
    cmd = ["graph", "sources"]
    project = request.query_params.get("project")
    if project:
        cmd += ["--project", project]
    stype = request.query_params.get("type")
    if stype:
        cmd += ["--type", stype]
    cmd += ["--limit", request.query_params.get("limit", "30")]
    stdout, stderr, rc = await run_cli(cmd)
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_source_read(request):
    source_id = request.path_params["id"]
    max_chars = request.query_params.get("max_chars", "50000")
    return JSONResponse(await run_cli_json(
        ["graph", "read", source_id, "--json", "--max-chars", max_chars, "--first"]
    ))

async def api_context(request):
    source_id = request.path_params["id"]
    turn = request.path_params["turn"]
    window = request.query_params.get("window", "3")
    stdout, stderr, rc = await run_cli(["graph", "context", source_id, turn, "--window", window])
    return JSONResponse({"content": stdout, "error": stderr if rc != 0 else None})

async def api_projects(request):
    stdout, stderr, rc = await run_cli(["graph", "projects"])
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_stats(request):
    stdout, stderr, rc = await run_cli(["graph", "stats"])
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_attention(request):
    cmd = ["graph", "attention"]
    last = request.query_params.get("last")
    if last:
        cmd += ["--last", last]
    search = request.query_params.get("search")
    if search:
        cmd += ["--search", search]
    stdout, stderr, rc = await run_cli(cmd)
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_active_sessions(request):
    """Find currently active Claude Code sessions (JSONL files still being written)."""
    import time
    from pathlib import Path

    threshold = int(request.query_params.get("threshold", "300"))  # seconds
    projects_dir = Path.home() / ".claude" / "projects"
    now = time.time()
    sessions = []

    if projects_dir.exists():
        for jsonl in projects_dir.rglob("*.jsonl"):
            try:
                stat = jsonl.stat()
                age = now - stat.st_mtime
                if age < threshold:
                    # Get last line for latest activity
                    last_line = ""
                    with open(jsonl, "rb") as f:
                        f.seek(max(0, stat.st_size - 2000))
                        last_line = f.read().decode("utf-8", errors="replace")

                    # Extract latest user or assistant text
                    latest = ""
                    import json as _json
                    for line in reversed(last_line.strip().split("\n")):
                        try:
                            e = _json.loads(line)
                            if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
                                msg = e.get("message", {})
                                content = msg.get("content", "")
                                if isinstance(content, str) and len(content) > 5:
                                    latest = content[:150]
                                    break
                                elif isinstance(content, list):
                                    for c in content:
                                        if isinstance(c, dict) and c.get("type") == "text":
                                            latest = c["text"][:150]
                                            break
                                    if latest:
                                        break
                        except _json.JSONDecodeError:
                            continue

                    sessions.append({
                        "session_id": jsonl.stem,
                        "project": jsonl.parent.name,
                        "size_bytes": stat.st_size,
                        "age_seconds": round(age),
                        "active": age < 60,
                        "latest": latest,
                    })
            except OSError:
                continue

    sessions.sort(key=lambda s: s["age_seconds"])
    return JSONResponse(sessions)


async def api_terminals(request):
    """List active terminal sessions (tmux-backed)."""
    live = _list_dashboard_tmux()
    # Clean up tracking for dead sessions
    dead = [k for k in _active_terminals if k not in live]
    for k in dead:
        del _active_terminals[k]
    # Detect and cache type for any untracked sessions (e.g. after server restart)
    for name in live:
        if name not in _active_terminals:
            info = _detect_terminal_type(name)
            info["started"] = asyncio.get_event_loop().time()
            _active_terminals[name] = info
    return JSONResponse([
        {"id": name, "alive": True, **_active_terminals[name]}
        for name in live
    ])

async def api_terminal_kill(request):
    """Kill a terminal session. If it's a Chat With session, ingest it into the graph."""
    name = request.path_params["id"]
    if _tmux_session_exists(name):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        _active_terminals.pop(name, None)
        # Ingest the completed session into the graph (fire-and-forget)
        if name.startswith("chatwith-"):
            asyncio.create_task(asyncio.to_thread(
                subprocess.run,
                ["graph", "sessions", "--all"],
                capture_output=True, timeout=30,
                cwd=str(Path(__file__).parents[2]),
            ))
        return JSONResponse({"status": "killed", "id": name})
    return JSONResponse({"status": "not_found", "id": name})


async def api_terminal_rename(request):
    """Rename a terminal session's display name."""
    name = request.path_params["id"]
    body = await request.json()
    new_name = body.get("name", "").strip()
    if name in _active_terminals:
        _active_terminals[name]["name"] = new_name if new_name else None
        return JSONResponse({"ok": True, "id": name, "name": new_name})
    return JSONResponse({"error": "not found"}, status_code=404)


async def api_primer(request):
    bead_id = request.path_params["id"]
    stdout, stderr, rc = await run_cli(["graph", "primer", bead_id, "--format", "dashboard"])
    if rc != 0:
        return JSONResponse({"error": stderr or "primer generation failed"}, status_code=500)
    try:
        import json as _json
        return JSONResponse(_json.loads(stdout))
    except (ValueError, TypeError):
        # Fallback: return raw content if JSON parsing fails
        return JSONResponse({"content": stdout, "error": None})


async def api_chatwith_primer(request):
    """Return a Chat With primer for a specific page type and context ID.

    GET /api/chatwith/primer/{page_type}?context={id}

    Returns {primer_text: str, session_name: str}.
    Returns 400 for unknown page_type or missing context param.
    Returns 404 if the context resource is not found.
    """
    from tools.dashboard.chatwith_primers import get_primer, VALID_PAGE_TYPES

    page_type = request.path_params["page_type"]
    context_id = request.query_params.get("context", "").strip()

    if not context_id:
        return JSONResponse(
            {"error": "Missing required query parameter: context"},
            status_code=400,
        )

    try:
        result = await asyncio.to_thread(get_primer, page_type, context_id)
        return JSONResponse(result)
    except ValueError as exc:
        msg = str(exc)
        if "Unknown page type" in msg:
            return JSONResponse(
                {"error": msg, "valid_types": VALID_PAGE_TYPES},
                status_code=400,
            )
        # Resource not found (experiment missing, etc.)
        return JSONResponse({"error": msg}, status_code=404)


async def api_chatwith_spawn(request):
    """Spawn a Chat With Claude session for a given page context.

    POST /api/chatwith/spawn
    Body: {page_type: str, context_id: str}

    Creates a tmux session named "chatwith-{context_id}" running Claude,
    injects the primer, and returns {session_name, ws_url, new}.
    If the session already exists, returns it without re-creating.
    """
    from tools.dashboard.chatwith_primers import get_primer, VALID_PAGE_TYPES

    body = await request.json()
    page_type = (body.get("page_type") or "").strip()
    context_id = (body.get("context_id") or "").strip()

    if not page_type or not context_id:
        return JSONResponse({"error": "Missing page_type or context_id"}, status_code=400)

    try:
        primer_result = await asyncio.to_thread(get_primer, page_type, context_id)
    except ValueError as exc:
        msg = str(exc)
        if "Unknown page type" in msg:
            return JSONResponse({"error": msg, "valid_types": VALID_PAGE_TYPES}, status_code=400)
        return JSONResponse({"error": msg}, status_code=404)

    session_name = primer_result["session_name"]
    primer_text = primer_result["primer_text"]

    already_exists = _tmux_session_exists(session_name)
    if not already_exists:
        # Launch Claude in a container (read-only repo, isolated from host)
        # launch_session returns a shell-safe docker run -it --rm command string.
        docker_cmd = launch_session(
            session_type="chatwith",
            name=session_name,
            prompt=None,
            metadata={"context_id": context_id, "page_type": page_type},
            detach=False,
        )
        if not docker_cmd:
            return JSONResponse({"error": "Failed to resolve credentials for container"}, status_code=500)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name,
             "-x", "220", "-y", "50", docker_cmd],
            env={**os.environ, "TERM": "xterm-256color"},
        )
        subprocess.run(
            ["tmux", "set-option", "-t", session_name, "mouse", "on"],
            capture_output=True,
        )

        # Wait for Claude to initialize and display its prompt
        await asyncio.sleep(4)

        # Write primer to a temp file and inject via tmux paste-buffer
        primer_path = f"/tmp/chatwith_primer_{context_id}.txt"
        Path(primer_path).write_text(primer_text, encoding="utf-8")
        subprocess.run(
            ["tmux", "load-buffer", "-b", "cw_primer", primer_path],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-b", "cw_primer", "-t", session_name],
            capture_output=True,
        )
        await asyncio.sleep(0.3)
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "", "Enter"],
            capture_output=True,
        )

    ws_url = f"/ws/terminal?attach={session_name}"
    return JSONResponse({
        "session_name": session_name,
        "ws_url": ws_url,
        "new": not already_exists,
    })


async def api_chatwith_check(request):
    """Check if a Chat With tmux session exists.

    GET /api/chatwith/check?session={session_name}
    Returns {exists: bool, session_name: str}.
    """
    session_name = request.query_params.get("session", "").strip()
    if not session_name:
        return JSONResponse({"error": "Missing session parameter"}, status_code=400)
    exists = _tmux_session_exists(session_name)
    return JSONResponse({"exists": exists, "session_name": session_name})


async def api_chatwith_sessions(request):
    """List all active Chat With tmux sessions.

    GET /api/chatwith/sessions
    Returns {sessions: [session_name, ...]} for all tmux sessions prefixed 'chatwith-'.
    """
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return JSONResponse({"sessions": []})
    sessions = [s for s in result.stdout.strip().split("\n") if s.startswith("chatwith-")]
    return JSONResponse({"sessions": sessions})


# ── Live Session Tailing ──────────────────────────────────────


def _tool_headline(name: str, inp: dict) -> str:
    """Extract a human-readable headline from a tool_use input dict.

    Returns a short string like ``Read path/to/file.py`` suitable for display
    in the collapsed summary of a tool call.
    """
    n = (name or "").lower()

    if n == "read":
        fp = inp.get("file_path", "")
        return f"`{fp}`" if fp else ""

    if n == "bash":
        cmd = inp.get("command", "")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"`{cmd}`" if cmd else ""

    if n == "write":
        fp = inp.get("file_path", "")
        content = inp.get("content", "")
        n_lines = content.count("\n") + 1 if content else 0
        return f"`{fp}` ({n_lines} lines)" if fp else ""

    if n == "edit":
        fp = inp.get("file_path", "")
        old = inp.get("old_string", "")
        preview = old.replace("\n", " ").strip()
        if len(preview) > 40:
            preview = preview[:37] + "..."
        extra = f' "{preview}"' if preview else ""
        return f"`{fp}`{extra}" if fp else ""

    if n == "grep":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        suffix = f" in `{path}`" if path else ""
        return f"`{pat}`{suffix}" if pat else ""

    if n == "glob":
        pat = inp.get("pattern", "")
        return f"`{pat}`" if pat else ""

    if n == "agent":
        desc = inp.get("description", "")
        return desc if desc else ""

    if n == "todowrite":
        todos = inp.get("todos", [])
        return f"{len(todos)} items" if todos else ""

    # Fallback: show first string-valued param, truncated
    for v in inp.values():
        if isinstance(v, str) and v:
            preview = v.replace("\n", " ").strip()
            if len(preview) > 50:
                preview = preview[:47] + "..."
            return preview
    return ""


def _parse_jsonl_entry(line: str) -> dict | None:
    """Parse a single JSONL line into a display entry."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None

    entry_type = raw.get("type")
    timestamp = raw.get("timestamp", "")
    is_sidechain = raw.get("isSidechain", False)

    # Skip non-content entries
    if entry_type in ("queue-operation", "progress", "system"):
        return None
    if is_sidechain:
        return None

    message = raw.get("message", {})
    role = message.get("role", entry_type)
    content_raw = message.get("content", "")

    if entry_type == "user":
        text = ""
        if isinstance(content_raw, str):
            text = content_raw
        elif isinstance(content_raw, list):
            for block in content_raw:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")
        if not text:
            return None
        return {
            "type": "user",
            "role": "user",
            "content": text[:2000],
            "timestamp": timestamp,
        }

    if entry_type == "assistant" and isinstance(content_raw, list):
        # Expand assistant content blocks into sub-entries
        blocks = []
        for block in content_raw:
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    blocks.append({
                        "type": "assistant_text",
                        "role": "assistant",
                        "content": text,
                        "timestamp": timestamp,
                    })
            elif btype == "tool_use":
                tool_input = block.get("input", {})
                tool_name = block.get("name", "?")
                headline = _tool_headline(tool_name, tool_input)
                # Truncate large inputs for display
                input_str = json.dumps(tool_input, indent=2)
                if len(input_str) > 3000:
                    input_str = input_str[:3000] + "\n... (truncated)"
                blocks.append({
                    "type": "tool_use",
                    "role": "assistant",
                    "tool_name": tool_name,
                    "tool_headline": headline,
                    "tool_id": block.get("id", ""),
                    "content": input_str,
                    "timestamp": timestamp,
                })
            elif btype == "thinking":
                thinking = block.get("thinking", "").strip()
                if thinking:
                    blocks.append({
                        "type": "thinking",
                        "role": "assistant",
                        "content": thinking[:1000],
                        "timestamp": timestamp,
                    })
        return blocks if blocks else None

    if entry_type == "tool_result":
        # Tool results can be large; extract just the summary
        tool_id = raw.get("toolUseId", "")
        result_content = ""
        if isinstance(content_raw, str):
            result_content = content_raw
        elif isinstance(content_raw, list):
            for block in content_raw:
                if isinstance(block, dict) and block.get("type") == "text":
                    result_content += block.get("text", "")
        if len(result_content) > 2000:
            result_content = result_content[:2000] + "\n... (truncated)"
        if not result_content:
            return None
        return {
            "type": "tool_result",
            "role": "tool",
            "tool_id": tool_id,
            "content": result_content,
            "timestamp": timestamp,
        }

    return None


def _find_session_files(run_name: str) -> list[Path]:
    """Find JSONL session files for a run, checking multiple locations."""
    # 1. Run directory sessions — use rglob because Claude Code writes JSONL
    #    into a subdirectory (e.g. sessions/-workspace-repo/<hash>.jsonl)
    run_dir = AGENT_RUNS_DIR / run_name
    sessions_dir = run_dir / "sessions"
    if sessions_dir.exists():
        files = sorted(sessions_dir.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        if files:
            return files

    return []


async def api_dispatch_tail(request):
    """Tail JSONL session data for a dispatch run.

    Returns parsed entries after a byte offset for incremental polling.
    GET /api/dispatch/tail/{run}?after=N
    """
    run_name = request.path_params["run"]
    after = int(request.query_params.get("after", "0"))

    session_files = _find_session_files(run_name)
    if not session_files:
        # Try docker exec fallback for running containers
        entries, new_offset, is_live = await _tail_from_container(run_name, after)
        if entries is not None:
            return JSONResponse({
                "entries": entries,
                "offset": new_offset,
                "is_live": is_live,
            })
        return JSONResponse({"entries": [], "offset": 0, "is_live": False})

    # Read from the largest/most recent session file
    session_file = session_files[-1]
    file_size = session_file.stat().st_size
    is_live = (import_time() - session_file.stat().st_mtime) < 120

    if after >= file_size:
        return JSONResponse({"entries": [], "offset": file_size, "is_live": is_live})

    entries = []
    with open(session_file, "rb") as f:
        f.seek(after)
        data = f.read()
        new_offset = after + len(data)

        text = data.decode("utf-8", errors="replace")
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = _parse_jsonl_entry(line)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                entries.extend(parsed)
            else:
                entries.append(parsed)

    return JSONResponse({
        "entries": entries,
        "offset": new_offset,
        "is_live": is_live,
    })


def import_time():
    """Lazy import of time.time()."""
    import time
    return time.time()


async def _tail_from_container(run_name: str, after: int) -> tuple:
    """Try to tail session data from a running container via docker exec."""
    # Extract bead ID from run name
    parts = run_name.rsplit("-", 2)
    if len(parts) < 3:
        return None, 0, False

    bead_id = parts[0]

    # Find running container for this bead
    stdout, _, rc = await run_cli(
        ["docker", "ps", "--filter", f"name=agent-{bead_id}", "--format", "{{.Names}}"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None, 0, False

    container_name = stdout.strip().split("\n")[0]

    # Find session files inside container
    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         "ls -t /home/agent/.claude/projects/*/*.jsonl 2>/dev/null | head -1"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None, 0, False

    session_path = stdout.strip()

    # Read from offset using tail -c (O(1) seek, unlike dd bs=1 which is O(N))
    if after > 0:
        # tail -c +N starts reading at byte N (1-indexed)
        stdout, _, rc = await run_cli(
            ["docker", "exec", container_name, "sh", "-c",
             f"tail -c +{after + 1} '{session_path}'"],
            timeout=10,
        )
    else:
        stdout, _, rc = await run_cli(
            ["docker", "exec", container_name, "cat", session_path],
            timeout=10,
        )

    if rc != 0:
        return None, 0, False

    entries = []
    new_offset = after + len(stdout.encode("utf-8"))
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_jsonl_entry(line)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            entries.extend(parsed)
        else:
            entries.append(parsed)

    return entries, new_offset, True


async def _latest_from_container(run_name: str) -> dict | None:
    """Get latest assistant text from container using tail (not cat of entire file)."""
    parts = run_name.rsplit("-", 2)
    if len(parts) < 3:
        return None

    bead_id = parts[0]

    stdout, _, rc = await run_cli(
        ["docker", "ps", "--filter", f"name=agent-{bead_id}", "--format", "{{.Names}}"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None

    container_name = stdout.strip().split("\n")[0]

    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         "ls -t /home/agent/.claude/projects/*/*.jsonl 2>/dev/null | head -1"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None

    session_path = stdout.strip()

    # Get file size for token estimation
    size_stdout, _, size_rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         f"stat -c %s '{session_path}' 2>/dev/null || echo 0"],
        timeout=5,
    )
    file_size_bytes = int(size_stdout.strip()) if size_rc == 0 and size_stdout.strip().isdigit() else 0

    # Only read last 4KB — enough to find the latest assistant text
    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         f"tail -c 4096 '{session_path}'"],
        timeout=5,
    )
    if rc != 0:
        return None

    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_jsonl_entry(line)
        if parsed is None:
            continue
        entries = parsed if isinstance(parsed, list) else [parsed]
        for e in reversed(entries):
            if e.get("type") == "assistant_text":
                return {
                    "text": e["content"][:100],
                    "timestamp": e.get("timestamp", ""),
                    "type": "assistant_text",
                    "is_live": True,
                    "file_size_bytes": file_size_bytes,
                }

    return None


async def api_dispatch_latest(request):
    """Return just the most recent entry for snippet display.

    GET /api/dispatch/latest/{run}
    Returns {text, timestamp, type, is_live, file_size_bytes}.
    file_size_bytes enables rough token estimation on the client (÷4).
    """
    run_name = request.path_params["run"]
    session_files = _find_session_files(run_name)

    if not session_files:
        # Try container fallback — use _latest_from_container to avoid
        # catting the entire session file every poll
        result = await _latest_from_container(run_name)
        if result:
            return JSONResponse(result)
        return JSONResponse({"text": "", "timestamp": "", "type": "", "is_live": False, "file_size_bytes": 0})

    session_file = session_files[-1]
    is_live = (import_time() - session_file.stat().st_mtime) < 120

    # Read last ~4KB to find latest assistant text
    file_size = session_file.stat().st_size
    read_from = max(0, file_size - 4096)
    with open(session_file, "rb") as f:
        f.seek(read_from)
        data = f.read().decode("utf-8", errors="replace")

    # Parse lines in reverse to find latest assistant text
    for line in reversed(data.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_jsonl_entry(line)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            for entry in reversed(parsed):
                if entry.get("type") == "assistant_text":
                    return JSONResponse({
                        "text": entry["content"][:100],
                        "timestamp": entry.get("timestamp", ""),
                        "type": "assistant_text",
                        "is_live": is_live,
                        "file_size_bytes": file_size,
                    })
        elif parsed.get("type") == "assistant_text":
            return JSONResponse({
                "text": parsed["content"][:100],
                "timestamp": parsed.get("timestamp", ""),
                "type": "assistant_text",
                "is_live": is_live,
                "file_size_bytes": file_size,
            })

    return JSONResponse({"text": "", "timestamp": "", "type": "", "is_live": is_live, "file_size_bytes": file_size})


# ── Session Tail & Send API ────────────────────────────────────

def _session_file_path(project: str, session_id: str) -> Path | None:
    """Resolve the JSONL path for a session, guarding against path traversal."""
    # Neither component may contain path separators
    if "/" in project or "\\" in project or "/" in session_id or "\\" in session_id:
        return None
    return Path.home() / ".claude" / "projects" / project / f"{session_id}.jsonl"


async def api_session_tail(request):
    """Tail JSONL entries for any session by project/session_id.

    GET /api/session/{project}/{session_id}/tail?after=N
    Returns {entries: [...], offset: N, is_live: bool}.
    Increment `after` with each poll to receive only new entries.
    """
    import time as _time

    project = request.path_params["project"]
    session_id = request.path_params["session_id"]
    after = int(request.query_params.get("after", "0"))

    session_file = _session_file_path(project, session_id)
    if session_file is None:
        return JSONResponse({"error": "Invalid project or session_id"}, status_code=400)
    if not session_file.exists():
        return JSONResponse({"error": "Session not found"}, status_code=404)

    file_size = session_file.stat().st_size
    is_live = (_time.time() - session_file.stat().st_mtime) < 120

    if after >= file_size:
        return JSONResponse({"entries": [], "offset": file_size, "is_live": is_live})

    entries = []
    with open(session_file, "rb") as f:
        f.seek(after)
        data = f.read()
        new_offset = after + len(data)
        text = data.decode("utf-8", errors="replace")
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = _parse_jsonl_entry(line)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                entries.extend(parsed)
            else:
                entries.append(parsed)

    return JSONResponse({"entries": entries, "offset": new_offset, "is_live": is_live})


async def api_session_send(request):
    """Send a message to a tmux-managed session via paste-buffer injection.

    POST /api/session/send
    POST /api/session/{project}/{session_id}/send  (project/session_id ignored)
    Body: {"tmux_session": "auto-t2", "message": "text"}

    Only works for tmux-managed sessions (terminal, chatwith, dispatch agents).
    Host interactive sessions have no stdin injection path — returns 404.
    Returns 400 if tmux_session or message is not provided.
    Returns 404 if the tmux session does not exist.
    Returns 503 if tmux is not available in this environment.
    """
    import tempfile
    import os

    body = await request.json()
    message = (body.get("message") or "")
    tmux_session = (body.get("tmux_session") or "").strip()

    if not tmux_session:
        return JSONResponse(
            {"error": "tmux_session is required. "
                       "Host interactive sessions have no stdin injection path."},
            status_code=400,
        )
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    try:
        exists = _tmux_session_exists(tmux_session)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )

    if not exists:
        return JSONResponse(
            {"error": f"tmux session '{tmux_session}' not found"},
            status_code=404,
        )

    # Write message to a temp file and inject via paste-buffer (same pattern as
    # chatwith primer injection, server.py lines 1074-1089).
    buf_name = "api_send"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(message)
            tmp_path = f.name
        subprocess.run(
            ["tmux", "load-buffer", "-b", buf_name, tmp_path],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-b", buf_name, "-t", tmux_session],
            capture_output=True,
        )
        await asyncio.sleep(0.1)
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_session, "", "Enter"],
            capture_output=True,
        )
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    return JSONResponse({"ok": True, "tmux_session": tmux_session})


# ── WebSocket Terminal ─────────────────────────────────────────

# Track active terminal sessions
_active_terminals: dict[str, dict] = {}
_term_counter = 0


def _tmux_session_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0


def _list_dashboard_tmux() -> list[str]:
    """List all tmux sessions created by the dashboard (prefixed 'auto-')."""
    result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().split("\n") if s.startswith("auto-")]


def _detect_terminal_type(tmux_name: str) -> dict:
    """Detect terminal session type for untracked sessions (e.g. after server restart)."""
    detected_cmd = "/bin/bash"
    detected_env = "host"
    # Check if a docker container with this name is running -> container session
    cr = subprocess.run(
        ["docker", "inspect", "-f", "{{.Path}}", tmux_name],
        capture_output=True, text=True,
    )
    if cr.returncode == 0:
        detected_env = "container"
        entrypoint = cr.stdout.strip()
        if "bash" in entrypoint or entrypoint == "sh":
            detected_cmd = "autonomy-agent-bash"
        else:
            detected_cmd = "autonomy-agent-claude"
    else:
        # Host session -- check tmux pane for claude
        pr = subprocess.run(
            ["tmux", "display-message", "-t", tmux_name, "-p",
             "#{pane_start_command} #{pane_current_command}"],
            capture_output=True, text=True,
        )
        pane_info = pr.stdout.strip().lower() if pr.returncode == 0 else ""
        if "claude" in pane_info:
            detected_cmd = "claude --dangerously-skip-permissions"
    return {"cmd": detected_cmd, "env": detected_env}


async def ws_terminal(websocket: WebSocket):
    """WebSocket endpoint that bridges xterm.js to a tmux session.

    All terminals run inside tmux so they persist across page navigations.
    The WebSocket just attaches/detaches — the process keeps running.

    Query params:
      cmd     — command to run in a new tmux session (default: bash)
      attach  — existing tmux session name to attach to
      id      — terminal session ID (auto-generated if not provided)
    """
    global _term_counter
    await websocket.accept()

    params = websocket.query_params
    attach = params.get("attach")
    term_id = params.get("id")

    if attach:
        # Attach to existing tmux session
        if not _tmux_session_exists(attach):
            await websocket.send_text(f"\r\n\x1b[31mSession '{attach}' not found\x1b[0m\r\n")
            await websocket.close()
            return
        tmux_name = attach
        # Ensure tmux mouse mode is on for reattached sessions so scroll
        # wheel triggers tmux copy-mode (scrollback lives in tmux, not xterm.js).
        # Users hold Shift to select text at the browser level.
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "mouse", "on"],
                        capture_output=True)
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "set-clipboard", "on"],
                        capture_output=True)
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "allow-passthrough", "on"],
                        capture_output=True)
    else:
        # Create a new tmux session
        cmd_str = params.get("cmd", "/bin/bash")
        _term_counter += 1
        if not term_id:
            term_id = f"auto-t{_term_counter}"
        tmux_name = term_id

        # Resolve special container commands
        if cmd_str == "autonomy-agent-claude":
            # launch_session returns a shell-safe docker run -it --rm command string
            launched = launch_session(
                session_type="terminal",
                name=tmux_name,
                prompt=None,
                detach=False,
                image="autonomy-agent:dashboard",
            )
            if launched:
                cmd_str = launched
        elif cmd_str == "autonomy-agent-bash":
            repo_root = str(Path(__file__).parents[2])
            cmd_str = (
                f"docker run -it --rm --name {tmux_name}"
                f" --network=host"
                f" --entrypoint /bin/bash"
                f" -v {repo_root}:/workspace/repo:ro"
                f" -v {repo_root}/.beads:/data/.beads"
                f" autonomy-agent"
            )

        # Create detached tmux session running the command
        # Enable set-clipboard so OSC 52 passes through to xterm.js
        subprocess.run([
            "tmux", "new-session", "-d", "-s", tmux_name,
            "-x", "120", "-y", "40",
            cmd_str,
        ], env={**os.environ, "TERM": "xterm-256color"})
        # Enable OSC 52 clipboard passthrough in this session
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "set-clipboard", "on"],
                        capture_output=True)
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "allow-passthrough", "on"],
                        capture_output=True)
        # Enable tmux mouse mode so scroll wheel triggers tmux copy-mode
        # (scrollback history lives in tmux, not xterm.js alternate buffer).
        # Text selection: hold Shift to bypass tmux and select at browser level.
        # Paste is handled at the xterm.js layer using the browser clipboard API.
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "mouse", "on"],
                        capture_output=True)

    # Track it — only set cmd/env on initial creation, not re-attach
    if tmux_name not in _active_terminals:
        if attach:
            # Reconnecting to a session we lost track of (server restart)
            info = _detect_terminal_type(tmux_name)
            info["started"] = asyncio.get_event_loop().time()
            _active_terminals[tmux_name] = info
        else:
            orig_cmd = params.get("cmd", "/bin/bash")
            is_container = "autonomy-agent" in orig_cmd
            _active_terminals[tmux_name] = {
                "cmd": orig_cmd,
                "env": "container" if is_container else "host",
                "started": asyncio.get_event_loop().time(),
            }

    # Now attach to the tmux session via a PTY
    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", 40, 120, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", tmux_name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"},
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave_fd)

    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    alive = True

    async def read_pty():
        nonlocal alive
        try:
            while alive:
                await asyncio.sleep(0.02)
                try:
                    data = os.read(master_fd, 65536)
                    if data:
                        await websocket.send_text(data.decode("utf-8", errors="replace"))
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if proc.poll() is not None:
                    break
        except Exception:
            pass
        alive = False

    reader_task = asyncio.create_task(read_pty())

    try:
        while alive:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "text" in msg:
                data = msg["text"]
                if data.startswith("\x1b[8;"):
                    try:
                        parts = data[4:-1].split(";")
                        rows, cols = int(parts[0]), int(parts[1])
                        ws_bytes = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws_bytes)
                        os.kill(proc.pid, signal.SIGWINCH)
                    except (ValueError, IndexError, OSError):
                        pass
                else:
                    try:
                        os.write(master_fd, data.encode("utf-8"))
                    except OSError:
                        break
            elif "bytes" in msg:
                try:
                    os.write(master_fd, msg["bytes"])
                except OSError:
                    break
    except Exception:
        pass
    finally:
        alive = False
        reader_task.cancel()
        # DON'T kill the tmux session — it persists for reconnection
        # Just detach by killing the attach process
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


async def page_timeline(request):
    return HTMLResponse(_load_template("base.html"))

async def page_timeline_fragment(request):
    """Return the Timeline page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/timeline.html")

async def page_trace_fragment(request):
    """Return the Trace page as an HTML fragment for SPA injection.

    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="tracePage()" component.
    The component reads the run name from window.location.pathname on init.
    """
    return templates.TemplateResponse(request, "pages/trace.html")

async def page_terminal(request):
    return HTMLResponse(_load_template("base.html"))

async def page_terminal_fragment(request):
    """Return the Terminal page chrome as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/terminal.html")


async def page_session_view(request):
    return HTMLResponse(_load_template("base.html"))

async def page_session_view_fragment(request):
    """Return the Session Viewer page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/session-view.html")


# ── Experiments API ────────────────────────────────────────────

async def api_experiments_create(request):
    """Create a new experiment. Returns {id: uuid}."""
    body = await request.json()
    title = body.get("title", "Untitled Experiment")
    description = body.get("description")
    fixture = body.get("fixture")  # JSON string or dict
    variants = body.get("variants", [])
    series_id = body.get("series_id")  # optional — links experiment into a series

    if not variants:
        return JSONResponse({"error": "At least one variant required"}, status_code=400)

    # Ensure fixture is stored as JSON string
    if fixture and not isinstance(fixture, str):
        fixture = json.dumps(fixture)

    exp_id = await asyncio.to_thread(
        create_experiment,
        title=title,
        description=description,
        fixture=fixture,
        variants=variants,
        series_id=series_id,
    )

    # Broadcast to SSE so gallery pages auto-update without refresh
    exp_data = await asyncio.to_thread(get_experiment, exp_id)
    if exp_data:
        topic = f"experiments:{exp_data['series_id']}"
        await event_bus.broadcast(topic, {
            "experiment_id": exp_id,
            "series_id": exp_data["series_id"],
            "series_seq": exp_data["series_seq"],
        })

    return JSONResponse({"id": exp_id}, status_code=201)


async def api_experiments_poll(request):
    """Poll experiment status. 202 while pending, 200 with results when completed."""
    exp_id = request.path_params["id"]
    exp = await asyncio.to_thread(get_experiment, exp_id)
    if not exp:
        return JSONResponse({"error": "not found"}, status_code=404)

    if exp["status"] == "pending":
        return JSONResponse({"status": "pending", "id": exp_id}, status_code=202)

    # Completed — return results
    results = []
    for v in exp["variants"]:
        if v["selected"]:
            results.append({"id": v["id"], "rank": v["rank"]})
    results.sort(key=lambda x: x["rank"] or 999)
    return JSONResponse({
        "status": "completed",
        "id": exp_id,
        "results": results,
    })


async def api_experiments_get(request):
    """Get full experiment data for gallery rendering."""
    exp_id = request.path_params["id"]
    exp = await asyncio.to_thread(get_experiment, exp_id)
    if not exp:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(exp)


async def api_experiments_submit(request):
    """Submit ranking results."""
    exp_id = request.path_params["id"]
    body = await request.json()
    selections = body.get("selections", [])
    ok = await asyncio.to_thread(submit_results, exp_id, selections)
    if not ok:
        return JSONResponse({"error": "experiment not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_experiments_pending(request):
    """List pending experiments (for toast notifications)."""
    pending = await asyncio.to_thread(list_pending_experiments)
    return JSONResponse(pending)


async def api_experiments_dismiss(request):
    """Dismiss an experiment and all pending siblings in its series."""
    exp_id = request.path_params["id"]
    ok = await asyncio.to_thread(dismiss_experiment, exp_id)
    if not ok:
        return JSONResponse({"error": "experiment not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_experiments_screenshot(request):
    """Save a screenshot blob for an experiment.

    POST /api/experiments/{id}/screenshot
    Body: raw image bytes (Content-Type: image/png or image/*)
    Returns: {path: "/absolute/path/to/screenshot.png"}
    """
    exp_id = request.path_params["id"]
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return JSONResponse({"error": "content-type must be image/*"}, status_code=400)

    exp = await asyncio.to_thread(get_experiment, exp_id)
    if not exp:
        return JSONResponse({"error": "not found"}, status_code=404)

    screenshot_dir = _REPO_ROOT / "data" / "experiments" / exp_id
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / "screenshot.png"

    body = await request.body()
    await asyncio.to_thread(screenshot_path.write_bytes, body)

    abs_path = str(screenshot_path.resolve())

    return JSONResponse({"path": abs_path})


async def page_experiment(request):
    return HTMLResponse(_load_template("base.html"))

async def page_experiment_fragment(request):
    """Return the Experiment page as an HTML fragment for SPA injection.

    Rendered via Jinja2 so {% include %} partials work.
    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="experimentPage()" component.
    The component reads the experiment ID from window.location.pathname on init.
    """
    return templates.TemplateResponse(request, "pages/experiment.html")


# ── HTML Pages ────────────────────────────────────────────────

def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()

async def page_index(request):
    return RedirectResponse(url="/beads")

async def page_beads(request):
    return HTMLResponse(_load_template("base.html"))

async def page_beads_fragment(request):
    """Return the Beads page as an HTML fragment for SPA injection.

    Rendered via Jinja2. The fragment is injected into #content by the client
    router, then Alpine.initTree() initialises the x-data="beadsPage()" component.
    """
    return templates.TemplateResponse(request, "pages/beads.html")

async def page_dispatch(request):
    return HTMLResponse(_load_template("base.html"))

async def page_dispatch_fragment(request):
    """Return the Dispatch page as an HTML fragment for SPA injection.

    Rendered via Jinja2 so {% include %} partials work.
    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="dispatchPage()" component.
    """
    return templates.TemplateResponse(request, "pages/dispatch.html")

async def page_sessions(request):
    return HTMLResponse(_load_template("base.html"))

async def page_sessions_fragment(request):
    """Return the Sessions page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/sessions.html")

async def api_dao_active_sessions(request):
    threshold = int(request.query_params.get("threshold", "600"))
    sessions = await asyncio.to_thread(dao_sessions.get_active_sessions, threshold)
    return JSONResponse(sessions)

async def api_dao_recent_sessions(request):
    limit = int(request.query_params.get("limit", "20"))
    sessions = await asyncio.to_thread(dao_sessions.get_recent_sessions, limit)
    return JSONResponse(sessions)

async def page_search(request):
    return HTMLResponse(_load_template("base.html"))

async def page_search_fragment(request):
    """Return the Search page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/search.html")

async def page_source(request):
    return HTMLResponse(_load_template("base.html"))

async def page_source_fragment(request):
    """Return the Source/Context page as an HTML fragment for SPA injection.

    Handles both /source/{id} (full source) and /source/{id}?turn=N (context view).
    The Alpine sourcePage component reads URL params on init to select the right mode.
    """
    return templates.TemplateResponse(request, "pages/source.html")

async def page_bead(request):
    return HTMLResponse(_load_template("base.html"))

async def page_bead_fragment(request):
    """Return the Bead detail page as an HTML fragment for SPA injection.

    Rendered via Jinja2 so {% include %} partials work.
    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="beadDetailPage()" component.
    The component reads the bead ID from window.location.pathname on init.
    """
    return templates.TemplateResponse(request, "pages/bead-detail.html")

async def api_dao_bead(request):
    """Return a single bead with labels, deps, and comments via DAO (not bd CLI).

    GET /api/dao/bead/{id}

    Uses dao_beads.get_bead() which connects directly to Dolt/MySQL.
    Returns 404 JSON if the bead does not exist.
    In mock mode, reads from the fixture file.
    """
    bead_id = request.path_params["id"]
    bead = await asyncio.to_thread(dao_beads.get_bead, bead_id)
    if bead is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(bead)


# ── SSE EventBus endpoint ─────────────────────────────────────

async def api_events(request):
    """Server-Sent Events endpoint — global broadcast.

    GET /api/events

    Every connected client receives ALL topics. Each event is sent as:
        event: {topic}
        data: {json}

    The browser EventSource API handles reconnection automatically.
    """
    queue = event_bus.subscribe()

    async def event_generator():
        try:
            while True:
                topic, data = await queue.get()
                yield {"event": topic, "data": json.dumps(data)}
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())


# ── Background watchers ───────────────────────────────────────

_DISPATCH_WATCHER_INTERVAL = 5   # seconds between dispatch polls


async def _collect_dispatch_data() -> dict:
    """Collect data for the 'dispatch' topic: active, waiting, blocked.

    Active dispatches come from SQLite dispatch_runs WHERE status=RUNNING,
    enriched with Dolt bead metadata (title, priority, labels).
    Waiting/blocked come from Dolt (readiness:approved beads), with
    currently-running beads excluded to avoid double-counting.
    """
    # Get bead data and running runs concurrently
    bead_data, running_runs = await asyncio.gather(
        asyncio.to_thread(dao_beads.get_dispatch_beads),
        asyncio.to_thread(dao_dispatch.get_running_with_stats),
    )

    # Look up Dolt metadata for all running beads in a single query
    running_bead_ids = [r["bead_id"] for r in running_runs if r.get("bead_id")]
    bead_meta = await asyncio.to_thread(
        dao_beads.get_bead_title_priority, running_bead_ids
    )

    # Build active list from SQLite RUNNING runs + Dolt metadata
    active = []
    for run in running_runs:
        bead_id = run.get("bead_id", "")
        meta = bead_meta.get(bead_id, {})
        container = None
        if run.get("container_name"):
            container = {
                "name": run["container_name"],
                "image": run.get("image"),
                "status": None,
            }
        active.append({
            "id": bead_id,
            "title": meta.get("title") or bead_id,
            "priority": meta.get("priority"),
            "labels": meta.get("labels", []),
            "container": container,
            "run_dir": run.get("id"),
            "last_snippet": run.get("last_snippet"),
            "token_count": run.get("token_count"),
            "tool_count": run.get("tool_count"),
            "turn_count": run.get("turn_count"),
            "cpu_pct": run.get("cpu_pct"),
            "cpu_usec": run.get("cpu_usec"),
            "mem_mb": run.get("mem_mb"),
            "duration_secs": run.get("duration_secs") or (
                int(time.time() - datetime.fromisoformat(run["started_at"]).replace(tzinfo=timezone.utc).timestamp())
                if run.get("started_at") else None
            ),
            "last_activity": run.get("last_activity"),
        })

    # Exclude running beads from waiting/blocked to avoid double-counting
    running_ids = set(running_bead_ids)

    # Waiting: strip to minimal fields, exclude currently-running beads
    waiting = [
        {
            "id": b["id"], "title": b["title"],
            "priority": b["priority"], "labels": b.get("labels", []),
            "status": b.get("status"),
        }
        for b in bead_data["approved_waiting"]
        if b["id"] not in running_ids
    ]

    # Blocked: rename open_blockers → blockers, exclude currently-running beads
    blocked = [
        {
            "id": b["id"], "title": b["title"],
            "priority": b["priority"], "labels": b.get("labels", []),
            "status": b.get("status"),
            "blockers": b.get("open_blockers", []),
        }
        for b in bead_data["approved_blocked"]
        if b["id"] not in running_ids
    ]

    return {
        "active": active,
        "waiting": waiting,
        "blocked": blocked,
        "paused": _get_pause_state(),
    }


def _count_active_sessions(threshold: int = 600) -> int:
    """Count active Claude Code sessions (JSONL files modified within threshold seconds)."""
    import time as _time
    now = _time.time()
    return sum(1 for f in (Path.home() / ".claude" / "projects").rglob("*.jsonl")
               if now - f.stat().st_mtime < threshold)


def _count_terminals() -> int:
    """Count active dashboard tmux sessions."""
    return len(_list_dashboard_tmux())


def _count_today_done() -> int:
    """Count dispatch runs completed successfully today."""
    conn = _timeline_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM dispatch_runs WHERE status='DONE' AND completed_at >= date('now')"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


async def _dispatch_watcher():
    """Background task: poll dispatch state and broadcast to 'dispatch' and 'nav' topics.

    Both topics are derived from the same data collection pass so they always
    see a consistent snapshot. Nav counts are derived directly from the dispatch
    lists (running_agents=len(active), approved_waiting=len(waiting),
    approved_blocked=len(blocked)) plus open_count from get_bead_counts().

    Always collects and broadcasts regardless of subscriber count — this keeps
    the EventBus cache warm so newly-connecting clients receive cached data
    immediately on subscribe().
    """
    while True:
        try:
            dispatch_data, counts, active_sessions, terminal_count, today_done = (
                await asyncio.gather(
                    _collect_dispatch_data(),
                    asyncio.to_thread(dao_beads.get_bead_counts),
                    asyncio.to_thread(_count_active_sessions),
                    asyncio.to_thread(_count_terminals),
                    asyncio.to_thread(_count_today_done),
                )
            )
            nav_data = {
                "open_beads": counts.get("open_count", 0),
                "running_agents": len(dispatch_data["active"]),
                "approved_waiting": len(dispatch_data["waiting"]),
                "approved_blocked": len(dispatch_data["blocked"]),
                "active_sessions": active_sessions,
                "terminal_count": terminal_count,
                "today_done": today_done,
            }
            await event_bus.broadcast("dispatch", dispatch_data)
            await event_bus.broadcast("nav", nav_data)
        except Exception:
            pass
        await asyncio.sleep(_DISPATCH_WATCHER_INTERVAL)


# ── App ───────────────────────────────────────────────────────

routes = [
    # Pages
    Route("/", page_index),
    Route("/beads", page_beads),
    Route("/pages/beads", page_beads_fragment),
    Route("/dispatch", page_dispatch),
    Route("/dispatch/alpine", page_dispatch),
    Route("/dispatch/lit", page_dispatch),
    Route("/pages/dispatch", page_dispatch_fragment),
    Route("/sessions", page_sessions),
    Route("/pages/sessions", page_sessions_fragment),
    Route("/pages/bead", page_bead_fragment),
    Route("/pages/timeline", page_timeline_fragment),
    Route("/pages/trace", page_trace_fragment),
    Route("/search", page_search),
    Route("/pages/search", page_search_fragment),
    Route("/source/{id}", page_source),
    Route("/pages/source", page_source_fragment),
    Route("/bead/{id}", page_bead),
    Route("/timeline", page_timeline),
    Route("/terminal", page_terminal),
    Route("/terminal/{session_id}", page_terminal),
    Route("/pages/terminal", page_terminal_fragment),
    Route("/pages/experiment", page_experiment_fragment),
    Route("/session/{project}/{session_id}", page_session_view),
    Route("/pages/session-view", page_session_view_fragment),

    # WebSocket
    WebSocketRoute("/ws/terminal", ws_terminal),

    # Events (SSE)
    Route("/api/events", api_events),

    # API
    Route("/api/beads/ready", api_beads_ready),
    Route("/api/beads/list", api_beads_list),
    Route("/api/beads/search", api_beads_search),
    Route("/api/bead/{id}", api_bead_show),
    Route("/api/bead/{id}/tree", api_bead_tree),
    Route("/api/bead/{id}/approve", api_bead_approve, methods=["POST"]),
    Route("/api/dispatch/pause", api_dispatch_pause_get),
    Route("/api/dispatch/pause", api_dispatch_pause_post, methods=["POST"]),
    Route("/api/dispatch/status", api_dispatch_status),
    Route("/api/dispatch/approved", api_dispatch_approved),
    Route("/api/dispatch/runs", api_dispatch_runs),
    Route("/api/dispatch/trace/{run}", api_dispatch_trace),
    Route("/dispatch/trace/{run}", page_dispatch),
    Route("/api/search", api_search),
    Route("/api/sources", api_sources),
    Route("/api/source/{id}", api_source_read),
    Route("/api/context/{id}/{turn}", api_context),
    Route("/api/projects", api_projects),
    Route("/api/stats", api_stats),
    Route("/api/attention", api_attention),
    Route("/api/active", api_active_sessions),
    Route("/api/dao/active_sessions", api_dao_active_sessions),
    Route("/api/dao/recent_sessions", api_dao_recent_sessions),
    Route("/api/dao/bead/{id}", api_dao_bead),
    Route("/api/terminals", api_terminals),
    Route("/api/terminal/{id}/kill", api_terminal_kill),
    Route("/api/terminal/{id}/rename", api_terminal_rename, methods=["POST"]),
    Route("/api/primer/{id}", api_primer),
    Route("/api/chatwith/primer/{page_type}", api_chatwith_primer),
    Route("/api/chatwith/spawn", api_chatwith_spawn, methods=["POST"]),
    Route("/api/chatwith/check", api_chatwith_check),
    Route("/api/chatwith/sessions", api_chatwith_sessions),
    Route("/api/dispatch/tail/{run}", api_dispatch_tail),
    Route("/api/dispatch/latest/{run}", api_dispatch_latest),
    Route("/api/session/send", api_session_send, methods=["POST"]),
    Route("/api/session/{project}/{session_id}/tail", api_session_tail),
    Route("/api/session/{project}/{session_id}/send", api_session_send, methods=["POST"]),
    Route("/api/timeline", api_timeline),
    Route("/api/timeline/stats", api_timeline_stats),

    # Experiments
    Route("/api/experiments", api_experiments_create, methods=["POST"]),
    Route("/api/experiments/pending", api_experiments_pending),
    Route("/api/experiments/{id}", api_experiments_poll),
    Route("/api/experiments/{id}/full", api_experiments_get),
    Route("/api/experiments/{id}/dismiss", api_experiments_dismiss, methods=["POST"]),
    Route("/api/experiments/{id}/submit", api_experiments_submit, methods=["POST"]),
    Route("/api/experiments/{id}/screenshot", api_experiments_screenshot, methods=["POST"]),
    Route("/experiments/{id}", page_experiment),

    # Static
    Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

async def _on_startup():
    asyncio.create_task(_dispatch_watcher())
    if os.environ.get("DASHBOARD_MOCK_EVENTS"):
        from tools.dashboard.dao.mock import mock_event_watcher
        asyncio.create_task(mock_event_watcher())

class _CSPMiddleware(BaseHTTPMiddleware):
    """Phase 1 CSP: blocks dangerous injections while permitting existing inline scripts."""

    _CSP = (
        "default-src 'self'; "
        "script-src 'self' cdn.jsdelivr.net 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'"
    )

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = self._CSP
        return response


app = Starlette(routes=routes, on_startup=[_on_startup])
app.add_middleware(_CSPMiddleware)


def main():
    import uvicorn
    uvicorn.run(
        "tools.dashboard.server:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
        reload=True,
        reload_dirs=["tools/dashboard"],
    )
