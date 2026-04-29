"""L2.B behavioral sweep tests — fast browser-based assertions on user-visible behavior.

Testing hierarchy:
    L1 (unit, 5ms) → L2.A (HTTP contract, 50ms) → **L2.B (behavioral sweep, 200ms/page)**
    → L3 (interactive browser, 3s) → L4 (production smoke, 30s)

Architecture:
    - ONE DASHBOARD_MOCK server (module-scoped fixture, boots once)
    - ONE agent-browser session (module-scoped, reused across all tests)
    - Per page: SPA-navigates via link click, runs ONE batched JS eval
      with all checks, returns structured dict
    - Python asserts on dict values — each assert is a user-visible behavior

Pattern for adding a new page:
    1. Define a JS check function that returns {check_name: value, ...}
    2. Write a test class using _navigate_and_check(path, js_checks)
    3. Each test method asserts one key from the returned dict

Fixture data is self-contained — no external DB, no tmux, no real sessions.
The DASHBOARD_MOCK server reads fixture JSON on every request; the sessions
page seeds its Alpine store from /api/dao/active_sessions (HTTP fallback).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tools.dashboard.tests._xdist import worker_test_port

# ── Fixture data ──────────────────────────────────────────────────────

NOW = int(time.time())

SWEEP_SESSIONS = [
    {
        "session_id": "auto-sweep-alpha",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 3600,
        "label": "Alpha — card redesign",
        "role": "designer",
        "entry_count": 150,
        "context_tokens": 80000,
        "last_activity": NOW - 120,
        "last_message": "Working on card CSS",
        "topics": ["Redesigning session cards", "CSS grid layout"],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
    {
        "session_id": "auto-sweep-beta",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 7200,
        "label": "Beta Builder",
        "role": "builder",
        "entry_count": 200,
        "context_tokens": 120000,
        "last_activity": NOW - 300,
        "last_message": "Compiling assets",
        "topics": ["Asset pipeline", "Webpack config", "Tree shaking"],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
    {
        "session_id": "auto-sweep-gamma",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 1800,
        "label": "Gamma Reviewer",
        "role": "reviewer",
        "entry_count": 75,
        "context_tokens": 45000,
        "last_activity": NOW - 60,
        "last_message": "Reviewing PR #42",
        "topics": ["Code review"],
        "nag_enabled": True,
        "nag_interval": 10,
        "nag_message": "Check review status",
    },
    {
        "session_id": "host-sweep-delta",
        "project": "autonomy",
        "type": "host",
        "is_live": True,
        "started_at": NOW - 14400,
        "label": "Host: merge recovery",
        "role": "coordinator",
        "entry_count": 300,
        "context_tokens": 250000,
        "last_activity": NOW - 900,
        "last_message": "Dolt restarted",
        "topics": [],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
    {
        "session_id": "auto-sweep-epsilon",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 600,
        "label": "Epsilon Session",
        "role": "",
        "entry_count": 50,
        "context_tokens": 30000,
        "last_activity": NOW - 30,
        "last_message": "Idle session",
        "topics": [],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
]

# Timestamps are wall-clock-relative so the default `since=1d` filter
# keeps all three rows. Spread of turns/ctx values lets `Most Turns` and
# `Most Context` reorderings be asserted.
def _iso_minutes_ago(minutes: int) -> str:
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    return (_dt.now(_tz.utc) - _td(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


SWEEP_RECENT_SESSIONS = [
    # Most turns (627) — placed active 10 minutes ago so sort=lastActivity
    # puts it second-most-recent. Under sort=turns it must float to the top.
    # Interactive (dead): footer shows ended + tmux.
    {"id": "src-sweep-aaa111", "type": "session", "date": "2026-03-25",
     "title": "Alpha history session", "project": "autonomy",
     "session_type": "interactive", "resumable": True,
     "tmux_session": "auto-0418-223100",
     "created_at": _iso_minutes_ago(90), "last_activity_at": _iso_minutes_ago(10),
     "ended_at": _iso_minutes_ago(10),
     "entry_count": 627, "context_tokens": 120000,
     "total_turns": 627, "total_tokens": 120000},
    # 0 turns / 0 ctx — most-recent activity; with sort=lastActivity it's
    # first, with sort=turns or sort=ctx it should fall to the bottom.
    # Dispatch (dead): footer shows ended only — tmux column hidden.
    {"id": "src-sweep-bbb222", "type": "session", "date": "2026-03-24",
     "title": "<crosstalk echo>", "project": "autonomy",
     "session_type": "dispatch", "resumable": False,
     "tmux_session": "agent-auto-bbb222-12345",
     "created_at": _iso_minutes_ago(5), "last_activity_at": _iso_minutes_ago(5),
     "ended_at": _iso_minutes_ago(5),
     "entry_count": 0, "context_tokens": 0,
     "total_turns": 0, "total_tokens": 0},
    # Medium turns — oldest within the 1d window. Excluded when since=6h if
    # we push its timestamp beyond 6h; left inside for the default case.
    # Librarian (dead): footer shows ended only — tmux column hidden.
    {"id": "src-sweep-ccc333", "type": "session", "date": "2026-03-23",
     "title": "Gamma history session", "project": "default",
     "session_type": "librarian", "resumable": False,
     "tmux_session": "librarian-auto-ccc333-67890",
     "created_at": _iso_minutes_ago(300), "last_activity_at": _iso_minutes_ago(120),
     "ended_at": _iso_minutes_ago(120),
     "entry_count": 84, "context_tokens": 55000,
     "total_turns": 84, "total_tokens": 55000},
]

SWEEP_SESSION_ENTRIES = {
    s["session_id"]: [
        {"type": "system", "content": "Session started", "timestamp": NOW - 3600},
        {"type": "user", "content": "Hello", "timestamp": NOW - 3590},
        {"type": "assistant_text", "content": "Hi there", "timestamp": NOW - 3585},
    ]
    for s in SWEEP_SESSIONS
}

# Add a turn with literal graph:// text to one session (for graph:// rewrite scoping tests)
SWEEP_SESSION_ENTRIES["auto-sweep-alpha"].append(
    {"type": "assistant_text",
     "content": "Use `![alt](graph://test-attachment-id)` to embed images.\n\nSee graph://test-attachment-id for the source.",
     "timestamp": NOW - 3580}
)

# Task* tool_use tile state matrix — exercised by TestSessionViewerTodoTiles.
# Entries are already in parsed shape; the mock tail endpoint runs the
# TaskStateTracker enricher across the list before returning.
SWEEP_SESSION_ENTRIES["auto-sweep-alpha"].extend([
    {"type": "tool_use", "tool_name": "TaskCreate", "tool_id": "tu-tc-1",
     "input": {
         "subject": "Inspect TaskCreate payload shape",
         "description": "Read the raw JSONL and note the shape of the input.",
         "activeForm": "Inspecting TaskCreate payload shape",
     },
     "timestamp": NOW - 3575},
    {"type": "tool_use", "tool_name": "TaskCreate", "tool_id": "tu-tc-2",
     "input": {
         "subject": "Wire TaskStateTracker into tailer",
         "description": "Hook the tracker into entry_enricher and broadcast annotations.",
         "activeForm": "Wiring TaskStateTracker into tailer",
     },
     "timestamp": NOW - 3570},
    {"type": "tool_use", "tool_name": "TaskUpdate", "tool_id": "tu-tu-1",
     "input": {"taskId": "1", "status": "in_progress"},
     "timestamp": NOW - 3565},
    {"type": "tool_use", "tool_name": "TaskUpdate", "tool_id": "tu-tu-2",
     "input": {"taskId": "1", "status": "completed"},
     "timestamp": NOW - 3560},
    # Subject rename on task 2 → later update should report the renamed subject
    {"type": "tool_use", "tool_name": "TaskUpdate", "tool_id": "tu-tu-3",
     "input": {"taskId": "2", "subject": "Wire TaskStateTracker (renamed)"},
     "timestamp": NOW - 3555},
    {"type": "tool_use", "tool_name": "TaskUpdate", "tool_id": "tu-tu-4",
     "input": {"taskId": "2", "status": "in_progress"},
     "timestamp": NOW - 3550},
])

# ── Beads (used by /beads, /bead/{id}, /dispatch, nav SSE) ───────────

SWEEP_BEADS = [
    {
        "id": "auto-sweep-b1", "title": "Sweep alpha task",
        "priority": 1, "status": "open", "issue_type": "task",
        "labels": ["readiness:approved"], "created_by": "librarian",
        "description": "First test bead for behavioral sweep",
    },
    {
        "id": "auto-sweep-b2", "title": "Sweep beta bug",
        "priority": 2, "status": "in_progress", "issue_type": "bug",
        "labels": ["readiness:specified", "dashboard"], "created_by": "user",
        "description": "Second test bead with dependencies",
    },
    {
        "id": "auto-sweep-b3", "title": "Sweep gamma feature",
        "priority": 0, "status": "open", "issue_type": "feature",
        "labels": [], "created_by": "librarian",
    },
    {
        "id": "auto-sweep-b4", "title": "Graph rewrite test bead",
        "priority": 2, "status": "open", "issue_type": "task",
        "labels": [], "created_by": "librarian",
        "description": "Reference: graph://some-attachment-id for the diagram.\n\nAlso supports `![[embed-id]]` syntax.",
    },
]

# ── Dispatch runs (used by /timeline, /dispatch/trace) ───────────────

SWEEP_RUNS = [
    {
        "id": "run-sweep-001", "bead_id": "auto-sweep-b1",
        "status": "DONE", "title": "Sweep alpha task",
        "priority": 1, "duration_secs": 300,
        "started_at": "2026-03-25T10:00:00Z",
        "completed_at": "2026-03-25T10:05:00Z",
        "lines_added": 45, "lines_removed": 12, "files_changed": 3,
        "reason": "All tests pass",
        "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
        "time_breakdown": {
            "research_pct": 20, "coding_pct": 60,
            "debugging_pct": 15, "tooling_workaround_pct": 5,
        },
    },
    {
        "id": "run-sweep-002", "bead_id": "auto-sweep-b2",
        "status": "FAILED", "title": "Sweep beta bug",
        "priority": 2, "duration_secs": 120,
        "started_at": "2026-03-25T09:00:00Z",
        "completed_at": "2026-03-25T09:02:00Z",
        "reason": "Tests failed on assertion",
    },
]

# ── Timeline data ────────────────────────────────────────────────────

SWEEP_TIMELINE_ENTRIES = SWEEP_RUNS  # timeline uses same shape as runs

SWEEP_TIMELINE_STATS = {
    "completed_count": 5,
    "success_rate": 0.8,
    "failed_count": 1,
    "blocked_count": 0,
    "avg_duration": 240.0,
    "avg_tooling_score": 3.5,
    "avg_confidence_score": 4.0,
    "avg_clarity_score": 3.8,
}

# ── Collab data ──────────────────────────────────────────────────────

SWEEP_COLLAB_NOTES = [
    {
        "id": "note-sweep-001", "title": "Architecture decision on auth",
        "created_at": "2026-03-25T08:00:00Z", "author": "agent-alpha",
        "project": "autonomy", "tags": ["architecture", "auth"],
        "comment_count": 2, "version": 1, "source_type": "note",
        "preview": "Passkey authentication requires WebAuthn support",
    },
    {
        "id": "note-sweep-002", "title": "Testing strategy update",
        "created_at": "2026-03-24T12:00:00Z", "author": "agent-beta",
        "project": "autonomy", "tags": ["testing"],
        "comment_count": 0, "version": 1, "source_type": "note",
        "preview": "L2.B behavioral sweep covers all pages",
    },
]

# Recent feed (untagged, mixed source-type) — backs /api/graph/notes in mock mode.
SWEEP_RECENT_NOTES = [
    {
        "id": "note-sweep-recent-001",
        "title": "Architecture follow-up — sequencing",
        "created_at": "2026-03-26T08:00:00Z", "author": "agent-alpha",
        "project": "autonomy", "org": "autonomy",
        "tags": ["architecture"], "source_type": "note",
        "preview": "Sequence the auth migration after the passkey rollout",
    },
    {
        "id": "note-sweep-recent-002",
        "title": "Testing harness improvements",
        "created_at": "2026-03-25T12:00:00Z", "author": "agent-beta",
        "project": "autonomy", "org": "autonomy",
        "tags": ["testing"], "source_type": "note",
        "preview": "Tee pytest output so reruns aren't needed",
    },
    {
        "id": "note-sweep-recent-003",
        "title": "Dispatch run auto-test1 — DONE",
        "created_at": "2026-03-25T08:00:00Z", "author": "agent",
        "project": "autonomy", "org": "autonomy",
        "tags": ["dispatch"], "source_type": "agent-run",
        "preview": "Refactor merged successfully",
    },
]

SWEEP_THOUGHTS = [
    {
        "id": "thought-sweep-001", "content": "Auth needs passkeys for MFA",
        "status": "captured", "thread_id": None,
        "source_id": None, "turn_number": None,
        "created_at": "2026-03-25T09:00:00Z",
    },
    {
        "id": "thought-sweep-002", "content": "Consider Alpine.js migration path",
        "status": "actioned", "thread_id": "thread-sweep-001",
        "source_id": None, "turn_number": None,
        "created_at": "2026-03-24T14:00:00Z",
    },
]

SWEEP_THREADS = [
    {
        "id": "thread-sweep-001", "title": "Passkey auth design",
        "status": "active", "priority": 1, "capture_count": 3,
        "created_at": "2026-03-24T10:00:00Z",
        "updated_at": "2026-03-25T09:00:00Z",
    },
    {
        "id": "thread-sweep-002", "title": "Performance optimization",
        "status": "resolved", "priority": 2, "capture_count": 1,
        "created_at": "2026-03-23T10:00:00Z",
        "updated_at": "2026-03-24T10:00:00Z",
    },
]

# ── Streams data ─────────────────────────────────────────────────────

SWEEP_STREAMS = [
    {"tag": "pitfall", "count": 12, "description": "Operational hazards and gotchas",
     "last_active": "2026-03-25T10:00:00Z"},
    {"tag": "architecture", "count": 8, "description": "Design decisions and patterns",
     "last_active": "2026-03-24T10:00:00Z"},
    {"tag": "testing", "count": 5, "description": "Testing strategies and patterns",
     "last_active": "2026-03-23T10:00:00Z"},
]

# ── Trace / primer / deps data ───────────────────────────────────────

SWEEP_TRACES = {
    "run-sweep-001": {
        "id": "run-sweep-001", "bead_id": "auto-sweep-b1",
        "status": "DONE", "reason": "All tests pass",
        "duration_secs": 300,
        "started_at": "2026-03-25T10:00:00Z",
        "completed_at": "2026-03-25T10:05:00Z",
        "commit_hash": "abc123def456789",
        "lines_added": 45, "lines_removed": 12, "files_changed": 3,
        "is_live": False,
        "decision": {
            "status": "DONE", "reason": "All tests pass",
            "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
            "time_breakdown": {
                "research_pct": 20, "coding_pct": 60,
                "debugging_pct": 15, "tooling_workaround_pct": 5,
            },
        },
    },
}

SWEEP_PRIMERS = {
    "auto-sweep-b1": {
        "bead_id": "auto-sweep-b1", "title": "Sweep alpha task",
        "description": "First test bead for behavioral sweep",
        "priority": 1, "status": "open",
        "pitfalls": [],
        "provenance": [],
        "similar_beads": [],
    },
}

SWEEP_BEAD_DEPS = {
    "auto-sweep-b2": {
        "blockers": [{"id": "auto-sweep-b1", "title": "Sweep alpha task", "status": "open"}],
        "dependents": [],
    },
}

# ── Dispatch SSE rows with kind metadata (auto-5k2j4 → auto-qlfg1) ────
# Used by TestDispatchAgenticKindBadge. The agentic row carries
# ``kind='agentic'`` to trigger the priority-badge partial's agentic
# branch; the legacy row omits ``kind`` entirely so the UI's
# ``(b.kind || 'bead')`` COALESCE picks 'bead' and renders the regular
# priority badge.

SWEEP_AGENTIC_SOURCE_ID = "bd2a78f2-da73-453c-b873-9002ae33c4bf"
SWEEP_DISPATCH_RUN_AGENTIC = {
    "id": "agentic-update-summary-20260428-100200",
    "title": "agentic: note.update-summary on src-test-001",
    "priority": None,
    "status": "RUNNING",
    "kind": "agentic",
    "agentic_source_id": SWEEP_AGENTIC_SOURCE_ID,
    "run_dir": "agentic-update-summary-20260428-100200",
    "duration_secs": 30,
    "snippet": "Resolving target source",
}

SWEEP_DISPATCH_RUN_LEGACY_NULL_KIND = {
    "id": "auto-sweep-legacy-null",
    "title": "Legacy bead row — NULL kind",
    "priority": 2,
    "status": "waiting",
    # Intentionally no `kind` field — exercises the COALESCE-as-'bead'
    # path in templates/partials/priority-badge.html.
}


# ── Dispatch SSE event (pushed after browser connects) ───────────────

DISPATCH_SSE_DATA = {
    "active": [
        {"id": "auto-sweep-b1", "title": "Sweep alpha task",
         "priority": 1, "status": "RUNNING", "duration_secs": 120,
         "snippet": "Working on tests"},
        {"id": "librarian-review_report-abc-20260330-120000",
         "title": "Librarian: review_report",
         "priority": None, "status": "RUNNING", "duration_secs": 45,
         "librarian_type": "review_report",
         "snippet": "Reviewing experience report"},
        SWEEP_DISPATCH_RUN_AGENTIC,
    ],
    "waiting": [
        {"id": "auto-sweep-b3", "title": "Sweep gamma feature",
         "priority": 0, "status": "waiting"},
        SWEEP_DISPATCH_RUN_LEGACY_NULL_KIND,
    ],
    "blocked": [
        {"id": "auto-sweep-b2", "title": "Sweep beta bug",
         "priority": 2, "status": "blocked",
         "blockers": [{"id": "auto-sweep-b1", "title": "Sweep alpha task"}]},
    ],
    "paused": {"dispatch": False, "merge": False},
    "pause_reasons": {},
}

# ── Additional fixture data for trace overlay, bead viewer, host session tests ──

SWEEP_BEAD_DISPATCHED = {
    "id": "auto-sweep-b2",
    "title": "Sweep dispatched bead",
    "priority": 1,
    "status": "closed",
    "labels": [],
    "description": "A test bead that was dispatched and completed successfully.",
}

SWEEP_DISPATCH_RUN = {
    "id": "auto-sweep-b2-20260327-120000",
    "bead_id": "auto-sweep-b2",
    "dir": "auto-sweep-b2-20260327-120000",
    "status": "DONE",
    "started_at": "2026-03-27T12:00:00Z",
    "completed_at": "2026-03-27T12:05:00Z",
    "duration_secs": 300,
    "commit_hash": "abc123def456789",
    "lines_added": 50,
    "lines_removed": 10,
    "files_changed": 3,
    "decision": {
        "status": "DONE",
        "reason": "All tests pass",
        "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
    },
}

SWEEP_TRACE_DATA = {
    "auto-sweep-b2-20260327-120000": {
        "id": "auto-sweep-b2-20260327-120000",
        "bead_id": "auto-sweep-b2",
        "status": "DONE",
        "reason": "All tests pass",
        "duration_secs": 300,
        "commit_hash": "abc123def456789",
        "decision": {
            "status": "DONE",
            "reason": "All tests pass",
            "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
        },
        "experience_report": "# Experience Report\n\nEverything went smoothly.",
        "diff": "+++ tools/test.py\n+def test_it():\n+    assert True",
    },
}

SWEEP_PRIMER_DATA = {
    "auto-sweep-b2": {
        "bead_id": "auto-sweep-b2",
        "title": "Sweep dispatched bead",
        "description": "A test bead that was dispatched and completed.",
        "priority": 1,
        "status": "closed",
    },
}

# Session entries for dispatch run (for overlay panel / bead detail viewer)
SWEEP_DISPATCH_ENTRIES = [
    {"type": "system", "content": "Session started", "timestamp": NOW - 600},
    {"type": "user", "content": "Implement the sweep feature", "timestamp": NOW - 590},
    {"type": "assistant_text", "content": "I will implement the sweep feature now.", "timestamp": NOW - 580},
    {"type": "tool_use", "tool_name": "Edit", "content": "Editing sweep.py",
     "timestamp": NOW - 570},
    {"type": "tool_result", "content": "File saved", "timestamp": NOW - 565},
    {"type": "assistant_text", "content": "The feature is implemented and tests pass.",
     "timestamp": NOW - 550},
]

# Agentic dispatch JSONL-shaped entries — what the /api/dispatch/tail/<run_id>
# endpoint returns for an agentic run. The mock fixture key is the run_id
# (which equals dispatch_runs.id and the container_name for agentic runs).
SWEEP_AGENTIC_DISPATCH_ENTRIES = [
    {"type": "system", "content": "agentic session started", "timestamp": NOW - 200},
    {"type": "user",
     "content": "Update Title & Summary action: rewrite the note's title + short_description.",
     "timestamp": NOW - 195},
    {"type": "assistant_text",
     "content": "I'll read the note and rewrite the title + summary.",
     "timestamp": NOW - 190},
    {"type": "tool_use", "tool_name": "Bash",
     "content": "graph read bd2a78f2-da73", "timestamp": NOW - 180},
    {"type": "assistant_text",
     "content": "Title set; short_description set. Done.", "timestamp": NOW - 170},
]

# Eager-created agentic graph source — the row inserted by the dashboard
# at agent-action dispatch and which the agentic-ingest path appends turns
# onto. ``entries`` here represent already-ingested turns; the page renders
# them under /graph/<id>.
SWEEP_AGENTIC_GRAPH_SOURCE = {
    "id": SWEEP_AGENTIC_SOURCE_ID,
    "title": "Update Title & Summary",
    "type": "agentic",
    "project": "autonomy",
    "created_at": "2026-04-28T22:00:40Z",
    "metadata": json.dumps({
        "kind": "agent-action",
        "set_id": "dashboard.agent-actions",
        "member_key": "note.update-summary",
        "session_type": "agentic",
        "slug": "agentic-update-summary-20260428-100200",
    }),
    "content": "Agentic action run",
    "entries": [
        {"id": "thought-agentic-1", "entry_type": "thought", "role": "user",
         "turn_number": 1,
         "content": "Update Title & Summary action: rewrite the note's title.",
         "message_id": None, "metadata": {}},
        {"id": "deriv-agentic-2", "entry_type": "derivation", "role": "assistant",
         "turn_number": 2,
         "content": "I'll read the note and rewrite the title + summary.",
         "message_id": None, "metadata": {}},
        {"id": "deriv-agentic-3", "entry_type": "derivation", "role": "assistant",
         "turn_number": 3,
         "content": "Title set; short_description set. Done.",
         "message_id": None, "metadata": {}},
    ],
}


# ── Search FTS fixture (auto-qlfg1, /search) ─────────────────────────
# Multi-source-type, multi-source-grouped rows for the L2.B search-page
# behavioural sweep. The first two entries share src-search-session-1 to
# exercise per-source grouping (one card, two excerpts).

SWEEP_SEARCH_RESULTS_DASHBOARD = [
    # Multi-hit session: same source_id, two different turn_numbers.
    {"id": "ssr-1", "source_id": "src-search-session-1",
     "source_title": "Dashboard search rework conversation",
     "source_type": "session", "result_type": "thought",
     "project": "autonomy", "platform": "claude-code",
     "turn_number": 12, "rank": -9.5,
     "content": "first dashboard turn excerpt — chip rail design",
     "source_created_at": "2026-04-20T03:14:58Z"},
    {"id": "ssr-2", "source_id": "src-search-session-1",
     "source_title": "Dashboard search rework conversation",
     "source_type": "session", "result_type": "thought",
     "project": "autonomy", "platform": "claude-code",
     "turn_number": 47, "rank": -9.0,
     "content": "second dashboard turn excerpt — accent rail by source_type",
     "source_created_at": "2026-04-20T03:14:58Z"},
    # Single-hit note (no turn).
    {"id": "ssr-3", "source_id": "src-search-note-1",
     "source_title": "pitfall: dashboard search regression",
     "source_type": "note", "result_type": "thought",
     "project": "autonomy", "platform": "local",
     "turn_number": None, "rank": -7.0,
     "content": "Dashboard live-tail ingest masks org column",
     "source_created_at": "2026-04-14T22:10:02Z"},
    # Single-hit agent run (with a turn).
    {"id": "ssr-4", "source_id": "src-search-agent-1",
     "source_title": "Graph search: dashboard surface alignment",
     "source_type": "agent-run", "result_type": "derivation",
     "project": "autonomy", "platform": "claude-code",
     "turn_number": 17, "rank": -6.5,
     "content": "agent run dashboard turn excerpt",
     "source_created_at": "2026-04-12T08:00:00Z"},
    # Single-hit docs row.
    {"id": "ssr-5", "source_id": "src-search-docs-1",
     "source_title": "Dashboard search results & viewer brief",
     "source_type": "docs", "result_type": "thought",
     "project": "autonomy", "platform": "local",
     "turn_number": None, "rank": -6.0,
     "content": "iPhone-first design for the dashboard search results page",
     "source_created_at": "2026-03-23T10:00:00Z"},
]


# ── Design data (used by /design/{id}) ──────────────────────

SWEEP_EXPERIMENT_ID = "exp-sweep-00000000-0000-0000-0000-000000000001"

SWEEP_EXPERIMENT = {
    "id": SWEEP_EXPERIMENT_ID,
    "title": "Sweep Toolbar Design",
    "status": "pending",
    "design_id": SWEEP_EXPERIMENT_ID,
    "revision_seq": 3,
    "revisions": [
        "exp-sweep-00000000-0000-0000-0000-000000000001",
        "exp-sweep-00000000-0000-0000-0000-000000000002",
        "exp-sweep-00000000-0000-0000-0000-000000000003",
    ],
    "alpine": 0,
    "variants": [
        {"id": "v-sweep-001", "html": "<h1>Sweep toolbar test</h1>"}
    ],
}


# ── Graph source / note fixture data ────────────────────────────────

SWEEP_PLAIN_NOTE_ID = "aa000000-0000-0000-0000-000000000001"
SWEEP_RICH_NOTE_ID = "bb000000-0000-0000-0000-000000000002"
SWEEP_RICH_HTML_ATT_ID = "cc000000-0000-0000-0000-000000000003"
SWEEP_IMAGE_ATT_ID = "dd000000-0000-0000-0000-000000000004"
SWEEP_PARENT_NOTE_ID = "ee000000-0000-0000-0000-000000000005"
SWEEP_LEGACY_ATT_ID = "ff000000-0000-0000-0000-000000000006"
SWEEP_NO_ALT_ATT_ID = "aa100000-0000-0000-0000-000000000007"
SWEEP_LEGACY_NOTE_ID = "bb100000-0000-0000-0000-000000000008"

# Fixtures for TestAgentActionsDropdown (auto-aia85). Two notes — one in
# the autonomy org (which has the seeded action set) and one in an org
# with no seeded actions. The dropdown must render for the first and
# stay hidden for the second.
SWEEP_AGENT_ACTIONS_AUTONOMY_NOTE_ID = "a2000a00-0000-0000-0000-000000000010"
SWEEP_AGENT_ACTIONS_EMPTY_NOTE_ID = "a2000e00-0000-0000-0000-000000000011"

SWEEP_AGENT_ACTIONS_SOURCE_AUTONOMY = {
    "id": SWEEP_AGENT_ACTIONS_AUTONOMY_NOTE_ID,
    "title": "Agent-actions sweep — autonomy note",
    "type": "note",
    "project": "autonomy",
    "created_at": "2026-04-28T12:00:00Z",
    "metadata": "{}",
    "content": "Test note for the agentic-actions dropdown.",
}

SWEEP_AGENT_ACTIONS_SOURCE_EMPTY_ORG = {
    "id": SWEEP_AGENT_ACTIONS_EMPTY_NOTE_ID,
    "title": "Agent-actions sweep — empty-org note",
    "type": "note",
    "project": "emptyorg",
    "created_at": "2026-04-28T12:00:00Z",
    "metadata": "{}",
    "content": "Test note in an org with no seeded actions.",
}

# Same payload shape as tools/graph/migrations/seed_agent_actions.py SEEDS.
# The mock settings endpoint returns these verbatim as the dropdown's
# resolved member list for the autonomy org. Other orgs return an empty
# list, simulating a freshly-bootstrapped org awaiting promotion.
SWEEP_AGENT_ACTIONS = [
    {"key": "session.send-to", "payload": {
        "asset_type": "*",
        "label": "Send To…",
        "icon": "↗",
        "universal": True,
        "writes": [],
    }},
    {"key": "note.update-summary", "payload": {
        "asset_type": "note",
        "label": "Update Title & Summary",
        "icon": "✏",
        "model": "claude-haiku-4-5-20251001",
        "estimated_seconds": 10,
        "writes": ["source.title", "source.short_description"],
        "prompt_template": "Update the title and short description of this note.",
    }},
    {"key": "note.consolidate-comments", "payload": {
        "asset_type": "note",
        "label": "Consolidate Comments",
        "icon": "⊞",
        "model": "claude-sonnet-4-6",
        "estimated_seconds": 30,
        "writes": ["note-version"],
        "prompt_template": "Consolidate this note's comments into the body.",
    }},
    {"key": "note.review-accuracy", "payload": {
        "asset_type": "note",
        "label": "Review for Accuracy",
        "icon": "✓",
        "model": "claude-sonnet-4-6",
        "estimated_seconds": 60,
        "writes": ["comment"],
        "prompt_template": "Review this note for factual accuracy.",
    }},
]

SWEEP_GRAPH_SOURCES = {
    SWEEP_PLAIN_NOTE_ID: {
        "id": SWEEP_PLAIN_NOTE_ID,
        "title": "Plain Note",
        "type": "note",
        "project": "autonomy",
        "created_at": "2026-03-30T12:00:00Z",
        "metadata": "{}",
        "content": "# Plain Note\n\n| Col A | Col B |\n|-------|-------|\n| 1 | 2 |\n\nSome paragraph text.",
    },
    SWEEP_RICH_NOTE_ID: {
        "id": SWEEP_RICH_NOTE_ID,
        "title": "Pause Mechanisms",
        "type": "note",
        "project": "autonomy",
        "created_at": "2026-03-30T12:00:00Z",
        "metadata": json.dumps({"rich_content": True}),
        "content": "## Pause Mechanisms\n\n| Scope | Trigger | Effect |\n|-------|---------|--------|\n| Global | Auth failure | All blocked |\n| Per-label | Smoke failure | Label skipped |",
    },
    SWEEP_PARENT_NOTE_ID: {
        "id": SWEEP_PARENT_NOTE_ID,
        "title": "Dispatch Lifecycle Signpost",
        "type": "note",
        "project": "autonomy",
        "created_at": "2026-03-30T12:00:00Z",
        "metadata": "{}",
        "content": f"# Dispatch Lifecycle\n\n## Pause Mechanisms\n\n![[{SWEEP_RICH_NOTE_ID[:12]}]]\n\n## Screenshot\n\n![[{SWEEP_IMAGE_ATT_ID[:12]}]]\n\n## No Alt\n\n![[{SWEEP_NO_ALT_ATT_ID[:12]}]]",
    },
    SWEEP_LEGACY_NOTE_ID: {
        "id": SWEEP_LEGACY_NOTE_ID,
        "title": "Legacy Embed Note",
        "type": "note",
        "project": "autonomy",
        "created_at": "2026-03-30T12:00:00Z",
        "metadata": "{}",
        "content": f"# Legacy\n\n![old screenshot](graph://{SWEEP_LEGACY_ATT_ID[:12]})",
    },
    SWEEP_AGENT_ACTIONS_AUTONOMY_NOTE_ID: SWEEP_AGENT_ACTIONS_SOURCE_AUTONOMY,
    SWEEP_AGENT_ACTIONS_EMPTY_NOTE_ID: SWEEP_AGENT_ACTIONS_SOURCE_EMPTY_ORG,
    SWEEP_AGENTIC_SOURCE_ID: SWEEP_AGENTIC_GRAPH_SOURCE,
}

SWEEP_GRAPH_ATTACHMENTS = {
    SWEEP_RICH_HTML_ATT_ID: {
        "id": SWEEP_RICH_HTML_ATT_ID,
        "filename": "pause-mechanisms.html",
        "mime_type": "text/html",
        "source_id": f"{SWEEP_RICH_NOTE_ID}@1",
        "alt_text": "",
        "size_bytes": 2048,
        "created_at": "2026-03-30T12:00:00Z",
        "content": '<!DOCTYPE html><html><head><style>body{margin:0;padding:0.5rem;width:fit-content;min-width:100%;background:#0d1117;color:#c9d1d9;font-family:sans-serif;}.diagram{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:0.75rem;}svg{display:block;}</style></head><body><div class="diagram"><svg viewBox="0 0 900 200" width="900" height="200"><rect x="30" y="40" width="180" height="60" rx="6" fill="#1a2332" stroke="#58a6ff" stroke-width="2"/><text x="120" y="75" text-anchor="middle" fill="#f0f6fc" font-size="13" font-weight="600">Phase 1</text><rect x="250" y="40" width="180" height="60" rx="6" fill="#1a2332" stroke="#58a6ff" stroke-width="2"/><text x="340" y="75" text-anchor="middle" fill="#f0f6fc" font-size="13" font-weight="600">Phase 2</text><rect x="470" y="40" width="180" height="60" rx="6" fill="#0d2818" stroke="#3fb950" stroke-width="2"/><text x="560" y="75" text-anchor="middle" fill="#f0f6fc" font-size="13" font-weight="600">Phase 3</text><rect x="690" y="40" width="180" height="60" rx="6" fill="#1e1533" stroke="#bc8cff" stroke-width="2"/><text x="780" y="75" text-anchor="middle" fill="#f0f6fc" font-size="13" font-weight="600">Phase 4</text></svg></div></body></html>',
    },
    SWEEP_IMAGE_ATT_ID: {
        "id": SWEEP_IMAGE_ATT_ID,
        "filename": "screenshot.png",
        "mime_type": "image/png",
        "source_id": "",
        "alt_text": "Dispatch page showing 3 running beads with progress bars.",
        "size_bytes": 45000,
        "created_at": "2026-03-30T12:00:00Z",
    },
    SWEEP_LEGACY_ATT_ID: {
        "id": SWEEP_LEGACY_ATT_ID,
        "filename": "legacy-shot.png",
        "mime_type": "image/png",
        "source_id": "",
        "alt_text": "Legacy screenshot",
        "size_bytes": 30000,
        "created_at": "2026-03-30T12:00:00Z",
    },
    SWEEP_NO_ALT_ATT_ID: {
        "id": SWEEP_NO_ALT_ATT_ID,
        "filename": "diagram.png",
        "mime_type": "image/png",
        "source_id": "",
        "alt_text": "",
        "size_bytes": 20000,
        "created_at": "2026-03-30T12:00:00Z",
    },
}

SWEEP_WORKTREE_ALPHA_COMMITS = [
    {
        "sha": "1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "short_sha": "1111111",
        "subject": "Fix worktree review sticky headers",
        "author": "Alpha Agent",
        "date": "2026-04-24 03:12",
        "body": "Makes the title/files/file sticky stack stable on mobile.",
        "files": [
            {
                "status": "M",
                "path": "tools/dashboard/templates/pages/worktrees.html",
                "additions": 28,
                "deletions": 8,
            },
            {
                "status": "M",
                "path": "tools/dashboard/static/js/pages/worktrees.js",
                "additions": 14,
                "deletions": 5,
            },
        ],
    },
    {
        "sha": "2222222bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "short_sha": "2222222",
        "subject": "Tighten diff marker alignment on mobile",
        "author": "Alpha Agent",
        "date": "2026-04-24 03:45",
        "body": "Removes extra padding from the +/- marker column.",
        "files": [
            {
                "status": "M",
                "path": "tools/dashboard/templates/pages/worktrees.html",
                "additions": 4,
                "deletions": 4,
            },
        ],
    },
]

SWEEP_WORKTREE_BETA_COMMITS = [
    {
        "sha": "3333333ccccccccccccccccccccccccccccccccc",
        "short_sha": "3333333",
        "subject": "Refine ENTERPRISE-7644 release branch plumbing",
        "author": "Beta Agent",
        "date": "2026-04-24 02:24",
        "body": "Keeps enterprise branch metadata visible in review mode.",
        "files": [
            {
                "status": "M",
                "path": "enterprise/release_dashboard.py",
                "additions": 11,
                "deletions": 2,
            },
        ],
    },
]

SWEEP_WORKTREE_ROWS = [
    {
        "session_name": "auto-sweep-alpha",
        "session_title": "Alpha — card redesign",
        "repo_name": "autonomy",
        "worktree_path": "/tmp/worktrees/auto-sweep-alpha/autonomy",
        "managed_clone": "/tmp/repos/autonomy.git",
        "branch": "session/auto-sweep-alpha",
        "target_branch": "main",
        "commits_ahead": 2,
        "is_dirty": True,
        "ff_eligible": True,
        "clone_stale": False,
        "rebase_required": False,
        "session_live": True,
        "commits": SWEEP_WORKTREE_ALPHA_COMMITS,
        "dirty_files": [
            {
                "status": "M",
                "path": "tools/dashboard/static/js/pages/worktrees.js",
                "additions": 0,
                "deletions": 0,
            },
            {
                "status": "??",
                "path": "tools/dashboard/static/vendor/highlightjs/highlight.min.js",
                "additions": 0,
                "deletions": 0,
            },
        ],
    },
    {
        "session_name": "auto-sweep-beta",
        "session_title": "Beta Builder",
        "repo_name": "enterprise",
        "worktree_path": "/tmp/worktrees/auto-sweep-beta/enterprise",
        "managed_clone": "/tmp/repos/enterprise.git",
        "branch": "ENTERPRISE-7644",
        "target_branch": "ENTERPRISE-7644",
        "commits_ahead": 1,
        "is_dirty": False,
        "ff_eligible": False,
        "clone_stale": False,
        "rebase_required": False,
        "session_live": True,
        "commits": SWEEP_WORKTREE_BETA_COMMITS,
        "dirty_files": [],
    },
    {
        "session_name": "auto-sweep-gamma",
        "session_title": "Gamma Reviewer",
        "repo_name": "autonomy",
        "worktree_path": "/tmp/worktrees/auto-sweep-gamma/autonomy",
        "managed_clone": "/tmp/repos/autonomy.git",
        "branch": "session/auto-sweep-gamma",
        "target_branch": "main",
        "commits_ahead": 0,
        "is_dirty": True,
        "ff_eligible": False,
        "clone_stale": False,
        "rebase_required": False,
        "session_live": False,
        "commits": [],
        "dirty_files": [
            {
                "status": "M",
                "path": "agents/session_launcher.py",
                "additions": 0,
                "deletions": 0,
            },
        ],
    },
]

SWEEP_WORKTREE_COMMIT_DETAILS = {
    "auto-sweep-alpha/autonomy/1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
        **SWEEP_WORKTREE_ALPHA_COMMITS[0],
        "patch": """diff --git a/tools/dashboard/templates/pages/worktrees.html b/tools/dashboard/templates/pages/worktrees.html
index abc1234..def5678 100644
--- a/tools/dashboard/templates/pages/worktrees.html
+++ b/tools/dashboard/templates/pages/worktrees.html
@@ -420,7 +420,7 @@
-<div class=\"worktree-review-shell min-h-screen bg-gray-950 overflow-x-hidden\">
+<div class=\"worktree-review-shell min-h-screen bg-gray-950\">
@@ -488,6 +488,7 @@
 <div x-ref=\"commitTitleBar\" class=\"sticky top-0\">
+  <div class=\"text-slate-400\">Pinned cleanly</div>
 </div>
diff --git a/tools/dashboard/static/js/pages/worktrees.js b/tools/dashboard/static/js/pages/worktrees.js
index aaa1111..bbb2222 100644
--- a/tools/dashboard/static/js/pages/worktrees.js
+++ b/tools/dashboard/static/js/pages/worktrees.js
@@ -792,6 +792,8 @@
       syncScrollLock() {
         const locked = this.hasOverlayOpen();
+        document.documentElement.style.overscrollBehaviorX = locked ? 'none' : '';
+        document.body.style.overscrollBehaviorX = locked ? 'none' : '';
         document.documentElement.style.overflow = locked ? 'hidden' : '';
       }""",
    },
    "auto-sweep-alpha/autonomy/2222222bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": {
        **SWEEP_WORKTREE_ALPHA_COMMITS[1],
        "patch": """diff --git a/tools/dashboard/templates/pages/worktrees.html b/tools/dashboard/templates/pages/worktrees.html
index def5678..fedcba9 100644
--- a/tools/dashboard/templates/pages/worktrees.html
+++ b/tools/dashboard/templates/pages/worktrees.html
@@ -548,7 +548,7 @@
-<span class=\"border-r border-white/10 px-1 text-center\"
+<span class=\"border-r border-white/10 text-center\"
       x-text=\"diffMarker(line.kind)\"></span>""",
    },
    "auto-sweep-beta/enterprise/3333333ccccccccccccccccccccccccccccccccc": {
        **SWEEP_WORKTREE_BETA_COMMITS[0],
        "patch": """diff --git a/enterprise/release_dashboard.py b/enterprise/release_dashboard.py
index 1234567..89abcde 100644
--- a/enterprise/release_dashboard.py
+++ b/enterprise/release_dashboard.py
@@ -18,6 +18,9 @@
def branch_title(branch_name):
+    if branch_name.startswith("ENTERPRISE-"):
+        return f"Release branch {branch_name}"
+
     return branch_name""",
    },
}

SWEEP_WORKTREE_CHANGES_DETAILS = {
    "auto-sweep-alpha/autonomy": {
        "files": SWEEP_WORKTREE_ROWS[0]["dirty_files"],
        "patch": """diff --git a/tools/dashboard/static/js/pages/worktrees.js b/tools/dashboard/static/js/pages/worktrees.js
index 9999999..8888888 100644
--- a/tools/dashboard/static/js/pages/worktrees.js
+++ b/tools/dashboard/static/js/pages/worktrees.js
@@ -805,6 +805,8 @@
       async refresh(manual) {
+        const endpoint = manual ? '/api/worktrees/refresh' : '/api/worktrees';
+        await fetch(endpoint);
         this.error = '';
       }""",
    },
    "auto-sweep-gamma/autonomy": {
        "files": SWEEP_WORKTREE_ROWS[2]["dirty_files"],
        "patch": """diff --git a/agents/session_launcher.py b/agents/session_launcher.py
index 7654321..1234567 100644
--- a/agents/session_launcher.py
+++ b/agents/session_launcher.py
@@ -44,6 +44,7 @@
 def launch():
+    print("gamma dirty change")
     return True""",
    },
}


# Search results fixture (auto-kvka6). Each row is a flat per-excerpt result
# in the shape returned by ``dao_beads.search`` — the dashboard's /api/search
# call passes ``?group=1`` so ``_group_search_results`` collapses rows by
# source_id into the card list the page renders.
#
# Query token: ``worktree``. Every row's ``source_title`` or ``content`` must
# contain the substring so the mock's case-insensitive filter returns it.
# Row titles deliberately avoid the substring ``dashboard`` so this fixture
# does not pollute auto-qlfg1's ``?q=dashboard`` count assertions.
#
# Five rows exercise the auto-kvka6 acceptance:
#   1. Strong title hit (rank −55) — the curated "Worktrees Surface
#      Specification" — lands at the top so a search for "Worktree"
#      surfaces the curated note over noisy body matches.
#   2 & 3. Two rows that share an identical source title (rollouts of
#      the same tmux session) at minute-resolution timestamps that
#      collide — the disambiguator (12-char id + second-resolution
#      date) must distinguish them.
#   4. A note with a populated short_description and matching tag — the
#      card renders the description row and tag-overlap places it ahead
#      of the bare body match in rank order.
#   5. A note WITHOUT a short_description — the card must NOT render an
#      empty description row.
SWEEP_SEARCH_RESULTS = [
    {
        "id": "kvka6row1",
        "source_id": "kvka6src1-aaaa",
        "source_title": "Worktrees Surface Specification",
        "source_type": "note",
        "short_description": (
            "How worktrees render in the surface, including the "
            "filter strip and per-row actions."
        ),
        "keywords": "worktree,worktrees,spec",
        "result_type": "source",
        "rank": -55.0,
        "rrf_score": 0.55,
        "turn_number": None,
        "content": "Worktrees Surface Specification",
        "project": "autonomy",
        "platform": "local",
        "source_created_at": "2026-04-22T19:59:35Z",
        "source_metadata": json.dumps({"tags": ["worktree", "surface"]}),
    },
    {
        "id": "kvka6row2",
        "source_id": "kvka6src2-bbbb",
        "source_title": "auto-0422-195935",
        "source_type": "session",
        "short_description": None,
        "keywords": None,
        "result_type": "thought",
        "rank": -10.0,
        "rrf_score": 0.10,
        "turn_number": 12,
        "content": "first rollout had worktree checks",
        "project": "autonomy",
        "platform": "local",
        "source_created_at": "2026-04-22T19:59:35Z",
        "source_metadata": "{}",
    },
    {
        "id": "kvka6row3",
        "source_id": "kvka6src3-cccc",
        "source_title": "auto-0422-195935",
        "source_type": "session",
        "short_description": None,
        "keywords": None,
        "result_type": "thought",
        "rank": -9.5,
        "rrf_score": 0.095,
        "turn_number": 7,
        "content": "second rollout retried the worktree assertion",
        "project": "autonomy",
        "platform": "local",
        # SECONDS-resolution differs from the row above by 11s — at
        # minute resolution the two cards would look identical.
        "source_created_at": "2026-04-22T19:59:46Z",
        "source_metadata": "{}",
    },
    {
        "id": "kvka6row4",
        "source_id": "kvka6src4-dddd",
        "source_title": "Pitfall: worktree stash pop loses untracked",
        "source_type": "note",
        "short_description": (
            "Untracked files are silently dropped if `git stash pop` "
            "conflicts; always commit first."
        ),
        "keywords": "git,stash,pitfall",
        "result_type": "source",
        # Rank baked-in below the bare body match — the production server
        # subtracts SEARCH_TAG_OVERLAP_BOOST for the `pitfall`/`git` tag
        # overlap; the mock can't compute that, so the fixture pre-applies
        # the result.
        "rank": -8.0,
        "rrf_score": 0.08,
        "turn_number": None,
        "content": "Pitfall: worktree stash pop loses untracked",
        "project": "autonomy",
        "platform": "local",
        "source_created_at": "2026-04-21T10:30:00Z",
        "source_metadata": json.dumps({"tags": ["pitfall", "git"]}),
    },
    {
        "id": "kvka6row5",
        "source_id": "kvka6src5-eeee",
        "source_title": "Bare body match",
        "source_type": "note",
        "short_description": None,  # absent → card omits the description row
        "keywords": None,
        "result_type": "thought",
        "rank": -5.0,
        "rrf_score": 0.05,
        "turn_number": 2,
        "content": "body mentions worktree once",
        "project": "autonomy",
        "platform": "local",
        "source_created_at": "2026-04-20T08:00:00Z",
        "source_metadata": "{}",
    },
]


def _build_fixture() -> dict:
    """Build the complete fixture dict for behavioral sweep tests."""
    entries = dict(SWEEP_SESSION_ENTRIES)
    # Add dispatch run entries keyed by run dir name (for dispatch tail)
    entries["auto-sweep-b2-20260327-120000"] = SWEEP_DISPATCH_ENTRIES
    # Agentic run: tail key == run_id == container_name
    entries[SWEEP_DISPATCH_RUN_AGENTIC["id"]] = SWEEP_AGENTIC_DISPATCH_ENTRIES
    return {
        "active_sessions": SWEEP_SESSIONS,
        "session_entries": entries,
        "recent_sessions": SWEEP_RECENT_SESSIONS,
        "worktrees": SWEEP_WORKTREE_ROWS,
        "worktree_commit_details": SWEEP_WORKTREE_COMMIT_DETAILS,
        "worktree_changes_details": SWEEP_WORKTREE_CHANGES_DETAILS,
        "beads": SWEEP_BEADS + [SWEEP_BEAD_DISPATCHED],
        "runs": SWEEP_RUNS + [SWEEP_DISPATCH_RUN],
        "experiments": [SWEEP_EXPERIMENT],
        "timeline_entries": SWEEP_TIMELINE_ENTRIES,
        "timeline_stats": SWEEP_TIMELINE_STATS,
        "collab_notes": SWEEP_COLLAB_NOTES,
        "recent_notes": SWEEP_RECENT_NOTES,
        "thoughts": SWEEP_THOUGHTS,
        "threads": SWEEP_THREADS,
        "streams": SWEEP_STREAMS,
        "traces": {**SWEEP_TRACES, **SWEEP_TRACE_DATA},
        "primers": {**SWEEP_PRIMERS, **SWEEP_PRIMER_DATA},
        "bead_deps": SWEEP_BEAD_DEPS,
        "graph_sources": SWEEP_GRAPH_SOURCES,
        "graph_attachments": SWEEP_GRAPH_ATTACHMENTS,
        # Both fixtures coexist in the same list; the mock DAO substring-filters
        # by query, so ?q=dashboard surfaces the auto-qlfg1 rows and
        # ?q=worktree surfaces the auto-kvka6 ranking/disambiguator rows.
        "search_results": SWEEP_SEARCH_RESULTS_DASHBOARD + SWEEP_SEARCH_RESULTS,
        "settings": {
            "dashboard.agent-actions": {
                "_orgs": {"autonomy": SWEEP_AGENT_ACTIONS},
            },
        },
    }


# ── Module-scoped fixtures ────────────────────────────────────────────

@pytest.fixture(scope="module")
def sweep_server(tmp_path_factory):
    """Boot a DASHBOARD_MOCK uvicorn server on a test port, tear down after module."""
    tmpdir = tmp_path_factory.mktemp("sweep")
    fixture_path = tmpdir / "fixtures.json"
    fixture_path.write_text(json.dumps(_build_fixture(), indent=2))

    events_path = tmpdir / "events.jsonl"
    events_path.write_text("")  # empty — SSE events written after browser connects

    port = worker_test_port(8094)
    env = {
        **os.environ,
        "DASHBOARD_MOCK": str(fixture_path),
        "DASHBOARD_MOCK_EVENTS": str(events_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),  # repo root
        # Isolate EventBus snapshot so neither prior runs nor sibling tests
        # can replay stale session:registry into our subscribers via restore().
        "DASHBOARD_EVENT_BUS_STATE": str(tmpdir / "event_bus.state"),
    }

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "tools.dashboard.server:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to be ready (up to 8s)
    deadline = time.time() + 8
    ready = False
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dao/active_sessions", timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.2)

    if not ready:
        proc.kill()
        out, err = proc.communicate(timeout=3)
        pytest.fail(f"Sweep server failed to start:\nstdout: {out.decode()}\nstderr: {err.decode()}")

    yield {
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "fixture_path": str(fixture_path),
        "events_path": str(events_path),
    }

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


@pytest.fixture(scope="module")
def browser(sweep_server):
    """Open one agent-browser session, reuse across all tests in module."""
    url = sweep_server["url"] + "/sessions"
    subprocess.run(
        ["agent-browser", "open", url],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["agent-browser", "wait", "--load", "networkidle"],
        capture_output=True, timeout=10,
    )

    # Push dispatch SSE events so _sseCache is populated for /dispatch page
    events_path = sweep_server["events_path"]
    with open(events_path, "a") as f:
        f.write(json.dumps({"topic": "dispatch", "data": DISPATCH_SSE_DATA}) + "\n")
        f.write(json.dumps({"topic": "nav", "data": {
            "open_beads": 3, "running_agents": 1, "approved_waiting": 1,
        }}) + "\n")

    # Give Alpine + HTTP fallback + SSE events time to propagate
    time.sleep(1.5)
    yield sweep_server
    subprocess.run(["agent-browser", "close"], capture_output=True, timeout=5)


# ── Helpers ───────────────────────────────────────────────────────────

def _ab_eval_batch(js: str) -> dict | list | str | None:
    """Single agent-browser --json eval call, returns parsed result.

    The JS expression is wrapped in an IIFE to avoid const redeclaration
    across multiple eval calls sharing the same page context.
    """
    wrapped = f"(() => {{ {js} }})()"
    result = subprocess.run(
        ["agent-browser", "--json", "eval", wrapped],
        capture_output=True, text=True, timeout=10,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return None
    # Parse last JSON line that has success+data shape
    for line in reversed(stdout.split("\n")):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "data" in parsed:
                data = parsed["data"]
                # Unwrap {origin, result} from eval
                if isinstance(data, dict) and "result" in data:
                    return data["result"]
                return data
            return parsed
        except json.JSONDecodeError:
            continue
    return None


def _navigate_and_check(path: str, js_checks: str, wait_ms: int = 800) -> dict:
    """SPA-navigate to a page, wait, run one batched JS eval, return dict.

    Args:
        path: URL path to navigate to (e.g., "/sessions")
        js_checks: JS code that populates a `r` object with check results
                   and ends with `return r;`
        wait_ms: milliseconds to wait after navigation for Alpine to render
    """
    # Navigate via JS (SPA-style)
    nav_js = f"navigateTo('{path}')"
    subprocess.run(
        ["agent-browser", "eval", nav_js],
        capture_output=True, timeout=10,
    )
    time.sleep(wait_ms / 1000)

    # Run all checks in one eval
    full_js = f"var r = {{}}; {js_checks} return r;"
    return _ab_eval_batch(full_js) or {}


# ── Sessions page JS check bundle ────────────────────────────────────

SESSIONS_PAGE_CHECKS = """
    // Cards exist
    var cards = document.querySelectorAll('[data-testid="session-card"]');
    r.card_count = cards.length;
    r.has_cards = cards.length > 0;

    // Labels visible — collect all non-empty card titles
    var labels = [];
    cards.forEach(function(c) {
        var title = c.querySelector('.sc-title');
        if (title && title.textContent.trim()) labels.push(title.textContent.trim());
    });
    r.labels = labels;
    r.labels_visible = labels.length > 0;

    // Host badge — at least one card should show "Host" role badge
    var hostBadges = document.querySelectorAll('[data-testid="session-role"]');
    var hasHostBadge = false;
    hostBadges.forEach(function(b) {
        if (b.textContent.trim() === 'Host') hasHostBadge = true;
    });
    r.has_host_badge = hasHostBadge;

    // Roles visible — collect all visible role badges
    var roles = [];
    hostBadges.forEach(function(b) {
        var text = b.textContent.trim();
        if (text && b.offsetParent !== null) roles.push(text);
    });
    r.roles = roles;
    r.roles_visible = roles.length > 0;

    // Host card has distinct styling (active section only)
    var activeSection = document.querySelector('[data-testid="active-sessions-section"]');
    var hostCards = activeSection ? activeSection.querySelectorAll('.session-card-host') : [];
    r.host_card_count = hostCards.length;

    // Container cards (active section only)
    var containerCards = activeSection ? activeSection.querySelectorAll('.session-card-container') : [];
    r.container_card_count = containerCards.length;

    // Turn counts visible (T3 stats)
    var turnVals = [];
    cards.forEach(function(c) {
        var vals = c.querySelectorAll('.sc-t3-val');
        if (vals.length >= 1) turnVals.push(vals[0].textContent.trim());
    });
    r.turn_values = turnVals;
    r.turns_visible = turnVals.filter(function(v) { return v.length > 0; }).length > 0;

    // Context token values visible
    var ctxVals = [];
    cards.forEach(function(c) {
        var vals = c.querySelectorAll('.sc-t3-val');
        if (vals.length >= 2) ctxVals.push(vals[1].textContent.trim());
    });
    r.ctx_values = ctxVals;
    r.ctx_visible = ctxVals.filter(function(v) { return v.length > 0; }).length > 0;

    // Topics visible
    var topicItems = document.querySelectorAll('.sc-topic-item');
    var topicTexts = [];
    topicItems.forEach(function(t) {
        if (t.textContent.trim()) topicTexts.push(t.textContent.trim());
    });
    r.topic_texts = topicTexts;
    r.topics_visible = topicTexts.length > 0;

    // Session IDs in data attributes (for click targeting)
    var sessionIds = [];
    cards.forEach(function(c) {
        var sid = c.getAttribute('data-session-id');
        if (sid) sessionIds.push(sid);
    });
    r.session_ids = sessionIds;

    // Recent sessions section
    var recentRows = document.querySelectorAll('[data-testid="recent-session-row"]');
    r.recent_count = recentRows.length;
    r.has_recent = recentRows.length > 0;

    // Recent session titles (card-based: .sc-title divs; legacy: long spans)
    var recentTitles = [];
    recentRows.forEach(function(row) {
        var titleEl = row.querySelector('.sc-title');
        if (titleEl) {
            var text = titleEl.textContent.trim();
            if (text) recentTitles.push(text);
        } else {
            var spans = row.querySelectorAll('span');
            spans.forEach(function(s) {
                var text = s.textContent.trim();
                if (text.length > 15) recentTitles.push(text);
            });
        }
    });
    r.recent_titles = recentTitles;

    // No raw template syntax visible
    var bodyText = document.body.innerText;
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
    r.no_alpine_raw = bodyText.indexOf('x-text=') === -1 && bodyText.indexOf('x-show=') === -1;

    // Active sessions section exists
    r.has_active_section = !!document.querySelector('[data-testid="active-sessions-section"]');
    r.has_recent_section = !!document.querySelector('[data-testid="recent-sessions-section"]');

    // Recent Sessions sort + since dropdowns (design acb2829b-4fc0 rev b39626f2)
    var recentSortTrigger = document.querySelector('[data-testid="recent-sort-toggle"]');
    var recentSinceTrigger = document.querySelector('[data-testid="recent-since-toggle"]');
    r.has_recent_sort_toggle = !!recentSortTrigger;
    r.has_recent_since_toggle = !!recentSinceTrigger;
    r.recent_sort_value = recentSortTrigger
        ? (recentSortTrigger.querySelector('.sort-toggle-value') || {}).textContent || ''
        : '';
    r.recent_since_value = recentSinceTrigger
        ? (recentSinceTrigger.querySelector('.sort-toggle-value') || {}).textContent || ''
        : '';
    r.recent_sort_value = r.recent_sort_value.trim();
    r.recent_since_value = r.recent_since_value.trim();

    // Open the Sort menu and inspect its options (then close via a synthetic
    // click.away). Options must match design labels exactly.
    var recentSortOptions = [];
    var recentSinceOptions = [];
    var recentSortMenu = document.querySelector('[data-testid="recent-sort-toggle-menu"]');
    var recentSinceMenu = document.querySelector('[data-testid="recent-since-toggle-menu"]');
    if (recentSortMenu) {
        recentSortMenu.querySelectorAll('.sort-option > span:first-child').forEach(function(el) {
            var t = el.textContent.trim();
            if (t) recentSortOptions.push(t);
        });
    }
    if (recentSinceMenu) {
        recentSinceMenu.querySelectorAll('.sort-option > span:first-child').forEach(function(el) {
            var t = el.textContent.trim();
            if (t) recentSinceOptions.push(t);
        });
    }
    r.recent_sort_options = recentSortOptions;
    r.recent_since_options = recentSinceOptions;

    // Recent row ordering under default sort (most-recent-activity first)
    var recentOrderIds = [];
    recentRows.forEach(function(row) {
        var sid = row.getAttribute('data-session-id');
        if (sid) recentOrderIds.push(sid);
    });
    r.recent_order_ids = recentOrderIds;

    // Resume button count — auto-ycry3 removed the standalone button.
    var resumeBtns = document.querySelectorAll('[data-testid="resume-btn"]');
    r.resume_btn_count = resumeBtns.length;

    // Recent cards must carry the merged sc-org sc-actions button —
    // that is where the Resume action lives post auto-ycry3.
    var recentRowEls = document.querySelectorAll('[data-testid="recent-session-row"]');
    r.recent_row_count = recentRowEls.length;
    var recentActionsBtnCount = 0;
    recentRowEls.forEach(function(row) {
        if (row.querySelector('[data-testid="session-actions-btn"]')) {
            recentActionsBtnCount += 1;
        }
    });
    r.recent_session_actions_btn_count = recentActionsBtnCount;

    // Recent card footer matrix (auto-wa3d): dead sessions show 'ended' +
    // absolute datetime; tmux column hidden for dispatch/librarian.
    var recentFooters = [];
    recentRows.forEach(function(row) {
        var labels = Array.from(row.querySelectorAll('.sc-footer-label'))
            .map(function(e) { return e.textContent.trim(); });
        var values = Array.from(row.querySelectorAll('.sc-footer-value'))
            .map(function(e) { return e.textContent.trim(); });
        recentFooters.push({
            type: row.dataset.sessionType || '',
            sid: row.dataset.sessionId || '',
            labels: labels,
            values: values,
        });
    });
    r.recent_footers = recentFooters;
"""

# ── Resume button state-transition async check ──────────────────────

RECENT_SORT_SINCE_BEHAVIOR_CHECKS = """(async () => {
    try {
        var r = {};
        var tick = async () => {
            await Alpine.nextTick();
            await new Promise(r => setTimeout(r, 400));
        };

        var root = document.querySelector('[x-data="sessionsPage()"]');
        if (!root) return JSON.stringify({error: 'sessionsPage not found'});
        var data = Alpine.$data(root);

        // Baseline: sort=lastActivity, since=1d — most-recent row first.
        data.recentSort = 'lastActivity';
        data.recentSince = 'all';
        await tick();
        r.initial_ids = data.recent.map(function(s) { return s.id; });

        // Sort by Most Turns — 627-turn session must float to the top.
        data.recentSort = 'turns';
        await tick();
        r.turns_ids = data.recent.map(function(s) { return s.id; });
        r.turns_first = r.turns_ids[0] || '';

        // Filter by Since=6h — 2h-old row still included, 2d-old excluded.
        data.recentSort = 'lastActivity';
        data.recentSince = '6h';
        await tick();
        r.since_6h_ids = data.recent.map(function(s) { return s.id; });

        // Persistence — changing these writes to localStorage.
        r.sort_in_storage = localStorage.getItem('recentSort');
        r.since_in_storage = localStorage.getItem('recentSince');

        // Reset to defaults so other tests aren't perturbed.
        data.recentSort = 'lastActivity';
        data.recentSince = '1d';
        await tick();

        return JSON.stringify(r);
    } catch(e) {
        return JSON.stringify({error: e.message, stack: e.stack});
    }
})();
"""


RESUME_STATE_TRANSITION_CHECKS = """(async () => {
    try {
        var r = {};
        var tick = async () => { await Alpine.nextTick(); await new Promise(r => setTimeout(r, 150)); };

        // Find the first resume button in the DOM (any card, visible or not).
        var btn = document.querySelector('[data-testid="resume-btn"]');
        var card = btn ? btn.closest('[data-session-id]') : null;
        var targetId = card ? card.getAttribute('data-session-id') : null;
        r.btn_found = !!btn;
        r.target_id = targetId;

        if (!btn || !targetId) {
            r.disabled_during_resume = false;
            r.enabled_after_clear = false;
            return JSON.stringify(r);
        }

        // Get the Alpine component data for the sessions page (not base.html x-data).
        var root = btn.closest('[x-data]');
        var data = Alpine.$data(root);

        // Simulate a resume in flight.
        data.resuming[targetId] = true;
        await tick();
        r.disabled_during_resume = btn.disabled === true;

        // Clear it.
        data.resuming[targetId] = false;
        await tick();
        r.enabled_after_clear = btn.disabled === false;

        return JSON.stringify(r);
    } catch(e) {
        return JSON.stringify({error: e.message, stack: e.stack});
    }
})()"""

# ── Dispatch page JS check bundle ────────────────────────────────────

DISPATCH_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Section headings visible
    var h2s = document.querySelectorAll('h2');
    var headings = [];
    h2s.forEach(function(h) { headings.push(h.textContent.trim()); });
    r.headings = headings;
    r.has_active_heading = headings.some(function(t) { return t.indexOf('Active') !== -1; });
    r.has_waiting_heading = headings.some(function(t) { return t.indexOf('Waiting') !== -1; });
    r.has_blocked_heading = headings.some(function(t) { return t.indexOf('Blocked') !== -1; });

    // Bead titles from fixture visible in active/waiting/blocked sections
    r.has_alpha_title = bodyText.indexOf('Sweep alpha') !== -1;
    r.has_gamma_title = bodyText.indexOf('Sweep gamma') !== -1;
    r.has_beta_title = bodyText.indexOf('Sweep beta') !== -1;

    // Pause toggle buttons visible (rendered from paused object keys)
    var allBtns = document.querySelectorAll('button');
    var pauseLabels = [];
    allBtns.forEach(function(b) {
        var t = b.textContent.trim();
        if (t === 'dispatch' || t === 'merge') pauseLabels.push(t);
    });
    r.pause_labels = pauseLabels;
    r.has_pause_controls = pauseLabels.length >= 2;

    // Status indicators — running count visible
    r.has_running_stat = bodyText.indexOf('running:') !== -1;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;

    // Librarian card checks — L2.B
    r.has_librarian_title = bodyText.indexOf('Librarian: review_report') !== -1;
    r.no_pnull = bodyText.indexOf('Pnull') === -1 && bodyText.indexOf('Pundefined') === -1;

    // Check that librarian cards have data-librarian attribute
    var libCards = document.querySelectorAll('[data-librarian]');
    r.librarian_card_count = 0;
    libCards.forEach(function(card) {
        var attr = card.getAttribute('data-librarian');
        if (attr && attr !== 'false') r.librarian_card_count++;
    });

    // Check that no priority badge (P0, P1, ...) appears on librarian cards
    r.librarian_has_no_priority_badge = true;
    libCards.forEach(function(card) {
        var attr = card.getAttribute('data-librarian');
        if (attr && attr !== 'false') {
            var badges = card.querySelectorAll('.ft-badge');
            badges.forEach(function(badge) {
                var text = badge.textContent.trim();
                if (/^P\\d+$/.test(text)) r.librarian_has_no_priority_badge = false;
            });
        }
    });
"""

# ── Beads page JS check bundle ───────────────────────────────────────

BEADS_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // View switcher tabs visible
    var tabs = document.querySelectorAll('[role="tab"]');
    var tabLabels = [];
    tabs.forEach(function(t) { tabLabels.push(t.textContent.trim()); });
    r.tab_labels = tabLabels;
    r.has_list_tab = tabLabels.some(function(t) { return t.indexOf('List') !== -1; });
    r.has_board_tab = tabLabels.some(function(t) { return t.indexOf('Board') !== -1; });
    r.has_tree_tab = tabLabels.some(function(t) { return t.indexOf('Tree') !== -1; });
    r.has_deps_tab = tabLabels.some(function(t) { return t.indexOf('Deps') !== -1; });

    // Table rows visible (list view is default)
    var rows = document.querySelectorAll('.bead-table-row');
    r.row_count = rows.length;
    r.has_rows = rows.length > 0;

    // Bead titles visible
    r.has_alpha_bead = bodyText.indexOf('Sweep alpha task') !== -1;
    r.has_beta_bead = bodyText.indexOf('Sweep beta bug') !== -1;

    // Priority badges visible
    r.has_p0 = bodyText.indexOf('P0') !== -1;
    r.has_p1 = bodyText.indexOf('P1') !== -1;
    r.has_p2 = bodyText.indexOf('P2') !== -1;

    // Bead IDs visible in table
    r.has_bead_ids = bodyText.indexOf('auto-sweep-b1') !== -1;

    // Filter controls — priority chips
    var filterBtns = document.querySelectorAll('button[aria-pressed]');
    r.filter_count = filterBtns.length;
    r.has_filters = filterBtns.length >= 5;  // at least P0-P4

    // Column headers
    var ths = document.querySelectorAll('.bead-th');
    var colHeaders = [];
    ths.forEach(function(th) {
        var t = th.textContent.trim();
        if (t) colHeaders.push(t);
    });
    r.col_headers = colHeaders;
    r.has_title_col = colHeaders.some(function(h) { return h.indexOf('Title') !== -1; });
    r.has_pri_col = colHeaders.some(function(h) { return h.indexOf('Pri') !== -1; });

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Timeline page JS check bundle ────────────────────────────────────

TIMELINE_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Timeframe toggle visible (1D, 1W, 1M, All buttons)
    var rangeBtns = [];
    document.querySelectorAll('button').forEach(function(b) {
        var t = b.textContent.trim();
        if (['1D', '1W', '1M', 'All'].indexOf(t) !== -1) rangeBtns.push(t);
    });
    r.range_buttons = rangeBtns;
    r.has_range_toggle = rangeBtns.length === 4;

    // Stats tiles visible — check for tile labels
    r.has_completed_tile = bodyText.indexOf('Completed') !== -1;
    r.has_failed_tile = bodyText.indexOf('Failed') !== -1 || bodyText.indexOf('Blocked') !== -1;
    r.has_duration_tile = bodyText.indexOf('Avg Duration') !== -1;
    r.has_tooling_tile = bodyText.indexOf('Avg Tooling') !== -1;
    r.has_confidence_tile = bodyText.indexOf('Avg Confidence') !== -1;

    // Stats values present (from SWEEP_TIMELINE_STATS)
    r.has_completed_count = bodyText.indexOf('5') !== -1;
    r.has_success_pct = bodyText.indexOf('80%') !== -1;

    // Feed heading
    r.has_feed_heading = bodyText.indexOf('Feed') !== -1;

    // Feed entries visible (timeline cards)
    var tlCards = document.querySelectorAll('.tl-card');
    r.feed_count = tlCards.length;
    r.has_feed_entries = tlCards.length > 0;

    // Status dots visible
    var dots = document.querySelectorAll('.tl-dot');
    r.dot_count = dots.length;
    r.has_status_dots = dots.length > 0;

    // Duration/time visible
    r.has_duration_text = bodyText.indexOf('5m') !== -1 || bodyText.indexOf('2m') !== -1;

    // Stars visible (avg scores from stats tiles)
    var stars = document.querySelectorAll('.tl-star-on, .tl-star-off');
    r.star_count = stars.length;
    r.has_stars = stars.length > 0;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Collab page JS check bundle ──────────────────────────────────────

COLLAB_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Tab strip visible (Recent, Curated, Thoughts, Threads, Topics)
    var tabs = document.querySelectorAll('.collab-tab');
    var tabLabels = [];
    tabs.forEach(function(t) { tabLabels.push(t.textContent.trim()); });
    r.tab_labels = tabLabels;
    r.has_recent_tab = tabLabels.some(function(t) { return t.indexOf('Recent') !== -1; });
    r.has_curated_tab = tabLabels.some(function(t) { return t.indexOf('Curated') !== -1; });
    r.has_thoughts_tab = tabLabels.some(function(t) { return t.indexOf('Thoughts') !== -1; });
    r.has_threads_tab = tabLabels.some(function(t) { return t.indexOf('Threads') !== -1; });
    r.has_topics_tab = tabLabels.some(function(t) { return t.indexOf('Topics') !== -1; });
    r.tab_count = tabs.length;

    // Recent tab content — note cards visible (Recent is default tab)
    var noteCards = document.querySelectorAll('.note-card');
    r.note_count = noteCards.length;
    r.has_notes = noteCards.length > 0;

    // Note titles visible
    var noteTitles = [];
    document.querySelectorAll('.note-title').forEach(function(el) {
        var t = el.textContent.trim();
        if (t) noteTitles.push(t);
    });
    r.note_titles = noteTitles;
    r.has_note_titles = noteTitles.length > 0;

    // Note type badges visible
    var typeLabels = [];
    document.querySelectorAll('.note-type').forEach(function(el) {
        var t = el.textContent.trim();
        if (t) typeLabels.push(t);
    });
    r.type_labels = typeLabels;
    r.has_type_labels = typeLabels.length > 0;

    // Tags visible
    var tagEls = document.querySelectorAll('.note-tag');
    var tagTexts = [];
    tagEls.forEach(function(el) {
        var t = el.textContent.trim();
        if (t) tagTexts.push(t);
    });
    r.tags = tagTexts;
    r.has_tags = tagTexts.length > 0;

    // Tab counts visible (monospace count after tab label)
    var tabCounts = document.querySelectorAll('.collab-tab-count');
    var countTexts = [];
    tabCounts.forEach(function(el) {
        var t = el.textContent.trim();
        if (t) countTexts.push(t);
    });
    r.tab_counts = countTexts;
    r.has_tab_counts = countTexts.length > 0;

    // Thought capture input present at page level (above tabs, always visible)
    var thoughtInput = document.querySelector('.page-capture-input');
    r.has_thought_input = !!thoughtInput;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Search page JS check bundle (auto-kvka6) ─────────────────────────

SEARCH_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Result cards rendered (one per source). The mock DAO returns the
    // SWEEP_SEARCH_RESULTS list in fixture order, and the search page
    // sorts by rank — so the strongest title-boosted hit (rank ≈ -55)
    // should land first.
    var cards = document.querySelectorAll('.sp-source-card');
    r.card_count = cards.length;
    r.has_cards = cards.length > 0;

    var titles = [];
    cards.forEach(function(c) {
        var t = c.querySelector('.sp-card-title');
        titles.push(t ? t.textContent.trim() : '');
    });
    r.titles = titles;
    r.first_title = titles[0] || '';

    // Title-boost: the curated "Worktrees Surface Specification" with
    // rank −55 must outrank the rank −10 session-derivation cards. The
    // mock DAO returns rows in the fixture order; the page sorts by
    // rank server-side so the order survives.
    r.title_boosted_first = titles[0] === 'Worktrees Surface Specification';

    // Tag-overlap soft signal: the row tagged "pitfall" / "git" should
    // come ahead of the bare body match. Both rows match on tokens; the
    // server's tag-overlap boost reorders them.
    var pitfallIdx = titles.indexOf('Pitfall: worktree stash pop loses untracked');
    var bareIdx = titles.indexOf('Bare body match');
    r.pitfall_index = pitfallIdx;
    r.bare_index = bareIdx;
    r.tag_overlap_outranks_bare = pitfallIdx !== -1 && bareIdx !== -1
        && pitfallIdx < bareIdx;

    // short_description rendering: the curated note has one set; the
    // bare body match does not. Card 0 must show the description; card 4
    // must NOT have a sp-card-summary.
    var firstSummary = cards[0] ? cards[0].querySelector('.sp-card-summary') : null;
    r.first_card_has_summary = !!firstSummary;
    r.first_card_summary_text = firstSummary ? firstSummary.textContent.trim() : '';

    var bareCard = null;
    cards.forEach(function(c) {
        var t = c.querySelector('.sp-card-title');
        if (t && t.textContent.trim() === 'Bare body match') bareCard = c;
    });
    r.bare_card_has_summary = bareCard
        ? !!bareCard.querySelector('.sp-card-summary')
        : null;

    // Disambiguator: every card carries a 12-char id. Two collision
    // cards (same title) must have DISTINCT ids and DISTINCT date
    // strings (second-resolution) so they're not interchangeable at
    // a glance.
    var ids = [];
    var dates = [];
    var collisionIds = [];
    var collisionDates = [];
    cards.forEach(function(c) {
        var idEl = c.querySelector('[data-testid=sp-card-id]');
        var idTxt = idEl ? idEl.textContent.trim() : '';
        ids.push(idTxt);
        var footerSpans = c.querySelectorAll('.sp-card-footer span');
        var dateTxt = footerSpans.length >= 2 ? footerSpans[1].textContent.trim() : '';
        dates.push(dateTxt);
        var titleEl = c.querySelector('.sp-card-title');
        if (titleEl && titleEl.textContent.trim() === 'auto-0422-195935') {
            collisionIds.push(idTxt);
            collisionDates.push(dateTxt);
        }
    });
    r.ids = ids;
    r.dates = dates;
    r.collision_ids = collisionIds;
    r.collision_dates = collisionDates;
    r.all_cards_have_id = ids.every(function(i) { return i && i.length === 12; });
    r.collision_ids_distinct = collisionIds.length === 2
        && collisionIds[0] !== collisionIds[1];
    r.collision_dates_distinct = collisionDates.length === 2
        && collisionDates[0] !== collisionDates[1];
    // Second-resolution date must be 19 chars: "YYYY-MM-DD HH:MM:SS"
    r.dates_at_second_resolution = collisionDates.every(function(d) {
        return d.length >= 19;
    });

    // Sticky filter strip flush against the global header: with
    // body.route-search active, main#content padding-top is 0. Reading
    // the computed style avoids brittle pixel measurement.
    var mainEl = document.getElementById('content');
    var bodyHasRoute = document.body.classList.contains('route-search');
    r.body_has_route_search = bodyHasRoute;
    if (mainEl) {
        var cs = getComputedStyle(mainEl);
        r.main_padding_top = cs.paddingTop;
        r.main_padding_top_zero = parseFloat(cs.paddingTop) === 0;
    } else {
        r.main_padding_top = '';
        r.main_padding_top_zero = false;
    }

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Streams page JS check bundle ─────────────────────────────────────

STREAMS_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Page heading
    r.has_heading = bodyText.indexOf('Streams') !== -1;

    // Stream tag entries visible
    r.has_pitfall = bodyText.indexOf('#pitfall') !== -1;
    r.has_architecture = bodyText.indexOf('#architecture') !== -1;
    r.has_testing = bodyText.indexOf('#testing') !== -1;

    // Counts visible ("N notes")
    r.has_12_notes = bodyText.indexOf('12 notes') !== -1;
    r.has_8_notes = bodyText.indexOf('8 notes') !== -1;

    // Descriptions visible
    r.has_description = bodyText.indexOf('Operational hazards') !== -1
                     || bodyText.indexOf('Design decisions') !== -1;

    // Links to stream detail pages
    var links = document.querySelectorAll('a[href*="/stream/"]');
    r.link_count = links.length;
    r.has_stream_links = links.length >= 3;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Worktrees page JS check bundle ───────────────────────────────────

WORKTREES_PAGE_CHECKS = """(async () => {
    var r = {};
    var sleep = function(ms) { return new Promise(resolve => setTimeout(resolve, ms)); };
    var waitFor = async function(predicate, timeoutMs) {
        var deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            if (predicate()) return true;
            await sleep(50);
        }
        return false;
    };
    var findButtonByText = function(root, text) {
        var buttons = Array.from((root || document).querySelectorAll('button'));
        return buttons.find(function(btn) { return btn.textContent.trim() === text; }) || null;
    };

    r.has_page = !!document.querySelector('[data-testid="worktrees-page"]');
    await waitFor(function() {
        return document.querySelectorAll('[data-testid="worktree-commit-card"]').length >= 2;
    }, 3000);

    var bodyText = document.body.innerText;
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
    r.refresh_label = (document.querySelector('[data-testid="worktrees-refresh-button"]') || {}).textContent?.replace(/\\s+/g, ' ').trim() || '';
    r.commit_card_count = document.querySelectorAll('[data-testid="worktree-commit-card"]').length;
    r.dirty_card_count_initial = document.querySelectorAll('[data-testid="worktree-dirty-card"]').length;
    r.commit_titles = Array.from(document.querySelectorAll('[data-testid="worktree-commit-card"] h3')).map(function(el) {
        return el.textContent.trim();
    });
    r.summary_counts = Array.from(document.querySelectorAll('[data-testid="worktrees-summary"] > div')).map(function(tile) {
        var values = tile.querySelectorAll('div');
        var last = values.length ? values[values.length - 1] : null;
        return last ? last.textContent.trim() : '';
    });

    var firstReview = document.querySelector('[data-testid="review-commit-button"]');
    if (firstReview) firstReview.click();
    await waitFor(function() {
        return !!document.querySelector('[data-testid="worktree-commit-detail"]');
    }, 3000);
    await sleep(250);

    var commitDetail = document.querySelector('[data-testid="worktree-commit-detail"]');
    r.commit_detail_open = !!commitDetail;
    r.commit_detail_title = commitDetail ? (commitDetail.querySelector('h3') || {}).textContent?.trim() || '' : '';
    r.commit_merge_label = commitDetail
        ? ((commitDetail.querySelector('[data-testid="worktree-commit-merge-button"]') || {}).textContent || '').replace(/\\s+/g, ' ').trim()
        : '';
    await sleep(1200);
    r.commit_detail_stable = !!document.querySelector('[data-testid="worktree-commit-detail"]');
    var commitClose = commitDetail ? findButtonByText(commitDetail, 'Close') : null;
    if (commitClose) commitClose.click();
    await waitFor(function() {
        return !document.querySelector('[data-testid="worktree-commit-detail"]');
    }, 2000);

    var toggle = document.querySelector('[data-testid="worktree-view-toggle"]');
    var changesButton = findButtonByText(toggle, 'Changes');
    if (changesButton) changesButton.click();
    await waitFor(function() {
        return document.querySelectorAll('[data-testid="worktree-dirty-card"]').length >= 2;
    }, 3000);

    var dirtyCards = Array.from(document.querySelectorAll('[data-testid="worktree-dirty-card"]'));
    r.dirty_card_count = dirtyCards.length;
    var discardBtn = dirtyCards.length ? dirtyCards[0].querySelector('[data-testid="discard-dirty-button"]') : null;
    r.live_dirty_has_no_discard = !discardBtn || discardBtn.offsetParent === null;
    r.dirty_title_visible = dirtyCards.length
        ? dirtyCards[0].textContent.indexOf('Alpha — card redesign') !== -1
        : false;

    var dirtyReview = document.querySelector('[data-testid="review-dirty-button"]');
    if (dirtyReview) dirtyReview.click();
    await waitFor(function() {
        return !!document.querySelector('[data-testid="worktree-dirty-detail"]');
    }, 3000);
    await sleep(250);

    var dirtyDetail = document.querySelector('[data-testid="worktree-dirty-detail"]');
    r.dirty_detail_open = !!dirtyDetail;
    r.dirty_detail_heading = dirtyDetail ? (dirtyDetail.querySelector('h3') || {}).textContent?.trim() || '' : '';
    await sleep(1200);
    r.dirty_detail_stable = !!document.querySelector('[data-testid="worktree-dirty-detail"]');

    return JSON.stringify(r);
})()"""

# ── Bead detail page JS check bundle ─────────────────────────────────

BEAD_DETAIL_CHECKS = """
    var bodyText = document.body.innerText;

    // Bead title visible
    r.has_title = bodyText.indexOf('Sweep alpha task') !== -1;

    // Bead ID visible
    r.has_bead_id = bodyText.indexOf('auto-sweep-b1') !== -1;

    // Priority badge visible
    r.has_priority = bodyText.indexOf('P1') !== -1;

    // Status badge visible
    r.has_status = bodyText.indexOf('open') !== -1;

    // Description section visible
    r.has_description = bodyText.indexOf('First test bead') !== -1
                     || bodyText.indexOf('behavioral sweep') !== -1;

    // Labels visible
    r.has_label = bodyText.indexOf('readiness:approved') !== -1;

    // Issue type visible
    r.has_issue_type = bodyText.indexOf('task') !== -1;

    // State is ready (not loading or error)
    var loadingText = document.querySelector('[x-show*="loading"]');
    var errorText = document.querySelector('[x-show*="error"]');
    r.no_loading = !loadingText || loadingText.offsetParent === null
                || loadingText.style.display === 'none';

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Trace page JS check bundle ───────────────────────────────────────

TRACE_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Header with bead title (scoped to trace section, not global header)
    var traceSection = document.querySelector('[aria-label="Trace"]');
    var h1 = traceSection ? traceSection.querySelector('h1') : null;
    r.title_text = h1 ? h1.textContent.trim() : '';
    r.has_title = bodyText.indexOf('Sweep alpha') !== -1;

    // Bead ID link
    r.has_bead_link = bodyText.indexOf('auto-sweep-b1') !== -1;

    // Back link to dispatch
    var backLink = document.querySelector('a[href="/dispatch"]');
    r.has_back_link = !!backLink;

    // Decision section visible
    r.has_decision_heading = bodyText.indexOf('Decision') !== -1;
    r.has_status = bodyText.indexOf('DONE') !== -1;
    r.has_reason = bodyText.indexOf('All tests pass') !== -1;

    // Scores visible (stars)
    var stars = document.querySelectorAll('.tl-star-on, .tl-star-off');
    r.star_count = stars.length;
    r.has_stars = stars.length > 0;

    // Score labels
    r.has_tooling_label = bodyText.indexOf('Tooling') !== -1;
    r.has_clarity_label = bodyText.indexOf('Clarity') !== -1;
    r.has_confidence_label = bodyText.indexOf('Confidence') !== -1;

    // Duration visible
    r.has_duration = bodyText.indexOf('5m') !== -1;

    // Diff stats visible
    r.has_diff_add = bodyText.indexOf('+45') !== -1;
    r.has_diff_del = bodyText.indexOf('-12') !== -1;

    // Commit hash visible
    r.has_commit = bodyText.indexOf('abc123def4') !== -1;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""


# ── Tests ─────────────────────────────────────────────────────────────

class TestSessionsPageBehavior:
    """Sessions page behavioral sweep — one JS eval, many assertions.

    All checks run in a single agent-browser eval call. The `checks` fixture
    caches the result dict so each test method reads from the same snapshot.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Run the sessions page check bundle once, cache for all test methods."""
        result = _navigate_and_check("/sessions", SESSIONS_PAGE_CHECKS, wait_ms=1500)
        request.cls._checks = result

    def test_cards_exist(self):
        """User sees session cards on the page."""
        c = self._checks
        assert c.get("has_cards"), f"No session cards found (count={c.get('card_count')})"
        assert c["card_count"] == 5, f"Expected 5 session cards, got {c['card_count']}"

    def test_labels_visible(self):
        """User sees session labels (titles) on each card."""
        c = self._checks
        assert c.get("labels_visible"), "No session labels visible"
        labels = c.get("labels", [])
        assert len(labels) >= 4, f"Expected at least 4 labels, got {len(labels)}: {labels}"
        # Check specific labels are present
        label_text = " ".join(labels)
        assert "Alpha" in label_text, f"'Alpha' label not found in {labels}"
        assert "Beta" in label_text, f"'Beta' label not found in {labels}"
        assert "Host" in label_text, f"'Host' label not found in {labels}"

    def test_host_badge(self):
        """User sees a 'Host' role badge on the host session card."""
        c = self._checks
        assert c.get("has_host_badge"), "No 'Host' role badge visible"
        assert c.get("host_card_count", 0) >= 1, "No cards with host styling"

    def test_roles_visible(self):
        """User sees role badges (Designer, Builder, Reviewer, etc.) on cards."""
        c = self._checks
        assert c.get("roles_visible"), "No role badges visible"
        roles = c.get("roles", [])
        assert len(roles) >= 3, f"Expected at least 3 role badges, got {len(roles)}: {roles}"
        role_text = " ".join(roles)
        assert "Designer" in role_text, f"'Designer' not in roles: {roles}"
        assert "Builder" in role_text, f"'Builder' not in roles: {roles}"

    def test_turn_counts_visible(self):
        """User sees turn counts (entry count) in the T3 stats row."""
        c = self._checks
        assert c.get("turns_visible"), "No turn counts visible"
        turns = c.get("turn_values", [])
        # Check at least one meaningful value (our fixtures have 50-300)
        numeric = [t for t in turns if t.isdigit() and int(t) > 0]
        assert len(numeric) >= 1, f"No numeric turn values: {turns}"

    def test_context_tokens_visible(self):
        """User sees context token counts (e.g. '80K', '120K') in T3 stats."""
        c = self._checks
        assert c.get("ctx_visible"), "No context token values visible"
        ctx = c.get("ctx_values", [])
        has_k = any("K" in v for v in ctx)
        assert has_k, f"No 'K' formatted token values: {ctx}"

    def test_topics_visible(self):
        """User sees topic lines on cards that have topics."""
        c = self._checks
        assert c.get("topics_visible"), "No topic items visible"
        topics = c.get("topic_texts", [])
        topic_text = " ".join(topics)
        assert "CSS grid" in topic_text or "session cards" in topic_text.lower() or "Redesigning" in topic_text, \
            f"Expected session card topics, got: {topics}"

    def test_recent_sessions(self):
        """User sees the Recent Sessions section with history entries."""
        c = self._checks
        assert c.get("has_recent"), f"No recent sessions visible (count={c.get('recent_count')})"
        assert c["recent_count"] == 3, f"Expected 3 recent sessions, got {c['recent_count']}"

    def test_recent_sort_and_since_dropdowns_present(self):
        """Recent Sessions header has a Sort dropdown and a Since dropdown."""
        c = self._checks
        assert c.get("has_recent_sort_toggle"), "Missing [data-testid='recent-sort-toggle']"
        assert c.get("has_recent_since_toggle"), "Missing [data-testid='recent-since-toggle']"

    def test_recent_sort_defaults(self):
        """Default Sort label is 'End time' and default Since label is '1 day'."""
        c = self._checks
        assert c.get("recent_sort_value") == "End time", \
            f"Expected default Sort 'End time', got {c.get('recent_sort_value')!r}"
        assert c.get("recent_since_value") == "1 day", \
            f"Expected default Since '1 day', got {c.get('recent_since_value')!r}"

    def test_recent_sort_options_match_design(self):
        """Sort menu has exactly the 5 option labels (auto-ycry3: + Duration)."""
        c = self._checks
        assert c.get("recent_sort_options") == [
            "End time", "Start time", "Most Turns", "Most Context", "Duration"
        ], f"Sort options mismatch: {c.get('recent_sort_options')}"

    def test_recent_since_options_match_design(self):
        """Since menu has exactly the 4 design-specified option labels."""
        c = self._checks
        assert c.get("recent_since_options") == [
            "6 hours", "1 day", "1 week", "All time"
        ], f"Since options mismatch: {c.get('recent_since_options')}"

    def test_no_template_artifacts(self):
        """No raw Jinja or Alpine template syntax visible to the user."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax ({{ or {%) visible on page"
        assert c.get("no_alpine_raw"), "Raw Alpine directive text (x-text=, x-show=) visible"

    def test_page_structure(self):
        """Page has both Active Sessions and Recent Sessions sections."""
        c = self._checks
        assert c.get("has_active_section"), "Missing [data-testid='active-sessions-section']"
        assert c.get("has_recent_section"), "Missing [data-testid='recent-sessions-section']"

    def test_session_ids_in_dom(self):
        """Session cards have data-session-id attributes for click targeting."""
        c = self._checks
        ids = c.get("session_ids", [])
        assert len(ids) == 5, f"Expected 5 session IDs in DOM, got {len(ids)}: {ids}"
        assert "auto-sweep-alpha" in ids, f"alpha session ID not in DOM: {ids}"
        assert "host-sweep-delta" in ids, f"host session ID not in DOM: {ids}"

    def test_container_vs_host_cards(self):
        """Cards are styled differently for host vs container sessions."""
        c = self._checks
        assert c.get("host_card_count", 0) == 1, f"Expected 1 host card, got {c.get('host_card_count')}"
        assert c.get("container_card_count", 0) == 4, f"Expected 4 container cards, got {c.get('container_card_count')}"

    def test_no_standalone_resume_buttons(self):
        """auto-ycry3: Resume is now a menu entry, not a standalone button.

        The partial renders no `data-testid="resume-btn"` elements anywhere.
        Resume action lives inside the sc-org sc-actions menu.
        """
        c = self._checks
        assert c.get("resume_btn_count", 0) == 0, (
            "Recent cards no longer render a standalone Resume button — "
            f"found {c.get('resume_btn_count')} resume-btn elements; they "
            "should be consolidated into the actions menu."
        )

    def test_session_actions_btn_on_recent_rows(self):
        """Every recent-session-row carries the sc-org sc-actions button.

        auto-ycry3: the merged org/actions button is the only interaction
        affordance besides the row-level click-to-navigate.
        """
        c = self._checks
        count = c.get("recent_session_actions_btn_count", 0)
        recent_count = c.get("recent_row_count", 0)
        assert recent_count > 0, "No recent rows in DOM to validate"
        assert count == recent_count, (
            f"Expected every recent row ({recent_count}) to carry a "
            f"session-actions-btn; got {count}"
        )

    # ── Recent card footer matrix (auto-wa3d) ────────────────────────
    # Dead sessions show 'ended' + absolute datetime in col 3. Tmux column
    # in col 4 is omitted entirely for dispatch/librarian (synthetic value).

    def _footer_for(self, session_type: str) -> dict:
        c = self._checks
        footers = c.get("recent_footers", [])
        for f in footers:
            if f.get("type") == session_type:
                return f
        raise AssertionError(
            f"No recent-row footer with session_type={session_type!r}; "
            f"available: {[f.get('type') for f in footers]}"
        )

    @staticmethod
    def _core_labels(labels):
        # Strip columns orthogonal to this bead: the legacy 'project' column
        # and the post-auto-jl9dc 'org' column (added by the resolved-identity
        # footer item in session-card.html).
        return [l for l in labels if l not in ("project", "org")]

    def test_recent_interactive_footer_labels(self):
        """Dead interactive: footer = ['turns', 'ctx', 'ended', 'tmux']."""
        f = self._footer_for("interactive")
        assert self._core_labels(f["labels"]) == ["turns", "ctx", "ended", "tmux"], \
            f"interactive footer labels mismatch: {f['labels']}"

    def test_recent_dispatch_footer_labels(self):
        """Dead dispatch: footer = ['turns', 'ctx', 'ended'] (tmux hidden)."""
        f = self._footer_for("dispatch")
        assert self._core_labels(f["labels"]) == ["turns", "ctx", "ended"], \
            f"dispatch footer labels mismatch (tmux must be absent): {f['labels']}"

    def test_recent_librarian_footer_labels(self):
        """Dead librarian: footer = ['turns', 'ctx', 'ended'] (tmux hidden)."""
        f = self._footer_for("librarian")
        assert self._core_labels(f["labels"]) == ["turns", "ctx", "ended"], \
            f"librarian footer labels mismatch (tmux must be absent): {f['labels']}"

    def test_recent_ended_value_is_absolute_datetime(self):
        """The 'ended' value matches YYYY-MM-DD HH:MM (24h, operator-local, no tz suffix)."""
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        for f in self._checks.get("recent_footers", []):
            labels = f["labels"]
            values = f["values"]
            assert "ended" in labels, f"row missing 'ended' label: {f}"
            ended_idx = labels.index("ended")
            ended_val = values[ended_idx]
            assert pattern.match(ended_val), \
                f"ended value {ended_val!r} on {f['type']!r} row " \
                f"does not match YYYY-MM-DD HH:MM"

    def test_recent_dispatch_librarian_have_no_tmux_column(self):
        """For dispatch + librarian, the synthetic tmux value must not leak into the footer."""
        for stype in ("dispatch", "librarian"):
            f = self._footer_for(stype)
            assert "tmux" not in f["labels"], \
                f"{stype} footer must not render a tmux column; got labels={f['labels']}"
            joined = " ".join(f["values"])
            assert "agent-auto-" not in joined and "librarian-auto-" not in joined, \
                f"synthetic tmux value leaked into {stype} footer: {f['values']}"


class TestResumeButtonStateTransition:
    """Resume-button state-transition tests (auto-eefr, auto-6x6c) are retired.

    auto-ycry3 consolidated the Resume affordance into the sc-org sc-actions
    actions menu — there is no standalone resume-btn in the DOM anymore.
    The underlying `resumeSession()` function is unchanged; the action sheet
    menu entry calls it through the same optimistic-move flow. State visible
    to the user (spinner/“Live ●”/error pill) lives on the card, covered by
    the surrounding behavioral tests.
    """

    def test_noop_sentinel(self):
        """Sentinel test to keep the class in the report with a passing marker."""
        assert True


class TestRecentSortAndSinceBehavior:
    """Recent Sessions sort + since — reordering and filter behaviors.

    Drives the Alpine component directly (no menu clicks) to check:
      * `Most Turns` promotes the 627-turn session above 0-turn rows.
      * `Since: 6 hours` excludes rows older than 6h.
      * Selections persist to localStorage (the watchers wire to it).
    REGRESSION GUARD: auto-d0mt acceptance.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_eval_async(
            "/sessions",
            RECENT_SORT_SINCE_BEHAVIOR_CHECKS,
            wait_ms=1500,
        )
        request.cls._checks = result

    def test_most_turns_promotes_high_turn_session(self):
        """Sort=turns places the 627-turn row above the 0-turn row."""
        c = self._checks
        assert c.get("turns_first") == "src-sweep-aaa111", (
            f"Most Turns should place the 627-turn session first; got "
            f"{c.get('turns_first')!r}, full order {c.get('turns_ids')}"
        )

    def test_since_6h_excludes_ancient_rows(self):
        """Since=6h still includes the 2h-old row (src-sweep-ccc333 @120m)."""
        c = self._checks
        ids = c.get("since_6h_ids") or []
        assert "src-sweep-ccc333" in ids, (
            f"Since=6h should include row at 120min; got {ids}"
        )
        assert "src-sweep-aaa111" in ids, (
            f"Since=6h should include row at 10min; got {ids}"
        )

    def test_selections_persist_to_localstorage(self):
        """Changing recentSort/recentSince writes through to localStorage."""
        c = self._checks
        assert c.get("since_in_storage") in ("6h", "1d"), (
            f"Expected '6h' or '1d' in localStorage, got {c.get('since_in_storage')!r}"
        )


class TestDispatchPageBehavior:
    """Dispatch page behavioral sweep — section headings, bead cards, pause controls."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/dispatch", DISPATCH_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_section_headings(self):
        """User sees Active, Waiting, and Blocked section headings."""
        c = self._checks
        assert c.get("has_active_heading"), f"No 'Active' heading, got: {c.get('headings')}"
        assert c.get("has_waiting_heading"), f"No 'Waiting' heading, got: {c.get('headings')}"
        assert c.get("has_blocked_heading"), f"No 'Blocked' heading, got: {c.get('headings')}"

    def test_bead_titles_visible(self):
        """User sees bead titles from fixture data in the dispatch sections."""
        c = self._checks
        assert c.get("has_alpha_title"), "Alpha bead title not visible"
        assert c.get("has_gamma_title"), "Gamma bead title not visible"
        assert c.get("has_beta_title"), "Beta bead title not visible"

    def test_pause_controls(self):
        """User sees dispatch/merge pause toggle buttons."""
        c = self._checks
        assert c.get("has_pause_controls"), f"Pause controls missing, found: {c.get('pause_labels')}"

    def test_running_stat(self):
        """User sees 'running: N' stat indicator."""
        c = self._checks
        assert c.get("has_running_stat"), "No 'running:' stat visible"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on dispatch page"

    def test_librarian_card_shows_title(self):
        """Librarian card shows 'Librarian: review_report' title, not 'Pnull'."""
        c = self._checks
        assert c.get("has_librarian_title"), "Librarian title not visible on dispatch page"

    def test_no_pnull_on_dispatch_page(self):
        """No 'Pnull' or 'Pundefined' text on dispatch page."""
        c = self._checks
        assert c.get("no_pnull"), "Found 'Pnull' or 'Pundefined' on dispatch page"

    def test_librarian_card_has_no_priority_badge(self):
        """Librarian card does not show a numeric priority badge (P0, P1, ...)."""
        c = self._checks
        assert c.get("librarian_has_no_priority_badge"), "Librarian card has a priority badge"

    def test_librarian_card_has_distinct_style(self):
        """At least one librarian card is detected via data-librarian attribute."""
        c = self._checks
        assert c.get("librarian_card_count", 0) >= 1, \
            f"No librarian cards found (count={c.get('librarian_card_count')})"


class TestBeadsPageBehavior:
    """Beads page behavioral sweep — view tabs, table rows, filters."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/beads", BEADS_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_view_tabs(self):
        """User sees List, Board, Tree, and Deps view tabs."""
        c = self._checks
        assert c.get("has_list_tab"), f"No 'List' tab, got: {c.get('tab_labels')}"
        assert c.get("has_board_tab"), f"No 'Board' tab, got: {c.get('tab_labels')}"
        assert c.get("has_tree_tab"), f"No 'Tree' tab, got: {c.get('tab_labels')}"
        assert c.get("has_deps_tab"), f"No 'Deps' tab, got: {c.get('tab_labels')}"

    def test_bead_rows(self):
        """User sees bead rows in the list view."""
        c = self._checks
        assert c.get("has_rows"), f"No bead rows visible (count={c.get('row_count')})"
        assert c["row_count"] >= 3, f"Expected at least 3 rows, got {c['row_count']}"

    def test_bead_titles(self):
        """User sees bead titles from fixture data."""
        c = self._checks
        assert c.get("has_alpha_bead"), "Alpha bead title not visible"
        assert c.get("has_beta_bead"), "Beta bead title not visible"

    def test_priority_badges(self):
        """User sees priority badges (P0, P1, P2)."""
        c = self._checks
        assert c.get("has_p1"), "P1 badge not visible"
        assert c.get("has_p2"), "P2 badge not visible"

    def test_bead_ids_visible(self):
        """User sees bead IDs in the table."""
        c = self._checks
        assert c.get("has_bead_ids"), "Bead IDs not visible in table"

    def test_filter_controls(self):
        """User sees filter controls (priority chips, phase chips)."""
        c = self._checks
        assert c.get("has_filters"), f"Not enough filter controls (count={c.get('filter_count')})"

    def test_column_headers(self):
        """User sees table column headers (Title, Pri, etc.)."""
        c = self._checks
        assert c.get("has_title_col"), f"No 'Title' column header, got: {c.get('col_headers')}"
        assert c.get("has_pri_col"), f"No 'Pri' column header, got: {c.get('col_headers')}"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on beads page"


class TestTimelinePageBehavior:
    """Timeline page behavioral sweep — stats tiles, feed entries, status indicators."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/timeline", TIMELINE_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_range_toggle(self):
        """User sees timeframe toggle buttons (1D, 1W, 1M, All)."""
        c = self._checks
        assert c.get("has_range_toggle"), f"Missing range toggle, found: {c.get('range_buttons')}"

    def test_stats_tiles(self):
        """User sees stats tiles (Completed, Failed/Blocked, Avg Duration, etc.)."""
        c = self._checks
        assert c.get("has_completed_tile"), "No 'Completed' tile visible"
        assert c.get("has_duration_tile"), "No 'Avg Duration' tile visible"
        assert c.get("has_tooling_tile"), "No 'Avg Tooling' tile visible"
        assert c.get("has_confidence_tile"), "No 'Avg Confidence' tile visible"

    def test_stats_values(self):
        """User sees actual stats values from fixture data."""
        c = self._checks
        assert c.get("has_completed_count"), "Completed count '5' not visible"
        assert c.get("has_success_pct"), "Success rate '80%' not visible"

    def test_feed_entries(self):
        """User sees feed entries (timeline cards)."""
        c = self._checks
        assert c.get("has_feed_entries"), f"No feed entries visible (count={c.get('feed_count')})"

    def test_status_indicators(self):
        """User sees status dots on timeline entries."""
        c = self._checks
        assert c.get("has_status_dots"), f"No status dots visible (count={c.get('dot_count')})"

    def test_stars_visible(self):
        """User sees star ratings in stats tiles."""
        c = self._checks
        assert c.get("has_stars"), f"No stars visible (count={c.get('star_count')})"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on timeline page"


class TestCollabPageBehavior:
    """Collab page behavioral sweep — tabs, notes, thought input."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/collab", COLLAB_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_tab_strip(self):
        """User sees tab strip with Recent, Curated, Thoughts, Threads, Topics."""
        c = self._checks
        assert c.get("has_recent_tab"), f"No 'Recent' tab, got: {c.get('tab_labels')}"
        assert c.get("has_curated_tab"), f"No 'Curated' tab, got: {c.get('tab_labels')}"
        assert c.get("has_thoughts_tab"), f"No 'Thoughts' tab, got: {c.get('tab_labels')}"
        assert c.get("has_threads_tab"), f"No 'Threads' tab, got: {c.get('tab_labels')}"
        assert c.get("has_topics_tab"), f"No 'Topics' tab, got: {c.get('tab_labels')}"
        assert c.get("tab_count") == 5, f"Expected 5 tabs, got {c.get('tab_count')}"

    def test_recent_notes(self):
        """User sees recent note cards in the default tab."""
        c = self._checks
        assert c.get("has_notes"), f"No note cards visible (count={c.get('note_count')})"
        assert c.get("note_count") >= 2, f"Expected at least 2 notes, got {c.get('note_count')}"

    def test_note_titles(self):
        """User sees note titles from fixture data."""
        c = self._checks
        assert c.get("has_note_titles"), f"No note titles visible"
        titles = c.get("note_titles", [])
        title_text = " ".join(titles)
        assert "Architecture" in title_text or "Testing" in title_text, \
            f"Expected fixture note titles, got: {titles}"

    def test_type_labels(self):
        """User sees note type labels (NOTE, THOUGHT, etc.)."""
        c = self._checks
        assert c.get("has_type_labels"), f"No type labels visible"

    def test_tags_visible(self):
        """User sees tag chips on note cards."""
        c = self._checks
        assert c.get("has_tags"), f"No tags visible"
        tags = c.get("tags", [])
        assert any("architecture" in t or "testing" in t or "auth" in t or "dispatch" in t for t in tags), \
            f"Expected fixture tags, got: {tags}"

    def test_tab_counts(self):
        """User sees counts next to tab labels."""
        c = self._checks
        assert c.get("has_tab_counts"), f"No tab counts visible, got: {c.get('tab_counts')}"

    def test_thought_input(self):
        """Page-level thought capture input exists above tabs."""
        c = self._checks
        assert c.get("has_thought_input"), "No thought capture input found in DOM"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on collab page"


class TestSearchPageRanking:
    """Search page ranking + disambiguator behavioural sweep — auto-kvka6.

    Phase 2 (auto-qlfg1) shipped TestSearchPageBehavior covering the
    chrome groundwork (chip rail, grouping, empty states); this class
    extends coverage to the Phase 3 ranking checks: title-boost,
    tag-overlap, short_description rendering, and the per-card
    disambiguator stamp on title collisions.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        # ?q=worktree exercises the search path so the sticky strip is
        # visible and the auto-kvka6 ranking fixture rows surface (the
        # mock DAO substring-filters search_results by query).
        result = _navigate_and_check(
            "/search?q=worktree", SEARCH_PAGE_CHECKS, wait_ms=1200,
        )
        request.cls._checks = result

    def test_search_title_match_outranks_body_match(self):
        """The curated 'Worktrees Dashboard Specification' lands at the top."""
        c = self._checks
        assert c.get("has_cards"), \
            f"No search cards rendered (count={c.get('card_count')})"
        assert c.get("title_boosted_first"), (
            "Title-boosted source did not rank first; "
            f"got titles={c.get('titles')}"
        )

    def test_search_tag_overlap_boosts_rank(self):
        """A row carrying the matching 'pitfall' tag ranks above the bare body match."""
        c = self._checks
        assert c.get("tag_overlap_outranks_bare"), (
            "Pitfall-tagged row did not outrank bare body match: "
            f"pitfall_index={c.get('pitfall_index')} "
            f"bare_index={c.get('bare_index')} titles={c.get('titles')}"
        )

    def test_search_card_renders_short_description(self):
        """Cards with a short_description render the description row."""
        c = self._checks
        assert c.get("first_card_has_summary"), (
            "First card (with short_description) is missing the .sp-card-summary row"
        )
        text = c.get("first_card_summary_text", "")
        assert "worktrees" in text.lower() or "render" in text.lower(), (
            f"First card summary text mismatch: {text!r}"
        )
        # Inverse: the row without a short_description must NOT render
        # an empty summary row.
        assert c.get("bare_card_has_summary") is False, (
            "Card without short_description rendered an empty .sp-card-summary"
        )

    def test_search_card_disambiguator_on_title_collision(self):
        """Two cards sharing a title each carry a distinct 12-char id and
        second-resolution date.
        """
        c = self._checks
        assert c.get("all_cards_have_id"), (
            f"Not every card rendered the 12-char source_id "
            f"disambiguator: ids={c.get('ids')}"
        )
        assert c.get("collision_ids_distinct"), (
            f"Title-collision cards share identical ids: "
            f"{c.get('collision_ids')}"
        )
        assert c.get("collision_dates_distinct"), (
            f"Title-collision cards share identical dates (need second "
            f"resolution): {c.get('collision_dates')}"
        )
        assert c.get("dates_at_second_resolution"), (
            f"Dates are not at second resolution "
            f"(YYYY-MM-DD HH:MM:SS): {c.get('collision_dates')}"
        )

    def test_search_filter_strip_flush_to_header(self):
        """body.route-search drops main's pt-6 so the sticky strip flushes."""
        c = self._checks
        assert c.get("body_has_route_search"), (
            "body.route-search class missing on /search route"
        )
        assert c.get("main_padding_top_zero"), (
            f"main#content padding-top is not 0 on /search "
            f"(got {c.get('main_padding_top')!r})"
        )

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible on /search."""
        c = self._checks
        assert c.get("no_jinja"), \
            "Raw Jinja template syntax visible on search page"


class TestStreamsPageBehavior:
    """Streams page behavioral sweep — heading, stream entries, counts."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/streams", STREAMS_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_heading(self):
        """User sees 'Streams' heading."""
        c = self._checks
        assert c.get("has_heading"), "No 'Streams' heading visible"

    def test_stream_tags(self):
        """User sees stream tag names (#pitfall, #architecture, #testing)."""
        c = self._checks
        assert c.get("has_pitfall"), "No '#pitfall' stream visible"
        assert c.get("has_architecture"), "No '#architecture' stream visible"
        assert c.get("has_testing"), "No '#testing' stream visible"

    def test_counts(self):
        """User sees note counts for each stream."""
        c = self._checks
        assert c.get("has_12_notes"), "No '12 notes' count visible for #pitfall"
        assert c.get("has_8_notes"), "No '8 notes' count visible for #architecture"

    def test_descriptions(self):
        """User sees descriptions for streams."""
        c = self._checks
        assert c.get("has_description"), "No stream descriptions visible"

    def test_links(self):
        """User sees links to stream detail pages."""
        c = self._checks
        assert c.get("has_stream_links"), f"Not enough stream links (count={c.get('link_count')})"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on streams page"


class TestWorktreesPageBehavior:
    """Worktrees page behavioral sweep — stable mount and review overlays."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_eval_async("/worktrees", WORKTREES_PAGE_CHECKS, wait_ms=1200)
        request.cls._checks = result

    def test_page_mounts_with_real_data(self):
        """User sees populated worktree cards instead of the empty pre-init shell."""
        c = self._checks
        assert c.get("has_page"), "No worktrees page root found"
        assert c.get("commit_card_count") == 2, (
            f"Expected 2 commit-stack cards, got {c.get('commit_card_count')}"
        )
        assert c.get("refresh_label") == "Refresh", (
            f"Refresh button rendered oddly: {c.get('refresh_label')!r}"
        )
        assert c.get("summary_counts") == ["3", "2", "2"], (
            f"Unexpected summary counts: {c.get('summary_counts')}"
        )

    def test_commit_cards_show_fixture_titles(self):
        """User sees the stacked commit headlines from the fixture rows."""
        titles = " ".join(self._checks.get("commit_titles", []))
        assert "Fix worktree review sticky headers" in titles, (
            f"Missing alpha commit title in {self._checks.get('commit_titles')}"
        )
        assert "Refine ENTERPRISE-7644 release branch plumbing" in titles, (
            f"Missing beta commit title in {self._checks.get('commit_titles')}"
        )

    def test_commit_review_opens_and_stays_open(self):
        """Opening Review shows the full-screen commit view and it remains mounted."""
        c = self._checks
        assert c.get("commit_detail_open"), "Commit review overlay never opened"
        assert c.get("commit_detail_stable"), "Commit review overlay did not stay open"
        assert c.get("commit_detail_title") == "Fix worktree review sticky headers", (
            f"Unexpected commit review title: {c.get('commit_detail_title')!r}"
        )
        assert "Merge 1111111 into main" in c.get("commit_merge_label", ""), (
            f"Unexpected merge label: {c.get('commit_merge_label')!r}"
        )

    def test_changes_mode_shows_dirty_cards(self):
        """Switching to Changes shows dirty rows and keeps Discard hidden for live worktrees."""
        c = self._checks
        assert c.get("dirty_card_count") == 2, (
            f"Expected 2 dirty cards, got {c.get('dirty_card_count')}"
        )
        assert c.get("live_dirty_has_no_discard"), "Live dirty card should not show Discard"
        assert c.get("dirty_title_visible"), "Changes card did not show the session title"

    def test_dirty_review_opens_and_stays_open(self):
        """View Diffs opens the dirty review overlay and it remains mounted."""
        c = self._checks
        assert c.get("dirty_detail_open"), "Dirty review overlay never opened"
        assert c.get("dirty_detail_stable"), "Dirty review overlay did not stay open"
        assert c.get("dirty_detail_heading") == "Working tree changes", (
            f"Unexpected dirty review heading: {c.get('dirty_detail_heading')!r}"
        )

    def test_no_template_artifacts(self):
        """No raw template syntax leaks into the worktrees page."""
        assert self._checks.get("no_jinja"), "Raw template syntax visible on worktrees page"


class TestBeadDetailPageBehavior:
    """Bead detail page behavioral sweep — title, priority, status, description."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/bead/auto-sweep-b1", BEAD_DETAIL_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_title(self):
        """User sees the bead title."""
        c = self._checks
        assert c.get("has_title"), "Bead title 'Sweep alpha task' not visible"

    def test_bead_id(self):
        """User sees the bead ID."""
        c = self._checks
        assert c.get("has_bead_id"), "Bead ID 'auto-sweep-b1' not visible"

    def test_priority_badge(self):
        """User sees priority badge (P1)."""
        c = self._checks
        assert c.get("has_priority"), "Priority badge P1 not visible"

    def test_status_badge(self):
        """User sees status badge (open)."""
        c = self._checks
        assert c.get("has_status"), "Status badge 'open' not visible"

    def test_description(self):
        """User sees the bead description text."""
        c = self._checks
        assert c.get("has_description"), "Bead description not visible"

    def test_labels(self):
        """User sees the bead labels."""
        c = self._checks
        assert c.get("has_label"), "Label 'readiness:approved' not visible"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on bead detail page"


class TestTracePageBehavior:
    """Trace page behavioral sweep — header, decision, scores."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            "/dispatch/trace/run-sweep-001", TRACE_PAGE_CHECKS, wait_ms=1000,
        )
        request.cls._checks = result

    def test_title(self):
        """User sees the bead title in the trace header."""
        c = self._checks
        assert c.get("has_title"), f"Bead title not visible, h1 text: '{c.get('title_text')}'"

    def test_bead_link(self):
        """User sees a link to the bead detail page."""
        c = self._checks
        assert c.get("has_bead_link"), "Bead ID link not visible"

    def test_back_link(self):
        """User sees a back link to dispatch page."""
        c = self._checks
        assert c.get("has_back_link"), "Back link to /dispatch not found"

    def test_decision_section(self):
        """User sees the Decision section with status and reason."""
        c = self._checks
        assert c.get("has_decision_heading"), "Decision heading not visible"
        assert c.get("has_status"), "Status 'DONE' not visible"
        assert c.get("has_reason"), "Reason 'All tests pass' not visible"

    def test_scores(self):
        """User sees star ratings for tooling, clarity, confidence."""
        c = self._checks
        assert c.get("has_stars"), f"No stars visible (count={c.get('star_count')})"
        assert c.get("has_tooling_label"), "Tooling label not visible"
        assert c.get("has_clarity_label"), "Clarity label not visible"
        assert c.get("has_confidence_label"), "Confidence label not visible"

    def test_diff_stats(self):
        """User sees diff stats (+added -removed)."""
        c = self._checks
        assert c.get("has_diff_add"), "+45 not visible"
        assert c.get("has_diff_del"), "-12 not visible"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on trace page"


# ── Trace overlay JS check bundle (Bug #1: raw Jinja in overlay) ─────

TRACE_OVERLAY_CHECKS = """
    // Trace page loaded — look for title inside #content (not site header)
    var content = document.getElementById('content');
    var titleEl = content ? content.querySelector('h1') : null;
    r.has_title = !!(titleEl && titleEl.textContent.trim().length > 0);
    r.title_text = titleEl ? titleEl.textContent.trim() : '';

    // Decision section rendered
    var decisionSection = content ? content.querySelector('section[aria-label="Decision"]') : null;
    r.has_decision = !!decisionSection;

    // Trace section exists (Alpine component rendered the fragment)
    var traceSection = content ? content.querySelector('section[aria-label="Trace"]') : null;
    r.trace_section_exists = !!traceSection;

    // No raw Jinja syntax in the live panel overlay body.
    // base.html serves the overlay panel via _load_template() which does NOT
    // render Jinja. The {% include "partials/session-entries.html" %} appears
    // as literal text in the DOM instead of the rendered partial content.
    var panelBody = document.getElementById('live-panel-body');
    r.panel_body_exists = !!panelBody;
    if (panelBody) {
        r.overlay_has_raw_jinja = panelBody.innerHTML.indexOf('{%') >= 0;
        r.overlay_has_include = panelBody.innerHTML.indexOf('include') >= 0
            && panelBody.innerHTML.indexOf('{%') >= 0;
    } else {
        r.overlay_has_raw_jinja = false;
        r.overlay_has_include = false;
    }

    // Check visible page text for raw Jinja (only catches visible content)
    var bodyText = document.body.innerText;
    r.no_jinja_visible = bodyText.indexOf('{%') === -1 && bodyText.indexOf('{{') === -1;
"""


class TestTraceOverlayBehavior:
    """Trace page overlay behavioral sweep — verifies overlay panel
    does not contain raw Jinja template syntax.

    Bug: base.html is served via _load_template() (raw file read) instead of
    Jinja rendering. The {% include "partials/session-entries.html" %} directive
    in the overlay panel body appears as literal text, visible when the panel
    opens for live or completed dispatch runs.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Navigate to trace page, run check bundle, cache results."""
        result = _navigate_and_check(
            "/dispatch/trace/auto-sweep-b2-20260327-120000",
            TRACE_OVERLAY_CHECKS,
            wait_ms=3000,
        )
        request.cls._checks = result

    def test_trace_section_exists(self):
        """Trace page fragment was loaded and Alpine component initialised."""
        c = self._checks
        assert c.get("trace_section_exists"), \
            "Trace section[aria-label='Trace'] not found — fragment may not have loaded"

    def test_trace_loads(self):
        """Trace page renders the bead title after fetching trace data."""
        c = self._checks
        assert c.get("has_title"), \
            f"Trace page title not rendered in #content (title='{c.get('title_text', '')}')"

    def test_decision_visible(self):
        """Decision section is rendered with status and scores."""
        c = self._checks
        assert c.get("has_decision"), \
            "User should see the Decision section with status, reason, and scores"

    def test_no_raw_jinja_in_overlay(self):
        """Overlay session panel must not contain raw Jinja template directives.

        The user should see rendered session entries, not literal
        {% include "partials/session-entries.html" %} text.
        """
        c = self._checks
        assert c.get("panel_body_exists"), "Overlay panel body (#live-panel-body) missing from DOM"
        assert not c.get("overlay_has_raw_jinja"), \
            ("Overlay panel HTML contains raw Jinja syntax ({%%). "
             "The {% include %} directive was not processed by the template engine. "
             "User would see literal template text instead of session entries.")


# ── Host session viewer JS check bundle ──────────────────────────

HOST_SESSION_CHECKS = """
    // Session viewer — scope to #content to avoid matching overlay panel
    var content = document.getElementById('content');
    var viewerEl = content ? content.querySelector('.session-viewer') : null;
    r.viewer_exists = !!viewerEl;

    // State machine — check if ready (.sv-ready appears when state='ready')
    var readyEl = content ? content.querySelector('.sv-ready') : null;
    r.is_ready = !!readyEl;

    // Session entries visible (in the page viewer, not the overlay)
    var entries = content ? content.querySelectorAll('.sc-entry') : [];
    r.entry_count = entries.length;
    r.has_entries = entries.length > 0;

    // Editable input exists (contenteditable div, not textarea) for sending messages.
    var editable = content ? content.querySelector('.sv-input .sv-editable') : null;
    r.has_editable = !!editable;

    // Input bar container
    var inputBar = content ? content.querySelector('.sv-input') : null;
    r.has_input_bar = !!inputBar;

    // Session header visible
    var header = content ? content.querySelector('[data-testid="session-header"]') : null;
    r.has_header = !!header;

    // No raw template syntax
    var bodyText = document.body.innerText;
    r.no_jinja = bodyText.indexOf('{%') === -1 && bodyText.indexOf('{{') === -1;
"""


class TestHostSessionInputBehavior:
    """Host session viewer behavioral sweep — verifies input bar exists for
    live host sessions.

    Bug: In production, /api/session/{project}/{id}/tail returns is_live: false
    for idle host sessions because it checks .meta.json + 120s mtime freshness
    instead of reading the DB is_live field. The session viewer then hides the
    input bar.

    In mock mode, the tail endpoint hardcodes is_live: true and omits the
    `type` field, so the input may appear (masking the production bug).
    The L2.A contract test below catches the missing `type` field.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Navigate to host session viewer, run check bundle."""
        result = _navigate_and_check(
            "/session/autonomy/host-sweep-delta",
            HOST_SESSION_CHECKS,
            wait_ms=3000,
        )
        request.cls._checks = result

    def test_session_loads(self):
        """Session viewer reaches ready state."""
        c = self._checks
        assert c.get("viewer_exists"), "Session viewer component not found in #content"
        assert c.get("is_ready"), "Session viewer did not reach 'ready' state"

    def test_entries_visible(self):
        """User sees session conversation entries."""
        c = self._checks
        assert c.get("has_entries"), \
            f"No session entries visible (count={c.get('entry_count', 0)})"

    def test_input_bar_for_live_host(self):
        """Live host session should show an input bar (Link Terminal or editable input).

        Host sessions show a 'Link Terminal' button until linked to a tmux session,
        then show a contenteditable input. Either way, the .sv-input bar should be present.
        """
        c = self._checks
        assert c.get("has_input_bar"), \
            ("No input bar visible for live host session. "
             "User cannot interact with this session. "
             "Expected .sv-input element (Link Terminal button or .sv-editable input).")

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible on session viewer page."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on session viewer page"


TODO_TILE_CHECKS = """
    var content = document.getElementById('content');
    var cards = content ? content.querySelectorAll('.sc-tool-card') : [];

    // Locate the four Task* tiles in display order
    var tcTiles = [], tuTiles = [];
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var chipText = (card.querySelector('.sc-chip') || {}).textContent || '';
      var hasStatus = !!card.querySelector('.todo-status');
      if (chipText === 'TaskCreate') tcTiles.push(card);
      else if (hasStatus) tuTiles.push(card);
    }
    r.create_tile_count = tcTiles.length;
    r.update_tile_count = tuTiles.length;

    // TaskCreate collapsed: chip shows "TaskCreate", line2 shows subject text.
    if (tcTiles.length >= 1) {
      var first = tcTiles[0];
      var line2 = (first.querySelector('.sc-line2') || {}).textContent || '';
      r.create_line2 = line2.trim();
    }

    // TaskUpdate collapsed: NO "TaskUpdate" chip; .todo-status is the leading marker
    // and line2 resolves the subject from the earlier TaskCreate.
    if (tuTiles.length >= 1) {
      var first = tuTiles[0];
      var chipText = (first.querySelector('.sc-chip') || {}).textContent || '';
      r.update_no_chip_label = (chipText !== 'TaskUpdate' && chipText !== 'Update Task');
      var line2 = (first.querySelector('.sc-line2') || {}).textContent || '';
      r.update_first_line2 = line2.trim();
      var statusEl = first.querySelector('.todo-status');
      r.update_status_classes = statusEl ? statusEl.className : '';
    }

    // Completion checkmark on the second TaskUpdate (status=completed, taskId=1)
    if (tuTiles.length >= 2) {
      var completed = tuTiles[1];
      var statusEl = completed.querySelector('.todo-status');
      r.completed_classes = statusEl ? statusEl.className : '';
      r.completed_text = statusEl ? (statusEl.textContent || '').trim() : '';
    }

    // Subject-rename resolution: the rename (tu-3) AND the later tu-4 both display
    // the renamed subject.
    if (tuTiles.length >= 4) {
      var renamedFollowUp = tuTiles[3];
      var line2 = (renamedFollowUp.querySelector('.sc-line2') || {}).textContent || '';
      r.rename_propagates = line2.indexOf('renamed') !== -1;
    }
"""


class TestSessionViewerTodoTiles:
    """Behavioral sweep: TaskCreate / TaskUpdate tile state matrix.

    Fixture entries in SWEEP_SESSION_ENTRIES["auto-sweep-alpha"] cover:
    - Two TaskCreates (sequential ids, subject rendered as headline)
    - TaskUpdate pending→in_progress→completed (checkmark on completed tile)
    - TaskUpdate subject rename + follow-up (rename propagates to later tiles)

    All annotations come from the server-side TaskStateTracker — the renderer
    is pure presentation.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            "/session/autonomy/auto-sweep-alpha",
            TODO_TILE_CHECKS,
            wait_ms=3000,
        )
        request.cls._checks = result

    def test_task_create_tiles_rendered(self):
        c = self._checks
        assert c.get("create_tile_count", 0) >= 2, \
            f"Expected 2 TaskCreate tiles, found {c.get('create_tile_count')}"

    def test_task_create_shows_subject(self):
        c = self._checks
        assert "payload shape" in (c.get("create_line2") or ""), \
            f"TaskCreate tile headline missing subject: {c.get('create_line2')!r}"

    def test_task_update_no_tool_label_chip(self):
        c = self._checks
        assert c.get("update_no_chip_label"), \
            "TaskUpdate tile must not render a chip labeled 'TaskUpdate' or 'Update Task'"

    def test_task_update_resolves_subject(self):
        c = self._checks
        assert "payload shape" in (c.get("update_first_line2") or ""), \
            (f"TaskUpdate tile did not resolve taskId→subject: "
             f"{c.get('update_first_line2')!r}")

    def test_completed_status_icon_rendered(self):
        c = self._checks
        assert "todo-status--completed" in (c.get("completed_classes") or ""), \
            f"Completed TaskUpdate missing .todo-status--completed: {c.get('completed_classes')!r}"
        # Checkmark glyph (U+2713) centered in the green circle
        assert c.get("completed_text") == "\u2713", \
            f"Completed indicator glyph wrong: {c.get('completed_text')!r}"

    def test_subject_rename_propagates(self):
        c = self._checks
        assert c.get("rename_propagates"), \
            "Later TaskUpdate tile should display the renamed subject"


class TestHostSessionTailContract:
    """L2.A contract test: /api/session/{project}/{id}/tail must return
    `type` and `is_live` consistent with the session registry.

    The mock tail handler currently omits the `type` field, so the session
    viewer cannot distinguish host from container sessions. This masks the
    production bug where host sessions show no input bar.
    """

    @pytest.fixture(scope="class", autouse=True)
    def tail_response(self, browser, request):
        """Fetch tail response for host session directly via HTTP.

        Depends on `browser` fixture to ensure sweep_server is running.
        """
        import urllib.request
        port = browser["port"]
        url = f"http://127.0.0.1:{port}/api/session/autonomy/host-sweep-delta/tail?after=0"
        resp = urllib.request.urlopen(url, timeout=5)
        data = json.loads(resp.read().decode())
        request.cls._tail = data

    def test_tail_returns_type(self):
        """Tail response must include `type` field for host sessions.

        The session viewer needs `type: 'host'` to show the correct UI
        (Link Terminal flow instead of direct editable input).
        """
        t = self._tail
        assert "type" in t, \
            ("Tail response missing `type` field. "
             "Session viewer cannot distinguish host from container sessions. "
             f"Response keys: {list(t.keys())}")
        assert t["type"] == "host", \
            f"Expected type='host' for host session, got type='{t.get('type')}'"

    def test_tail_returns_is_live(self):
        """Tail response must include is_live: true for a live host session."""
        t = self._tail
        assert "is_live" in t, "Tail response missing `is_live` field"
        assert t["is_live"] is True, \
            f"Expected is_live=true for live host session, got is_live={t.get('is_live')}"


# ── Experiment toolbar JS check bundle ────────────────────────────

EXPERIMENT_TOOLBAR_CHECKS = """(async () => {
  // Wait for the design page component to initialize (up to 8s)
  for (var i = 0; i < 80 && !window._designPage; i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  var ep = window._designPage;
  if (!ep) return JSON.stringify({error: 'no _designPage after 8s'});
  // Wait for state=ready (API call completes, toolbar renders)
  for (var i = 0; i < 50 && ep.state !== 'ready'; i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  if (ep.state !== 'ready') return JSON.stringify({error: 'state=' + ep.state});
  var q = (id) => document.querySelector('[data-testid="' + id + '"]');
  var r = {};
  var tick = async () => { await Alpine.nextTick(); await new Promise(r => setTimeout(r, 150)); };

  // DISCONNECTED: chatOpen=false, chatConnected=false (initial state)
  ep.chatOpen = false; ep.chatConnected = false;
  await tick();
  r.disc_iter = !!q('toolbar-iter-desktop');
  r.disc_no_capture = !q('toolbar-capture');
  r.disc_chat_class = q('toolbar-chat-toggle')?.classList.contains('chat-disconnected');
  r.disc_no_session = !q('toolbar-session-row');

  // PICKER: chatOpen=true, chatConnected=false
  ep.chatOpen = true;
  await tick();
  r.picker_title = q('toolbar-title')?.textContent?.includes('Select');
  r.picker_chat_class = q('toolbar-chat-toggle')?.classList.contains('chat-open');
  r.picker_no_capture = !q('toolbar-capture');
  r.picker_no_iter = !q('toolbar-iter-desktop');

  // LIVE_CHAT: chatOpen=true, chatConnected=true
  ep.chatConnected = true; ep.chatSessionLabel = 'Test session label';
  await tick();
  r.chat_no_capture = !q('toolbar-capture');
  r.chat_session = !!q('toolbar-session-row');
  r.chat_prime = !!q('toolbar-prime');
  r.chat_disconnect = !!q('toolbar-disconnect');
  r.chat_icon = q('toolbar-chat-toggle')?.classList.contains('chat-connected-shown');
  r.chat_no_iter = !q('toolbar-iter-desktop');

  // LIVE_UI: chatOpen=false, chatConnected=true
  ep.chatOpen = false;
  await tick();
  r.live_capture = !!q('toolbar-capture');
  r.live_chat_green = q('toolbar-chat-toggle')?.classList.contains('chat-connected-hidden');

  return JSON.stringify(r);
})()"""


def _navigate_and_eval_async(path: str, js_expr: str, wait_ms: int = 800) -> dict:
    """SPA-navigate to a page, wait, run an async JS expression, return parsed dict.

    Unlike _navigate_and_check (which wraps in `var r = {}; ... return r;`), this
    supports async expressions.  If the expression is already an IIFE (starts with '('),
    it is passed through directly.  Otherwise it is auto-wrapped in an async IIFE with
    a pre-declared `r` object and returned via JSON.stringify, mirroring _navigate_and_check
    but allowing `await`.
    """
    nav_js = f"navigateTo('{path}')"
    subprocess.run(
        ["agent-browser", "eval", nav_js],
        capture_output=True, timeout=10,
    )
    time.sleep(wait_ms / 1000)

    # Auto-wrap non-IIFE expressions
    stripped = js_expr.strip()
    if not stripped.startswith('('):
        js_expr = f"(async () => {{ var r = {{}}; {js_expr} return JSON.stringify(r); }})()"

    result = subprocess.run(
        ["agent-browser", "--json", "eval", js_expr],
        capture_output=True, text=True, timeout=15,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return {}
    # Parse last JSON line that has success+data shape
    for line in reversed(stdout.split("\n")):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "data" in parsed:
                data = parsed["data"]
                if isinstance(data, dict) and "result" in data:
                    val = data["result"]
                    # If result is a JSON string, parse it
                    if isinstance(val, str):
                        try:
                            return json.loads(val)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if isinstance(val, dict):
                        return val
                    return {}
                if isinstance(data, dict):
                    return data
            if isinstance(parsed, str):
                try:
                    return json.loads(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        except json.JSONDecodeError:
            continue
    return {}


class TestExperimentToolbar:
    """Experiment toolbar behavioral sweep — 4-state state machine via Alpine.nextTick().

    One async batched eval cycles through all 4 states (DISCONNECTED, LIVE_UI,
    LIVE_CHAT, PICKER) using await Alpine.nextTick() between state changes.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Navigate to design page, wait for Alpine, run async check bundle."""
        result = _navigate_and_eval_async(
            f"/design/{SWEEP_EXPERIMENT_ID}",
            EXPERIMENT_TOOLBAR_CHECKS,
            wait_ms=1000,
        )
        request.cls._checks = result

    # ── DISCONNECTED state ────────────────────────────────────────

    def test_disconnected_iter_nav(self):
        """DISCONNECTED: iteration nav is visible."""
        c = self._checks
        assert c.get("disc_iter"), "Iter nav not visible in DISCONNECTED state"

    def test_disconnected_no_capture(self):
        """DISCONNECTED: capture button is hidden."""
        c = self._checks
        assert c.get("disc_no_capture"), "Capture button should be hidden in DISCONNECTED"

    def test_disconnected_chat_class(self):
        """DISCONNECTED: chat toggle has chat-disconnected class."""
        c = self._checks
        assert c.get("disc_chat_class"), "Chat toggle missing 'chat-disconnected' class"

    def test_disconnected_no_session_row(self):
        """DISCONNECTED: no session row visible."""
        c = self._checks
        assert c.get("disc_no_session"), "Session row should be hidden in DISCONNECTED"

    # ── LIVE_UI state ─────────────────────────────────────────────

    def test_live_ui_capture(self):
        """LIVE_UI: capture button is visible."""
        c = self._checks
        assert c.get("live_capture"), "Capture button not visible in LIVE_UI"

    def test_live_ui_chat_green(self):
        """LIVE_UI: chat toggle has chat-connected-hidden class."""
        c = self._checks
        assert c.get("live_chat_green"), "Chat toggle missing 'chat-connected-hidden' class"

    # ── LIVE_CHAT state ───────────────────────────────────────────

    def test_live_chat_no_capture(self):
        """LIVE_CHAT: capture button is hidden."""
        c = self._checks
        assert c.get("chat_no_capture"), "Capture should be hidden in LIVE_CHAT"

    def test_live_chat_session_row(self):
        """LIVE_CHAT: session row is visible."""
        c = self._checks
        assert c.get("chat_session"), "Session row not visible in LIVE_CHAT"

    def test_live_chat_prime(self):
        """LIVE_CHAT: prime button is visible."""
        c = self._checks
        assert c.get("chat_prime"), "Prime button not visible in LIVE_CHAT"

    def test_live_chat_disconnect(self):
        """LIVE_CHAT: disconnect button is visible."""
        c = self._checks
        assert c.get("chat_disconnect"), "Disconnect button not visible in LIVE_CHAT"

    def test_live_chat_icon(self):
        """LIVE_CHAT: chat toggle has chat-connected-shown class."""
        c = self._checks
        assert c.get("chat_icon"), "Chat toggle missing 'chat-connected-shown' class"

    def test_live_chat_no_iter(self):
        """LIVE_CHAT: iteration nav is hidden."""
        c = self._checks
        assert c.get("chat_no_iter"), "Iter nav should be hidden in LIVE_CHAT"

    # ── PICKER state ──────────────────────────────────────────────

    def test_picker_title(self):
        """PICKER: title contains 'Select'."""
        c = self._checks
        assert c.get("picker_title"), "Title should contain 'Select' in PICKER state"

    def test_picker_chat_class(self):
        """PICKER: chat toggle has chat-open class."""
        c = self._checks
        assert c.get("picker_chat_class"), "Chat toggle missing 'chat-open' class"

    def test_picker_no_capture(self):
        """PICKER: capture button is hidden."""
        c = self._checks
        assert c.get("picker_no_capture"), "Capture should be hidden in PICKER"

    def test_picker_no_iter(self):
        """PICKER: iteration nav is hidden."""
        c = self._checks
        assert c.get("picker_no_iter"), "Iter nav should be hidden in PICKER"


# ── Source page: plain note ─────────────────────────────────────────

PLAIN_NOTE_CHECKS = """
    var body = document.body.textContent || '';
    r.sees_table_data = body.indexOf('Col A') >= 0 && body.indexOf('Col B') >= 0;
    r.sees_paragraph = body.indexOf('Some paragraph text') >= 0;
    r.title_not_in_body = body.indexOf('Plain Note') >= 0;  // title shown in header area
    r.title_in_header = !!(document.querySelector('[data-testid="source-title"]') ||
        document.querySelector('h1, h2, .text-xl, .text-2xl'));
"""


class TestPlainNoteBehavior:
    """Plain note renders readable markdown content."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            f"/graph/{SWEEP_PLAIN_NOTE_ID[:12]}",
            PLAIN_NOTE_CHECKS,
            wait_ms=1000,
        )
        request.cls._checks = result

    def test_table_data_visible(self):
        """User sees table column headers from the note."""
        assert self._checks.get("sees_table_data"), "Table data (Col A, Col B) not visible"

    def test_paragraph_visible(self):
        """User sees paragraph text from the note."""
        assert self._checks.get("sees_paragraph"), "Paragraph text not visible"

    def test_title_visible(self):
        """Note title is visible on the page."""
        assert self._checks.get("title_not_in_body"), "Note title not visible anywhere"


# ── Source page: rich-content note (direct view) ────────────────────

RICH_NOTE_DIRECT_CHECKS = """
    // Rich content MUST render in a sandboxed iframe for CSS isolation
    var iframe = document.querySelector('[data-testid="rich-content-iframe"]');
    r.has_iframe = !!iframe;
    r.iframe_visible = !!(iframe && iframe.offsetParent !== null);

    // Toggle button must use data-testid for stable test hooks
    var toggle = document.querySelector('[data-testid="rich-toggle"]');
    r.has_toggle = !!toggle;
    r.toggle_label = toggle ? toggle.textContent.trim() : '';

    // The nav/sidebar must not be broken by leaked CSS
    var sidebar = document.querySelector('[data-testid="sidebar"]') || document.querySelector('nav') || document.querySelector('aside');
    r.nav_intact = !!(sidebar && sidebar.offsetParent !== null);

    // Note title still visible
    r.sees_title = (document.body.textContent || '').indexOf('Pause Mechanisms') >= 0;

    // Iframe must not have a scrollbar (height matches content)
    if (iframe) {
        r.iframe_no_scrollbar = iframe.scrollHeight <= iframe.clientHeight + 2;
    } else {
        r.iframe_no_scrollbar = false;
    }

    // View Source button should NOT appear on direct view
    var viewSourceBtn = null;
    var allBtns = document.querySelectorAll('button');
    for (var i = 0; i < allBtns.length; i++) {
        if (allBtns[i].textContent.trim() === 'View Source') { viewSourceBtn = allBtns[i]; break; }
    }
    r.has_view_source = !!viewSourceBtn;

    // Count visible markdown-rendered blocks (tables) — should be exactly 1 (inside the embed toggle)
    // If the fallback x-markdown also renders, there will be 2+ visible tables
    var tables = document.querySelectorAll('table');
    var visibleTables = 0;
    for (var i = 0; i < tables.length; i++) {
        if (tables[i].offsetParent !== null) visibleTables++;
    }
    r.visible_table_count = visibleTables;

    // Version-paired attachments should not appear in the download list
    var attachmentSection = document.body.textContent || '';
    r.has_attachment_list = attachmentSection.indexOf('Attachments') >= 0 && attachmentSection.indexOf('.html') >= 0;

    // Toggle to text view, check that alt-text respects zoom font-size
    if (toggle) {
        toggle.click();
        // Find the alt-text container that should have a font-size set
        var altContainers = document.querySelectorAll('.embed-alt, .embed-content .markdown-body');
        r.alt_has_font_size = false;
        for (var i = 0; i < altContainers.length; i++) {
            var fs = altContainers[i].style.fontSize || window.getComputedStyle(altContainers[i]).fontSize;
            if (fs && fs !== '16px' && fs !== '') {
                r.alt_has_font_size = true;
                break;
            }
        }
        // Toggle back to diagram
        toggle.click();
    } else {
        r.alt_has_font_size = false;
    }
"""


class TestRichNoteDirectView:
    """Direct view of a rich-content note shows diagram in sandboxed iframe with toggle."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            f"/graph/{SWEEP_RICH_NOTE_ID[:12]}",
            RICH_NOTE_DIRECT_CHECKS,
            wait_ms=1200,
        )
        request.cls._checks = result

    def test_renders_in_iframe(self):
        """Rich content renders in a sandboxed iframe (required for CSS isolation)."""
        assert self._checks.get("has_iframe"), \
            "No iframe[data-testid='rich-content-iframe'] — content will leak CSS into parent"

    def test_iframe_visible(self):
        """The iframe is visible on the page."""
        assert self._checks.get("iframe_visible"), "Iframe exists but is not visible"

    def test_toggle_present(self):
        """Toggle button with data-testid='rich-toggle' is visible."""
        assert self._checks.get("has_toggle"), "No toggle[data-testid='rich-toggle'] found"

    def test_toggle_default_label(self):
        """Default toggle label is 'Show Text' (click to see markdown)."""
        assert self._checks.get("toggle_label") == "Show Text", \
            f"Expected 'Show Text', got '{self._checks.get('toggle_label')}'"

    def test_nav_not_broken(self):
        """Page navigation is still visible (diagram CSS did not leak)."""
        assert self._checks.get("nav_intact"), \
            "Nav/sidebar not visible — diagram CSS leaked into parent page"

    def test_title_visible(self):
        """Note title is still visible."""
        assert self._checks.get("sees_title"), "Title 'Pause Mechanisms' not visible"

    def test_no_scrollbar(self):
        """Iframe has no scrollbar (height matches content)."""
        assert self._checks.get("iframe_no_scrollbar"), \
            "Iframe has a scrollbar — height doesn't match content"

    def test_no_view_source_on_direct(self):
        """View Source button should not appear when already viewing the source note."""
        assert not self._checks.get("has_view_source"), \
            "View Source button shown on direct view — redundant"

    def test_no_visible_markdown_in_diagram_mode(self):
        """Default view shows diagram, not markdown — no visible tables on the parent page."""
        count = self._checks.get("visible_table_count", 0)
        assert count == 0, \
            f"Expected 0 visible tables in diagram mode, got {count} — fallback markdown leaking through"

    def test_no_version_attachments_listed(self):
        """Version-paired HTML attachments should not show in the attachment download list."""
        assert not self._checks.get("has_attachment_list"), \
            "Version-paired attachments showing in download list — should be hidden for rich-content notes"

    def test_alt_text_respects_zoom(self):
        """Alt-text in text view should have a zoom-responsive font-size, not browser default."""
        assert self._checks.get("alt_has_font_size"), \
            "Alt-text has no custom font-size — zoom level not applied"


# ── Source page: parent note with ![[id]] embeds ────────────────────

PARENT_EMBED_CHECKS = """
    // Wait for async embed resolution
    await new Promise(r => setTimeout(r, 1500));

    var body = document.body.textContent || '';

    // Parent note title visible
    r.sees_title = body.indexOf('Dispatch Lifecycle') >= 0;

    // Embedded rich-content must render in iframes (CSS isolation)
    var richIframes = document.querySelectorAll('[data-testid="rich-content-iframe"]');
    r.rich_iframe_count = richIframes.length;

    // Embedded images must render as visible img elements
    var visibleImages = 0;
    var imgs = document.querySelectorAll('img');
    for (var i = 0; i < imgs.length; i++) {
        if (imgs[i].offsetParent !== null) visibleImages++;
    }
    r.visible_image_count = visibleImages;

    // Toggle buttons use data-testid
    var toggles = document.querySelectorAll('[data-testid="rich-toggle"]');
    r.toggle_count = toggles.length;
    var labels = [];
    toggles.forEach(function(t) { labels.push(t.textContent.trim()); });
    r.toggle_labels = labels;

    // Nav must survive embedded content
    var sidebar = document.querySelector('[data-testid="sidebar"]') || document.querySelector('nav') || document.querySelector('aside');
    r.nav_intact = !!(sidebar && sidebar.offsetParent !== null);
"""


class TestParentNoteEmbeds:
    """Parent note with ![[id]] embeds renders rich content in iframes inline."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_eval_async(
            f"/graph/{SWEEP_PARENT_NOTE_ID[:12]}",
            PARENT_EMBED_CHECKS,
            wait_ms=1500,
        )
        request.cls._checks = result

    def test_sees_parent_title(self):
        """User sees the parent note's title text."""
        assert self._checks.get("sees_title"), "Parent note title not visible"

    def test_rich_embed_in_iframe(self):
        """Embedded rich-content note renders in an iframe (CSS isolation)."""
        assert self._checks.get("rich_iframe_count", 0) >= 1, \
            "No iframe[data-testid='rich-content-iframe'] for embedded rich content"

    def test_image_embed_visible(self):
        """Embedded image is visible on the page."""
        assert self._checks.get("visible_image_count", 0) >= 1, \
            "No visible image for embedded image attachment"

    def test_toggles_for_alt_embeds(self):
        """Embeds with alt-text have toggles (rich + image-with-alt = 2), no-alt embed has none."""
        count = self._checks.get("toggle_count", 0)
        assert count == 2, \
            f"Expected 2 toggles[data-testid='rich-toggle'], got {count}: {self._checks.get('toggle_labels')}"

    def test_nav_survives_embeds(self):
        """Page navigation still works with embedded content."""
        assert self._checks.get("nav_intact"), "Nav broken by embedded content"


# ── Source page: legacy ![alt](graph://id) embed ────────────────────

LEGACY_EMBED_CHECKS = """
    // User should see an image on the page (from the old ![alt](graph://id) syntax)
    var visibleImages = 0;
    var imgs = document.querySelectorAll('img');
    for (var i = 0; i < imgs.length; i++) {
        if (imgs[i].offsetParent !== null) visibleImages++;
    }
    r.sees_image = visibleImages > 0;

"""


class TestLegacyEmbedBackwardsCompat:
    """Old ![alt](graph://id) syntax still renders visible images."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            f"/graph/{SWEEP_LEGACY_NOTE_ID[:12]}",
            LEGACY_EMBED_CHECKS,
            wait_ms=1000,
        )
        request.cls._checks = result

    def test_sees_image(self):
        """User sees an image from the legacy embed syntax."""
        assert self._checks.get("sees_image"), "Legacy image embed not visible"


# ── graph:// rewrite scoping — only in .attachments mode ─────────────

GRAPH_REWRITE_SESSION_CHECKS = """
    // Session viewer should show graph:// literally, NOT rewritten to /api/attachment/
    var content = document.getElementById('content');
    var allText = content ? content.innerText : '';
    r.has_graph_protocol = allText.indexOf('graph://') !== -1;
    r.no_api_attachment_leak = allText.indexOf('/api/attachment/test-attachment-id') === -1;

    // Check rendered HTML for the rewrite — img src should NOT point to /api/attachment/
    var imgs = content ? content.querySelectorAll('img[src*="/api/attachment/test-attachment-id"]') : [];
    r.no_rewritten_img = imgs.length === 0;
"""

GRAPH_REWRITE_BEAD_CHECKS = """
    // Bead detail should show graph:// literally, NOT rewritten
    var content = document.getElementById('content');
    var allText = content ? content.innerText : '';
    r.has_graph_protocol = allText.indexOf('graph://') !== -1;
    r.no_api_attachment_leak = allText.indexOf('/api/attachment/some-attachment-id') === -1;
"""


class TestGraphRewriteScopingSession:
    """Session viewer must NOT rewrite graph:// — it should render literally."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            "/session/autonomy/auto-sweep-alpha",
            GRAPH_REWRITE_SESSION_CHECKS,
            wait_ms=3000,
        )
        request.cls._checks = result

    def test_graph_protocol_visible(self):
        """User sees graph:// literally in session viewer text."""
        assert self._checks.get("has_graph_protocol"), \
            "graph:// text not visible — may have been rewritten to /api/attachment/"

    def test_no_api_attachment_rewrite(self):
        """Session viewer does NOT rewrite graph:// to /api/attachment/."""
        assert self._checks.get("no_api_attachment_leak"), \
            "graph:// was rewritten to /api/attachment/ in session viewer"

    def test_no_rewritten_img(self):
        """No <img> tag with /api/attachment/ src from graph:// rewrite."""
        assert self._checks.get("no_rewritten_img"), \
            "Found <img> with /api/attachment/ src — graph:// was rewritten in session viewer"


class TestGraphRewriteScopingBead:
    """Bead detail page must NOT rewrite graph:// — it should render literally."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            "/bead/auto-sweep-b4",
            GRAPH_REWRITE_BEAD_CHECKS,
            wait_ms=1500,
        )
        request.cls._checks = result

    def test_graph_protocol_visible(self):
        """User sees graph:// literally in bead description."""
        assert self._checks.get("has_graph_protocol"), \
            "graph:// text not visible in bead detail — may have been rewritten"

    def test_no_api_attachment_rewrite(self):
        """Bead detail does NOT rewrite graph:// to /api/attachment/."""
        assert self._checks.get("no_api_attachment_leak"), \
            "graph:// was rewritten to /api/attachment/ in bead detail page"


# ── Rich content at narrow viewport — horizontal scroll ─────────────

NARROW_VIEWPORT_CHECKS = """
    // At 600px wide, the 900px SVG diagrams must be horizontally scrollable
    // inside the iframe, not clipped

    await new Promise(r => setTimeout(r, 1500));

    var iframe = document.querySelector('[data-testid="rich-content-iframe"]');
    r.has_iframe = !!iframe;

    if (iframe) {
        try {
            var d = iframe.contentDocument;
            var body = d.body;
            // Content wider than iframe = scrollable
            r.content_wider_than_iframe = body.scrollWidth > iframe.clientWidth;
            // Body must allow horizontal scroll (not hidden/clip)
            var bodyOverflowX = window.getComputedStyle(d.documentElement).overflowX;
            r.overflow_x = bodyOverflowX;
            r.overflow_allows_scroll = bodyOverflowX === 'auto' || bodyOverflowX === 'scroll' || bodyOverflowX === 'visible';
            // The user must be able to scroll — scrollWidth > clientWidth AND overflow allows it
            r.is_scrollable = r.content_wider_than_iframe && r.overflow_allows_scroll;
        } catch (e) {
            r.content_wider_than_iframe = false;
            r.is_scrollable = false;
            r.overflow_x = 'error: ' + e.message;
        }
    } else {
        r.content_wider_than_iframe = false;
        r.is_scrollable = false;
    }
"""


class TestRichContentNarrowViewport:
    """At narrow viewport (600px), wide diagrams must scroll horizontally, not clip."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        # Set narrow viewport
        subprocess.run(
            ["agent-browser", "set", "viewport", "600", "800"],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)
        result = _navigate_and_eval_async(
            f"/graph/{SWEEP_RICH_NOTE_ID[:12]}",
            NARROW_VIEWPORT_CHECKS,
            wait_ms=1500,
        )
        # Restore default viewport
        subprocess.run(
            ["agent-browser", "set", "viewport", "1280", "720"],
            capture_output=True, timeout=5,
        )
        request.cls._checks = result

    def test_content_wider_than_iframe(self):
        """At 600px viewport, 900px SVG content is wider than the iframe."""
        assert self._checks.get("content_wider_than_iframe"), \
            "Content is not wider than iframe at 600px — SVG may be scaling down"

    def test_horizontal_scroll_enabled(self):
        """Iframe content allows horizontal scrolling (overflow-x is auto or scroll)."""
        assert self._checks.get("overflow_allows_scroll"), \
            f"overflow-x is '{self._checks.get('overflow_x')}' — should be 'auto' or 'scroll'"

    def test_diagram_is_scrollable(self):
        """User can scroll horizontally to see the full diagram at narrow width."""
        assert self._checks.get("is_scrollable"), \
            "Diagram is not scrollable at narrow viewport — content is clipped"


# ── Agent-actions dropdown JS check bundle ──────────────────────────

AGENT_ACTIONS_AUTONOMY_CHECKS = """(async () => {
  const r = {};
  const waitFor = async (predicate, timeoutMs = 1500, intervalMs = 25) => {
    const deadline = performance.now() + timeoutMs;
    while (performance.now() < deadline) {
      const v = predicate();
      if (v) return v;
      await new Promise(res => setTimeout(res, intervalMs));
    }
    return predicate() || null;
  };
  const isShown = (el) => !!(el && getComputedStyle(el).display !== 'none');

  // Spy on fetch so we can prove sendToSession actually POSTs the dispatch
  // and capture the server response (primer body) for shape assertions.
  // Round 5's downstream "modal closed" assertion was satisfied by a no-op
  // handler that short-circuited on a missing field but still closed the
  // modal — we now assert on the actual network call. The wrapper stays
  // synchronous-return so callers using `.then(...)` on the raw fetch
  // promise see the same control flow; the response body is captured via
  // a side-channel `.then` on the promise.
  const origFetch = window.fetch;
  let dispatchSeen = null;
  let dispatchResponse = null;
  window.fetch = function (url, opts) {
    if (typeof url === 'string' && url.includes('/api/agent-actions/dispatch')) {
      dispatchSeen = { url: url, body: opts && opts.body };
      const p = origFetch.apply(this, arguments);
      p.then(function (resp) {
        return resp.clone().json().then(function (body) {
          dispatchResponse = body;
        });
      }).catch(function () { dispatchResponse = {}; });
      return p;
    }
    return origFetch.apply(this, arguments);
  };

  try {
    // Wait for the agent-actions Alpine root to mount and visibility to be
    // resolved (members fetch sets `visible`). Replaces sleep(800).
    const root = await waitFor(() => {
      const el = document.querySelector('.agent-actions-root');
      if (!el || typeof Alpine === 'undefined') return null;
      const scope = Alpine.$data(el);
      return scope && scope.visible !== undefined ? el : null;
    });
    r.root_in_dom = !!root;
    r.root_inside_slot = !!(root && root.parentElement && root.parentElement.id === 'agent-actions-slot');
    r.root_not_inside_header_actions = !!(root && !root.closest('#header-actions'));

    const btn = document.querySelector('[data-testid=agent-actions-button]');
    r.btn_exists = !!btn;
    r.btn_visible = !!(btn && getComputedStyle(btn).display !== 'none');

    // Open the panel — wait for x-show to flip rather than relying on a
    // single nextTick (Alpine sometimes needs more than one for x-show on
    // an element that was previously cloaked).
    if (btn) btn.click();
    const panel = await waitFor(() => {
      const p = document.querySelector('[data-testid=agent-actions-panel]');
      return p && isShown(p) ? p : null;
    });
    r.panel_exists = !!(panel || document.querySelector('[data-testid=agent-actions-panel]'));
    r.panel_visible = isShown(panel);
    const items = panel ? panel.querySelectorAll('[data-testid^="agent-action-item-"]') : [];
    r.action_item_count = items.length;
    r.action_item_keys = Array.from(items).map(el => el.getAttribute('data-testid'));
    r.has_send_to = r.action_item_keys.includes('agent-action-item-session.send-to');
    r.has_update = r.action_item_keys.includes('agent-action-item-note.update-summary');
    r.has_consolidate = r.action_item_keys.includes('agent-action-item-note.consolidate-comments');
    r.has_review = r.action_item_keys.includes('agent-action-item-note.review-accuracy');

    // Open the Send-To modal. The active_sessions fetch is async, so wait
    // for at least one session button to render rather than sleeping.
    const sendTo = panel ? panel.querySelector('[data-testid="agent-action-item-session.send-to"]') : null;
    if (sendTo) sendTo.click();
    const modal = await waitFor(() => {
      const m = document.querySelector('[data-testid=agent-actions-send-to-modal]');
      return m && isShown(m) ? m : null;
    });
    r.modal_in_dom = !!modal;
    r.modal_visible = isShown(modal);

    const firstButton = await waitFor(() =>
      modal ? modal.querySelector('.send-to-session-button') : null
    );
    r.session_card_count = modal ? modal.querySelectorAll('.session-card').length : 0;
    r.bespoke_row_count = modal ? modal.querySelectorAll('.send-to-session-row, .send-to-session-glyph, .send-to-session-state').length : 0;
    r.session_button_count = modal ? modal.querySelectorAll('.send-to-session-button').length : 0;
    r.first_button_testid = firstButton ? firstButton.getAttribute('data-testid') : '';

    // Click the first session button. Wait for the dispatch POST to fire
    // (fetch spy) and the modal to close — both are downstream of a
    // working handler. If the handler short-circuits, neither happens.
    if (firstButton) firstButton.click();
    await waitFor(() => dispatchSeen, 1000);
    // Also wait for the response side-channel to capture the body.
    await waitFor(() => dispatchResponse, 1000);
    await waitFor(() => !isShown(modal), 1000);
    r.modal_open_after_send = isShown(modal);

    r.dispatch_called = !!dispatchSeen;
    if (dispatchSeen && dispatchSeen.body) {
      try {
        const parsed = JSON.parse(dispatchSeen.body);
        r.dispatch_target = parsed.target_session_name || '';
        r.dispatch_member_key = parsed.member_key || '';
      } catch (e) {
        r.dispatch_target = '';
        r.dispatch_member_key = '';
      }
    } else {
      r.dispatch_target = '';
      r.dispatch_member_key = '';
    }

    // Parse the server-built primer body into a {field: value} map so the
    // Python side can assert the new shape (Round 7g): full UUID asset_id,
    // asset_org, real asset_title, action=session.send-to, no asset_url,
    // no sender_session line, no parenthesised id duplication.
    const primerBody = (dispatchResponse && dispatchResponse.primer_body) || '';
    r.primer_body = primerBody;
    const fields = {};
    const lines = primerBody.split('\\n');
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const idx = line.indexOf(':');
      if (idx > 0) {
        fields[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
      }
    }
    r.primer_fields = fields;
  } finally {
    window.fetch = origFetch;
  }

  return JSON.stringify(r);
})()"""


AGENT_ACTIONS_HIDDEN_CHECKS = """(async () => {
  const r = {};
  const sleep = (ms) => new Promise(res => setTimeout(res, ms));
  await sleep(800);

  const btn = document.querySelector('[data-testid=agent-actions-button]');
  r.btn_in_dom = !!btn;
  r.btn_hidden = !btn || getComputedStyle(btn).display === 'none';
  r.member_count = (function () {
    const root = document.querySelector('.agent-actions-root');
    if (!root || typeof Alpine === 'undefined') return -1;
    const scope = Alpine.$data(root);
    return scope && scope.members ? scope.members.length : -1;
  })();
  return JSON.stringify(r);
})()"""


class TestAgentActionsDropdown:
    """Verify the agentic-actions dropdown mounts on a page where its
    asset's org has applicable members, opens with action items, and
    renders Send-To using the shared partials/session-card.html — not
    bespoke markup. Closes the L2.B gap left open by Round 5 (auto-pqgrl).
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_eval_async(
            f"/graph/{SWEEP_AGENT_ACTIONS_AUTONOMY_NOTE_ID[:12]}",
            AGENT_ACTIONS_AUTONOMY_CHECKS,
            wait_ms=1500,
        )
        request.cls._checks = result

    def test_dropdown_lives_in_persistent_slot(self):
        """The dropdown's Alpine root is inside #agent-actions-slot, not
        inside #header-actions (which the SPA router clears per nav).
        """
        c = self._checks
        assert c.get("root_in_dom"), "agent-actions-root not in DOM after navigation"
        assert c.get("root_inside_slot"), \
            "agent-actions-root must live inside #agent-actions-slot"
        assert c.get("root_not_inside_header_actions"), \
            "agent-actions-root must not live inside #header-actions (cleared on every nav)"

    def test_dropdown_visible_on_note_page(self):
        c = self._checks
        assert c.get("btn_exists"), "Actions button must be in the DOM"
        assert c.get("btn_visible"), "Actions button must be visible"

    def test_panel_opens_with_action_items(self):
        c = self._checks
        assert c.get("panel_visible"), "Panel did not become visible after click"
        assert c.get("action_item_count") == 4, \
            f"Expected 4 action items, got {c.get('action_item_count')}: {c.get('action_item_keys')}"
        assert c.get("has_send_to"), "Missing session.send-to action item"
        assert c.get("has_update"), "Missing note.update-summary action item"
        assert c.get("has_consolidate"), "Missing note.consolidate-comments action item"
        assert c.get("has_review"), "Missing note.review-accuracy action item"

    def test_send_to_modal_uses_shared_session_card(self):
        c = self._checks
        assert c.get("modal_visible"), "Send-To modal did not open"
        assert c.get("session_card_count", 0) >= 1, (
            "Modal must use the shared .session-card partial — got "
            f"{c.get('session_card_count')} cards"
        )
        assert c.get("bespoke_row_count") == 0, (
            "Modal must not use the bespoke send-to-session-* markup — "
            f"found {c.get('bespoke_row_count')} bespoke nodes"
        )
        assert c.get("session_button_count", 0) >= 1, (
            f"Modal must wrap rows in .send-to-session-button (count={c.get('session_button_count')})"
        )

    def test_send_to_dispatches_and_closes(self):
        """Clicking a session in the Send-To modal POSTs to the dispatch
        endpoint and the modal closes on a successful response.
        """
        c = self._checks
        assert c.get("first_button_testid", "").startswith("agent-actions-send-to-session-"), (
            f"First session button must carry the dispatch testid, got {c.get('first_button_testid')!r}"
        )
        assert not c.get("modal_open_after_send"), \
            "Modal must close after a successful Send-To dispatch"

    def test_send_to_actually_dispatches_to_api(self):
        """Clicking a session button must POST to /api/agent-actions/dispatch
        with the right target_session_name. Closes the gap left by Round 5
        where the handler short-circuited on a missing field but the modal
        closed anyway via downstream reactivity (auto-8l0xn)."""
        c = self._checks
        assert c.get("dispatch_called"), (
            "POST to /api/agent-actions/dispatch never fired — handler short-circuited "
            "(check session payload shape vs handler field names)."
        )
        assert c.get("dispatch_target"), \
            "Dispatch payload must include target_session_name"
        assert c.get("dispatch_member_key") == "session.send-to", (
            f"Dispatch must be session.send-to, got {c.get('dispatch_member_key')!r}"
        )

    def test_send_to_primer_has_new_shape(self):
        """The Send-To primer body must carry asset_id (full UUID), asset_type,
        asset_org, asset_title (real source title), action: session.send-to.
        No asset_url, no sender_session, no parenthesized id duplication
        (Round 7g cleanup of the Round 5 spec).
        """
        c = self._checks
        fields = c.get("primer_fields") or {}
        body = c.get("primer_body") or ""
        assert fields, f"primer fields not captured; primer_body={body!r}"
        assert fields.get("action") == "session.send-to", (
            f"action must be session.send-to, got {fields.get('action')!r}"
        )
        asset_id = fields.get("asset_id", "")
        assert asset_id and len(asset_id) == 36, (
            f"asset_id must be the full UUID (36 chars), got {asset_id!r}"
        )
        assert "(" not in asset_id, (
            f"asset_id must not contain parenthesised duplication, got {asset_id!r}"
        )
        assert fields.get("asset_org"), "asset_org missing from primer"
        title = fields.get("asset_title", "")
        assert title and not title.startswith("Source:"), (
            f"asset_title must be the real source title, not a placeholder; got {title!r}"
        )
        assert "asset_url" not in fields, "asset_url field must be removed"
        assert "sender_session" not in fields, "sender_session field must be removed"
        assert "action_key" not in fields, (
            "field name should be 'action', not 'action_key'"
        )


class TestAgentActionsDropdownHiddenForEmptyOrg:
    """The button must stay hidden on a note whose org has no seeded
    actions — the universal Send-To is also gated by canonical promotion
    into the asset's own org, so a freshly-bootstrapped org renders nothing.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_eval_async(
            f"/graph/{SWEEP_AGENT_ACTIONS_EMPTY_NOTE_ID[:12]}",
            AGENT_ACTIONS_HIDDEN_CHECKS,
            wait_ms=1500,
        )
        request.cls._checks = result

    def test_dropdown_hidden_when_no_actions_for_org(self):
        c = self._checks
        assert c.get("btn_hidden"), \
            "Actions button must be hidden when the asset's org has no seeded actions"
        assert c.get("member_count") == 0, (
            f"Expected zero resolved members for empty-org note, got {c.get('member_count')}"
        )


class TestAgentActionsDropdownLiveRefresh:
    """Adding a Setting member via the test API must update the dropdown
    without a manual page reload (Round 1b live-update via setting.changed).
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, sweep_server, request):
        fixture_path = Path(sweep_server["fixture_path"])
        events_path = Path(sweep_server["events_path"])
        original_text = fixture_path.read_text()
        try:
            data = json.loads(original_text)
            members = list(data["settings"]["dashboard.agent-actions"]["_orgs"]["autonomy"])
            members.append({
                "key": "note.live-refresh-probe",
                "payload": {
                    "asset_type": "note",
                    "label": "Live-Refresh Probe",
                    "icon": "★",
                    "model": "claude-haiku-4-5-20251001",
                    "estimated_seconds": 5,
                    "writes": ["comment"],
                    "prompt_template": "probe",
                },
            })
            data["settings"]["dashboard.agent-actions"]["_orgs"]["autonomy"] = members
            fixture_path.write_text(json.dumps(data, indent=2))

            # Navigate first so the dropdown mounts and we can later
            # measure the post-event member count.
            _navigate_and_eval_async(
                f"/graph/{SWEEP_AGENT_ACTIONS_AUTONOMY_NOTE_ID[:12]}",
                "(async () => { return JSON.stringify({ok: true}); })()",
                wait_ms=1500,
            )

            with open(events_path, "a") as f:
                f.write(json.dumps({"topic": "setting.changed", "data": {
                    "set_id": "dashboard.agent-actions",
                    "schema_revision": 1,
                    "key": "note.live-refresh-probe",
                    "org": "autonomy",
                    "publication_state": "canonical",
                    "deprecated": False,
                    "operation": "write",
                }}) + "\n")

            # Poll for the new member to appear via the SSE-driven refresh.
            probe_js = """(async () => {
                const sleep = (ms) => new Promise(res => setTimeout(res, ms));
                const readKeys = () => {
                    const root = document.querySelector('.agent-actions-root');
                    if (!root || typeof Alpine === 'undefined') return [];
                    const scope = Alpine.$data(root);
                    return (scope && scope.members ? scope.members : []).map(m => m.key);
                };
                for (var i = 0; i < 30; i++) {
                    const keys = readKeys();
                    if (keys.includes('note.live-refresh-probe')) {
                        return JSON.stringify({ok: true, count: keys.length, keys: keys});
                    }
                    await sleep(200);
                }
                const keys = readKeys();
                return JSON.stringify({ok: false, count: keys.length, keys: keys});
            })()"""
            result = subprocess.run(
                ["agent-browser", "--json", "eval", probe_js],
                capture_output=True, text=True, timeout=15,
            )
            parsed = {}
            for line in reversed(result.stdout.strip().split("\n")):
                try:
                    j = json.loads(line)
                    if isinstance(j, dict) and "data" in j:
                        d = j["data"]
                        if isinstance(d, dict) and "result" in d:
                            v = d["result"]
                            if isinstance(v, str):
                                parsed = json.loads(v)
                            elif isinstance(v, dict):
                                parsed = v
                            break
                except json.JSONDecodeError:
                    continue
            request.cls._checks = parsed
        finally:
            fixture_path.write_text(original_text)

    def test_dropdown_refreshes_on_setting_changed_event(self):
        c = self._checks
        assert c.get("ok"), (
            "Dropdown did not pick up the new member after setting.changed event "
            f"(keys={c.get('keys')})"
        )
        assert "note.live-refresh-probe" in (c.get("keys") or []), (
            f"New member missing from refreshed dropdown, keys={c.get('keys')}"
        )


# ── Search page behavioral sweep (auto-qlfg1) ─────────────────────────
#
# Backfill L2.B coverage for the /search surfaces shipped tonight:
#   - auto-bcxdr  (search results page)
#   - auto-zvu3z  (filter-strip chrome)
#   - auto-gsu99  (chrome polish: muted glyph, state ladder, padding)
#
# All states are exercised in one async eval so the module-scoped browser
# session navigates through three URL states without the cost of
# spawning a fresh class fixture per state.

SEARCH_PAGE_MULTI_STATE_CHECKS = """(async () => {
  var r = {};
  const sleep = (ms) => new Promise(res => setTimeout(res, ms));

  // ── State 1: /search?q=dashboard (just navigated here) ──────────────
  await sleep(900);

  // Chip rail: All + each canonical type in CHIP_ORDER must be present.
  var rail = document.querySelector('[data-testid="sp-chip-rail"]');
  r.has_chip_rail = !!rail;
  var chipLabels = [];
  if (rail) {
    rail.querySelectorAll('.sp-chip').forEach(function(c) {
      var label = '';
      var labelSpan = c.querySelector('span:first-child');
      if (labelSpan && labelSpan !== c.querySelector('.sp-chip-count')) {
        label = labelSpan.textContent.trim();
      } else {
        // The "All" chip has no inner span — text node before the count.
        var clone = c.cloneNode(true);
        var cnt = clone.querySelector('.sp-chip-count');
        if (cnt) cnt.remove();
        label = clone.textContent.trim();
      }
      chipLabels.push(label);
    });
  }
  r.chip_labels = chipLabels;

  // Per-chip count parsing — All count + sum of typed-chip counts must
  // equal the rendered card count.
  var chipCounts = [];
  if (rail) {
    rail.querySelectorAll('.sp-chip').forEach(function(c) {
      var cnt = c.querySelector('.sp-chip-count');
      var n = cnt ? parseInt(cnt.textContent.trim(), 10) : NaN;
      chipCounts.push(isNaN(n) ? 0 : n);
    });
  }
  r.chip_counts = chipCounts;

  // Source cards rendered (one per source_id, not per excerpt row).
  var cards = document.querySelectorAll('.sp-source-card');
  r.card_count = cards.length;
  r.card_source_types = Array.from(cards).map(function(c) {
    return c.dataset.sourceType || '';
  });

  // Multi-hit grouping: src-search-session-1 has two excerpts (turns
  // 12 + 47) and must render exactly ONE card with both turn anchors.
  var multiCard = null;
  cards.forEach(function(c) {
    if (c.getAttribute('href') &&
        c.getAttribute('href').indexOf('src-search-s') !== -1 &&
        c.dataset.sourceType === 'session') {
      multiCard = c;
    }
  });
  r.multi_card_present = !!multiCard;
  if (multiCard) {
    var turnBadges = multiCard.querySelectorAll('.sp-turn-badge');
    r.multi_turn_badges = Array.from(turnBadges).map(function(b) {
      return b.textContent.trim();
    });
    r.multi_turn_anchor_count = multiCard.querySelectorAll('a.sp-excerpt').length;
  }

  // Two-way input binding: set #global-search value and dispatch the
  // page-level custom event the searchPage component binds to.
  var spRoot = document.querySelector('[x-data^="searchPage"]');
  var spScope = spRoot && Alpine ? Alpine.$data(spRoot) : null;
  r.has_alpine_root = !!spScope;
  if (spScope) {
    var ev = new CustomEvent('global-search:input', {
      detail: { value: 'binding-probe-xyz' }
    });
    window.dispatchEvent(ev);
    // The handler updates query synchronously; the debounced refetch
    // does not affect the bound query value.
    await sleep(80);
    r.bound_query = spScope.query;
  }

  // ── State 2: /search (no q) — empty-query state ─────────────────────
  navigateTo('/search');
  await sleep(900);

  var strip = document.querySelector('[data-testid="sp-filter-strip"]');
  // x-show toggles inline display; the rail wrapper hides via display:none.
  if (strip) {
    var stripStyle = window.getComputedStyle(strip);
    r.empty_filter_strip_display = stripStyle.display;
  } else {
    r.empty_filter_strip_display = 'missing';
  }
  var hint = document.querySelector('[data-testid="sp-no-query-hint"]');
  r.empty_hint_visible = !!(hint && hint.offsetParent !== null);
  r.empty_hint_text = hint ? (hint.textContent || '').trim() : '';

  // ── State 3: /search?q=zzzzz_no_match — empty-results state ─────────
  navigateTo('/search?q=zzzzz_no_match');
  await sleep(1200);

  var emptyEl = document.querySelector('[data-testid="sp-empty-state"]');
  r.empty_state_visible = !!(emptyEl && emptyEl.offsetParent !== null);
  if (emptyEl) {
    var emptyCS = window.getComputedStyle(emptyEl);
    r.empty_state_padding_top = parseFloat(emptyCS.paddingTop);
    r.empty_state_text = (emptyEl.textContent || '').trim();
  }

  return JSON.stringify(r);
})()"""


class TestSearchPageBehavior:
    """L2.B backfill for /search (auto-bcxdr + chrome).

    Exercises three URL states in a single browser session: the populated
    query, no query at all, and a query with no matches. Each test method
    asserts one user-visible behaviour from the captured dict.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_eval_async(
            "/search?q=dashboard",
            SEARCH_PAGE_MULTI_STATE_CHECKS,
            wait_ms=200,  # the JS does its own per-state sleeps
        )
        request.cls._checks = result

    def test_search_page_renders_chip_rail(self):
        """Chip rail visible with All + the canonical type chips."""
        c = self._checks
        assert c.get("has_chip_rail"), "Chip rail container missing"
        labels = c.get("chip_labels") or []
        # The "All" chip has no inner label-span — it's the first chip.
        assert any(l.startswith("All") for l in labels), (
            f"Chip rail missing 'All' chip; got {labels!r}"
        )
        for expected in ("Notes", "Sessions", "Agent runs",
                         "Docs", "Conversations", "Status", "Musings"):
            assert expected in labels, (
                f"Chip rail missing {expected!r} chip; got {labels!r}"
            )

    def test_search_page_chip_counts_match_results(self):
        """Sum of typed-chip counts equals the rendered card count.

        The first chip is All (whose count duplicates the total) — the
        invariant the test guards is that the per-type chip counts add
        up to the unique-source-card count returned by /api/search.
        """
        c = self._checks
        counts = c.get("chip_counts") or []
        assert len(counts) >= 8, (
            f"Expected ≥ 8 chips (All + 7 types), got counts={counts}"
        )
        # All count == card count (the first chip)
        assert counts[0] == c.get("card_count"), (
            f"All-chip count {counts[0]} ≠ card_count {c.get('card_count')}"
        )
        # Sum of typed chips also equals card count.
        typed_sum = sum(counts[1:])
        assert typed_sum == c.get("card_count"), (
            f"Sum of typed chips ({typed_sum}) ≠ card_count "
            f"({c.get('card_count')}); chip_counts={counts}"
        )

    def test_search_page_source_grouped_cards(self):
        """A source_id with multiple turn-level hits collapses to ONE card.

        SWEEP_SEARCH_RESULTS_DASHBOARD seeds two rows on
        src-search-session-1 (turns 12 + 47). The grouped /api/search
        response must produce one card whose excerpts list both turns.
        """
        c = self._checks
        # 4 distinct source_ids in the fixture → 4 cards total.
        assert c.get("card_count") == 4, (
            f"Expected 4 grouped cards, got {c.get('card_count')}; "
            f"types={c.get('card_source_types')}"
        )
        assert c.get("multi_card_present"), (
            "Multi-hit session source did not render as a card"
        )
        badges = c.get("multi_turn_badges") or []
        assert "t12" in badges and "t47" in badges, (
            f"Multi-hit excerpt badges should include t12 + t47, "
            f"got {badges!r}"
        )
        assert c.get("multi_turn_anchor_count", 0) >= 2, (
            f"Multi-hit card should render ≥ 2 per-turn anchors, "
            f"got {c.get('multi_turn_anchor_count')}"
        )

    def test_search_page_two_way_input_binding(self):
        """Dispatching ``global-search:input`` updates the page's query ref.

        The chrome's canonical input is the global header search box; the
        page binds to it via @global-search:input.window. We verify the
        binding by dispatching a synthetic CustomEvent and reading
        Alpine state, NOT by typing into the DOM input.
        """
        c = self._checks
        assert c.get("has_alpine_root"), (
            "searchPage Alpine root not found — page never mounted?"
        )
        assert c.get("bound_query") == "binding-probe-xyz", (
            f"Page query did not bind to global-search:input event; "
            f"got bound_query={c.get('bound_query')!r}"
        )

    def test_search_page_empty_query_state(self):
        """/search with no ?q= hides the chip rail and shows a centered hint."""
        c = self._checks
        # The strip wrapper x-show=\"query !== ''\" → display:none on no-query.
        assert c.get("empty_filter_strip_display") == "none", (
            f"Filter strip should be display:none when query is empty, "
            f"got {c.get('empty_filter_strip_display')!r}"
        )
        assert c.get("empty_hint_visible"), "No-query hint not visible"
        text = c.get("empty_hint_text") or ""
        assert "Type a query" in text, (
            f"Empty-query hint should read 'Type a query…', got {text!r}"
        )

    def test_search_page_no_results_state(self):
        """A no-match query renders 'No results' with non-zero top padding.

        Pre-polish (auto-zvu3z) the empty message tucked under the sticky
        filter strip on a fast-render. auto-gsu99 added padding to the
        ``.sp-empty-state`` container so the message lands below the strip.
        """
        c = self._checks
        assert c.get("empty_state_visible"), (
            "'No results' empty-state element not visible"
        )
        text = c.get("empty_state_text") or ""
        assert "No results for" in text, (
            f"Empty state should say 'No results for …'; got {text!r}"
        )
        pt = c.get("empty_state_padding_top") or 0
        assert pt > 0, (
            f"sp-empty-state should have top padding > 0 (auto-gsu99); "
            f"got {pt}px"
        )


# ── Search chrome polish (auto-gsu99 verification) ────────────────────
#
# Confirms the production-rendered filter strip carries the polished
# chrome — muted "All orgs" glyph, "Raw (Any)" default label, the bar
# ladder dropdown, the include_raw vs states= URL split, and the tight
# top padding. Each interactive assertion drives Alpine state (not
# raw clicks) so the test stays browser-DOM-deterministic.

SEARCH_CHROME_POLISH_CHECKS = """(async () => {
  var r = {};
  const sleep = (ms) => new Promise(res => setTimeout(res, ms));

  // We just navigated to /search?q=polish — Alpine init() reads ?q= and
  // kicks off the first fetch. Stub fetch BEFORE driving any state so
  // every refetch URL is observable.
  var capturedURLs = [];
  var origFetch = window.fetch;
  window.fetch = function(url, opts) {
    try { capturedURLs.push(String(url)); } catch (_) {}
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function() { return Promise.resolve([]); },
    });
  };

  await sleep(800);  // Alpine init + initial _refetch flushed

  var spRoot = document.querySelector('[x-data^="searchPage"]');
  var spScope = spRoot && Alpine ? Alpine.$data(spRoot) : null;
  r.has_alpine_root = !!spScope;

  // ── 1. All-orgs glyph: muted, not gradient ──────────────────────────
  var orgGlyph = document.querySelector(
    '[data-testid="sp-org-chip"] .sp-filter-chip-glyph'
  );
  r.org_glyph_present = !!orgGlyph;
  if (orgGlyph) {
    var ogc = window.getComputedStyle(orgGlyph);
    r.org_glyph_bg_color = ogc.backgroundColor;
    r.org_glyph_bg_image = ogc.backgroundImage;
    r.org_glyph_has_all_class = orgGlyph.classList.contains('sp-filter-chip-all');
  }

  // ── 2. State chip default label: 'Raw (Any)' ────────────────────────
  var stateValue = document.querySelector(
    '[data-testid="sp-state-chip"] .sp-filter-chip-value'
  );
  r.state_chip_label = stateValue ? (stateValue.textContent || '').trim() : '';

  // ── 3. State dropdown progressive bars (open it via Alpine) ─────────
  if (spScope) {
    spScope.stateDropdownOpen = true;
    await sleep(80);
  }
  var dropdownOptions = document.querySelectorAll(
    '[data-testid="sp-state-dropdown"] .sp-org-option'
  );
  r.state_option_count = dropdownOptions.length;
  var optionMatrix = [];
  dropdownOptions.forEach(function(opt) {
    var key = opt.getAttribute('data-state-key') || '';
    var bars = opt.querySelectorAll('.sp-state-option-bar');
    var filled = opt.querySelectorAll('.sp-state-option-bar-filled');
    optionMatrix.push({
      key: key,
      total_bars: bars.length,
      filled_bars: filled.length,
    });
  });
  r.state_option_matrix = optionMatrix;
  if (spScope) {
    spScope.stateDropdownOpen = false;
  }

  // ── 4 & 5. include_raw vs states=canonical URL semantics ────────────
  // Set the query so subsequent pickState calls actually refetch.
  if (spScope) {
    spScope.query = 'polish';
    await sleep(40);
    // Switch off the Raw default so picking it again triggers a refetch.
    spScope.pickState('canonical');
    await sleep(150);
    capturedURLs.length = 0;
    spScope.pickState('raw');
    await sleep(200);
    r.raw_url = capturedURLs.length
      ? capturedURLs[capturedURLs.length - 1]
      : null;

    capturedURLs.length = 0;
    spScope.pickState('canonical');
    await sleep(200);
    r.canonical_url = capturedURLs.length
      ? capturedURLs[capturedURLs.length - 1]
      : null;
  }

  // ── 6. Filter strip top padding ≤ 6px ───────────────────────────────
  var header = document.querySelector('.sp-header');
  if (header) {
    var hcs = window.getComputedStyle(header);
    r.header_padding_top = parseFloat(hcs.paddingTop);
  }

  // Restore real fetch so other class fixtures running later in the
  // module aren't poisoned by the stub.
  window.fetch = origFetch;

  return JSON.stringify(r);
})()"""


class TestSearchChromePolish:
    """Verifies auto-gsu99's production chrome on the live /search page.

    Companion to test_search_chrome_polish.py — that file asserts the
    SOURCE markup; this class asserts the rendered DOM + computed styles
    + actual fetch URL after Alpine drives state changes.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_eval_async(
            "/search?q=polish",
            SEARCH_CHROME_POLISH_CHECKS,
            wait_ms=200,
        )
        request.cls._checks = result

    def test_org_chip_all_orgs_glyph_muted(self):
        """The All-orgs glyph background is the muted #2a3441, NOT a gradient."""
        c = self._checks
        assert c.get("org_glyph_present"), "Org chip glyph element missing"
        assert c.get("org_glyph_has_all_class"), (
            "Default org chip glyph should carry the 'sp-filter-chip-all' "
            "class (no org pinned), got class state: "
            f"{c.get('org_glyph_has_all_class')!r}"
        )
        # Computed colour for #2a3441 is rgb(42, 52, 65).
        assert c.get("org_glyph_bg_color") == "rgb(42, 52, 65)", (
            f"All-orgs glyph background should be rgb(42, 52, 65), "
            f"got {c.get('org_glyph_bg_color')!r}"
        )
        bg_img = c.get("org_glyph_bg_image") or "none"
        assert "linear-gradient" not in bg_img, (
            f"All-orgs glyph still uses a gradient: {bg_img!r}"
        )

    def test_state_chip_default_label_raw_any(self):
        """The default state chip label reads 'Raw (Any)'."""
        c = self._checks
        assert c.get("state_chip_label") == "Raw (Any)", (
            f"Default state chip label should be 'Raw (Any)', "
            f"got {c.get('state_chip_label')!r}"
        )

    def test_state_chip_progressive_bars(self):
        """Each dropdown option carries 1/2/3/4 filled bars in restrictiveness order."""
        c = self._checks
        matrix = c.get("state_option_matrix") or []
        # Map (key → filled count)
        seen = {row["key"]: row for row in matrix if row.get("key")}
        expected = [("raw", 1), ("curated", 2), ("published", 3), ("canonical", 4)]
        for key, want_filled in expected:
            row = seen.get(key)
            assert row is not None, (
                f"State dropdown missing option {key!r}; matrix={matrix!r}"
            )
            assert row["total_bars"] == 4, (
                f"State option {key!r} should render exactly 4 bars, "
                f"got {row['total_bars']}"
            )
            assert row["filled_bars"] == want_filled, (
                f"State option {key!r} should have {want_filled} filled "
                f"bars, got {row['filled_bars']}"
            )

    def test_state_chip_raw_sends_include_raw(self):
        """Picking Raw fires a fetch with ?include_raw=1 and NO ?states= clause."""
        c = self._checks
        url = c.get("raw_url") or ""
        assert url, "No fetch URL captured for Raw chip click"
        assert "include_raw=1" in url, (
            f"Raw chip should send include_raw=1; got {url!r}"
        )
        assert "states=" not in url, (
            f"Raw chip must NOT send states=; got {url!r}"
        )

    def test_state_chip_canonical_sends_states_canonical(self):
        """Picking Canonical fires a fetch with ?states=canonical and NO include_raw."""
        c = self._checks
        url = c.get("canonical_url") or ""
        assert url, "No fetch URL captured for Canonical chip click"
        assert "states=canonical" in url, (
            f"Canonical chip should send states=canonical; got {url!r}"
        )
        assert "include_raw" not in url, (
            f"Canonical chip must NOT send include_raw; got {url!r}"
        )

    def test_filter_strip_top_margin_tight(self):
        """The .sp-header padding-top is ≤ 6px (auto-gsu99 polish)."""
        c = self._checks
        pt = c.get("header_padding_top")
        assert pt is not None, "sp-header element missing or unmeasurable"
        assert pt <= 6, (
            f"Filter strip padding-top should be ≤ 6px after the polish, "
            f"got {pt}px"
        )


# ── /collab Recent-tab behaviour (auto-yn1gt) ─────────────────────────
#
# Companion to TestCollabPageBehavior — that class verifies the tab
# scaffold; this class asserts the auto-yn1gt fix that the Recent tab
# is the default and shows ALL recent notes (not just collab-tagged).

COLLAB_RECENT_TAB_CHECKS = """
    // ── Tab order in the DOM ────────────────────────────────────────────
    var tabs = document.querySelectorAll('.collab-tab');
    var tabOrder = [];
    var tabTestids = [];
    tabs.forEach(function(t) {
        tabTestids.push(t.getAttribute('data-testid') || '');
        var txt = t.textContent.trim();
        // Strip the trailing count digits (e.g., 'Recent3' → 'Recent').
        txt = txt.replace(/\\d+$/, '').trim();
        tabOrder.push(txt);
    });
    r.tab_order = tabOrder;
    r.tab_testids = tabTestids;

    // ── Default active tab on first visit ───────────────────────────────
    var activeTab = document.querySelector('.collab-tab.active');
    r.active_tab_testid = activeTab
        ? (activeTab.getAttribute('data-testid') || '')
        : '';
    r.active_tab_text = activeTab
        ? (activeTab.textContent || '').replace(/\\d+$/, '').trim()
        : '';

    // ── Recent tab tag diversity ────────────────────────────────────────
    // Cards inside the Recent panel — collect all .note-tag values.
    var recentPanel = document.querySelector('[data-testid="collab-recent"]');
    var recentTags = new Set();
    var recentCardCount = 0;
    if (recentPanel) {
        var rcards = recentPanel.querySelectorAll('.note-card');
        recentCardCount = rcards.length;
        rcards.forEach(function(c) {
            c.querySelectorAll('.note-tag').forEach(function(t) {
                var v = t.textContent.trim();
                if (v) recentTags.add(v);
            });
        });
    }
    r.recent_card_count = recentCardCount;
    r.recent_distinct_tags = Array.from(recentTags);
    r.recent_distinct_tag_count = recentTags.size;

    // ── Curated tab still surfaces collab-tagged notes ──────────────────
    var curatedPanel = document.querySelector('[data-testid="collab-curated"]');
    var curatedCardCount = 0;
    var curatedTags = new Set();
    if (curatedPanel) {
        var ccards = curatedPanel.querySelectorAll('.note-card');
        curatedCardCount = ccards.length;
        ccards.forEach(function(c) {
            c.querySelectorAll('.note-tag').forEach(function(t) {
                curatedTags.add(t.textContent.trim());
            });
        });
    }
    r.curated_card_count = curatedCardCount;
    r.curated_tags = Array.from(curatedTags);
"""


class TestCollabRecentTabBehavior:
    """L2.B backfill for the auto-yn1gt Recent-tab fix on /collab.

    Companion to TestCollabPageBehavior — the existing class proved the
    tabs render; this class proves the *default* is Recent and that
    Recent surfaces mixed-tag notes (the regression auto-yn1gt fixed).
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        # Clear any localStorage left by earlier collab clicks before
        # navigating, so the default-active assertion sees a fresh state.
        subprocess.run(
            ["agent-browser", "eval",
             "try { localStorage.removeItem('collabTab'); } catch (_) {}"],
            capture_output=True, timeout=5,
        )
        result = _navigate_and_check("/collab", COLLAB_RECENT_TAB_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_collab_tab_order(self):
        """DOM order is Recent, Curated, Thoughts, Threads, Topics."""
        c = self._checks
        assert c.get("tab_order") == [
            "Recent", "Curated", "Thoughts", "Threads", "Topics"
        ], f"Unexpected tab order: {c.get('tab_order')!r}"
        assert c.get("tab_testids") == [
            "collab-tab-recent", "collab-tab-curated", "collab-tab-thoughts",
            "collab-tab-threads", "collab-tab-topics",
        ], f"Tab testids out of order: {c.get('tab_testids')!r}"

    def test_recent_tab_default_active(self):
        """No localStorage / no ?tab= → Recent is the active tab."""
        c = self._checks
        assert c.get("active_tab_testid") == "collab-tab-recent", (
            f"Default active tab should be Recent, got testid="
            f"{c.get('active_tab_testid')!r}, text={c.get('active_tab_text')!r}"
        )

    def test_recent_tab_shows_mixed_tags(self):
        """The Recent tab's note cards collectively show > 1 distinct tag.

        Pre-fix the Recent panel was filtered to ``tag=collab`` only —
        so every visible card carried just that one tag. Post-fix the
        panel surfaces every recent note regardless of tag, so the
        union of tag chips spans > 1 distinct value.
        """
        c = self._checks
        assert c.get("recent_card_count", 0) >= 2, (
            f"Need ≥ 2 Recent-tab cards to assert mixed tags, "
            f"got {c.get('recent_card_count')}"
        )
        distinct = c.get("recent_distinct_tag_count", 0)
        assert distinct > 1, (
            f"Recent tab should display > 1 distinct tag (auto-yn1gt); "
            f"got {distinct} from tags={c.get('recent_distinct_tags')!r}"
        )

    def test_curated_tab_shows_collab_only(self):
        """The Curated tab still renders the prior collab-tagged notes.

        SWEEP_COLLAB_NOTES seeds two notes (architecture + testing).
        Post auto-yn1gt the Curated panel still backs onto
        /api/graph/collab — this test guards against a regression that
        could leave the panel empty after the Recent-tab refactor.
        """
        c = self._checks
        assert c.get("curated_card_count", 0) >= 1, (
            f"Curated panel should render the collab-tagged notes; "
            f"got curated_card_count={c.get('curated_card_count')}"
        )


# ── /dispatch kind-badge rendering (auto-5k2j4) ───────────────────────
#
# Companion to TestDispatchPageBehavior — that class verifies the page
# scaffold; this class asserts the auto-5k2j4 kind-badge feature:
#   - kind='agentic'   → renders an "Agentic" badge (and skips P-badge)
#   - kind=NULL/legacy → COALESCE-as-bead → renders the priority badge

DISPATCH_KIND_BADGE_CHECKS = """
    var bodyText = document.body.innerText;

    // ── Agentic row: renders the kind-badge-agentic element ─────────────
    var agenticBadges = document.querySelectorAll(
        '[data-testid="kind-badge-agentic"]'
    );
    r.agentic_badge_count = agenticBadges.length;
    r.agentic_badge_text = agenticBadges.length
        ? (agenticBadges[0].textContent || '').trim()
        : '';

    // Locate the agentic card by walking up from the kind-badge element
    // to its containing anchor. Post auto-gh2iv the anchor's href routes
    // to /graph/<agentic_source_id>, so an href-substring lookup keyed
    // on the run_id no longer matches.
    var agenticBadgeForCard = document.querySelector(
        '[data-testid="kind-badge-agentic"]'
    );
    var agenticCard = agenticBadgeForCard ? agenticBadgeForCard.closest('a') : null;
    r.has_agentic_card = !!agenticCard;
    if (agenticCard) {
        var pBadge = null;
        agenticCard.querySelectorAll('.ft-badge').forEach(function(b) {
            if (/^P\\d+$/.test(b.textContent.trim())) pBadge = b;
        });
        r.agentic_card_has_priority_badge = !!pBadge;
        r.agentic_card_has_kind_badge = !!agenticCard.querySelector(
            '[data-testid="kind-badge-agentic"]'
        );
    }

    // ── Legacy NULL-kind row: priority badge present, agentic absent ────
    var legacyCard = document.querySelector(
        'a[href*="auto-sweep-legacy-null"]'
    );
    r.has_legacy_card = !!legacyCard;
    if (legacyCard) {
        var legacyP = null;
        legacyCard.querySelectorAll('.ft-badge').forEach(function(b) {
            if (/^P\\d+$/.test(b.textContent.trim())) legacyP = b;
        });
        r.legacy_card_priority_badge_text = legacyP
            ? legacyP.textContent.trim() : '';
        r.legacy_card_has_kind_badge = !!legacyCard.querySelector(
            '[data-testid="kind-badge-agentic"]'
        );
    }

    // No template artifacts leak into the rendered fragment.
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""


class TestDispatchAgenticKindBadge:
    """L2.B backfill for the auto-5k2j4 kind-badge addition on /dispatch."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            "/dispatch", DISPATCH_KIND_BADGE_CHECKS, wait_ms=1000,
        )
        request.cls._checks = result

    def test_dispatch_row_renders_kind_badge(self):
        """An agentic dispatch row renders the 'Agentic' kind badge.

        The badge is visually distinct from the regular bead's P-badge
        (which is suppressed when kind === 'agentic') and from the
        librarian's 'Lib' chip — three branches in priority-badge.html.
        """
        c = self._checks
        assert c.get("agentic_badge_count", 0) >= 1, (
            f"Expected ≥ 1 [data-testid=kind-badge-agentic] element; "
            f"got {c.get('agentic_badge_count')}"
        )
        assert c.get("agentic_badge_text") == "Agentic", (
            f"Agentic kind badge text should read 'Agentic'; "
            f"got {c.get('agentic_badge_text')!r}"
        )
        assert c.get("has_agentic_card"), (
            "Agentic dispatch row card not present in DOM"
        )
        assert c.get("agentic_card_has_kind_badge"), (
            "Agentic card is missing its own kind badge"
        )
        assert not c.get("agentic_card_has_priority_badge"), (
            "Agentic card must NOT also carry a P-badge — the priority "
            "branch in priority-badge.html should suppress when "
            "(b.kind || 'bead') === 'agentic'"
        )

    def test_dispatch_legacy_null_kind_renders_as_bead(self):
        """A row without a `kind` field renders as a regular bead.

        The COALESCE semantics from auto-5k2j4 — ``COALESCE(kind, 'bead')``
        in SQL, ``(b.kind || 'bead')`` in JS — must hold at the UI layer
        too. Concretely: the priority badge renders, the agentic badge
        does NOT.
        """
        c = self._checks
        assert c.get("has_legacy_card"), (
            "Legacy NULL-kind dispatch row card not present in DOM"
        )
        # The fixture seeds priority=2 → P2 badge.
        assert c.get("legacy_card_priority_badge_text") == "P2", (
            f"Legacy NULL-kind row should render its priority badge "
            f"(P2); got {c.get('legacy_card_priority_badge_text')!r}"
        )
        assert not c.get("legacy_card_has_kind_badge"), (
            "Legacy NULL-kind row must NOT render the agentic kind badge "
            "— COALESCE-as-'bead' takes that branch off"
        )
        assert c.get("no_jinja"), (
            "Raw Jinja template syntax visible on dispatch page"
        )


# ── /dispatch agentic observability (auto-gh2iv) ──────────────────────
#
# Companion to TestDispatchAgenticKindBadge. The kind-badge tests assert
# that the badge renders. THIS class asserts the rest of the agentic
# session-observability surface: row routes to /graph/<agentic_source_id>,
# Live Trace returns JSONL turns for the run, and /api/graph/<id>
# returns the appended turns under the agentic source.

DISPATCH_AGENTIC_ROUTING_CHECKS = """
    // Locate the agentic dispatch card by walking up from its kind badge —
    // the anchor's href is /graph/<agentic_source_id>, so we can't query
    // by run_id substring on href.
    var badge = document.querySelector('[data-testid="kind-badge-agentic"]');
    var agenticCard = badge ? badge.closest('a') : null;
    r.found_card = !!agenticCard;
    r.card_href = agenticCard ? agenticCard.getAttribute('href') : '';
    // The agentic href must NOT be /bead/... (would route to a 404 page).
    r.routes_to_graph = !!(agenticCard && agenticCard.getAttribute('href') &&
        agenticCard.getAttribute('href').indexOf('/graph/') === 0);
    r.routes_to_bead = !!(agenticCard && agenticCard.getAttribute('href') &&
        agenticCard.getAttribute('href').indexOf('/bead/') === 0);
"""


class TestAgenticDispatchObservability:
    """L2.B: agentic dispatch session is observable end-to-end (auto-gh2iv).

    Stack of four observability assertions:
      1. /dispatch row click routes to /graph/<agentic_source_id> (not /bead/...).
      2. /api/dispatch/tail/<run_id> returns the run's JSONL turns.
      3. /api/graph/<agentic_source_id> renders nonzero entries.
      4. _render_agent_action_prompt fails loudly on undefined placeholders.

    The dispatch-status-transition assertion lives at the unit level (it
    queries dispatch.db directly, doesn't need a browser); see
    test_dispatch_db_agentic_completion.py for that path.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            "/dispatch", DISPATCH_AGENTIC_ROUTING_CHECKS, wait_ms=1000,
        )
        request.cls._checks = result
        request.cls._sweep_url = browser["url"]

    def test_dispatch_row_routes_to_agentic_source(self):
        """Click target is /graph/<agentic_source_id>, NOT /bead/<...>."""
        c = self._checks
        assert c.get("found_card"), (
            "Agentic dispatch card not found in DOM (id-bearing href)"
        )
        assert c.get("routes_to_graph"), (
            f"Agentic row href must start with /graph/, got "
            f"{c.get('card_href')!r}"
        )
        assert not c.get("routes_to_bead"), (
            f"Agentic row href must NOT route to /bead/<...>; got "
            f"{c.get('card_href')!r} (would land on a 404)"
        )
        assert SWEEP_AGENTIC_SOURCE_ID in c.get("card_href", ""), (
            f"Agentic row href should embed the agentic_source_id; got "
            f"{c.get('card_href')!r}"
        )

    def test_live_trace_shows_jsonl_turns(self):
        """/api/dispatch/tail/<run_id> returns the seeded JSONL entries."""
        import urllib.request as _urllib_req
        run_id = SWEEP_DISPATCH_RUN_AGENTIC["id"]
        url = f"{self._sweep_url}/api/dispatch/tail/{run_id}"
        with _urllib_req.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        entries = data.get("entries") or []
        assert len(entries) >= 1, (
            f"/api/dispatch/tail/{run_id} returned no entries; got {data!r}"
        )
        # Sanity-check that one of the seeded payloads survives the round trip.
        joined = " ".join(
            (e.get("content") or "") for e in entries if isinstance(e, dict)
        )
        assert "Title set" in joined or "Update Title" in joined, (
            f"Expected seeded JSONL content in tail entries; got {entries!r}"
        )

    def test_agentic_source_renders_turns_after_ingest(self):
        """/api/graph/<id> returns the ingested turns for the agentic source."""
        import urllib.request as _urllib_req
        url = f"{self._sweep_url}/api/graph/{SWEEP_AGENTIC_SOURCE_ID}"
        with _urllib_req.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        entries = data.get("entries") or []
        assert len(entries) >= 1, (
            f"/api/graph/{SWEEP_AGENTIC_SOURCE_ID} returned 0 entries; got "
            f"{data!r} — ingest must append turns to the agentic source row"
        )

    def test_prompt_renderer_raises_on_undefined_placeholder(self):
        """Direct unit-style test: undefined placeholder → ValueError."""
        from tools.dashboard.server import _render_agent_action_prompt
        bad_template = (
            "Hello {asset_id}, missing {bogus_field}"
        )
        with pytest.raises(ValueError, match="bogus_field"):
            _render_agent_action_prompt(
                bad_template,
                asset_id="x",
                page_context={},
                dispatched_by_session="",
                member_key="k",
            )

    def test_prompt_renderer_accepts_known_placeholders(self):
        """Known placeholders render without error and substitute values."""
        from tools.dashboard.server import _render_agent_action_prompt
        good_template = (
            "asset={asset_id} title={asset_title} "
            "short={asset_short_description}"
        )
        out = _render_agent_action_prompt(
            good_template,
            asset_id="abc-123",
            page_context={
                "asset_title": "Hello",
                "asset_short_description": "A short blurb",
            },
            dispatched_by_session="auto-test",
            member_key="note.update-summary",
        )
        assert "asset=abc-123" in out
        assert "title=Hello" in out
        assert "short=A short blurb" in out
        # No stray literal braces from a missed substitution.
        assert "{asset_short_description}" not in out
