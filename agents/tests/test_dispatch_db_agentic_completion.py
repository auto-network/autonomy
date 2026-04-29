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


def test_insert_run_preserves_agentic_source_id_round_trip():
    """Launch an agentic run with ``agentic_source_id`` set, then complete
    it via insert_run threading the column back. The INSERT OR REPLACE
    must NOT clobber the column to NULL.

    Regression for Bug B from auto-gh2iv: the completion watcher's upsert
    omitted the column, NULL'd it on every dispatch finish, and broke
    downstream JOINs against ``sources``. Host Guardian threaded the
    column through; this test pins the contract so it stays threaded.
    """
    tmp = _use_temp_db()
    src = "abc-source-id-001"

    db.insert_launch_run(
        run_id="agentic-preserve-test",
        bead_id="",
        started_at=1714340200.0,
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="agentic-preserve-test",
        output_dir="/tmp/agentic-preserve",
        kind="agentic",
        agentic_source_id=src,
    )

    # Simulate the completion watcher: SELECT the RUNNING row + thread
    # ``agentic_source_id`` back into insert_run.
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    running_row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?",
        ("agentic-preserve-test",),
    ).fetchone()
    conn.close()
    assert running_row is not None
    assert running_row["agentic_source_id"] == src, (
        "baseline: launch row must carry the agentic_source_id"
    )

    db.insert_run(
        run_id="agentic-preserve-test",
        bead_id="",
        started_at=1714340200.0,
        completed_at=1714340500.0,
        status="DONE",
        reason="",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="agentic-preserve-test",
        exit_code=0,
        output_dir="/tmp/agentic-preserve",
        kind="agentic",
        agentic_source_id=running_row["agentic_source_id"],
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    final = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?",
        ("agentic-preserve-test",),
    ).fetchone()
    conn.close()

    assert final is not None
    assert final["agentic_source_id"] == src, (
        "agentic_source_id clobbered by INSERT OR REPLACE: "
        f"{final['agentic_source_id']!r}"
    )
    assert final["status"] == "DONE"
    assert final["kind"] == "agentic"


def test_insert_run_omitted_agentic_source_id_does_not_inherit():
    """Symmetric guard: when insert_run is called WITHOUT
    ``agentic_source_id`` (e.g. a bead row), the column stays NULL —
    INSERT OR REPLACE doesn't carry it forward from any prior matching
    row. Documents the explicit-thread-through contract.
    """
    tmp = _use_temp_db()

    db.insert_launch_run(
        run_id="bead-no-source-id",
        bead_id="b-1",
        started_at=1714340600.0,
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="bead-no-source-id",
        output_dir="/tmp/bead-no-source",
        kind="bead",
    )

    db.insert_run(
        run_id="bead-no-source-id",
        bead_id="b-1",
        started_at=1714340600.0,
        completed_at=1714340700.0,
        status="DONE",
        reason="",
        decision=None,
        commit_hash="",
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="bead-no-source-id",
        exit_code=0,
        output_dir="/tmp/bead-no-source",
        kind="bead",
        # agentic_source_id intentionally omitted.
    )

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?",
        ("bead-no-source-id",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["agentic_source_id"] is None, (
        "bead rows without agentic_source_id must stay NULL — INSERT OR "
        "REPLACE must not inherit it from any other row"
    )
    assert row["status"] == "DONE"
    assert row["kind"] == "bead"
