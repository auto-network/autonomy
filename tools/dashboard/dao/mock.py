"""Mock DAO layer — reads fixture data from a JSON file.

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
