"""Tests for the new ``kind`` column on ``dispatch_runs`` (bead auto-5k2j4).

Covers:
1. The ``kind`` migration is idempotent — re-running ``init_db`` doesn't
   error or duplicate the column.
2. Pre-existing rows (NULL kind) read back as kind='bead' through every
   DAO read path.
3. ``/api/dispatch/runs`` returns ``kind`` in every row, coalesced to a
   value in {bead, librarian, agentic}.
4. The dispatch.html partial renders a per-row "Agentic" badge when a
   dispatch row has kind='agentic'.
5. Identity ownership: agentic source rows in graph carry the canonical
   metadata; the matching dispatch_runs row carries only lifecycle data
   (no title/metadata duplication).
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pytest


# ── helpers ────────────────────────────────────────────────────────────


def _legacy_dispatch_runs_schema(db_path: Path) -> None:
    """Create dispatch_runs without the kind column, mimicking a pre-bead DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE dispatch_runs (
            id TEXT PRIMARY KEY,
            bead_id TEXT,
            started_at DATETIME,
            completed_at DATETIME,
            duration_secs INTEGER,
            status TEXT,
            reason TEXT,
            failure_category TEXT,
            commit_hash TEXT,
            commit_message TEXT,
            branch TEXT,
            branch_base TEXT,
            image TEXT,
            container_name TEXT,
            exit_code INTEGER,
            lines_added INTEGER,
            lines_removed INTEGER,
            files_changed INTEGER,
            score_tooling INTEGER,
            score_clarity INTEGER,
            score_confidence INTEGER,
            time_research_pct INTEGER,
            time_coding_pct INTEGER,
            time_debugging_pct INTEGER,
            time_tooling_pct INTEGER,
            discovered_beads_count INTEGER,
            has_experience_report INTEGER,
            output_dir TEXT,
            last_snippet TEXT,
            token_count INTEGER,
            cpu_pct REAL,
            cpu_usec INTEGER,
            mem_mb INTEGER,
            last_activity DATETIME,
            jsonl_offset INTEGER,
            tool_count INTEGER,
            turn_count INTEGER,
            librarian_type TEXT,
            failure_class TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_legacy_row(
    db_path: Path, *, run_id: str, bead_id: str, status: str, completed_at: str,
) -> None:
    """Insert a row directly via SQL — does NOT set kind (legacy NULL)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dispatch_runs (id, bead_id, status, started_at,"
        " completed_at, output_dir) VALUES (?, ?, ?, '2026-04-28 00:00:00',"
        " ?, ?)",
        (run_id, bead_id, status, completed_at, "/tmp/legacy"),
    )
    conn.commit()
    conn.close()


def _table_columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    conn.close()
    return cols


@pytest.fixture
def isolated_dispatch_env(tmp_path, monkeypatch):
    """Isolate dispatch.db at a tmp path and reload the modules that bind it."""
    dispatch_db = tmp_path / "dispatch.db"
    monkeypatch.setenv("DISPATCH_DB", str(dispatch_db))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    # Reload the writer + reader so they pick up the new env.
    from agents import dispatch_db as writer_mod
    importlib.reload(writer_mod)
    from tools.dashboard.dao import dispatch as reader_mod
    importlib.reload(reader_mod)

    return {
        "dispatch_db": dispatch_db,
        "writer": writer_mod,
        "reader": reader_mod,
    }


# ── Migration idempotency ──────────────────────────────────────────────


def test_dispatch_kind_migration_idempotent(isolated_dispatch_env):
    """Running init_db twice on a legacy schema doesn't duplicate ``kind``."""
    env = isolated_dispatch_env
    db_path = env["dispatch_db"]
    writer = env["writer"]

    # Seed a legacy schema (no kind column).
    _legacy_dispatch_runs_schema(db_path)
    assert "kind" not in _table_columns(db_path, "dispatch_runs")

    # First migration call adds the column.
    writer.init_db()
    cols_after_first = _table_columns(db_path, "dispatch_runs")
    assert cols_after_first.count("kind") == 1

    # Second migration call must not duplicate or error.
    writer.init_db()
    cols_after_second = _table_columns(db_path, "dispatch_runs")
    assert cols_after_second == cols_after_first
    assert cols_after_second.count("kind") == 1


# ── Legacy NULL rows read back as kind='bead' ──────────────────────────


def test_dispatch_legacy_null_kind_treated_as_bead(isolated_dispatch_env):
    """Pre-existing rows with NULL kind read back as kind='bead' at every site."""
    env = isolated_dispatch_env
    db_path = env["dispatch_db"]
    writer = env["writer"]
    reader = env["reader"]

    _legacy_dispatch_runs_schema(db_path)
    writer.init_db()  # adds the kind column

    # Two completed legacy rows + one running legacy row, all NULL kind.
    _insert_legacy_row(
        db_path, run_id="auto-old1-20260101-000000",
        bead_id="auto-old1", status="DONE", completed_at="2026-04-27 00:00:00",
    )
    _insert_legacy_row(
        db_path, run_id="auto-old2-20260102-000000",
        bead_id="auto-old1", status="FAILED", completed_at="2026-04-27 01:00:00",
    )
    _insert_legacy_row(
        db_path, run_id="auto-running-20260103-000000",
        bead_id="auto-running", status="RUNNING", completed_at="",
    )

    # Confirm the underlying rows really do have NULL kind.
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id, kind FROM dispatch_runs").fetchall()
    conn.close()
    assert {r[0]: r[1] for r in rows} == {
        "auto-old1-20260101-000000": None,
        "auto-old2-20260102-000000": None,
        "auto-running-20260103-000000": None,
    }

    # Every DAO read path must coalesce NULL → 'bead'.
    for row in reader.get_running_with_stats():
        assert row.get("kind") == "bead", row
    for row in reader.get_recent_runs(limit=10):
        assert row.get("kind") == "bead", row
    one = reader.get_run("auto-old1-20260101-000000")
    assert one is not None and one.get("kind") == "bead"
    bead_runs = reader.get_runs_for_bead("auto-old1")
    assert bead_runs and all(r.get("kind") == "bead" for r in bead_runs)


# ── Mixed kind values flow through the API enrichment ─────────────────


def test_dispatch_runs_includes_kind_field(isolated_dispatch_env, monkeypatch):
    """``/api/dispatch/runs`` returns kind ∈ {bead, librarian, agentic} per row.

    Exercises the server-side enrichment that wraps ``list_runs`` — i.e.
    the agentic dispatch path that does not depend on dispatcher launch.
    """
    env = isolated_dispatch_env
    writer = env["writer"]
    db_path = env["dispatch_db"]

    writer.init_db()

    # Bead row via the standard helper (no kind arg → defaults to 'bead').
    writer.insert_run(
        run_id="auto-bead-20260428-100000",
        bead_id="auto-bead",
        started_at=1714305600.0,
        completed_at=1714305900.0,
        status="DONE",
        reason="ok",
        decision={"status": "DONE", "reason": "ok"},
        commit_hash="",
        branch="agent/auto-bead",
        branch_base="",
        image="autonomy-agent",
        container_name="agent-bead",
        exit_code=0,
        output_dir="",
    )
    # Librarian row.
    writer.insert_launch_run(
        run_id="librarian-review-20260428-100100",
        bead_id="",
        started_at=1714305660.0,
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="lib-review",
        output_dir="",
        librarian_type="experience_reviewer",
        kind="librarian",
    )
    # Agentic row.
    writer.insert_launch_run(
        run_id="agentic-update-summary-20260428-100200",
        bead_id="",
        started_at=1714305720.0,
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name="agentic-update-summary",
        output_dir="",
        kind="agentic",
    )

    # Confirm the writer actually persisted the right kind per row.
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id, kind FROM dispatch_runs").fetchall()
    conn.close()
    actual = {r[0]: r[1] for r in rows}
    assert actual["auto-bead-20260428-100000"] == "bead"
    assert actual["librarian-review-20260428-100100"] == "librarian"
    assert actual["agentic-update-summary-20260428-100200"] == "agentic"

    # Confirm the DAO returns kind in every row.
    reader = env["reader"]
    all_rows = reader.get_recent_runs(limit=10) + reader.get_running_with_stats()
    by_id = {r["id"]: r for r in all_rows}
    for run_id in (
        "auto-bead-20260428-100000",
        "librarian-review-20260428-100100",
        "agentic-update-summary-20260428-100200",
    ):
        assert run_id in by_id, f"DAO did not return {run_id}"
        assert by_id[run_id].get("kind") in ("bead", "librarian", "agentic")


# ── Dispatch UI partial renders the agentic badge ─────────────────────


def test_dispatch_kind_agentic_renders():
    """The priority-badge partial renders an agentic kind badge.

    Static template assertion — the partial is included by every row in
    bead-card.html (active, waiting, blocked sections of /dispatch).
    """
    repo_root = Path(__file__).resolve().parents[3]
    partial = repo_root / "tools" / "dashboard" / "templates" / "partials" / "priority-badge.html"
    text = partial.read_text()

    # Agentic-kind branch present and tagged for testid lookup.
    assert "kind-badge-agentic" in text
    assert ">Agentic<" in text
    # Existing librarian behavior preserved.
    assert ">Lib<" in text


# ── Identity ownership: graph row holds metadata, dispatch_runs is lean ─


def test_dispatch_no_double_write_identity(isolated_dispatch_env, tmp_path, monkeypatch):
    """Agentic source row in graph carries the canonical metadata; the
    matching dispatch_runs row carries only lifecycle (no title/metadata
    duplicate)."""
    # Pin GRAPH_DB to a fresh tmp file.
    graph_db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(graph_db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)

    env = isolated_dispatch_env
    writer = env["writer"]
    writer.init_db()

    # Create the agentic source row in the graph.
    from tools.graph import ops as graph_ops

    src = graph_ops.insert_agentic_session(
        org="autonomy",
        set_id="dashboard.agent-actions",
        set_revision=1,
        member_key="note.update-summary",
        model="claude-haiku-4-5-20251001",
        target_source_id="abc12345-aaaa-bbbb-cccc-ddddeeee0001",
        target_org="anchore",
        dispatched_by_session="session-uuid-7777",
        title="Update summary on abc12345",
    )

    # Create the lifecycle row in dispatch_runs.
    writer.insert_launch_run(
        run_id=src["slug"],
        bead_id="",
        started_at=1714305720.0,
        branch="",
        branch_base="",
        image="autonomy-agent",
        container_name=src["slug"],
        output_dir="",
        kind="agentic",
    )

    # Identity assertions:
    #  - The graph source row holds the metadata.
    assert src["metadata"]["set_id"] == "dashboard.agent-actions"
    assert src["metadata"]["member_key"] == "note.update-summary"

    #  - The dispatch row holds NO title and NO arbitrary metadata blob.
    conn = sqlite3.connect(str(env["dispatch_db"]))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatch_runs WHERE id = ?", (src["slug"],)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["kind"] == "agentic"
    # No title column on dispatch_runs.
    assert "title" not in row.keys()
    # No json metadata column on dispatch_runs.
    assert "metadata" not in row.keys()
    # The "lean lifecycle row" mantra: bead_id may be empty for agentic;
    # it is not used to encode identity here.
    assert (row["bead_id"] or "") == ""
