"""Mock DAO layer + event watcher — file-driven testing without databases.

Activated by setting DASHBOARD_MOCK=path/to/fixtures.json before starting
the server. Every DAO call reads the file fresh, so agents can edit the
fixture file and refresh the page to see changes immediately.

Fixture file format:
{
  "beads": [ {bead dict}, ... ],
  "runs": [ {dispatch run dict}, ... ],
  "bead_counts": { "open_count": 5, ... }  // optional override
}

Bead dicts must have at minimum: id, title, status, priority.
See FIXTURE_DEFAULTS below for fields that get auto-filled if omitted.

SSE events: Set DASHBOARD_MOCK_EVENTS=path/to/events.jsonl. The mock
event watcher tails this file and broadcasts each new line to the event
bus. Agents append JSONL lines to push SSE updates:

  echo '{"topic":"dispatch","data":{"active":[],"waiting":[],"blocked":[]}}' >> events.jsonl
  echo '{"topic":"nav","data":{"open_beads":5,"running_agents":1}}' >> events.jsonl
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(os.environ.get("DASHBOARD_MOCK", "fixtures.json"))

BEAD_DEFAULTS: dict[str, Any] = {
    "status": "open",
    "priority": 2,
    "issue_type": "task",
    "description": "",
    "design": None,
    "acceptance_criteria": None,
    "notes": None,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "closed_at": None,
    "assignee": None,
    "estimated_minutes": None,
    "close_reason": None,
    "created_by": None,
    "owner": None,
    "labels": [],
    "deps": [],
    "comments": [],
}

SESSION_DEFAULTS: dict[str, Any] = {
    "session_id": "session000000",
    "tmux_session": "session000000",
    "project": "default",
    "type": "container",
    "is_live": True,
    "started_at": "2026-01-01T00:00:00Z",
    "graph_source_id": "",
    "label": "",
    "role": "",
    "entry_count": 0,
    "context_tokens": 0,
    "last_activity": None,
    "last_message": "",
    "topics": "[]",
    "nag_enabled": False,
    "nag_interval": None,
    "nag_message": None,
    "linked": False,
    # Legacy fields for backward compat
    "size_bytes": 1024000,
    "age_seconds": 120,
    "active": False,
    "latest": "",
}

RECENT_SESSION_DEFAULTS: dict[str, Any] = {
    "id": "src-000000000000",
    "type": "session",
    "date": "2026-01-01",
    "title": "",
    "project": "",
}

RUN_DEFAULTS: dict[str, Any] = {
    "status": "COMPLETED",
    "bead_id": "auto-test",
    "started_at": "2026-01-01T00:00:00Z",
    "completed_at": "2026-01-01T00:01:00Z",
    "last_activity": None,
    "exit_code": 0,
    "reason": None,
    "snippet": None,
    "tokens": None,
    "cost": None,
    # Fields needed for timeline/trace rendering
    "title": None,
    "priority": None,
    "duration_secs": None,
    "commit_hash": None,
    "commit_message": None,
    "lines_added": None,
    "lines_removed": None,
    "files_changed": None,
    "scores": None,
    "time_breakdown": None,
    "failure_category": None,
    "discovered_beads_count": 0,
    "has_experience_report": False,
}


def _load() -> dict:
    """Read and parse the fixture file. Returns empty structure if missing."""
    if not FIXTURE_PATH.exists():
        return {"beads": [], "runs": []}
    return json.loads(FIXTURE_PATH.read_text())


def _fill(row: dict, defaults: dict) -> dict:
    """Fill missing keys from defaults."""
    out = dict(defaults)
    out.update(row)
    return out


def _beads() -> list[dict]:
    return [_fill(b, BEAD_DEFAULTS) for b in _load().get("beads", [])]


def _runs() -> list[dict]:
    return [_fill(r, RUN_DEFAULTS) for r in _load().get("runs", [])]


# ── beads DAO interface ──────────────────────────────────────────────

def get_beads_by_label(label: str) -> list[dict]:
    return [b for b in _beads() if label in (b.get("labels") or [])]


def get_open_beads(limit: int = 200) -> list[dict]:
    return [b for b in _beads() if b["status"] != "closed"][:limit]


def get_bead(bead_id: str) -> dict | None:
    return next((b for b in _beads() if b["id"] == bead_id), None)


def get_bead_counts() -> dict[str, int]:
    data = _load()
    if "bead_counts" in data:
        return data["bead_counts"]
    beads = _beads()
    open_beads = [b for b in beads if b["status"] != "closed"]
    approved = [b for b in open_beads if "readiness:approved" in (b.get("labels") or [])]
    return {
        "open_count": len([b for b in open_beads if b["status"] == "open"]),
        "in_progress_count": len([b for b in open_beads if b["status"] == "in_progress"]),
        "approved_count": len(approved),
        "approved_blocked_count": 0,
        "total_open_count": len(open_beads),
    }


def get_dispatch_beads() -> dict[str, list[dict]]:
    data = _load()
    if "dispatch_beads" in data:
        return data["dispatch_beads"]
    beads = _beads()
    approved = [b for b in beads if "readiness:approved" in (b.get("labels") or []) and b["status"] == "open"]
    return {"approved_waiting": approved, "approved_blocked": []}


def get_bead_title_priority(bead_ids: list[str]) -> dict[str, dict]:
    beads = _beads()
    return {
        b["id"]: {"id": b["id"], "title": b["title"], "priority": b["priority"], "labels": b.get("labels", [])}
        for b in beads if b["id"] in bead_ids
    }


# ── experiments mock ──────────────────────────────────────────────────

EXPERIMENT_DEFAULTS: dict[str, Any] = {
    "id": "exp-000000",
    "title": "Untitled Experiment",
    "description": None,
    "fixture": None,
    "status": "pending",
    "series_id": None,
    "series_seq": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "alpine": 0,
    "variants": [],
    "sibling_ids": [],
}

VARIANT_DEFAULTS: dict[str, Any] = {
    "id": "var-000000",
    "experiment_id": "exp-000000",
    "html": "<p>Empty variant</p>",
    "selected": 0,
    "rank": None,
}


def _experiments() -> list[dict]:
    data = _load()
    exps = []
    for e in data.get("experiments", []):
        exp = dict(EXPERIMENT_DEFAULTS)
        exp.update(e)
        if exp["series_id"] is None:
            exp["series_id"] = exp["id"]
        # Fill variant defaults
        exp["variants"] = [
            {**VARIANT_DEFAULTS, "experiment_id": exp["id"], **v}
            for v in exp.get("variants", [])
        ]
        if not exp["sibling_ids"]:
            exp["sibling_ids"] = [exp["id"]]
        exps.append(exp)
    return exps


def get_experiment(exp_id: str) -> dict | None:
    return next((e for e in _experiments() if e["id"] == exp_id), None)


def list_pending_experiments() -> list[dict]:
    return [e for e in _experiments() if e["status"] == "pending"]


def resolve_experiment_prefix(partial_id: str) -> tuple[str | None, list[str] | None]:
    if len(partial_id) >= 36:
        return partial_id, None
    matches = [e["id"] for e in _experiments() if e["id"].startswith(partial_id)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, matches
    return None, None


def create_experiment(*, title, description=None, fixture=None, variants=None, series_id=None, alpine=False):
    """No-op in mock mode — experiments are defined in fixture file."""
    import uuid
    return str(uuid.uuid4())


def submit_results(exp_id, selections):
    return True


def dismiss_experiment(exp_id):
    return True


# ── sessions DAO interface ───────────────────────────────────────────

def get_active_sessions(threshold: int = 600) -> list[dict]:
    data = _load()
    return [_fill(s, SESSION_DEFAULTS) for s in data.get("active_sessions", [])]


def get_session_entries(session_id: str) -> list[dict] | None:
    """Return mock session entries for tail endpoint, or None if not found."""
    data = _load()
    entries_map = data.get("session_entries", {})
    return entries_map.get(session_id)


def get_recent_sessions(limit: int = 20) -> list[dict]:
    data = _load()
    return [_fill(s, RECENT_SESSION_DEFAULTS) for s in data.get("recent_sessions", [])][:limit]


# ── dispatch DAO interface ───────────────────────────────────────────

def get_running_with_stats() -> list[dict]:
    return [r for r in _runs() if r["status"] == "RUNNING"]


def get_recent_runs(limit: int = 50) -> list[dict]:
    runs = [r for r in _runs() if r["status"] != "RUNNING"]
    return sorted(runs, key=lambda r: r.get("completed_at", ""), reverse=True)[:limit]


def get_run(run_id: str) -> dict | None:
    return next((r for r in _runs() if r["id"] == run_id), None)


def get_runs_for_bead(bead_id: str) -> list[dict]:
    return [r for r in _runs() if r.get("bead_id") == bead_id]


# ── Mock event watcher (replaces _dispatch_watcher) ─────────────────

EVENTS_PATH = Path(os.environ.get("DASHBOARD_MOCK_EVENTS", ""))


async def mock_event_watcher():
    """Tail a JSONL file and broadcast each new line to the event bus.

    Each line must be: {"topic": "...", "data": {...}}
    Agents append lines to push SSE updates to connected browsers.
    Polls every 0.5s for new lines. Silently ignores missing file or
    malformed lines.
    """
    from tools.dashboard.event_bus import event_bus

    if not EVENTS_PATH or not str(EVENTS_PATH):
        return

    import asyncio
    lines_read = 0
    while True:
        try:
            if EVENTS_PATH.exists():
                all_lines = EVENTS_PATH.read_text().splitlines()
                new_lines = all_lines[lines_read:]
                lines_read = len(all_lines)
                for line in new_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        topic = event.get("topic")
                        data = event.get("data")
                        if topic and data is not None:
                            await event_bus.broadcast(topic, data)
                    except (json.JSONDecodeError, AttributeError):
                        pass
        except Exception:
            pass
        await asyncio.sleep(0.5)
