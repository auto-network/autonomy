"""Mock DAO layer + event watcher — file-driven testing without databases.

Activated by setting DASHBOARD_MOCK=path/to/fixtures.json before starting
the server. Every DAO call reads the file fresh, so agents can edit the
fixture file and refresh the page to see changes immediately.

Fixture file format:
{
  "beads": [ {bead dict}, ... ],
  "runs": [ {dispatch run dict}, ... ],
  "active_sessions": [ {session dict}, ... ],
  "session_status": [ {dashboard tmux_session row dict}, ... ],  // optional
  "session_entries": { "session_id": [ {entry}, ... ] },
  "recent_sessions": [ {session dict}, ... ],
  "worktrees": [ {worktree row dict}, ... ],
  "worktree_commit_details": { "session/repo/sha": {commit detail dict} },
  "worktree_changes_details": { "session/repo": {dirty detail dict} },
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
    "parent_id": None,
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
    "todos": [],
    "nag_enabled": False,
    "nag_interval": None,
    "nag_message": None,
    "dispatch_nag_enabled": False,
    "linked": False,
    # auto-ngis4: harness + model are session identity (icon-rail note
    # graph://553c7437-036). Default unknown so legacy fixtures behave
    # the same as production rows that haven't seen an assistant turn yet.
    "harness": "claude",
    "harness_state": "{}",
    "model": None,
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
    "session_uuid": "",
    "file_path": "",
    "resumable": False,
    "session_type": "interactive",
    "total_tokens": 0,
    "total_turns": 0,
    "entry_count": 0,
    "context_tokens": 0,
    "role": "",
    "bead_id": "",
    "tmux_session": "",
    "activity_state": "dead",
    "created_at": "2026-01-01T00:00:00Z",
    "last_activity_at": "2026-01-01T00:00:00Z",
    "ended_at": "2026-01-01T00:00:00Z",
    "librarian_type": None,
    "librarian_target_bead_id": None,
    "librarian_target_bead_title": "",
    # auto-ngis4 — see SESSION_DEFAULTS for context.
    "harness": "claude",
    "model": None,
}

WORKTREE_FILE_DEFAULTS: dict[str, Any] = {
    "status": "M",
    "path": "mock/file.txt",
    "additions": 0,
    "deletions": 0,
}

WORKTREE_COMMIT_DEFAULTS: dict[str, Any] = {
    "sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "short_sha": "deadbee",
    "subject": "Mock worktree commit",
    "author": "Mock Agent",
    "date": "2026-01-01 00:00",
    "body": "",
    "files": [],
    "stats": None,
    "patch": "",
}

WORKTREE_ROW_DEFAULTS: dict[str, Any] = {
    "session_name": "auto-mock-worktree",
    "session_title": "",
    "repo_name": "autonomy",
    "worktree_path": "/tmp/worktrees/auto-mock-worktree/autonomy",
    "managed_clone": "/tmp/repos/autonomy.git",
    "branch": "session/auto-mock-worktree",
    "commits_ahead": 0,
    "is_dirty": False,
    "ff_eligible": False,
    "clone_stale": False,
    "rebase_required": False,
    "session_live": False,
    "target_branch": "main",
    "commits": [],
    "dirty_files": [],
    # auto-ngis4 — see SESSION_DEFAULTS for context.
    "session_harness": None,
    "session_model": None,
}

WORKTREE_DIRTY_DETAIL_DEFAULTS: dict[str, Any] = {
    "files": [],
    "patch": "",
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
    "librarian_type": None,
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
    beads = _beads()
    bead = next((b for b in beads if b["id"] == bead_id), None)
    if bead is None:
        return None
    out = dict(bead)
    children = [dict(b) for b in beads if b.get("parent_id") == bead_id]
    children.sort(key=lambda b: (b.get("priority", 2), b.get("id", "")))
    out["children"] = children
    return out


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

def _attach_org(rows: list[dict]) -> list[dict]:
    """Resolve and attach the org identity object for each row."""
    from tools.dashboard.org_identity import resolve_session_org
    for row in rows:
        if "org" not in row:
            row["org"] = resolve_session_org(row)
    return rows


def get_active_sessions(threshold: int = 600) -> list[dict]:
    data = _load()
    rows = [_fill(s, SESSION_DEFAULTS) for s in data.get("active_sessions", [])]
    from tools.dashboard.dao.sessions import _ACTIVE_SESSION_TYPES
    rows = [r for r in rows if r.get("type") in _ACTIVE_SESSION_TYPES]
    return _attach_org(rows)


def get_session_by_id(session_id: str) -> dict | None:
    """Return any active-session row by session_id without the type filter.

    Production DAO (dao/sessions.py) treats session_id as ``session_uuid OR
    tmux_name``; mock rows may key either field, so check both. The mock
    tail needs this because get_active_sessions filters out dispatch and
    librarian rows via _ACTIVE_SESSION_TYPES.
    """
    data = _load()
    for s in data.get("active_sessions", []):
        if s.get("session_id") == session_id or s.get("session_uuid") == session_id:
            return _attach_org([_fill(s, SESSION_DEFAULTS)])[0]
    return None


def get_session_by_run_dir(run_dir: str) -> dict | None:
    """Return any active-session row by run_dir without the type filter.

    Mirrors get_session_by_id for the dispatch-tail path the overlay uses.
    """
    data = _load()
    for s in data.get("active_sessions", []):
        if s.get("run_dir") == run_dir:
            return _attach_org([_fill(s, SESSION_DEFAULTS)])[0]
    return None


def get_session_entries(session_id: str) -> list[dict] | None:
    """Return mock session entries for tail endpoint, or None if not found."""
    data = _load()
    entries_map = data.get("session_entries", {})
    return entries_map.get(session_id)


def get_recent_sessions(
    limit: int | None = None,
    sort: str = "lastActivity",
    since: str = "1d",
    type_group: str = "all",
) -> list[dict]:
    """Mirror of ``dao.sessions.get_recent_sessions`` for DASHBOARD_MOCK fixtures.

    The real DAO applies ``sort`` + ``since`` + per-type quotas inside a
    SQL-backed pipeline; this mock reproduces the same behavior in Python
    over ``recent_sessions`` fixtures so behavioral sweep tests exercise both.
    """
    import time as _time
    from datetime import datetime as _dt

    from tools.dashboard.dao.sessions import (
        _DEFAULT_TYPE_QUOTAS,
        _group_for_session_type,
    )
    from tools.graph.duration import parse_duration

    data = _load()
    rows = [_fill(s, RECENT_SESSION_DEFAULTS) for s in data.get("recent_sessions", [])]

    if type_group not in _DEFAULT_TYPE_QUOTAS:
        type_group = "all"
    quotas = _DEFAULT_TYPE_QUOTAS[type_group]

    # ── since filter ──
    def _epoch(ts):
        if not ts:
            return 0.0
        try:
            return _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0

    if since and since != "all":
        try:
            cutoff = _time.time() - parse_duration(since)
        except ValueError:
            cutoff = None
        if cutoff is not None:
            rows = [
                r for r in rows
                if _epoch(r.get("last_activity_at") or r.get("created_at") or "") >= cutoff
            ]

    # ── sort key matching the requested column ──
    if sort == "created":
        def _sort_key(r):
            return _epoch(r.get("created_at", ""))
    elif sort == "turns":
        def _sort_key(r):
            return float(r.get("entry_count") or r.get("total_turns") or 0)
    elif sort == "ctx":
        def _sort_key(r):
            return float(r.get("context_tokens") or r.get("total_tokens") or 0)
    elif sort == "duration":
        def _sort_key(r):
            end = _epoch(r.get("last_activity_at") or r.get("ended_at") or "")
            start = _epoch(r.get("created_at") or "")
            return end - start if end and start else 0.0
    else:  # lastActivity (default)
        def _sort_key(r):
            return _epoch(r.get("last_activity_at", ""))

    # ── bucket by type group and trim to quota ──
    buckets: dict[str, list[dict]] = {"interactive": [], "dispatch": [], "librarian": []}
    for row in rows:
        buckets[_group_for_session_type(row.get("session_type"))].append(row)

    trimmed: list[dict] = []
    for group, bucket in buckets.items():
        q = quotas.get(group, 0)
        if q <= 0:
            continue
        bucket.sort(key=_sort_key, reverse=True)
        trimmed.extend(bucket[:q])

    trimmed.sort(key=_sort_key, reverse=True)
    out = trimmed if limit is None else trimmed[:limit]
    return _attach_org(out)


def get_session_status_rows(since: str | None = None) -> list[dict]:
    """Mirror of dashboard session-status rows for CLI/API tests."""
    import time as _time
    from datetime import datetime as _dt

    from tools.graph.duration import parse_duration

    data = _load()
    explicit = data.get("session_status")
    if isinstance(explicit, list):
        rows = [dict(r) for r in explicit]
    else:
        rows: list[dict] = []
        for sess in [_fill(s, SESSION_DEFAULTS) for s in data.get("active_sessions", [])]:
            last_activity = sess.get("last_activity")
            if not isinstance(last_activity, (int, float)):
                last_activity = _time.time()
            rows.append(
                {
                    "tmux_name": sess.get("tmux_session") or sess.get("session_id") or "",
                    "is_live": 1 if sess.get("is_live", True) else 0,
                    "activity_state": sess.get("activity_state") or "idle",
                    "last_activity": float(last_activity),
                    "created_at": float(last_activity),
                    "context_tokens": int(sess.get("context_tokens") or 0),
                    "label": sess.get("label") or "",
                    # auto-ngis4 — surface harness + model on every status row.
                    "harness": sess.get("harness") or "claude",
                    "harness_state": sess.get("harness_state") or "{}",
                    "model": sess.get("model") or None,
                }
            )

        if since is None:
            rows.sort(
                key=lambda row: float(row.get("last_activity") or row.get("created_at") or 0.0),
                reverse=True,
            )
            return rows

        seen = {row["tmux_name"] for row in rows if row.get("tmux_name")}

        def _epoch(ts):
            if not ts:
                return 0.0
            if isinstance(ts, (int, float)):
                return float(ts)
            try:
                return _dt.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                return 0.0

        for sess in [_fill(s, RECENT_SESSION_DEFAULTS) for s in data.get("recent_sessions", [])]:
            tmux_name = sess.get("tmux_session") or sess.get("session_uuid") or sess.get("id") or ""
            if tmux_name in seen:
                continue
            last_activity = _epoch(
                sess.get("last_activity_at") or sess.get("ended_at") or sess.get("created_at")
            )
            rows.append(
                {
                    "tmux_name": tmux_name,
                    "is_live": 0,
                    "activity_state": "dead",
                    "last_activity": last_activity,
                    "created_at": _epoch(sess.get("created_at")),
                    "context_tokens": int(sess.get("context_tokens") or 0),
                    "label": sess.get("title") or "",
                    # auto-ngis4 — surface harness + model on every status row.
                    "harness": sess.get("harness") or "claude",
                    "model": sess.get("model") or None,
                }
            )

    if since is not None:
        cutoff = _time.time() - parse_duration(since)
        rows = [
            row for row in rows
            if float(row.get("last_activity") or row.get("created_at") or 0.0) > cutoff
        ]

    rows.sort(
        key=lambda row: float(row.get("last_activity") or row.get("created_at") or 0.0),
        reverse=True,
    )
    return rows


# ── worktrees DAO interface ─────────────────────────────────────────

def _worktree_file(file: dict) -> dict:
    return _fill(file, WORKTREE_FILE_DEFAULTS)


def _worktree_commit(commit: dict) -> dict:
    data = _fill(commit, WORKTREE_COMMIT_DEFAULTS)
    files = [_worktree_file(file) for file in data.get("files", [])]
    data["files"] = files
    stats = data.get("stats")
    if not stats:
        data["stats"] = {
            "files": len(files),
            "additions": sum(file.get("additions", 0) or 0 for file in files),
            "deletions": sum(file.get("deletions", 0) or 0 for file in files),
        }
    return data


def get_worktrees() -> list[dict]:
    data = _load()
    rows = []
    for row in data.get("worktrees", []):
        item = _fill(row, WORKTREE_ROW_DEFAULTS)
        item["commits"] = [_worktree_commit(commit) for commit in item.get("commits", [])]
        item["dirty_files"] = [_worktree_file(file) for file in item.get("dirty_files", [])]
        rows.append(item)
    return rows


def get_worktree_commit_detail(session_name: str, repo_name: str, sha: str) -> dict | None:
    data = _load()
    details = data.get("worktree_commit_details", {})
    key = f"{session_name}/{repo_name}/{sha}"
    detail = details.get(key)
    if detail is None:
        return None
    return _worktree_commit(detail)


def get_worktree_changes_detail(session_name: str, repo_name: str) -> dict | None:
    data = _load()
    details = data.get("worktree_changes_details", {})
    key = f"{session_name}/{repo_name}"
    detail = details.get(key)
    if detail is None:
        return None
    item = _fill(detail, WORKTREE_DIRTY_DETAIL_DEFAULTS)
    item["files"] = [_worktree_file(file) for file in item.get("files", [])]
    return item


def get_worktree_integrated_diff_detail(session_name: str, repo_name: str) -> dict | None:
    """Mock-mode integrated PR diff. Same shape as the dirty-changes detail."""
    data = _load()
    details = data.get("worktree_integrated_diff_details", {})
    key = f"{session_name}/{repo_name}"
    detail = details.get(key)
    if detail is None:
        return None
    item = _fill(detail, WORKTREE_DIRTY_DETAIL_DEFAULTS)
    item["files"] = [_worktree_file(file) for file in item.get("files", [])]
    return item


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


# ── graph recent-notes DAO interface ────────────────────────────────

RECENT_NOTE_DEFAULTS: dict[str, Any] = {
    "id": "note-mock-recent-001",
    "title": "Mock recent note",
    "created_at": "2026-01-01T00:00:00Z",
    "author": "",
    "project": "",
    "org": "",
    "tags": [],
    "source_type": "note",
    "preview": "",
}


def _default_recent_notes() -> list[dict]:
    """Plausible defaults so DASHBOARD_MOCK fixtures without ``recent_notes``
    still render a populated /collab Recent tab. Mixes source types, authors,
    and tags so the redesigned card helpers can be exercised end to end."""
    return [
        {
            "id": "note-mock-recent-001",
            "title": "pitfall: dashboard hot-reload resets EventBus epoch",
            "created_at": "2026-04-27T14:00:00Z",
            "author": "host-0418-192255",
            "project": "autonomy",
            "org": "autonomy",
            "tags": ["pitfall", "dashboard", "eventbus"],
            "source_type": "note",
            "preview": "Hot reload of the Dashboard creates a lot of issues because it resets sequence numbers",
        },
        {
            "id": "note-mock-recent-002",
            "title": "EventBus state persistence across uvicorn reload",
            "created_at": "2026-04-26T12:00:00Z",
            "author": "terminal:auto-83g69",
            "project": "autonomy",
            "org": "autonomy",
            "tags": ["eventbus", "dashboard"],
            "source_type": "note",
            "preview": "Snapshot is atomic (temp file + rename), restored on startup before any subscriber connects",
        },
        {
            "id": "note-mock-recent-003",
            "title": "Search needs source_type colors and per-turn drill-down",
            "created_at": "2026-04-25T09:30:00Z",
            "author": "",
            "project": "autonomy",
            "org": "autonomy",
            "tags": [],
            "source_type": "thought",
            "preview": "",
        },
        {
            "id": "note-mock-recent-004",
            "title": "Per-org DB + cross-org search architecture",
            "created_at": "2026-04-24T16:45:00Z",
            "author": "terminal:host-0420-122533",
            "project": "autonomy",
            "org": "autonomy",
            "tags": ["architecture", "graph", "canonical"],
            "source_type": "note",
            "preview": "Project = hard boundary (sharing/sync/access). Tags = soft boundary (search/ranking).",
        },
        {
            "id": "note-mock-recent-005",
            "title": "Dispatch run auto-83g69 — DONE",
            "created_at": "2026-04-23T22:10:00Z",
            "author": "agent",
            "project": "autonomy",
            "org": "autonomy",
            "tags": ["dispatch"],
            "source_type": "agent-run",
            "preview": "Refactored monitor_ipc to share the SSE bus with the dashboard reload watcher.",
        },
    ]


def get_recent_notes(limit: int = 50) -> list[dict]:
    """Mirror of ``graph_ops.list_notes`` for DASHBOARD_MOCK fixtures.

    Reads ``recent_notes`` from the fixture if present. Otherwise falls back
    to a plausible mixed-type default so the /collab Recent tab is never
    empty in mock mode.
    """
    data = _load()
    rows = data.get("recent_notes")
    if rows is None:
        rows = _default_recent_notes()
    return [_fill(n, RECENT_NOTE_DEFAULTS) for n in rows][:limit]


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
    "source_title": "Mock result",
    "source_type": "note",
    "result_type": "thought",
    "project": "",
    "platform": "local",
    "rank": -1.0,
    "rrf_score": 0.0,
    "content": "",
    "turn_number": None,
    "source_created_at": "",
    "source_metadata": "{}",
    "short_description": None,
    "keywords": None,
}


def _row_session_type(row: dict) -> str | None:
    """Extract ``metadata.session_type`` from a fixture row.

    Fixtures may carry ``session_type`` either at the top level (the
    behavioural-sweep shape) or nested inside ``source_metadata`` JSON
    (the production /api/search shape). Check both so a single fixture
    row can drive both client-shape and server-shape assertions.
    """
    direct = row.get("session_type")
    if direct is not None:
        return direct
    meta = row.get("source_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if isinstance(meta, dict):
        v = meta.get("session_type")
        if isinstance(v, str):
            return v
    return None


def search(
    query: str,
    limit: int = 20,
    project: str | None = None,
    order: str = "relevance",
    session_type: list[str] | None = None,
) -> list[dict]:
    data = _load()
    results = [_fill(r, SEARCH_RESULT_DEFAULTS) for r in data.get("search_results", [])]
    if project:
        results = [r for r in results if r.get("project") == project]
    if query:
        # Behavioural sweep relies on the no-match branch to drive the
        # empty-state UI. Production FTS is full-text; here we approximate
        # with a case-insensitive substring across title + content so a
        # query like "zzzzz_no_match" filters everything out.
        q_lower = query.lower()
        results = [
            r for r in results
            if q_lower in (r.get("source_title", "") or "").lower()
            or q_lower in (r.get("content", "") or "").lower()
        ]
    # Strict session_type filter — empty list returns zero rows; rows
    # with NULL session_type are NEVER kept once a non-None filter is
    # applied. Mirrors db.search's contract.
    if session_type is not None:
        if not session_type:
            return []
        allowed = set(session_type)
        results = [r for r in results if _row_session_type(r) in allowed]
    if order == "recent":
        # Stable sort by source_created_at DESC. Empty timestamps sort last.
        results = sorted(
            results,
            key=lambda r: r.get("source_created_at") or "",
            reverse=True,
        )
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


# ── settings DAO interface ──────────────────────────────────────────
# Mirrors the response shape of /api/graph/settings/<set_id> (production
# returns SetMembers.as_payload()) for the dashboard.agent-actions
# dropdown. The fixture's "settings" key is keyed by set_id and may
# specify members per-org via {"_orgs": {"<slug>": [...]}} or a flat list
# that applies to every org.

def get_settings_members(set_id: str, org: str | None = None) -> list[dict]:
    """Return the members list for *set_id*, optionally filtered to *org*.

    Each row is normalized to ``{"key": str, "payload": dict, "org": str}``
    matching the subset of ``ResolvedSetting.to_dict()`` that the dashboard
    Settings API exposes. Members declared without an explicit org apply
    to every caller (canonical members), mirroring the production
    promotion model. Members carrying ``deprecated == 1`` are dropped to
    mirror the production ``read_set`` filter (auto-17oir).
    """
    data = _load()
    block = data.get("settings") or {}
    raw = block.get(set_id)
    if raw is None:
        return []
    if isinstance(raw, dict):
        flat = list(raw.get("_all", []))
        per_org = raw.get("_orgs") or {}
        if org and org in per_org:
            flat = flat + list(per_org[org])
    else:
        flat = list(raw)
    out: list[dict] = []
    for entry in flat:
        if entry.get("deprecated"):
            continue
        member = dict(entry)
        member.setdefault("org", org or "")
        member.setdefault("payload", {})
        out.append(member)
    return out


def add_setting_member(
    set_id: str,
    key: str,
    payload: dict,
    *,
    org: str | None = None,
) -> str:
    """Append a Setting member to the fixture file under ``set_id``.

    Mirrors :func:`tools.graph.settings_ops.add_setting` for the mock DAO:
    the next ``get_settings_members(set_id)`` call surfaces the new row.
    Existing rows with the same ``key`` are *replaced* (latest-write-wins
    semantics for non-append-only sets like ``operator-message``); for
    append-only sets the caller passes a unique key (e.g. uuid) so the
    replace is a no-op. Returns the synthetic id assigned to the row.
    """
    from uuid import uuid4
    sid = str(uuid4())
    data = _load()
    block = data.setdefault("settings", {})
    raw = block.setdefault(set_id, {})
    if isinstance(raw, list):
        raw = {"_all": list(raw)}
        block[set_id] = raw
    all_list = raw.setdefault("_all", [])
    all_list[:] = [m for m in all_list if m.get("key") != key]
    member = {"id": sid, "key": key, "payload": dict(payload)}
    if org:
        member["org"] = org
    all_list.append(member)
    FIXTURE_PATH.write_text(json.dumps(data, indent=2))
    return sid


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
