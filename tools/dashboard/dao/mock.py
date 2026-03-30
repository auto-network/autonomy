"""Mock DAO layer + event watcher — file-driven testing without databases.

Activated by setting DASHBOARD_MOCK=path/to/fixtures.json before starting
the server. Every DAO call reads the file fresh, so agents can edit the
fixture file and refresh the page to see changes immediately.

Fixture file format:
{
  "beads": [ {bead dict}, ... ],
  "runs": [ {dispatch run dict}, ... ],
  "active_sessions": [ {session dict}, ... ],
  "session_entries": { "session_id": [ {entry}, ... ] },
  "recent_sessions": [ {session dict}, ... ],
  "experiments": [ {experiment dict with "variants": [{...}]} ],
  "bead_counts": { "open_count": 5, ... },  // optional override
  "dispatch_beads": { "approved_waiting": [...] },  // optional override
  "timeline_entries": [ {run dict}, ... ],
  "timeline_stats": { "completed_count": 0, ... },
  "collab_notes": [ {note dict}, ... ],
  "thoughts": [ {thought dict}, ... ],
  "threads": [ {thread dict}, ... ],
  "streams": [ {stream dict}, ... ],
  "stream_items": { "tag": [ {item dict}, ... ] },
  "traces": { "run-id": {trace dict} },
  "primers": { "bead-id": {primer dict} },
  "bead_deps": { "bead-id": {"blockers": [], "dependents": []} },
  "search_results": [ {result dict}, ... ],
  "graph_sources": { "source-id": {source dict} }
}

Bead dicts must have at minimum: id, title, status, priority.
Experiment dicts must have at minimum: id, title; variants need: id, html.
See *_DEFAULTS below for fields that get auto-filled if omitted.

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


# ── designs mock ──────────────────────────────────────────────────

DESIGN_DEFAULTS: dict[str, Any] = {
    "id": "exp-000000",
    "title": "Untitled Design",
    "description": None,
    "fixture": None,
    "status": "pending",
    "design_id": None,
    "revision_seq": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "alpine": 0,
    "variants": [],
    "revisions": [],
}

VARIANT_DEFAULTS: dict[str, Any] = {
    "id": "var-000000",
    "revision_id": "exp-000000",
    "html": "<p>Empty variant</p>",
    "selected": 0,
    "rank": None,
}


def _designs() -> list[dict]:
    data = _load()
    designs = []
    for e in data.get("experiments", []):
        des = dict(DESIGN_DEFAULTS)
        des.update(e)
        # Support both old and new field names in fixture data
        if des["design_id"] is None:
            des["design_id"] = des.pop("series_id", None) or des["id"]
        if "series_id" in des:
            if des["design_id"] is None:
                des["design_id"] = des.pop("series_id")
            else:
                des.pop("series_id", None)
        if "series_seq" in des and "revision_seq" not in e:
            des["revision_seq"] = des.pop("series_seq")
        elif "series_seq" in des:
            des.pop("series_seq", None)
        # Fill variant defaults
        des["variants"] = [
            {**VARIANT_DEFAULTS, "revision_id": des["id"], **v}
            for v in des.get("variants", [])
        ]
        # Support both old and new field names for revisions list
        if not des["revisions"]:
            des["revisions"] = des.pop("sibling_ids", None) or [des["id"]]
        elif "sibling_ids" in des:
            des.pop("sibling_ids", None)
        designs.append(des)
    return designs


def get_design(rev_id: str) -> dict | None:
    return next((e for e in _designs() if e["id"] == rev_id), None)


def list_pending_designs() -> list[dict]:
    return [e for e in _designs() if e["status"] == "pending"]


def resolve_design_prefix(partial_id: str) -> tuple[str | None, list[str] | None]:
    if len(partial_id) >= 36:
        return partial_id, None
    matches = [e["id"] for e in _designs() if e["id"].startswith(partial_id)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, matches
    return None, None


def create_design(*, title, description=None, fixture=None, variants=None, design_id=None, alpine=False):
    """No-op in mock mode — designs are defined in fixture file."""
    import uuid
    return str(uuid.uuid4())


def submit_results(rev_id, selections):
    return True


def dismiss_design(rev_id):
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


# ── timeline DAO interface ──────────────────────────────────────────

TIMELINE_ENTRY_DEFAULTS: dict[str, Any] = {
    "id": "run-mock-001",
    "bead_id": "auto-test",
    "status": "DONE",
    "title": "Mock task",
    "priority": 2,
    "started_at": "2026-01-01T00:00:00Z",
    "completed_at": "2026-01-01T00:05:00Z",
    "duration_secs": 300,
    "commit_hash": None,
    "commit_message": None,
    "lines_added": None,
    "lines_removed": None,
    "files_changed": None,
    "scores": None,
    "time_breakdown": None,
    "failure_category": None,
    "reason": None,
    "discovered_beads_count": 0,
    "has_experience_report": False,
}

TIMELINE_STATS_DEFAULTS: dict[str, Any] = {
    "completed_count": 0,
    "success_rate": 0.0,
    "failed_count": 0,
    "blocked_count": 0,
    "avg_duration": None,
    "avg_tooling_score": None,
    "avg_confidence_score": None,
    "avg_clarity_score": None,
}


def get_timeline_entries(range_str: str | None = None, limit: int = 200) -> list[dict]:
    data = _load()
    entries = [_fill(e, TIMELINE_ENTRY_DEFAULTS) for e in data.get("timeline_entries", [])]
    return entries[:limit]


def get_timeline_stats(range_str: str | None = None) -> dict:
    data = _load()
    if "timeline_stats" in data:
        stats = dict(TIMELINE_STATS_DEFAULTS)
        stats.update(data["timeline_stats"])
        return stats
    # Auto-compute from timeline_entries
    entries = data.get("timeline_entries", [])
    if not entries:
        return dict(TIMELINE_STATS_DEFAULTS)
    total = len(entries)
    completed = sum(1 for e in entries if e.get("status") == "DONE")
    failed = sum(1 for e in entries if e.get("status") == "FAILED")
    blocked = sum(1 for e in entries if e.get("status") == "BLOCKED")
    durations = [e["duration_secs"] for e in entries if e.get("duration_secs") is not None]
    return {
        "completed_count": completed,
        "success_rate": round(completed / total, 4) if total > 0 else 0.0,
        "failed_count": failed,
        "blocked_count": blocked,
        "avg_duration": round(sum(durations) / len(durations), 1) if durations else None,
        "avg_tooling_score": None,
        "avg_confidence_score": None,
        "avg_clarity_score": None,
    }


# ── graph collab DAO interface ──────────────────────────────────────

COLLAB_NOTE_DEFAULTS: dict[str, Any] = {
    "id": "note-mock-001",
    "title": "Mock note",
    "created_at": "2026-01-01T00:00:00Z",
    "author": "",
    "project": "",
    "tags": [],
    "comment_count": 0,
    "version": 1,
}


def get_collab_notes(limit: int = 20) -> list[dict]:
    data = _load()
    return [_fill(n, COLLAB_NOTE_DEFAULTS) for n in data.get("collab_notes", [])][:limit]


# ── graph thoughts DAO interface ────────────────────────────────────

THOUGHT_DEFAULTS: dict[str, Any] = {
    "id": "thought-mock-001",
    "content": "Mock thought",
    "status": "captured",
    "thread_id": None,
    "source_id": None,
    "turn_number": None,
    "created_at": "2026-01-01T00:00:00Z",
}


def get_thoughts(limit: int = 50, thread_id: str | None = None, since: str | None = None) -> list[dict]:
    data = _load()
    items = [_fill(t, THOUGHT_DEFAULTS) for t in data.get("thoughts", [])]
    if thread_id:
        items = [t for t in items if t.get("thread_id") == thread_id]
    return items[:limit]


# ── graph threads DAO interface ─────────────────────────────────────

THREAD_DEFAULTS: dict[str, Any] = {
    "id": "thread-mock-001",
    "title": "Mock thread",
    "status": "active",
    "priority": 1,
    "capture_count": 0,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def get_threads(limit: int = 20, status: str | None = "active") -> list[dict]:
    data = _load()
    items = [_fill(t, THREAD_DEFAULTS) for t in data.get("threads", [])]
    if status:
        items = [t for t in items if t.get("status") == status]
    return items[:limit]


# ── graph streams DAO interface ─────────────────────────────────────

STREAM_DEFAULTS: dict[str, Any] = {
    "tag": "mock",
    "count": 0,
    "description": "",
    "last_active": "2026-01-01T00:00:00Z",
}


def get_streams() -> list[dict]:
    data = _load()
    return [_fill(s, STREAM_DEFAULTS) for s in data.get("streams", [])]


STREAM_ITEM_DEFAULTS: dict[str, Any] = {
    "id": "note-mock-001",
    "title": "Mock stream item",
    "created_at": "2026-01-01T00:00:00Z",
    "author": "",
    "tags": [],
    "source_type": "note",
    "preview": "",
}


def get_stream_items(tag: str, limit: int = 50) -> list[dict]:
    data = _load()
    items_map = data.get("stream_items", {})
    items = items_map.get(tag, [])
    return [_fill(i, STREAM_ITEM_DEFAULTS) for i in items][:limit]


# ── dispatch trace DAO interface ────────────────────────────────────

TRACE_DEFAULTS: dict[str, Any] = {
    "id": "run-mock-001",
    "bead_id": "auto-test",
    "status": "DONE",
    "reason": "Completed successfully",
    "duration_secs": 300,
    "started_at": "2026-01-01T00:00:00Z",
    "completed_at": "2026-01-01T00:05:00Z",
    "commit_hash": None,
    "commit_message": None,
    "branch": None,
    "branch_base": None,
    "lines_added": None,
    "lines_removed": None,
    "files_changed": None,
    "decision": None,
    "experience_report": None,
    "diff": None,
}


def get_trace(run_id: str) -> dict | None:
    data = _load()
    traces = data.get("traces", {})
    # Try exact match, then bead_id match via runs
    if run_id in traces:
        return _fill(traces[run_id], TRACE_DEFAULTS)
    # Fall back to matching a run and building trace from it
    run = get_run(run_id)
    if run:
        return _fill(run, TRACE_DEFAULTS)
    # Try as bead_id
    bead_runs = get_runs_for_bead(run_id)
    if bead_runs:
        return _fill(bead_runs[0], TRACE_DEFAULTS)
    return None


# ── primer DAO interface ────────────────────────────────────────────

PRIMER_DEFAULTS: dict[str, Any] = {
    "bead_id": "auto-test",
    "title": "Mock bead",
    "description": "Mock description",
    "priority": 2,
    "status": "open",
    "pitfalls": [],
    "provenance": [],
    "similar_beads": [],
}


def get_primer(bead_id: str) -> dict | None:
    data = _load()
    primers = data.get("primers", {})
    if bead_id in primers:
        result = dict(PRIMER_DEFAULTS)
        result.update(primers[bead_id])
        return result
    # Fall back to building from bead data
    bead = get_bead(bead_id)
    if bead:
        return {
            "bead_id": bead["id"],
            "title": bead.get("title", ""),
            "description": bead.get("description", ""),
            "priority": bead.get("priority", 2),
            "status": bead.get("status", "open"),
            "pitfalls": [],
            "provenance": [],
            "similar_beads": [],
        }
    return None


# ── bead deps DAO interface ─────────────────────────────────────────

def get_bead_deps(bead_id: str) -> dict:
    data = _load()
    deps_map = data.get("bead_deps", {})
    if bead_id in deps_map:
        return deps_map[bead_id]
    return {"blockers": [], "dependents": []}


# ── search DAO interface ────────────────────────────────────────────

SEARCH_RESULT_DEFAULTS: dict[str, Any] = {
    "id": "src-mock-001",
    "source_id": "src-mock-001",
    "title": "Mock result",
    "type": "note",
    "project": "",
    "rank": 1.0,
    "snippet": "",
    "turn_number": None,
}


def search(query: str, limit: int = 20, project: str | None = None) -> list[dict]:
    data = _load()
    results = [_fill(r, SEARCH_RESULT_DEFAULTS) for r in data.get("search_results", [])]
    if project:
        results = [r for r in results if r.get("project") == project]
    return results[:limit]


# ── graph source DAO interface ──────────────────────────────────────

SOURCE_DEFAULTS: dict[str, Any] = {
    "id": "src-mock-001",
    "title": "Mock source",
    "type": "note",
    "project": "",
    "created_at": "2026-01-01T00:00:00Z",
    "metadata": "{}",
    "content": "Mock content",
}


def get_source(source_id: str) -> dict | None:
    data = _load()
    sources = data.get("graph_sources", {})
    if source_id in sources:
        return _fill(sources[source_id], SOURCE_DEFAULTS)
    # Try prefix match
    for sid, src in sources.items():
        if sid.startswith(source_id):
            return _fill(src, SOURCE_DEFAULTS)
    return None


def get_attachment(attachment_id: str) -> dict | None:
    """Get attachment from fixture data by ID or prefix."""
    data = _load()
    attachments = data.get("graph_attachments", {})
    if attachment_id in attachments:
        return attachments[attachment_id]
    for aid, att in attachments.items():
        if aid.startswith(attachment_id):
            return att
    return None


def resolve_source_for_api(source_id: str) -> dict | None:
    """Return a graph-read-shaped response for a source, suitable for api_graph_resolve.

    Returns the same shape as `graph read --json --first`:
    {source: {...}, entries: [{content: ...}], edges: [], comments: [], version_count: N}
    """
    src = get_source(source_id)
    if not src:
        return None
    content = src.pop("content", "Mock content")
    entries = src.pop("entries", None)
    if entries is None:
        entries = [{"id": "entry-001", "entry_type": "thought", "role": "user",
                    "turn_number": 1, "content": content, "message_id": None, "metadata": {}}]
    comments = src.pop("comments", [])
    version_count = src.pop("version_count", 1)
    return {
        "source": src,
        "entries": entries,
        "edges": [],
        "comments": comments,
        "version_count": version_count,
    }


def resolve_embed(embed_id: str, version: str | None = None) -> dict | None:
    """Return embed resolution data for a ![[id]] reference.

    Checks sources first (rich-content note), then attachments.
    """
    src = get_source(embed_id)
    if src:
        meta = src.get("metadata", "{}")
        if isinstance(meta, str):
            import json as _json
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        content = src.get("content", "")
        if meta.get("rich_content"):
            # Find HTML attachment for this rich-content note
            data = _load()
            attachments = data.get("graph_attachments", {})
            html_att = None
            for aid, att in attachments.items():
                att_source = att.get("source_id", "")
                if att_source.startswith(src["id"]):
                    if version and att_source == f"{src['id']}@{version}":
                        html_att = att
                        break
                    elif not version:
                        html_att = att  # take latest
            return {
                "type": "rich-content",
                "id": src["id"],
                "title": src.get("title", ""),
                "attachment_url": f"/api/attachment/{html_att['id'][:12]}" if html_att else None,
                "alt_text": content,
                "mime_type": "text/html",
            }
        else:
            return {
                "type": "note",
                "id": src["id"],
                "title": src.get("title", ""),
                "content": content,
            }
    att = get_attachment(embed_id)
    if att:
        return {
            "type": "attachment",
            "id": att["id"],
            "filename": att.get("filename", ""),
            "attachment_url": f"/api/attachment/{att['id'][:12]}",
            "alt_text": att.get("alt_text", ""),
            "mime_type": att.get("mime_type", "application/octet-stream"),
        }
    return None


# ── session mutation stubs (no-ops in mock mode) ────────────────────
# These prevent crashes when session management endpoints are called in mock mode.

def update_label(tmux_name: str, label: str) -> None:
    pass

def update_topics(tmux_name: str, topics: list) -> None:
    pass

def update_role(tmux_name: str, role: str) -> None:
    pass

def update_nag_config(tmux_name: str, **kwargs) -> None:
    pass

def update_dispatch_nag(tmux_name: str, enabled: bool) -> None:
    pass

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
