"""Tests for ``record_worktree_merge_run`` and the ``kind='worktree-merge'``
read path (bead auto-ecmss).

Covers:
1. The writer inserts a DONE row with the expected commit / branch /
   container_name / reason / lines fields.
2. The writer is idempotent on commit_hash — calling twice with the
   same hash leaves exactly one row.
3. The row materializes through ``api_timeline`` with kind preserved
   so the timeline UI sees it.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest


@pytest.fixture
def isolated_dispatch_env(tmp_path, monkeypatch):
    """Pin DISPATCH_DB to a tmp file and reload the writer module."""
    dispatch_db_path = tmp_path / "dispatch.db"
    monkeypatch.setenv("DISPATCH_DB", str(dispatch_db_path))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    from agents import dispatch_db as writer_mod
    importlib.reload(writer_mod)
    writer_mod.init_db()

    return {
        "dispatch_db": dispatch_db_path,
        "writer": writer_mod,
    }


def test_record_worktree_merge_writes_done_row(isolated_dispatch_env, monkeypatch):
    """Inserts a row with kind='worktree-merge', status='DONE', and the
    fields the timeline reads (commit / branch / container_name / reason).
    """
    writer = isolated_dispatch_env["writer"]
    db_path = isolated_dispatch_env["dispatch_db"]

    # Stub git diff so the writer doesn't shell out in CI.
    monkeypatch.setattr(
        writer, "_git_diff_stats_range",
        lambda _repo, _sha: (12, 3, 2),
    )

    run_id = writer.record_worktree_merge_run(
        commit_hash="abcdef0123456789",
        commit_message="Add worktree dashboard\n\nLong body",
        branch="session/auto-foo",
        branch_base="master",
        container_name="auto-foo",
        reason="ff",
        target_repo="/tmp/repo",
    )

    assert run_id == "wt-abcdef012345"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["kind"] == "worktree-merge"
    assert row["status"] == "DONE"
    assert row["bead_id"] is None
    assert row["commit_hash"] == "abcdef0123456789"
    # commit_message stores the full message (subject + body) so the
    # activity-feed card can render the body as a subtitle (auto-24a60).
    assert row["commit_message"] == "Add worktree dashboard\n\nLong body"
    assert row["branch"] == "session/auto-foo"
    assert row["branch_base"] == "master"
    assert row["container_name"] == "auto-foo"
    assert row["reason"] == "ff"
    assert row["lines_added"] == 12
    assert row["lines_removed"] == 3
    assert row["files_changed"] == 2
    # Lifecycle row carries no agent decision payload.
    assert row["score_tooling"] is None
    assert row["score_clarity"] is None
    assert row["score_confidence"] is None
    assert row["agentic_source_id"] is None
    assert row["librarian_type"] is None
    assert row["output_dir"] == ""
    # auto-614q7: callers omitting ``duration_secs`` get the legacy 0
    # placeholder; ``started_at`` and ``completed_at`` end up identical.
    assert row["duration_secs"] == 0
    assert row["started_at"] == row["completed_at"]


def test_record_worktree_merge_persists_real_duration_secs(
    isolated_dispatch_env, monkeypatch,
):
    """auto-614q7: ``record_worktree_merge_run`` accepts a measured
    ``duration_secs`` and persists it as the column value, while backing
    ``started_at`` off ``completed_at`` by that many seconds. Sub-second
    merges still round to 0 — that's the audit's flagged limitation, not
    a defect: the operator-facing ``30s+ rebase`` use case lives well
    above the rounding boundary.
    """
    writer = isolated_dispatch_env["writer"]
    db_path = isolated_dispatch_env["dispatch_db"]

    monkeypatch.setattr(
        writer, "_git_diff_stats_range",
        lambda _repo, _sha: (1, 0, 1),
    )

    run_id = writer.record_worktree_merge_run(
        commit_hash="cafebabefeedface",
        commit_message="Slow rebase",
        branch="session/auto-slow",
        branch_base="master",
        container_name="auto-slow",
        reason="cherry-pick",
        duration_secs=37,
    )
    assert run_id == "wt-cafebabefeed"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["duration_secs"] == 37
    # started_at = completed_at - 37s — both are ISO-ish strings without
    # tz suffix so parse them back into datetimes for the assertion.
    from datetime import datetime as _dt
    started = _dt.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
    completed = _dt.strptime(row["completed_at"], "%Y-%m-%d %H:%M:%S")
    assert (completed - started).total_seconds() == 37


def test_record_worktree_merge_clamps_negative_duration_secs(
    isolated_dispatch_env, monkeypatch,
):
    """A negative ``duration_secs`` (caller bug — clock went backwards,
    truncated float) is clamped to 0 instead of producing a future
    ``started_at``.
    """
    writer = isolated_dispatch_env["writer"]
    db_path = isolated_dispatch_env["dispatch_db"]

    monkeypatch.setattr(
        writer, "_git_diff_stats_range",
        lambda _repo, _sha: (0, 0, 0),
    )

    run_id = writer.record_worktree_merge_run(
        commit_hash="0000111122223333",
        commit_message="Nope",
        branch="session/auto-nope",
        branch_base="master",
        container_name="auto-nope",
        reason="ff",
        duration_secs=-5,
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["duration_secs"] == 0
    assert row["started_at"] == row["completed_at"]


def test_record_worktree_merge_is_idempotent(isolated_dispatch_env, monkeypatch):
    """Calling the writer twice with the same commit_hash yields one row."""
    writer = isolated_dispatch_env["writer"]
    db_path = isolated_dispatch_env["dispatch_db"]

    monkeypatch.setattr(
        writer, "_git_diff_stats_range",
        lambda _repo, _sha: (1, 1, 1),
    )

    first = writer.record_worktree_merge_run(
        commit_hash="deadbeefcafebabe",
        commit_message="first",
        branch="session/auto-x",
        branch_base="master",
        container_name="auto-x",
        reason="ff",
    )
    second = writer.record_worktree_merge_run(
        commit_hash="deadbeefcafebabe",
        commit_message="second",  # would clobber if not idempotent
        branch="session/auto-x",
        branch_base="master",
        container_name="auto-x",
        reason="cherry-pick",
    )
    assert first == second == "wt-deadbeefcafe"

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT id, reason, commit_message FROM dispatch_runs WHERE id = ?",
        (first,),
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    # First-write wins (INSERT OR IGNORE), so the second call is a no-op.
    assert rows[0][1] == "ff"
    assert rows[0][2] == "first"


def test_record_worktree_merge_renders_via_api_timeline(
    isolated_dispatch_env, monkeypatch,
):
    """The row flows through ``_row_to_timeline_entry`` with kind preserved.

    Goes through the in-process read path the production endpoint uses
    so the test verifies the SQL SELECT, the entry mapping, and the
    librarian / agentic enrichment chain pass worktree-merge rows
    through unchanged.
    """
    writer = isolated_dispatch_env["writer"]

    monkeypatch.setattr(
        writer, "_git_diff_stats_range",
        lambda _repo, _sha: (5, 0, 1),
    )
    writer.record_worktree_merge_run(
        commit_hash="0123456789abcdef",
        commit_message="Trim header padding",
        branch="session/auto-bar",
        branch_base="master",
        container_name="auto-bar",
        reason="commit-merge",
    )

    # Reload server so it picks up the patched DISPATCH_DB env.
    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)

    # Mimic api_timeline's read pipeline: SELECT * + _row_to_timeline_entry
    # + the librarian/agentic enrichment.
    conn = server_mod._timeline_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs ORDER BY completed_at DESC"
        ).fetchall()
        entries = [server_mod._row_to_timeline_entry(r) for r in rows]
        server_mod._enrich_with_librarian_data(conn, entries)
    finally:
        conn.close()
    server_mod._enrich_timeline_agentic(entries)

    by_id = {e["run_id"]: e for e in entries}
    entry = by_id["wt-0123456789ab"]
    assert entry["kind"] == "worktree-merge"
    assert entry["status"] == "DONE"
    assert entry["reason"] == "commit-merge"
    assert entry["commit_hash"] == "0123456789abcdef"
    assert entry["commit_message"] == "Trim header padding"
    assert entry["branch"] == "session/auto-bar"
    assert entry["container_name"] == "auto-bar"
    assert entry["lines_added"] == 5
    assert entry["lines_removed"] == 0
    assert entry["files_changed"] == 1
    # Agentic / librarian enrichment must not pollute the row.
    assert entry["agentic_source_id"] is None
    assert entry["librarian_type"] is None
    assert entry["librarian_review"] is None
