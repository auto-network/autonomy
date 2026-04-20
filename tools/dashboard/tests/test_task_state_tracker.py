"""Unit tests for TaskStateTracker — the server-side enricher that resolves
TaskCreate/TaskUpdate tool_use entries into stable subject/status annotations.

These tests exercise the tracker through its public enrich() API, asserting
that the in-place ``todo_annotation`` dict matches the expected state after
each mutation. JSONL is the durable source of truth; these fixtures feed
parsed entries (matching ``_parse_jsonl_entry`` output) straight into the
tracker, just as the session monitor does at runtime.
"""

from __future__ import annotations

from tools.dashboard.session_monitor import TaskStateTracker


def _create(subject: str, description: str = "", activeForm: str = "") -> dict:
    """Build a parsed TaskCreate tool_use entry (shape matches _parse_jsonl_entry)."""
    return {
        "type": "tool_use",
        "tool_name": "TaskCreate",
        "tool_id": f"tu-create-{subject[:8]}",
        "input": {
            "subject": subject,
            "description": description,
            "activeForm": activeForm,
        },
    }


def _update(task_id: str, **fields) -> dict:
    return {
        "type": "tool_use",
        "tool_name": "TaskUpdate",
        "tool_id": f"tu-update-{task_id}-{fields.get('status','')}",
        "input": {"taskId": task_id, **fields},
    }


# ── Seed + sequential ids ─────────────────────────────────────────────

def test_task_create_seeds_fresh_state():
    """TaskCreate with subject/description populates the tile annotation."""
    tracker = TaskStateTracker()
    e = _create("Inspect payload shape", description="Look at the raw JSONL")
    tracker.enrich("sess-a", [e])
    ann = e["todo_annotation"]
    assert ann["action"] == "create"
    assert ann["task_id"] == "1"
    assert ann["subject"] == "Inspect payload shape"
    assert ann["description"] == "Look at the raw JSONL"
    assert ann["status"] == "pending"


def test_sequential_ids_across_creates():
    """Successive TaskCreates without explicit taskId get sequential ids."""
    tracker = TaskStateTracker()
    entries = [_create("first"), _create("second"), _create("third")]
    tracker.enrich("sess-a", entries)
    assert [e["todo_annotation"]["task_id"] for e in entries] == ["1", "2", "3"]


# ── Status transitions ────────────────────────────────────────────────

def test_task_update_applies_status_delta():
    """TaskUpdate carries only {taskId, status}; tracker resolves subject."""
    tracker = TaskStateTracker()
    create = _create("Run the tests")
    upd = _update("1", status="in_progress")
    tracker.enrich("sess-a", [create, upd])
    ann = upd["todo_annotation"]
    assert ann["action"] == "update"
    assert ann["status"] == "in_progress"
    assert ann["subject"] == "Run the tests"
    assert ann["prev_status"] == "pending"


def test_status_pending_to_in_progress_to_completed():
    tracker = TaskStateTracker()
    create = _create("Ship the feature")
    up1 = _update("1", status="in_progress")
    up2 = _update("1", status="completed")
    tracker.enrich("sess-a", [create, up1, up2])
    assert up1["todo_annotation"]["prev_status"] == "pending"
    assert up1["todo_annotation"]["status"] == "in_progress"
    assert up2["todo_annotation"]["prev_status"] == "in_progress"
    assert up2["todo_annotation"]["status"] == "completed"
    assert up2["todo_annotation"]["subject"] == "Ship the feature"


# ── Subject rename resolution ─────────────────────────────────────────

def test_subject_rename_resolved_on_later_updates():
    """A subject rename via TaskUpdate propagates to every subsequent update."""
    tracker = TaskStateTracker()
    create = _create("Inspect old thing")
    rename = _update("1", subject="Inspect new thing")
    later = _update("1", status="completed")
    tracker.enrich("sess-a", [create, rename, later])
    # The rename itself reports the new subject and flags prev_subject
    assert rename["todo_annotation"]["subject"] == "Inspect new thing"
    assert rename["todo_annotation"]["prev_subject"] == "Inspect old thing"
    # Later updates see only the new subject (old name is gone)
    assert later["todo_annotation"]["subject"] == "Inspect new thing"
    assert later["todo_annotation"]["prev_subject"] == ""


def test_update_without_changes_reports_empty_prev_subject():
    """A pure status update should not surface a prev_subject rename marker."""
    tracker = TaskStateTracker()
    create = _create("Stable subject")
    upd = _update("1", status="completed")
    tracker.enrich("sess-a", [create, upd])
    assert upd["todo_annotation"]["prev_subject"] == ""
    assert upd["todo_annotation"]["subject"] == "Stable subject"


# ── Restart replay determinism ────────────────────────────────────────

def test_replay_from_empty_matches_live_state():
    """Tailer restart replays the JSONL and produces identical annotations."""
    # Live session: entries arrive incrementally, tracker is long-lived.
    live = TaskStateTracker()
    a1 = _create("A", description="first")
    a2 = _create("B", description="second")
    a3 = _update("1", status="in_progress")
    a4 = _update("2", subject="B renamed")
    a5 = _update("1", status="completed")
    live.enrich("sess-live", [a1, a2, a3, a4, a5])
    live_annotations = [e["todo_annotation"] for e in (a1, a2, a3, a4, a5)]

    # Restart: fresh tracker, replay the same entries in the same order.
    restart = TaskStateTracker()
    b1 = _create("A", description="first")
    b2 = _create("B", description="second")
    b3 = _update("1", status="in_progress")
    b4 = _update("2", subject="B renamed")
    b5 = _update("1", status="completed")
    restart.enrich("sess-restart", [b1, b2, b3, b4, b5])
    restart_annotations = [e["todo_annotation"] for e in (b1, b2, b3, b4, b5)]

    assert live_annotations == restart_annotations


def test_reset_clears_session_state():
    """reset() drops a session's map so a subsequent replay starts at taskId=1."""
    tracker = TaskStateTracker()
    tracker.enrich("sess-a", [_create("first"), _create("second")])
    tracker.reset("sess-a")
    e = _create("reborn")
    tracker.enrich("sess-a", [e])
    assert e["todo_annotation"]["task_id"] == "1"


def test_sessions_are_isolated():
    """Two sessions with the same taskId must not cross-contaminate."""
    tracker = TaskStateTracker()
    a_create = _create("session A task")
    b_create = _create("session B task")
    a_update = _update("1", status="completed")
    b_update = _update("1", status="in_progress")
    tracker.enrich("sess-a", [a_create, a_update])
    tracker.enrich("sess-b", [b_create, b_update])
    assert a_update["todo_annotation"]["subject"] == "session A task"
    assert b_update["todo_annotation"]["subject"] == "session B task"
    assert a_update["todo_annotation"]["status"] == "completed"
    assert b_update["todo_annotation"]["status"] == "in_progress"


# ── Robustness ────────────────────────────────────────────────────────

def test_non_task_entries_are_not_annotated():
    """enrich() is safe to call on mixed entry lists — it only touches Task*."""
    tracker = TaskStateTracker()
    noise = [
        {"type": "user", "content": "hello"},
        {"type": "tool_use", "tool_name": "Bash", "input": {"command": "ls"}},
        {"type": "assistant_text", "content": "ok"},
    ]
    tracker.enrich("sess-a", noise)
    for e in noise:
        assert "todo_annotation" not in e


def test_orphan_task_update_still_annotates_gracefully():
    """TaskUpdate referencing an unknown taskId should not raise."""
    tracker = TaskStateTracker()
    orphan = _update("99", status="completed")
    tracker.enrich("sess-a", [orphan])
    ann = orphan["todo_annotation"]
    assert ann["task_id"] == "99"
    assert ann["status"] == "completed"
    # No prior state → prev_status is empty string (not None) so JS can compare safely.
    assert ann["prev_status"] == ""
    assert ann["subject"] == ""
