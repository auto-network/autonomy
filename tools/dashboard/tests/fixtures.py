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


def make_experiment(exp_id=None, title="Test Experiment", html="<h1>Test</h1>"):
    eid = exp_id or str(uuid.uuid4())
    return {
        "id": eid,
        "title": title,
        "status": "pending",
        "series_id": eid,
        "series_seq": 1,
        "alpine": 0,
        "variants": [
            {"id": str(uuid.uuid4()), "html": html}
        ],
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
    return {"active_sessions": STANDARD_SESSIONS, "beads": [], "experiments": [make_experiment(TEST_EXPERIMENT_ID)]}


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
