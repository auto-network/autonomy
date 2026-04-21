"""Autonomy Dashboard — Starlette server.

Thin rendering layer over the bd and graph CLI tools.
Every view the dashboard shows, an agent can also produce via CLI.
"""

import asyncio
import fcntl
import hashlib
import json
import logging
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
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow importing from agents/ (sibling of tools/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket
from sse_starlette.sse import EventSourceResponse

from agents.dispatch_db import (
    list_runs, get_run, get_runs_for_bead, get_currently_running, DB_PATH,
    clear_paused, is_paused, get_pause_reason,
)
from agents.session_launcher import launch_session
from agents import workspace_settings
from agents.primer_renderer import render_workspace_primer
from agents.workspace_manager import WorkspaceError, prepare_session_mounts
if os.environ.get("DASHBOARD_MOCK"):
    from tools.dashboard.dao.mock import (
        create_design, get_design, submit_results,
        list_pending_designs, dismiss_design, resolve_design_prefix,
    )
else:
    from agents.design_db import (
        create_design, get_design, submit_results, list_pending as list_pending_designs,
        dismiss_design, resolve_design_prefix,
    )
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from tools.dashboard.event_bus import event_bus, _SERVER_EPOCH
from tools.dashboard.session_monitor import count_tool_uses, session_monitor, TaskStateTracker
from tools.dashboard.dao import auth_db, dashboard_db
if os.environ.get("DASHBOARD_MOCK"):
    from tools.dashboard.dao import mock as dao_beads
    from tools.dashboard.dao import mock as dao_dispatch
    from tools.dashboard.dao import mock as dao_sessions
else:
    from tools.dashboard.dao import beads as dao_beads
    from tools.dashboard.dao import dispatch as dao_dispatch
    from tools.dashboard.dao import sessions as dao_sessions

from tools.dashboard.tmux_send import tmux_send, tmux_send_sync
from tools.graph import ops as graph_ops


STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

def _static_version() -> str:
    import time as _time
    t0 = _time.monotonic()
    try:
        mtimes = [p.stat().st_mtime for p in (Path(__file__).parent / "static").rglob("*") if p.is_file()]
        version = str(int(max(mtimes))) if mtimes else str(int(_time.time()))
    except Exception:
        version = str(int(_time.time()))
    elapsed_ms = (_time.monotonic() - t0) * 1000
    logger.warning("[static_version] computed in %.1fms → %s", elapsed_ms, version)
    return version

DISPATCH_STATE_PATH = _REPO_ROOT / "data" / "dispatch.state"
# Labels always shown in pause UI even if not in dispatch.state
_KNOWN_PAUSE_LABELS = ["dashboard"]


# ── CLI Subprocess Helper ─────────────────────────────────────

async def run_cli(cmd: list[str], timeout: int = 30, stdin_data: str | None = None) -> tuple[str, str, int]:
    """Run a CLI command async and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        input_bytes = stdin_data.encode() if stdin_data is not None else None
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_bytes), timeout=timeout)
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
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_open_beads())
    return JSONResponse(await run_cli_json(["bd", "ready", "--json"]))

async def api_beads_list(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_open_beads(limit=100))
    return JSONResponse(await run_cli_json(["bd", "list", "--json", "-n", "100", "--sort", "updated"]))

async def api_bead_show(request):
    bead_id = request.path_params["id"]
    if os.environ.get("DASHBOARD_MOCK"):
        bead = dao_beads.get_bead(bead_id)
        if not bead:
            return JSONResponse({"error": "bead not found"}, status_code=404)
        return JSONResponse(bead)
    return JSONResponse(await run_cli_json(["bd", "show", bead_id, "--json"]))

async def api_bead_tree(request):
    bead_id = request.path_params["id"]
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_bead_deps(bead_id))
    return JSONResponse(await run_cli_json(["bd", "dep", "tree", bead_id, "--json"]))


async def api_bead_deps(request):
    """Return both blockers (down) and dependents (up) for a bead."""
    bead_id = request.path_params["id"]
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_bead_deps(bead_id))
    down, up = await asyncio.gather(
        run_cli_json(["bd", "dep", "list", bead_id, "--json"]),
        run_cli_json(["bd", "dep", "list", bead_id, "--direction=up", "--json"]),
    )
    blockers = down if isinstance(down, list) else []
    dependents = up if isinstance(up, list) else []
    return JSONResponse({"blockers": blockers, "dependents": dependents})


async def api_beads_search(request):
    """Search beads by title and description. Falls back to issues.jsonl if bd unavailable."""
    q = request.query_params.get("q", "").strip().lower()
    if not q:
        return JSONResponse({"error": "missing q parameter"})

    if os.environ.get("DASHBOARD_MOCK"):
        # Search mock beads by title/description
        beads = dao_beads.get_open_beads(limit=500)
        terms = q.split()
        results = [
            b for b in beads
            if all(t in f"{b.get('title', '')} {b.get('description', '')}".lower() for t in terms)
        ]
        return JSONResponse(results)

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
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "bead_id": bead_id})
    stdout, stderr, rc = await run_cli(["bd", "set-state", bead_id, "readiness=approved",
                                         "--reason", "dashboard: approved for dispatch"])
    if rc != 0:
        return JSONResponse({"error": stderr.strip(), "ok": False}, status_code=400)
    return JSONResponse({"ok": True, "bead_id": bead_id})

async def api_pinned_beads(request):
    """Return beads with the 'pinned' label."""
    beads = await asyncio.to_thread(dao_beads.get_beads_by_label, "pinned")
    return JSONResponse(beads)

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
        if label.endswith("_reason"):
            continue  # Skip reason keys — handled by _get_pause_reasons
        if label not in result:
            result[label] = bool(paused)
    return result


def _get_pause_reasons() -> dict:
    """Return pause reasons for labels that have them.

    Reads {label}_reason keys from dispatch.state and returns {label: reason_string}.
    Only includes labels that are currently paused AND have a reason stored.
    """
    raw = _read_dispatch_state()
    reasons = {}
    for key, value in raw.items():
        if key.endswith("_reason") and isinstance(value, str) and value:
            label = key[:-len("_reason")]
            if raw.get(label):  # Only include if label is actually paused
                reasons[label] = value
    return reasons


async def api_dispatch_pause_get(request):
    """GET /api/dispatch/pause — return current pause state for all label queues."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"paused": {}, "reasons": {}})
    return JSONResponse({"paused": _get_pause_state(), "reasons": _get_pause_reasons()})


async def api_dispatch_pause_post(request):
    """POST /api/dispatch/pause — set pause state for a label queue.

    Body: {"label": "dashboard", "paused": true}
    Returns updated full pause state with reasons.
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
        state.pop(f"{label}_reason", None)  # Clear reason on unpause
    _write_dispatch_state(state)
    new_pause = _get_pause_state()
    new_reasons = _get_pause_reasons()
    # Broadcast updated pause state + reasons via SSE
    await event_bus.broadcast("dispatch_pause", {"paused": new_pause, "reasons": new_reasons})
    return JSONResponse({"paused": new_pause, "reasons": new_reasons})


async def api_dispatch_resume(request):
    """POST /api/dispatch/resume — clear auth-failure pause so dispatcher resumes launching."""
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("dispatcher_state", {"paused": False, "reason": None})
        return JSONResponse({"ok": True, "was_paused": False, "cleared_reason": None})
    was_paused = is_paused()
    reason = get_pause_reason() if was_paused else None
    clear_paused()
    # Broadcast cleared state so all clients update immediately
    await event_bus.broadcast("dispatcher_state", {"paused": False, "reason": None})
    return JSONResponse({"ok": True, "was_paused": was_paused, "cleared_reason": reason})


async def api_dispatch_resume_bead(request):
    """POST /api/dispatch/resume/{bead_id} — revive a dead dispatch interactively.

    Finds the most recent dead dispatch row for ``bead_id`` and registers a
    NEW interactive container session with the monitor that points at the
    original JSONL. The dead row stays dead (history intact); the new row
    shows up on the Active list because its type is ``container``.
    """
    bead_id = request.path_params["bead_id"]
    from tools.dashboard.dao.dashboard_db import get_conn as _get_conn
    import uuid as _uuid
    import time as _time

    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM tmux_sessions"
        " WHERE bead_id=? AND type='dispatch' AND is_live=0"
        " ORDER BY created_at DESC LIMIT 1",
        (bead_id,),
    ).fetchone()
    if row is None:
        return JSONResponse(
            {"error": "No dead dispatch session found for bead",
             "bead_id": bead_id},
            status_code=404,
        )

    orig_tmux = row["tmux_name"]
    new_tmux = f"{bead_id}-resume-{int(_time.time())}"
    new_uuid = str(_uuid.uuid4())
    project = row["project"]
    jsonl_path = row["jsonl_path"]
    resolution_dir = row["resolution_dir"] or (
        str(Path(jsonl_path).parent) if jsonl_path else None
    )

    await session_monitor.register_session(
        tmux_name=new_tmux,
        type="container",
        jsonl_path=Path(jsonl_path) if jsonl_path else None,
        run_dir=Path(resolution_dir).parent if resolution_dir else None,
        bead_id=bead_id,
        project=project,
    )
    # register() inserts the session with the UUID derived from jsonl_path.
    # For resume, we want a NEW session_uuid so the Active card does not
    # collide with the dead row's identity.
    conn.execute(
        "UPDATE tmux_sessions SET session_uuid=? WHERE tmux_name=?",
        (new_uuid, new_tmux),
    )
    conn.commit()

    return JSONResponse({
        "ok": True,
        "resumed_from": orig_tmux,
        "tmux_session": new_tmux,
        "session_uuid": new_uuid,
        "bead_id": bead_id,
    }, status_code=201)


async def api_dispatch_pause_state(request):
    """GET /api/dispatch/pause-state — return dispatcher pause state from SQLite."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"paused": False, "reason": None})
    paused = is_paused()
    reason = get_pause_reason() if paused else None
    return JSONResponse({"paused": paused, "reason": reason})


def _get_dispatcher_state() -> dict:
    """Read dispatcher pause state and merge health from SQLite/git for SSE broadcast."""
    paused = is_paused()
    reason = get_pause_reason() if paused else None

    # Check for UU (unmerged) files that block all merges
    merge_health: dict = {"status": "ok"}
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=5,
            cwd=str(_REPO_ROOT),
        ).stdout
        uu_files = [line for line in porcelain.splitlines() if line[:2] == "UU"]
        if uu_files:
            merge_health = {
                "status": "blocked",
                "reason": f"UU: {uu_files[0][3:].strip()}",
                "count": len(uu_files),
            }
    except Exception:
        pass  # Non-critical — don't break SSE on git failure

    return {"paused": paused, "reason": reason, "merge_health": merge_health}


async def api_dispatch_status(request):
    """Show dispatched beads with their dispatch state.

    Reads the dispatch dimension (queued/launching/running/collecting/merging/done/failed)
    from bd labels + docker ps for containers. Adds currently-running runs from SQLite.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        running = dao_dispatch.get_running_with_stats()
        return JSONResponse({
            "claimed": [],
            "dispatching": [],
            "containers": [],
            "running_runs": running,
        })
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
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_dispatch_beads())
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


AGENT_RUNS_DIR = Path(os.environ.get(
    "DASHBOARD_AGENT_RUNS_DIR",
    str(Path(__file__).parent.parent.parent / "data" / "agent-runs"),
))


def _enrich_dispatch_runs(runs: list[dict]) -> None:
    """Add smoke_result and librarian_review to dispatch run dicts in-place."""
    # Smoke results — read from each run's output directory
    for run in runs:
        run["smoke_result"] = _read_smoke_result(run.get("_output_dir") or None)

    # Librarian entries — read review from their own output_dir
    for run in runs:
        if run.get("_librarian_type"):
            run["librarian_review"] = _read_librarian_results(
                run.get("_output_dir") or None
            )

    # Regular dispatch entries — bulk-query librarian_jobs for review results
    regular_runs = [
        r for r in runs
        if not r.get("_librarian_type") and r.get("_run_id")
    ]
    if not regular_runs:
        return

    run_ids = [r["_run_id"] for r in regular_runs]
    placeholders = ",".join("?" * len(run_ids))
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
        conn = _timeline_conn()
        rows = conn.execute(sql, run_ids).fetchall()
        conn.close()
    except Exception:
        return

    reviews: dict[str, dict] = {}
    for row in rows:
        run_id = row["dispatch_run_id"]
        if run_id in reviews:
            continue
        if row["job_status"] == "running":
            reviews[run_id] = {"status": "running"}
        elif row["job_status"] == "done":
            results = _read_librarian_results(row["lib_output_dir"])
            reviews[run_id] = results if results is not None else {"status": "done"}

    for run in regular_runs:
        run["librarian_review"] = reviews.get(run["_run_id"])

    # ── Experience report summary ────────────────────────────
    for run in runs:
        output_dir = run.get("_output_dir")
        if output_dir:
            exp_path = Path(output_dir) / "experience_report.md"
            try:
                if exp_path.exists():
                    lines = exp_path.read_text().strip().split("\n")[:5]
                    run["experience_summary"] = "\n".join(lines)
            except OSError:
                pass

    # ── Validation + pitfall notes (batched graph query) ─────
    try:

        # Collect bead IDs from regular runs
        bead_ids = {r["bead_id"] for r in regular_runs if r.get("bead_id")}

        # Batch query: validation notes
        if bead_ids:
            val_notes = graph_ops.list_sources(source_type="note", tags=["validation"], limit=200)
            val_by_bead: dict[str, dict] = {}
            for n in val_notes:
                title = n.get("title") or ""
                for bid in bead_ids:
                    if bid in title and bid not in val_by_bead:
                        val_by_bead[bid] = {"source_id": n["id"][:12], "title": title[:80]}
            for run in regular_runs:
                bid = run.get("bead_id")
                if bid and bid in val_by_bead:
                    run["validation"] = val_by_bead[bid]

        # Batch query: pitfall notes — single query covering all run time windows
        earliest_start = min(
            (r["_started_at"] for r in regular_runs if r.get("_started_at")),
            default=None,
        )
        if earliest_start:
            pitfall_notes = graph_ops.list_sources(
                source_type="note", tags=["pitfall"], since=earliest_start, limit=500,
            )
            for run in regular_runs:
                started = run.get("_started_at")
                completed = run.get("_completed_at")
                if not started or not completed:
                    continue
                matched = [
                    p for p in pitfall_notes
                    if started <= (p.get("created_at") or "") <= completed
                ]
                if matched:
                    run["pitfalls"] = [
                        {"id": p["id"][:12], "title": (p.get("title") or "")[:60]}
                        for p in matched
                    ]
    except Exception:
        pass


def _enrich_librarian_fields(runs: list[dict]) -> None:
    """Populate librarian-specific fields: synthetic title, fallback bead_id."""
    for run in runs:
        librarian_type = run.get("librarian_type")
        if not librarian_type:
            continue
        if not run.get("bead_id"):
            run["bead_id"] = run.get("dir") or run.get("id") or ""
        if not run.get("title"):
            run["title"] = f"Librarian: {librarian_type}"


async def api_dispatch_runs(request):
    """List dispatch runs from SQLite (includes RUNNING rows)."""
    if os.environ.get("DASHBOARD_MOCK"):
        all_runs = dao_dispatch.get_recent_runs(limit=50)
        running = dao_dispatch.get_running_with_stats()
        combined = [*running, *all_runs]
        _enrich_librarian_fields(combined)
        return JSONResponse(combined)
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

        librarian_type = row.get("librarian_type") or None
        dir_name = row.get("id", "")
        bead_id = row.get("bead_id", "")
        # Librarian runs have empty bead_id — use dir name as identifier
        if not bead_id and librarian_type:
            bead_id = dir_name
        # Synthetic title for librarian runs
        title = None
        if librarian_type:
            title = f"Librarian: {librarian_type}"

        runs.append({
            "bead_id": bead_id,
            "timestamp": timestamp,
            "dir": dir_name,
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
            "smoke_result": None,
            "librarian_review": None,
            "librarian_type": librarian_type,
            "title": title,
            # internal fields for enrichment — stripped before response
            "_run_id": row.get("id", ""),
            "_output_dir": row.get("output_dir") or "",
            "_librarian_type": librarian_type,
            "_started_at": row.get("started_at") or "",
            "_completed_at": row.get("completed_at") or "",
        })

    await asyncio.to_thread(_enrich_dispatch_runs, runs)

    for run in runs:
        run.pop("_run_id", None)
        run.pop("_output_dir", None)
        run.pop("_librarian_type", None)
        run.pop("_started_at", None)
        run.pop("_completed_at", None)

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
        "smoke_result": _read_smoke_result(row["output_dir"]),
        "_output_dir": row["output_dir"] or "",  # internal field, stripped before response
    }


def _parse_review_summary(text: str) -> dict | None:
    """Parse experience reviewer markdown summary into structured data.

    Expected format (from experience_reviewer/prompt.md):
        ### Extracted
        - [pitfall] description → graph note created (note-id)
        - [bug] description → bead created (bead-id)
        ### Skipped
        - description — reason: why skipped

    Returns dict with extracted/skipped lists, or None if no parseable content.
    """
    ext_match = re.search(
        r"###\s*Extracted\s*\n(.*?)(?=\n###|\Z)", text, re.DOTALL
    )
    skip_match = re.search(
        r"###\s*Skipped\s*\n(.*?)(?=\n###|\Z)", text, re.DOTALL
    )
    if not ext_match and not skip_match:
        return None

    extracted: list[dict] = []
    if ext_match:
        for line in ext_match.group(1).strip().splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            line = line[2:]
            m = re.match(r"\[(\w+)\]\s+(.*?)(?:\s*→\s*(.*))?$", line)
            if not m:
                continue
            item: dict = {"type": m.group(1), "description": m.group(2).strip()}
            prov = m.group(3) or ""
            bead_m = re.search(r"bead created \(([^)]+)\)", prov)
            if bead_m:
                item["bead_id"] = bead_m.group(1)
            note_m = re.search(r"note created \(([^)]+)\)", prov)
            if note_m:
                item["source_id"] = note_m.group(1)
            extracted.append(item)

    skipped: list[dict] = []
    if skip_match:
        for line in skip_match.group(1).strip().splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            line = line[2:]
            parts = line.split(" — reason: ", 1)
            item = {"description": parts[0].strip()}
            if len(parts) > 1:
                item["reason"] = parts[1].strip()
            skipped.append(item)

    return {"status": "done", "extracted": extracted, "skipped": skipped}


def _read_review_from_session(output_dir: str) -> dict | None:
    """Extract review summary from librarian session JSONL.

    Scans assistant messages for the experience reviewer's markdown summary
    (### Extracted / ### Skipped sections) and parses into structured data.
    """
    sessions_dir = Path(output_dir) / "sessions"
    if not sessions_dir.is_dir():
        return None

    jsonl_files = sorted(
        sessions_dir.glob("**/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jsonl_files:
        return None

    last_summary: str | None = None
    try:
        with open(jsonl_files[0]) as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if not isinstance(content, str):
                    continue
                if "### Extracted" in content or "### Skipped" in content:
                    last_summary = content
    except OSError:
        return None

    if not last_summary:
        return None
    return _parse_review_summary(last_summary)


def _read_librarian_results(output_dir: str | None) -> dict | None:
    """Read results from a librarian's output directory.

    Tries in order:
    1. results.json — structured output (written by future experience_reviewer enhancement)
    2. Session JSONL — parse review summary from assistant messages
    3. decision.json — fallback for basic status
    """
    if not output_dir:
        return None
    base = Path(output_dir)

    # 1. Structured results.json (preferred)
    try:
        with open(base / "results.json") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    # 2. Parse review summary from session JSONL
    session_results = _read_review_from_session(output_dir)
    if session_results is not None:
        return session_results

    # 3. Fallback to decision.json
    try:
        with open(base / "decision.json") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    return None


def _read_smoke_result(output_dir: str | None) -> dict | None:
    """Read smoke_result.json from a run's output directory. Returns None if absent."""
    if not output_dir:
        return None
    smoke_path = Path(output_dir) / "smoke_result.json"
    try:
        return json.loads(smoke_path.read_text()) if smoke_path.exists() else None
    except (OSError, json.JSONDecodeError):
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

    # Populate parent_run_id on librarian entries (for timeline hiding logic)
    lib_run_ids = [
        e["run_id"] for e in entries
        if e.get("librarian_type") and e.get("run_id")
    ]
    if lib_run_ids:
        lib_ph = ",".join("?" * len(lib_run_ids))
        parent_sql = f"""
            SELECT dr.id AS lib_run_id,
                   json_extract(lj.payload, '$.run_id') AS parent_run_id
            FROM dispatch_runs dr
            JOIN librarian_jobs lj ON (
                dr.librarian_type = lj.job_type
                AND dr.id LIKE 'librarian-' || lj.job_type || '-' || substr(lj.id, 1, 8) || '-%'
            )
            WHERE dr.id IN ({lib_ph})
        """
        try:
            parent_rows = conn.execute(parent_sql, lib_run_ids).fetchall()
            parent_map = {r["lib_run_id"]: r["parent_run_id"] for r in parent_rows}
            for entry in entries:
                if entry.get("librarian_type"):
                    entry["parent_run_id"] = parent_map.get(entry["run_id"])
        except Exception:
            pass

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
    if os.environ.get("DASHBOARD_MOCK"):
        range_str = request.query_params.get("range")
        limit = min(int(request.query_params.get("limit", "200")), 1000)
        entries = dao_dispatch.get_timeline_entries(range_str, limit)
        # Enrich with bead title/priority from mock beads
        bead_ids = [e["bead_id"] for e in entries if e.get("bead_id")]
        if bead_ids:
            meta = dao_beads.get_bead_title_priority(bead_ids)
            for entry in entries:
                bid = entry.get("bead_id")
                if bid and bid in meta:
                    entry["title"] = meta[bid].get("title") or entry["bead_id"]
                    entry["priority"] = meta[bid].get("priority")
        return JSONResponse(entries)

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
    if os.environ.get("DASHBOARD_MOCK"):
        range_str = request.query_params.get("range")
        return JSONResponse(dao_dispatch.get_timeline_stats(range_str))

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

    if os.environ.get("DASHBOARD_MOCK"):
        trace = dao_dispatch.get_trace(run_name)
        if not trace:
            return JSONResponse({"error": "run not found"}, status_code=404)
        return JSONResponse({
            "run": trace.get("id", run_name),
            "bead_id": trace.get("bead_id", ""),
            "bead": dao_beads.get_bead(trace.get("bead_id", "")),
            "decision": trace.get("decision"),
            "experience_report": trace.get("experience_report", ""),
            "commit_hash": trace.get("commit_hash", ""),
            "branch": trace.get("branch", ""),
            "diff": trace.get("diff", ""),
            "has_session": False,
            "is_live": False,
            "duration_secs": trace.get("duration_secs"),
            "commit_message": trace.get("commit_message", ""),
            "lines_added": trace.get("lines_added"),
            "lines_removed": trace.get("lines_removed"),
            "files_changed": trace.get("files_changed"),
        })

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
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "20"))
        project = request.query_params.get("project")
        results = dao_beads.search(q, limit=limit, project=project)
        _enrich_search_results(results)
        return JSONResponse(results)
    cmd = ["graph", "search", q, "--json", "--limit", request.query_params.get("limit", "20")]
    project = request.query_params.get("project")
    if project:
        cmd += ["--project", project]
    if request.query_params.get("or"):
        cmd += ["--or"]
    results = await run_cli_json(cmd)
    if not isinstance(results, list):
        return JSONResponse(results)
    if request.query_params.get("group"):
        grouped = {}
        for r in results:
            sid = r.get("source_id", r.get("id"))
            if sid not in grouped:
                grouped[sid] = r
                grouped[sid]["match_count"] = 1
            else:
                grouped[sid]["match_count"] = grouped[sid].get("match_count", 0) + 1
        results = sorted(grouped.values(), key=lambda x: x.get("rank", 0))
    _enrich_search_results(results)
    return JSONResponse(results)


def _enrich_search_results(results: list) -> None:
    """Attach resolved ``org`` and 24hr ``date`` to each search result in-place."""
    from tools.dashboard.org_identity import resolve_org_identity, session_org_slug
    for r in results:
        if not isinstance(r, dict):
            continue
        if "org" not in r:
            r["org"] = resolve_org_identity(session_org_slug(r))
        # Normalize date to "YYYY-MM-DD HH:MM" (24hr). Source may supply
        # created_at / date / last_activity_at in various forms.
        if "date" not in r or not r.get("date"):
            r["date"] = _format_search_date(
                r.get("created_at") or r.get("date") or r.get("last_activity_at") or ""
            )
        else:
            r["date"] = _format_search_date(r["date"])


def _format_search_date(raw: str) -> str:
    """Normalize a timestamp string to ``YYYY-MM-DD HH:MM`` (24hr)."""
    if not raw:
        return ""
    # Strip trailing Z / fractional seconds, support ISO or space-delimited.
    s = raw.replace("T", " ").replace("Z", "")
    # Drop microseconds / timezone offset if present.
    if "+" in s:
        s = s.split("+", 1)[0]
    if "." in s:
        s = s.split(".", 1)[0]
    s = s.strip()
    # Accept "YYYY-MM-DD" alone; otherwise keep to minute resolution.
    if len(s) >= 16:
        return s[:16]
    return s[:10]

async def api_sources(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"results": "", "error": None})
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
    if os.environ.get("DASHBOARD_MOCK"):
        source = dao_beads.get_source(source_id)
        if not source:
            return JSONResponse({"error": "source not found"}, status_code=404)
        _attach_source_org(source)
        return JSONResponse(source)
    max_chars = request.query_params.get("max_chars", "50000")
    result = await run_cli_json(
        ["graph", "read", source_id, "--json", "--max-chars", max_chars, "--first"]
    )
    _attach_source_org(result)
    return JSONResponse(result)


def _attach_source_org(result: dict | None) -> None:
    """Attach resolved ``org`` to the nested ``source`` of a graph-read response.

    ``graph read --json --first`` returns ``{source: {...}, entries: [...], ...}``.
    The mock DAO returns the source dict directly. Handle both shapes.
    """
    if not isinstance(result, dict):
        return
    from tools.dashboard.org_identity import resolve_org_identity, session_org_slug
    src = result.get("source") if isinstance(result.get("source"), dict) else result
    if not isinstance(src, dict):
        return
    if "org" not in src:
        src["org"] = resolve_org_identity(session_org_slug(src))

async def api_context(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"content": "", "error": None})
    source_id = request.path_params["id"]
    turn = request.path_params["turn"]
    window = request.query_params.get("window", "3")
    stdout, stderr, rc = await run_cli(["graph", "context", source_id, turn, "--window", window])
    return JSONResponse({"content": stdout, "error": stderr if rc != 0 else None})

async def api_projects(request):
    """Return the workspace registry — one entry per containerized project.

    Each entry carries the fields the frontend needs for grouping, display,
    and routing: ``id``, ``name``, ``description``, ``graph_project`` (the
    org the workspace belongs to), and ``dind`` (whether Docker-in-Docker
    is enabled — drives the UI badge).
    """
    try:
        projects = workspace_settings.load_workspaces()
    except workspace_settings.WorkspaceSettingsError as e:
        return JSONResponse({"projects": [], "error": str(e)}, status_code=500)
    from tools.dashboard.org_identity import resolve_org_identity
    entries = []
    org_cache: dict[str, dict] = {}
    for p in projects.values():
        slug = p.graph_project
        if slug not in org_cache:
            org_cache[slug] = resolve_org_identity(slug)
        entries.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "graph_project": slug,
            "org": org_cache[slug],
            "dind": p.dind,
        })
    return JSONResponse({"projects": entries})

async def api_stats(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"results": "", "error": None})
    stdout, stderr, rc = await run_cli(["graph", "stats"])
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_attention(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"results": "", "error": None})
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
    """List active terminal sessions (tmux-backed, DB-sourced)."""
    live_tmux = set(_list_dashboard_tmux())
    db_sessions = dashboard_db.get_live_sessions()
    result = []
    for row in db_sessions:
        name = row["tmux_name"]
        alive = name in live_tmux
        if not alive:
            # Mark dead in DB if tmux is gone
            dashboard_db.mark_dead(name)
            continue
        result.append({
            "id": name,
            "alive": True,
            "cmd": "",
            "env": "container" if row["type"] == "container" else "host",
            "started": row["created_at"],
        })
    # Also include live tmux sessions not in DB yet (e.g. pre-existing)
    db_names = {r["tmux_name"] for r in db_sessions}
    for name in live_tmux:
        if name not in db_names:
            info = _detect_terminal_type(name)
            info["started"] = asyncio.get_event_loop().time()
            result.append({"id": name, "alive": True, **info})
    return JSONResponse(result)

async def api_terminal_kill(request):
    """Kill a terminal session. If it's a Chat With session, ingest it into the graph."""
    name = request.path_params["id"]
    if _tmux_session_exists(name):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        # Deregister from session monitor (marks dead in DB)
        await session_monitor.deregister(name)
        # Revoke CrossTalk tokens for this session
        await asyncio.to_thread(auth_db.revoke_token, name)
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
    if dashboard_db.session_exists(name):
        # Display name is UI-only — stored in DB as last_message prefix if needed
        return JSONResponse({"ok": True, "id": name, "name": new_name})
    return JSONResponse({"error": "not found"}, status_code=404)


# ── CrossTalk API ────────────────────────────────────────────────────────────

def _crosstalk_auth(request) -> tuple[str | None, JSONResponse | None]:
    """Extract and verify a CrossTalk bearer token.

    Returns (sender_tmux_name, None) on success, or (None, error_response) on failure.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None, JSONResponse({"error": "missing or invalid Authorization header"}, status_code=401)
    raw_token = auth[7:]
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    sender = auth_db.resolve_token(token_hash)
    if sender is None:
        return None, JSONResponse({"error": "invalid or revoked token"}, status_code=401)
    return sender, None


async def api_monitor_register(request):
    """POST /api/monitor/register — register a session with the in-process monitor.

    Body: {tmux_name, type, jsonl_path, bead_id, project, run_dir?}

    Calls session_monitor.register_session() in-process so that the DB row
    AND the inotify watch AND the SSE registry broadcast all happen. This is
    the IPC bridge between the dispatcher (separate process) and the
    dashboard — a direct dashboard_db.upsert_session() from the dispatcher
    would bypass watch setup and the SSE broadcast, leaving the overlay
    frozen (the auto-ylj6r gap, pitfall graph://f4b1bb26-a1).

    Idempotent on re-register: existing rows are left in place and the
    in-process watch is refreshed.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tmux_name = body.get("tmux_name")
    session_type = body.get("type")
    jsonl_path = body.get("jsonl_path")
    if not tmux_name or not session_type:
        return JSONResponse(
            {"error": "tmux_name and type are required"}, status_code=400,
        )

    bead_id = body.get("bead_id")
    project = body.get("project")
    run_dir = body.get("run_dir")

    existing = dashboard_db.get_session(tmux_name)
    if existing is not None:
        # Idempotent re-register: refresh the in-process watch without a
        # duplicate INSERT. register_session() would swallow the
        # IntegrityError silently and leave the watch un-refreshed.
        if jsonl_path and session_monitor._use_inotify:
            try:
                session_monitor._add_file_watch(tmux_name, jsonl_path)
            except Exception:
                logger.exception(
                    "api_monitor_register: watch refresh failed for %s",
                    tmux_name,
                )
        return JSONResponse({"ok": True, "tmux_name": tmux_name})

    try:
        await session_monitor.register_session(
            tmux_name=tmux_name,
            type=session_type,
            jsonl_path=jsonl_path,
            run_dir=run_dir,
            bead_id=bead_id,
            project=project,
        )
    except Exception as exc:
        logger.exception("api_monitor_register failed for %s", tmux_name)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "tmux_name": tmux_name})


async def api_monitor_deregister(request):
    """POST /api/monitor/deregister — mark a session dead in the monitor.

    Body: {tmux_name}

    Calls session_monitor.deregister_session() in-process so the inotify
    watch is removed, the DB row is flipped to is_live=0, and the registry
    SSE broadcast fires. Idempotent — missing sessions return 200.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tmux_name = body.get("tmux_name")
    if not tmux_name:
        return JSONResponse({"error": "tmux_name is required"}, status_code=400)

    try:
        await session_monitor.deregister_session(tmux_name)
    except Exception as exc:
        logger.exception("api_monitor_deregister failed for %s", tmux_name)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True})


async def api_crosstalk_send(request):
    """POST /api/crosstalk/send — deliver a plain-text message to a peer session."""
    sender, err = _crosstalk_auth(request)
    if err:
        return err

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    target = body.get("target", "")
    message = body.get("message", "")

    # Validate message: plain text only, no angle brackets, max 4000 chars
    if not message or len(message) > 4000:
        return JSONResponse({"error": "message must be 1-4000 characters"}, status_code=400)
    if "<" in message or ">" in message:
        return JSONResponse({"error": "message must not contain < or > characters"}, status_code=400)

    # Validate target exists
    if not target or not _tmux_session_exists(target):
        return JSONResponse({"error": f"target session not found: {target}"}, status_code=404)

    # Resolve sender metadata from dashboard_db
    sender_row = dashboard_db.get_session(sender)
    sender_label = (sender_row or {}).get("label", "") or sender
    sender_source_id = (sender_row or {}).get("graph_source_id", "") or ""
    sender_entry_count = (sender_row or {}).get("entry_count", 0) or 0

    # Build envelope
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = (
        f'<crosstalk from="{sender}"\n'
        f'           label="{sender_label}"\n'
        f'           source="{sender_source_id}" turn="{sender_entry_count}"\n'
        f'           timestamp="{iso_now}">\n'
        f'{message}\n'
        f'</crosstalk>'
    )

    # Inject via unified tmux_send (per-session lock + double-Enter retry)
    await tmux_send(target, envelope)

    # Store in crosstalk_messages
    await asyncio.to_thread(
        auth_db.insert_message,
        sender, sender_label, target,
        sender_source_id or None, sender_entry_count or None,
        message, time.time(),
    )

    return JSONResponse({
        "delivered": True,
        "from": sender,
        "label": sender_label,
        "source_id": sender_source_id or None,
        "turn": sender_entry_count or None,
        "target": target,
    })


_MAX_BROADCAST_IDLE_SECS = 21600  # 6 hours


async def api_crosstalk_broadcast(request):
    """POST /api/crosstalk/broadcast — send message to all recently-active peers.

    Query params:
        max_idle: int — max idle seconds (default 3600, capped at 21600)
    Body JSON: {"message": "..."}
    """
    sender, err = _crosstalk_auth(request)
    if err:
        return err

    # Parse max_idle from query params
    try:
        max_idle = int(request.query_params.get("max_idle", "3600"))
    except (ValueError, TypeError):
        return JSONResponse({"error": "max_idle must be an integer (seconds)"}, status_code=400)
    if max_idle < 0:
        return JSONResponse({"error": "max_idle must be non-negative"}, status_code=400)
    if max_idle > _MAX_BROADCAST_IDLE_SECS:
        return JSONResponse(
            {"error": f"max_idle exceeds maximum of {_MAX_BROADCAST_IDLE_SECS}s (6h)"},
            status_code=400,
        )

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    message = body.get("message", "")
    if not message or len(message) > 4000:
        return JSONResponse({"error": "message must be 1-4000 characters"}, status_code=400)
    if "<" in message or ">" in message:
        return JSONResponse({"error": "message must not contain < or > characters"}, status_code=400)

    # Get live sessions, filter by idle time
    conn = dashboard_db.get_conn()
    rows = conn.execute(
        "SELECT tmux_name, last_activity, created_at FROM tmux_sessions WHERE is_live=1 AND tmux_name != ?",
        (sender,),
    ).fetchall()

    now = time.time()
    targets = []
    skipped = 0
    for r in rows:
        la = r["last_activity"]
        if la is None:
            # Fall back to created_at (ISO string)
            ca = r["created_at"]
            if isinstance(ca, str):
                try:
                    la = datetime.fromisoformat(ca).replace(tzinfo=timezone.utc).timestamp()
                except (ValueError, TypeError):
                    la = None
        if la is None or (now - la) >= max_idle:
            skipped += 1
            continue
        targets.append(r["tmux_name"])

    # Resolve sender metadata
    sender_row = dashboard_db.get_session(sender)
    sender_label = (sender_row or {}).get("label", "") or sender
    sender_source_id = (sender_row or {}).get("graph_source_id", "") or ""
    sender_entry_count = (sender_row or {}).get("entry_count", 0) or 0

    # Build envelope
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = (
        f'<crosstalk from="{sender}"\n'
        f'           label="{sender_label}"\n'
        f'           source="{sender_source_id}" turn="{sender_entry_count}"\n'
        f'           timestamp="{iso_now}">\n'
        f'{message}\n'
        f'</crosstalk>'
    )

    sent = 0
    failed = 0
    for target in targets:
        try:
            await tmux_send(target, envelope)
            await asyncio.to_thread(
                auth_db.insert_message,
                sender, sender_label, target,
                sender_source_id or None, sender_entry_count or None,
                message, time.time(),
            )
            sent += 1
        except Exception:
            failed += 1

    return JSONResponse({
        "sent": sent,
        "skipped_idle": skipped,
        "failed": failed,
        "total_live": len(rows),
        "max_idle": max_idle,
    })


async def api_crosstalk_peers(request):
    """GET /api/crosstalk/peers — list live sessions excluding the caller."""
    sender, err = _crosstalk_auth(request)
    if err:
        return err

    conn = dashboard_db.get_conn()
    rows = conn.execute(
        "SELECT tmux_name, type, label, created_at FROM tmux_sessions WHERE is_live=1 AND tmux_name != ?",
        (sender,),
    ).fetchall()
    return JSONResponse({"peers": [dict(r) for r in rows]})


async def api_primer(request):
    bead_id = request.path_params["id"]
    if os.environ.get("DASHBOARD_MOCK"):
        primer = dao_beads.get_primer(bead_id)
        if not primer:
            return JSONResponse({"error": "bead not found"}, status_code=404)
        return JSONResponse(primer)
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

    Returns {primer_text: str}.
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
        # Resource not found (design missing, etc.)
        return JSONResponse({"error": msg}, status_code=404)


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
    sessions = [s for s in result.stdout.strip().split("\n")
                 if s.startswith("chatwith-") or s.startswith("chat-")]
    return JSONResponse({"sessions": sessions})



# ── Live Session Tailing ──────────────────────────────────────


_CROSSTALK_RE = re.compile(
    r'<crosstalk\s+from="([^"]+)"\s+label="([^"]*)"\s+source="([^"]*)"\s+turn="([^"]*)"\s+timestamp="([^"]+)">\n(.*)\n</crosstalk>',
    re.DOTALL,
)


def _classify_crosstalk(text: str) -> dict | None:
    """Detect CrossTalk peer messages in user entries.

    Returns a dict with sender info and message body, or None if the text
    is not a valid crosstalk envelope.  Body must be plain text (no angle
    brackets) to avoid injection.
    """
    stripped = text.strip()
    m = _CROSSTALK_RE.fullmatch(stripped)
    if not m:
        return None
    body = m.group(6)
    if '<' in body or '>' in body:
        return None  # not valid crosstalk — body must be plain text
    return {
        "from": m.group(1),
        "label": m.group(2),
        "source": m.group(3),
        "turn": m.group(4),
        "timestamp": m.group(5),
        "message": body,
    }


def _parse_crosstalk_send(command: str, timestamp: str) -> dict | None:
    """Detect outbound CrossTalk send in a Bash command."""
    if "crosstalk/send" not in command:
        return None
    import shlex
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None  # malformed shell quoting
    # Find the token after -d
    payload = None
    for i, tok in enumerate(tokens):
        if tok == "-d" and i + 1 < len(tokens):
            try:
                parsed = json.loads(tokens[i + 1])
                if isinstance(parsed, dict) and "target" in parsed and "message" in parsed:
                    payload = parsed
                    break
            except (json.JSONDecodeError, ValueError):
                continue
    if not payload:
        return None
    return {
        "type": "crosstalk",
        "role": "crosstalk",
        "content": payload.get("message", ""),
        "sender": "self",
        "sender_label": "",
        "source_id": "",
        "turn": "",
        "target": payload.get("target", ""),
        "direction": "sent",
        "timestamp": timestamp,
    }


def _parse_graph_comment_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r'graph comment\s+(\S+)', command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "comment-added",
        "role": "assistant",
        "source_id": m.group(1),
        "content": "Added comment",
        "timestamp": timestamp,
    }


def _parse_dispatch_approve_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r'graph dispatch approve\s+(\S+)', command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "dispatch-approved",
        "role": "assistant",
        "bead_id": m.group(1),
        "content": f"Approved {m.group(1)} for dispatch",
        "timestamp": timestamp,
    }


def _parse_bd_setstate_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r'bd set-state\s+(\S+)\s+(\S+=\S+)', command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "state-changed",
        "role": "assistant",
        "bead_id": m.group(1),
        "state": m.group(2),
        "content": f"Set {m.group(2)} on {m.group(1)}",
        "timestamp": timestamp,
    }


def _upconvert_graph_result(content: str, timestamp: str, tool_id: str = "") -> dict | None:
    """Upconvert graph CLI tool_result output to semantic tiles.

    Detects note creation, thought capture, and comment addition confirmations
    in tool_result text and returns a semantic_bash entry with extracted IDs.
    Preserves tool_id so activity_state tracking can match tool_use → result.
    """
    if not isinstance(content, str):
        return None
    # Note saved (src:abc123-456)
    base = {"type": "semantic_bash", "role": "tool", "timestamp": timestamp}
    if tool_id:
        base["tool_id"] = tool_id
    # Match only actual graph CLI output.  Real output looks like:
    #   "  ✓ Note saved (src:abc123-def) — 25 lines, 1234 chars"
    # The ✓ must appear at the start of a line (after optional whitespace)
    # to avoid false positives when Bash prints repr/test output that
    # happens to contain ✓ embedded in a larger string.
    m = re.search(r"^\s*\u2713 Note saved \(src:([a-f0-9-]+)\)", content, re.MULTILINE)
    if m:
        return {**base, "semantic_type": "note-created",
                "source_id": m.group(1), "content": content.strip()[:100]}
    m = re.search(r"^\s*\u2713 Captured:\s*([a-f0-9-]+)", content, re.MULTILINE)
    if m:
        return {**base, "semantic_type": "thought-captured",
                "source_id": m.group(1), "content": content.strip()[:100]}
    m = re.search(r"^\s*\u2713 Comment added.*?id:([a-f0-9-]+)", content, re.MULTILINE)
    if m:
        return {**base, "semantic_type": "comment-added",
                "comment_id": m.group(1), "content": content.strip()[:100]}
    return None


def _enrich_semantic_tile(entry: dict) -> None:
    """Enrich note-created/thought-captured/comment-added tiles with graph.db data.

    One SQLite read per semantic tile. Mutates entry in place, adding title,
    preview, and tags fields. Graceful fallback: if graph.db unavailable or
    source not found, the entry is left unchanged (raw CLI output).
    """
    source_id = entry.get("source_id") or entry.get("comment_id")
    if not source_id:
        return

    db_path = _graph_db_path()
    try:
        if db_path:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        else:
            # Default path
            default = Path(__file__).resolve().parents[2] / "data" / "graph.db"
            if not default.exists():
                return
            conn = sqlite3.connect(f"file:{default}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except (sqlite3.OperationalError, OSError):
        return

    try:
        # For comments, look up the parent note's metadata
        lookup_id = source_id
        row = conn.execute(
            "SELECT id, title, metadata FROM sources WHERE id = ?", (lookup_id,)
        ).fetchone()
        if not row:
            # Try prefix match
            row = conn.execute(
                "SELECT id, title, metadata FROM sources WHERE id LIKE ? LIMIT 1",
                (f"{lookup_id}%",),
            ).fetchone()
        if not row:
            return

        meta = {}
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            pass

        # For comment-added, use the parent note's data
        if entry.get("semantic_type") == "comment-added" and meta.get("parent_source_id"):
            parent_row = conn.execute(
                "SELECT id, title, metadata FROM sources WHERE id = ?",
                (meta["parent_source_id"],),
            ).fetchone()
            if not parent_row:
                parent_row = conn.execute(
                    "SELECT id, title, metadata FROM sources WHERE id LIKE ? LIMIT 1",
                    (f"{meta['parent_source_id']}%",),
                ).fetchone()
            if parent_row:
                row = parent_row
                try:
                    meta = json.loads(parent_row["metadata"]) if parent_row["metadata"] else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}

        # Extract title (strip leading # headings)
        title = (row["title"] or "").lstrip("#").strip()

        # Extract tags from metadata
        tags = meta.get("tags", [])

        # Extract preview from first content entry, skip heading lines
        content_row = conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ? ORDER BY turn_number LIMIT 1",
            (row["id"],),
        ).fetchone()
        preview = ""
        if content_row and content_row["content"]:
            lines = content_row["content"].split("\n")
            body_lines = [l for l in lines if not l.startswith("#") and l.strip()]
            preview = " ".join(body_lines)[:120]

        entry["title"] = title
        entry["preview"] = preview
        entry["tags"] = tags if isinstance(tags, list) else []
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        pass  # graceful fallback
    finally:
        conn.close()


def _classify_system_message(text: str) -> dict | None:
    """Detect harness-injected system messages in user entries.

    Returns a compact dict with type 'system' and a summary, or None if the
    text is a normal user message.
    """
    stripped = text.strip()

    # --- task-notification: extract status + summary -----------------------
    if "<task-notification>" in stripped:
        summary = ""
        status = ""
        m_summary = re.search(r"<summary>(.*?)</summary>", stripped, re.DOTALL)
        m_status = re.search(r"<status>(.*?)</status>", stripped, re.DOTALL)
        if m_summary:
            summary = m_summary.group(1).strip()
        if m_status:
            status = m_status.group(1).strip()
        label = summary if summary else f"Task {status}" if status else "Task notification"
        return {"summary": label, "tag": "task-notification"}

    # --- system-reminder: compact label, preserve body for expand ----------
    if "<system-reminder>" in stripped:
        m = re.search(r"<system-reminder>(.*?)</system-reminder>", stripped, re.DOTALL)
        body = m.group(1).strip() if m else stripped
        return {"summary": "System reminder", "tag": "system-reminder", "body": body}

    # --- local-command-stdout: summarise -----------------------------------
    if "<local-command-stdout>" in stripped:
        return {"summary": "Command output", "tag": "local-command-stdout"}

    # --- command-name: summarise -------------------------------------------
    if "<command-name>" in stripped:
        m = re.search(r"<command-name>(.*?)</command-name>", stripped, re.DOTALL)
        name = m.group(1).strip() if m else "command"
        return {"summary": f"Command: {name}", "tag": "command-name"}

    return None


def _parse_jsonl_entry(line: str) -> dict | None:
    """Parse a single JSONL line into a display entry."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None

    entry_type = raw.get("type")
    timestamp = raw.get("timestamp", "")
    is_sidechain = raw.get("isSidechain", False)

    # Compact-summary turns: Claude's continuation boilerplate written after a
    # context compaction. Emit a distinct entry kind (not a user bubble) so the
    # viewer can render it as a boundary marker.
    if raw.get("isCompactSummary") or raw.get("isVisibleInTranscriptOnly"):
        message = raw.get("message", {})
        content_raw = message.get("content", "")
        text = ""
        if isinstance(content_raw, str):
            text = content_raw
        elif isinstance(content_raw, list):
            text = "".join(
                b.get("text", "") for b in content_raw
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not text:
            return None
        return {
            "type": "compact_summary",
            "role": "compact_summary",
            "content": text,
            "timestamp": timestamp,
        }

    # Queued user messages: sent while agent was working, logged as queue-operation
    # instead of user. Render enqueue entries with content as user messages.
    # Track content so we can dedup if the same text also appears as a user entry.
    if entry_type == "queue-operation":
        op = raw.get("operation")
        content = raw.get("content", "")
        if op == "enqueue" and content and not content.startswith("<task-notification"):
            # Check for CrossTalk envelope before treating as user message
            ct = _classify_crosstalk(content)
            if ct:
                return {
                    "type": "crosstalk",
                    "role": "crosstalk",
                    "content": ct["message"],
                    "sender": ct["from"],
                    "sender_label": ct["label"],
                    "source_id": ct["source"],
                    "turn": ct["turn"],
                    "timestamp": timestamp,
                    "queued": True,
                }
            return {"type": "user", "content": content, "timestamp": timestamp, "queued": True}
        return None

    # Skip non-content entries
    if entry_type in ("progress", "system"):
        return None
    if is_sidechain:
        return None

    message = raw.get("message", {})
    role = message.get("role", entry_type)
    content_raw = message.get("content", "")

    if entry_type == "user":
        text = ""
        tool_results = []
        if isinstance(content_raw, str):
            text = content_raw
        elif isinstance(content_raw, list):
            for block in content_raw:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text += block.get("text", "")
                elif btype == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = "".join(
                            b.get("text", "") for b in result_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    tool_use_id = block.get("tool_use_id", "")
                    # Always emit the tool_result for tracking (_resultMap,
                    # pending_tool_ids).  Optionally ALSO emit a semantic
                    # tile for display — augment, don't replace.
                    tool_results.append({
                        "type": "tool_result",
                        "role": "tool",
                        "tool_id": tool_use_id,
                        "content": result_content,
                        "is_error": block.get("is_error", False),
                        "timestamp": timestamp,
                    })
                    sem = _upconvert_graph_result(result_content, timestamp,
                                                  tool_id=tool_use_id)
                    if sem:
                        _enrich_semantic_tile(sem)
                        tool_results.append(sem)

        entries = []

        if text:
            # Detect CrossTalk peer messages before system message check
            ct = _classify_crosstalk(text)
            if ct:
                entries.append({
                    "type": "crosstalk",
                    "role": "crosstalk",
                    "content": ct["message"],
                    "sender": ct["from"],
                    "sender_label": ct["label"],
                    "source_id": ct["source"],
                    "turn": ct["turn"],
                    "timestamp": timestamp,
                })
            # Detect harness-injected system messages masquerading as user entries
            elif (sys_info := _classify_system_message(text)):
                sys_entry = {
                    "type": "system",
                    "role": "system",
                    "content": sys_info["summary"],
                    "tag": sys_info["tag"],
                    "timestamp": timestamp,
                }
                if sys_info.get("body"):
                    sys_entry["body"] = sys_info["body"]
                entries.append(sys_entry)
            else:
                entries.append({
                    "type": "user",
                    "role": "user",
                    "content": text,
                    "timestamp": timestamp,
                })

        entries.extend(tool_results)

        if not entries:
            return None
        return entries if len(entries) > 1 else entries[0]

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
                # Semantic Bash: detect outbound CrossTalk sends
                if tool_name == "Bash":
                    cmd = (tool_input.get("command") or "")
                    if "crosstalk/send" in cmd:
                        ct_entry = _parse_crosstalk_send(cmd, timestamp)
                        if ct_entry:
                            blocks.append(ct_entry)
                            continue
                    # Semantic Bash: graph comment
                    if "graph comment" in cmd and "integrate" not in cmd:
                        parsed = _parse_graph_comment_cmd(cmd, timestamp)
                        if parsed:
                            blocks.append(parsed)
                            continue
                    # Semantic Bash: graph dispatch approve
                    if "graph dispatch approve" in cmd:
                        parsed = _parse_dispatch_approve_cmd(cmd, timestamp)
                        if parsed:
                            blocks.append(parsed)
                            continue
                    # Semantic Bash: bd set-state
                    if "bd set-state" in cmd:
                        parsed = _parse_bd_setstate_cmd(cmd, timestamp)
                        if parsed:
                            blocks.append(parsed)
                            continue
                blocks.append({
                    "type": "tool_use",
                    "role": "assistant",
                    "tool_name": tool_name,
                    "tool_id": block.get("id", ""),
                    "input": tool_input,
                    "timestamp": timestamp,
                })
            elif btype == "thinking":
                thinking = block.get("thinking", "").strip()
                if thinking:
                    blocks.append({
                        "type": "thinking",
                        "role": "assistant",
                        "content": thinking,
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
        if not result_content:
            return {
                "type": "tool_result",
                "role": "tool",
                "tool_id": tool_id,
                "content": "",
                "is_error": raw.get("is_error", False),
                "timestamp": timestamp,
            }
        # Always emit tool_result for tracking; optionally also a semantic tile
        base_result = {
            "type": "tool_result",
            "role": "tool",
            "tool_id": tool_id,
            "content": result_content,
            "is_error": raw.get("is_error", False),
            "timestamp": timestamp,
        }
        sem = _upconvert_graph_result(result_content, timestamp, tool_id=tool_id)
        if sem:
            _enrich_semantic_tile(sem)
            return [base_result, sem]
        return base_result

    return None



def _enrich_entries(entries: list[dict], session_dir: Path | None = None) -> None:
    """Post-process parsed entries: enrich Agent tool_results with subagent info.

    Enriches Agent tool_results with subagent tool call counts by reading the
    actual subagent JSONL files.  Mutates entries in place.

    Args:
        entries: Parsed JSONL entries (tool_use and tool_result dicts).
        session_dir: Path to the session directory (parent of the JSONL file).
            When provided, subagent files are discovered at
            ``session_dir/{session_id}/subagents/*.meta.json``.
    """
    if session_dir is None:
        return

    agent_descriptions: dict[str, str] = {}  # tool_id -> description
    claimed: set[str] = set()

    for entry in entries:
        if entry.get("type") == "tool_use" and entry.get("tool_name") == "Agent":
            tool_id = entry.get("tool_id", "")
            desc = entry.get("input", {}).get("description", "")
            if tool_id and desc:
                agent_descriptions[tool_id] = desc

        elif entry.get("type") == "tool_result" and entry.get("tool_id"):
            tool_id = entry["tool_id"]
            if tool_id not in agent_descriptions:
                continue
            target_desc = agent_descriptions[tool_id]

            # Find the subagents directory — try multiple session ID patterns
            # Container: session_dir/{uuid}/subagents/
            # The session_dir is the parent of the JSONL file
            subagents_dir = session_dir / "subagents"
            if not subagents_dir.is_dir():
                continue

            for meta_path in sorted(subagents_dir.glob("*.meta.json")):
                if str(meta_path) in claimed:
                    continue
                try:
                    meta = json.loads(meta_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if meta.get("description") == target_desc:
                    claimed.add(str(meta_path))
                    jsonl_path = meta_path.with_suffix("").with_suffix(".jsonl")
                    if jsonl_path.exists():
                        count = count_tool_uses(jsonl_path)
                        if count > 0:
                            entry["tool_calls"] = count
                    break


def _dedup_queued_entries(entries: list[dict]) -> list[dict]:
    """Remove duplicate user/crosstalk entries that follow a queued version.

    When a user sends a message while the agent is working, Claude Code writes
    two JSONL entries: a queue-operation (enqueue) and a subsequent user entry
    with identical content.  _parse_jsonl_entry renders both, causing duplicates.
    This helper keeps the queued version and drops the duplicate that follows.
    """
    result = []
    last_enqueue_content = None
    for entry in entries:
        if entry.get("queued"):
            last_enqueue_content = entry.get("content", "").strip()
            result.append(entry)
        elif (entry.get("type") in ("user", "crosstalk")
              and last_enqueue_content
              and entry.get("content", "").strip() == last_enqueue_content):
            last_enqueue_content = None  # consumed — skip duplicate
        else:
            # Don't reset the tracker on unrelated intervening entries
            # (assistant turns, tool_result, etc.).  The duplicate user/
            # crosstalk that mirrors the queued content typically arrives
            # AFTER the agent's assistant turn, not immediately after the
            # queue-operation.
            result.append(entry)
    return result


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

    # Mock mode: resolve by run_dir against the fixture, mirroring
    # api_session_tail's DASHBOARD_MOCK branch.
    if os.environ.get("DASHBOARD_MOCK"):
        sess = dao_sessions.get_session_by_run_dir(run_name)
        if sess:
            sid = sess.get("session_id") or sess.get("tmux_session") or run_name
            entries = dao_sessions.get_session_entries(sid) or []
            TaskStateTracker().enrich(sid, entries)
            return JSONResponse({
                "entries": entries,
                "offset": len(entries),
                "is_live": bool(sess.get("is_live", True)),
                "session_id": sid,
                "tmux_session": sess.get("tmux_session") or sid,
                "tmux_name": sess.get("tmux_session") or sid,
                "project": sess.get("project") or "autonomy",
                "type": sess.get("type") or "dispatch",
                "role": sess.get("role") or "",
                "resolved": True,
                "seq": len(entries),
            })
        # fall through to filesystem scan

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

    session_uuid = session_file.stem
    project = session_file.parent.name

    # Resolve tmux_name for this dispatch run. The monitor broadcasts
    # session:messages keyed on tmux_name (session_monitor.py:1075), so the
    # tail response MUST key its session_id on tmux_name too — otherwise the
    # overlay's SSE filter drops every broadcast (auto-yaw58).
    db_row = session_monitor.get_one(run_name)
    if db_row is None:
        owner = session_monitor._find_session_by_uuid(session_uuid)
        if owner:
            db_row = session_monitor.get_one(owner)
    tmux_name = db_row["tmux_name"] if db_row else run_name
    session_id = tmux_name

    if after >= file_size:
        return JSONResponse({
            "entries": [], "offset": file_size, "is_live": is_live,
            "session_id": session_id, "tmux_name": tmux_name,
            "tmux_session": tmux_name, "session_uuid": session_uuid,
            "project": project,
        })

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

    entries = _dedup_queued_entries(entries)
    _enrich_entries(entries, session_dir=session_file.parent / session_file.stem)
    return JSONResponse({
        "entries": entries,
        "offset": new_offset,
        "is_live": is_live,
        "session_id": session_id,
        "tmux_name": tmux_name,
        "tmux_session": tmux_name,
        "session_uuid": session_uuid,
        "project": project,
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

    entries = _dedup_queued_entries(entries)
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


async def api_session_tail(request):
    """Tail JSONL entries for any session by project/session_id.

    GET /api/session/{project}/{session_id}/tail?after=N
    Returns {entries: [...], offset: N, is_live: bool}.
    Increment `after` with each poll to receive only new entries.
    """
    project = request.path_params["project"]
    session_id = request.path_params["session_id"]
    after = int(request.query_params.get("after", "0"))

    # Mock mode: return fixture entries if available
    if os.environ.get("DASHBOARD_MOCK"):
        entries = dao_sessions.get_session_entries(session_id)
        if entries is not None:
            # Look up session metadata from mock DAO for is_live and type.
            # Use get_session_by_id so dispatch/librarian rows (filtered out
            # of get_active_sessions) still resolve — otherwise is_live
            # defaults to True and dead-dispatch viewers render as live.
            mock_session = dao_sessions.get_session_by_id(session_id) or {}
            TaskStateTracker().enrich(session_id, entries)
            resp = {
                "entries": entries, "offset": len(entries),
                "is_live": bool(mock_session.get("is_live", True)),
                "type": mock_session.get("type", ""),
                "role": mock_session.get("role", ""),
                "tmux_session": mock_session.get("tmux_session", session_id),
                "seq": len(entries),
                "resolved": bool(mock_session.get("resolved", True)),
            }
            return JSONResponse(resp)

    # First, try resolving via DB (session_id may be a tmux_name,
    # a dispatch session_uuid, a UUID in session_uuids, or a run_dir id)
    session_file = None
    db_row = session_monitor.get_one(session_id)
    if db_row is None:
        owner = session_monitor._find_session_by_uuid(session_id)
        if owner is None:
            # Also try the dead/historical rows where session_uuid matches
            from tools.dashboard.dao.dashboard_db import get_conn as _get_conn
            try:
                conn = _get_conn()
                row = conn.execute(
                    "SELECT tmux_name FROM tmux_sessions"
                    " WHERE session_uuid=? OR session_uuids LIKE ?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (session_id, f"%{session_id}%"),
                ).fetchone()
                if row:
                    owner = row[0] if not hasattr(row, "keys") else row["tmux_name"]
            except Exception:
                owner = None
        if owner:
            db_row = session_monitor.get_one(owner)

    if db_row and db_row.get("jsonl_path"):
        candidate = Path(db_row["jsonl_path"])
        if candidate.exists():
            session_file = candidate

    # Last-resort: scan data/agent-runs/ for a JSONL matching the id
    if session_file is None:
        fallback = session_monitor.resolve_session_file(session_id)
        if fallback is not None:
            session_file = fallback

    if session_file is None:
        # Session file not resolved — check if it's a newly created session
        # registered in the monitor but with no JSONL yet
        if db_row and db_row.get("is_live"):
            return JSONResponse({
                "entries": [], "offset": 0, "is_live": True,
                "type": db_row.get("type", ""),
                "role": db_row.get("role", ""),
                "session_id": db_row.get("tmux_name", ""),
                "tmux_session": db_row.get("tmux_name", ""),
                "tmux_name": db_row.get("tmux_name", ""),
                "session_uuid": db_row.get("session_uuid", ""),
                "seq": 0, "resolved": False,
            })
        return JSONResponse(
            {"error": "Session not found", "session_id": session_id},
            status_code=404,
        )
    if not session_file.exists():
        if db_row and db_row.get("is_live"):
            return JSONResponse({
                "entries": [], "offset": 0, "is_live": True,
                "type": db_row.get("type", ""),
                "role": db_row.get("role", ""),
                "session_id": db_row.get("tmux_name", ""),
                "tmux_session": db_row.get("tmux_name", ""),
                "tmux_name": db_row.get("tmux_name", ""),
                "session_uuid": db_row.get("session_uuid", ""),
                "seq": 0, "resolved": False,
            })
        return JSONResponse(
            {"error": "Session not found", "session_id": session_id},
            status_code=404,
        )

    file_size = session_file.stat().st_size
    # Determine session type from resolved path
    home_projects = Path.home() / ".claude" / "projects"
    session_type = "host" if session_file.is_relative_to(home_projects) else "container"

    # Liveness from DB — dashboard.db is the sole owner after initial seed
    is_live = bool(db_row.get("is_live")) if db_row else False
    tmux_name = db_row.get("tmux_name", "") if db_row else ""
    # Resolved: session has a linked JSONL path or non-empty session_uuids
    resolved = bool(db_row.get("jsonl_path")) or (
        bool(db_row.get("session_uuids")) and db_row["session_uuids"] != "[]"
    ) if db_row else False

    seq = 0  # broadcast_seq is now on ephemeral _TailState, not in DB row

    role = db_row.get("role", "") if db_row else ""
    activity_state = db_row.get("activity_state", "idle") if db_row else "idle"
    session_uuid = db_row.get("session_uuid", "") if db_row else ""
    base_resp = {"entries": [], "offset": file_size, "is_live": is_live,
                 "type": session_type, "role": role,
                 "activity_state": activity_state,
                 "seq": seq, "resolved": resolved}
    if tmux_name:
        base_resp["session_id"] = tmux_name
        base_resp["tmux_session"] = tmux_name
        base_resp["tmux_name"] = tmux_name
    if session_uuid:
        base_resp["session_uuid"] = session_uuid
    if after >= file_size:
        return JSONResponse(base_resp)

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

    entries = _dedup_queued_entries(entries)
    _enrich_entries(entries, session_dir=session_file.parent / session_file.stem)
    # Task* tile annotations need full-history context to resolve taskId→subject.
    # Partial polls (after>0) miss earlier TaskCreates, so replay from offset 0.
    if after > 0:
        history: list = []
        with open(session_file, "rb") as f:
            history_data = f.read(after)
        for line in history_data.decode("utf-8", errors="replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = _parse_jsonl_entry(line)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                history.extend(parsed)
            else:
                history.append(parsed)
        local_tracker = TaskStateTracker()
        local_tracker.enrich("_http", history)
        local_tracker.enrich("_http", entries)
    else:
        TaskStateTracker().enrich("_http", entries)
    resp = {"entries": entries, "offset": new_offset, "is_live": is_live,
            "type": session_type, "role": role,
            "activity_state": activity_state,
            "seq": seq, "resolved": resolved}
    if tmux_name:
        resp["session_id"] = tmux_name
        resp["tmux_session"] = tmux_name
        resp["tmux_name"] = tmux_name
    if session_uuid:
        resp["session_uuid"] = session_uuid
    return JSONResponse(resp)


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

    # Inject via unified tmux_send (per-session lock + double-Enter retry)
    logger.warning("[session-send] tmux=%r message=%r", tmux_session, message)
    try:
        await tmux_send(tmux_session, message)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )

    return JSONResponse({"ok": True, "tmux_session": tmux_session})


async def api_session_interrupt(request):
    """Send Escape key to a tmux session to interrupt a running tool.

    POST /api/session/{tmux_name}/interrupt
    Returns: {"ok": true}
    """
    tmux_name = request.path_params["tmux_name"]
    try:
        exists = _tmux_session_exists(tmux_name)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )
    if not exists:
        return JSONResponse(
            {"error": f"tmux session '{tmux_name}' not found"},
            status_code=404,
        )
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_name, "Escape"],
        capture_output=True,
    )
    logger.warning("[session-interrupt] tmux=%r", tmux_name)
    return JSONResponse({"ok": True})


async def api_terminal_unclaimed(request):
    """Return unclaimed host tmux sessions — those with no jsonl_path yet.

    GET /api/terminal/unclaimed
    Returns live host sessions from dashboard.db that don't yet have a JSONL link.
    """
    sessions = dashboard_db.get_live_sessions()
    now = time.time()
    result = []
    for row in sessions:
        if row["type"] != "host":
            continue
        if row.get("jsonl_path"):
            continue  # already linked
        # Verify still alive
        alive = _tmux_session_exists(row["tmux_name"])
        if not alive:
            continue
        elapsed = int(now - row["created_at"])
        result.append({
            "tmux_session": row["tmux_name"],
            "elapsed_seconds": elapsed,
            "cmd": "",
        })
    return JSONResponse(result)


async def api_session_send_handshake(request):
    """Send a handshake string to a candidate tmux session for link confirmation.

    POST /api/session/send-handshake
    Body: {"tmux_session": "auto-t6"}
    Returns: {"ok": true, "handshake": "<the string sent>"}
    """
    body = await request.json()
    tmux_session = (body.get("tmux_session") or "").strip()
    if not tmux_session:
        return JSONResponse({"error": "tmux_session is required"}, status_code=400)

    handshake = "[dashboard] confirming terminal link \u2014 please reply with I SEE IT"

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

    # Inject via unified tmux_send (per-session lock + double-Enter retry)
    logger.warning("[send-handshake] tmux=%r", tmux_session)
    try:
        await tmux_send(tmux_session, handshake)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )

    return JSONResponse({"ok": True, "handshake": handshake})


async def api_session_confirm_link(request):
    """Confirm a terminal link after handshake — scans filesystem for JSONL.

    POST /api/session/confirm-link
    Body: {"tmux_session": "auto-t6", "handshake": "[dashboard] confirming..."}
    Returns: {"ok": true, "project": "...", "session_id": "..."}

    Scans ~/.claude/projects/ for the newest JSONL files containing the
    handshake text.  No SSE/store dependency — solves the chicken-and-egg
    problem where entries are empty because jsonl_path is NULL.
    """
    body = await request.json()
    tmux_session = (body.get("tmux_session") or "").strip()
    handshake_text = (body.get("handshake") or "").strip()

    if not tmux_session:
        return JSONResponse({"error": "tmux_session required"}, status_code=400)

    # Scan all project directories for newest JSONL containing handshake
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return JSONResponse({"error": "no projects directory"}, status_code=404)

    # Collect all JSONL files across all projects, sorted by mtime descending
    all_jsonls = []
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            all_jsonls.append(jf)

    all_jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # Check newest files first — read last 5 entries for handshake text
    for jf in all_jsonls[:5]:  # only check 5 newest files
        try:
            lines = jf.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            tail = lines[-5:] if len(lines) > 5 else lines
            for line in tail:
                if handshake_text and handshake_text in line:
                    # Found it — this is our file
                    project = jf.parent.name
                    session_id = jf.stem
                    logger.info("confirm-link: FOUND handshake in %s/%s", project, session_id[:12])
                    dashboard_db.link_and_enrich(
                        tmux_session,
                        session_uuid=session_id,
                        jsonl_path=str(jf),
                        project=project,
                    )
                    # Install inotify watches so the tailer starts broadcasting
                    # session:messages as new entries arrive. Without this, the
                    # link is persisted in the DB but no live SSE flows —
                    # the viewer only sees new content on /tail?after=0 fetches
                    # (nav or force refresh). See signpost d931649b-413 §2/§7c.
                    from tools.dashboard.session_monitor import _TailState
                    if tmux_session not in session_monitor._tail_states:
                        session_monitor._tail_states[tmux_session] = _TailState(
                            resolution_dir=jf.parent,
                        )
                    session_monitor._add_file_watch(tmux_session, str(jf))
                    session_monitor._add_dir_watch(tmux_session, str(jf.parent))
                    await session_monitor._broadcast_registry()
                    return JSONResponse({"ok": True, "project": project, "session_id": session_id})
        except Exception:
            continue

    return JSONResponse({"error": "handshake not found in any recent JSONL"}, status_code=404)


async def api_session_get(request):
    """GET /api/session/{tmux_name} — return session details."""
    tmux_name = request.path_params["tmux_name"]
    session = dashboard_db.get_session(tmux_name)
    if not session:
        return JSONResponse({"error": "not found"}, status_code=404)
    from tools.dashboard.org_identity import resolve_session_org
    return JSONResponse({
        "session_id": session["tmux_name"],
        "session_uuid": session.get("session_uuid"),
        "graph_source_id": session.get("graph_source_id"),
        "type": session.get("type"),
        "role": session.get("role", ""),
        "activity_state": session.get("activity_state", "idle"),
        "project": session.get("project"),
        "org": resolve_session_org(session),
        "is_live": bool(session.get("is_live")),
        "dispatch_nag_enabled": bool(session.get("dispatch_nag")),
        "nag_enabled": bool(session.get("nag_enabled")),
        "nag_interval": session.get("nag_interval"),
        "nag_message": session.get("nag_message"),
        "nag_last_sent": session.get("nag_last_sent"),
    })


async def api_session_label(request):
    """Set or clear the user-facing label for a session.

    PUT /api/session/{tmux_name}/label
    Body: {"label": "Dashboard auth design"}
    Returns: {"ok": true}
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    label = body.get("label", "").strip()
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_label(tmux_name, label)
    # Also update graph source title if the session has been ingested
    session_row = dashboard_db.get_session(tmux_name)
    if session_row and session_row.get("graph_source_id"):
        graph_ops.update_source_title(session_row["graph_source_id"], label)
    # Broadcast via SSE so all clients update
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_topics(request):
    """Set sub-topic status lines for a session card.

    PUT /api/session/{tmux_name}/topics
    Body: {"topics": ["Researching auth flow", "Reading server.py"]}
    Returns: {"ok": true}

    Rules: 1-4 strings, max 80 chars each, plain text only.
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    topics_raw = body.get("topics", [])

    # Validate
    if not isinstance(topics_raw, list):
        return JSONResponse({"error": "topics must be an array"}, status_code=400)
    if len(topics_raw) > 4:
        return JSONResponse({"error": "max 4 topics"}, status_code=400)

    # Sanitize: plain text, strip, truncate to 80 chars
    topics = []
    for t in topics_raw:
        if not isinstance(t, str):
            continue
        clean = t.strip().replace('<', '').replace('>', '')[:80]
        if clean:
            topics.append(clean)

    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_topics(tmux_name, topics)
    # Broadcast via SSE so all clients update
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_role(request):
    """Set or clear the explicit role for a session.

    PUT /api/session/{tmux_name}/role
    Body: {"role": "coordinator"}
    Returns: {"ok": true}

    Any string up to 32 characters accepted. Empty string clears the role.
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    role = body.get("role", "")

    if not isinstance(role, str):
        return JSONResponse({"error": "role must be a string"}, status_code=400)

    role = role.strip().lower()
    if len(role) > 32:
        return JSONResponse({"error": "role too long (max 32 chars)"}, status_code=400)

    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_role(tmux_name, role)
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_nag(request):
    """Configure nag alerts for a session.

    PUT /api/session/{tmux_name}/nag
    Body: {"enabled": true, "interval": 15, "message": "Status update please."}
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    enabled = body.get("enabled")
    interval = body.get("interval")
    message = body.get("message")

    if interval is not None:
        if not isinstance(interval, int) or interval < 1 or interval > 120:
            return JSONResponse({"error": "interval must be 1-120 minutes"}, status_code=400)
    if message is not None:
        message = str(message).strip().replace('<', '').replace('>', '')[:200]

    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_nag_config(
        tmux_name,
        enabled=enabled,
        interval=interval,
        message=message,
    )
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_nag_delete(request):
    """Disable nag for a session.

    DELETE /api/session/{tmux_name}/nag
    """
    tmux_name = request.path_params["tmux_name"]
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_nag_config(tmux_name, enabled=False)
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_dispatch_nag(request):
    """Enable or disable dispatch completion nag for a session.

    PUT /api/session/{tmux_name}/dispatch-nag
    Body: {"enabled": true}
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_dispatch_nag(tmux_name, enabled)
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def _resolve_primer(primer: str) -> str | None:
    """Resolve a graph:// URL to its text content.  Returns None on failure."""
    graph_id = primer.removeprefix("graph://") if primer.startswith("graph://") else primer
    if not graph_id:
        return None
    try:
        stdout, stderr, rc = await run_cli(
            ["graph", "read", graph_id, "--max-chars", "50000"], timeout=10,
        )
        if rc == 0 and stdout.strip():
            return stdout.strip()
        logger.warning("_resolve_primer: graph read failed  id=%s  rc=%d  stderr=%s",
                       graph_id, rc, stderr.strip())
    except Exception:
        logger.warning("_resolve_primer: exception resolving %s", graph_id, exc_info=True)
    return None


async def api_session_create(request):
    """Create a new session (container, project, or host) and return its tmux name.

    POST /api/session/create
    Body: {
        "project": "enterprise-ng",   # optional — launches workspace container
        "type": "host" | "container", # optional — defaults to container
        "primer": "graph://...",      # optional — graph URL whose content is
                                      #            injected as first message
    }
    Returns: {"tmux_name": str, "label": str, "type": str}

    Semantics:
      • `project` set → load workspace config, prepare mounts/worktrees,
        launch image with DinD/graph scoping via project registry.
      • `type == "host"` → start `claude --dangerously-skip-permissions` on
        the host, then watch for its JSONL to appear.
      • neither → default `autonomy-agent:dashboard` container session.

    Container sessions block up to 30s for the monitor to mark them live with
    a resolved JSONL path.  Host sessions return immediately — their JSONL is
    discovered asynchronously by `_watch_for_host_session_jsonl`.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    session_type = body.get("type", "container")
    project_name = body.get("project")
    primer_url = body.get("primer")

    if session_type not in ("container", "host"):
        return JSONResponse(
            {"error": "type must be 'container' or 'host'"}, status_code=400,
        )
    if project_name and session_type == "host":
        return JSONResponse(
            {"error": "project cannot be combined with type='host'"},
            status_code=400,
        )

    # ── Generate unique tmux name ───────────────────────────────
    prefix = "host" if session_type == "host" else "auto"
    tmux_name = f"{prefix}-{time.strftime('%m%d-%H%M%S')}"
    if dashboard_db.session_exists(tmux_name):
        import random
        tmux_name = f"{prefix}-{time.strftime('%m%d-%H%M%S')}-{random.randint(10, 99)}"

    # ── Build the command to run inside tmux ───────────────────
    proj = None
    if project_name:
        try:
            proj = workspace_settings.get_workspace(project_name)
        except KeyError:
            return JSONResponse(
                {"error": f"Unknown project '{project_name}'"}, status_code=400,
            )
        except workspace_settings.WorkspaceSettingsError as e:
            return JSONResponse(
                {"error": f"Project config error: {e}"}, status_code=500,
            )
        missing_artifacts = workspace_settings.validate_artifacts(proj)
        if missing_artifacts:
            first = missing_artifacts[0]
            message = workspace_settings.format_missing_artifact_error(first, proj)
            logger.warning(
                "api_session_create: missing required artifact  project=%s  name=%s  path=%s",
                proj.id, first.artifact.name, first.path,
            )
            return JSONResponse(
                {
                    "error": message,
                    "missing_artifacts": [
                        {
                            "name": m.artifact.name,
                            "description": m.artifact.description,
                            "help": m.artifact.help,
                            "expected_path": str(m.path),
                        }
                        for m in missing_artifacts
                    ],
                },
                status_code=400,
            )
        try:
            project_mounts = prepare_session_mounts(proj, tmux_name)
        except WorkspaceError as e:
            logger.error(
                "api_session_create: workspace prep failed  project=%s  err=%s",
                proj.id, e,
            )
            return JSONResponse(
                {"error": f"Workspace prep failed: {e}"}, status_code=500,
            )
        project_mounts.update(workspace_settings.artifact_mounts(proj))
        meta: dict = {
            "tmux_session": tmux_name,
            "project": proj.id,
            "graph_project": proj.graph_project,
        }
        if proj.default_tags:
            meta["graph_tags"] = list(proj.default_tags)
        extra_env: dict[str, str] = dict(proj.env) if proj.env else {}
        for _var in proj.env_from_host:
            _val = os.environ.get(_var)
            if _val is not None:
                extra_env[_var] = _val
        extra_env = extra_env or None
        # Render the runtime primer from the workspace config (layer 1 of
        # the context stack). Writing it into the session's run_dir keeps
        # it colocated with other session artifacts.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_dir = _REPO_ROOT / "data" / "agent-runs" / f"{tmux_name}-{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        primer_path = run_dir / ".claude_md"
        primer_path.write_text(render_workspace_primer(proj))
        global_claude_md = primer_path
        startup_script = (_REPO_ROOT / proj.startup) if proj.startup else None
        working_dir = proj.working_dir or "/workspace/repo"
        cmd_str = launch_session(
            session_type="terminal",
            name=tmux_name,
            prompt=None,
            detach=False,
            image=proj.image,
            mounts=project_mounts or None,
            metadata=meta,
            extra_env=extra_env,
            output_dir=str(run_dir),
            global_claude_md=global_claude_md,
            startup_script=startup_script,
            privileged=proj.dind,
            working_dir=working_dir,
            network_host=proj.network_host,
        )
        if not cmd_str:
            return JSONResponse(
                {"error": f"launch_session failed for project '{proj.id}'"},
                status_code=500,
            )
        is_container = True
    elif session_type == "host":
        model = "claude-opus-4-7[1m]"
        cmd_str = (
            f"BD_ACTOR=terminal:{tmux_name} AUTONOMY_SESSION={tmux_name} "
            f"claude --dangerously-skip-permissions --model {model}"
        )
        is_container = False
    else:
        cmd_str = launch_session(
            session_type="terminal",
            name=tmux_name,
            prompt=None,
            detach=False,
            image="autonomy-agent:dashboard",
            metadata={"tmux_session": tmux_name},
            global_claude_md=_REPO_ROOT / "agents/shared/terminal/CLAUDE.md",
        )
        if not cmd_str:
            return JSONResponse({"error": "Failed to resolve credentials"}, status_code=500)
        is_container = True

    # ── Launch tmux ─────────────────────────────────────────────
    tmux_cmd = ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "120", "-y", "40"]
    if not is_container:
        tmux_cmd += ["-c", str(_REPO_ROOT)]
    tmux_cmd.append(cmd_str)
    result = subprocess.run(
        tmux_cmd, env={**os.environ, "TERM": "xterm-256color"}, capture_output=True,
    )
    if result.returncode != 0:
        logger.error(
            "api_session_create: tmux new-session failed  tmux=%s  rc=%d  stderr=%s",
            tmux_name, result.returncode, result.stderr.decode().strip(),
        )
        return JSONResponse(
            {"error": f"tmux creation failed: {result.stderr.decode().strip()}"},
            status_code=500,
        )

    # Enable OSC 52 + mouse + passthrough for all sessions
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "set-clipboard", "on"], capture_output=True)
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "mouse", "on"], capture_output=True)
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "allow-passthrough", "on"], capture_output=True)

    # ── Register with session monitor ───────────────────────────
    if is_container:
        agent_runs = _REPO_ROOT / "data" / "agent-runs"
        run_dirs = sorted(
            agent_runs.glob(f"{tmux_name}-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ) if agent_runs.exists() else []
        sess_dir = run_dirs[0] / "sessions" if run_dirs else agent_runs
        monitor_project = proj.id if proj else sess_dir.name
        await session_monitor.register(
            tmux_name=tmux_name,
            session_type="container",
            project=monitor_project,
            jsonl_path=sess_dir,
            seed_message="Starting..." if not primer_url else "",
        )
    else:
        project_folder = str(_REPO_ROOT).replace("/", "-")
        projects_dir = Path.home() / ".claude" / "projects" / project_folder
        await session_monitor.register(
            tmux_name=tmux_name,
            session_type="host",
            project=project_folder,
        )
        asyncio.create_task(
            _watch_for_host_session_jsonl(projects_dir, tmux_name),
        )

    # ── Resolve primer (container only) ─────────────────────────
    first_message: str | None = None
    primer_error: str | None = None
    if is_container:
        first_message = "Hello"
        if primer_url:
            resolved = await _resolve_primer(primer_url)
            if resolved:
                first_message = resolved
            else:
                primer_error = f"Could not resolve primer {primer_url!r}, falling back to hello"
                logger.warning("api_session_create: %s", primer_error)

    if first_message:
        async def _inject_first_message():
            await asyncio.sleep(5)
            try:
                await tmux_send(tmux_name, first_message)
                logger.info(
                    "api_session_create: injected first message into %s  len=%d  primer=%s",
                    tmux_name, len(first_message), bool(primer_url and not primer_error),
                )
            except Exception:
                logger.warning(
                    "api_session_create: failed to inject first message into %s",
                    tmux_name, exc_info=True,
                )

        asyncio.create_task(_inject_first_message())

    response_type = "container" if is_container else "host"

    # ── Host sessions: return immediately; watcher resolves JSONL ──
    if not is_container:
        return JSONResponse({
            "tmux_name": tmux_name,
            "label": "",
            "type": response_type,
        })

    # ── Container sessions: poll for is_live + jsonl_path ──
    deadline = time.time() + 30
    while time.time() < deadline:
        row = dashboard_db.get_session(tmux_name)
        if row and row.get("is_live") and row.get("jsonl_path"):
            resp = {
                "tmux_name": tmux_name,
                "label": row.get("label", ""),
                "type": response_type,
            }
            if primer_error:
                resp["primer_warning"] = primer_error
            return JSONResponse(resp)
        await asyncio.sleep(1)

    resp = {
        "tmux_name": tmux_name,
        "label": "",
        "type": response_type,
        "warning": "Session created but not yet tracked by session monitor. It may appear shortly.",
    }
    if primer_error:
        resp["primer_warning"] = primer_error
    return JSONResponse(resp, status_code=202)


async def api_session_resume(request):
    """Resume an existing Claude session.

    POST /api/session/resume
    Body: {"source_id": "abc123"} OR {"session_uuid": "uuid", "file_path": "/path/to.jsonl"}
    Returns: {"tmux_name": str, "label": str, "type": str}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    source_id = body.get("source_id")
    session_uuid = body.get("session_uuid")
    file_path = body.get("file_path")
    session_type = None  # "container" or "host"

    # ── Resolve from graph source metadata if source_id provided ──
    if source_id:
        try:
            src = graph_ops.resolve_source_strict(source_id)
        except Exception:
            return JSONResponse({"error": "Failed to query graph.db"}, status_code=500)

        if src is None:
            return JSONResponse({"error": f"Source '{source_id}' not found"}, status_code=404)
        if isinstance(src, list):
            return JSONResponse(
                {"error": f"Ambiguous source_id '{source_id}', {len(src)} candidates"},
                status_code=400,
            )
        if src.get("type") != "session":
            return JSONResponse(
                {"error": f"Source '{source_id}' is type '{src.get('type')}', not a session"},
                status_code=400,
            )

        file_path = src.get("file_path", "")
        meta = {}
        if src.get("metadata"):
            try:
                meta = json.loads(src["metadata"])
            except Exception:
                pass
        session_uuid = meta.get("session_uuid", "")

    # ── Validate required fields ──
    if not session_uuid:
        return JSONResponse({"error": "session_uuid is required (provide source_id or session_uuid)"}, status_code=400)
    if not file_path:
        return JSONResponse({"error": "file_path is required (provide source_id or file_path)"}, status_code=400)

    # ── Verify JSONL file exists on disk ──
    jsonl_path = Path(file_path)
    if not jsonl_path.exists():
        return JSONResponse(
            {"error": f"JSONL file not found: {file_path}"},
            status_code=404,
        )

    # ── Determine session type ──
    agent_runs_dir = str(_REPO_ROOT / "data" / "agent-runs")
    if session_type is None:
        if file_path.startswith(agent_runs_dir) or "/agent-runs/" in file_path:
            session_type = "container"
        else:
            session_type = "host"

    # ── Guard: reject if session is already active ──
    live_session = dashboard_db.find_live_session(
        session_uuid=session_uuid, file_path=file_path,
    )
    if live_session:
        return JSONResponse(
            {"error": f"Session is already active as '{live_session['tmux_name']}'"},
            status_code=409,
        )

    # ── Look up existing dead session in dashboard.db ──
    dead_session = dashboard_db.find_dead_session(
        session_uuid=session_uuid, file_path=file_path,
    )

    if dead_session and not dead_session.get("is_live"):
        # Reuse the original session identity
        tmux_name = dead_session["tmux_name"]
        label = dead_session.get("label", "")
    else:
        # No dead session found — derive name from graph metadata or generate one
        tmux_name = None
        label = ""

        # Try to get original container_name from graph source metadata
        if source_id:
            try:
                meta_str = src.get("metadata", "")
                meta_obj = json.loads(meta_str) if isinstance(meta_str, str) and meta_str else {}
                tmux_name = meta_obj.get("container_name", "")
            except Exception:
                pass

        if not tmux_name:
            tmux_name = f"resume-{time.strftime('%m%d-%H%M%S')}"

        if dashboard_db.session_exists(tmux_name):
            import random
            tmux_name = f"resume-{time.strftime('%m%d-%H%M%S')}-{random.randint(10, 99)}"

    model = "claude-opus-4-7[1m]"

    if session_type == "container":
        # Derive output_dir from JSONL parent path
        # JSONL is typically at data/agent-runs/<name>-<ts>/sessions/<uuid>/<file>.jsonl
        # output_dir is the run directory (parent of sessions/)
        output_dir = str(jsonl_path.parent.parent.parent)

        # If this was a workspace (project-scoped) session, resume with the
        # same image, mounts, and env.  dashboard.db.project stores the
        # project id (e.g. "enterprise-ng") for workspace sessions.
        proj_for_resume = None
        dead_project = (dead_session or {}).get("project") if dead_session else None
        if dead_project:
            try:
                proj_for_resume = workspace_settings.get_workspace(dead_project)
            except (KeyError, workspace_settings.WorkspaceSettingsError):
                proj_for_resume = None

        if proj_for_resume is not None:
            missing_artifacts = workspace_settings.validate_artifacts(proj_for_resume)
            if missing_artifacts:
                first = missing_artifacts[0]
                message = workspace_settings.format_missing_artifact_error(first, proj_for_resume)
                logger.warning(
                    "api_session_resume: missing required artifact  project=%s  name=%s  path=%s",
                    proj_for_resume.id, first.artifact.name, first.path,
                )
                return JSONResponse(
                    {
                        "error": message,
                        "missing_artifacts": [
                            {
                                "name": m.artifact.name,
                                "description": m.artifact.description,
                                "help": m.artifact.help,
                                "expected_path": str(m.path),
                            }
                            for m in missing_artifacts
                        ],
                    },
                    status_code=400,
                )
            try:
                resume_mounts = prepare_session_mounts(proj_for_resume, tmux_name)
            except WorkspaceError as e:
                logger.error(
                    "api_session_resume: workspace prep failed  project=%s  err=%s",
                    proj_for_resume.id, e,
                )
                return JSONResponse(
                    {"error": f"Workspace prep failed: {e}"}, status_code=500,
                )
            resume_mounts.update(workspace_settings.artifact_mounts(proj_for_resume))
            meta: dict = {
                "tmux_session": tmux_name,
                "project": proj_for_resume.id,
                "graph_project": proj_for_resume.graph_project,
            }
            if proj_for_resume.default_tags:
                meta["graph_tags"] = list(proj_for_resume.default_tags)
            extra_env: dict[str, str] = dict(proj_for_resume.env) if proj_for_resume.env else {}
            for _var in proj_for_resume.env_from_host:
                _val = os.environ.get(_var)
                if _val is not None:
                    extra_env[_var] = _val
            extra_env = extra_env or None
            run_dir = Path(output_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            primer_path = run_dir / ".claude_md"
            primer_path.write_text(render_workspace_primer(proj_for_resume))
            global_claude_md = primer_path
            startup_script = (
                _REPO_ROOT / proj_for_resume.startup
                if proj_for_resume.startup else None
            )
            working_dir = proj_for_resume.working_dir or "/workspace/repo"
            docker_cmd = launch_session(
                session_type="terminal",
                name=tmux_name,
                prompt=None,
                detach=False,
                image=proj_for_resume.image,
                mounts=resume_mounts or None,
                metadata=meta,
                extra_env=extra_env,
                global_claude_md=global_claude_md,
                startup_script=startup_script,
                privileged=proj_for_resume.dind,
                working_dir=working_dir,
                output_dir=output_dir,
                model=model,
                resume_uuid=session_uuid,
                network_host=proj_for_resume.network_host,
            )
        else:
            docker_cmd = launch_session(
                session_type="terminal",
                name=tmux_name,
                prompt=None,
                detach=False,
                image="autonomy-agent:dashboard",
                metadata={"tmux_session": tmux_name},
                output_dir=output_dir,
                model=model,
                resume_uuid=session_uuid,
                global_claude_md=_REPO_ROOT / "agents/shared/terminal/CLAUDE.md",
            )
        if not docker_cmd:
            return JSONResponse({"error": "Failed to build resume command"}, status_code=500)

        cmd_str = docker_cmd
    else:
        # Host session: bare claude command
        cmd_str = (
            f"BD_ACTOR=terminal:{tmux_name} AUTONOMY_SESSION={tmux_name} "
            f"claude --dangerously-skip-permissions --model {model} --resume {session_uuid}"
        )

    # ── Launch in tmux ──
    tmux_cmd = ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "120", "-y", "40"]
    if session_type == "host":
        tmux_cmd += ["-c", str(_REPO_ROOT)]
    tmux_cmd.append(cmd_str)

    result = subprocess.run(tmux_cmd, env={**os.environ, "TERM": "xterm-256color"}, capture_output=True)
    if result.returncode != 0:
        logger.error("api_session_resume: tmux new-session failed  tmux=%s  rc=%d  stderr=%s",
                     tmux_name, result.returncode, result.stderr.decode().strip())
        return JSONResponse(
            {"error": f"tmux creation failed: {result.stderr.decode().strip()}"},
            status_code=500,
        )

    # Enable OSC 52 + mouse + passthrough
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "set-clipboard", "on"], capture_output=True)
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "mouse", "on"], capture_output=True)
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "allow-passthrough", "on"], capture_output=True)

    # ── Register with session monitor ──
    if dead_session:
        # Revive the existing row: set is_live=1, reset file_offset for full backfill
        dashboard_db.revive_session(tmux_name, file_offset=0)
        # Re-register with session_monitor for tailing (uses existing DB row)
        await session_monitor.register_revived(
            tmux_name=tmux_name,
            jsonl_path=Path(file_path),
        )
    elif session_type == "container":
        # New registration with the JSONL file path for immediate backfill
        await session_monitor.register(
            tmux_name=tmux_name,
            session_type="container",
            project="autonomy",
            jsonl_path=Path(file_path),
            session_uuid=session_uuid,
        )
    else:
        # Host session: register with JSONL path for backfill
        project_folder = str(_REPO_ROOT).replace("/", "-")
        await session_monitor.register(
            tmux_name=tmux_name,
            session_type="host",
            project=project_folder,
            jsonl_path=Path(file_path),
            session_uuid=session_uuid,
        )

    if not label:
        label = src.get("title", "") if source_id and src else ""
    if not label:
        label = f"Resumed: {session_uuid[:12]}"

    return JSONResponse({
        "tmux_name": tmux_name,
        "label": label,
        "type": session_type,
    })


async def api_upload(request):
    """Upload a file to the workspace.

    POST /api/upload
    Multipart form: file field required, optional path param for target directory.
    Saves to data/uploads/ by default (or the specified subdirectory of repo root).
    Returns: {"ok": true, "path": "/workspace/repo/data/uploads/filename.jpg", "filename": "filename.jpg"}
    """
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "invalid multipart form"}, status_code=400)

    upload = form.get("file")
    if upload is None:
        return JSONResponse({"error": "file field is required"}, status_code=400)

    filename = Path(upload.filename).name if upload.filename else "upload"
    # Sanitize filename — strip path separators, limit length
    filename = re.sub(r"[^\w.\-]", "_", filename)[:200] or "upload"

    # Target directory: optional `path` param, default data/uploads
    target_dir_param = (form.get("path") or "").strip()
    if target_dir_param:
        # Resolve relative to repo root, prevent path traversal
        target_dir = (_REPO_ROOT / target_dir_param).resolve()
        if not str(target_dir).startswith(str(_REPO_ROOT)):
            return JSONResponse({"error": "invalid path"}, status_code=400)
    else:
        target_dir = _REPO_ROOT / "data" / "uploads"

    target_dir.mkdir(parents=True, exist_ok=True)

    # Avoid clobbering existing files by appending a counter
    dest = target_dir / filename
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    contents = await upload.read()
    dest.write_bytes(contents)

    host_path = str(dest)
    agent_path = host_path

    tmux_session = (form.get("tmux_session") or "").strip()
    if tmux_session:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", tmux_session],
            capture_output=True, text=True,
        )
        if inspect.returncode == 0 and inspect.stdout.strip() == "true":
            container_path = f"/tmp/{dest.name}"
            cp = subprocess.run(
                ["docker", "cp", host_path, f"{tmux_session}:{container_path}"],
                capture_output=True, text=True,
            )
            if cp.returncode == 0:
                agent_path = container_path

    return JSONResponse({"ok": True, "path": agent_path, "host_path": host_path, "filename": dest.name})


# ── WebSocket Terminal ─────────────────────────────────────────

# Per-project locks to serialise host session JSONL watchers
_host_launch_locks: dict[str, asyncio.Lock] = {}


def _get_host_launch_lock(project_folder: str) -> asyncio.Lock:
    if project_folder not in _host_launch_locks:
        _host_launch_locks[project_folder] = asyncio.Lock()
    return _host_launch_locks[project_folder]


async def _watch_for_host_session_jsonl(
    projects_dir: Path, tmux_name: str, timeout: float = 10.0
) -> None:
    """Watch for a new JSONL to appear after a host Claude session starts.

    Polls every 500ms for up to `timeout` seconds. Updates dashboard.db with
    the discovered JSONL path — the ONLY code that sets jsonl_path for host sessions.
    """
    lock = _get_host_launch_lock(projects_dir.name)
    async with lock:
        existing = set(projects_dir.glob("*.jsonl")) if projects_dir.exists() else set()
        logger.info("JSONL watcher started  tmux=%s  existing=%d", tmux_name, len(existing))
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            if not projects_dir.exists():
                continue
            current = set(projects_dir.glob("*.jsonl"))
            new_files = current - existing
            if new_files:
                new_jsonl = min(new_files, key=lambda p: p.stat().st_mtime)
                logger.info("JSONL watcher found new session  uuid=%s  tmux=%s", new_jsonl.stem, tmux_name)
                # LINK + ENRICH: set session_uuid, jsonl_path, and graph_source_id
                dashboard_db.link_and_enrich(
                    tmux_name,
                    session_uuid=new_jsonl.stem,
                    jsonl_path=str(new_jsonl),
                    project=projects_dir.name,
                )

                # Set up tail state with resolution_dir FIRST — _add_file_watch
                # constructs a default _TailState (no resolution_dir) on first
                # call, so the prior "if not in _tail_states" guard was always
                # false here and resolution_dir never got assigned.
                from tools.dashboard.session_monitor import _TailState
                ts = session_monitor._tail_states.get(tmux_name)
                if ts is None:
                    session_monitor._tail_states[tmux_name] = _TailState(
                        resolution_dir=projects_dir)
                else:
                    ts.resolution_dir = projects_dir

                # Now add the inotify watches
                session_monitor._add_file_watch(tmux_name, str(new_jsonl))
                session_monitor._add_dir_watch(tmux_name, str(projects_dir))

                # Broadcast registry so clients see resolved=true
                await session_monitor._broadcast_registry()
                return
        logger.warning("JSONL watcher timed out after %.0fs  tmux=%s", timeout, tmux_name)


def _tmux_session_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0


_DASHBOARD_PREFIXES = ("auto-", "host-", "chat-", "chatwith-")


def _list_dashboard_tmux() -> list[str]:
    """List all tmux sessions created by the dashboard.

    Returns [] on subprocess failure (tmux unreachable, socket missing, etc.)
    and logs a warning. Callers that drive mark_dead off this value (notably
    api_terminals) MUST treat [] as ambiguous, not authoritative — a silent
    tmux failure was the mass-session-deactivation root cause on 2026-04-20.
    """
    result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(
            "_list_dashboard_tmux: tmux list-sessions failed  rc=%d  stderr=%r",
            result.returncode, (result.stderr or "").strip()[:200],
        )
        return []
    return [s for s in result.stdout.strip().split("\n")
            if any(s.startswith(p) for p in _DASHBOARD_PREFIXES)]


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
    """WebSocket endpoint that bridges xterm.js to an existing tmux session.

    This endpoint is ATTACH-ONLY — session creation happens via
    POST /api/session/create.  Requests without `?attach=` are rejected.

    The WebSocket just bridges PTY I/O and forwards resize events; the tmux
    session persists across attaches so users can disconnect and reconnect.

    Query params:
      attach — existing tmux session name to attach to (required)
    """
    await websocket.accept()

    params = websocket.query_params
    attach = params.get("attach")
    logger.info("ws_terminal: connect  attach=%s", attach)

    if not attach:
        await websocket.send_text(
            "\r\n\x1b[31mws_terminal requires ?attach=<tmux_name>; "
            "use POST /api/session/create to start a new session\x1b[0m\r\n",
        )
        await websocket.close()
        return

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

    # Ensure session is tracked in DB for sessions not yet registered
    # (e.g. after a server restart where tmux outlived the monitor).
    if not dashboard_db.session_exists(tmux_name):
        info = _detect_terminal_type(tmux_name)
        stype = "container" if info["env"] == "container" else "host"
        try:
            await session_monitor.register(
                tmux_name=tmux_name,
                session_type=stype,
                project="unknown",
            )
        except Exception:
            pass  # already exists

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


async def page_test_input(request):
    """Serve the mobile chat input prototype as a standalone full page.

    Why: safe-area-inset-bottom and visualViewport behaviors cannot be verified
    inside the Design Studio iframe — this route delivers the raw HTML with no
    dashboard chrome so real iOS keyboard/safe-area behavior can be tested.
    """
    from pathlib import Path
    no_cache = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    # Baked reference prototype (graph attachment 976c334a-97e)
    path = Path(__file__).resolve().parent.parent.parent / "data/test-fixtures/input-prototype.html"
    try:
        return HTMLResponse(path.read_text(), headers=no_cache)
    except FileNotFoundError:
        return PlainTextResponse(
            f"Prototype not found at {path}",
            status_code=404,
            headers=no_cache,
        )


_TEST_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Access-Control-Allow-Origin": "*",
}
_test_debug: dict = {}
_test_version = {"v": 1}


async def api_test_debug_post(request):
    global _test_debug
    try:
        _test_debug = await request.json()
    except Exception:
        _test_debug = {}
    return Response(status_code=204, headers=_TEST_NO_CACHE)


async def api_test_debug_get(request):
    return JSONResponse(_test_debug, headers=_TEST_NO_CACHE)


async def api_test_version_get(request):
    return JSONResponse(_test_version, headers=_TEST_NO_CACHE)


async def api_test_version_bump(request):
    _test_version["v"] += 1
    return JSONResponse(_test_version, headers=_TEST_NO_CACHE)


_test_toast = {"msg": ""}


async def api_test_toast_get(request):
    return JSONResponse(_test_toast, headers=_TEST_NO_CACHE)


async def api_test_toast_post(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    _test_toast["msg"] = body.get("msg", "") if isinstance(body, dict) else ""
    return Response(status_code=204, headers=_TEST_NO_CACHE)


# ── Design Studio API ────────────────────────────────────────────

def _resolve_design_id_or_error(raw_id: str):
    """Resolve partial design/revision UUID. Returns (full_id, None) or (None, JSONResponse)."""
    full_id, matches = resolve_design_prefix(raw_id)
    if full_id:
        return full_id, None
    if matches:
        return None, JSONResponse(
            {"error": "ambiguous prefix", "matches": matches}, status_code=400
        )
    return None, JSONResponse({"error": "not found"}, status_code=404)


async def api_design_create(request):
    """Create a new design revision. Returns {id: uuid}."""
    body = await request.json()
    title = body.get("title", "Untitled Design")
    description = body.get("description")
    fixture = body.get("fixture")  # JSON string or dict
    variants = body.get("variants", [])
    # Accept both new and legacy field names
    design_id = body.get("design_id") or body.get("series_id")
    alpine = bool(body.get("alpine"))  # inject Alpine.js runtime in iframe

    if not variants:
        return JSONResponse({"error": "At least one variant required"}, status_code=400)

    # Ensure fixture is stored as JSON string
    if fixture and not isinstance(fixture, str):
        fixture = json.dumps(fixture)

    rev_id = await asyncio.to_thread(
        create_design,
        title=title,
        description=description,
        fixture=fixture,
        variants=variants,
        design_id=design_id,
        alpine=alpine,
    )

    # Broadcast to SSE so gallery pages auto-update without refresh
    design_data = await asyncio.to_thread(get_design, rev_id)
    if design_data:
        topic = f"design:{design_data['design_id']}"
        await event_bus.broadcast(topic, {
            "revision_id": rev_id,
            "design_id": design_data["design_id"],
            "revision_seq": design_data["revision_seq"],
        })

    return JSONResponse({"id": rev_id}, status_code=201)


async def api_design_poll(request):
    """Poll design revision status. 202 while pending, 200 with results when completed."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    design = await asyncio.to_thread(get_design, rev_id)
    if not design:
        return JSONResponse({"error": "not found"}, status_code=404)

    if design["status"] == "pending":
        return JSONResponse({"status": "pending", "id": rev_id}, status_code=202)

    # Completed — return results
    results = []
    for v in design["variants"]:
        if v["selected"]:
            results.append({"id": v["id"], "rank": v["rank"]})
    results.sort(key=lambda x: x["rank"] or 999)
    return JSONResponse({
        "status": "completed",
        "id": rev_id,
        "results": results,
    })


async def api_design_get(request):
    """Get full design revision data for gallery rendering."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    design = await asyncio.to_thread(get_design, rev_id)
    if not design:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(design)


async def api_design_submit(request):
    """Submit ranking results."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    body = await request.json()
    selections = body.get("selections", [])
    ok = await asyncio.to_thread(submit_results, rev_id, selections)
    if not ok:
        return JSONResponse({"error": "design not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_design_pending(request):
    """List pending designs (for toast notifications)."""
    pending = await asyncio.to_thread(list_pending_designs)
    return JSONResponse(pending)


async def api_design_dismiss(request):
    """Dismiss a design and all pending revisions."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    ok = await asyncio.to_thread(dismiss_design, rev_id)
    if not ok:
        return JSONResponse({"error": "design not found"}, status_code=404)
    return JSONResponse({"ok": True})



async def api_design_screenshot(request):
    """Save a screenshot blob for a design revision and optionally inject into agent.

    POST /api/design/{id}/screenshot?tmux_session=chatwith-xxx
    Body: raw image bytes (Content-Type: image/png or image/*)
    Returns: {path: "/absolute/path/to/screenshot.png", injected: bool}

    When tmux_session is provided, performs the two-send image injection:
    1. docker cp screenshot into container (so path exists for Claude Code)
    2. Send bare image path as first message (triggers isMeta=True image injection)
    3. 200ms later, send follow-up text so agent knows to act on the image
    """
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return JSONResponse({"error": "content-type must be image/*"}, status_code=400)

    design = await asyncio.to_thread(get_design, rev_id)
    if not design:
        return JSONResponse({"error": "not found"}, status_code=404)

    screenshot_dir = _REPO_ROOT / "data" / "experiments" / rev_id
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / "screenshot.png"

    body = await request.body()
    await asyncio.to_thread(screenshot_path.write_bytes, body)

    abs_path = str(screenshot_path.resolve())

    # Two-send image injection when tmux_session is provided
    tmux_session = request.query_params.get("tmux_session", "").strip()
    injected = False
    if tmux_session and _tmux_session_exists(tmux_session):
        # Copy screenshot into container so Claude Code can read it
        container_image_path = "/tmp/screenshot.png"
        cp_result = subprocess.run(
            ["docker", "cp", abs_path, f"{tmux_session}:{container_image_path}"],
            capture_output=True,
        )
        if cp_result.returncode == 0:
            # First send: bare image path (triggers isMeta=True image injection)
            await tmux_send(tmux_session, container_image_path)
            # Second send: follow-up text so agent sees the image and acts
            await tmux_send(
                tmux_session,
                "Screenshot captured — describe what you see and continue iterating",
            )
            injected = True
            logger.info("[screenshot] Two-send injection complete for %s", tmux_session)
        else:
            logger.warning(
                "[screenshot] docker cp failed for %s: %s",
                tmux_session, cp_result.stderr.decode(errors="replace"),
            )

    return JSONResponse({"path": abs_path, "injected": injected})


async def page_design(request):
    return HTMLResponse(_load_template("base.html"))

async def page_design_fragment(request):
    """Return the Design page as an HTML fragment for SPA injection.

    Rendered via Jinja2 so {% include %} partials work.
    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="designPage()" component.
    The component reads the design ID from window.location.pathname on init.
    """
    return templates.TemplateResponse(request, "pages/design.html")

async def page_experiments_redirect(request):
    """Redirect /experiments/{id} to /design/{id} for backwards compat."""
    exp_id = request.path_params["id"]
    return RedirectResponse(url=f"/design/{exp_id}", status_code=301)


# ── HTML Pages ────────────────────────────────────────────────

def _load_template(name: str) -> str:
    # Render via Jinja so {% include %} partials are expanded; the static
    # version token stays a literal marker Jinja leaves untouched.
    content = templates.env.get_template(name).render()
    return content.replace("__STATIC_VERSION__", _static_version())


async def api_version(request):
    return JSONResponse({"version": _static_version()})

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
    if os.environ.get("DASHBOARD_MOCK"):
        sessions = dao_sessions.get_active_sessions()
    else:
        # Read directly from session monitor — zero filesystem access
        sessions = session_monitor.get_registry()
    # Active list surfaces interactive sessions only. Dispatch + librarian
    # rows are first-class in the monitor registry for SSE and tail, but
    # filtered out here (auto-ylj6r Phase 5).
    from tools.dashboard.dao.sessions import _ACTIVE_SESSION_TYPES
    sessions = [s for s in sessions if s.get("type") in _ACTIVE_SESSION_TYPES]
    return JSONResponse(sessions)

_recent_sessions_limit_deprecated_logged = False


async def api_dao_recent_sessions(request):
    global _recent_sessions_limit_deprecated_logged
    if "limit" in request.query_params and not _recent_sessions_limit_deprecated_logged:
        logger.warning(
            "/api/dao/recent_sessions: 'limit' query param is deprecated and ignored; "
            "row count is now controlled by server-side per-type quotas"
        )
        _recent_sessions_limit_deprecated_logged = True
    sort = request.query_params.get("sort", "lastActivity")
    since = request.query_params.get("since", "1d")
    type_group = request.query_params.get("type", "all")
    sessions = await asyncio.to_thread(
        dao_sessions.get_recent_sessions, None, sort, since, type_group
    )
    return JSONResponse(sessions)

async def page_search(request):
    """Serve the search results page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_search_fragment(request):
    """Return the search results page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/search.html")

async def page_streams(request):
    """Serve the streams landing page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_streams_fragment(request):
    """Return the streams landing page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/streams.html")

async def page_collab(request):
    """Serve the collab hub page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_collab_fragment(request):
    """Return the collab hub page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/collab.html")

async def page_stream(request):
    """Serve the stream page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_stream_fragment(request):
    """Return the stream page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/stream.html")

async def page_source(request):
    return HTMLResponse(_load_template("base.html"))

async def page_source_redirect(request):
    """301 redirect /source/{id} → /graph/{id}, preserving query params."""
    id = request.path_params["id"]
    qs = str(request.query_params)
    target = f"/graph/{id}" + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=301)

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
                topic, data, seq = await queue.get()
                yield {"id": f"{seq}:{_SERVER_EPOCH}", "event": topic, "data": json.dumps(data)}
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())


async def api_events_replay(request):
    """Return missed events from the ring buffer for gap replay.

    GET /api/events/replay?from={seq}&to={seq}
    Returns {events: [...], complete: bool}.
    If complete=false, the buffer doesn't cover the range —
    caller should fall back to full re-fetch from disk.
    """
    from_seq = int(request.query_params.get("from", "0"))
    to_seq = int(request.query_params.get("to", "0"))
    if from_seq <= 0 or to_seq <= 0 or from_seq > to_seq:
        return JSONResponse({"error": "Invalid range"}, status_code=400)
    events, complete = event_bus.replay(from_seq, to_seq)
    status_code = 200 if complete else 206
    return JSONResponse({"events": events, "complete": complete}, status_code=status_code)


# ── Background watchers ───────────────────────────────────────

_DISPATCH_WATCHER_INTERVAL = 5   # seconds between dispatch polls

_WATCHER_HELPERS = [
    "collect_dispatch_data", "get_bead_counts", "count_active_sessions",
    "count_terminals", "count_today_done", "get_dispatcher_state", "get_pinned_beads",
    "count_streams",
]
_watcher_errors: dict[str, str] = {}  # helper_name -> last error string


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
        librarian_type = run.get("librarian_type") or None
        meta = bead_meta.get(bead_id, {})
        container = None
        if run.get("container_name"):
            container = {
                "name": run["container_name"],
                "image": run.get("image"),
                "status": None,
            }
        # Librarian runs: use dir name as id, synthetic title, no priority
        effective_id = bead_id or run.get("id", "")
        if librarian_type:
            effective_title = f"Librarian: {librarian_type}"
        else:
            effective_title = meta.get("title") or bead_id
        active.append({
            "id": effective_id,
            "title": effective_title,
            "priority": meta.get("priority") if not librarian_type else None,
            "labels": meta.get("labels", []),
            "librarian_type": librarian_type,
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
            "last_activity": (
                datetime.fromisoformat(run["last_activity"]).replace(tzinfo=timezone.utc).timestamp()
                if run.get("last_activity") else None
            ),
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
        "pause_reasons": _get_pause_reasons(),
    }


def _count_active_sessions() -> int:
    """Count active Claude Code sessions from the session monitor."""
    return session_monitor.count()


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


def _count_streams() -> int:
    """Count distinct tags across all notes (active stream count)."""
    return graph_ops.count_active_streams()


async def _dispatch_watcher():
    """Background task: poll dispatch state and broadcast to SSE topics.

    Uses return_exceptions=True so one failing helper doesn't kill the rest.
    Logs errors on state change only (first failure / recovery).
    """
    while True:
        try:
            results = await asyncio.gather(
                _collect_dispatch_data(),
                asyncio.to_thread(dao_beads.get_bead_counts),
                asyncio.to_thread(_count_active_sessions),
                asyncio.to_thread(_count_terminals),
                asyncio.to_thread(_count_today_done),
                asyncio.to_thread(_get_dispatcher_state),
                asyncio.to_thread(dao_beads.get_beads_by_label, "pinned"),
                asyncio.to_thread(_count_streams),
                return_exceptions=True,
            )

            # Log per-helper errors on state change (avoid spam)
            for name, result in zip(_WATCHER_HELPERS, results):
                if isinstance(result, BaseException):
                    err_str = f"{type(result).__name__}: {result}"
                    if _watcher_errors.get(name) != err_str:
                        logger.error("[dispatch_watcher] %s failed: %s", name, err_str)
                        _watcher_errors[name] = err_str
                else:
                    if name in _watcher_errors:
                        logger.info("[dispatch_watcher] %s recovered", name)
                        del _watcher_errors[name]

            # Unpack with safe defaults for failed helpers
            dispatch_data = results[0] if not isinstance(results[0], BaseException) else {"active": [], "waiting": [], "blocked": [], "paused": {}}
            counts = results[1] if not isinstance(results[1], BaseException) else {}
            active_sessions = results[2] if not isinstance(results[2], BaseException) else 0
            terminal_count = results[3] if not isinstance(results[3], BaseException) else 0
            today_done = results[4] if not isinstance(results[4], BaseException) else 0
            dispatcher_state = results[5] if not isinstance(results[5], BaseException) else {"paused": False, "reason": None}
            pinned_beads = results[6] if not isinstance(results[6], BaseException) else []
            stream_count = results[7] if not isinstance(results[7], BaseException) else 0

            nav_data = {
                "open_beads": counts.get("open_count", 0),
                "running_agents": len(dispatch_data["active"]),
                "approved_waiting": len(dispatch_data["waiting"]),
                "approved_blocked": len(dispatch_data["blocked"]),
                "active_sessions": active_sessions,
                "terminal_count": terminal_count,
                "today_done": today_done,
                "pinned": pinned_beads,
                "stream_count": stream_count,
            }
            await event_bus.broadcast("dispatch", dispatch_data)
            await event_bus.broadcast("nav", nav_data)
            await event_bus.broadcast("dispatcher_state", dispatcher_state)
        except Exception:
            logger.exception("[dispatch_watcher] unexpected top-level error")
        await asyncio.sleep(_DISPATCH_WATCHER_INTERVAL)


# ── Graph Write API (single-writer proxy) ─────────────────────
# Containers mount graph.db read-only and POST writes here.
# The server shells out to the graph CLI on the host, serialising all writes.

_GRAPH_SOURCE_ID_RE = re.compile(r'^[0-9a-f-]+$', re.IGNORECASE)
_GRAPH_TAGS_RE = re.compile(r'^[a-zA-Z0-9_,:-]+$')
_GRAPH_MAX_CONTENT = 100_000  # 100KB


def _safe_unlink(path: str) -> None:
    """Remove a temp file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _graph_validate_content(body: dict, field: str = "content") -> str | None:
    """Validate and return content field, or return error string."""
    content = body.get(field, "")
    if not content:
        return f"{field} required"
    if len(content) > _GRAPH_MAX_CONTENT:
        return f"{field} exceeds 100KB limit ({len(content)} bytes)"
    return None


def _graph_validate_source_id(value: str) -> str | None:
    """Return error string if source_id is malformed."""
    if not value or not _GRAPH_SOURCE_ID_RE.match(value):
        return f"malformed source_id: {value!r}"
    return None


async def api_graph_note(request):
    """Create a note via graph CLI. Accepts JSON or multipart (when attachments present)."""
    import tempfile

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        content = str(form.get("content", ""))
        if not content:
            return JSONResponse({"error": "content required"}, status_code=400)
        if len(content) > _GRAPH_MAX_CONTENT:
            return JSONResponse({"error": f"content exceeds 100KB limit"}, status_code=400)

        cmd = ["graph", "note", "-c", "-"]
        tags = form.get("tags")
        if tags:
            if not _GRAPH_TAGS_RE.match(str(tags)):
                return JSONResponse({"error": f"invalid tags: {tags!r}"}, status_code=400)
            cmd += ["--tags", str(tags)]
        if form.get("project"):
            cmd += ["-p", str(form["project"])]
        if form.get("author"):
            cmd += ["--author", str(form["author"])]

        # Write uploaded files to temp locations and add --attach flags
        tmp_paths = []
        for key, upload in form.multi_items():
            if key != "attachments":
                continue
            file_contents = await upload.read()
            if not file_contents:
                continue
            if len(file_contents) > 50 * 1024 * 1024:
                for p in tmp_paths:
                    _safe_unlink(p)
                return JSONResponse({"error": "attachment too large (max 50MB)"}, status_code=400)
            suffix = ""
            if upload.filename and "." in upload.filename:
                suffix = "." + upload.filename.rsplit(".", 1)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(file_contents)
                tmp_paths.append(tmp.name)
            cmd += ["--attach", tmp_paths[-1]]

        stdout, stderr, rc = await run_cli(cmd, timeout=60, stdin_data=content)

        for p in tmp_paths:
            _safe_unlink(p)

        if rc != 0:
            return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
        _checkpoint_graph()
        return JSONResponse({"ok": True, "output": stdout})
    else:
        body = await request.json()
        err = _graph_validate_content(body)
        if err:
            return JSONResponse({"error": err}, status_code=400)

        content = body["content"]

        cmd = ["graph", "note", "-c", "-"]
        if body.get("tags"):
            tags = body["tags"]
            if not _GRAPH_TAGS_RE.match(tags):
                return JSONResponse({"error": f"invalid tags: {tags!r}"}, status_code=400)
            cmd += ["--tags", tags]
        if body.get("project"):
            cmd += ["-p", body["project"]]
        if body.get("author"):
            cmd += ["--author", body["author"]]

        stdout, stderr, rc = await run_cli(cmd, timeout=30, stdin_data=content)

        if rc != 0:
            return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
        _checkpoint_graph()
        return JSONResponse({"ok": True, "output": stdout})


async def api_graph_note_update(request):
    """Update a note via graph CLI. Accepts JSON or multipart (when attachments present)."""
    import tempfile

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        source_id = str(form.get("source_id", ""))
        err = _graph_validate_source_id(source_id)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        content = str(form.get("content", ""))
        if not content:
            return JSONResponse({"error": "content required"}, status_code=400)
        if len(content) > _GRAPH_MAX_CONTENT:
            return JSONResponse({"error": "content exceeds 100KB limit"}, status_code=400)

        cmd = ["graph", "note", "update", source_id, "-c", "-"]
        integrate_raw = form.get("integrate_ids")
        if integrate_raw:
            try:
                ids = json.loads(str(integrate_raw))
            except (json.JSONDecodeError, TypeError):
                ids = []
            for cid in ids:
                cmd += ["--integrate", str(cid)]

        tmp_paths = []
        for key, upload in form.multi_items():
            if key != "attachments":
                continue
            file_contents = await upload.read()
            if not file_contents:
                continue
            if len(file_contents) > 50 * 1024 * 1024:
                for p in tmp_paths:
                    _safe_unlink(p)
                return JSONResponse({"error": "attachment too large (max 50MB)"}, status_code=400)
            suffix = ""
            if upload.filename and "." in upload.filename:
                suffix = "." + upload.filename.rsplit(".", 1)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(file_contents)
                tmp_paths.append(tmp.name)
            cmd += ["--attach", tmp_paths[-1]]

        stdout, stderr, rc = await run_cli(cmd, timeout=60, stdin_data=content)

        for p in tmp_paths:
            _safe_unlink(p)

        if rc != 0:
            return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
        _checkpoint_graph()
        return JSONResponse({"ok": True, "output": stdout})
    else:
        body = await request.json()

        source_id = body.get("source_id", "")
        err = _graph_validate_source_id(source_id)
        if err:
            return JSONResponse({"error": err}, status_code=400)

        err = _graph_validate_content(body)
        if err:
            return JSONResponse({"error": err}, status_code=400)

        content = body["content"]

        cmd = ["graph", "note", "update", source_id, "-c", "-"]
        for cid in body.get("integrate_ids", []):
            cmd += ["--integrate", cid]

        stdout, stderr, rc = await run_cli(cmd, timeout=30, stdin_data=content)

        if rc != 0:
            return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
        _checkpoint_graph()
        return JSONResponse({"ok": True, "output": stdout})


async def api_graph_comment(request):
    """Add a comment to a note via graph CLI."""
    body = await request.json()

    source_id = body.get("source_id", "")
    err = _graph_validate_source_id(source_id)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    err = _graph_validate_content(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    content = body["content"]

    cmd = ["graph", "comment", source_id, "-c", "-"]
    if body.get("actor"):
        cmd += ["--actor", body["actor"]]

    stdout, stderr, rc = await run_cli(cmd, timeout=30, stdin_data=content)

    if rc != 0:
        return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": stdout})


async def api_graph_comment_integrate(request):
    """Mark a comment as integrated via graph CLI."""
    body = await request.json()

    comment_id = body.get("comment_id", "")
    err = _graph_validate_source_id(comment_id)
    if err:
        return JSONResponse({"error": f"malformed comment_id: {comment_id!r}"}, status_code=400)

    stdout, stderr, rc = await run_cli(
        ["graph", "comment", "integrate", comment_id], timeout=15
    )

    if rc != 0:
        return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
    return JSONResponse({"ok": True, "output": stdout})


async def api_graph_bead(request):
    """Create a bead with provenance via graph CLI."""
    body = await request.json()

    title = body.get("title", "")
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    if len(title) > 200:
        return JSONResponse({"error": "title too long (max 200 chars)"}, status_code=400)

    priority = body.get("priority", 1)
    if not isinstance(priority, int) or priority < 0 or priority > 3:
        return JSONResponse({"error": "priority must be integer 0-3"}, status_code=400)

    cmd = ["graph", "bead", title, "-p", str(priority)]

    desc = body.get("description", "")
    stdin_data = None
    if desc:
        if len(desc) > _GRAPH_MAX_CONTENT:
            return JSONResponse({"error": "description exceeds 100KB"}, status_code=400)
        cmd += ["-d", "-"]
        stdin_data = desc

    if body.get("type"):
        cmd += ["-t", body["type"]]
    if body.get("source"):
        source_id = body["source"]
        err = _graph_validate_source_id(source_id)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        cmd += ["--source", source_id]
    if body.get("turns"):
        cmd += ["--turns", body["turns"]]
    if body.get("note"):
        cmd += ["--note", body["note"]]

    stdout, stderr, rc = await run_cli(cmd, timeout=60, stdin_data=stdin_data)

    if rc != 0:
        return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
    return JSONResponse({"ok": True, "output": stdout})


async def api_graph_link(request):
    """Create a provenance edge via graph CLI."""
    body = await request.json()

    bead_id = body.get("bead_id", "")
    if not bead_id:
        return JSONResponse({"error": "bead_id required"}, status_code=400)

    source_id = body.get("source_id", "")
    err = _graph_validate_source_id(source_id)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    relationship = body.get("relationship", "informed_by")

    cmd = ["graph", "link", bead_id, source_id, "-r", relationship]
    if body.get("turn"):
        cmd += ["-t", body["turn"]]
    if body.get("note"):
        cmd += ["--note", body["note"]]

    stdout, stderr, rc = await run_cli(cmd, timeout=30)

    if rc != 0:
        return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
    return JSONResponse({"ok": True, "output": stdout})


_ingest_lock = asyncio.Lock()


async def api_graph_sessions(request):
    """Ingest sessions via graph CLI."""
    if _ingest_lock.locked():
        return JSONResponse({"ok": True, "output": "ingest already in progress", "skipped": True})

    async with _ingest_lock:
        body = await request.json()

        cmd = ["graph", "sessions"]
        if body.get("all"):
            cmd += ["--all"]
        if body.get("project"):
            cmd += ["--project", body["project"]]
        if body.get("force"):
            cmd += ["--force"]

        stdout, stderr, rc = await run_cli(cmd, timeout=120)

        if rc != 0:
            return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
        return JSONResponse({"ok": True, "output": stdout})


async def api_graph_attach(request):
    """Attach a file to the graph via multipart form upload."""
    import tempfile
    form = await request.form()
    upload = form.get("file")
    if not upload:
        return JSONResponse({"error": "file field required"}, status_code=400)

    # Write uploaded file to a temp location
    contents = await upload.read()
    if not contents:
        return JSONResponse({"error": "empty file"}, status_code=400)
    if len(contents) > 50 * 1024 * 1024:  # 50MB limit
        return JSONResponse({"error": "file too large (max 50MB)"}, status_code=400)

    suffix = ""
    if upload.filename and "." in upload.filename:
        suffix = "." + upload.filename.rsplit(".", 1)[1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    cmd = ["graph", "attach", tmp_path]

    source_id = form.get("source_id")
    if source_id:
        if not _GRAPH_SOURCE_ID_RE.match(str(source_id)):
            import os
            os.unlink(tmp_path)
            return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
        cmd += ["--source", str(source_id)]

    turn = form.get("turn")
    if turn is not None:
        cmd += ["--turn", str(turn)]

    stdout, stderr, rc = await run_cli(cmd, timeout=60)

    # Clean up temp file
    import os
    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    if rc != 0:
        return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": stdout})


# ── Attachment serving ────────────────────────────────────────

async def api_source_attachments(request):
    """List attachments linked to a source."""
    source_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    atts = graph_ops.list_attachments(source_id=source_id)
    return JSONResponse({"attachments": atts})


async def api_attachment_serve(request):
    """Serve an attachment file by ID with correct Content-Type."""
    attachment_id = request.path_params["attachment_id"]

    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as mock_dao
        att = mock_dao.get_attachment(attachment_id)
        if not att:
            return JSONResponse({"error": "attachment not found"}, status_code=404)
        content = att.get("content")
        if content:
            if isinstance(content, str):
                content = content.encode()
            return Response(content, media_type=att.get("mime_type", "application/octet-stream"))
        mime = att.get("mime_type", "")
        if mime.startswith("image/"):
            import base64
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            )
            return Response(png, media_type="image/png")
        if mime == "text/html":
            return Response(b"<html><body>Mock HTML attachment</body></html>", media_type="text/html")
        return Response(b"mock content", media_type=mime or "application/octet-stream")

    att = graph_ops.get_attachment(attachment_id)
    if not att:
        return JSONResponse({"error": "attachment not found"}, status_code=404)
    file_path = Path(att["file_path"])
    if not file_path.is_absolute():
        file_path = _REPO_ROOT / file_path
    if not file_path.exists():
        return JSONResponse({"error": "file missing"}, status_code=404)
    return FileResponse(
        file_path,
        media_type=att.get("mime_type") or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


async def api_graph_search(request):
    """Container-side CLI calls this to get WAL-fresh, cross-org-ready results.

    Mirrors what ``graph search`` would return on the host: a JSON list of
    result rows from ``ops.search``. The dashboard's own search UI uses the
    older ``/api/search`` endpoint (which shells out to ``graph search``);
    this endpoint is the structured companion meant for ``HttpClient.search``.
    """
    q = request.query_params.get("q", "")
    if not q:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    limit = int(request.query_params.get("limit", "25"))
    project = request.query_params.get("project")
    or_mode = bool(request.query_params.get("or"))
    tag = request.query_params.get("tag")
    states_param = request.query_params.get("states")
    states = [s for s in states_param.split(",") if s] if states_param else None
    include_raw = bool(request.query_params.get("include_raw"))
    ssi_param = request.query_params.get("session_source_ids")
    session_source_ids = [s for s in ssi_param.split(",") if s] if ssi_param else None
    session_author_pattern = request.query_params.get("session_author_pattern")
    results = graph_ops.search(
        q, limit=limit, project=project, or_mode=or_mode, tag=tag,
        states=states, include_raw=include_raw,
        session_source_ids=session_source_ids,
        session_author_pattern=session_author_pattern,
    )
    return JSONResponse(results)


async def api_graph_source_get(request):
    """Get a single source by id (or id prefix). Companion to HttpClient.get_source."""
    source_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    src = graph_ops.get_source(source_id)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(src)


async def api_graph_sources_list(request):
    """List sources with filters. Companion to HttpClient.list_sources."""
    limit = int(request.query_params.get("limit", "50"))
    project = request.query_params.get("project")
    source_type = request.query_params.get("type")
    tags_param = request.query_params.get("tags")
    tags = [t for t in tags_param.split(",") if t] if tags_param else None
    sources = graph_ops.list_sources(
        limit=limit, project=project, source_type=source_type, tags=tags,
    )
    return JSONResponse({"sources": sources})


async def api_graph_attachment_get(request):
    """Get attachment metadata by id. Companion to HttpClient.get_attachment."""
    attachment_id = request.path_params["attachment_id"]
    if not _GRAPH_SOURCE_ID_RE.match(attachment_id):
        return JSONResponse({"error": f"malformed attachment_id: {attachment_id!r}"}, status_code=400)
    att = graph_ops.get_attachment(attachment_id)
    if not att:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(att)


async def api_graph_collab_topics(request):
    """List tag taxonomy entries. Companion to HttpClient.list_collab_topics."""
    return JSONResponse({"topics": graph_ops.list_collab_topics()})


# ── Settings primitive (graph://0d3f750f-f9c) ──────────────


def _parse_settings_read_params(query_params) -> tuple[int | None, int | None, int | None, str | None]:
    """Pull target/min/stored revision + error str from query params."""
    def _to_int(name):
        v = query_params.get(name)
        if v is None:
            return None
        try:
            return int(v)
        except ValueError:
            return f"invalid {name}: {v!r}"
    for name in ("target_revision", "min_revision", "stored_revision"):
        v = _to_int(name)
        if isinstance(v, str):
            return None, None, None, v
    return (
        int(query_params["target_revision"]) if "target_revision" in query_params else None,
        int(query_params["min_revision"]) if "min_revision" in query_params else None,
        int(query_params["stored_revision"]) if "stored_revision" in query_params else None,
        None,
    )


async def api_graph_settings_list(request):
    """GET /api/graph/settings/<set_id> — resolved members of a SET."""
    set_id = request.path_params["set_id"]
    target, minrev, stored, err = _parse_settings_read_params(request.query_params)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    members = graph_ops.read_set(
        set_id, target_revision=target, min_revision=minrev,
    )
    out = members.to_dict()
    if stored is not None:
        out["members"] = [m for m in out["members"] if m["stored_revision"] == stored]
    return JSONResponse(out)


async def api_graph_settings_get_by_key(request):
    """GET /api/graph/settings/<set_id>/<key> — single resolved member by key."""
    set_id = request.path_params["set_id"]
    key = request.path_params["key"]
    target, minrev, stored, err = _parse_settings_read_params(request.query_params)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    members = graph_ops.read_set(
        set_id, target_revision=target, min_revision=minrev,
    )
    for m in members.members:
        if m.key == key:
            return JSONResponse(m.to_dict())
    return JSONResponse({"error": "not found"}, status_code=404)


async def api_graph_setting_get(request):
    """GET /api/graph/setting/<id> — resolve a single Setting by id."""
    sid = request.path_params["id"]
    target, _, _, err = _parse_settings_read_params(request.query_params)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    got = graph_ops.get_setting(sid, target_revision=target)
    if got is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(got.to_dict())


async def api_graph_setting_create(request):
    """POST /api/graph/setting — body: {set_id, schema_revision, key, payload, state}."""
    from tools.graph.schemas.registry import SchemaValidationError
    body = await request.json()
    required = ("set_id", "schema_revision", "key", "payload")
    missing = [k for k in required if k not in body]
    if missing:
        return JSONResponse(
            {"error": f"missing fields: {missing}"}, status_code=400,
        )
    try:
        sid = graph_ops.add_setting(
            body["set_id"],
            int(body["schema_revision"]),
            body["key"],
            body["payload"],
            state=body.get("state", "raw"),
        )
    except SchemaValidationError as e:
        return JSONResponse(
            {"error": "schema validation failed", "detail": str(e)},
            status_code=400,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": sid}, status_code=201)


async def api_graph_setting_override(request):
    """POST /api/graph/setting/<id>/override — body: {payload, state}."""
    from tools.graph.schemas.registry import SchemaValidationError
    target_id = request.path_params["id"]
    body = await request.json()
    if "payload" not in body:
        return JSONResponse({"error": "payload required"}, status_code=400)
    try:
        sid = graph_ops.override_setting(
            target_id, body["payload"], state=body.get("state", "raw"),
        )
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except SchemaValidationError as e:
        return JSONResponse(
            {"error": "schema validation failed", "detail": str(e)},
            status_code=400,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": sid}, status_code=201)


async def api_graph_setting_exclude(request):
    """POST /api/graph/setting/<id>/exclude — body: {state}."""
    target_id = request.path_params["id"]
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        sid = graph_ops.exclude_setting(
            target_id, state=body.get("state", "raw"),
        )
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": sid}, status_code=201)


async def api_graph_setting_promote(request):
    """POST /api/graph/setting/<id>/promote — body: {to_state}."""
    sid = request.path_params["id"]
    body = await request.json()
    to_state = body.get("to_state") or body.get("to")
    if not to_state:
        return JSONResponse({"error": "to_state required"}, status_code=400)
    try:
        graph_ops.promote_setting(sid, to_state)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


async def api_graph_setting_deprecate(request):
    """POST /api/graph/setting/<id>/deprecate — body: {successor_id?}."""
    sid = request.path_params["id"]
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        graph_ops.deprecate_setting(sid, successor_id=body.get("successor_id"))
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({"ok": True})


async def api_graph_setting_delete(request):
    """DELETE /api/graph/setting/<id> — hard-delete (raw only)."""
    sid = request.path_params["id"]
    try:
        graph_ops.remove_setting(sid)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


async def api_graph_set_ids(request):
    """GET /api/graph/sets — list known set_ids."""
    return JSONResponse({"set_ids": graph_ops.list_set_ids()})


# ── Org registry (graph://d970d946-f95) ──────────────────────


def _enrich_org_identity(detail: dict) -> dict:
    """Resolve identity through the override→canonical→generated cascade.

    Mutates ``detail`` in place to add ``identity_resolved`` carrying the
    final display values; preserves the raw bootstrap row + the raw
    canonical Setting under the original keys.
    """
    from tools.dashboard.org_identity import resolve_org_identity
    org = detail.get("org") or {}
    slug = org.get("slug")
    detail["identity_resolved"] = resolve_org_identity(slug)
    return detail


async def api_orgs_list(request):
    """GET /api/orgs — enumerate orgs with bootstrap row + cascade identity."""
    from tools.graph import org_ops
    orgs = org_ops.list_orgs()
    entries = []
    for ref in orgs:
        detail = org_ops.show_org(ref.slug)
        if detail is None:
            continue
        entries.append(_enrich_org_identity(detail))
    return JSONResponse({"orgs": entries})


async def api_orgs_show(request):
    """GET /api/orgs/<slug> — bootstrap row + autonomy.org#1 Setting."""
    from tools.graph import org_ops
    slug = request.path_params["slug"]
    detail = org_ops.show_org(slug)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_enrich_org_identity(detail))


async def api_orgs_create(request):
    """POST /api/orgs — body: {slug, type?, identity?}."""
    from tools.graph import org_ops
    body = await request.json()
    slug = body.get("slug")
    if not slug:
        return JSONResponse({"error": "slug required"}, status_code=400)
    type_ = body.get("type", "shared")
    identity_payload = body.get("identity")
    try:
        ref = org_ops.create_org(
            slug, type_=type_, identity_payload=identity_payload,
        )
    except org_ops.OrgExistsError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except org_ops.OrgError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(ref.to_dict(), status_code=201)


async def api_orgs_delete(request):
    """DELETE /api/orgs/<slug>?force=1 — refuses on cross-DB references."""
    from tools.graph import org_ops
    slug = request.path_params["slug"]
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    try:
        report = org_ops.remove_org(slug, force=force)
    except org_ops.OrgNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except org_ops.OrgReferencedError as e:
        return JSONResponse(
            {
                "error": str(e),
                "references": [r.to_dict() for r in e.references],
            },
            status_code=409,
        )
    except org_ops.OrgError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(report.to_dict())


async def api_graph_resolve(request):
    """Universal graph entity resolver — sources, attachments, partial ID prefix."""
    id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(id):
        return JSONResponse({"error": f"malformed id: {id!r}"}, status_code=400)

    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as mock_dao
        result = mock_dao.resolve_source_for_api(id)
        if result:
            _attach_source_org(result)
            return JSONResponse(result)
        att = mock_dao.get_attachment(id)
        if att:
            return JSONResponse({
                "type": "attachment",
                "id": att["id"],
                "filename": att.get("filename", ""),
                "mime_type": att.get("mime_type", ""),
                "size_bytes": att.get("size_bytes", 0),
                "source_id": att.get("source_id", ""),
                "turn": att.get("turn"),
                "created_at": att.get("created_at", ""),
                "url": f"/api/attachment/{att['id'][:12]}",
            })
        return JSONResponse({"error": "not found"}, status_code=404)

    source = graph_ops.get_source(id)
    if source:
        result = await run_cli_json(
            ["graph", "read", source["id"], "--json", "--max-chars", "50000", "--first"]
        )
        _attach_source_org(result)
        return JSONResponse(result)
    att = graph_ops.get_attachment(id)
    if att:
        return JSONResponse({
            "type": "attachment",
            "id": att["id"],
            "filename": att["filename"],
            "mime_type": att["mime_type"],
            "size_bytes": att["size_bytes"],
            "source_id": att["source_id"],
            "turn": att.get("turn"),
            "created_at": att["created_at"],
            "url": f"/api/attachment/{att['id'][:12]}",
        })
    comment = graph_ops.get_comment(id)
    if comment:
        return JSONResponse({
            "type": "comment",
            "id": comment["id"],
            "source_id": comment["source_id"],
            "content": comment["content"],
            "actor": comment.get("actor", "user"),
            "created_at": comment.get("created_at", ""),
            "integrated": bool(comment.get("integrated", 0)),
            "redirect": f"/graph/{comment['source_id'][:12]}?highlight={comment['id'][:12]}",
        })
    return JSONResponse({"error": "not found"}, status_code=404)


async def api_resolve_embed(request):
    """Resolve a ![[id]] embed reference for the dashboard renderer.

    Returns type, attachment URL, alt-text, and mime type for rendering iframes/images/toggles.
    """
    embed_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(embed_id):
        return JSONResponse({"error": f"malformed id: {embed_id!r}"}, status_code=400)

    version = request.query_params.get("version")

    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as mock_dao
        result = mock_dao.resolve_embed(embed_id, version)
        if result:
            return JSONResponse(result)
        return JSONResponse({"error": "not found"}, status_code=404)

    embed = graph_ops.resolve_embed(embed_id, version=version)
    if embed:
        return JSONResponse(embed)
    return JSONResponse({"error": "not found"}, status_code=404)


def _graph_db_path() -> str | None:
    """Resolve graph DB path: GRAPH_DB env var, then default."""
    import os
    return os.environ.get("GRAPH_DB") or None


def _checkpoint_graph():
    """Flush WAL to main DB so immutable=1 readers see current data."""
    graph_ops.checkpoint()


async def api_graph_stream(request):
    """List notes matching a tag as a chronological feed."""
    if os.environ.get("DASHBOARD_MOCK"):
        tag = request.path_params["tag"]
        limit = int(request.query_params.get("limit", "50"))
        items = dao_beads.get_stream_items(tag, limit)
        return JSONResponse({"tag": tag, "count": len(items), "items": items})

    tag = request.path_params["tag"]
    limit = int(request.query_params.get("limit", "50"))
    offset = int(request.query_params.get("offset", "0"))
    items = graph_ops.stream_get(tag, limit=limit, offset=offset)
    return JSONResponse({"tag": tag, "count": len(items), "items": items})


async def api_graph_streams(request):
    """List active tag streams with note counts, descriptions, and last_active."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"streams": dao_beads.get_streams()})
    return JSONResponse({"streams": graph_ops.streams_summary()})


async def api_graph_collab_list(request):
    """List collab-tagged notes as structured JSON."""
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "20"))
        return JSONResponse({"notes": dao_beads.get_collab_notes(limit)})

    limit = int(request.query_params.get("limit", "20"))
    sources = graph_ops.list_collab_sources(limit=limit)
    items = []
    for s in sources:
        meta = json.loads(s["metadata"]) if isinstance(s.get("metadata"), str) else (s.get("metadata") or {})
        items.append({
            "id": s["id"],
            "title": s.get("title", ""),
            "created_at": meta.get("created_at", s.get("created_at", "")),
            "author": meta.get("author", ""),
            "project": s.get("project", ""),
            "tags": meta.get("tags", []),
            "comment_count": s.get("comment_count", 0),
            "version": meta.get("version", 1),
        })
    return JSONResponse({"notes": items})


async def api_graph_collab_tag(request):
    """Add the 'collab' tag to an existing source."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag operation skipped"})
    source_id = request.path_params["source_id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    source = graph_ops.get_source(source_id)
    if not source:
        return JSONResponse({"error": f"no source found matching '{source_id}'"}, status_code=404)
    if isinstance(source, list):
        return JSONResponse({"error": f"multiple sources match '{source_id}' — use a longer prefix"}, status_code=400)
    added = graph_ops.add_tag(source["id"], "collab")
    title = (source.get("title") or "?")[:60]
    if added:
        msg = f"  \u2713 Tagged {source['id'][:12]} \"{title}\" as collab"
    else:
        msg = f"  Already tagged: {source['id'][:12]} \"{title}\""
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg})


async def api_graph_tag_add(request):
    """Add a tag to a source."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag add operation skipped"})
    source_id = request.path_params["source_id"]
    tag_name = request.path_params["tag_name"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    if not _GRAPH_TAGS_RE.match(tag_name):
        return JSONResponse({"error": f"malformed tag name: {tag_name!r}"}, status_code=400)
    source = graph_ops.get_source(source_id)
    if not source:
        return JSONResponse({"error": f"no source found matching '{source_id}'"}, status_code=404)
    if isinstance(source, list):
        return JSONResponse({"error": f"multiple sources match '{source_id}' — use a longer prefix"}, status_code=400)
    added = graph_ops.add_tag(source["id"], tag_name)
    title = (source.get("title") or "?")[:60]
    if added:
        msg = f"  ✓ Tagged {source['id'][:12]} \"{title}\" ← {tag_name}"
    else:
        msg = f"  Already tagged: {source['id'][:12]} \"{title}\" ← {tag_name}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg})


async def api_graph_tag_remove(request):
    """Remove a tag from a source."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag remove operation skipped"})
    source_id = request.path_params["source_id"]
    tag_name = request.path_params["tag_name"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    if not _GRAPH_TAGS_RE.match(tag_name):
        return JSONResponse({"error": f"malformed tag name: {tag_name!r}"}, status_code=400)
    source = graph_ops.get_source(source_id)
    if not source:
        return JSONResponse({"error": f"no source found matching '{source_id}'"}, status_code=404)
    if isinstance(source, list):
        return JSONResponse({"error": f"multiple sources match '{source_id}' — use a longer prefix"}, status_code=400)
    removed = graph_ops.remove_tag(source["id"], tag_name)
    title = (source.get("title") or "?")[:60]
    if removed:
        msg = f"  ✓ Untagged {source['id'][:12]} \"{title}\" ✗ {tag_name}"
    else:
        msg = f"  Not tagged: {source['id'][:12]} \"{title}\" ✗ {tag_name}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg})


async def api_graph_tag_merge(request):
    """Merge one tag into another."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag merge operation skipped"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    from_tag = (body.get("from") or "").strip()
    to_tag = (body.get("to") or "").strip()
    if not from_tag or not to_tag:
        return JSONResponse({"error": "'from' and 'to' are required"}, status_code=400)
    reason = (body.get("reason") or "").strip()
    force = body.get("force", False)
    result = graph_ops.tag_merge(from_tag, to_tag, reason=reason, force=force)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=result.get("status", 400))
    retagged = result["count"]
    note_id = result["note_id"]
    msg = (f"  ✓ Merged '{from_tag}' → '{to_tag}' ({retagged} sources retagged)\n"
           f"  Provenance: graph://{note_id[:12]}")
    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "output": msg,
        "count": retagged,
        "note_id": note_id,
    })


async def api_graph_thought(request):
    """Create a thought capture via API proxy."""
    from tools.graph.models import new_id
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    content = (body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)
    if len(content) > _GRAPH_MAX_CONTENT:
        return JSONResponse({"error": f"content exceeds {_GRAPH_MAX_CONTENT} bytes"}, status_code=400)
    thread_id = body.get("thread_id")
    source_id = body.get("source_id")
    turn_number = body.get("turn_number")
    actor = body.get("actor", "user")
    if source_id and not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    if thread_id and not _GRAPH_SOURCE_ID_RE.match(thread_id):
        return JSONResponse({"error": f"malformed thread_id: {thread_id!r}"}, status_code=400)
    if thread_id:
        thread = graph_ops.get_thread(thread_id)
        if not thread:
            return JSONResponse({"error": f"thread not found: {thread_id}"}, status_code=404)
        thread_id = thread["id"]
    capture_id = new_id()
    graph_ops.insert_capture(
        capture_id, content,
        source_id=source_id,
        turn_number=int(turn_number) if turn_number else None,
        thread_id=thread_id,
        actor=actor,
    )
    msg = f"  \u2713 Captured: {capture_id[:11]}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg, "id": capture_id})


async def api_graph_thread(request):
    """Create a thread via API proxy."""
    from tools.graph.models import new_id
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    if len(title) > 500:
        return JSONResponse({"error": "title too long (max 500)"}, status_code=400)
    priority = int(body.get("priority", 1))
    actor = body.get("actor", "user")
    thread_id = new_id()
    graph_ops.insert_thread(thread_id, title, priority=priority, created_by=actor)
    msg = f"  \u2713 Thread: {thread_id[:11]} \"{title}\" [active, P{priority}]"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg, "id": thread_id})


async def api_graph_thread_action(request):
    """Thread actions (park/done/active/assign/attach) via API proxy."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    action = body.get("action", "")
    thread_id = body.get("thread_id", "")
    target = body.get("target")

    if action not in ("park", "done", "active", "assign", "attach"):
        return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

    if action in ("park", "done", "active"):
        thread = graph_ops.update_thread_status(
            thread_id, "parked" if action == "park" else action,
        )
        title = thread["title"] if thread else thread_id
        return JSONResponse({"ok": True, "output": f"  \u2713 {action.capitalize()}: {thread_id} \"{title}\"\n"})
    if not target:
        return JSONResponse({"error": "assign requires target thread_id"}, status_code=400)
    graph_ops.assign_capture_to_thread(thread_id, target)
    return JSONResponse({"ok": True, "output": f"  \u2713 Assigned {thread_id} \u2192 thread {target}\n"})


async def api_graph_thoughts(request):
    """List thought captures as structured JSON."""
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "50"))
        thread_id = request.query_params.get("thread")
        since_param = request.query_params.get("since")
        return JSONResponse({"thoughts": dao_beads.get_thoughts(limit, thread_id, since_param)})

    limit = int(request.query_params.get("limit", "50"))
    thread_id = request.query_params.get("thread")
    since_param = request.query_params.get("since")
    since_iso = _parse_range(since_param) if since_param else None
    all_mode = not thread_id and not request.query_params.get("inbox")
    captures = graph_ops.list_captures(
        thread_id=thread_id,
        status="*" if all_mode else None,
        since=since_iso,
        limit=limit,
    )
    items = [
        {
            "id": c["id"],
            "content": c.get("content", ""),
            "status": c.get("status", "captured"),
            "thread_id": c.get("thread_id"),
            "source_id": c.get("source_id"),
            "turn_number": c.get("turn_number"),
            "created_at": c.get("created_at", ""),
        }
        for c in captures
    ]
    return JSONResponse({"thoughts": items})


async def api_graph_threads(request):
    """List threads as structured JSON."""
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "20"))
        status = request.query_params.get("status", "active")
        show_all = request.query_params.get("all")
        return JSONResponse({"threads": dao_beads.get_threads(limit, status=None if show_all else status)})

    limit = int(request.query_params.get("limit", "20"))
    status = request.query_params.get("status", "active")
    show_all = request.query_params.get("all")
    threads = graph_ops.list_threads(status=None if show_all else status, limit=limit)
    items = [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "status": t.get("status", "active"),
            "priority": t.get("priority", 1),
            "capture_count": t.get("capture_count", 0),
            "created_at": t.get("created_at", ""),
            "updated_at": t.get("updated_at", ""),
        }
        for t in threads
    ]
    return JSONResponse({"threads": items})


async def api_journal(request):
    """List journal entries with three zoom levels."""
    limit = int(request.query_params.get("limit", "50"))
    since_param = request.query_params.get("since")
    since_iso = _parse_range(since_param) if since_param else None
    return JSONResponse({"entries": graph_ops.list_journal_entries(since=since_iso, limit=limit)})


async def api_graph_journal_write(request):
    """Write a journal entry via graph CLI proxy."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: journal write skipped"})

    body = await request.json()

    # Validate required fields
    for field in ("compact", "normal", "timestamp_start", "timestamp_end"):
        if field not in body:
            return JSONResponse({"error": f"missing required field: {field}"}, status_code=400)

    # Shell out to graph journal write via CLI
    import json as _json
    stdin_data = _json.dumps(body)
    stdout, stderr, rc = await run_cli(
        ["graph", "journal", "write", "-c", "-"],
        timeout=30,
        stdin_data=stdin_data,
    )

    if rc != 0:
        return JSONResponse({"error": stderr, "rc": rc}, status_code=500)
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": stdout})


async def api_graph_collab_tag_describe(request):
    """Set or update a tag description via API proxy."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag describe operation skipped"})
    tag_name = request.path_params["name"]
    if not _GRAPH_TAGS_RE.match(tag_name):
        return JSONResponse({"error": f"malformed tag name: {tag_name!r}"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    description = (body.get("description") or "").strip()
    if not description:
        return JSONResponse({"error": "description is required"}, status_code=400)
    if len(description) > _GRAPH_MAX_CONTENT:
        return JSONResponse({"error": f"description exceeds {_GRAPH_MAX_CONTENT} bytes"}, status_code=400)
    actor = body.get("actor", "user")
    graph_ops.update_tag_description(tag_name, description, actor=actor)
    msg = f"  \u2713 Tag '{tag_name}': {description[:60]}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg})


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
    Route("/streams", page_streams),
    Route("/pages/streams", page_streams_fragment),
    Route("/collab", page_collab),
    Route("/pages/collab", page_collab_fragment),
    Route("/stream/{tag}", page_stream),
    Route("/pages/stream", page_stream_fragment),
    Route("/graph/{id}", page_source),
    Route("/source/{id}", page_source_redirect),
    Route("/pages/source", page_source_fragment),
    Route("/bead/{id}", page_bead),
    Route("/timeline", page_timeline),
    Route("/terminal", page_terminal),
    Route("/terminal/{session_id}", page_terminal),
    Route("/pages/terminal", page_terminal_fragment),
    Route("/pages/design", page_design_fragment),
    Route("/session/{project}/{session_id}", page_session_view),
    Route("/pages/session-view", page_session_view_fragment),
    Route("/test/input", page_test_input),
    Route("/api/test/debug", api_test_debug_get),
    Route("/api/test/debug", api_test_debug_post, methods=["POST"]),
    Route("/api/test/version", api_test_version_get),
    Route("/api/test/version", api_test_version_bump, methods=["POST"]),
    Route("/api/test/toast", api_test_toast_get),
    Route("/api/test/toast", api_test_toast_post, methods=["POST"]),

    # WebSocket
    WebSocketRoute("/ws/terminal", ws_terminal),

    # Events (SSE)
    Route("/api/events", api_events),
    Route("/api/events/replay", api_events_replay),

    # API
    Route("/api/beads/ready", api_beads_ready),
    Route("/api/beads/list", api_beads_list),
    Route("/api/beads/search", api_beads_search),
    Route("/api/bead/{id}", api_bead_show),
    Route("/api/bead/{id}/tree", api_bead_tree),
    Route("/api/bead/{id}/deps", api_bead_deps),
    Route("/api/bead/{id}/approve", api_bead_approve, methods=["POST"]),
    Route("/api/pinned", api_pinned_beads),
    Route("/api/dispatch/pause", api_dispatch_pause_get),
    Route("/api/dispatch/pause", api_dispatch_pause_post, methods=["POST"]),
    Route("/api/dispatch/resume", api_dispatch_resume, methods=["POST"]),
    Route("/api/dispatch/resume/{bead_id}", api_dispatch_resume_bead, methods=["POST"]),
    Route("/api/dispatch/pause-state", api_dispatch_pause_state),
    Route("/api/dispatch/status", api_dispatch_status),
    Route("/api/dispatch/approved", api_dispatch_approved),
    Route("/api/dispatch/runs", api_dispatch_runs),
    Route("/api/dispatch/trace/{run}", api_dispatch_trace),
    Route("/dispatch/trace/{run}", page_dispatch),
    Route("/api/search", api_search),
    Route("/api/sources", api_sources),
    Route("/api/graph/streams", api_graph_streams, methods=["GET"]),
    Route("/api/graph/stream/{tag}", api_graph_stream, methods=["GET"]),
    Route("/api/graph/collab", api_graph_collab_list, methods=["GET"]),
    Route("/api/graph/collab/tag/{source_id}", api_graph_collab_tag, methods=["PUT"]),
    Route("/api/graph/collab/tag-describe/{name}", api_graph_collab_tag_describe, methods=["PUT"]),
    Route("/api/graph/tag/merge", api_graph_tag_merge, methods=["POST"]),
    Route("/api/graph/tag/{source_id}/{tag_name}", api_graph_tag_add, methods=["PUT"]),
    Route("/api/graph/tag/{source_id}/{tag_name}", api_graph_tag_remove, methods=["DELETE"]),
    Route("/api/journal", api_journal, methods=["GET"]),
    Route("/api/graph/thoughts", api_graph_thoughts, methods=["GET"]),
    Route("/api/graph/threads", api_graph_threads, methods=["GET"]),
    Route("/api/graph/thought", api_graph_thought, methods=["POST"]),
    Route("/api/graph/thread", api_graph_thread, methods=["POST"]),
    Route("/api/graph/thread/action", api_graph_thread_action, methods=["POST"]),
    # Service-layer GET endpoints — used by HttpClient (container CLI).
    Route("/api/graph/search", api_graph_search, methods=["GET"]),
    Route("/api/graph/sources", api_graph_sources_list, methods=["GET"]),
    Route("/api/graph/source/{id}", api_graph_source_get, methods=["GET"]),
    Route("/api/graph/attachment/{attachment_id}", api_graph_attachment_get, methods=["GET"]),
    Route("/api/graph/collab-topics", api_graph_collab_topics, methods=["GET"]),
    # Settings primitive (graph://0d3f750f-f9c). Routes ordered specific → generic.
    Route("/api/graph/sets", api_graph_set_ids, methods=["GET"]),
    Route("/api/graph/settings/{set_id}/{key}", api_graph_settings_get_by_key, methods=["GET"]),
    Route("/api/graph/settings/{set_id}", api_graph_settings_list, methods=["GET"]),
    Route("/api/graph/setting", api_graph_setting_create, methods=["POST"]),
    Route("/api/graph/setting/{id}/override", api_graph_setting_override, methods=["POST"]),
    Route("/api/graph/setting/{id}/exclude", api_graph_setting_exclude, methods=["POST"]),
    Route("/api/graph/setting/{id}/promote", api_graph_setting_promote, methods=["POST"]),
    Route("/api/graph/setting/{id}/deprecate", api_graph_setting_deprecate, methods=["POST"]),
    Route("/api/graph/setting/{id}", api_graph_setting_get, methods=["GET"]),
    Route("/api/graph/setting/{id}", api_graph_setting_delete, methods=["DELETE"]),
    Route("/api/graph/{id}", api_graph_resolve),
    Route("/api/source/{id}", api_source_read),
    Route("/api/source/{id}/attachments", api_source_attachments),
    Route("/api/context/{id}/{turn}", api_context),
    Route("/api/projects", api_projects),
    Route("/api/orgs", api_orgs_list, methods=["GET"]),
    Route("/api/orgs", api_orgs_create, methods=["POST"]),
    Route("/api/orgs/{slug}", api_orgs_show, methods=["GET"]),
    Route("/api/orgs/{slug}", api_orgs_delete, methods=["DELETE"]),
    Route("/api/stats", api_stats),
    Route("/api/attention", api_attention),
    Route("/api/active", api_active_sessions),
    Route("/api/dao/active_sessions", api_dao_active_sessions),
    Route("/api/dao/recent_sessions", api_dao_recent_sessions),
    Route("/api/dao/bead/{id}", api_dao_bead),
    Route("/api/terminals", api_terminals),
    Route("/api/terminal/{id}/kill", api_terminal_kill, methods=["POST"]),
    Route("/api/terminal/{id}/rename", api_terminal_rename, methods=["POST"]),
    Route("/api/primer/{id}", api_primer),
    Route("/api/chatwith/primer/{page_type}", api_chatwith_primer),
    Route("/api/chatwith/check", api_chatwith_check),
    Route("/api/chatwith/sessions", api_chatwith_sessions),
    Route("/api/dispatch/tail/{run}", api_dispatch_tail),
    Route("/api/dispatch/latest/{run}", api_dispatch_latest),
    Route("/api/terminal/unclaimed", api_terminal_unclaimed),
    Route("/api/session/create", api_session_create, methods=["POST"]),
    Route("/api/session/resume", api_session_resume, methods=["POST"]),
    Route("/api/session/send-handshake", api_session_send_handshake, methods=["POST"]),
    Route("/api/session/confirm-link", api_session_confirm_link, methods=["POST"]),
    Route("/api/session/{tmux_name}", api_session_get, methods=["GET"]),
    Route("/api/session/{tmux_name}/interrupt", api_session_interrupt, methods=["POST"]),
    Route("/api/session/{tmux_name}/label", api_session_label, methods=["PUT"]),
    Route("/api/session/{tmux_name}/topics", api_session_topics, methods=["PUT"]),
    Route("/api/session/{tmux_name}/role", api_session_role, methods=["PUT"]),
    Route("/api/session/{tmux_name}/nag", api_session_nag, methods=["PUT"]),
    Route("/api/session/{tmux_name}/nag", api_session_nag_delete, methods=["DELETE"]),
    Route("/api/session/{tmux_name}/dispatch-nag", api_session_dispatch_nag, methods=["PUT"]),
    Route("/api/session/send", api_session_send, methods=["POST"]),
    Route("/api/session/{project}/{session_id}/tail", api_session_tail),
    Route("/api/session/{project}/{session_id}/send", api_session_send, methods=["POST"]),
    Route("/api/upload", api_upload, methods=["POST"]),
    Route("/api/timeline", api_timeline),
    Route("/api/timeline/stats", api_timeline_stats),
    Route("/api/version", api_version),

    # Graph write API (single-writer proxy for containers)
    Route("/api/graph/note", api_graph_note, methods=["POST"]),
    Route("/api/graph/note/update", api_graph_note_update, methods=["POST"]),
    Route("/api/graph/comment", api_graph_comment, methods=["POST"]),
    Route("/api/graph/comment/integrate", api_graph_comment_integrate, methods=["POST"]),
    Route("/api/graph/bead", api_graph_bead, methods=["POST"]),
    Route("/api/graph/link", api_graph_link, methods=["POST"]),
    Route("/api/graph/journal", api_graph_journal_write, methods=["POST"]),
    Route("/api/graph/sessions", api_graph_sessions, methods=["POST"]),
    Route("/api/graph/attach", api_graph_attach, methods=["POST"]),

    # CrossTalk
    Route("/api/crosstalk/send", api_crosstalk_send, methods=["POST"]),
    Route("/api/crosstalk/broadcast", api_crosstalk_broadcast, methods=["POST"]),
    Route("/api/crosstalk/peers", api_crosstalk_peers, methods=["GET"]),

    # Monitor IPC — dispatcher registers dispatch/librarian sessions here so
    # inotify watches + SSE broadcasts are wired up in-process.
    Route("/api/monitor/register", api_monitor_register, methods=["POST"]),
    Route("/api/monitor/deregister", api_monitor_deregister, methods=["POST"]),

    # Embed resolution + attachment serving
    Route("/api/resolve/{id}", api_resolve_embed),
    Route("/api/attachment/{attachment_id}", api_attachment_serve),

    # Design Studio (formerly Experiments)
    Route("/api/design", api_design_create, methods=["POST"]),
    Route("/api/design/pending", api_design_pending),
    Route("/api/design/{id}", api_design_poll),
    Route("/api/design/{id}/full", api_design_get),
    Route("/api/design/{id}/dismiss", api_design_dismiss, methods=["POST"]),
    Route("/api/design/{id}/submit", api_design_submit, methods=["POST"]),
    Route("/api/design/{id}/screenshot", api_design_screenshot, methods=["POST"]),
    Route("/design/{id}", page_design),
    # Backwards compat redirects
    Route("/experiments/{id}", page_experiments_redirect),

    # Static
    Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

# Background task handles — captured during startup, cancelled during shutdown
_dispatch_watcher_task: asyncio.Task | None = None
_mock_event_watcher_task: asyncio.Task | None = None

# Task* tile enricher — per-session taskId → subject/status map. Populated by
# the session monitor tailer as it walks JSONL entries; also used by the HTTP
# history endpoint to produce identical annotations on page load.
_task_state_tracker = TaskStateTracker()

async def _on_startup():
    global _dispatch_watcher_task, _mock_event_watcher_task
    if os.environ.get("DASHBOARD_MOCK"):
        # Mock mode: skip real database init and session monitor.
        # Broadcast initial SSE events from fixture data so SSE-dependent
        # pages (dispatch) render without waiting.
        # NOTE: session:registry is NOT broadcast here — pages fetch fresh
        # data from /api/dao/active_sessions, and broadcasting cached data
        # in the event bus breaks test isolation when fixtures are swapped
        # dynamically between page loads.
        runs = dao_dispatch.get_running_with_stats()
        for r in runs:
            if r.get("last_activity") and isinstance(r["last_activity"], str):
                r["last_activity"] = datetime.fromisoformat(r["last_activity"]).replace(tzinfo=timezone.utc).timestamp()
        await event_bus.broadcast("dispatch", {"active": runs, "waiting": [], "blocked": []})
        # Nav counts from fixture beads
        counts = dao_beads.get_bead_counts()
        running_count = len(runs)
        await event_bus.broadcast("nav", {
            "open_beads": counts.get("total_open_count", 0),
            "running_agents": running_count,
        })
        if os.environ.get("DASHBOARD_MOCK_EVENTS"):
            from tools.dashboard.dao.mock import mock_event_watcher
            _mock_event_watcher_task = asyncio.create_task(mock_event_watcher())
        return

    from agents.dispatch_db import init_db
    init_db()  # ensure dispatch schema exists
    dashboard_db.init_db()  # ensure dashboard.db schema exists
    auth_db.init_db()  # ensure auth.db schema exists
    # First-launch bootstrap: ensure data/orgs/{autonomy,personal}.db exist.
    # Idempotent — pre-existing DBs are left untouched. See graph://d970d946-f95.
    try:
        from tools.graph import org_ops
        org_ops.ensure_bootstrap_orgs()
    except Exception:
        logger.exception("ensure_bootstrap_orgs() failed; continuing startup")
    # Seed from filesystem on first run (one-time), then start background tasks
    await session_monitor.seed_from_filesystem()
    await session_monitor.start(
        event_bus=event_bus,
        entry_parser=_parse_jsonl_entry,
        entry_enricher=_task_state_tracker.enrich,
        todo_snapshot=_task_state_tracker.snapshot,
    )
    _dispatch_watcher_task = asyncio.create_task(_dispatch_watcher())
    if os.environ.get("DASHBOARD_MOCK_EVENTS"):
        from tools.dashboard.dao.mock import mock_event_watcher
        _mock_event_watcher_task = asyncio.create_task(mock_event_watcher())

async def _on_shutdown():
    global _dispatch_watcher_task, _mock_event_watcher_task
    tasks = [t for t in (_dispatch_watcher_task, _mock_event_watcher_task) if t and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _dispatch_watcher_task = None
    _mock_event_watcher_task = None
    try:
        await session_monitor.stop()
    except Exception:
        logger.exception("error during session_monitor.stop()")

@asynccontextmanager
async def _lifespan(app):
    await _on_startup()
    try:
        yield
    finally:
        await _on_shutdown()

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

    _CSP_FRAMEABLE = (
        "default-src 'self'; "
        "script-src 'self' cdn.jsdelivr.net 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'self'"
    )

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Allow same-origin framing for attachment URLs (rich-content iframes)
        if request.url.path.startswith("/api/attachment/"):
            response.headers["Content-Security-Policy"] = self._CSP_FRAMEABLE
        else:
            response.headers["Content-Security-Policy"] = self._CSP
        return response


app = Starlette(routes=routes, lifespan=_lifespan)
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
        reload_excludes=["tools/dashboard/tests"],
    )
