"""Tests for agentic dispatch_runs completion + render-failure recording.

Covers auto-gh2iv:

  * ``record_dispatch_failure`` flips a RUNNING row to FAILED with a
    populated ``failure_class`` and ``reason`` (used by the prompt
    renderer's static-check guard).

  * ``insert_run`` works for ``kind='agentic'`` rows that have no
    ``bead_id``. The agentic completion watcher writes via the same
    helper as the bead path; the lookup is keyed by ``id`` (run_id ==
    container_name) — never by ``bead_id``.
"""

import sqlite3
import tempfile
from pathlib import Path

import agents.dispatch_db as db


def _use_temp_db():
    tmp = tempfile.mktemp(suffix=".db")
    db.DB_PATH = Path(tmp)
    db.init_db()
    return tmp


def test_record_dispatch_failure_marks_row_failed():
    """A RUNNING row + record_dispatch_failure → FAILED + failure_class set."""
    tmp = _use_temp_db()

    db.insert_launch_run(
        run_id="agentic-render-fail-test",
        bead_id="",
        started_at=1714340000.0,
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="agentic-render-fail-test",
        output_dir="/tmp/agentic-render-fail",
        kind="agentic",
        agentic_source_id="src-render-fail-001",
    )

    db.record_dispatch_failure(
        "agentic-render-fail-test",
        failure_class="prompt_render_error",
        reason=(
            "prompt template references undefined placeholder(s) "
            "['bogus_field']"
        ),
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?",
        ("agentic-render-fail-test",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "FAILED"
    assert row["failure_class"] == "prompt_render_error"
    assert "bogus_field" in (row["reason"] or "")
    assert row["completed_at"] is not None


def test_record_dispatch_failure_missing_run_id_is_safe():
    """Empty run_id is a no-op (used as a defensive guard)."""
    _use_temp_db()
    # Should not raise.
    db.record_dispatch_failure("", failure_class="x", reason="y")


def test_insert_run_agentic_kind_no_bead_id():
    """insert_run with kind='agentic' and bead_id='' produces a valid row.

    The agentic completion watcher (poll_and_collect_agentic) calls
    insert_run this way. The row must persist with status DONE,
    completed_at populated, and bead_id stored as empty/NULL — never
    raised, never silently coerced into a bead row.
    """
    tmp = _use_temp_db()

    db.insert_launch_run(
        run_id="agentic-completion-test",
        bead_id="",
        started_at=1714340100.0,
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="agentic-completion-test",
        output_dir="/tmp/agentic-completion",
        kind="agentic",
        agentic_source_id="src-completion-001",
    )

    db.insert_run(
        run_id="agentic-completion-test",
        bead_id="",
        started_at=1714340100.0,
        completed_at=1714340400.0,
        status="DONE",
        reason="",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="agentic-completion-test",
        exit_code=0,
        output_dir="/tmp/agentic-completion",
        kind="agentic",
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?",
        ("agentic-completion-test",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "DONE"
    assert row["kind"] == "agentic"
    # bead_id is empty string or NULL — both acceptable for an agentic row.
    assert (row["bead_id"] or "") == ""
    assert row["completed_at"] is not None
    assert row["duration_secs"] == 300
    assert row["exit_code"] == 0
