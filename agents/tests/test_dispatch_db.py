"""Tests for dispatch_db — SQLite run metadata storage."""

import json
import sqlite3
import tempfile
from pathlib import Path

import agents.dispatch_db as db


def _use_temp_db():
    """Point dispatch_db at a temp file and return its path."""
    tmp = tempfile.mktemp(suffix=".db")
    db.DB_PATH = Path(tmp)
    db.init_db()
    return tmp


def test_init_creates_table():
    tmp = _use_temp_db()
    conn = sqlite3.connect(tmp)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    conn.close()
    assert ("dispatch_runs",) in tables


def test_insert_full_decision():
    tmp = _use_temp_db()
    db.insert_run(
        run_id="auto-abc-20260316-120000",
        bead_id="auto-abc",
        started_at=1710000000.0,
        completed_at=1710000300.0,
        status="DONE",
        reason="All tests pass",
        decision={
            "status": "DONE",
            "reason": "All tests pass",
            "scores": {"tooling": 4, "clarity": 5, "confidence": 3},
            "time_breakdown": {
                "research_pct": 20,
                "coding_pct": 60,
                "debugging_pct": 15,
                "tooling_workaround_pct": 5,
            },
            "discovered_beads": [{"title": "follow-up"}],
        },
        commit_hash="",
        branch="agent/auto-abc",
        branch_base="",
        image="autonomy-agent",
        container_name="agent-auto-abc-9999",
        exit_code=0,
        output_dir="/tmp/test-run",
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dispatch_runs WHERE id = ?",
                       ("auto-abc-20260316-120000",)).fetchone()
    conn.close()

    assert row is not None
    assert row["bead_id"] == "auto-abc"
    assert row["duration_secs"] == 300
    assert row["status"] == "DONE"
    assert row["score_tooling"] == 4
    assert row["score_clarity"] == 5
    assert row["score_confidence"] == 3
    assert row["time_research_pct"] == 20
    assert row["time_coding_pct"] == 60
    assert row["discovered_beads_count"] == 1
    assert row["image"] == "autonomy-agent"


def test_insert_no_decision():
    tmp = _use_temp_db()
    db.insert_run(
        run_id="auto-xyz-20260316-130000",
        bead_id="auto-xyz",
        started_at=1710000000.0,
        completed_at=1710000060.0,
        status="FAILED",
        reason="No decision file",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="",
        container_name="",
        exit_code=1,
        output_dir="",
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dispatch_runs WHERE id = ?",
                       ("auto-xyz-20260316-130000",)).fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "FAILED"
    assert row["score_tooling"] is None
    assert row["discovered_beads_count"] == 0
    assert row["has_experience_report"] == 0


def test_insert_or_replace():
    """Duplicate run_id should replace, not error."""
    tmp = _use_temp_db()
    kwargs = dict(
        run_id="auto-dup-20260316-140000",
        bead_id="auto-dup",
        started_at=1710000000.0,
        completed_at=1710000100.0,
        status="FAILED",
        reason="first",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="",
        container_name="",
        exit_code=1,
        output_dir="",
    )
    db.insert_run(**kwargs)
    db.insert_run(**{**kwargs, "status": "DONE", "reason": "second"})

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM dispatch_runs WHERE id = ?",
                        ("auto-dup-20260316-140000",)).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["status"] == "DONE"
    assert rows[0]["reason"] == "second"


def test_get_runs_for_bead():
    """Lookup by bead_id returns runs ordered most recent first."""
    _use_temp_db()
    base = dict(
        bead_id="auto-multi",
        started_at=1710000000.0,
        status="DONE",
        reason="ok",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="",
        container_name="",
        exit_code=0,
        output_dir="",
    )
    db.insert_run(run_id="auto-multi-20260316-120000", completed_at=1710000300.0, **base)
    db.insert_run(run_id="auto-multi-20260317-120000", completed_at=1710086700.0, **base)

    runs = db.get_runs_for_bead("auto-multi")
    assert len(runs) == 2
    # Most recent first
    assert runs[0]["id"] == "auto-multi-20260317-120000"
    assert runs[1]["id"] == "auto-multi-20260316-120000"


def test_get_runs_for_bead_empty():
    """Lookup by nonexistent bead_id returns empty list."""
    _use_temp_db()
    assert db.get_runs_for_bead("nonexistent") == []


def test_insert_launch_run():
    """insert_launch_run creates a RUNNING row with launch-time fields."""
    tmp = _use_temp_db()
    db.insert_launch_run(
        run_id="auto-live-20260317-100000",
        bead_id="auto-live",
        started_at=1710000000.0,
        branch="agent/auto-live",
        branch_base="abc123",
        image="autonomy-agent",
        container_name="agent-auto-live-1234",
        output_dir="/tmp/test-live",
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dispatch_runs WHERE id = ?",
                       ("auto-live-20260317-100000",)).fetchone()
    conn.close()

    assert row is not None
    assert row["bead_id"] == "auto-live"
    assert row["status"] == "RUNNING"
    assert row["completed_at"] is None
    assert row["exit_code"] is None
    assert row["branch"] == "agent/auto-live"
    assert row["image"] == "autonomy-agent"
    assert row["output_dir"] == "/tmp/test-live"


def test_launch_then_complete():
    """insert_launch_run followed by insert_run updates the row."""
    _use_temp_db()
    run_id = "auto-lc-20260317-110000"

    # Launch
    db.insert_launch_run(
        run_id=run_id,
        bead_id="auto-lc",
        started_at=1710000000.0,
        branch="agent/auto-lc",
        branch_base="",
        image="autonomy-agent",
        container_name="agent-auto-lc-5678",
        output_dir="",
    )

    # Verify RUNNING
    row = db.get_run(run_id)
    assert row["status"] == "RUNNING"
    assert row["completed_at"] is None

    # Complete (upsert replaces the RUNNING row)
    db.insert_run(
        run_id=run_id,
        bead_id="auto-lc",
        started_at=1710000000.0,
        completed_at=1710000300.0,
        status="DONE",
        reason="All good",
        decision={"status": "DONE", "reason": "All good"},
        commit_hash="",
        branch="agent/auto-lc",
        branch_base="",
        image="autonomy-agent",
        container_name="agent-auto-lc-5678",
        exit_code=0,
        output_dir="",
    )

    # Verify updated
    row = db.get_run(run_id)
    assert row["status"] == "DONE"
    assert row["completed_at"] is not None
    assert row["reason"] == "All good"


def test_get_currently_running():
    """get_currently_running returns only RUNNING rows."""
    _use_temp_db()

    # Insert a RUNNING row
    db.insert_launch_run(
        run_id="auto-run-20260317-120000",
        bead_id="auto-run",
        started_at=1710000000.0,
        branch="agent/auto-run",
        branch_base="",
        image="autonomy-agent",
        container_name="agent-auto-run-1111",
        output_dir="",
    )
    # Insert a completed row
    db.insert_run(
        run_id="auto-done-20260317-120000",
        bead_id="auto-done",
        started_at=1710000000.0,
        completed_at=1710000300.0,
        status="DONE",
        reason="ok",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="",
        container_name="",
        exit_code=0,
        output_dir="",
    )

    running = db.get_currently_running()
    assert len(running) == 1
    assert running[0]["bead_id"] == "auto-run"
    assert running[0]["status"] == "RUNNING"


def test_list_runs_completed_only():
    """list_runs(completed_only=True) excludes RUNNING rows."""
    _use_temp_db()

    # Insert a RUNNING row
    db.insert_launch_run(
        run_id="auto-r1-20260317-130000",
        bead_id="auto-r1",
        started_at=1710000000.0,
        branch="",
        branch_base="",
        image="",
        container_name="",
        output_dir="",
    )
    # Insert a completed row
    db.insert_run(
        run_id="auto-c1-20260317-130000",
        bead_id="auto-c1",
        started_at=1710000000.0,
        completed_at=1710000300.0,
        status="DONE",
        reason="ok",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="",
        container_name="",
        exit_code=0,
        output_dir="",
    )

    # Default: returns all (including RUNNING)
    all_runs = db.list_runs()
    assert len(all_runs) == 2

    # completed_only=True: excludes RUNNING
    completed = db.list_runs(completed_only=True)
    assert len(completed) == 1
    assert completed[0]["bead_id"] == "auto-c1"


def test_insert_launch_run_ignore_duplicate():
    """insert_launch_run uses INSERT OR IGNORE — doesn't overwrite existing rows."""
    _use_temp_db()
    run_id = "auto-ign-20260317-140000"

    # Insert and complete
    db.insert_launch_run(
        run_id=run_id, bead_id="auto-ign", started_at=1710000000.0,
        branch="", branch_base="", image="", container_name="", output_dir="",
    )
    db.insert_run(
        run_id=run_id, bead_id="auto-ign", started_at=1710000000.0,
        completed_at=1710000300.0, status="DONE", reason="done",
        decision=None, commit_hash="", branch="", branch_base="",
        image="", container_name="", exit_code=0, output_dir="",
    )

    # Try to re-insert launch — should be ignored (row already exists with DONE)
    db.insert_launch_run(
        run_id=run_id, bead_id="auto-ign", started_at=1710000000.0,
        branch="", branch_base="", image="", container_name="", output_dir="",
    )

    row = db.get_run(run_id)
    assert row["status"] == "DONE"  # Not overwritten to RUNNING


def test_schema_has_live_stats_columns():
    """dispatch_runs table must have all live stats columns after init."""
    tmp = _use_temp_db()
    conn = sqlite3.connect(tmp)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dispatch_runs)").fetchall()}
    conn.close()
    for col in ("last_snippet", "token_count", "cpu_pct", "cpu_usec",
                "mem_mb", "last_activity", "jsonl_offset"):
        assert col in cols, f"Missing column: {col}"


def test_update_live_stats_basic():
    """update_live_stats sets fields on a RUNNING row."""
    _use_temp_db()
    run_id = "auto-ls-20260317-150000"
    db.insert_launch_run(
        run_id=run_id, bead_id="auto-ls", started_at=1710000000.0,
        branch="", branch_base="", image="", container_name="", output_dir="",
    )

    db.update_live_stats(
        run_id=run_id,
        last_snippet="Hello from the agent",
        token_delta=150,
        cpu_pct=12.5,
        cpu_usec=500000,
        mem_mb=64,
        last_activity="2026-03-17T15:01:00Z",
        jsonl_offset=1024,
    )

    row = db.get_run(run_id)
    assert row["last_snippet"] == "Hello from the agent"
    assert row["token_count"] == 150
    assert row["cpu_pct"] == 12.5
    assert row["cpu_usec"] == 500000
    assert row["mem_mb"] == 64
    assert row["last_activity"] == "2026-03-17T15:01:00Z"
    assert row["jsonl_offset"] == 1024


def test_update_live_stats_accumulates_tokens():
    """Calling update_live_stats multiple times accumulates token_count."""
    _use_temp_db()
    run_id = "auto-tok-20260317-160000"
    db.insert_launch_run(
        run_id=run_id, bead_id="auto-tok", started_at=1710000000.0,
        branch="", branch_base="", image="", container_name="", output_dir="",
    )

    db.update_live_stats(run_id=run_id, token_delta=100)
    db.update_live_stats(run_id=run_id, token_delta=250)
    db.update_live_stats(run_id=run_id, token_delta=50)

    row = db.get_run(run_id)
    assert row["token_count"] == 400


def test_update_live_stats_no_op_on_defaults():
    """update_live_stats with all defaults does nothing (no SQL executed)."""
    _use_temp_db()
    run_id = "auto-noop-20260317-170000"
    db.insert_launch_run(
        run_id=run_id, bead_id="auto-noop", started_at=1710000000.0,
        branch="", branch_base="", image="", container_name="", output_dir="",
    )
    # Should not raise; snippet remains None
    db.update_live_stats(run_id=run_id)
    row = db.get_run(run_id)
    assert row["last_snippet"] is None
    assert row["token_count"] is None


def test_update_live_stats_snippet_updates():
    """update_live_stats replaces last_snippet with the newest value."""
    _use_temp_db()
    run_id = "auto-snip-20260317-180000"
    db.insert_launch_run(
        run_id=run_id, bead_id="auto-snip", started_at=1710000000.0,
        branch="", branch_base="", image="", container_name="", output_dir="",
    )

    db.update_live_stats(run_id=run_id, last_snippet="first message")
    db.update_live_stats(run_id=run_id, last_snippet="second message")

    row = db.get_run(run_id)
    assert row["last_snippet"] == "second message"


def test_read_jsonl_incremental(tmp_path):
    """_read_jsonl_incremental parses snippet and tokens from JSONL."""
    from agents.dispatcher import _read_jsonl_incremental

    jsonl = tmp_path / "session.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Hello"},
         "timestamp": "2026-03-17T10:00:00Z"},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi there, I will help."}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }, "timestamp": "2026-03-17T10:00:01Z"},
    ]
    jsonl.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    snippet, token_delta, new_offset, tool_delta, turn_delta, last_activity = _read_jsonl_incremental(jsonl, 0)

    assert snippet == "Hi there, I will help."
    assert token_delta == 30
    assert new_offset == jsonl.stat().st_size
    assert last_activity == "2026-03-17T10:00:01Z"


def test_read_jsonl_incremental_offset(tmp_path):
    """_read_jsonl_incremental seeks to offset and only reads new lines."""
    from agents.dispatcher import _read_jsonl_incremental

    jsonl = tmp_path / "session.jsonl"
    first_line = json.dumps({"type": "assistant", "message": {
        "content": "old message",
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }, "timestamp": "2026-03-17T10:00:00Z"}) + "\n"
    second_line = json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "new message"}],
        "usage": {"input_tokens": 3, "output_tokens": 7},
    }, "timestamp": "2026-03-17T10:01:00Z"}) + "\n"

    jsonl.write_text(first_line + second_line)
    offset = len(first_line.encode())

    snippet, token_delta, new_offset, tool_delta, turn_delta, last_activity = _read_jsonl_incremental(
        jsonl, offset
    )

    assert snippet == "new message"
    assert token_delta == 10  # Only from second line
    assert new_offset == jsonl.stat().st_size
    assert last_activity == "2026-03-17T10:01:00Z"


def test_read_jsonl_incremental_no_new_data(tmp_path):
    """_read_jsonl_incremental returns defaults when offset is at EOF."""
    from agents.dispatcher import _read_jsonl_incremental

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(json.dumps({"type": "assistant", "message": {}}) + "\n")
    file_size = jsonl.stat().st_size

    snippet, token_delta, new_offset, tool_delta, turn_delta, last_activity = _read_jsonl_incremental(
        jsonl, file_size
    )

    assert snippet is None
    assert token_delta == 0
    assert new_offset == file_size
    assert last_activity is None
