"""
Test fixture generators for the dashboard mock DAO.

Write a fixture dict, save it as JSON, set DASHBOARD_MOCK to the path.
The mock DAO reads it fresh on every request — change the file, refresh the page.
"""
import json
import uuid
from pathlib import Path


def make_session(
    session_id, label="", role="", type="container",
    last_message="", entry_count=100, context_tokens=50000,
    is_live=True, topics="[]",
):
    return {
        "session_id": session_id,
        "tmux_session": session_id,
        "label": label,
        "role": role,
        "type": type,
        "is_live": is_live,
        "last_message": last_message,
        "entry_count": entry_count,
        "context_tokens": context_tokens,
        "topics": topics,
    }


MOCK_SESSION_ENTRIES = [
    {"type": "system", "content": "Session started", "timestamp": 1700000000},
    {"type": "user", "content": "Hello, can you help me with this task?", "timestamp": 1700000010},
    {"type": "assistant_text", "content": "Of course! I'd be happy to help.", "timestamp": 1700000015},
]


def make_experiment(exp_id=None, title="Test Design", html="<h1>Test</h1>"):
    eid = exp_id or str(uuid.uuid4())
    return {
        "id": eid,
        "title": title,
        "status": "pending",
        "design_id": eid,
        "revision_seq": 1,
        "alpine": 0,
        "variants": [
            {"id": str(uuid.uuid4()), "html": html}
        ],
        "revisions": [eid],
    }


# Default experiment ID used across all test fixtures
TEST_EXPERIMENT_ID = "test-exp-00000000-0000-0000-0000-000000000001"


# ── Pre-built fixture sets ────────────────────────────────────────────

STANDARD_SESSIONS = [
    make_session("auto-test-designer", label="Test Designer — card redesign",
                 role="designer", last_message="Working on card CSS"),
    make_session("auto-test-validator", label="Test Validator",
                 role="reviewer", last_message="auto-f4p4 validated PASS"),
    make_session("auto-test-coordinator", label="Session Coordinator",
                 role="coordinator", last_message="Fleet: 4 active sessions"),
    make_session("host-test-host", label="Host: merge recovery",
                 type="host", last_message="Dolt restarted"),
    make_session("chatwith-orphan", label="",
                 last_message="orphan session"),
]


def empty_sessions():
    return {"active_sessions": [], "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def standard_sessions():
    entries = {s["session_id"]: MOCK_SESSION_ENTRIES for s in STANDARD_SESSIONS}
    return {"active_sessions": STANDARD_SESSIONS, "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)], "session_entries": entries}


def many_sessions(n=100):
    sessions = [
        make_session(f"auto-bulk-{i:04d}", label=f"Bulk Session {i}",
                     role=["designer", "builder", "reviewer", "coordinator"][i % 4],
                     last_message=f"Working on task {i}",
                     context_tokens=50000 + i * 1000)
        for i in range(n)
    ]
    return {"active_sessions": sessions, "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def single_session():
    return {"active_sessions": [
        make_session("auto-solo", label="Solo Session", role="designer",
                     last_message="Only session available")
    ], "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def no_labels():
    return {"active_sessions": [
        make_session("auto-nolabel-1", last_message="No label set"),
        make_session("auto-nolabel-2", last_message="Also no label"),
        make_session("host-nolabel", type="host", last_message="Host without label"),
    ], "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def long_labels():
    return {"active_sessions": [
        make_session("auto-long",
                     label="This is an extremely long session label that should be truncated by CSS text-overflow ellipsis and not break the card layout",
                     role="designer",
                     last_message="Preview text that is also quite long and should be handled gracefully by the UI without causing overflow or layout issues"),
    ], "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def xss_attempt():
    return {"active_sessions": [
        make_session("auto-xss", label='<script>alert("xss")</script>',
                     last_message='<img onerror="alert(1)" src=x>'),
    ], "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def all_dead():
    return {"active_sessions": [
        make_session("auto-dead-1", label="Dead Session", is_live=False),
        make_session("auto-dead-2", label="Also Dead", is_live=False),
    ], "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def only_chatwith():
    return {"active_sessions": [
        make_session("chatwith-abc123", label="", last_message="orphan"),
        make_session("chat-def456", label="", last_message="also orphan"),
    ], "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


def write_fixture(fixture_dict, path):
    """Write fixture to a JSON file for DASHBOARD_MOCK."""
    Path(path).write_text(json.dumps(fixture_dict, indent=2))
    return str(path)


# ── State-aware session generators ───────────────────────────────────
# These helpers produce sessions with the `linked` field set explicitly
# for testing the viewer state machine.  Phase 3 (auto-h4gh) renames
# linked → resolved; until then tests use `linked` to match current
# session-store.js (line 47).

def make_unresolved_session(session_id, **kwargs):
    """Host session with linked=false (no jsonl_path) for testing Unresolved state."""
    return {**make_session(session_id, type="host", **kwargs), "linked": False}


def make_dead_session(session_id, **kwargs):
    """Dead session for testing Complete state."""
    return {**make_session(session_id, is_live=False, **kwargs), "linked": True}


def make_linked_session(session_id, **kwargs):
    """Linked live session for testing Live state."""
    return {**make_session(session_id, is_live=True, **kwargs), "linked": True}


# ── Timeline fixture generators ──────────────────────────────────────

def make_timeline_entry(
    run_id="run-001", bead_id="auto-test", status="DONE",
    title="Mock task", priority=2, duration_secs=300,
    started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:05:00Z",
    **kwargs,
):
    return {
        "id": run_id, "bead_id": bead_id, "status": status,
        "title": title, "priority": priority, "duration_secs": duration_secs,
        "started_at": started_at, "completed_at": completed_at,
        **kwargs,
    }


def make_collab_note(
    note_id=None, title="Mock note", author="", project="",
    tags=None, comment_count=0, version=1,
):
    return {
        "id": note_id or f"note-{uuid.uuid4().hex[:8]}",
        "title": title, "author": author, "project": project,
        "tags": tags or [], "comment_count": comment_count, "version": version,
        "created_at": "2026-01-01T00:00:00Z",
    }


def make_thought(
    thought_id=None, content="Mock thought", status="captured",
    thread_id=None, source_id=None, turn_number=None,
):
    return {
        "id": thought_id or f"thought-{uuid.uuid4().hex[:8]}",
        "content": content, "status": status, "thread_id": thread_id,
        "source_id": source_id, "turn_number": turn_number,
        "created_at": "2026-01-01T00:00:00Z",
    }


def make_thread(
    thread_id=None, title="Mock thread", status="active",
    priority=1, capture_count=0,
):
    return {
        "id": thread_id or f"thread-{uuid.uuid4().hex[:8]}",
        "title": title, "status": status, "priority": priority,
        "capture_count": capture_count,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def make_stream(tag="pitfall", count=5, description="", last_active="2026-01-01T00:00:00Z"):
    return {"tag": tag, "count": count, "description": description, "last_active": last_active}


def make_stream_item(
    item_id=None, title="Mock stream note", author="",
    tags=None, source_type="note", preview="",
):
    return {
        "id": item_id or f"note-{uuid.uuid4().hex[:8]}",
        "title": title, "author": author, "tags": tags or [],
        "source_type": source_type, "preview": preview,
        "created_at": "2026-01-01T00:00:00Z",
    }


def make_trace(
    run_id="run-001", bead_id="auto-test", status="DONE",
    reason="Completed successfully", duration_secs=300,
    **kwargs,
):
    return {
        "id": run_id, "bead_id": bead_id, "status": status,
        "reason": reason, "duration_secs": duration_secs,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        **kwargs,
    }


def make_primer(
    bead_id="auto-test", title="Mock bead", description="Mock description",
    priority=2, status="open", pitfalls=None, provenance=None,
):
    return {
        "bead_id": bead_id, "title": title, "description": description,
        "priority": priority, "status": status,
        "pitfalls": pitfalls or [], "provenance": provenance or [],
        "similar_beads": [],
    }


# ── Comprehensive fixture set (all page types) ───────────────────────

def full_fixture():
    """Fixture with data for every page — timeline, collab, thoughts, threads, streams, traces, primers."""
    sessions = STANDARD_SESSIONS
    entries = {s["session_id"]: MOCK_SESSION_ENTRIES for s in sessions}
    beads = [
        {"id": "auto-test1", "title": "Test bead one", "priority": 1, "status": "open", "labels": []},
        {"id": "auto-test2", "title": "Test bead two", "priority": 2, "status": "in_progress", "labels": ["readiness:approved"]},
    ]
    runs = [
        make_timeline_entry("run-001", "auto-test1", "DONE", "Test task one", duration_secs=300),
        make_timeline_entry("run-002", "auto-test2", "FAILED", "Test task two", duration_secs=120, reason="Tests failed"),
        make_timeline_entry("run-003", "auto-test1", "BLOCKED", "Blocked task", duration_secs=60, reason="Missing dep"),
    ]
    return {
        "active_sessions": sessions,
        "session_entries": entries,
        "beads": beads,
        "experiments": [make_experiment(TEST_EXPERIMENT_ID)],
        "runs": runs,
        "timeline_entries": runs,
        "collab_notes": [
            make_collab_note(title="Architecture decision", tags=["collab", "architecture"]),
            make_collab_note(title="Testing strategy", tags=["collab", "testing"], comment_count=3),
        ],
        "thoughts": [
            make_thought(content="Auth needs passkeys"),
            make_thought(content="Consider Alpine.js migration", thread_id="thread-001"),
        ],
        "threads": [
            make_thread("thread-001", "Passkey auth design", capture_count=2),
            make_thread("thread-002", "Performance optimization", status="resolved"),
        ],
        "streams": [
            make_stream("pitfall", 12, "Operational hazards"),
            make_stream("architecture", 8, "Design decisions"),
            make_stream("testing", 5, "Testing strategies"),
        ],
        "stream_items": {
            "pitfall": [
                make_stream_item(title="Mock DAO doesn't cover timeline", tags=["pitfall", "dashboard"]),
                make_stream_item(title="SSE events need initial broadcast", tags=["pitfall", "sse"]),
            ],
        },
        "traces": {
            "run-001": make_trace("run-001", "auto-test1", "DONE", "Completed", 300),
        },
        "primers": {
            "auto-test1": make_primer("auto-test1", "Test bead one", "First test bead"),
        },
        "bead_deps": {
            "auto-test2": {"blockers": [{"id": "auto-test1", "title": "Test bead one"}], "dependents": []},
        },
        "search_results": [
            {"id": "src-001", "source_id": "src-001", "title": "Auth design doc", "type": "note", "rank": 1.0, "snippet": "Passkey authentication"},
        ],
        "graph_sources": {
            "src-001": {"id": "src-001", "title": "Auth design doc", "type": "note", "content": "Full auth design document content"},
        },
    }
